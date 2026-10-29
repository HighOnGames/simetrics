"""LapLogger analysis: compare laps recorded by the LapLogger app corner by corner.

Run with the desktop Python (3.8+, stdlib only):

    py -3.12 lap_analysis.py list
    py -3.12 lap_analysis.py report                      # latest session, latest combo
    py -3.12 lap_analysis.py report --combo silverstone --lap 7 --open
    py -3.12 lap_analysis.py --game fh6 report --open    # Forza Horizon 6 laps (fh6_laplogger.py)
    py -3.12 lap_analysis.py report --focus road         # coaching focus: road | race | mix (default)
    py -3.12 lap_analysis.py --game fh6 tag-device --device auto   # record the input device in older laps

Laps are read from Simetrics' data/<game> folder (--root to override); --game is any installed
game module (modules/<id>/, see modules/README.md).
The report prints a text summary and writes an HTML page with charts to
<data folder>/reports/. Both games write the same lap CSV schema (see LAP_CSV_SCHEMA.md).
"""

import argparse
import html
import json
import math
import os
import re
import statistics
import struct
import sys
import time
import webbrowser
from pathlib import Path

STEP_M = 2.0            # resampling grid
BRAKE_ON = 0.15
FULL_GAS = 0.9
# speeds are handled in km/h internally (thresholds are tuned for it) but shown in mph;
# distances stay in metres
MPH = 1 / 1.609344

# Coaching focus: which advice the user wants. Every tip and habit check below is tagged
# with where it applies, and FOCUS filters or rewords it - one switch for a UI toggle.
#   road - improving real-world driving: road-safe pedal/gear habits; lap-time tips hidden
#   race - lap time in the sim: racecraft allowed (left-foot braking, handbrake, coasting in gear)
#   mix  - both: road habits checked, lap-time tips shown but labelled "(track)"
FOCUS_MODES = ("road", "race", "mix")
DEFAULT_FOCUS = "mix"


STUDIO_DIR = Path(__file__).resolve().parent.parent      # Simetrics (this file is in analysis/)


# ----------------------------------------------------------------- game modules
# Each game is a module in modules/<id>/: module.json (manifest: names, data folder, colours, filters,
# provenance, wording) and an optional adapter.py (hooks: lap metadata, validity, pictures, references).
# The core never names a game: anything game-specific comes from a module, and a hook a module doesn't
# define gets the core default below. Interface: modules/README.md.

CORE_API = 1
MODULES_DIR = Path(__file__).resolve().parent.parent / "modules"

DEFAULT_HOOKS = {
    # validity_info: game flag + independently observed track limits; classify_validity(meta, obs) may
    # replace the observation-based part (called only when the game's own invalid flag is clear)
    "classify_validity": None,
    "VALIDITY_SCOPE": "observed track limits and game flags",
    "PACE_SCOPE": "observed-clear laps (not game certification)",
    "REFERENCE_ROLE": "fastest observed-clear lap (not game certification)",
    "CUT_TEXT": "lap invalidated here",
    "REFERENCE_OPTIONS": {},        # --ref names -> description; reference_lap(core, name, laps) -> Lap or None
    "reference_lap": None,
    "legacy_data_dir": None,        # () -> Path or None: pre-Simetrics lap folder
    # Simetrics (app/engine.py, server.py, prompts.py)
    "display_info": None,           # (game, car, track, layout, lapmeta, lib, settings) -> dict
    "image_file": None,             # (parts, settings) -> Path or None, for /img/<id>/...
    "brief_identity": None,         # (combo) -> dict of extra identity fields
    "brief_identity_lines": None,   # (identity) -> [str]
}


class GameModule:
    def __init__(self, path, manifest, adapter):
        self.path = Path(path)
        self.manifest = manifest
        self.adapter = adapter
        self.id = manifest["id"]
        self.name = manifest.get("name") or self.id
        self.short = manifest.get("short") or self.id.upper()
        self.version = str(manifest.get("version", "0"))
        self.core_api = int(manifest.get("core_api", 1))
        self.data_folder = manifest.get("data_folder") or self.id
        self.order = manifest.get("order", 100)

    def hook(self, name):
        return getattr(self.adapter, name, DEFAULT_HOOKS.get(name)) if self.adapter else DEFAULT_HOOKS.get(name)

    def note(self, key):
        """(order, text) of a wording note from the manifest, or None."""
        n = (self.manifest.get("notes") or {}).get(key)
        if isinstance(n, dict):
            return n.get("order", self.order), n.get("text", "")
        return (self.order, n) if n else None


_MODULES = None
MODULE_ERRORS = {}       # folder name -> {"error", "name", "version", "core_api"} for modules that didn't load


def _load_module(d):
    man = {}
    try:
        man = json.loads((d / "module.json").read_text(encoding="utf-8"))
        if man.get("id") != d.name:
            raise ValueError("id {0!r} does not match the folder name {1!r}".format(man.get("id"), d.name))
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", d.name):
            raise ValueError("module ids are lower-case letters, digits, - and _ (got {0!r})".format(d.name))
        if int(man.get("core_api", 1)) > CORE_API:
            raise ValueError("needs core API {0}; this Simetrics has core API {1}: update Simetrics".format(
                man.get("core_api"), CORE_API))
        adapter = None
        if (d / "adapter.py").is_file():
            import importlib.util
            spec = importlib.util.spec_from_file_location("laplogger_module_" + d.name, d / "adapter.py")
            adapter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(adapter)
        return GameModule(d, man, adapter), None
    except Exception as e:          # one broken module must not stop the others
        return None, {"error": "{0}: {1}".format(type(e).__name__, e), "name": man.get("name") or d.name,
                      "version": man.get("version"), "core_api": man.get("core_api")}


def modules():
    """{id: GameModule} for every installed module that loaded, in display order."""
    global _MODULES
    if _MODULES is None:
        found = []
        MODULE_ERRORS.clear()
        if MODULES_DIR.is_dir():
            for d in sorted(MODULES_DIR.iterdir()):
                if d.is_dir() and not d.name.startswith((".", "_")) and (d / "module.json").is_file():
                    m, err = _load_module(d)
                    if m:
                        found.append(m)
                    else:
                        MODULE_ERRORS[d.name] = err
        _MODULES = {m.id: m for m in sorted(found, key=lambda m: (m.order, m.id))}
    return _MODULES


def module(game):
    return modules().get(game)


def hook(game, name):
    m = module(game)
    return m.hook(name) if m else DEFAULT_HOOKS.get(name)


def notes(key, sep=" "):
    """A manifest wording note from every module, joined in order ('' if none)."""
    found = sorted(n for n in (m.note(key) for m in modules().values()) if n)
    return sep.join(t for _o, t in found)


def headerless_game():
    """The module that owns lap files without a game= header (the oldest logger versions)."""
    return next((m.id for m in modules().values() if m.manifest.get("headerless_laps")), "unknown")


def lap_game(meta):
    return meta.get("game", headerless_game())


def data_folder(game):
    m = module(game)
    return m.data_folder if m else game


def _core():
    """This module, for hooks that build laps (reference_lap)."""
    return sys.modules.get(__name__) or sys.modules.get("lap_analysis")


def default_root(args):
    """Simetrics' data/<game> folder; the module's pre-Simetrics location if it doesn't exist."""
    if args.root:
        return Path(args.root)
    studio = STUDIO_DIR / "data" / data_folder(args.game)
    if studio.is_dir():
        return studio
    legacy = hook(args.game, "legacy_data_dir")
    return (legacy() if legacy else None) or studio


# ----------------------------------------------------------------- loading

def _present_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def validity_info(meta, cols=None):
    """Separate recorded game flags from observed track-limit coverage.

    AC 1.4 and earlier did not observe tyres outside the track. Their positive
    `valid` headers cannot certify track limits. A game module's classify_validity hook can replace the
    observation rules (e.g. a game that only reports rewinds/resets).
    A positive invalid sample always overrides a header claiming validity.
    """
    cols = cols or {}
    game = lap_game(meta)
    raw = meta.get("valid")
    recorded = True if str(raw) == "1" else False if str(raw) == "0" else None
    def flagged(key):
        return any(_present_number(v) and float(v) >= 0.5 for v in cols.get(key, []))
    native_invalid = recorded is False or str(meta.get("game_invalid_seen", "0")) == "1" or flagged("invalid") or flagged("game_invalid")
    coverage = meta.get("track_limits_coverage", "unavailable")
    def source_value(key):
        value = meta.get(key)
        return value if value and value.lower() not in ("unavailable", "unspecified", "none", "unknown") else None
    source = source_value("track_limits_source")
    game_source = source_value("game_validity_source")
    # Logger 1.5's latch excludes pit lane/pit box readings; raw tyre counts do not.
    # Prefer its explicit observation rather than reclassifying pit travel as off-track.
    explicit_limits = "track_limits_exceeded" in cols or "off_track_events" in meta
    outside = flagged("track_limits_exceeded")
    if not explicit_limits:
        outside = outside or any(_present_number(v) and float(v) > 2 for v in cols.get("tyres_out", []))
    outside = outside or float(meta.get("off_track_events", 0) or 0) > 0
    penalty = (str(meta.get("penalty_seen", "0")) == "1"
               or any(_present_number(v) and float(v) > 0 for v in cols.get("penalty_s", []))
               or any(_present_number(v) and float(v) == 6 for v in cols.get("flag", [])))
    custom = None if native_invalid else hook(game, "classify_validity")
    custom = custom and custom(meta, {"recorded": recorded, "outside": outside, "penalty": penalty,
                                      "coverage": coverage, "source": source, "game_source": game_source})
    if native_invalid:
        status, label, eligible = "invalid", "Game invalid / rewind recorded", False
    elif custom:
        status, label, eligible = custom
    elif outside:
        status, label, eligible = "off_track", "Observed off-track (>2 tyres)", False
    elif penalty:
        status, label, eligible = "penalty", "Game penalty observed", False
    elif coverage == "complete" and source and meta.get("game_validity_coverage") == "complete" and game_source:
        status, label, eligible = "observed_clear", "No track-limit breach observed (not game certification)", True
    else:
        status, label, eligible = "unverified", "Track validity unverified", None
    return {"status": status, "label": label, "valid": eligible, "recorded_valid": recorded,
            "game_invalid": True if native_invalid else False if game_source and meta.get("game_validity_coverage") == "complete" else None,
            "game_validity_source": game_source,
            "game_validity_coverage": meta.get("game_validity_coverage", "unavailable"),
            "track_limits_source": source, "track_limits_coverage": coverage,
            "off_track": outside if outside or coverage == "complete" and source else None,
            "off_track_events": int(meta.get("off_track_events", 0) or 0) if source else None,
            "off_track_s": float(meta.get("off_track_s", 0) or 0) if source else None,
            "penalty_observed": penalty, "scope": hook(game, "VALIDITY_SCOPE")}


def continuity_info(cols):
    """Raw time/world-position continuity, before distance resampling drops resets."""
    t = cols.get("t_ms", [])
    repeated = sum(b == a for a, b in zip(t, t[1:]))
    backwards = sum(b < a for a, b in zip(t, t[1:]))
    gaps = [b - a for a, b in zip(t, t[1:])]
    jumps = []
    if all(k in cols for k in ("x", "z", "speed")):
        for i, gap in enumerate(gaps):
            if gap <= 0:
                continue
            vals = [cols[k][j] for k in ("x", "z", "speed") for j in (i, i + 1)]
            if not all(_present_number(v) for v in vals):
                continue
            step = math.hypot(cols["x"][i + 1] - cols["x"][i], cols["z"][i + 1] - cols["z"][i])
            expected = (cols["speed"][i] + cols["speed"][i + 1]) / 2 / 3.6 * gap / 1000.0
            if step - expected > max(20.0, expected * 3):
                jumps.append({"t_ms": t[i + 1], "distance_m": round(step, 2), "gap_ms": gap})
    long_gap = max(gaps, default=0) > 1000
    return {"repeated_timestamps": repeated, "backward_timestamps": backwards,
            "position_discontinuities": jumps, "long_gap": long_gap,
            "pace_usable": not backwards and not jumps and not long_gap}


class Lap:
    def __init__(self, path, meta, cols):
        self.path = Path(path)
        self.meta = meta
        self.cols = cols
        self.time_s = int(meta["lap_time_ms"]) / 1000.0
        self.validity = validity_info(meta, cols)
        self.recorded_valid = self.validity["recorded_valid"]
        self.valid = self.validity["valid"]       # True / False / None (unverified)
        self.continuity = continuity_info(cols)
        self.pace_eligible = self.valid is not False and self.continuity["pace_usable"]
        self.session = meta.get("session", "")
        self.number = int(meta.get("lap_number", 0))
        self.length = float(meta.get("track_length_m", 0) or 0)
        self.game = lap_game(meta)
        # closed = a lap that ends where it started; 0 for point-to-point runs
        self.closed = meta.get("closed", "1") == "1"
        self.grid = None
        # input device as the logger (or tag-device) recorded it; laps without the key are "unknown",
        # never guessed on the fly, so the app, the CLI and the briefs always agree
        dev = meta.get("input_device", "unknown")
        self.device = dev if dev in DEVICES else "unknown"

    @property
    def label(self):
        return "{0} L{1}".format(self.session, self.number)


# ----------------------------------------------------------------- input device
# The loggers record what the game reports after its own input mapping (gas, brake, clutch, steer), so
# any device the game supports gives the same columns. What differs is what the numbers can mean:
#   wheel    - pedals with travel, steering in real degrees of wheel rotation
#   gamepad  - analogue triggers (pressure is real), a self-centring stick: steering degrees are only
#              "stick position x an assumed lock", so steering is judged as a fraction of full lock
#   keyboard - keys are on/off: no pedal pressure; steering moves in steps (or follows the mouse)
#   unknown  - not recorded (laps logged before the device was recorded): judged like a wheel, as before
DEVICES = ("wheel", "gamepad", "keyboard", "unknown")          # display order
DEVICE_LABELS = {"wheel": "Wheel", "gamepad": "Controller", "keyboard": "Keyboard & mouse", "unknown": "Unknown device"}
DEVICE_TRAITS = {
    # pressure: brake/throttle report how far they are pressed; pedals: two feet on pedals (left-foot
    # braking is possible); steer_deg: the steering column is a real wheel angle
    "wheel": {"pressure": True, "pedals": True, "steer_deg": True},
    "gamepad": {"pressure": True, "pedals": False, "steer_deg": False},
    "keyboard": {"pressure": False, "pedals": False, "steer_deg": False},
    "unknown": {"pressure": True, "pedals": True, "steer_deg": True},
}


def traits(device):
    return DEVICE_TRAITS.get(device or "unknown", DEVICE_TRAITS["unknown"])


def device_summary(laps):
    """'wheel' or 'wheel (12 laps), controller (3 laps)'."""
    counts = {}
    for l in laps:
        counts[l.device] = counts.get(l.device, 0) + 1
    if len(counts) == 1:
        return DEVICE_LABELS[next(iter(counts))].lower()
    return ", ".join("{0} ({1} laps)".format(DEVICE_LABELS[d].lower(), counts[d]) for d in DEVICES if d in counts)


def dominant_device(laps):
    """The device most laps were driven with (ties: display order)."""
    counts = {}
    for l in laps:
        counts[l.device] = counts.get(l.device, 0) + 1
    return min(counts, key=lambda d: (-counts[d], DEVICES.index(d))) if counts else "unknown"


def steer_normalised(lap):
    """(steering as -1..1 of full lock, method) from the raw samples, or (None, None). FH6 sends the
    input itself (steer_in); AC only the angle, so it is scaled by steer_lock_deg when known, else by
    the largest angle in the lap (a pad or keyboard reaches full lock in most laps)."""
    c = lap.cols
    if "steer_in" in c:
        return list(c["steer_in"]), "input"
    if "steer" not in c:
        return None, None
    lock = float(lap.meta.get("steer_lock_deg", 0) or 0) / 2.0
    method = "lock"
    if lock <= 0:
        lock, method = max(abs(x) for x in c["steer"]), "lap_max"
    if lock <= 0:
        return None, None
    return [max(-1.0, min(1.0, x / lock)) for x in c["steer"]], method


def device_features(lap):
    """Measurements that tell input devices apart, from the raw samples while moving (> 16 km/h):
    gas_mid / brake_mid: share of pressed samples (> 5%) between 10% and 90% (keys: about 0);
    snaps / flicks per minute: steering from > 40% to the centre, or from the centre to > 60%, within
    80 ms (a released or flicked thumbstick; a wheel can't be turned that fast)."""
    c = lap.cols
    t, n = c["t_ms"], len(c["t_ms"])
    moving = [c["speed"][i] > 16 for i in range(n)]

    def mid(v):
        on = [v[i] for i in range(n) if moving[i] and v[i] > 0.05]
        return (sum(1 for x in on if 0.1 < x < 0.9) / len(on) if on else None), len(on)

    gas_mid, gas_n = mid(c["gas"])
    brake_mid, brake_n = mid(c["brake"])
    s, _method = steer_normalised(lap)
    snaps = flicks = 0
    if s:
        for i in range(n):
            if not moving[i]:
                continue
            a = abs(s[i])
            if a > 0.4 or a < 0.05:
                k = i + 1
                while k < n and t[k] - t[i] <= 80:
                    if (a > 0.4 and abs(s[k]) < 0.05) or (a < 0.05 and abs(s[k]) > 0.6):
                        if a > 0.4:
                            snaps += 1
                        else:
                            flicks += 1
                        break
                    k += 1
    dt = [(t[i + 1] - t[i]) / 1000.0 for i in range(n - 1)]
    minutes = sum(dt[i] for i in range(n - 1) if moving[i]) / 60.0
    hz = (n - 1) / max((t[-1] - t[0]) / 1000.0, 1e-3) if n > 1 else 0
    return {"gas_mid": gas_mid, "brake_mid": brake_mid, "gas_s": gas_n / hz if hz else 0, "brake_s": brake_n / hz if hz else 0,
            "snaps_per_min": snaps / minutes if minutes else None, "flicks_per_min": flicks / minutes if minutes else None,
            "moving_s": minutes * 60}


def classify_device(lap):
    """(device, evidence) guessed from the data, for games that don't say which device was used (FH6).
    keyboard: on/off throttle and brake; gamepad: steering that snaps to and from the centre faster
    than a wheel can turn; wheel: analogue pedals and steering that never snaps. Anything else is
    "unknown". Checked on 23 real FH6 and 58 AC wheel laps (all: wheel) and synthetic pad/keyboard laps."""
    f = device_features(lap)
    ev = "gas_mid={0} brake_mid={1} snaps={2}/min flicks={3}/min".format(*(
        "-" if v is None else "{0:.2f}".format(v) for v in (f["gas_mid"], f["brake_mid"], f["snaps_per_min"], f["flicks_per_min"])))
    if f["moving_s"] < 20 or f["gas_mid"] is None or f["gas_s"] < 3:
        return "unknown", ev + " (too little driving to tell)"
    quick = (f["snaps_per_min"] or 0) + (f["flicks_per_min"] or 0)
    brake_binary = f["brake_mid"] is None or f["brake_s"] < 1 or f["brake_mid"] < 0.12
    if f["gas_mid"] < 0.08 and brake_binary:
        return "keyboard", ev
    if quick >= 2:
        return "gamepad", ev
    # flat-out laps (drifting) hold the throttle at 100%: then the brake has to show the travel
    analog = f["gas_mid"] >= 0.1 or (f["brake_s"] >= 1 and (f["brake_mid"] or 0) >= 0.25)
    if quick < 1 and analog:
        return "wheel", ev
    return "unknown", ev


def load_csv(path):
    meta, header, cols = {}, None, None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                k, _, v = line[1:].strip().partition("=")
                meta[k] = v
            elif header is None:
                header = line.split(",")
                cols = {h: [] for h in header}
            else:
                for h, v in zip(header, line.split(",")):
                    cols[h].append(float(v))
    return Lap(path, meta, cols)


def combos(root):
    return sorted([d for d in root.iterdir() if d.is_dir() and "@" in d.name],
                  key=lambda d: max((f.stat().st_mtime for f in d.glob("*.csv")), default=0))


# ----------------------------------------------------------------- resampling

def interp(xs, ys, x_new):
    out, j, n = [], 0, len(xs)
    for x in x_new:
        while j < n - 2 and xs[j + 1] < x:
            j += 1
        x0, x1 = xs[j], xs[j + 1]
        f = 0.0 if x1 == x0 else min(max((x - x0) / (x1 - x0), 0.0), 1.0)
        out.append(ys[j] + (ys[j + 1] - ys[j]) * f)
    return out


def prepare(lap, length):
    """Resample a lap onto a fixed distance grid (STEP_M metres)."""
    c = lap.cols
    n = len(c["pos"])
    pos = list(c["pos"])
    for i in range(n if lap.closed else 0):
        if i < n * 0.1 and pos[i] > 0.5:
            pos[i] -= 1.0
        elif i > n * 0.9 and pos[i] < 0.5:
            pos[i] += 1.0
    keep, last = [], -1e9
    for i in range(n):
        d = pos[i] * length
        if d > last:
            keep.append(i)
            last = d
    d = [pos[i] * length for i in keep]
    chans = {k: [c[k][i] for i in keep] for k in c if k != "pos"}
    chans["t"] = [v / 1000.0 for v in chans.pop("t_ms")]
    sn, lap.steer_norm_method = steer_normalised(lap)
    if sn is not None:
        # steering as % of full lock, + = right: the device-neutral steering measure
        chans["steer_pct"] = [100.0 * sn[i] for i in keep]
    if "sa_fl" in chans:
        chans["slip_f"] = [(abs(a) + abs(b)) / 2 for a, b in zip(chans["sa_fl"], chans["sa_fr"])]
        chans["slip_r"] = [(abs(a) + abs(b)) / 2 for a, b in zip(chans["sa_rl"], chans["sa_rr"])]
    if d[0] > 0:
        d.insert(0, 0.0)
        for k, v in chans.items():
            v.insert(0, 0.0 if k == "t" else v[0])
    if d[-1] < length:
        d.append(length)
        for k, v in chans.items():
            v.append(lap.time_s if k == "t" else v[-1])
    grid = [i * STEP_M for i in range(int(length // STEP_M) + 1)]
    if grid[-1] < length:
        grid.append(length)
    g = {"d": grid}
    for k, v in chans.items():
        g[k] = interp(d, v, grid)
    if "gear" in g:
        g["gear"] = [int(round(x)) for x in g["gear"]]
    g["t"][-1] = lap.time_s
    g["speed_mph"] = [v * MPH for v in g["speed"]]
    lap.grid = g
    return g


def project_positions(ref, laps):
    """For tracks without an AI spline (pos is always 0): derive pos by projecting every
    sample's world x/z onto the reference lap's driven path. Point-to-point runs
    (ref.closed False) use the open path: no closing segment and no wrap-around."""
    rx, rz = ref.cols["x"], ref.cols["z"]
    n = len(rx)
    cum = [0.0]
    for i in range(1, n):
        cum.append(cum[-1] + math.hypot(rx[i] - rx[i - 1], rz[i] - rz[i - 1]))
    closed = ref.closed
    length = cum[-1] + (math.hypot(rx[0] - rx[-1], rz[0] - rz[-1]) if closed else 0.0)
    win = 300
    for lap in laps:
        xs, zs = lap.cols["x"], lap.cols["z"]
        pos, j = [], None
        for x, z in zip(xs, zs):
            if j is None:
                cand = range(n)
            elif closed:
                cand = (k % n for k in range(j - win, j + win))
            else:
                cand = range(max(0, j - win), min(n, j + win))
            j = min(cand, key=lambda k: (rx[k] - x) ** 2 + (rz[k] - z) ** 2)
            k2 = (j + 1) % n if closed else min(j + 1, n - 1)
            sx, sz = rx[k2] - rx[j], rz[k2] - rz[j]
            seg = sx * sx + sz * sz
            f = min(max(((x - rx[j]) * sx + (z - rz[j]) * sz) / seg, 0.0), 1.0) if seg > 0 else 0.0
            seg_len = math.sqrt(seg) if (k2 or not closed) else length - cum[-1]
            pos.append((cum[j] + f * seg_len) / length)
        lap.cols["pos"] = pos
        lap.length = length
    return length


def smooth(v, half):
    out, n = [], len(v)
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out.append(sum(v[a:b]) / (b - a))
    return out


# ----------------------------------------------------------------- corners

def find_corners(g, min_drop=5.0):
    """Apexes = speed minima with enough drop either side; segments partition the lap."""
    s = smooth(g["speed"], 5)
    n = len(s)
    w = int(30 / STEP_M)
    cand = [i for i in range(1, n - 1) if s[i] == min(s[max(0, i - w):i + w + 1])]
    # drop consecutive duplicates on flat minima
    cand = [c for k, c in enumerate(cand) if k == 0 or c - cand[k - 1] > 1]
    while cand:
        prom = []
        for k, c in enumerate(cand):
            a = cand[k - 1] if k > 0 else 0
            b = cand[k + 1] if k + 1 < len(cand) else n - 1
            prom.append(min(max(s[a:c + 1]), max(s[c:b + 1])) - s[c])
        worst = min(range(len(cand)), key=lambda k: prom[k])
        if prom[worst] >= min_drop:
            break
        cand.pop(worst)
    bounds = []
    for k, c in enumerate(cand):
        a = cand[k - 1] if k > 0 else 0
        bounds.append(max(range(a, c + 1), key=lambda i: s[i]))
    corners = []
    for k, c in enumerate(cand):
        end = bounds[k + 1] if k + 1 < len(cand) else n - 1
        corners.append({"name": "T{0}".format(k + 1), "apex": c, "a": bounds[k], "b": end})
    return corners


def corner_metrics(g, cn, device=None):
    """Per-corner measurements. device (input_device): keyboard has no pedal pressure, so brake_peak is
    None; for a gamepad or keyboard the steering angle is not a wheel angle, so steer_max / steer_work
    (degrees) are None and only the % of full lock versions are given."""
    tr = traits(device)
    a, b = cn["a"], cn["b"]
    d, v = g["d"], g["speed"]
    m = min(range(a, b + 1), key=lambda i: v[i])
    out = {"seg_time": g["t"][b] - g["t"][a], "min_speed": v[m], "min_at": d[m],
           "gear": g.get("gear", [None] * len(d))[m]}
    br = [i for i in range(a, m + 1) if g["brake"][i] > BRAKE_ON]
    out["brake_at"] = d[br[0]] if br else None
    out["brake_peak"] = (max(g["brake"][a:m + 1]) if br else 0.0) if tr["pressure"] else None
    out["entry_speed"] = v[br[0]] if br else v[a]
    rel = [i for i in range(a, b + 1) if g["brake"][i] > 0.05]
    out["brake_release"] = d[rel[-1]] if rel else None
    full = [i for i in range(m, b + 1) if g["gas"][i] >= FULL_GAS]
    out["full_gas_at"] = d[full[0]] if full else None
    ex = min(b, m + int(100 / STEP_M))
    out["exit_speed"] = v[ex]
    out["coast_m"] = STEP_M * sum(1 for i in range(a, b + 1)
                                  if g["gas"][i] < 0.1 and g["brake"][i] < 0.05 and v[i] > 20)
    if "clutch" in g:
        # clutch pedal down (AC: 1 = engaged) for more than a quick shift, from entry to apex
        out["clutch_down_m"] = STEP_M * sum(1 for i in range(a, m + 1) if g["clutch"][i] < 0.5)
    if "steer" in g:
        deg = tr["steer_deg"]
        out["steer_work"] = sum(abs(g["steer"][i + 1] - g["steer"][i]) for i in range(a, b)) if deg else None
        out["steer_max"] = max(abs(x) for x in g["steer"][a:b + 1]) if deg else None
    if "steer_pct" in g:
        out["steer_work_pct"] = sum(abs(g["steer_pct"][i + 1] - g["steer_pct"][i]) for i in range(a, b))
        out["steer_max_pct"] = max(abs(x) for x in g["steer_pct"][a:b + 1])
    if "slip_r" in g:
        out["rear_slip"] = max(g["slip_r"][a:b + 1])
        out["front_slip"] = max(g["slip_f"][a:b + 1])
    if "beta" in g:
        # body slip angle (logger AC 1.3+ / FH6 1.1+); nan when the game build lacks it
        slide = [abs(x) for x in g["beta"][a:b + 1] if x == x]
        if slide:
            out["beta_peak"] = max(slide)
            out["slide_m"] = STEP_M * sum(1 for x in slide if x > 10)
    if "invalid" in g:
        out["cut"] = any(g["invalid"][i] > 0.5 and g["invalid"][max(a, i - 1)] < 0.5 for i in range(a, b + 1))
    if "track_limits_exceeded" in g:
        out["off_track"] = any(g["track_limits_exceeded"][i] > 0.5 and g["track_limits_exceeded"][max(0, i - 1)] < 0.5 for i in range(a, b + 1))
    return out


def advice_items(lap_m, ref_m, cut_text="lap invalidated here", slip_norm=False, device=None, ref_device=None):
    """Corner tips as dicts: id, text, scope. scope "race" = only about lap time (racecraft);
    "both" = also a road habit (control, smoothness, pedal use). slip_norm: slip angles are the
    game's normalised values (FH6: 1 = grip limit), not degrees. device / ref_device: input_device of
    the lap and the reference. Brake points, coasting, apex and exit speed, throttle points and the
    line are distance and timing, so they compare across devices. Pedal pressure and steering are only
    compared between laps on the same kind of device (and pressure never on a keyboard)."""
    tips = []
    same = (device or "unknown") == (ref_device or device or "unknown")

    def tip(tid, scope, text):
        tips.append({"id": tid, "scope": scope, "text": text})

    def both(k):
        return lap_m.get(k) is not None and ref_m.get(k) is not None

    if both("brake_at"):
        diff = ref_m["brake_at"] - lap_m["brake_at"]
        if diff > 8:
            tip("brake_early", "race", "braking {0:.0f} m earlier".format(diff))
        elif diff < -8 and lap_m["min_speed"] < ref_m["min_speed"] - 2:
            tip("brake_late_overslow", "race", "braking {0:.0f} m later but overslowing the apex".format(-diff))
        elif diff < -8:
            tip("brake_late", "race", "braking {0:.0f} m later".format(-diff))
    elif lap_m.get("brake_at") is not None and ref_m.get("brake_at") is None:
        tip("brake_not_needed", "race", "braking where the best lap only lifts")
    dv = lap_m["min_speed"] - ref_m["min_speed"]
    if dv < -3:
        tip("apex_slow", "race", "{0:.0f} mph lower minimum speed".format(-dv * MPH))
    elif dv > 3:
        tip("apex_fast", "race", "{0:.0f} mph higher minimum speed".format(dv * MPH))
    if same and traits(device)["pressure"] and (ref_m.get("brake_peak") or 0) > 0.85 and 0 < (lap_m.get("brake_peak") or 0) < 0.75:
        # firm braking when it is needed matters on the road too (emergency stops)
        tip("brake_soft", "both", "peak brake input {0:.0f}% vs {1:.0f}%".format(100 * lap_m["brake_peak"], 100 * ref_m["brake_peak"]))
    if both("full_gas_at"):
        diff = lap_m["full_gas_at"] - ref_m["full_gas_at"]
        if diff > 15:
            tip("gas_late", "race", "full throttle {0:.0f} m later".format(diff))
    elif ref_m.get("full_gas_at") is not None and lap_m.get("full_gas_at") is None:
        tip("gas_never_full", "race", "never reaching full throttle on exit")
    if lap_m["coast_m"] - ref_m["coast_m"] > 15:
        # off both pedals but in gear = engine braking: good anticipation on the road, slow on track
        tip("coast_more", "race", "{0:.0f} m more coasting".format(lap_m["coast_m"] - ref_m["coast_m"]))
    de = lap_m["exit_speed"] - ref_m["exit_speed"]
    if de < -4:
        tip("exit_slow", "race", "exit {0:.0f} mph slower".format(-de * MPH))
    if slip_norm:
        if "rear_slip" in lap_m and lap_m["rear_slip"] > max(1.0, ref_m.get("rear_slip", 0) * 1.4):
            tip("rear_slide", "both", "rear slip {0:.1f} (game's normalised slip, 1.0 = grip limit)".format(lap_m["rear_slip"]))
    elif "rear_slip" in lap_m and lap_m["rear_slip"] > max(8, ref_m.get("rear_slip", 0) * 1.4):
        tip("rear_slide", "both", "rear slip angle {0:.0f} deg".format(lap_m["rear_slip"]))
    if same and traits(device)["steer_deg"]:
        if lap_m.get("steer_work") is not None and ref_m.get("steer_work") and lap_m["steer_work"] > ref_m["steer_work"] * 1.5 + 20:
            tip("steer_corrections", "both", "steering input variation {0:.0f} deg vs {1:.0f} deg".format(lap_m["steer_work"], ref_m["steer_work"]))
    elif same and lap_m.get("steer_work_pct") is not None and ref_m.get("steer_work_pct"):
        # stick / keys: % of full lock (20 deg of a 900 deg wheel is about 4%)
        if lap_m["steer_work_pct"] > ref_m["steer_work_pct"] * 1.5 + 5:
            tip("steer_corrections", "both", "steering input variation {0:.0f}% of full lock vs {1:.0f}%".format(
                lap_m["steer_work_pct"], ref_m["steer_work_pct"]))
    if both("beta_peak") and lap_m["beta_peak"] > ref_m["beta_peak"] + 15:
        tip("big_slide", "both", "{0:.0f} deg slide vs {1:.0f} on the reference".format(lap_m["beta_peak"], ref_m["beta_peak"]))
    if lap_m.get("clutch_down_m", 0) > 25:
        # absolute, not vs reference: coasting on the clutch is a habit, not a lap-to-lap difference
        tip("clutch_coast", "both", "clutch held down for {0:.0f} m into the corner (coasting, no engine braking)".format(lap_m["clutch_down_m"]))
    if lap_m.get("cut"):
        tip("cut", "race", cut_text)
    if lap_m.get("off_track"):
        tip("off_track", "race", "more than two tyres outside track observed here (independent coaching rule)")
    return tips


def apply_focus(items, focus=None):
    """Filter/label tips for a focus mode. None keeps every tip unlabelled (pre-focus behaviour)."""
    if focus is None or focus == "race":
        return [t["text"] for t in items]
    if focus == "road":
        return [t["text"] for t in items if t["scope"] != "race"]
    return [t["text"] + (" (track)" if t["scope"] == "race" else "") for t in items]


def advice(lap_m, ref_m, cut_text="lap invalidated here", slip_norm=False, focus=None, device=None, ref_device=None):
    return apply_focus(advice_items(lap_m, ref_m, cut_text, slip_norm, device, ref_device), focus)


# ----------------------------------------------------------------- shared session statistics
# One definition, used by this report and by Simetrics (app pages and exported briefs), so the
# numbers agree everywhere: corner statistics come from the comparison laps, and a corner's gap is the
# median section time minus the fastest section time among those laps.

def comparison_laps(laps):
    """Shared explicit population. Known off-track/invalid/discontinuous laps are diagnostic only."""
    valid = [l for l in laps if l.valid is True and l.pace_eligible]
    rolling = [l for l in valid if l.meta.get("standing_start") != "1"]
    if rolling:
        scopes = {hook(l.game, "PACE_SCOPE") for l in rolling}
        scope = scopes.pop() if len(scopes) == 1 else DEFAULT_HOOKS["PACE_SCOPE"]
        return rolling, scope + " that aren't standing starts"
    if valid:
        return valid, "observed-clear / rewind-free laps (every one is a standing start)"
    unknown = [l for l in laps if l.valid is None and l.pace_eligible]
    rolling = [l for l in unknown if l.meta.get("standing_start") != "1"]
    if rolling:
        return rolling, "recorded rolling laps with unverified track validity (no observed-clear laps available)"
    if unknown:
        return unknown, "recorded laps with unverified track validity (all standing starts)"
    return list(laps), "all laps for diagnostics only (none is pace-eligible)"


def reference_laps(laps):
    return ([l for l in laps if l.valid is True and l.pace_eligible]
            or [l for l in laps if l.valid is None and l.pace_eligible] or list(laps))


def select_reference(laps):
    return min(reference_laps(laps), key=lambda l: l.time_s)


def reference_role(lap):
    if not lap.pace_eligible:
        return "diagnostic reference (no pace-eligible laps available)"
    if lap.valid is None:
        return "fastest recorded lap; track validity unverified"
    return hook(lap.game, "REFERENCE_ROLE")


def corner_stats(ms):
    """One corner over the comparison laps. ms = corner_metrics() dicts in lap order. Speeds km/h."""
    segs = [m["seg_time"] for m in ms]
    fastest = min(range(len(ms)), key=lambda i: segs[i])

    def vals(k, only_braking=False):
        # None = not measured for that lap's device (e.g. brake pressure on a keyboard): left out, never 0
        return [m[k] for m in ms if m.get(k) is not None and not (only_braking and m.get("brake_at") is None)]

    def med(k, only_braking=False):
        v = vals(k, only_braking)
        return statistics.median(v) if v else None

    def sd(k):
        v = vals(k)
        return statistics.pstdev(v) if len(v) > 1 else None

    gears = vals("gear")
    return {"n": len(ms), "median_seg": statistics.median(segs), "fastest_seg": segs[fastest], "fastest_index": fastest,
            "gap": statistics.median(segs) - segs[fastest],
            "min_speed": med("min_speed"), "min_speed_sd": sd("min_speed"),
            "brake_at": med("brake_at"), "brake_sd": sd("brake_at"), "braking_laps": len(vals("brake_at")),
            "brake_peak": med("brake_peak", only_braking=True),
            "steer_max": med("steer_max"), "steer_max_pct": med("steer_max_pct"),
            "beta_peak": med("beta_peak"), "coast_m": med("coast_m"),
            "clutch_down_m": med("clutch_down_m"),
            "gear": max(set(gears), key=gears.count) if gears else None}


def theoretical_best(grids, metrics, corners):
    """Fastest time to the first corner plus the fastest time through every corner section, over the
    given laps. Illustrative only: the sections come from different laps."""
    if not grids or not corners:
        return None
    return min(g["t"][corners[0]["a"]] for g in grids) + sum(min(m[k]["seg_time"] for m in metrics) for k in range(len(corners)))


def corner_maps_file():
    return STUDIO_DIR / "data" / "studio" / "corner_maps.json"


def pinned_frame(combo_id):
    """Simetrics pins the lap that defines a car@track's distance frame and corner numbering, so
    T labels don't move when a faster lap is driven. Returns the pin dict or None."""
    try:
        with open(corner_maps_file(), encoding="utf-8") as f:
            return json.load(f).get(combo_id)
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------- habit checks
# Pedal/gear habits checked on every lap. Absolute (not vs a reference lap). The same measurement
# is judged differently per focus: e.g. brake+throttle overlap is left-foot braking (bad habit) for
# road driving, but normal racecraft on track, where only dragging the brake on throttle is flagged.
# Per focus: (label, warn above median per lap or None = report only, meaning when over).
# "devices" overrides the definition for an input device: on a controller or keyboard the overlap is two
# triggers or keys, not two feet, so it says nothing about left-foot braking in a real car.

HABIT_CHECKS = [
    {"key": "two_feet_s", "unit": "s/lap", "focus": {
        "road": ("Brake + throttle pressed together (left-foot braking)", 0.1,
                 "on the road: right foot for both pedals, left foot only for the clutch"),
        "race": ("Brake dragging while on the throttle", 0.5,
                 "left-foot braking is fine on track, but overlapping the pedals this long scrubs speed")},
     "devices": {
         "gamepad": {
             "road": ("Brake + throttle triggers pressed together", None,
                      "two triggers on a controller, not your feet: this doesn't show a road pedal habit"),
             "race": ("Brake dragging while on the throttle (triggers)", 0.5,
                      "overlapping the triggers this long scrubs speed")},
         "keyboard": {
             "road": ("Brake + throttle keys pressed together", None,
                      "two keys, not your feet: this doesn't show a road pedal habit"),
             "race": ("Brake dragging while on the throttle (keys)", 0.5,
                      "holding both keys this long scrubs speed")}}},
    {"key": "clutch_coast_s", "unit": "s/lap", "focus": {
        "road": ("Clutch held down while rolling (> 0.5 s)", 0.5,
                 "coasting: no engine braking, and you must find a gear before you can accelerate"),
        "race": ("Clutch held down while rolling (> 0.5 s)", 0.5,
                 "no drive and no engine braking: the car is unsettled and you lose time")}},
    {"key": "neutral_coast_s", "unit": "s/lap", "focus": {
        "road": ("Rolling in neutral (> 0.5 s)", 0.5, "same as clutch coasting: stay in gear"),
        "race": ("Rolling in neutral (> 0.5 s)", 0.5, "missed or slow gear changes")}},
    {"key": "handbrake_pulls", "unit": "per lap", "focus": {
        "road": ("Handbrake pulls while moving", 0.0, "track-only technique: never in a moving road car"),
        "race": ("Handbrake pulls while moving", None, "")}},
]


def habit_check_def(check, focus, device=None):
    """(label, warn, meaning) for a focus and input device; mix judges habits as road (the stricter one)."""
    f = "race" if focus == "race" else "road"
    return check.get("devices", {}).get(device or "unknown", check["focus"])[f]


def habit_na_reason(key, device):
    """Why a habit check has no value: not recorded by the logger, or not applicable to the device."""
    if key == "clutch_coast_s" and device in ("gamepad", "keyboard"):
        return "not applicable: no clutch used"
    if key == "clutch_coast_s":
        return "not applicable: auto-clutch on, or no clutch recorded"
    return "not recorded by this logger"


def clutch_in_use(lap):
    """True when the driver works a clutch in this lap. Auto-clutch (AC assists.ini, recorded as
    auto_clutch=1) means no; on a controller or keyboard the clutch must actually go down while moving
    (a clutch button); a wheel is assumed to have a pedal, so 0 s of clutch coasting stays a measurement."""
    c = lap.cols.get("clutch")
    if c is None or lap.meta.get("auto_clutch") == "1":
        return False
    if traits(lap.device)["pedals"]:
        return True
    return any(c[i] == c[i] and c[i] < 0.5 and lap.cols["speed"][i] > 16 for i in range(len(c)))


def _spells(flags, t_ms, min_s):
    """Total seconds of runs of True longer than min_s."""
    total, start = 0.0, None
    for i, f in enumerate(flags + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            d = (t_ms[i - 1] - t_ms[start]) / 1000.0
            if d > min_s:
                total += d
            start = None
    return total


def road_habits(lap):
    c = lap.cols
    n = len(c["t_ms"])
    t = c["t_ms"]
    moving = [c["speed"][i] > 16 for i in range(n)]          # ~10 mph
    clutch = c.get("clutch")
    up = [clutch is None or not clutch[i] == clutch[i] or clutch[i] >= 0.5 for i in range(n)]
    both = [c["brake"][i] > 0.1 and c["gas"][i] > 0.1 and up[i] for i in range(n)]
    out = {"two_feet_s": sum((t[i + 1] - t[i]) / 1000.0 for i in range(n - 1) if both[i])}
    out["clutch_coast_s"] = _spells([moving[i] and not up[i] for i in range(n)], t, 0.5) if clutch_in_use(lap) else None
    gear = c.get("gear")
    out["neutral_coast_s"] = _spells([moving[i] and int(gear[i]) == 0 for i in range(n)], t, 0.5) if gear else None
    # handbrake: FH6 sends it; AC has no Python channel, so use a locked rear wheel
    # with the brake pedal off (rear NdSlip far past the limit, AC 1.3+)
    hb = c.get("handbrake")
    if hb is not None:
        flags = [moving[i] and hb[i] > 0.1 for i in range(n)]
    elif "ns_rl" in c:
        def ns(v):
            return v if v == v else 0.0
        flags = [moving[i] and c["brake"][i] < 0.05 and (ns(c["ns_rl"][i]) + ns(c["ns_rr"][i])) / 2 > 1000 for i in range(n)]
    else:
        flags = None
    if flags is None:
        out["handbrake_pulls"] = None
    else:
        # one pull = flagged samples with gaps of at most 0.1 s between them (time-based, so 50 Hz AC
        # and 46 Hz FH6 count the same way)
        pulls, on, last = 0, False, None
        for i, f in enumerate(flags):
            if f:
                if not on:
                    pulls += 1
                on, last = True, t[i]
            elif on and t[i] - last > 100:
                on = False
        out["handbrake_pulls"] = pulls
    return out


def road_summary(laps, focus=DEFAULT_FOCUS, device=None):
    """Median per lap for each check over a set of laps, plus laps over the focus's threshold.
    device: whose definitions to use (default: the device most of the laps were driven with)."""
    per = [road_habits(l) for l in laps]
    device = device or dominant_device(laps)
    res = {}
    for check in HABIT_CHECKS:
        key = check["key"]
        warn = habit_check_def(check, focus, device)[1]
        vals = [p[key] for p in per if p[key] is not None]
        if not vals:
            res[key] = None
            continue
        res[key] = {"median": statistics.median(vals), "max": max(vals),
                    "laps": sum(1 for v in vals if warn is not None and v > warn), "n": len(vals), "total": sum(vals)}
    return res


def road_report(pool, prev, focus=DEFAULT_FOCUS):
    """Text lines and an HTML card for the session's habit checks under a focus, vs the previous session."""
    device = dominant_device(pool)
    cur = road_summary(pool, focus, device)
    old = road_summary(prev, focus, device) if prev else None
    if focus == "race":
        title, intro = "Technique checks", "Pedal and gear technique for lap time, checked on every lap."
    else:
        title, intro = "Road habits", "Habits that carry over to your real car, checked on every lap. Lap time doesn't matter here."
    lines = ["{0} (focus: {1}; input device: {2}; checked on every lap, not vs the reference):".format(
        title, focus, DEVICE_LABELS[device].lower())]
    rows = []
    for check in HABIT_CHECKS:
        key, unit = check["key"], check["unit"]
        label, warn, meaning = habit_check_def(check, focus, device)
        s = cur[key]
        if s is None:
            lines.append("  {0:<6}{1:<56} {2}".format("-", label, habit_na_reason(key, device)))
            continue
        if key == "handbrake_pulls":
            value = "{0} pulls in {1} laps".format(int(s["total"]), s["n"])
            bad = warn is not None and s["total"] > warn
        else:
            value = "median {0:.2f} {1}, {2} of {3} laps over {4:g} s".format(s["median"], unit, s["laps"], s["n"], warn) \
                if warn is not None else "median {0:.2f} {1}".format(s["median"], unit)
            bad = warn is not None and s["median"] > warn
        trend = ""
        if old and old.get(key):
            o = old[key]
            a, b = (o["total"] / o["n"], s["total"] / s["n"]) if key == "handbrake_pulls" else (o["median"], s["median"])
            a, b = round(a, 2), round(b, 2)
            trend = "better" if b < a else "worse" if b > a else "same"
            trend = " ({0} than last session: {1:.2f} -> {2:.2f})".format(trend, a, b) if trend != "same" else " (same as last session)"
        status = "info" if warn is None else "WATCH" if bad else "ok"
        lines.append("  {0:<6}{1:<56} {2}{3}".format(status, label, value, trend))
        if bad:
            lines.append("        -> {0}".format(meaning))
        rows.append((status, label, value + trend, meaning if bad else ""))
    card = ("<div class='card road'><h2>{0}</h2><div class='sub'>{1} Focus: {2}. Input device: {3}.</div><table>".format(
                html.escape(title), html.escape(intro), focus, DEVICE_LABELS[device].lower()) +
            "".join("<tr><td class='{0}'>{1}</td><td>{2}</td><td>{3}</td><td>{4}</td></tr>".format(
                "lose" if st == "WATCH" else "gain" if st == "ok" else "", st, html.escape(lb), html.escape(v), html.escape(m))
                for st, lb, v, m in rows) + "</table></div>")
    return lines, card


# ----------------------------------------------------------------- report

def fmt_lap(s):
    return "{0}:{1:06.3f}".format(int(s // 60), s % 60)


def pick_session(laps, session):
    sessions = sorted({l.session for l in laps if l.session != "AC-best"})
    if not sessions:
        return None
    if session in (None, "latest"):
        return sessions[-1]
    for s in sessions:
        if session in s:
            return s
    sys.exit("No session matching '{0}'. Sessions: {1}".format(session, ", ".join(sessions)))


def analyse(args):
    root = default_root(args)
    if not root.is_dir():
        sys.exit("No laps logged yet ({0} missing). Drive with the LapLogger app on.".format(root))
    cs = combos(root)
    if args.combo:
        exact = [c for c in cs if c.name.lower() == args.combo.lower()]
        cs = exact or [c for c in cs if args.combo.lower() in c.name.lower()]
    if not cs:
        sys.exit("No logged car@track folders found in {0}".format(root))
    combo = cs[-1]
    laps = [load_csv(p) for p in sorted(combo.glob("*.csv"))]
    if not laps:
        sys.exit("No laps in {0}".format(combo))
    session = pick_session(laps, args.session) if args.session != "all" else None
    pool = [l for l in laps if args.session == "all" or l.session == session]
    valid = reference_laps(pool)
    length = next((l.length for l in laps if l.length > 0), 0.0)

    ref = None
    module_ref = hook(args.game, "reference_lap")
    if module_ref and args.ref in hook(args.game, "REFERENCE_OPTIONS"):
        ref = module_ref(_core(), args.ref, laps)
    elif args.ref not in (None, "best"):
        ref = load_csv(args.ref) if os.path.isfile(args.ref) else next(
            (l for l in laps if args.ref in l.path.name), None)
    if ref is None:
        ref = select_reference(laps)

    # distance frame + corner numbering: the lap Simetrics pinned for this car@track, else the reference
    combo_id = "{0}~{1}".format(args.game, combo.name)
    pin = pinned_frame(combo_id)
    frame = next((l for l in laps if pin and l.path.name == pin.get("frame_file")), None) or ref
    if length <= 0 or max(frame.cols.get("pos", [0])) - min(frame.cols.get("pos", [0])) < 0.5:
        if "x" not in frame.cols:
            sys.exit("This track has no AI line and the reference lap has no position data.")
        length = project_positions(frame, list(set(pool) | {ref, frame}))
    for l in set(pool) | {ref, frame}:
        prepare(l, length)
    corners = find_corners(frame.grid)

    if args.lap is not None:
        target = next((l for l in pool if l.number == args.lap), None)
        if target is None:
            sys.exit("Lap {0} not in session {1}".format(args.lap, session))
    else:
        best = min(valid, key=lambda l: l.time_s)
        if best is not ref:
            target = best
        else:
            others = sorted([l for l in valid if l is not ref], key=lambda l: l.time_s)
            target = others[len(others) // 2] if others else ref

    ref_m = [corner_metrics(ref.grid, c, ref.device) for c in corners]
    per_lap = {l: [corner_metrics(l.grid, c, l.device) for c in corners] for l in pool}
    cmp_laps, cmp_desc = comparison_laps(pool)
    stats = [corner_stats([per_lap[l][k] for l in cmp_laps]) for k in range(len(corners))]
    theoretical = theoretical_best([l.grid for l in cmp_laps], [per_lap[l] for l in cmp_laps], corners)

    out = []
    m = ref.meta
    out.append("{0} @ {1}{2}".format(m["car"], m["track"], ("-" + m["layout"]) if m.get("layout") else ""))
    out.append("Session {0}: {1} recorded laps ({2} observed-clear / rewind-free, {3} unverified track validity, {4} known invalid/off-track/penalty). Input device: {5}".format(
        session or "all", len(pool), sum(l.valid is True for l in pool), sum(l.valid is None for l in pool), sum(l.valid is False for l in pool), device_summary(pool)))
    out.append("Reference: " + reference_role(ref))
    if ref.device != dominant_device(pool):
        out.append("  Reference lap driven on: {0} (pedal pressure and steering are only compared between laps on the "
                   "same kind of device)".format(DEVICE_LABELS[ref.device].lower()))
    out.append("Reference: {0} {1}   Theoretical (fastest sections combined, illustrative only): {2}".format(
        ref.label, fmt_lap(ref.time_s), fmt_lap(theoretical)))
    out.append("Corner map: {0}".format(
        "pinned v{0}, from {1}".format(pin.get("version"), frame.label) if pin and frame.path.name == pin.get("frame_file")
        else "from the reference lap {0} (not pinned)".format(ref.label)))
    out.append("")
    sessions = sorted({l.session for l in laps if l.session != "AC-best"})
    prev_s = sessions[sessions.index(session) - 1] if session in sessions and sessions.index(session) > 0 else None
    focus = getattr(args, "focus", DEFAULT_FOCUS)
    road_lines, road_card = road_report(pool, [l for l in laps if l.session == prev_s] if prev_s else None, focus)
    out += road_lines
    out.append("")
    out.append("Laps (seconds lost vs reference, per corner):")
    head = "{0:<20}{1:>10}  ".format("lap", "time") + "".join("{0:>6}".format(c["name"]) for c in corners)
    out.append(head)
    for l in sorted(pool, key=lambda l: (l.session, l.number)):
        row = "{0:<20}{1:>10}  ".format(l.label + ("*" if l.valid is False else "?" if l.valid is None else ""), fmt_lap(l.time_s))
        row += "".join("{0:>6.2f}".format(per_lap[l][k]["seg_time"] - ref_m[k]["seg_time"]) for k in range(len(corners)))
        out.append(row)
    out.append("  (* = known invalid/off-track/penalty; ? = track validity unverified)")
    out.append("")
    out.append("Corner summary over the {0} {1} ({2}); gap = median section time minus the fastest:".format(
        len(cmp_laps), cmp_desc, ", ".join("L{0}".format(l.number) for l in sorted(cmp_laps, key=lambda l: (l.session, l.number)))))
    slide = any(st["beta_peak"] is not None for st in stats)
    out.append("{0:<5}{1:>9}{2:>9}{3:>9}{4:>10}{5:>9}{6:>11}{7:>6}".format(
        "", "at (m)", "gap s", "fastest", "min mph", "+/- mph", "brake +/-m", "gear") + ("{0:>11}".format("slide deg") if slide else ""))
    for k, c in enumerate(corners):
        st = stats[k]
        out.append("{0:<5}{1:>9.0f}{2:>9.2f}{3:>9}{4:>10.0f}{5:>9.1f}{6:>11}{7:>6}".format(
            c["name"], frame.grid["d"][c["apex"]], st["gap"], "L{0}".format(cmp_laps[st["fastest_index"]].number),
            st["min_speed"] * MPH, (st["min_speed_sd"] or 0.0) * MPH,
            "{0:.0f}".format(st["brake_sd"]) if st["brake_sd"] is not None else "-",
            st["gear"] if st["gear"] is not None else "-") +
            ("{0:>11.0f}".format(st["beta_peak"]) if slide and st["beta_peak"] is not None else ""))
    ranked = sorted(range(len(corners)), key=lambda k: -stats[k]["gap"])
    out.append("Biggest gaps to your own fastest section: " +
               ", ".join("{0} {1:.2f}s".format(corners[k]["name"], stats[k]["gap"]) for k in ranked[:3]))
    out.append("")
    out.append("Detail: {0} {1} vs reference {2} {3} ({4:+.3f}s)".format(
        target.label, fmt_lap(target.time_s), ref.label, fmt_lap(ref.time_s), target.time_s - ref.time_s))
    if focus == "road":
        out.append("  (focus road: lap-time-only tips hidden; use --focus mix or race to see them)")
    elif focus == "mix":
        out.append("  ((track) = lap-time technique for the sim, not something to copy on the road)")
    tips_by_corner, detail = [], []
    for k, c in enumerate(corners):
        lm = per_lap[target][k] if target in per_lap else corner_metrics(target.grid, c, target.device)
        dt = lm["seg_time"] - ref_m[k]["seg_time"]
        items = advice_items(lm, ref_m[k], hook(target.game, "CUT_TEXT"),
                             target.meta.get("slip_units") == "normalized", target.device, ref.device)
        tips = apply_focus(items, focus)
        tips_by_corner.append((c, dt, lm, tips))
        detail.append({"corner": c["name"], "dt_s": round(dt, 3), "differences": items})
        if dt > 0.05 or tips:
            out.append("  {0:<4}{1:+.2f}s  {2}".format(c["name"], dt, "; ".join(tips) or "-"))
    text = "\n".join(out)

    if args.json:
        def r(v, n=3):
            return None if v is None else round(v, n)
        print(json.dumps({
            "format": "lap_analysis-report", "version": 1, "combo_id": combo_id, "session": session or "all",
            "laps": [{"lap": "L{0}".format(l.number), "session": l.session, "file": l.path.name, "time_s": l.time_s,
                      "valid": l.valid, "recorded_valid": l.recorded_valid, "validity": l.validity,
                      "pace_eligible": l.pace_eligible,
                      "standing_start": l.meta.get("standing_start") == "1", "input_device": l.device}
                     for l in sorted(pool, key=lambda l: (l.session, l.number))],
            "input_device": dominant_device(pool), "reference_input_device": ref.device,
            "comparison": {"population": cmp_desc, "laps": [l.path.name for l in cmp_laps],
                           "gap_definition": "median section time minus the fastest section time over the comparison laps"},
            "reference": ref.path.name, "reference_role": reference_role(ref),
            "corner_frame": {"file": frame.path.name, "pinned_version": pin.get("version") if pin else None},
            "theoretical_s": r(theoretical), "theoretical_note": "fastest sections from different laps combined: illustrative only",
            "units": {"speed": "km/h", "distance": "m"},
            "corners": [dict(name=c["name"], apex_m=r(frame.grid["d"][c["apex"]], 1), gap_s=r(st["gap"]), median_s=r(st["median_seg"]),
                             fastest_s=r(st["fastest_seg"]), fastest_lap=cmp_laps[st["fastest_index"]].path.name,
                             min_speed_kmh=r(st["min_speed"], 1), min_speed_sd_kmh=r(st["min_speed_sd"], 1),
                             brake_at_m=r(st["brake_at"], 1), brake_sd_m=r(st["brake_sd"], 1), brake_peak_input=r(st["brake_peak"], 2),
                             steer_max_deg=r(st["steer_max"], 0), steer_max_pct=r(st["steer_max_pct"], 0),
                             slide_peak_deg=r(st["beta_peak"], 1), gear=st["gear"], n=st["n"])
                        for c, st in zip(corners, stats)],
            "detail": {"lap": target.path.name, "vs": ref.path.name, "corners": detail},
            "habits": road_summary(pool, focus), "focus": focus,
        }, indent=1))
    else:
        print(text)

    if args.json and not args.out:
        return
    reports = Path(args.out) if args.out else root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / "{0}_{1}.html".format(combo.name, session or "all")
    path.write_text(render_html(ref, target, corners, ref_m, tips_by_corner, text, road_card), encoding="utf-8")
    if not args.json:
        print("\nReport: {0}".format(path))
    if args.open:
        webbrowser.open(path.as_uri())


# ----------------------------------------------------------------- html

def svg_panel(ref_g, lap_g, series, height, corners, ymin=None, ymax=None, zero=False, title=""):
    W, P = 1000, 34
    d = ref_g["d"]
    L = d[-1] or 1
    vals = [v for _, g, k, _ in series for v in g[k]]
    lo = min(vals) if ymin is None else ymin
    hi = max(vals) if ymax is None else ymax
    if zero:
        lo, hi = min(lo, 0), max(hi, 0)
    if hi - lo < 1e-6:
        hi = lo + 1

    def X(x):
        return P + (W - P - 6) * x / L

    def Y(y):
        return 6 + (height - 22) * (1 - (y - lo) / (hi - lo))

    parts = ['<svg viewBox="0 0 {0} {1}" class="panel" role="img" aria-label="{2}">'.format(W, height, html.escape(title))]
    parts.append('<text x="{0}" y="14" class="ttl">{1}</text>'.format(P + 4, html.escape(title)))
    for c in corners:
        x = X(d[c["apex"]])
        parts.append('<line x1="{0:.1f}" x2="{0:.1f}" y1="6" y2="{1}" class="grid"/>'.format(x, height - 16))
        parts.append('<text x="{0:.1f}" y="{1}" class="lbl" text-anchor="middle">{2}</text>'.format(x, height - 4, c["name"]))
    if zero:
        parts.append('<line x1="{0}" x2="{1}" y1="{2:.1f}" y2="{2:.1f}" class="axis"/>'.format(P, W - 6, Y(0)))
    for yv in (lo, hi):
        parts.append('<text x="{0}" y="{1:.1f}" class="lbl" text-anchor="end">{2:.4g}</text>'.format(P - 4, Y(yv) + 4, yv))
    for name, g, k, cls in series:
        pts = " ".join("{0:.1f},{1:.1f}".format(X(x), Y(y)) for x, y in zip(g["d"], g[k]))
        parts.append('<polyline class="{0}" points="{1}"><title>{2}</title></polyline>'.format(cls, pts, html.escape(name)))
    parts.append("</svg>")
    return "".join(parts)


def svg_map(ref_g, lap_g, delta, corners):
    if "x" not in lap_g:
        return ""
    xs, zs = lap_g["x"], lap_g["z"]
    minx, maxx, minz, maxz = min(xs), max(xs), min(zs), max(zs)
    span = max(maxx - minx, maxz - minz) or 1
    S = 520

    def P(i):
        return 10 + (xs[i] - minx) / span * (S - 20), 10 + (zs[i] - minz) / span * (S - 20)

    w = int(20 / STEP_M)
    parts = ['<svg viewBox="0 0 {0} {0}" class="map" role="img" aria-label="Track map coloured by time lost">'.format(S)]
    for i in range(0, len(xs) - w, w):
        rate = (delta[i + w] - delta[i]) / (w * STEP_M) * 1000   # ms lost per metre
        cls = "lose" if rate > 0.4 else "gain" if rate < -0.4 else "even"
        (x0, y0), (x1, y1) = P(i), P(i + w)
        parts.append('<line x1="{0:.1f}" y1="{1:.1f}" x2="{2:.1f}" y2="{3:.1f}" class="{4}"/>'.format(x0, y0, x1, y1, cls))
    for c in corners:
        x, y = P(min(c["apex"], len(xs) - 1))
        parts.append('<text x="{0:.1f}" y="{1:.1f}" class="mlbl">{2}</text>'.format(x + 6, y - 6, c["name"]))
    parts.append("</svg>")
    return "".join(parts)


def render_html(ref, target, corners, ref_m, tips_by_corner, text, road_card=""):
    rg, lg = ref.grid, target.grid
    delta = [a - b for a, b in zip(lg["t"], rg["t"])]
    lg = dict(lg, delta=delta)
    panels = [
        svg_panel(rg, lg, [("reference", rg, "speed_mph", "ref"), ("lap", lg, "speed_mph", "lap")], 220, corners, title="Speed mph"),
        svg_panel(rg, lg, [("delta", lg, "delta", "delta")], 150, corners, zero=True, title="Time vs reference (s, up = losing)"),
        svg_panel(rg, lg, [("ref throttle", rg, "gas", "ref"), ("throttle", lg, "gas", "gas")], 120, corners, 0, 1, title="Throttle"),
        svg_panel(rg, lg, [("ref brake", rg, "brake", "ref"), ("brake", lg, "brake", "brake")], 120, corners, 0, 1, title="Brake"),
    ]
    if "steer" in lg and "steer" in rg and traits(target.device)["steer_deg"] and traits(ref.device)["steer_deg"]:
        panels.append(svg_panel(rg, lg, [("ref steer", rg, "steer", "ref"), ("steer", lg, "steer", "lap")], 130, corners, zero=True, title="Steering deg"))
    elif "steer_pct" in lg and "steer_pct" in rg:
        panels.append(svg_panel(rg, lg, [("ref steer", rg, "steer_pct", "ref"), ("steer", lg, "steer_pct", "lap")], 130, corners,
                                -100, 100, zero=True, title="Steering % of full lock"))
    slide = "beta" in lg and "beta" in rg and all(x == x for x in lg["beta"] + rg["beta"])
    if slide:
        panels.append(svg_panel(rg, lg, [("ref slide", rg, "beta", "ref"), ("slide", lg, "beta", "lap")], 130, corners,
                                zero=True, title="Slide angle deg (+ = sliding right)"))
    if "gear" in lg:
        panels.append(svg_panel(rg, lg, [("ref gear", rg, "gear", "ref"), ("gear", lg, "gear", "lap")], 100, corners, title="Gear"))
    rows = []
    for c, dt, lm, tips in tips_by_corner:
        k = corners.index(c)
        slide_td = "<td>{0:.0f} / {1:.0f}</td>".format(lm["beta_peak"], ref_m[k]["beta_peak"]) if (
            slide and "beta_peak" in lm and "beta_peak" in ref_m[k]) else ("<td>&ndash;</td>" if slide else "")
        rows.append("<tr><td>{0}</td><td class='{1}'>{2:+.2f}</td><td>{3:.0f} / {4:.0f}</td>{6}<td>{5}</td></tr>".format(
            c["name"], "lose" if dt > 0.05 else "gain" if dt < -0.05 else "", dt,
            lm["min_speed"] * MPH, ref_m[k]["min_speed"] * MPH, html.escape("; ".join(tips)) or "&ndash;", slide_td))
    title = "{0} @ {1}".format(ref.meta["car"], ref.meta["track"])
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lap Analysis</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--line:#d9d9d4;--ref:#9a9a94;--lap:#2563eb;--gas:#16a34a;--brake:#dc2626;--lose:#dc2626;--gain:#16a34a;--card:#fff}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#151514;--fg:#ececea;--muted:#9c9c96;--line:#33332f;--ref:#77776f;--lap:#60a5fa;--gas:#4ade80;--brake:#f87171;--lose:#f87171;--gain:#4ade80;--card:#1e1e1c}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:20px 16px;max-width:1100px;margin-inline:auto}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:var(--muted);margin-bottom:16px}}
h2{{font-size:16px;margin:0 0 2px}} .road .sub{{margin-bottom:8px}} .road td:first-child{{font-weight:600}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:14px;overflow-x:auto}}
svg.panel{{width:100%;height:auto;display:block}} svg.map{{width:100%;max-width:520px;display:block;margin:auto}}
polyline{{fill:none;stroke-width:1.6}} .ref{{stroke:var(--ref);stroke-dasharray:4 3}} .lap,.delta{{stroke:var(--lap)}}
.gas{{stroke:var(--gas)}} .brake{{stroke:var(--brake)}} .grid{{stroke:var(--line)}} .axis{{stroke:var(--muted)}}
text{{fill:var(--muted);font-size:11px}} .ttl{{fill:var(--fg);font-weight:600}} .mlbl{{fill:var(--fg);font-size:13px;font-weight:600}}
line.lose{{stroke:var(--lose);stroke-width:5}} line.gain{{stroke:var(--gain);stroke-width:5}} line.even{{stroke:var(--ref);stroke-width:5}}
table{{border-collapse:collapse;width:100%}} td,th{{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}}
td.lose{{color:var(--lose)}} td.gain{{color:var(--gain)}} pre{{white-space:pre;font-size:12px;margin:0}}
</style></head><body>
<h1>{title}</h1>
<div class="sub">{lap} {laptime} ({ldev}) vs reference {ref} {reftime} ({rdev}) &mdash; grey dashed = reference</div>
{road}
<div class="card"><table><tr><th>Corner</th><th>Time (s)</th><th>Min speed lap / ref</th>{slide_th}<th>What changed</th></tr>{rows}</table></div>
<div class="card">{panels}</div>
<div class="card">{map}<div class="sub" style="text-align:center;margin:6px 0 0">red = losing time, green = gaining</div></div>
<div class="card"><pre>{text}</pre></div>
</body></html>""".format(title=html.escape(title), lap=html.escape(target.label), laptime=fmt_lap(target.time_s),
                         ref=html.escape(ref.label), reftime=fmt_lap(ref.time_s), rows="".join(rows),
                         slide_th="<th>Peak slide lap / ref (deg)</th>" if slide else "",
                         panels="".join(panels), map=svg_map(rg, lg, delta, corners) or "No position data for a map.",
                         text=html.escape(text), road=road_card,
                         ldev=DEVICE_LABELS[target.device].lower(), rdev=DEVICE_LABELS[ref.device].lower())


# ----------------------------------------------------------------- list

def list_laps(args):
    root = default_root(args)
    if not root.is_dir():
        sys.exit("No laps logged yet ({0} missing).".format(root))
    for c in combos(root):
        laps = [load_csv(p) for p in sorted(c.glob("*.csv"))]
        print(c.name)
        for s in sorted({l.session for l in laps}):
            ls = [l for l in laps if l.session == s]
            v = [l for l in reference_laps(ls) if l.pace_eligible]
            best = fmt_lap(min(l.time_s for l in v)) if v else "-"
            print("  session {0}: {1} recorded laps, {2} observed-clear / rewind-free, {3} unverified, best recorded eligible {4}, {5}".format(
                s, len(ls), sum(l.valid is True for l in ls), sum(l.valid is None for l in ls), best, device_summary(ls)))


# ----------------------------------------------------------------- tag-device (back-fill input_device)

def write_meta(path, updates):
    """Rewrite a lap file's '# key=value' header with these keys added or changed (sorted, as the loggers
    write it). The samples, the line endings and the file's modification time are kept."""
    path = Path(path)
    st = path.stat()
    with open(path, newline="") as f:
        lines = f.readlines()
    meta, body = {}, 0
    for i, line in enumerate(lines):
        if not line.startswith("#"):
            body = i
            break
        k, _, v = line[1:].strip().partition("=")
        meta[k] = v
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    meta.update({k: str(v) for k, v in updates.items()})
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="") as f:
        f.writelines("# {0}={1}{2}".format(k, meta[k], eol) for k in sorted(meta))
        f.writelines(lines[body:])
    os.replace(tmp, path)
    os.utime(path, (st.st_atime, st.st_mtime))


def tag_device(args):
    """Record input_device in laps logged before it was recorded (or fix a wrong one with --force).
    --device auto guesses from the data (classify_device); a named device is recorded as source=user."""
    root = default_root(args)
    cs = combos(root)
    if args.combo:
        cs = [c for c in cs if args.combo.lower() in c.name.lower()]
    changed = kept = 0
    for c in cs:
        for p in sorted(c.glob("*.csv")):
            lap = load_csv(p)
            if args.session and args.session not in lap.session:
                continue
            if "input_device" in lap.meta and not args.force:
                kept += 1
                continue
            if args.device == "auto":
                dev, ev = classify_device(lap)
                upd = {"input_device": dev, "input_device_source": "heuristic", "input_device_evidence": ev}
            else:
                dev = args.device
                upd = {"input_device": dev, "input_device_source": "user"}
                if args.detail:
                    upd["input_detail"] = args.detail
            upd["input_device_tagged"] = time.strftime("%Y-%m-%d")
            print("{0}/{1}: {2}{3}".format(c.name, p.name, dev, "  (" + upd["input_device_evidence"] + ")" if "input_device_evidence" in upd else ""))
            if not args.dry_run:
                write_meta(p, upd)
            changed += 1
    print("{0} {1} lap(s); {2} already had input_device (use --force to overwrite)".format(
        "Would tag" if args.dry_run else "Tagged", changed, kept))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help="lap data folder (default: Simetrics/data/<game>)")
    mods = modules()
    for name, err in MODULE_ERRORS.items():
        print("! game module {0} not loaded: {1}".format(name, err["error"]), file=sys.stderr)
    p.add_argument("--game", choices=list(mods), default=next(iter(mods), None),
                   help="which game's lap data folder to use ({0})".format(
                       " or ".join("data/" + m.data_folder for m in mods.values())))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list logged car/track combos and sessions")
    r = sub.add_parser("report", help="analyse a session")
    r.add_argument("--combo", help="the car@track folder name; an exact match wins, otherwise a part of it (default: most recent)")
    r.add_argument("--session", default="latest", help="session stamp, 'latest' or 'all'")
    r.add_argument("--lap", type=int, help="lap number to compare in detail")
    r.add_argument("--ref", default="best", help="'best' (all sessions), {0}or a CSV path/name".format("".join(
        "'{0}' ({1} only: {2}) ".format(k, m.short, v) for m in mods.values() for k, v in m.hook("REFERENCE_OPTIONS").items())))
    r.add_argument("--open", action="store_true", help="open the HTML report")
    r.add_argument("--json", action="store_true", help="print the results as JSON (no HTML report unless --out is given)")
    r.add_argument("--out", help="folder for the HTML report (default: <data folder>/reports)")
    r.add_argument("--focus", choices=FOCUS_MODES, default=DEFAULT_FOCUS,
                   help="coaching focus: road (real-world habits, lap-time tips hidden), race (lap time, "
                        "racecraft such as left-foot braking allowed) or mix (both, track tips labelled; default)")
    t = sub.add_parser("tag-device", help="record the input device in laps that don't have it (back-fill)")
    t.add_argument("--device", required=True, choices=["wheel", "gamepad", "keyboard", "auto"],
                   help="the device the laps were driven with, or 'auto' to guess it from each lap's data")
    t.add_argument("--detail", help="optional hardware name, e.g. 'Logitech G920' (stored as input_detail)")
    t.add_argument("--combo", help="only car@track folders containing this text")
    t.add_argument("--session", help="only sessions containing this text")
    t.add_argument("--force", action="store_true", help="overwrite an input_device that is already recorded")
    t.add_argument("--dry-run", action="store_true", help="only print what would change")
    args = p.parse_args()
    {"list": list_laps, "tag-device": tag_device}.get(args.cmd, analyse)(args)


if __name__ == "__main__":
    main()
