"""ElevenLabs text-to-speech with an on-disk phrase cache.

Cache key = sha256(voice | model | format | text). Repeated phrases
("Dinner's ready") never hit the network again.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time

import httpx

from .config import Settings

log = logging.getLogger("announce.tts")

ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream"


def cache_key(s: Settings, text: str) -> str:
    h = hashlib.sha256()
    h.update(f"{s.eleven_voice_id}|{s.eleven_model}|{s.eleven_output_format}|".encode())
    h.update(text.strip().encode())
    return h.hexdigest()[:24]


def cache_path(s: Settings, key: str) -> str:
    return os.path.join(s.cache_dir, f"{key}.mp3")


async def synthesize(s: Settings, text: str) -> tuple[str, bool]:
    """Return (path_to_mp3, was_cached). Streams from ElevenLabs to disk."""
    os.makedirs(s.cache_dir, exist_ok=True)
    key = cache_key(s, text)
    path = cache_path(s, key)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path, True
    if not s.eleven_api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")

    t0 = time.monotonic()
    url = ELEVEN_URL.format(voice=s.eleven_voice_id)
    params = {"output_format": s.eleven_output_format, "optimize_streaming_latency": "3"}
    body = {
        "text": text,
        "model_id": s.eleven_model,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": 1.0},
    }
    headers = {"xi-api-key": s.eleven_api_key, "accept": "audio/mpeg"}

    # Write to a temp file in the same dir, then atomic rename, so a
    # concurrent request for the same phrase never sees a half-written clip.
    fd, tmp = tempfile.mkstemp(dir=s.cache_dir, suffix=".part")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            async with client.stream("POST", url, params=params, json=body, headers=headers) as r:
                if r.status_code != 200:
                    detail = (await r.aread())[:500]
                    raise RuntimeError(f"elevenlabs {r.status_code}: {detail!r}")
                first = None
                with os.fdopen(fd, "wb") as f:
                    async for chunk in r.aiter_bytes():
                        if first is None:
                            first = time.monotonic() - t0
                        f.write(chunk)
        os.replace(tmp, path)
        log.info("tts ok key=%s ttfb=%.2fs total=%.2fs bytes=%d",
                 key, first or -1, time.monotonic() - t0, os.path.getsize(path))
        return path, False
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
