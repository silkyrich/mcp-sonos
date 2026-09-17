"""Sound search, the local library, and fetch + ffmpeg conversion."""
from __future__ import annotations

import shutil
import subprocess

import httpx
import pytest

from app import sounds
from app.config import Settings


def settings(tmp_path, **kw) -> Settings:
    base = dict(eleven_api_key="", eleven_voice_id="v", eleven_model="m", eleven_output_format="f",
                api_token="t", host_ip="10.0.0.99", port=8765, cache_dir=str(tmp_path / "cache"),
                default_volume=40, max_clip_seconds=5, discovery_timeout=1,
                sounds_dir=str(tmp_path / "sounds"), freesound_api_key="", max_sound_bytes=1_000_000)
    return Settings(**{**base, **kw})


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_ogg(path: str) -> None:
    # ffmpeg's built-in vorbis encoder (libvorbis isn't in every build) wants stereo
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                    "-ac", "2", "-c:a", "vorbis", "-strict", "-2", path], check=True)


# ---- search ---------------------------------------------------------------

def test_bbc_search_maps_results(monkeypatch, tmp_path):
    seen = {}

    def fake_post(url, json, timeout):
        seen["body"] = json
        return httpx.Response(200, json={"results": [
            {"id": "07070033", "description": "Thunder, one clap.", "duration": 12345}]},
            request=httpx.Request("POST", url))

    monkeypatch.setattr(sounds.httpx, "post", fake_post)
    out = sounds.search(settings(tmp_path), "thunder", 5)
    assert seen["body"]["criteria"]["query"] == "thunder"
    assert out["results"] == [{"source": "bbc", "id": "07070033", "description": "Thunder, one clap.",
                               "seconds": 12.3, "url": sounds.BBC_MEDIA.format(id="07070033"),
                               "licence": sounds.BBC_LICENCE}]
    assert out["errors"] == [] and out["library"] == []


def test_freesound_only_when_keyed_and_search_errors_are_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(sounds.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    monkeypatch.setattr(sounds.httpx, "get", lambda url, params, timeout: httpx.Response(200, json={"results": [
        {"id": 1, "name": "Boom", "username": "x", "duration": 2.0, "license": "CC0",
         "previews": {"preview-hq-mp3": "https://f/1.mp3"}}]}, request=httpx.Request("GET", url)))
    out = sounds.search(settings(tmp_path), "boom")
    assert out["results"] == [] and out["errors"] and "bbc" in out["errors"][0]
    out = sounds.search(settings(tmp_path, freesound_api_key="k"), "boom")
    assert [r["source"] for r in out["results"]] == ["freesound"]
    assert out["results"][0]["url"] == "https://f/1.mp3" and out["results"][0]["licence"] == "CC0"


# ---- library --------------------------------------------------------------

def test_library_lists_and_resolves_case_insensitively(tmp_path):
    s = settings(tmp_path)
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "Doorbell.wav").write_bytes(b"x")
    (tmp_path / "sounds" / "notes.txt").write_bytes(b"x")
    assert sounds.library(s) == ["Doorbell"]
    assert sounds.library_path(s, "doorbell").endswith("Doorbell.wav")
    with pytest.raises(KeyError):
        sounds.library_path(s, "thunder")
    with pytest.raises(KeyError):
        sounds.library_path(s, "../etc/passwd")


def test_search_filters_library_without_network(monkeypatch, tmp_path):
    s = settings(tmp_path)
    (tmp_path / "sounds").mkdir()
    for n in ("Doorbell.wav", "thunder.ogg"):
        (tmp_path / "sounds" / n).write_bytes(b"x")
    monkeypatch.setattr(sounds, "search_bbc", lambda q, l: [])
    assert sounds.search(s, "door")["library"] == ["Doorbell"]
    assert sounds.search(s, "")["library"] == ["Doorbell", "thunder"]


# ---- prepare --------------------------------------------------------------

def test_fetch_rejects_web_pages(monkeypatch, tmp_path):
    class R:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}

        def __enter__(self): return self
        def __exit__(self, *a): pass
        def iter_bytes(self): yield b"<html>"

    monkeypatch.setattr(sounds.httpx, "stream", lambda *a, **k: R())
    with pytest.raises(ValueError, match="not an audio file"):
        sounds.fetch("https://example.com/page", str(tmp_path / "raw"), 1000)
    with pytest.raises(ValueError, match="http"):
        sounds.fetch("ftp://x/y", str(tmp_path / "raw"), 1000)


@needs_ffmpeg
def test_prepare_converts_library_ogg_to_mp3_and_caches(tmp_path):
    s = settings(tmp_path)
    (tmp_path / "sounds").mkdir()
    make_ogg(str(tmp_path / "sounds" / "beep.ogg"))
    path, secs, cached = sounds.prepare(s, sound="beep")
    assert path.endswith(".mp3") and not cached and secs and 0.4 < secs < 0.7
    assert open(path, "rb").read(3) in (b"ID3", b"\xff\xfb", b"\xff\xf3")
    path2, _, cached2 = sounds.prepare(s, sound="beep")
    assert path2 == path and cached2


@needs_ffmpeg
def test_prepare_fetches_url_and_converts(monkeypatch, tmp_path):
    s = settings(tmp_path)
    make_ogg(str(tmp_path / "src.ogg"))
    data = open(tmp_path / "src.ogg", "rb").read()

    class R:
        status_code = 200
        headers = {"content-type": "audio/ogg"}

        def __enter__(self): return self
        def __exit__(self, *a): pass
        def iter_bytes(self): yield data

    monkeypatch.setattr(sounds.httpx, "stream", lambda *a, **k: R())
    path, secs, cached = sounds.prepare(s, url="https://example.com/thunder.ogg")
    assert path.endswith(".mp3") and not cached and secs
    with pytest.raises(ValueError):
        sounds.prepare(s, url="https://x/a", sound="b")


@needs_ffmpeg
def test_prepare_rejects_non_audio(monkeypatch, tmp_path):
    s = settings(tmp_path)
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "junk.mp3").write_bytes(b"this is not audio at all")
    with pytest.raises(ValueError, match="not audio"):
        sounds.prepare(s, sound="junk")
