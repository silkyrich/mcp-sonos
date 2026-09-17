"""HTTP surface for the bridge.

  Announcements and clips (whatever was playing resumes afterwards)
    POST /announce                {"text": "...", "rooms": ["Kitchen"] | "all", "volume": 40}
    POST /play                    {"url": "https://...mp3", "rooms": ..., "volume": ..., "seconds": ...}

  Local-only controls (not in Sonos's cloud API)
    GET  /rooms                   discovered rooms + which support the audioClip fast path
    GET  /rooms/{room}/eq         POST the same shape to change it
    POST /rooms/{room}/tv         switch a soundbar to TV input
    GET  /rooms/{room}/sleep      POST {"minutes": 30} (0 cancels)
    GET  /rooms/{room}/settings   POST {"status_light": false, "buttons_enabled": true}
    POST /rooms/{room}/stream     {"url": "https://.../stream.mp3", "title": "..."}
    GET  /alarms                  POST to create/edit, DELETE /alarms/{id}

  Unauthenticated
    GET  /health
    GET  /audio/{f}               the clip files the players fetch

{room} is a room name ("Kitchen", "living room") or a player id (RINCON_...),
the same ids the official Sonos connector uses.

Auth: `Authorization: Bearer <API_TOKEN>` on everything except /audio and /health.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import controls, tts
from .config import Settings, load
from .sonos import Bridge

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("announce")

settings: Settings = load()
bridge = Bridge(settings)
_sema = asyncio.Semaphore(1)  # one announcement at a time; others queue


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(bridge.discover, True)
    yield


app = FastAPI(title="mcp-sonos bridge", version="0.1.0", lifespan=lifespan)


def auth(req: Request) -> None:
    hdr = req.headers.get("authorization", "")
    tok = hdr[7:] if hdr.lower().startswith("bearer ") else ""
    if not tok or not secrets.compare_digest(tok, settings.api_token):
        raise HTTPException(401, "bad token")


async def run(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    """Run a blocking SoCo call in a thread, mapping errors to HTTP codes."""
    try:
        return await asyncio.to_thread(fn, *args, **kw)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\"")) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def player(room: str):
    return bridge.player(room)


# ------------------------------------------------------------------- models

Rooms = list[str] | str


class AnnounceIn(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    rooms: Rooms = "all"
    volume: int | None = Field(default=None, ge=0, le=100)


class PlayIn(BaseModel):
    url: str = Field(min_length=8)
    rooms: Rooms = "all"
    volume: int | None = Field(default=None, ge=0, le=100)
    seconds: float | None = Field(default=None, gt=0, le=600)


class Balance(BaseModel):
    left: int = Field(ge=0, le=100)
    right: int = Field(ge=0, le=100)


class EqIn(BaseModel):
    bass: int | None = None
    treble: int | None = None
    loudness: bool | None = None
    balance: Balance | None = None
    sub_enabled: bool | None = None
    sub_gain: int | None = None
    surround_enabled: bool | None = None
    surround_level: int | None = None
    music_surround_level: int | None = None
    surround_full_volume_enabled: bool | None = None


class SleepIn(BaseModel):
    minutes: float = Field(ge=0, le=24 * 60)


class SettingsIn(BaseModel):
    status_light: bool | None = None
    buttons_enabled: bool | None = None


class StreamIn(BaseModel):
    url: str = Field(min_length=8)
    title: str | None = None


class AlarmIn(BaseModel):
    id: str | None = None
    room: str | None = None
    time: str | None = None
    recurrence: str | list[str] | None = None
    enabled: bool | None = None
    volume: int | None = Field(default=None, ge=0, le=100)
    include_grouped_rooms: bool | None = None
    duration_minutes: int | None = Field(default=None, ge=0, le=24 * 60)


# ------------------------------------------------------------------- basics

def _clip_url(path: str) -> str:
    return f"http://{settings.host_ip}:{settings.port}/audio/{os.path.basename(path)}"


def _est_seconds(path: str) -> float:
    # mp3_44100_128 -> 128 kbit/s. Only used to bound how long we wait.
    return os.path.getsize(path) * 8 / 128_000


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "players": len(bridge._players)}


@app.get("/rooms", dependencies=[Depends(auth)])
async def rooms(refresh: bool = False) -> list[dict]:
    if refresh:
        await asyncio.to_thread(bridge.discover, True)
    return await run(bridge.rooms)


# ------------------------------------------------------------------- announce / clips

@app.post("/announce", dependencies=[Depends(auth)])
async def announce(body: AnnounceIn) -> JSONResponse:
    t0 = time.monotonic()
    try:
        path, cached = await tts.synthesize(settings, body.text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"tts failed: {e}") from e
    t_tts = time.monotonic() - t0
    async with _sema:
        rep = await run(bridge.announce, body.rooms, _clip_url(path), body.volume, cached,
                        _est_seconds(path))
    rep.timings["tts"] = t_tts
    rep.timings["end_to_end"] = time.monotonic() - t0
    log.info("announce %r rooms=%s strategy=%s t=%.2fs warn=%d",
             body.text[:40], rep.rooms, rep.strategy, rep.timings["end_to_end"], len(rep.warnings))
    return JSONResponse(rep.__dict__)


@app.post("/play", dependencies=[Depends(auth)])
async def play(body: PlayIn) -> JSONResponse:
    """Play an arbitrary clip URL (a doorbell, a chime) with the same restore semantics."""
    async with _sema:
        rep = await run(bridge.announce, body.rooms, body.url, body.volume, False, body.seconds)
    return JSONResponse(rep.__dict__)


@app.get("/audio/{name}")
async def audio(name: str):
    if "/" in name or ".." in name or not name.endswith(".mp3"):
        raise HTTPException(404)
    path = os.path.join(settings.cache_dir, name)
    if not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/mpeg")


# ------------------------------------------------------------------- local-only controls

@app.get("/rooms/{room}/eq", dependencies=[Depends(auth)])
async def get_eq(room: str) -> dict:
    return await run(lambda: controls.get_eq(player(room)))


@app.post("/rooms/{room}/eq", dependencies=[Depends(auth)])
async def set_eq(room: str, body: EqIn) -> dict:
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(400, "nothing to change")
    return await run(lambda: controls.set_eq(player(room), changes))


@app.post("/rooms/{room}/tv", dependencies=[Depends(auth)])
async def tv(room: str) -> dict:
    return await run(lambda: controls.switch_to_tv(player(room)))


@app.get("/rooms/{room}/sleep", dependencies=[Depends(auth)])
async def get_sleep(room: str) -> dict:
    return await run(lambda: controls.get_sleep(player(room)))


@app.post("/rooms/{room}/sleep", dependencies=[Depends(auth)])
async def set_sleep(room: str, body: SleepIn) -> dict:
    return await run(lambda: controls.set_sleep(player(room), body.minutes))


@app.get("/rooms/{room}/settings", dependencies=[Depends(auth)])
async def get_settings(room: str) -> dict:
    return await run(lambda: controls.get_settings(player(room)))


@app.post("/rooms/{room}/settings", dependencies=[Depends(auth)])
async def set_settings(room: str, body: SettingsIn) -> dict:
    return await run(lambda: controls.set_settings(player(room), body.status_light, body.buttons_enabled))


@app.post("/rooms/{room}/stream", dependencies=[Depends(auth)])
async def stream(room: str, body: StreamIn) -> dict:
    return await run(lambda: controls.play_stream(player(room), body.url, body.title))


@app.get("/alarms", dependencies=[Depends(auth)])
async def alarms() -> list[dict]:
    return await run(controls.list_alarms, bridge)


@app.post("/alarms", dependencies=[Depends(auth)])
async def set_alarm(body: AlarmIn) -> dict:
    return await run(lambda: controls.set_alarm(
        bridge, alarm_id=body.id, room=body.room, time=body.time, recurrence_=body.recurrence,
        enabled=body.enabled, volume=body.volume, include_grouped_rooms=body.include_grouped_rooms,
        duration_minutes=body.duration_minutes))


@app.delete("/alarms/{alarm_id}", dependencies=[Depends(auth)])
async def delete_alarm(alarm_id: str) -> dict:
    return await run(controls.delete_alarm, bridge, alarm_id)
