"""Unit tests for the UPnP announce path using an in-memory fake of SoCo.

Models a typical household: five rooms, two of them home-theatre units on
TV input, two grouped. Checks that after an
announcement the group topology, transport URIs and volumes are back.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import pytest

# ---- fake soco ------------------------------------------------------------


class FakeGroup:
    def __init__(self, coordinator, members):
        self.coordinator = coordinator
        self.members = members


class FakeAV:
    def __init__(self, dev):
        self.dev = dev

    def GetMediaInfo(self, _):
        return {"CurrentURI": self.dev.uri, "CurrentURIMetaData": self.dev.meta}


@dataclass
class FakeSoCo:
    player_name: str
    uid: str
    ip_address: str
    uri: str = ""
    meta: str = ""
    state: str = "STOPPED"
    volume: int = 20
    mute: bool = False
    bass: int = 0
    treble: int = 0
    loudness: bool = True
    fixed_volume: bool = False
    balance: tuple = (100, 100)
    is_soundbar: bool = False
    has_subwoofer: bool = False
    sub_enabled: bool = True
    sub_gain: int = 0
    surround_enabled: bool = True
    surround_level: int = 0
    music_surround_level: int = 0
    surround_full_volume_enabled: bool = False
    status_light: bool = True
    buttons_enabled: bool = True
    sleep: int | None = None
    tv_calls: int = 0
    coordinator_uid: str | None = None  # None => self
    play_calls: list = field(default_factory=list)
    _polls: int = 0
    house: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.avTransport = FakeAV(self)

    # topology
    @property
    def is_coordinator(self):
        return self.coordinator_uid in (None, self.uid)

    @property
    def group(self):
        coord_uid = self.coordinator_uid or self.uid
        coord = self.house[coord_uid]
        members = [d for d in self.house.values() if (d.coordinator_uid or d.uid) == coord_uid]
        return FakeGroup(coord, members)

    def join(self, master):
        self.coordinator_uid = master.uid
        self.uri = f"x-rincon:{master.uid}"
        self.state = master.state

    def unjoin(self):
        self.coordinator_uid = None
        if self.uri.startswith("x-rincon:"):
            self.uri, self.state = "", "STOPPED"

    # transport
    def get_current_transport_info(self):
        # after play_uri, report PLAYING once then STOPPED so the waiter returns
        if self.state == "PLAYING_CLIP":
            self._polls += 1
            return {"current_transport_state": "PLAYING" if self._polls < 2 else "STOPPED"}
        return {"current_transport_state": self.state}

    def get_current_track_info(self):
        return {"playlist_position": "3", "position": "0:01:00"}

    def play_uri(self, uri, meta="", title="", start=True):
        self.play_calls.append(uri)
        self.uri, self.meta = uri, meta
        if uri.startswith("http"):
            self.state, self._polls = "PLAYING_CLIP", 0
        else:
            self.state = "PLAYING" if start else "STOPPED"

    def play(self):
        self.state = "PLAYING"

    def pause(self):
        self.state = "PAUSED_PLAYBACK"

    def stop(self):
        self.state = "STOPPED"

    def play_from_queue(self, i, start=True):
        self.uri = f"x-rincon-queue:{self.uid}#0"
        self.state = "PLAYING" if start else "STOPPED"

    def seek(self, pos):
        pass

    def switch_to_tv(self):
        self.tv_calls += 1
        self.uri, self.state = f"x-sonos-htastream:{self.uid}:spdif", "PLAYING"

    def get_sleep_timer(self):
        return self.sleep

    def set_sleep_timer(self, secs):
        self.sleep = secs

    play_mode = "NORMAL"
    cross_fade = False


def make_house():
    house: dict[str, FakeSoCo] = {}

    def add(name, uid, **kw):
        d = FakeSoCo(player_name=name, uid=uid, ip_address=f"10.0.0.{len(house)+1}", house=house, **kw)
        house[uid] = d
        return d

    add("Kitchen", "K", uri="x-sonosapi-stream:radio1", meta="<radio/>", state="PLAYING", volume=18)
    add("Living Room", "L", uri="x-sonos-htastream:L:spdif", state="PLAYING", volume=30, is_soundbar=True)
    add("Main Bedroom", "M", uri="", state="STOPPED", volume=10)
    add("Office", "O", uri="x-sonos-htastream:O:spdif", state="PLAYING", volume=25,
        is_soundbar=True, has_subwoofer=True)
    add("Kids Room", "A", uri="x-rincon-queue:A#0", state="PLAYING", volume=15)
    # Bedroom grouped under Kids Room
    house["M"].join(house["A"])
    return house


@pytest.fixture
def bridge(monkeypatch):
    house = make_house()
    fake_soco = types.ModuleType("soco")
    fake_soco.discover = lambda **kw: list(house.values())
    fake_soco.SoCo = FakeSoCo
    snap_mod = types.ModuleType("soco.snapshot")
    import importlib
    real_snapshot = importlib.import_module("soco.snapshot")
    snap_mod.Snapshot = real_snapshot.Snapshot
    fake_soco.alarms = importlib.import_module("soco.alarms")
    monkeypatch.setitem(sys.modules, "soco", fake_soco)
    monkeypatch.setitem(sys.modules, "soco.snapshot", snap_mod)
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]
    monkeypatch.setattr("time.sleep", lambda s: None)
    from app.config import Settings
    from app import sonos as sonos_mod
    s = Settings(eleven_api_key="x", eleven_voice_id="v", eleven_model="m", eleven_output_format="f",
                 api_token="t", host_ip="10.0.0.99", port=8765, cache_dir="/tmp/c",
                 default_volume=40, max_clip_seconds=5, discovery_timeout=1)
    b = sonos_mod.Bridge(s)
    # no real players -> no fast path
    monkeypatch.setattr(b, "probe_audioclip", lambda p: False)
    return b, house


def snapshot_state(house):
    # a grouped player's transport state is its coordinator's, as on real Sonos
    return {u: (d.coordinator_uid or d.uid, d.uri, house[d.coordinator_uid or d.uid].state, d.volume)
            for u, d in house.items()}


def test_single_room_restores_radio(bridge):
    b, house = bridge
    before = snapshot_state(house)
    rep = b.announce(["kitchen"], "http://10.0.0.99:8765/audio/x.mp3", 50, False, 2.0)
    assert rep.rooms == ["Kitchen"] and rep.strategy == "upnp"
    assert "http://10.0.0.99:8765/audio/x.mp3" in house["K"].play_calls
    assert snapshot_state(house) == before
    assert not rep.warnings, rep.warnings


def test_tv_room_returns_to_tv(bridge):
    b, house = bridge
    b.announce(["Office"], "http://c/x.mp3", None, False, 1.0)
    assert house["O"].uri == "x-sonos-htastream:O:spdif"
    assert house["O"].state == "PLAYING"
    assert house["O"].volume == 25


def test_all_rooms_regroups_and_restores(bridge):
    b, house = bridge
    before = snapshot_state(house)
    rep = b.announce("all", "http://c/x.mp3", 45, False, 1.0)
    assert len(rep.rooms) == 5
    # during play everyone was under the leader; afterwards topology is back
    assert snapshot_state(house) == before, rep.warnings
    # bedroom is again a member of the kids room group
    assert house["M"].coordinator_uid == "A"
    assert not rep.warnings, rep.warnings


def test_member_of_group_only(bridge):
    """Announce in the bedroom, which is a member of the kids room group; the kids
    room should be untouched and the bedroom should rejoin afterwards."""
    b, house = bridge
    b.announce(["main bedroom"], "http://c/x.mp3", 30, False, 1.0)
    assert house["A"].play_calls == []
    assert house["M"].coordinator_uid == "A"
    assert house["M"].volume == 10


def test_unknown_room(bridge):
    b, _ = bridge
    with pytest.raises(KeyError):
        b.announce(["Garage"], "http://c/x.mp3", None, False, 1.0)


def test_fuzzy_room_names(bridge):
    b, _ = bridge
    ps = b.resolve(["Kid's Room", "kids", "LIVING ROOM"])
    assert [p.name for p in ps] == ["Kids Room", "Living Room"]


def test_player_ids_resolve_like_cloud_api(bridge):
    """The official Sonos connector addresses players by RINCON id; accept those too."""
    b, house = bridge
    house["K"].uid = "RINCON_ABC01400"
    b.discover(True)
    assert b.player("rincon_abc01400").name == "Kitchen"


# ---- local-only controls ---------------------------------------------------

def test_eq_reports_only_what_the_player_has(bridge):
    from app import controls
    b, _ = bridge
    kitchen = controls.get_eq(b.player("Kitchen"))
    office = controls.get_eq(b.player("Office"))
    assert "sub_gain" not in kitchen and "surround_level" not in kitchen
    assert office["sub_gain"] == 0 and "surround_level" in office
    assert kitchen["balance"] == {"left": 100, "right": 100}


def test_set_eq_returns_previous_and_validates(bridge):
    from app import controls
    b, house = bridge
    out = controls.set_eq(b.player("Office"), {"bass": 3, "sub_gain": -2, "balance": {"left": 100, "right": 80}})
    assert out["previous"]["bass"] == 0 and out["now"]["bass"] == 3
    assert house["O"].sub_gain == -2 and house["O"].balance == (100, 80)
    with pytest.raises(ValueError):
        controls.set_eq(b.player("Office"), {"bass": 11})
    with pytest.raises(ValueError):
        controls.set_eq(b.player("Kitchen"), {"sub_gain": 1})  # no sub


def test_switch_to_tv_only_on_soundbars(bridge):
    from app import controls
    b, house = bridge
    controls.switch_to_tv(b.player("Living Room"))
    assert house["L"].tv_calls == 1
    with pytest.raises(ValueError):
        controls.switch_to_tv(b.player("Kitchen"))


def test_sleep_timer(bridge):
    from app import controls
    b, house = bridge
    out = controls.set_sleep(b.player("Kitchen"), 30)
    assert out["previous"]["remaining_seconds"] is None and house["K"].sleep == 1800
    controls.set_sleep(b.player("Kitchen"), 0)
    assert house["K"].sleep is None


def test_settings(bridge):
    from app import controls
    b, house = bridge
    out = controls.set_settings(b.player("Kitchen"), status_light=False, buttons_enabled=None)
    assert out["previous"]["status_light"] is True and house["K"].status_light is False
    assert house["K"].buttons_enabled is True


@pytest.mark.parametrize("value,code", [
    ("daily", "DAILY"), ("Weekdays", "WEEKDAYS"), (["mon", "wednesday", "sun"], "ON_013"), ("ON_12", "ON_12"),
])
def test_recurrence(value, code):
    from app import controls
    assert controls.recurrence(value) == code


def test_recurrence_rejects_junk():
    from app import controls
    with pytest.raises(ValueError):
        controls.recurrence("fortnightly")
    with pytest.raises(ValueError):
        controls.recurrence(["funday"])


def test_capabilities_are_read_from_device_block():
    from app.sonos import capabilities
    info = {"playerId": "RINCON_X", "device": {"capabilities": ["PLAYBACK", "AUDIO_CLIP"]}}
    assert "AUDIO_CLIP" in capabilities(info)
    assert capabilities({"playerId": "RINCON_X"}) == []
