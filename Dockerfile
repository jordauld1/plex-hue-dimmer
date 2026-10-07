FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY plex_hue_dimmer.py .
COPY tests tests

CMD ["python", "-u", "plex_hue_dimmer.py"]
