FROM python:3.12-slim
# ffmpeg converts any sound-effect format the caller finds into mp3 for the speakers
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY sounds ./default-sounds
VOLUME /data/cache
ENV CACHE_DIR=/data/cache SOUNDS_DIR=/data/sounds PORT=8765
# host networking is required: SSDP discovery + players fetching clips from this box
# seed the library with the bundled sounds, without overwriting the owner's own files
CMD ["sh", "-c", "mkdir -p $SOUNDS_DIR && cp -n default-sounds/* $SOUNDS_DIR/ 2>/dev/null; uvicorn app.server:app --host 0.0.0.0 --port ${PORT}"]
