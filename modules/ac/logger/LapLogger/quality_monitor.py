"""Read-only track-limit evidence for original Assetto Corsa.

Compatible with AC's embedded Python 3.3. No ac/ctypes import is required.
The shared-memory prefix follows CSP's AC sim_info.py (pack 4, Windows UTF-16):
https://github.com/ac-custom-shaders-patch/acc-extension-apps/blob/master/apps/python/AccExtHelper/sim_info.py
This is the original AC layout, not ACC's isValidLap layout.
"""

import math
import os
import struct

try:
    import mmap
except ImportError:
    mmap = None


MAX_FRAME_S = 0.25


def _number(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def _integer(value, low, high):
    value = _number(value)
    if value is not None and low <= value <= high and int(value) == value:
        return int(value)
    return None


def _coverage(known, total, gap=False):
    if not known:
        return "unavailable"
    return "complete" if known == total and not gap else "partial"


class SharedMemoryReader:
    """Decode small read-only AC shared-memory prefixes on demand.

    A missing game may cause mmap to create an empty named mapping. Status,
    clock/lap agreement and packet-progress checks prevent treating it as data.
    Repeated packet IDs are allowed briefly: sampling may be faster than updates.
    """

    def __init__(self, opener=None):
        self._opener = opener
        self._maps = {}
        self._progress = {}

    def _map(self, name, size):
        if name in self._maps:
            return self._maps[name]
        try:
            if self._opener is not None:
                handle = self._opener(name, size)
            elif mmap is not None and os.name == "nt":
                handle = mmap.mmap(-1, size, tagname=name, access=mmap.ACCESS_READ)
            else:
                return None
            if handle is not None:
                self._maps[name] = handle
            return handle
        except Exception:
            return None

    def _snapshot(self, name, size):
        handle = self._map(name, size)
        if handle is None:
            return None
        try:
            for _attempt in range(2):
                raw = handle[:size]
                if len(raw) != size or not any(raw):
                    return None
                packet = struct.unpack_from("<i", raw, 0)[0]
                if packet == struct.unpack_from("<i", handle[:4], 0)[0]:
                    return raw
        except Exception:
            self._drop(name)
        return None

    def _drop(self, name):
        handle = self._maps.pop(name, None)
        self._progress.pop(name, None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def _fresh(self, name, packet, lap_t, lap_count):
        old = self._progress.get(name)
        if old is None or packet != old[0] or lap_count != old[1] or lap_t < old[2]:
            self._progress[name] = (packet, lap_count, lap_t)
            return True
        return lap_t - old[2] <= 1000

    def read(self, lap_t, lap_count):
        result = {"tyres_out": None, "penalty_s": None, "flag": None,
                  "is_in_pit": None, "source": None}
        if _number(lap_t) is None or _integer(lap_count, 0, 1000000) is None:
            return result
        raw = self._snapshot("acpmf_graphics", 280)
        if raw is None:
            return result
        packet, status = struct.unpack_from("<ii", raw, 0)
        completed = struct.unpack_from("<i", raw, 132)[0]
        clock = struct.unpack_from("<i", raw, 140)[0]
        pit = struct.unpack_from("<i", raw, 160)[0]
        pit_lane = struct.unpack_from("<i", raw, 276)[0]
        if (status != 2 or completed != lap_count or clock < 0 or
                abs(clock - lap_t) > 1500 or pit not in (0, 1) or pit_lane not in (0, 1) or
                not self._fresh("acpmf_graphics", packet, lap_t, lap_count)):
            return result
        result["is_in_pit"] = bool(pit or pit_lane)
        penalty = struct.unpack_from("<f", raw, 264)[0]
        if math.isfinite(penalty) and 0 <= penalty <= 86400:
            result["penalty_s"] = penalty
        result["flag"] = _integer(struct.unpack_from("<i", raw, 268)[0], 0, 6)
        physics = self._snapshot("acpmf_physics", 248)
        if physics is None:
            return result
        packet = struct.unpack_from("<i", physics, 0)[0]
        if not self._fresh("acpmf_physics", packet, lap_t, lap_count):
            return result
        result["tyres_out"] = _integer(struct.unpack_from("<i", physics, 244)[0], 0, 4)
        if result["tyres_out"] is not None:
            result["source"] = "shared_memory.physics"
        return result

    def close(self):
        for name in list(self._maps):
            self._drop(name)


class QualityMonitor:
    """Feature-detect CSP's optional tyres-out getter, then use shared memory."""

    def __init__(self, shared_reader=None):
        self.shared_reader = shared_reader if shared_reader is not None else SharedMemoryReader()

    def read(self, ac, lap_t, lap_count):
        try:
            evidence = self.shared_reader.read(lap_t, lap_count)
        except Exception:
            evidence = {}
        result = {"tyres_out": _integer(evidence.get("tyres_out"), 0, 4),
                  "penalty_s": evidence.get("penalty_s"), "flag": evidence.get("flag"),
                  "is_in_pit": evidence.get("is_in_pit"), "source": evidence.get("source")}
        getter = getattr(ac, "ext_getNumberOfTyresOut", None)
        if callable(getter):
            try:
                try:
                    raw = getter(0)
                except TypeError:
                    raw = getter()
                tyres = _integer(raw, 0, 4)
            except Exception:
                tyres = None
            if tyres is not None:
                result["tyres_out"] = tyres
                result["source"] = "csp.ext_getNumberOfTyresOut"
        if result["tyres_out"] is None:
            result["source"] = None
        return result

    def close(self):
        try:
            self.shared_reader.close()
        except Exception:
            pass


class LapQuality:
    """Accumulate every observed active frame, including transient one-frame cuts.

    `track_limits_exceeded` is a separate coaching rule (>2 tyres out), never a
    claim that the game invalidated the lap. Unknown evidence does not re-arm an
    excursion. A confirmed return in bounds (or pits) is needed before a new one.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.frames = 0
        self.track_frames = 0
        self.track_known = 0
        self.game_known = 0
        self.sources = set()
        self.game_invalid_seen = False
        self.track_limits_exceeded = False
        self.penalty_seen = False
        self.penalty_max_s = None
        self.off_track_events = 0
        self.off_track_s = 0.0
        self.max_tyres_out = None
        self._off = False
        self._gap = False

    def observe(self, observation, delta_s, game_invalid=None):
        observation = observation or {}
        self.frames += 1
        native = _integer(game_invalid, 0, 1)
        if native is not None:
            self.game_known += 1
            self.game_invalid_seen = self.game_invalid_seen or bool(native)
        delta = _number(delta_s)
        if delta is None or delta < 0 or delta > MAX_FRAME_S:
            self._gap = True
        delta = min(MAX_FRAME_S, max(0.0, delta)) if delta is not None else 0.0
        tyres = _integer(observation.get("tyres_out"), 0, 4)
        pit = observation.get("is_in_pit") is True
        penalty = _number(observation.get("penalty_s"))
        if penalty is not None and 0 <= penalty <= 86400:
            self.penalty_max_s = penalty if self.penalty_max_s is None else max(self.penalty_max_s, penalty)
        if (penalty is not None and penalty > 0) or observation.get("flag") == 6:
            self.penalty_seen = True
        if pit:
            self._off = False
            return False
        self.track_frames += 1
        if tyres is None:
            return None
        self.track_known += 1
        self.max_tyres_out = tyres if self.max_tyres_out is None else max(self.max_tyres_out, tyres)
        self.sources.add(observation.get("source") or "unspecified")
        exceeded = tyres > 2
        if exceeded:
            self.track_limits_exceeded = True
            if not self._off:
                self.off_track_events += 1
            self.off_track_s += delta
        self._off = exceeded
        return exceeded

    def metadata(self):
        source = "unavailable" if not self.sources else next(iter(self.sources)) if len(self.sources) == 1 else "mixed"
        return {"game_validity_source": "ac.LapInvalidated" if self.game_known else "unavailable",
                "game_validity_coverage": _coverage(self.game_known, self.frames, self._gap),
                "track_limits_source": source,
                "track_limits_coverage": _coverage(self.track_known, self.track_frames, self._gap),
                "track_limits_rule": "more_than_2_tyres",
                "game_invalid_seen": int(self.game_invalid_seen),
                "track_limits_exceeded": int(self.track_limits_exceeded),
                "penalty_seen": int(self.penalty_seen),
                "penalty_max_s": self.penalty_max_s if self.penalty_max_s is not None else "unknown",
                "off_track_events": self.off_track_events,
                "off_track_s": round(self.off_track_s, 6),
                "max_tyres_out": self.max_tyres_out if self.max_tyres_out is not None else "unknown"}
