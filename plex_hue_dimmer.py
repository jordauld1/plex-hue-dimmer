"""Dim Philips Hue lights while Plex is playing, and put them back afterwards.

Plex posts a webhook on play/pause/resume/stop. This service turns those into
gentle brightness changes on one Hue room or zone, through the bridge's local
API (no cloud). It only ever changes brightness: lights that are off stay off,
lights already at or below the dim level are left alone, and a light you
adjust by hand while something is playing is yours again until the next
episode or movie starts.
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

log = logging.getLogger("plex-hue-dimmer")

# Hue reports brightness as a float percentage that lands a little off what was
# requested (ask for 15, read back 14.96), so "is it still what we set?" needs
# some slack. Wide enough for that rounding, narrow enough that a press of the
# dimmer switch (about 15-20% per step) always counts as a manual change.
TOLERANCE = 2.0

# The bridge's readings trail the light itself by a few seconds: 3s after a
# 2s fade from 15% to 50% has been seen to still read 42%. Until this long
# after a fade ends, any reading along the fade's path still counts as ours.
FADE_SLACK = 10.0

PLAYING, PAUSED = "playing", "paused"


def _env_list(name):
    return {v.strip().casefold() for v in os.environ.get(name, "").split(",") if v.strip()}


def _env_float(name, default):
    return float(os.environ.get(name, default))


@dataclass
class Config:
    bridge: str = ""
    app_key: str = ""
    group: str = ""
    players: set = field(default_factory=set)
    users: set = field(default_factory=set)
    media_types: set = field(default_factory=lambda: {"movie", "episode"})
    dim_level: float = 15
    pause_level: float = 50
    pause_delay: float = 5
    stop_delay: float = 3
    finished_delay: float = 15
    dim_transition: float = 3
    pause_transition: float = 2
    restore_transition: float = 4
    stale_hours: float = 8
    webhook_token: str = ""
    port: int = 8095
    state_file: str = "/data/state.json"

    @classmethod
    def from_env(cls):
        return cls(
            bridge=os.environ.get("HUE_BRIDGE", ""),
            app_key=os.environ.get("HUE_APP_KEY", ""),
            group=os.environ.get("HUE_GROUP", ""),
            players=_env_list("PLEX_PLAYERS"),
            users=_env_list("PLEX_USERS"),
            media_types=_env_list("MEDIA_TYPES") or {"movie", "episode"},
            dim_level=_env_float("DIM_LEVEL", 15),
            pause_level=_env_float("PAUSE_LEVEL", 50),
            pause_delay=_env_float("PAUSE_DELAY", 5),
            stop_delay=_env_float("STOP_DELAY", 3),
            finished_delay=_env_float("FINISHED_DELAY", 15),
            dim_transition=_env_float("DIM_TRANSITION", 3),
            pause_transition=_env_float("PAUSE_TRANSITION", 2),
            restore_transition=_env_float("RESTORE_TRANSITION", 4),
            stale_hours=_env_float("STALE_HOURS", 8),
            webhook_token=os.environ.get("WEBHOOK_TOKEN", ""),
            port=int(os.environ.get("PORT", 8095)),
            state_file=os.environ.get("STATE_FILE", "/data/state.json"),
        )

    def wants_player(self, player):
        """Match on the player's uuid or its name, as Plex reports them."""
        ids = {str(player.get("uuid", "")).casefold(), str(player.get("title", "")).casefold()}
        return bool(ids & self.players)


class Hue:
    """The few calls we need from the bridge's local CLIP v2 API."""

    def __init__(self, session, bridge, app_key):
        self.session = session
        self.base = f"https://{bridge}/clip/v2/resource"
        self.headers = {"hue-application-key": app_key}

    async def _get(self, path):
        # The bridge's certificate is signed by Signify's own CA for a hostname
        # equal to the bridge id, so ordinary verification can't pass. It's a
        # LAN-only hop to a fixed address.
        async with self.session.get(f"{self.base}/{path}", headers=self.headers, ssl=False) as r:
            r.raise_for_status()
            return (await r.json())["data"]

    async def groups(self):
        return await self._get("room") + await self._get("zone")

    async def group_lights(self, name):
        """Light ids and names in the room or zone called `name`.

        With no name, the only room/zone on the bridge is used.
        """
        groups = await self.groups()
        names = [g["metadata"]["name"] for g in groups]
        if name:
            found = [g for g in groups if g["metadata"]["name"].casefold() == name.casefold()]
        else:
            found = groups if len(groups) == 1 else []
        if not found:
            raise SystemExit(f"Set HUE_GROUP to one of: {', '.join(names) or '(no rooms or zones)'}")
        ids, devices = [], None
        for child in found[0]["children"]:
            if child["rtype"] == "light":  # zones list lights directly
                ids.append(child["rid"])
            elif child["rtype"] == "device":  # rooms list devices
                if devices is None:
                    devices = {d["id"]: d for d in await self._get("device")}
                ids += [s["rid"] for s in devices[child["rid"]]["services"] if s["rtype"] == "light"]
        lights = {l["id"]: l["metadata"]["name"] for l in await self._get("light")}
        return {i: lights.get(i, i) for i in ids}

    async def get_light(self, light_id):
        """(on, brightness percentage) for one light."""
        data = (await self._get(f"light/{light_id}"))[0]
        return data["on"]["on"], data.get("dimming", {}).get("brightness", 100.0)

    async def set_brightness(self, light_id, brightness, transition):
        body = {"dimming": {"brightness": brightness},
                "dynamics": {"duration": int(transition * 1000)}}
        async with self.session.put(f"{self.base}/light/{light_id}", headers=self.headers,
                                    json=body, ssl=False) as r:
            r.raise_for_status()


class Dimmer:
    """Turns player events into light changes.

    The lights follow the combined state of the watched players: dim while any
    is playing, part-way up shortly after everything pauses, and back to how
    they were once everything stops. Changes that loosen the lights wait
    (pause_delay, stop_delay) and are cancelled if playback comes back first.

    Stopping after watching to the end waits longer (finished_delay), because
    that's what autoplay looks like: Plex sends stop for one episode and then
    play for the next. "Watched to the end" is Plex's scrobble event, sent once
    about 90% of an item has played.

    `owned` holds the lights we've dimmed: their brightness before we touched
    them, what we last set, and when that transition finishes. A light leaves
    `owned` when it no longer reads back as what we set, meaning someone
    changed it, and then we never restore it over their choice.
    """

    def __init__(self, hue, cfg, lights):
        self.hue = hue
        self.cfg = cfg
        self.lights = lights  # id -> name
        self.players = {}  # player uuid -> {"state": PLAYING|PAUSED, "seen": epoch, "finished": bool}
        self.owned = {}  # light id -> {"orig", "set", "until"}
        self.mode = "idle"  # what the lights currently show: idle / dim / pause
        self._lock = asyncio.Lock()
        self._timer = None
        self._state_path = Path(cfg.state_file)

    # -- events ---------------------------------------------------------------

    async def handle(self, event, player):
        async with self._lock:
            if event == "media.play":  # a new item, not yet watched to the end
                self.players[player] = {"state": PLAYING, "seen": time.time()}
            elif event in ("media.resume", "media.pause"):
                entry = self.players.setdefault(player, {})
                entry["state"] = PLAYING if event == "media.resume" else PAUSED
                entry["seen"] = time.time()
            elif event == "media.scrobble":
                if player in self.players:
                    self.players[player]["finished"] = True
                    self._save()
                return
            elif event == "media.stop":
                finished = self.players.pop(player, {}).get("finished", False)
                await self._settle(grab=False, finished=finished)
                return
            else:
                return
            # Only the start of a new item (or playback from idle) dims lights
            # we don't already hold. Resuming leaves alone a light you turned
            # up during the pause.
            grab = event == "media.play" or self.mode == "idle"
            await self._settle(grab)

    async def expire_stale(self):
        """Forget players that went quiet without a stop (crashed client, lost network)."""
        async with self._lock:
            cutoff = time.time() - self.cfg.stale_hours * 3600
            stale = [p for p, s in self.players.items() if s["seen"] < cutoff]
            if not stale:
                return
            for p in stale:
                log.info("No word from player %s in %gh; treating it as stopped", p, self.cfg.stale_hours)
                del self.players[p]
            await self._settle(grab=False)

    def _target(self):
        states = {s["state"] for s in self.players.values()}
        if PLAYING in states:
            return "dim"
        if PAUSED in states:
            return "pause"
        return "idle"

    async def _settle(self, grab, finished=False):
        """Move toward the target mode: dim at once, loosen after a delay."""
        self._cancel_timer()
        target = self._target()
        if target == "dim":
            await self._apply("dim", grab)
        elif target != self.mode:
            self._schedule(target, finished)
        self._save()

    def _schedule(self, target, finished=False):
        if target == "pause":
            delay = self.cfg.pause_delay
        else:
            delay = self.cfg.finished_delay if finished else self.cfg.stop_delay
        self._timer = asyncio.create_task(self._later(delay, target))

    def _cancel_timer(self):
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    async def _later(self, delay, mode):
        await asyncio.sleep(delay)
        async with self._lock:
            self._timer = None
            if self._target() == mode:
                await self._apply(mode, grab=False)
                self._save()

    # -- lights ---------------------------------------------------------------

    def _level(self, mode, orig):
        if mode == "dim":
            return self.cfg.dim_level
        if mode == "pause":
            # Part-way up, but never brighter than the light was to begin with.
            return min(self.cfg.pause_level, orig)
        return orig

    def _transition(self, mode):
        return {"dim": self.cfg.dim_transition, "pause": self.cfg.pause_transition}.get(
            mode, self.cfg.restore_transition)

    async def _apply(self, mode, grab):
        transition = self._transition(mode)
        for light, name in self.lights.items():
            try:
                on, bri = await self.hue.get_light(light)
            except Exception as e:
                log.warning("Couldn't read %s: %s", name, e)
                continue
            held = self.owned.get(light)
            if held is not None and not self._still_ours(held, on, bri):
                log.info("%s was changed by hand (now %s); leaving it alone",
                         name, f"{bri:.0f}%" if on else "off")
                del self.owned[light]
                held = None
            if held is None:
                if not (grab and mode == "dim"):
                    continue
                if not on:
                    log.info("%s is off; leaving it off", name)
                    continue
                if bri <= self.cfg.dim_level + TOLERANCE:
                    log.info("%s is already dim (%.0f%%); leaving it", name, bri)
                    continue
                held = self.owned[light] = {"orig": bri}
            level = self._level(mode, held["orig"])
            if abs(level - bri) > 0.5:
                try:
                    await self.hue.set_brightness(light, level, transition)
                except Exception as e:
                    log.warning("Couldn't set %s: %s", name, e)
                    continue
                log.info("%s: %.0f%% -> %.0f%% (%s)", name, bri, level, mode)
            # Remember the span this fade covers. If the previous fade's
            # readings may still be arriving, its span counts too.
            now = time.time()
            lo, hi = min(bri, level), max(bri, level)
            if now < held.get("until", 0):
                lo, hi = min(lo, held.get("lo", lo)), max(hi, held.get("hi", hi))
            held.update(set=level, lo=lo, hi=hi, until=now + transition + FADE_SLACK)
        if mode == "idle":
            self.owned.clear()
        self.mode = mode

    @staticmethod
    def _still_ours(held, on, bri):
        if not on:
            return False
        if abs(bri - held.get("set", held["orig"])) <= TOLERANCE:
            return True
        # Mid-fade, or the bridge hasn't caught up yet: fine if it's on the way.
        return (time.time() < held.get("until", 0)
                and held.get("lo", 0) - TOLERANCE <= bri <= held.get("hi", 100) + TOLERANCE)

    # -- persistence ----------------------------------------------------------
    # A restart mid-movie must not forget the brightness to restore.

    def _save(self):
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"players": self.players, "owned": self.owned, "mode": self.mode}))
            tmp.replace(self._state_path)
        except OSError as e:
            log.warning("Couldn't save state: %s", e)

    def load(self):
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        self.players = data.get("players", {})
        self.owned = {k: v for k, v in data.get("owned", {}).items() if k in self.lights}
        self.mode = data.get("mode", "idle")
        log.info("Restored state: mode=%s, %d player(s), %d light(s) held",
                 self.mode, len(self.players), len(self.owned))
        target = self._target()
        if target != self.mode and target != "dim":
            self._schedule(target)


# -- web --------------------------------------------------------------------------


def _position(meta):
    """' at 21:40/23:40' when Plex says where playback is, else ''."""
    def mmss(ms):
        return f"{int(ms) // 60000}:{int(ms) // 1000 % 60:02d}"
    if "viewOffset" in meta and "duration" in meta:
        return f" at {mmss(meta['viewOffset'])}/{mmss(meta['duration'])}"
    return ""


async def read_payload(request):
    """The JSON part of a Plex webhook.

    Play and resume webhooks also carry the poster as a JPEG part. Reading the
    form the usual way would spool that to a temp file (there is none on a
    read-only container) and trip the 1MB body limit, so stream past it.
    """
    if not request.content_type.startswith("multipart/"):
        raise ValueError("not multipart")
    reader = await request.multipart()
    async for part in reader:
        if part.name == "payload":
            return json.loads(await part.text())
        await part.release()
    raise ValueError("no payload part")


def make_app(cfg, dimmer):
    async def webhook(request):
        if cfg.webhook_token and request.query.get("token") != cfg.webhook_token:
            return web.Response(status=403)
        try:
            payload = await read_payload(request)
        except ValueError:
            return web.Response(status=400, text="expected a Plex webhook")
        event = payload.get("event", "")
        player = payload.get("Player") or {}
        meta = payload.get("Metadata") or {}
        account = (payload.get("Account") or {}).get("title", "")
        if not event.startswith("media."):
            return web.Response(text="ignored")
        watched = cfg.wants_player(player)
        log.log(logging.INFO if watched or not cfg.players else logging.DEBUG,
                "%s on %r (uuid %s) by %r: %s %r%s", event, player.get("title"), player.get("uuid"),
                account, meta.get("type"), meta.get("title"), _position(meta))
        if (watched and meta.get("type") in cfg.media_types
                and (not cfg.users or account.casefold() in cfg.users)):
            # Reply straight away; Plex doesn't need to wait on the lights.
            asyncio.create_task(dimmer.handle(event, player["uuid"]))
        return web.Response(text="ok")

    async def state(_):
        return web.json_response({"mode": dimmer.mode, "players": dimmer.players,
                                  "owned": dimmer.owned, "lights": dimmer.lights})

    app = web.Application()
    app.router.add_post("/webhook", webhook)
    app.router.add_get("/state", state)
    return app


async def run(cfg):
    if not cfg.bridge or not cfg.app_key:
        raise SystemExit("HUE_BRIDGE and HUE_APP_KEY must be set (run `pair` to get a key)")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        hue = Hue(session, cfg.bridge, cfg.app_key)
        lights = await hue.group_lights(cfg.group)
        log.info("Controlling: %s", ", ".join(lights.values()))
        if not cfg.players:
            log.warning("PLEX_PLAYERS is empty: logging webhooks only. Play something and "
                        "copy the player's name or uuid from the log.")
        dimmer = Dimmer(hue, cfg, lights)
        dimmer.load()
        runner = web.AppRunner(make_app(cfg, dimmer))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
        log.info("Listening for Plex on :%d/webhook", cfg.port)
        while True:
            await asyncio.sleep(600)
            await dimmer.expire_stale()


async def pair(bridge):
    """Make an app key. Press the bridge's link button first."""
    async with aiohttp.ClientSession() as session:
        async with session.post(f"https://{bridge}/api", ssl=False,
                                json={"devicetype": "plex-hue-dimmer#docker"}) as r:
            reply = (await r.json(content_type=None))[0]
    if "success" in reply:
        print(reply["success"]["username"])
    else:
        raise SystemExit(reply.get("error", {}).get("description", reply))


async def groups(cfg):
    async with aiohttp.ClientSession() as session:
        for g in await Hue(session, cfg.bridge, cfg.app_key).groups():
            print(f"{g['type']}: {g['metadata']['name']}")


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    cfg = Config.from_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "pair":
        asyncio.run(pair(cfg.bridge))
    elif cmd == "groups":
        asyncio.run(groups(cfg))
    else:
        asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
