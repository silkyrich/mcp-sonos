"""Local-only speaker controls: the things Sonos's cloud Control API (and so
the official Sonos connector) doesn't expose.

  eq         bass, treble, loudness, balance, sub and surround levels
  tv         switch a soundbar to its TV input
  sleep      sleep timer
  alarms     list / create / edit / delete
  settings   status light, touch controls
  stream     play an arbitrary stream URL (not a clip; it keeps playing)

All of these are plain UPnP calls through SoCo and are blocking; the HTTP
layer runs them in a thread. Each setter returns the previous values so a
change can be undone.
"""
from __future__ import annotations

import re
from typing import Any

from soco import alarms as soco_alarms

from .sonos import Bridge, Player

# ------------------------------------------------------------------- eq

# name -> (min, max) for ints, None for booleans. Only ones SoCo exposes and
# the player accepts; sub/surround fields are skipped on players without them.
EQ_FIELDS: dict[str, tuple[int, int] | None] = {
    "bass": (-10, 10),
    "treble": (-10, 10),
    "loudness": None,
    "sub_enabled": None,
    "sub_gain": (-15, 15),
    "surround_enabled": None,
    "surround_level": (-15, 15),         # TV audio
    "music_surround_level": (-15, 15),   # music
    "surround_full_volume_enabled": None,
}
SUB_FIELDS = {"sub_enabled", "sub_gain"}
SURROUND_FIELDS = {"surround_enabled", "surround_level", "music_surround_level",
                   "surround_full_volume_enabled"}


def _read(dev: Any, name: str) -> Any:
    try:
        return getattr(dev, name)
    except Exception:  # noqa: BLE001 - player doesn't support it
        return None


def get_eq(p: Player) -> dict:
    dev = p.dev
    out: dict[str, Any] = {"room": p.name}
    has_sub = bool(_read(dev, "has_subwoofer"))
    soundbar = bool(_read(dev, "is_soundbar"))
    for f in EQ_FIELDS:
        if f in SUB_FIELDS and not has_sub:
            continue
        if f in SURROUND_FIELDS and not soundbar:
            continue
        v = _read(dev, f)
        if v is not None:
            out[f] = bool(v) if EQ_FIELDS[f] is None else v
    bal = _read(dev, "balance")
    if bal is not None:
        out["balance"] = {"left": bal[0], "right": bal[1]}
    return out


def set_eq(p: Player, changes: dict) -> dict:
    before = get_eq(p)
    dev = p.dev
    for k, v in changes.items():
        if v is None:
            continue
        if k == "balance":
            left, right = int(v["left"]), int(v["right"])
            if not (0 <= left <= 100 and 0 <= right <= 100):
                raise ValueError("balance left/right must be 0-100")
            dev.balance = (left, right)
            continue
        if k not in EQ_FIELDS:
            raise ValueError(f"unknown eq field {k!r}; known: {sorted(EQ_FIELDS) + ['balance']}")
        if k not in before:
            raise ValueError(f"{p.name} has no {k!r} setting")
        rng = EQ_FIELDS[k]
        if rng is None:
            setattr(dev, k, bool(v))
        else:
            iv = int(v)
            if not rng[0] <= iv <= rng[1]:
                raise ValueError(f"{k} must be {rng[0]}..{rng[1]}")
            setattr(dev, k, iv)
    return {"previous": before, "now": get_eq(p)}


# ------------------------------------------------------------------- tv / sleep / settings

def switch_to_tv(p: Player) -> dict:
    if not _read(p.dev, "is_soundbar"):
        raise ValueError(f"{p.name} is not a soundbar")
    p.dev.switch_to_tv()
    return {"room": p.name, "input": "tv"}


def get_sleep(p: Player) -> dict:
    secs = p.dev.get_sleep_timer()
    return {"room": p.name, "remaining_seconds": secs}


def set_sleep(p: Player, minutes: float) -> dict:
    before = get_sleep(p)
    p.dev.set_sleep_timer(None if minutes <= 0 else int(minutes * 60))
    return {"previous": before, "now": get_sleep(p)}


def get_settings(p: Player) -> dict:
    return {"room": p.name, "status_light": _read(p.dev, "status_light"),
            "buttons_enabled": _read(p.dev, "buttons_enabled")}


def set_settings(p: Player, status_light: bool | None, buttons_enabled: bool | None) -> dict:
    before = get_settings(p)
    if status_light is not None:
        p.dev.status_light = bool(status_light)
    if buttons_enabled is not None:
        p.dev.buttons_enabled = bool(buttons_enabled)
    return {"previous": before, "now": get_settings(p)}


# ------------------------------------------------------------------- stream

def play_stream(p: Player, url: str, title: str | None) -> dict:
    """Start an internet radio / HLS / mp3 stream and leave it playing.
    Plays on the room's group coordinator so the whole group hears it."""
    coord = p.dev.group.coordinator
    coord.play_uri(url, title=title or "Stream", force_radio=url.startswith("http"))
    return {"room": p.name, "coordinator": coord.player_name, "url": url}


# ------------------------------------------------------------------- alarms

DAY_CODES = {"sun": "0", "mon": "1", "tue": "2", "wed": "3", "thu": "4", "fri": "5", "sat": "6"}
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")


def recurrence(value: str | list[str]) -> str:
    """"daily" | "weekdays" | "weekends" | "once" | ["mon","wed"] -> Sonos code."""
    if isinstance(value, list):
        try:
            codes = sorted({DAY_CODES[d.lower()[:3]] for d in value})
        except KeyError as e:
            raise ValueError(f"unknown day {e}; use mon..sun") from e
        if not codes:
            raise ValueError("days list is empty")
        return "ON_" + "".join(codes)
    v = value.strip().upper()
    if v in {"DAILY", "WEEKDAYS", "WEEKENDS", "ONCE"} or re.fullmatch(r"ON_[0-6]+", v):
        return v
    raise ValueError("recurrence must be daily, weekdays, weekends, once, or a list of days")


def _alarm_dict(a: Any) -> dict:
    chime = not a.program_uri or a.program_uri.startswith("x-rincon-buzzer:")
    return {
        "id": a.alarm_id,
        "room": a.zone.player_name if a.zone else None,
        "room_id": a.zone.uid if a.zone else a.room_uuid,
        "time": a.start_time.strftime("%H:%M"),
        "recurrence": a.recurrence,
        "enabled": a.enabled,
        "volume": a.volume,
        "duration": a.duration.strftime("%H:%M:%S") if a.duration else None,
        "include_grouped_rooms": a.include_linked_zones,
        "sound": "chime" if chime else "source",
        "source_uri": None if chime else a.program_uri,
    }


def _any_dev(bridge: Bridge) -> Any:
    players = list(bridge.discover().values())
    if not players:
        raise RuntimeError("no Sonos players found")
    return players[0].dev


def list_alarms(bridge: Bridge) -> list[dict]:
    return sorted((_alarm_dict(a) for a in soco_alarms.get_alarms(_any_dev(bridge))),
                  key=lambda d: (d["room"] or "", d["time"]))


def _find_alarm(bridge: Bridge, alarm_id: str) -> Any:
    for a in soco_alarms.get_alarms(_any_dev(bridge)):
        if str(a.alarm_id) == str(alarm_id):
            return a
    raise KeyError(f"no alarm with id {alarm_id}")


def _parse_time(t: str):
    import datetime as dt
    if not HHMM.match(t):
        raise ValueError("time must be 24-hour HH:MM")
    parts = [int(x) for x in t.split(":")]
    return dt.time(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)


def set_alarm(bridge: Bridge, *, alarm_id: str | None, room: str | None, time: str | None,
              recurrence_: str | list[str] | None, enabled: bool | None, volume: int | None,
              include_grouped_rooms: bool | None, duration_minutes: int | None) -> dict:
    """Create (no alarm_id) or edit (alarm_id) an alarm. Edits only touch the
    fields passed, so an existing alarm keeps its radio station / playlist.
    New alarms use the Sonos chime."""
    import datetime as dt
    if alarm_id is None:
        if not room or not time:
            raise ValueError("a new alarm needs room and time")
        a = soco_alarms.Alarm(bridge.player(room).dev, start_time=_parse_time(time),
                              recurrence="DAILY", enabled=True, volume=20)
        previous = None
    else:
        a = _find_alarm(bridge, alarm_id)
        previous = _alarm_dict(a)
        if room:
            a.zone = bridge.player(room).dev
        if time:
            a.start_time = _parse_time(time)
    if recurrence_ is not None:
        a.recurrence = recurrence(recurrence_)
    if enabled is not None:
        a.enabled = bool(enabled)
    if volume is not None:
        if not 0 <= int(volume) <= 100:
            raise ValueError("volume must be 0-100")
        a.volume = int(volume)
    if include_grouped_rooms is not None:
        a.include_linked_zones = bool(include_grouped_rooms)
    if duration_minutes is not None:
        m = int(duration_minutes)
        a.duration = None if m <= 0 else dt.time(m // 60, m % 60, 0)
    a.save()
    return {"previous": previous, "now": _alarm_dict(a)}


def delete_alarm(bridge: Bridge, alarm_id: str) -> dict:
    a = _find_alarm(bridge, alarm_id)
    gone = _alarm_dict(a)
    a.remove()
    return {"deleted": gone}
