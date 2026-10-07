"""State-machine tests against a fake bridge, with delays shrunk to fractions of a second.

Run: python -m unittest discover tests
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import plex_hue_dimmer  # noqa: E402
from aiohttp import FormData  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from plex_hue_dimmer import Config, Dimmer, make_app  # noqa: E402

plex_hue_dimmer.FADE_SLACK = 0

TV = "player-uuid-tv"
OTHER = "player-uuid-other"


class FakeHue:
    def __init__(self, **lights):
        # name -> (on, brightness)
        self.state = dict(lights)
        self.calls = []

    async def get_light(self, light):
        return self.state[light]

    async def set_brightness(self, light, brightness, transition):
        self.calls.append((light, brightness))
        on, _ = self.state[light]
        self.state[light] = (on, brightness)

    def touch(self, light, on, bri):
        """Someone used the dimmer switch or the app."""
        self.state[light] = (on, bri)


class DimmerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(dim_level=15, pause_level=40, pause_delay=0.05, stop_delay=0, finished_delay=0.1,
                          dim_transition=0, pause_transition=0, restore_transition=0,
                          state_file=os.path.join(self.tmp.name, "state.json"))
        self.hue = FakeHue(a=(True, 80.0), b=(True, 100.0))
        self.dimmer = self.make()

    async def asyncTearDown(self):
        self.dimmer._cancel_timer()
        self.tmp.cleanup()

    def make(self):
        return Dimmer(self.hue, self.cfg, {k: k for k in self.hue.state})

    def bri(self, light):
        return self.hue.state[light][1]

    async def wait(self, seconds):
        await asyncio.sleep(seconds)

    async def test_play_dims_and_stop_restores_straight_away(self):
        await self.dimmer.handle("media.play", TV)
        self.assertEqual((self.bri("a"), self.bri("b")), (15, 15))
        await self.dimmer.handle("media.stop", TV)
        await self.wait(0.01)
        self.assertEqual((self.bri("a"), self.bri("b")), (80, 100))
        self.assertEqual(self.dimmer.mode, "idle")
        self.assertEqual(self.dimmer.owned, {})

    async def test_stop_after_watching_to_the_end_waits(self):
        await self.dimmer.handle("media.play", TV)
        await self.dimmer.handle("media.scrobble", TV)
        await self.dimmer.handle("media.stop", TV)
        await self.wait(0.05)
        self.assertEqual(self.bri("a"), 15, "restored before the finished delay")
        await self.wait(0.1)
        self.assertEqual((self.bri("a"), self.bri("b")), (80, 100))

    async def test_watched_flag_survives_pause_in_the_credits(self):
        await self.dimmer.handle("media.play", TV)
        await self.dimmer.handle("media.scrobble", TV)
        await self.dimmer.handle("media.pause", TV)
        await self.dimmer.handle("media.resume", TV)
        await self.dimmer.handle("media.stop", TV)
        await self.wait(0.05)
        self.assertEqual(self.bri("a"), 15)

    async def test_next_item_starts_unwatched(self):
        await self.dimmer.handle("media.play", TV)
        await self.dimmer.handle("media.scrobble", TV)
        await self.dimmer.handle("media.stop", TV)
        await self.dimmer.handle("media.play", TV)
        await self.dimmer.handle("media.stop", TV)  # stopped part-way through
        await self.wait(0.01)
        self.assertEqual(self.bri("a"), 80)

    async def test_next_episode_autoplay_never_undims(self):
        await self.dimmer.handle("media.play", TV)
        await self.dimmer.handle("media.scrobble", TV)
        await self.dimmer.handle("media.stop", TV)
        await self.wait(0.05)
        await self.dimmer.handle("media.play", TV)
        await self.wait(0.15)
        self.assertEqual(self.bri("a"), 15)
        # and the original level still comes back at the very end
        await self.dimmer.handle("media.stop", TV)
        await self.wait(0.01)
        self.assertEqual((self.bri("a"), self.bri("b")), (80, 100))

    async def test_pause_goes_part_way_up_and_resume_dims_again(self):
        await self.dimmer.handle("media.play", TV)
        await self.dimmer.handle("media.pause", TV)
        await self.wait(0.08)
        self.assertEqual((self.bri("a"), self.bri("b")), (40, 40))
        await self.dimmer.handle("media.resume", TV)
        self.assertEqual((self.bri("a"), self.bri("b")), (15, 15))

    async def test_quick_pause_does_not_flicker(self):
        await self.dimmer.handle("media.play", TV)
        await self.dimmer.handle("media.pause", TV)
        await self.wait(0.02)
        await self.dimmer.handle("media.resume", TV)
        await self.wait(0.08)
        self.assertEqual(self.hue.calls, [("a", 15), ("b", 15)])

    async def test_pause_level_never_brighter_than_original(self):
        self.hue.touch("a", True, 30.0)
        await self.dimmer.handle("media.play", TV)
        await self.dimmer.handle("media.pause", TV)
        await self.wait(0.08)
        self.assertEqual(self.bri("a"), 30)
        self.assertEqual(self.bri("b"), 40)

    async def test_off_and_already_dim_lights_are_left_alone(self):
        self.hue.touch("a", False, 80.0)
        self.hue.touch("b", True, 10.0)
        await self.dimmer.handle("media.play", TV)
        await self.dimmer.handle("media.stop", TV)
        await self.wait(0.15)
        self.assertEqual(self.hue.calls, [])
        self.assertEqual(self.hue.state, {"a": (False, 80.0), "b": (True, 10.0)})

    async def test_manual_change_is_respected_through_resume_and_stop(self):
        await self.dimmer.handle("media.play", TV)
        await self.dimmer.handle("media.pause", TV)
        await self.wait(0.08)
        self.hue.touch("a", True, 100.0)  # turned up to grab a snack
        await self.dimmer.handle("media.resume", TV)
        self.assertEqual(self.bri("a"), 100)
        self.assertEqual(self.bri("b"), 15)
        await self.dimmer.handle("media.stop", TV)
        await self.wait(0.15)
        self.assertEqual(self.bri("a"), 100)
        self.assertEqual(self.bri("b"), 100)

    async def test_light_switched_off_during_playback_stays_off(self):
        await self.dimmer.handle("media.play", TV)
        self.hue.touch("a", False, 15.0)
        await self.dimmer.handle("media.stop", TV)
        await self.wait(0.15)
        self.assertEqual(self.hue.state["a"], (False, 15.0))
        self.assertEqual(self.bri("b"), 100)

    async def test_next_item_redims_a_light_turned_up_by_hand(self):
        await self.dimmer.handle("media.play", TV)
        self.hue.touch("a", True, 60.0)
        await self.dimmer.handle("media.stop", TV)
        await self.dimmer.handle("media.play", TV)
        self.assertEqual(self.bri("a"), 15)
        await self.dimmer.handle("media.stop", TV)
        await self.wait(0.15)
        self.assertEqual(self.bri("a"), 60, "restores to the level chosen by hand")

    async def test_stays_dim_while_any_player_plays(self):
        await self.dimmer.handle("media.play", TV)
        await self.dimmer.handle("media.play", OTHER)
        await self.dimmer.handle("media.stop", TV)
        await self.wait(0.15)
        self.assertEqual(self.bri("a"), 15)
        await self.dimmer.handle("media.stop", OTHER)
        await self.wait(0.15)
        self.assertEqual(self.bri("a"), 80)

    async def test_restart_mid_movie_still_restores(self):
        await self.dimmer.handle("media.play", TV)
        self.dimmer._cancel_timer()
        self.dimmer = self.make()
        self.dimmer.load()
        await self.dimmer.handle("media.stop", TV)
        await self.wait(0.15)
        self.assertEqual((self.bri("a"), self.bri("b")), (80, 100))

    async def test_stale_player_is_expired(self):
        await self.dimmer.handle("media.play", TV)
        self.dimmer.players[TV]["seen"] -= 9 * 3600
        await self.dimmer.expire_stale()
        await self.wait(0.15)
        self.assertEqual(self.bri("a"), 80)


class RecordingDimmer:
    def __init__(self):
        self.events = []
        self.mode, self.players, self.owned, self.lights = "idle", {}, {}, {}

    async def handle(self, event, player):
        self.events.append((event, player))


class WebhookTest(unittest.IsolatedAsyncioTestCase):
    """The real request shape: multipart, with a poster JPEG on play/resume."""

    async def asyncSetUp(self):
        self.dimmer = RecordingDimmer()
        cfg = Config(players={"living room"})
        self.client = TestClient(TestServer(make_app(cfg, self.dimmer)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def post(self, event, player="Living Room", kind="episode", thumb=True):
        form = FormData()
        form.add_field("payload", json.dumps({
            "event": event, "Account": {"title": "someone"},
            "Player": {"title": player, "uuid": "uuid-" + player},
            "Metadata": {"type": kind, "title": "Pilot"}}), content_type="application/json")
        if thumb:  # bigger than aiohttp's 1MB default body limit
            form.add_field("thumb", b"\xff\xd8" + b"\0" * 2_000_000,
                           filename="thumb.jpg", content_type="image/jpeg")
        r = await self.client.post("/webhook", data=form)
        await asyncio.sleep(0)  # let the spawned handle() run
        return r.status

    async def test_play_with_poster_is_accepted(self):
        self.assertEqual(await self.post("media.play"), 200)
        self.assertEqual(self.dimmer.events, [("media.play", "uuid-Living Room")])

    async def test_other_players_and_media_are_ignored(self):
        self.assertEqual(await self.post("media.play", player="Bedroom"), 200)
        self.assertEqual(await self.post("media.play", kind="track"), 200)
        self.assertEqual(self.dimmer.events, [])

    async def test_not_a_plex_webhook(self):
        r = await self.client.post("/webhook", data=b"hello")
        self.assertEqual(r.status, 400)


class ConfigTest(unittest.TestCase):
    def test_player_matches_name_or_uuid_case_insensitively(self):
        cfg = Config(players={"living room", "abc-123"})
        self.assertTrue(cfg.wants_player({"title": "Living Room", "uuid": "zzz"}))
        self.assertTrue(cfg.wants_player({"title": "Living Room", "uuid": "ABC-123"}))
        self.assertFalse(cfg.wants_player({"title": "Bedroom", "uuid": "zzz"}))


if __name__ == "__main__":
    unittest.main()
