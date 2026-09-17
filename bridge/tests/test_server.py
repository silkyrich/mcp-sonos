"""HTTP-level tests for clip upload and announce-by-clip, with the Sonos side stubbed."""
from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

KEY = "0123456789abcdef0123456789abcdef"  # gitleaks:allow - dummy clip key


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_TOKEN", "t")
    monkeypatch.setenv("HOST_IP", "10.0.0.99")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    server = importlib.import_module("app.server")
    server_sonos = importlib.import_module("app.sonos")
    calls = []

    def fake_announce(rooms, url, volume, cached, secs):
        calls.append((rooms, url, volume, cached))
        return server_sonos.Report(rooms=["Kitchen"], strategy="audioclip", cached=cached)

    monkeypatch.setattr(server.bridge, "announce", fake_announce)
    monkeypatch.setattr(server.bridge, "discover", lambda force=False: {})
    with TestClient(server.app) as c:
        c.calls = calls
        yield c


H = {"Authorization": "Bearer t"}


def test_put_clip_then_announce_it(client, tmp_path):
    r = client.post("/announce", json={"clip": KEY, "rooms": ["Kitchen"]}, headers=H)
    assert r.status_code == 404 and r.json()["detail"] == "clip not cached"
    r = client.put(f"/clips/{KEY}", content=b"ID3fake", headers={**H, "content-type": "audio/mpeg"})
    assert r.status_code == 200 and (tmp_path / f"{KEY}.mp3").read_bytes() == b"ID3fake"
    r = client.post("/announce", json={"clip": KEY, "rooms": ["Kitchen"], "volume": 30}, headers=H)
    assert r.status_code == 200
    assert client.calls == [(["Kitchen"], f"http://10.0.0.99:8765/audio/{KEY}.mp3", 30, True)]


def test_clip_key_and_body_validation(client):
    assert client.put("/clips/../../etc", content=b"x", headers=H).status_code in (400, 404)
    assert client.put("/clips/NOTHEX", content=b"x", headers=H).status_code == 400
    assert client.put(f"/clips/{KEY}", content=b"", headers=H).status_code == 400
    assert client.put(f"/clips/{KEY}", content=b"x").status_code == 401


def test_announce_needs_exactly_one_of_text_or_clip(client):
    assert client.post("/announce", json={"rooms": "all"}, headers=H).status_code == 400
    assert client.post("/announce", json={"text": "hi", "clip": KEY}, headers=H).status_code == 400


def test_audio_supports_head_and_range(client, tmp_path):
    client.put(f"/clips/{KEY}", content=b"ID3" + b"x" * 200, headers=H)
    r = client.head(f"/audio/{KEY}.mp3")
    assert r.status_code == 200 and r.headers["content-length"] == "203" and r.content == b""
    r = client.get(f"/audio/{KEY}.mp3", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.content) == 10
    assert client.get("/audio/nope.mp3").status_code == 404
