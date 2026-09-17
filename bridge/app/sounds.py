"""Sound effects: where to find them, and getting them into a form the
speakers will play.

Sonos's audioClip API only plays mp3 (and wav); UPnP play_uri is pickier
still about what it streams from the internet. So every clip goes through
`prepare()`: fetch it, run it through ffmpeg into a normalised 44.1 kHz mp3,
cache it by a hash of the source, and serve it from our own /audio. That
means ogg/flac/wav/m4a all work, and the speakers only ever fetch from the
LAN.

Sources, in the order a caller should try them:
  1. the local library (SOUNDS_DIR, files dropped in by the owner)
  2. BBC Sound Effects (~33k clips, no key; personal/educational use under
     the BBC RemArc licence)
  3. Freesound (optional; needs FREESOUND_API_KEY; CC-licensed, licence
     returned per clip)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile

import httpx

from .config import Settings

log = logging.getLogger("announce.sounds")

BBC_SEARCH = "https://sound-effects-api.bbcrewind.co.uk/api/sfx/search"
BBC_MEDIA = "https://sound-effects-media.bbcrewind.co.uk/mp3/{id}.mp3"
BBC_LICENCE = "BBC RemArc licence: personal, educational or research use only"
FREESOUND_SEARCH = "https://freesound.org/apiv2/search/text/"

AUDIO_EXT = {".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus", ".aiff", ".aif", ".wma"}


# ------------------------------------------------------------------- search

def search_bbc(query: str, limit: int) -> list[dict]:
    body = {"criteria": {"from": 0, "size": limit, "query": query}}
    r = httpx.post(BBC_SEARCH, json=body, timeout=15.0)
    r.raise_for_status()
    out = []
    for hit in r.json().get("results", []):
        out.append({
            "source": "bbc",
            "id": hit["id"],
            "description": hit.get("description", ""),
            "seconds": round((hit.get("duration") or 0) / 1000, 1),
            "url": BBC_MEDIA.format(id=hit["id"]),
            "licence": BBC_LICENCE,
        })
    return out


def search_freesound(query: str, limit: int, api_key: str) -> list[dict]:
    params = {"query": query, "page_size": limit, "fields": "id,name,duration,previews,license,username",
              "filter": "duration:[0.5 TO 120]", "token": api_key}
    r = httpx.get(FREESOUND_SEARCH, params=params, timeout=15.0)
    r.raise_for_status()
    out = []
    for hit in r.json().get("results", []):
        out.append({
            "source": "freesound",
            "id": str(hit["id"]),
            "description": f"{hit.get('name', '')} (by {hit.get('username', '?')})",
            "seconds": round(hit.get("duration") or 0, 1),
            "url": (hit.get("previews") or {}).get("preview-hq-mp3"),
            "licence": hit.get("license"),
        })
    return [o for o in out if o["url"]]


def search(s: Settings, query: str, limit: int = 8) -> dict:
    query = query.strip()
    results: list[dict] = []
    errors: list[str] = []
    for name, fn in (("bbc", lambda: search_bbc(query, limit)),
                     ("freesound", lambda: search_freesound(query, limit, s.freesound_api_key))):
        if name == "freesound" and not s.freesound_api_key:
            continue
        try:
            results += fn()
        except Exception as e:  # noqa: BLE001
            log.warning("%s search failed: %s", name, e)
            errors.append(f"{name}: {e}")
    lib = [n for n in library(s) if query.lower() in n.lower()] if query else library(s)
    return {"query": query, "library": lib, "results": results, "errors": errors}


# ------------------------------------------------------------------- library

def library(s: Settings) -> list[str]:
    """Names (without extension) of the files in SOUNDS_DIR."""
    if not os.path.isdir(s.sounds_dir):
        return []
    return sorted(os.path.splitext(f)[0] for f in os.listdir(s.sounds_dir)
                  if os.path.splitext(f)[1].lower() in AUDIO_EXT)


def library_path(s: Settings, name: str) -> str:
    base = os.path.basename(name)
    if not base or base != name:
        raise KeyError(f"bad sound name {name!r}")
    for f in os.listdir(s.sounds_dir) if os.path.isdir(s.sounds_dir) else []:
        stem, ext = os.path.splitext(f)
        if stem.lower() == base.lower() and ext.lower() in AUDIO_EXT:
            return os.path.join(s.sounds_dir, f)
    raise KeyError(f"no sound {name!r} in the library; have {library(s)}")


# ------------------------------------------------------------------- prepare

def _key(source: str) -> str:
    return "s" + hashlib.sha256(source.encode()).hexdigest()[:31]


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def probe_seconds(path: str) -> float | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "json", path], capture_output=True, text=True, timeout=20, check=True)
        return round(float(json.loads(out.stdout)["format"]["duration"]), 2)
    except Exception:  # noqa: BLE001
        return None


def convert(src: str, dst: str) -> None:
    """Any audio -> 44.1 kHz stereo 128k mp3, loudness-normalised so a whisper
    of a field recording and a slammed doorbell land at a similar level."""
    tmp = dst + ".part.mp3"
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", src, "-vn", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
           "-ar", "44100", "-ac", "2", "-b:a", "128k", "-f", "mp3", tmp]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True)
    except subprocess.CalledProcessError as e:
        raise ValueError(f"not audio ffmpeg can decode: {e.stderr.strip()[:200]}") from e
    os.replace(tmp, dst)


def fetch(url: str, dst: str, max_bytes: int) -> None:
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("url must be http(s)")
    with httpx.stream("GET", url, follow_redirects=True, timeout=30.0,
                      headers={"user-agent": "mcp-sonos/0.1"}) as r:
        if r.status_code != 200:
            raise ValueError(f"fetching {url} -> HTTP {r.status_code}")
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype.startswith("text/") or ctype in ("application/json", "application/xhtml+xml"):
            raise ValueError(f"{url} is {ctype}, not an audio file (a web page, or a player page rather "
                             f"than the file itself)")
        size = 0
        with open(dst, "wb") as f:
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"file is over {max_bytes // 1_000_000} MB")
                f.write(chunk)


def prepare(s: Settings, *, url: str | None = None, sound: str | None = None) -> tuple[str, float | None, bool]:
    """Return (path to a playable mp3 in the cache, seconds, was_cached)."""
    if (url is None) == (sound is None):
        raise ValueError("pass exactly one of url or sound")
    src_id = url if url else "library:" + os.path.basename(library_path(s, sound))  # type: ignore[arg-type]
    os.makedirs(s.cache_dir, exist_ok=True)
    dst = os.path.join(s.cache_dir, _key(src_id) + ".mp3")
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return dst, probe_seconds(dst), True
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the bridge")
    with tempfile.TemporaryDirectory(dir=s.cache_dir) as tmp:
        if url:
            raw = os.path.join(tmp, "raw")
            fetch(url, raw, s.max_sound_bytes)
        else:
            raw = library_path(s, sound)  # type: ignore[arg-type]
        convert(raw, dst)
    secs = probe_seconds(dst)
    log.info("prepared %s -> %s (%.1fs)", src_id[:80], os.path.basename(dst), secs or -1)
    return dst, secs, False
