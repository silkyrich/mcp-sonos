"""Sonos bridge: discovery, announce-with-restore, and the fast audioClip path.

Two playback strategies, chosen per player:

  1. audioClip (fast, clean). Newer players expose a local HTTPS API on
     port 1443 with an `audioClip` endpoint that ducks whatever is playing,
     plays a URL, then resumes — no snapshot/restore dance. We probe each
     player once and remember the answer.

  2. UPnP play_uri + snapshot/restore (works on everything). We record group
     topology and per-player state, temporarily group the targets under one
     coordinator, play the clip, wait for it to end, rebuild the original
     groups and restore transport state. TV/line-in inputs come back via
     their stream URIs; cloud queues are the one thing SoCo can't reliably
     resume, so we try and log rather than pretend.

All SoCo calls are blocking; the FastAPI layer runs `announce` in a thread
and serialises announcements with a lock so two never fight over topology.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import httpx
import soco
from soco import SoCo
from soco.snapshot import Snapshot

from .config import Settings

log = logging.getLogger("announce.sonos")

# Community-known key accepted by the local player API.
LOCAL_API_KEY = "123e4567-e89b-12d3-a456-426655440000"  # gitleaks:allow - public placeholder UUID, not a secret


def capabilities(info: dict) -> list[str]:
    """Capabilities from /players/local/info; they live under `device`."""
    return (info.get("device") or {}).get("capabilities") or info.get("capabilities") or []


def norm(name: str) -> str:
    """'Kid's Room' == 'kids room' == 'Kids-Room'."""
    return "".join(c for c in name.lower() if c.isalnum())


@dataclass
class Player:
    name: str
    ip: str
    uid: str
    dev: SoCo
    audioclip: bool | None = None  # None = not probed yet


@dataclass
class Report:
    rooms: list[str]
    strategy: str
    cached: bool
    timings: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class Bridge:
    def __init__(self, s: Settings):
        self.s = s
        self._players: dict[str, Player] = {}
        self._lock = threading.Lock()
        self._last_discovery = 0.0

    # ----------------------------------------------------------- discovery

    def discover(self, force: bool = False) -> dict[str, Player]:
        if not force and self._players and time.monotonic() - self._last_discovery < 300:
            return self._players
        zones = soco.discover(timeout=self.s.discovery_timeout, allow_network_scan=True) or set()
        found: dict[str, Player] = {}
        for z in zones:
            try:
                p = Player(name=z.player_name, ip=z.ip_address, uid=z.uid, dev=z)
            except Exception as e:  # noqa: BLE001
                log.warning("skip zone %s: %s", getattr(z, "ip_address", "?"), e)
                continue
            prev = self._players.get(norm(p.name))
            if prev:
                p.audioclip = prev.audioclip
            found[norm(p.name)] = p
        if found:
            self._players = found
            self._last_discovery = time.monotonic()
        log.info("discovered %d players: %s", len(self._players),
                 ", ".join(p.name for p in self._players.values()))
        return self._players

    def rooms(self) -> list[dict]:
        out = []
        for p in self.discover().values():
            out.append({"name": p.name, "ip": p.ip, "uid": p.uid,
                        "audioclip": self.probe_audioclip(p)})
        return out

    def resolve(self, rooms: list[str] | str) -> list[Player]:
        players = self.discover()
        if rooms == "all" or rooms == ["all"]:
            return list(players.values())
        if isinstance(rooms, str):
            rooms = [rooms]
        by_uid = {v.uid.upper(): v for v in players.values()}
        out: list[Player] = []
        for r in rooms:
            # room name, or a player id (RINCON_...) as used by the Sonos cloud API
            p = players.get(norm(r)) or by_uid.get(r.strip().upper())
            if not p:
                # allow prefix / substring matches: "bed" -> "Main Bedroom"
                cands = [v for k, v in players.items() if norm(r) in k]
                if len(cands) == 1:
                    p = cands[0]
            if not p:
                raise KeyError(f"unknown room {r!r}; known: {[v.name for v in players.values()]}")
            if p not in out:
                out.append(p)
        return out

    def player(self, room: str) -> Player:
        return self.resolve([room])[0]

    # ----------------------------------------------------------- audioClip

    def probe_audioclip(self, p: Player) -> bool:
        if p.audioclip is not None:
            return p.audioclip
        try:
            r = httpx.get(f"https://{p.ip}:1443/api/v1/players/local/info",
                          headers={"X-Sonos-Api-Key": LOCAL_API_KEY}, verify=False, timeout=3.0)
            p.audioclip = r.status_code == 200 and "AUDIO_CLIP" in capabilities(r.json())
        except Exception as e:  # noqa: BLE001
            log.info("audioclip probe failed for %s: %s", p.name, e)
            p.audioclip = False
        log.info("%s audioclip=%s", p.name, p.audioclip)
        return p.audioclip

    def _audioclip(self, p: Player, url: str, volume: int) -> None:
        body = {"name": "announce", "appId": "com.github.mcp-sonos",
                "streamUrl": url, "volume": volume, "priority": "HIGH"}
        r = httpx.post(f"https://{p.ip}:1443/api/v1/players/local/audioClip",
                       json=body, headers={"X-Sonos-Api-Key": LOCAL_API_KEY},
                       verify=False, timeout=5.0)
        if r.status_code >= 300:
            raise RuntimeError(f"audioClip {p.name} -> {r.status_code} {r.text[:200]}")

    # ----------------------------------------------------------- announce

    def announce(self, rooms: list[str] | str, url: str, volume: int | None,
                 cached: bool, clip_seconds: float | None) -> Report:
        """Play `url` on the rooms. Blocking. Serialised via self._lock."""
        vol = self.s.default_volume if volume is None else max(0, min(100, volume))
        with self._lock:
            targets = self.resolve(rooms)
            fast = [p for p in targets if self.probe_audioclip(p)]
            slow = [p for p in targets if p not in fast]
            rep = Report(rooms=[p.name for p in targets], strategy="", cached=cached)
            t0 = time.monotonic()

            # Fast path players: fire-and-forget, the player ducks and resumes itself.
            for p in fast:
                try:
                    self._audioclip(p, url, vol)
                except Exception as e:  # noqa: BLE001
                    rep.warnings.append(f"audioclip failed on {p.name}, falling back: {e}")
                    slow.append(p)
            rep.timings["audioclip_dispatch"] = time.monotonic() - t0

            if slow:
                self._announce_upnp(slow, url, vol, clip_seconds, rep)

            rep.strategy = ("audioclip" if fast and not slow else
                            "upnp" if slow and not fast else "mixed")
            rep.timings["total"] = time.monotonic() - t0
            return rep

    # -- the slow path -----------------------------------------------------

    def _announce_upnp(self, targets: list[Player], url: str, vol: int,
                       clip_seconds: float | None, rep: Report) -> None:
        t0 = time.monotonic()
        everyone = list(self.discover().values())
        target_uids = {p.uid for p in targets}

        # 1. Snapshot topology + state for every player we might disturb:
        #    the targets, plus anyone sharing a group with a target.
        affected: dict[str, Player] = {}
        topo: dict[str, str] = {}  # uid -> original coordinator uid
        for p in everyone:
            try:
                coord = p.dev.group.coordinator.uid
            except Exception:  # noqa: BLE001
                coord = p.uid
            topo[p.uid] = coord
        for p in everyone:
            in_target_group = topo[p.uid] in {topo[t] for t in target_uids} or p.uid in target_uids
            if in_target_group:
                affected[p.uid] = p
        snaps: dict[str, Snapshot] = {}
        for uid, p in affected.items():
            try:
                snap = Snapshot(p.dev)
                snap.snapshot()
                snaps[uid] = snap
            except Exception as e:  # noqa: BLE001
                rep.warnings.append(f"snapshot failed for {p.name}: {e}")
        rep.timings["snapshot"] = time.monotonic() - t0

        # 2. Build the announce group: leader = first target, others join it.
        leader = targets[0]
        try:
            if not leader.dev.is_coordinator or len(leader.dev.group.members) > 1:
                leader.dev.unjoin()
        except Exception as e:  # noqa: BLE001
            rep.warnings.append(f"unjoin leader {leader.name}: {e}")
        for p in targets[1:]:
            try:
                p.dev.join(leader.dev)
            except Exception as e:  # noqa: BLE001
                rep.warnings.append(f"join {p.name}: {e}")
        for p in targets:
            try:
                p.dev.mute = False
                p.dev.volume = vol
            except Exception as e:  # noqa: BLE001
                rep.warnings.append(f"volume {p.name}: {e}")
        rep.timings["group"] = time.monotonic() - t0

        # 3. Play and wait.
        try:
            leader.dev.play_uri(url, title="Announcement")
            self._wait_for_end(leader.dev, clip_seconds)
        except Exception as e:  # noqa: BLE001
            rep.warnings.append(f"play failed on {leader.name}: {e}")
        rep.timings["played"] = time.monotonic() - t0

        # 4. Rebuild original topology, then restore state.
        by_uid = {p.uid: p for p in everyone}
        for uid, p in affected.items():
            want = topo[uid]
            try:
                cur = p.dev.group.coordinator.uid
            except Exception:  # noqa: BLE001
                cur = None
            if want == uid:
                if cur != uid:
                    _safe(lambda: p.dev.unjoin(), rep, f"restore unjoin {p.name}")
        for uid, p in affected.items():
            want = topo[uid]
            if want != uid:
                coord = by_uid.get(want)
                if coord:
                    _safe(lambda: p.dev.join(coord.dev), rep, f"restore join {p.name}")
        # coordinators first (transport), then everyone (volume)
        for uid, snap in snaps.items():
            if snap.is_coordinator:
                _safe(lambda: self._restore(snap), rep, f"restore {affected[uid].name}")
        for uid, snap in snaps.items():
            if not snap.is_coordinator:
                _safe(lambda: snap._restore_volume(False), rep, f"restore vol {affected[uid].name}")
        rep.timings["restored"] = time.monotonic() - t0

    def _restore(self, snap: Snapshot) -> None:
        """SoCo's restore, plus a best-effort resume for cloud queues (Apple
        Music / Spotify controlled from the Sonos app), which SoCo skips."""
        snap.restore(fade=False)
        if snap.is_playing_cloud_queue and snap.transport_state == "PLAYING":
            try:
                snap.device.play_uri(snap.media_uri, snap.media_metadata or "", start=True)
            except Exception as e:  # noqa: BLE001
                log.warning("cloud queue resume on %s failed: %s", snap.device.player_name, e)

    def _wait_for_end(self, dev: SoCo, clip_seconds: float | None) -> None:
        cap = min(self.s.max_clip_seconds, (clip_seconds or 0) + 8) if clip_seconds else self.s.max_clip_seconds
        deadline = time.monotonic() + cap
        seen_playing = False
        while time.monotonic() < deadline:
            try:
                st = dev.get_current_transport_info()["current_transport_state"]
            except Exception:  # noqa: BLE001
                st = "UNKNOWN"
            if st == "PLAYING":
                seen_playing = True
            elif seen_playing and st in ("STOPPED", "PAUSED_PLAYBACK"):
                return
            time.sleep(0.25)
        log.warning("clip wait hit cap (%.1fs) on %s", cap, dev.player_name)


def _safe(fn, rep: Report, label: str) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        rep.warnings.append(f"{label}: {e}")
