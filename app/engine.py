"""Simetrics engine: finds every logged lap of the installed game modules, runs lap_analysis.py
on it and summarises sessions, corners and habits for the web UI.

All lap maths comes from lap_analysis.py (corner detection, corner metrics, advice, habit checks),
so the numbers match the HTML reports. This file only adds per-lap totals (coasting %, full
throttle %, shifts, incidents), per-session aggregation and the trend / "work on next" rules.
Speeds are km/h internally (lap_analysis thresholds) and converted to mph for the UI.
"""

import hashlib
import importlib.util
import json
import math
import os
import re
import statistics
import sys
import threading
import time
from pathlib import Path

# Everything lives in the Simetrics folder:
#   app/         this server + web UI          analysis/   lap_analysis.py (core maths, module loader), schema
#   modules/<id>/ one per game: manifest, adapter, logger   data/<game>/ laps written by the loggers
#   data/studio/ settings, library (names, pictures), analysis cache
STUDIO_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = STUDIO_DIR / "data"
STATE_DIR = DATA_DIR / "studio"
CACHE_DIR = STATE_DIR / "cache"
IMAGES_DIR = STATE_DIR / "images"
LIBRARY_FILE = STATE_DIR / "library.json"
SETTINGS_FILE = STATE_DIR / "settings.json"
ENGINE_VERSION = "6"

# ----------------------------------------------------------------- lap_analysis import

def _load_lap_analysis():
    path = STUDIO_DIR / "analysis" / "lap_analysis.py"
    spec = importlib.util.spec_from_file_location("lap_analysis", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lap_analysis"] = mod           # game-module hooks get the core from here
    spec.loader.exec_module(mod)
    mod.__path_used__ = path
    return mod


la = _load_lap_analysis()
MPH = la.MPH
# installed game modules (modules/<id>/), in display order
MODULES = la.modules()
GAMES = {gid: m.name for gid, m in MODULES.items()}


def lap_roots():
    """data/<folder> of every installed module that has laps. Laps of a removed module stay on disk."""
    return {gid: DATA_DIR / m.data_folder for gid, m in MODULES.items() if (DATA_DIR / m.data_folder).is_dir()}


def disciplines(game):
    """User-settable event types a module offers for its routes (library edit dialog), or []."""
    m = MODULES.get(game)
    return list(((m.manifest.get("filters") or {}).get("disciplines") or [])) if m else []

DEFAULT_SETTINGS = {
    "port": 8765,
    # display units only: data and analysis stay km/h + metres internally
    "units": {"speed": "kmh", "distance": "m"},      # speed: kmh | mph, distance: m | ft
}
UNIT_CHOICES = {"speed": ("kmh", "mph"), "distance": ("m", "ft")}


def units():
    u = dict(DEFAULT_SETTINGS["units"])
    u.update({k: v for k, v in (SETTINGS.get("units") or {}).items() if v in UNIT_CHOICES.get(k, ())})
    return u


def save_settings(patch):
    with _lib_lock:
        s = dict(SETTINGS)
        s.update(patch)
        _write_json(SETTINGS_FILE, s)
        SETTINGS.clear()
        SETTINGS.update(s)
    return SETTINGS

# input devices in display order (wheel first); keys are lap_analysis.DEVICES
DEVICE_LABELS = {"wheel": "Wheel", "gamepad": "Controller", "keyboard": "Keyboard & mouse", "unknown": "Unknown device"}


# ----------------------------------------------------------------- settings, library

def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def load_settings():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    s = dict(DEFAULT_SETTINGS)
    for m in MODULES.values():          # module defaults (e.g. where the game is installed)
        for k, v in (m.manifest.get("settings") or {}).items():
            s.setdefault(k, v)
    s.update(_read_json(SETTINGS_FILE, {}))
    if not SETTINGS_FILE.exists():
        _write_json(SETTINGS_FILE, s)
    return s


SETTINGS = load_settings()
_lib_lock = threading.Lock()


def library():
    """User edits: display names, event types, chosen skins, uploaded pictures (keys are '<game>:...')."""
    lib = _read_json(LIBRARY_FILE, {})
    for k in ("cars", "tracks"):
        lib.setdefault(k, {})
    return lib


def update_library(kind, key, fields):
    with _lib_lock:
        lib = library()
        entry = lib[kind].setdefault(key, {})
        for k, v in fields.items():
            if v in (None, ""):
                entry.pop(k, None)
            else:
                entry[k] = v
        _write_json(LIBRARY_FILE, lib)
    return entry


# ----------------------------------------------------------------- display info (names, classes, images)

def _default_display(game, car, track, layout, lapmeta, lib, settings):
    """Display info for a module without a display_info hook: names from the lap header."""
    car_key = "{0}:{1}".format(game, car)
    track_key = "{0}:{1}{2}".format(game, track, ":" + layout if layout else "")
    cl, tl = lib["cars"].get(car_key, {}), lib["tracks"].get(track_key, {})
    return {"car_key": car_key, "track_key": track_key, "car_name": cl.get("name") or car, "brand": "",
            "track_name": tl.get("name") or track + (" ({0})".format(layout) if layout else ""),
            "event": "Circuit" if lapmeta.get("closed", "1") == "1" else "Point to point",
            "discipline": tl.get("discipline", ""), "car_class": "", "class_sort": "", "drivetrain": "",
            "car_img": cl.get("image"), "track_img": tl.get("image"), "skins": [], "skin": None, "map": None,
            "car_detail": ""}


def meta_for(game, car, track, layout, lapmeta):
    """Display info for a car@track folder (the game module's display_info hook), merged with the user's
    library edits."""
    fn = la.hook(game, "display_info") or _default_display
    return fn(game, car, track, layout, lapmeta, library(), SETTINGS)


# ----------------------------------------------------------------- index (headers only)

def read_meta(path):
    meta = {}
    with open(path) as f:
        for line in f:
            if not line.startswith("#"):
                break
            k, _, v = line[1:].strip().partition("=")
            meta[k] = v
    return meta


def session_time(stamp):
    """yymmdd-HHMMSS -> epoch seconds (local)."""
    try:
        return time.mktime(time.strptime(stamp, "%y%m%d-%H%M%S"))
    except ValueError:
        return 0.0


def combo_id(game, folder):
    return "{0}~{1}".format(game, folder)


def scan():
    combos = []
    for game, root in lap_roots().items():
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or "@" not in d.name:
                continue
            files = sorted(d.glob("*.csv"))
            if not files:
                continue
            metas = []
            for p in files:
                try:
                    m = read_meta(p)
                    m["_file"] = p.name
                    metas.append(m)
                except OSError:
                    pass
            if not metas:
                continue
            m0 = metas[-1]
            car, track, layout = m0.get("car", d.name.split("@")[0]), m0.get("track", ""), m0.get("layout", "")
            info = meta_for(game, car, track, layout, m0)
            sessions = sorted({m.get("session", "") for m in metas})
            statuses = [la.validity_info(m) for m in metas]
            valid = [m for m, v in zip(metas, statuses) if v["valid"] is True]
            unknown = [m for m, v in zip(metas, statuses) if v["valid"] is None]
            devices = _device_counts(metas)
            times = [int(m["lap_time_ms"]) / 1000.0 for m in (valid or unknown) if m.get("lap_time_ms")]
            last = max(session_time(s) for s in sessions) if sessions else 0
            combos.append(dict(info, **{
                "id": combo_id(game, d.name), "game": game, "game_name": GAMES[game],
                "folder": d.name, "car": car, "track": track, "layout": layout,
                "laps": len(metas), "valid": len(valid), "sessions": len(sessions),
                "invalid": sum(v["status"] == "invalid" for v in statuses),
                "off_track": sum(v["off_track"] is True for v in statuses),
                "penalty": sum(v["penalty_observed"] is True for v in statuses),
                "unverified": sum(v["valid"] is None for v in statuses),
                "reference_role": "observed-clear / rewind-free best" if valid else "recorded best; track validity unverified" if unknown else "no pace-eligible best",
                "best": min(times) if times else None, "last": last,
                "closed": m0.get("closed", "1") == "1",
                "devices": devices, "device": devices[0]["device"],
                "fingerprint": _fingerprint(files, combo_id(game, d.name)),
                "warnings": ["{0} of {1} laps in this {2} folder have game={3} in their header".format(
                    len(bad), len(metas), GAMES[game], ", ".join(sorted({la.lap_game(m) for m in bad})))]
                if (bad := [m for m in metas if la.lap_game(m) != game]) else [],
            }))
    return combos


def _meta_device(m):
    d = m.get("input_device", "unknown")
    return d if d in la.DEVICES else "unknown"


def _device_counts(metas):
    """[{device, label, laps, sessions, best}] from lap headers, most laps first (ties: wheel, controller,
    keyboard, unknown)."""
    out = []
    for dev in la.DEVICES:
        ms = [m for m in metas if _meta_device(m) == dev]
        if not ms:
            continue
        valid = [m for m in ms if la.validity_info(m)["valid"] is True]
        unknown = [m for m in ms if la.validity_info(m)["valid"] is None]
        times = [int(m["lap_time_ms"]) / 1000.0 for m in (valid or unknown) if m.get("lap_time_ms")]
        out.append({"device": dev, "label": DEVICE_LABELS[dev], "laps": len(ms),
                    "sessions": len({m.get("session", "") for m in ms}), "best": min(times) if times else None})
    out.sort(key=lambda x: (-x["laps"], la.DEVICES.index(x["device"])))
    return out or [{"device": "unknown", "label": DEVICE_LABELS["unknown"], "laps": 0, "sessions": 0, "best": None}]


def _fingerprint(files, cid=None):
    pin = (corner_maps().get(cid) or {}) if cid else {}
    h = hashlib.sha1((ENGINE_VERSION + str(la.__path_used__.stat().st_mtime) + "|" + str(pin.get("version"))
                      + "|" + str(pin.get("frame_file"))).encode())
    for p in files:
        st = p.stat()
        h.update("{0}|{1}|{2}".format(p.name, st.st_size, int(st.st_mtime)).encode())
    return h.hexdigest()[:16]


def combo_dir(game, folder):
    root = lap_roots().get(game)
    if root is None:
        raise KeyError(game)
    d = (root / folder).resolve()
    if d.parent != root.resolve() or not d.is_dir():
        raise KeyError(folder)
    return d


# ----------------------------------------------------------------- deleting car/track combos

def _combo_storage(combo):
    """Every file the app keeps for one combo: the lap folder, its HTML reports and the analysis cache."""
    d = combo_dir(combo["game"], combo["folder"])
    files = [p for p in d.rglob("*") if p.is_file()]
    reports = d.parent / "reports"
    pat = re.compile(r"^{0}_(\d{{6}}-\d{{6}}|all)\.html$".format(re.escape(combo["folder"])))
    if reports.is_dir():
        files += [p for p in reports.iterdir() if p.is_file() and pat.match(p.name)]
    cache = CACHE_DIR / "{0}.json".format(re.sub(r"[^\w@.-]", "_", combo["id"]))
    if cache.is_file():
        files.append(cache)
    return d, files


def delete_combos(combos, kind, remaining, dry_run=True):
    """Delete the lap folders, reports, analysis caches and corner maps of `combos`. When no remaining
    combo uses the car (kind "cars") or track (kind "tracks"), its library entry and picture go too.
    dry_run only lists what would go. Files are removed for good, not sent to the Recycle Bin."""
    plan = []
    for c in combos:
        d, files = _combo_storage(c)
        plan.append((c, d, files))
    keys = {c["car_key"] if kind == "cars" else c["track_key"] for c in combos}
    keys -= {c["car_key"] if kind == "cars" else c["track_key"] for c in remaining}
    lib = library()[kind]
    images = [IMAGES_DIR / e["image"].split("/img/user/")[1].split("?")[0]
              for k in keys if (e := lib.get(k)) and "/img/user/" in e.get("image", "")]
    images = [p for p in images if p.is_file()]
    out = {
        "combos": [{"id": c["id"], "laps": c["laps"], "folder": c["folder"]} for c, _, _ in plan],
        "laps": sum(c["laps"] for c, _, _ in plan),
        "files": sum(len(f) for _, _, f in plan) + len(images),
        "bytes": sum(p.stat().st_size for _, _, f in plan for p in f) + sum(p.stat().st_size for p in images),
        "library_entries": sorted(k for k in keys if k in lib),
        "errors": [],
    }
    if dry_run:
        return out
    for c, d, files in plan:
        for p in files:
            try:
                p.unlink()
            except OSError as e:
                out["errors"].append("{0}: {1}".format(p.name, e.strerror or e))
        for sub in sorted((p for p in d.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            try:
                sub.rmdir()
            except OSError:
                pass
        try:
            d.rmdir()
        except OSError as e:
            out["errors"].append("{0}: {1}".format(d.name, e.strerror or e))
        _grid_cache.pop(c["id"], None)
        for k in [k for k in _lap_cache if k[0] == c["id"]]:
            _lap_cache.pop(k)
    for p in images:
        try:
            p.unlink()
        except OSError as e:
            out["errors"].append("{0}: {1}".format(p.name, e.strerror or e))
    with _lib_lock:
        maps = corner_maps()
        if [maps.pop(c["id"]) for c in combos if c["id"] in maps]:
            _write_json(la.corner_maps_file(), maps)
        lib_all = library()
        if [lib_all[kind].pop(k) for k in keys if k in lib_all[kind]]:
            _write_json(LIBRARY_FILE, lib_all)
    return out


# ----------------------------------------------------------------- per-lap maths (on top of lap_analysis)

def _nan(v):
    return v != v


def gear_changes(gears, t_ms):
    """Shifts per lap, ignoring neutral blips under 0.15 s (some games report N for 1-4 samples during every shift)."""
    runs = []
    for g, t in zip(gears, t_ms):
        g = int(round(g))
        if runs and runs[-1][0] == g:
            runs[-1][2] = t
        else:
            runs.append([g, t, t])
    runs = [r for r in runs if not (r[0] == 0 and r[2] - r[1] < 150)]
    merged = []
    for g, _t0, _t1 in runs:
        if merged and merged[-1] == g:
            continue
        merged.append(g)
    return max(0, len(merged) - 1)


def incidents(lap, ref_g, corners):
    """Spins (|slide angle| > 60 deg, needs the beta channel) and big slowdowns (under 45% of the
    reference's speed at the same spot and at least 25 mph down). One per 60 m. The first 200 m
    of a standing-start lap are skipped."""
    g = lap.grid
    out = []
    has_beta = "beta" in g and any(not _nan(x) for x in g["beta"])
    n = min(len(g["d"]), len(ref_g["d"]))
    last = -1e9
    for i in range(n):
        d = g["d"][i]
        if d < (200 if lap.meta.get("standing_start") == "1" else 30) or d > g["d"][-1] - 20:
            continue
        kind = None
        if has_beta and not _nan(g["beta"][i]) and abs(g["beta"][i]) > 60:
            kind = "spin"
        elif g["speed"][i] < 0.45 * ref_g["speed"][i] and ref_g["speed"][i] - g["speed"][i] > 40:
            kind = "slowdown"
        if kind and d - last > 60:
            cn = next((c["name"] for c in corners if ref_g["d"][c["a"]] <= d <= ref_g["d"][c["b"]]), "")
            out.append({"d": round(d), "kind": kind, "corner": cn})
            last = d
        elif kind:
            last = d
    return out


def lap_totals(lap):
    g = lap.grid
    n = len(g["d"])
    coast = sum(1 for i in range(n) if g["gas"][i] < 0.1 and g["brake"][i] < 0.05 and g["speed"][i] > 20)
    full = sum(1 for i in range(n) if g["gas"][i] >= la.FULL_GAS)
    out = {
        "coast_pct": 100.0 * coast / n,
        "full_pct": 100.0 * full / n,
        "shifts": gear_changes(lap.cols["gear"], lap.cols["t_ms"]) if "gear" in lap.cols else None,
        "top_mph": max(g["speed"]) * MPH,
        "dist_m": g["d"][-1],
    }
    if "beta" in g:
        b = [abs(x) for x in g["beta"] if not _nan(x)]
        out["slide_m"] = la.STEP_M * sum(1 for x in b if x > 10) if b else None
    return out


def _r(v, nd=2):
    if v is None or (isinstance(v, float) and (_nan(v) or math.isinf(v))):
        return None
    return round(v, nd) if isinstance(v, float) else v


def _clean_metrics(m):
    out = {}
    for k, v in m.items():
        if isinstance(v, float):
            v = _r(v, 3)
        out[k] = v
    return out


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _stdev(vals):
    vals = [v for v in vals if v is not None]
    return statistics.pstdev(vals) if len(vals) > 1 else None


# ----------------------------------------------------------------- corner maps (pinned distance frame)
# The lap that defines a car@track's distance frame (x/z projection) and corner numbering is pinned in
# data/studio/corner_maps.json, so T labels don't move when a faster lap is driven. The fastest valid lap
# is still the reference for time comparisons. Re-mapping makes a new version; lap_analysis.py reads the
# same file, so the CLI uses the same frame.

def corner_maps():
    return _read_json(la.corner_maps_file(), {})


def _save_corner_map(cid, entry):
    with _lib_lock:
        maps = corner_maps()
        maps[cid] = entry
        la.corner_maps_file().parent.mkdir(parents=True, exist_ok=True)
        _write_json(la.corner_maps_file(), maps)


def remap(cid, frame_file, reason="re-mapped to the current best lap"):
    old = corner_maps().get(cid) or {}
    _save_corner_map(cid, {"frame_file": frame_file, "version": int(old.get("version", 0)) + 1,
                           "created": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason})


def _frame_file(cid, files, best_file):
    pin = corner_maps().get(cid)
    if pin and pin.get("frame_file") in files:
        return pin["frame_file"]
    remap(cid, best_file, "first analysis" if not pin else "the pinned lap file is missing: re-mapped to the best lap")
    return best_file


def _sha1(path):
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:12]


def _corner_set(cid, frame, corners, method):
    pin = corner_maps().get(cid) or {}
    g = frame.grid
    entry = dict(pin, frame_file=frame.path.name, frame_sha1=pin.get("frame_sha1") or _sha1(frame.path),
                 method=method, length_m=round(g["d"][-1], 1),
                 corners=[{"name": c["name"], "from_m": round(g["d"][c["a"]], 1), "apex_m": round(g["d"][c["apex"]], 1),
                           "to_m": round(g["d"][c["b"]], 1)} for c in corners])
    if entry != pin:
        _save_corner_map(cid, entry)
    return {"id": "{0}#v{1}".format(cid, entry.get("version", 1)), "version": entry.get("version", 1),
            "frame_file": frame.path.name, "frame_lap": "L{0}".format(frame.number), "frame_session": frame.session,
            "frame_sha1": entry["frame_sha1"], "method": method, "length_m": entry["length_m"],
            "created": entry.get("created"), "reason": entry.get("reason")}


def _needs_projection(lap):
    return lap.length <= 0 or max(lap.cols.get("pos", [0])) - min(lap.cols.get("pos", [0])) < 0.5


DISTANCE_METHODS = {True: "x/z positions projected onto the corner-map lap's driven path",
                    False: "the game's track spline position (pos) x track length"}


# ----------------------------------------------------------------- data quality and channel provenance

CHANNELS = ("speed", "gas", "brake", "clutch", "steer", "gear", "x", "z", "beta", "yaw_rate", "handbrake",
            "sa_rl", "ns_rl", "tyre_t_fl", "susp_fl")

def provenance(game):
    """Where each channel comes from in this game (module manifest), for the UI and the briefs."""
    m = MODULES.get(game)
    return dict((m.manifest.get("provenance") or {})) if m else {}


def lap_quality(lap):
    """Sampling continuity, distance cross-check and channel availability from the raw samples."""
    c = lap.cols
    t = c["t_ms"]
    n = len(t)
    gaps = [t[i + 1] - t[i] for i in range(n - 1)]
    span = (t[-1] - t[0]) / 1000.0 if n > 1 else 0
    q = {"samples": n, "hz": _r((n - 1) / span, 1) if span > 0 else None,
         "max_gap_ms": max(gaps) if gaps else None, "non_monotonic": sum(1 for g in gaps if g <= 0)}
    q.update(lap.continuity)
    if "x" in c and "z" in c and n > 1:
        path = sum(math.hypot(c["x"][i + 1] - c["x"][i], c["z"][i + 1] - c["z"][i]) for i in range(n - 1))
        by_speed = sum((c["speed"][i] + c["speed"][i + 1]) / 2 / 3.6 * gaps[i] / 1000.0 for i in range(n - 1) if gaps[i] > 0)
        q["distance_check_pct"] = _r(100.0 * (by_speed / path - 1), 2) if path > 0 else None
    ch = {}
    for k in CHANNELS:
        v = c.get(k)
        if v is None:
            ch[k] = "missing"
            continue
        real = [x for x in v if not _nan(x)]
        ch[k] = "unavailable" if not real else "constant" if max(real) == min(real) else "ok"
    q["channels"] = ch
    w = []
    if q["max_gap_ms"] is not None and q["max_gap_ms"] > 150:
        w.append("gap of {0} ms between samples".format(int(q["max_gap_ms"])))
    if q["repeated_timestamps"]:
        w.append("{0} repeated timestamps".format(q["repeated_timestamps"]))
    if q["backward_timestamps"]:
        w.append("{0} timestamps went backwards".format(q["backward_timestamps"]))
    if q["position_discontinuities"]:
        w.append("{0} position discontinuity / reset candidate(s); excluded from pace statistics".format(len(q["position_discontinuities"])))
    if q.get("distance_check_pct") is not None and abs(q["distance_check_pct"]) > 2:
        w.append("speed-integrated distance differs from the position path by {0:+.1f}%".format(q["distance_check_pct"]))
    q["warnings"] = w
    return q


def _quality_summary(rows):
    qs = [r["quality"] for r in rows]
    chans = {}
    for k in CHANNELS:
        states = {q["channels"].get(k, "missing") for q in qs}
        chans[k] = states.pop() if len(states) == 1 else "mixed: " + ", ".join(sorted(states))
    return {"laps": len(qs), "max_gap_ms": max((q["max_gap_ms"] or 0) for q in qs) if qs else None,
            "hz": _median([q["hz"] for q in qs]),
            "distance_check_pct": _median([q.get("distance_check_pct") for q in qs]),
            "laps_with_warnings": [{"file": r["file"], "lap": "L{0}".format(r["number"]), "warnings": r["quality"]["warnings"]}
                                   for r in rows if r["quality"]["warnings"]],
            "channels": chans}


# ----------------------------------------------------------------- combo analysis

_grid_cache = {}           # combo id -> (fingerprint, laps by file, ref, frame, corners, method)
_grid_lock = threading.Lock()
_combo_locks = {}


def _load_combo(game, folder, fingerprint=None):
    """Load and prepare every lap of a car@track folder the way lap_analysis.analyse does: the fastest
    valid lap is the reference; the pinned corner-map lap defines the distance frame and corners."""
    cid = combo_id(game, folder)
    lock = _combo_locks.setdefault(cid, threading.Lock())
    with lock:
        hit = _grid_cache.get(cid)
        d = combo_dir(game, folder)
        files = sorted(d.glob("*.csv"))
        fp = fingerprint or _fingerprint(files, cid)
        if hit and hit[0] == fp:
            return hit[1:]
        laps = [la.load_csv(p) for p in files]
        byfile = {l.path.name: l for l in laps}
        ref = la.select_reference(laps)
        frame = byfile[_frame_file(cid, byfile, ref.path.name)]
        proj = _needs_projection(frame)
        length = la.project_positions(frame, laps) if proj else next((l.length for l in laps if l.length > 0), frame.length)
        for l in laps:
            la.prepare(l, length)
        corners = la.find_corners(frame.grid)
        out = (byfile, ref, frame, corners, DISTANCE_METHODS[proj])
        with _grid_lock:
            _grid_cache[cid] = (fp,) + out
            while len(_grid_cache) > 4:
                _grid_cache.pop(next(iter(_grid_cache)))
        return out


_lap_cache = {}            # (combo id, fingerprint) -> {"frame", "length", "proj", "corners", "laps": {file: Lap}}


def _load_laps(combo, frame_file, files):
    """Just these laps (plus the corner-map lap), prepared exactly as in _load_combo: every lap is projected
    onto the frame lap's path on its own, so the grids are identical to the full analysis without
    loading the whole folder (a lap page opens in well under a second)."""
    cid, fp = combo["id"], combo["fingerprint"]
    full = _grid_cache.get(cid)
    if full and full[0] == fp:
        return {f: full[1][f] for f in files if f in full[1]}, full[3], full[4]
    lock = _combo_locks.setdefault(cid, threading.Lock())
    with lock:
        d = combo_dir(combo["game"], combo["folder"])
        entry = _lap_cache.get((cid, fp))
        if entry is None:
            frame = la.load_csv(d / frame_file)
            proj = _needs_projection(frame)
            length = la.project_positions(frame, [frame]) if proj else frame.length
            la.prepare(frame, length)
            entry = {"frame": frame, "length": length, "proj": proj, "corners": la.find_corners(frame.grid),
                     "laps": {frame_file: frame}}
            for k in [k for k in _lap_cache if k[0] == cid]:
                _lap_cache.pop(k)
            _lap_cache[(cid, fp)] = entry
            while len(_lap_cache) > 6:
                _lap_cache.pop(next(iter(_lap_cache)))
        for f in files:
            if f in entry["laps"] or not (d / f).is_file():
                continue
            lap = la.load_csv(d / f)
            if entry["proj"]:
                la.project_positions(entry["frame"], [lap])     # projects lap only; the frame keeps its pos
            la.prepare(lap, entry["length"])
            entry["laps"][f] = lap
            if len(entry["laps"]) > 40:
                entry["laps"].pop(next(k for k in entry["laps"] if k != frame_file))
        return {f: entry["laps"][f] for f in files if f in entry["laps"]}, entry["frame"], entry["corners"]


def _population(laps):
    """lap_analysis.comparison_laps plus the exclusions, for the briefs."""
    cmp_laps, desc = la.comparison_laps(laps)
    keep = {l.path.name for l in cmp_laps}
    excluded = [{"file": l.path.name, "lap": "L{0}".format(l.number),
                 "reason": l.validity["label"] if l.valid is False else "data discontinuity" if not l.pace_eligible else "standing start" if l.meta.get("standing_start") == "1" else "different validity population"}
                for l in laps if l.path.name not in keep]
    return cmp_laps, {"description": desc, "laps": ["L{0}".format(l.number) for l in cmp_laps],
                      "files": sorted(keep), "excluded": excluded}


def _status_counts(laps):
    return {"valid": sum(l.valid is True for l in laps),
            "invalid": sum(l.validity["status"] == "invalid" for l in laps),
            "off_track": sum(l.validity["off_track"] is True for l in laps),
            "penalty": sum(l.validity["penalty_observed"] is True for l in laps),
            "unverified": sum(l.valid is None for l in laps)}


def _typical_population(laps, rows_by_file):
    clear = [l for l in laps if rows_by_file[l.path.name]["clean"] and l.valid is True]
    unknown = [l for l in laps if rows_by_file[l.path.name]["clean"] and l.valid is None]
    picked = clear or unknown
    scope = "observed-clear / rewind-free" if clear else "track validity unverified" if unknown else "none eligible"
    return picked, "median of rolling laps without detected major incidents or continuity failures ({0})".format(scope)


def _best_time(laps):
    eligible = [l for l in la.reference_laps(laps) if l.pace_eligible]
    return min((l.time_s for l in eligible), default=None)


def analyse_combo(combo):
    """Summary of one car@track folder: laps, sessions, corners, time-loss map, quality. Cached on disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / "{0}.json".format(re.sub(r"[^\w@.-]", "_", combo["id"]))
    old = _read_json(cache, None)
    if old and old.get("fingerprint") == combo["fingerprint"]:
        return old
    byfile, ref, frame, corners, method = _load_combo(combo["game"], combo["folder"], combo["fingerprint"])
    laps = sorted(byfile.values(), key=lambda l: (l.session, l.number, l.path.name))
    rg, fg = ref.grid, frame.grid
    slip_norm = ref.meta.get("slip_units") == "normalized"
    ref_m = [la.corner_metrics(rg, c, ref.device) for c in corners]
    per_lap = {l.path.name: [la.corner_metrics(l.grid, c, l.device) for c in corners] for l in laps}

    lap_rows = []
    for l in laps:
        inc = incidents(l, rg, corners)
        standing = l.meta.get("standing_start") == "1"
        lap_rows.append({
            "file": l.path.name, "session": l.session, "number": l.number,
            "time": l.time_s, "valid": l.valid, "recorded_valid": l.recorded_valid,
            "validity": l.validity, "pace_eligible": l.pace_eligible, "standing": standing,
            "incidents": inc,
            # Compatibility key; public wording is "without detected major incidents".
            # Unverified legacy validity is disclosed separately, never certified as clean.
            "clean": l.pace_eligible and not standing and not inc,
            "corners": [_clean_metrics(m) for m in per_lap[l.path.name]],
            "habits": {k: _r(v, 3) for k, v in la.road_habits(l).items()},
            "totals": {k: _r(v, 2) for k, v in lap_totals(l).items()},
            "tips": [[[t["id"], t["scope"]] for t in la.advice_items(per_lap[l.path.name][k], ref_m[k], slip_norm=slip_norm,
                                                                    device=l.device, ref_device=ref.device)]
                     for k in range(len(corners))],
            "device": l.device, "device_source": l.meta.get("input_device_source", "none"),
            "device_detail": l.meta.get("input_detail", ""),
            "logger": l.meta.get("logger_version", ""),
            "rewinds": int(l.meta.get("rewinds", 0) or 0),
            "quality": lap_quality(l),
        })
    rows_by_file = {r["file"]: r for r in lap_rows}

    sessions = []
    for s in sorted({l.session for l in laps}):
        sl = [l for l in laps if l.session == s]
        typical, typical_definition = _typical_population(sl, rows_by_file)
        clean = [l for l in sl if rows_by_file[l.path.name]["clean"]]
        rows = [rows_by_file[l.path.name] for l in sl]
        cmp_laps, pop = _population(sl)
        sc = _session_corners(cmp_laps, per_lap, corners, ref_m)
        sessions.append({
            "session": s, "start": session_time(s), "laps": len(sl), **_status_counts(sl), "clean": len(clean),
            "typical_definition": typical_definition,
            "best_role": la.reference_role(la.select_reference(sl)),
            "device": la.dominant_device(sl), "devices": _device_counts([l.meta for l in sl]),
            "standing": sum(1 for l in sl if l.meta.get("standing_start") == "1"),
            "best": _best_time(sl),
            "typical": _median([l.time_s for l in typical]),
            "typical_laps": ["L{0}".format(l.number) for l in typical],
            "theoretical": _r(la.theoretical_best([l.grid for l in cmp_laps], [per_lap[l.path.name] for l in cmp_laps], corners), 3),
            "population": pop,
            "corners": sc,
            "habits": dict(_session_habits(rows), brake_sd=_r(_median([c["brake_sd"] for c in sc]), 1)),
            "habit_na": {c["key"]: la.habit_na_reason(c["key"], la.dominant_device(sl)) for c in la.HABIT_CHECKS},
            "loss_map": _loss_map(cmp_laps, rg),
            "quality": _quality_summary(rows),
        })

    all_cmp, _pop = _population(laps)
    all_typical, typical_definition = _typical_population(laps, rows_by_file)
    step = 2
    path = [[_r(fg["x"][i], 1), _r(fg["z"][i], 1)] for i in range(0, len(fg["d"]), step)] if "x" in fg else []
    out = {
        "fingerprint": combo["fingerprint"],
        "id": combo["id"],
        "ref": ref.path.name,
        "ref_time": ref.time_s,
        "reference_role": la.reference_role(ref),
        **_status_counts(laps),
        "ref_device": ref.device,
        "devices": _device_counts([l.meta for l in laps]),
        "corner_set": _corner_set(combo["id"], frame, corners, method),
        "length_m": fg["d"][-1],
        "closed": frame.closed,
        "best": _best_time(laps),
        "typical": _median([l.time_s for l in all_typical]),
        "typical_definition": typical_definition,
        "theoretical": _r(la.theoretical_best([l.grid for l in all_cmp], [per_lap[l.path.name] for l in all_cmp], corners), 3),
        "corners": [{"name": c["name"], "apex_d": fg["d"][c["apex"]], "a_d": fg["d"][c["a"]], "b_d": fg["d"][c["b"]],
                     "x": _r(fg["x"][c["apex"]], 1) if "x" in fg else None,
                     "z": _r(fg["z"][c["apex"]], 1) if "x" in fg else None,
                     "ref": _clean_metrics(ref_m[k])} for k, c in enumerate(corners)],
        "path": path, "path_step_m": la.STEP_M * step,
        "outline": path[::max(1, len(path) // 160)],
        "laps": lap_rows,
        "sessions": sessions,
        "slip_norm": slip_norm,
        "provenance": provenance(combo["game"]),
        "quality": _quality_summary(lap_rows),
    }
    _write_json(cache, out)
    return out


def _session_corners(laps, per_lap, corners, ref_m):
    """lap_analysis.corner_stats over the comparison laps, in the app's units and with lap ids."""
    out = []
    for k in range(len(corners)):
        st = la.corner_stats([per_lap[l.path.name][k] for l in laps])
        fast = laps[st["fastest_index"]]
        out.append({
            "loss": _r(st["gap"], 3),
            "loss_vs_ref": _r(st["median_seg"] - ref_m[k]["seg_time"], 3),
            "median_s": _r(st["median_seg"], 3), "n": st["n"],
            "fastest_s": _r(st["fastest_seg"], 3), "fastest_file": fast.path.name, "fastest_lap": "L{0}".format(fast.number),
            "fastest_min_mph": _r(per_lap[fast.path.name][k]["min_speed"] * MPH, 1),
            "min_mph": _r(st["min_speed"] * MPH, 1) if st["min_speed"] is not None else None,
            "min_mph_sd": _r(st["min_speed_sd"] * MPH, 1) if st["min_speed_sd"] is not None else None,
            "brake_sd": _r(st["brake_sd"], 1), "brake_at": _r(st["brake_at"], 1), "braking_laps": st["braking_laps"],
            "brake_peak": _r(st["brake_peak"], 2),
            "steer_max": _r(st["steer_max"], 0), "steer_max_pct": _r(st["steer_max_pct"], 0), "beta_peak": _r(st["beta_peak"], 1),
            "coast_m": _r(st["coast_m"], 0), "clutch_down_m": _r(st["clutch_down_m"], 0),
            "gear": st["gear"],
        })
    return out


def _session_habits(rows):
    def med(group, key):
        return _r(_median([r[group].get(key) for r in rows]), 3)
    n = len(rows)
    # None = not measurable on the lap's device (keyboard keys have no pressure): left out, never 0
    peaks = [m["brake_peak"] for r in rows for m in r["corners"] if m.get("brake_at") is not None and m.get("brake_peak") is not None]
    return {
        "two_feet_s": med("habits", "two_feet_s"),
        "clutch_coast_s": med("habits", "clutch_coast_s"),
        "neutral_coast_s": med("habits", "neutral_coast_s"),
        "handbrake_pulls": _r(sum(r["habits"]["handbrake_pulls"] for r in rows) / n, 2)
        if n and all(r["habits"].get("handbrake_pulls") is not None for r in rows) else None,
        "coast_pct": med("totals", "coast_pct"),
        "full_pct": med("totals", "full_pct"),
        "shifts": med("totals", "shifts"),
        "slide_m": med("totals", "slide_m"),
        "brake_peak": _r(statistics.median(peaks), 3) if peaks else None,
        "incidents": _r(sum(len(r["incidents"]) for r in rows) / n, 2) if n else None,
        "spins": sum(1 for r in rows for i in r["incidents"] if i["kind"] == "spin"),
    }


def _loss_map(laps, rg):
    """Median over laps of time lost per metre vs the reference, per 20 m (ms/m, + = losing)."""
    w = int(20 / la.STEP_M)
    n = len(rg["d"])
    out = []
    for i in range(0, n - 1, w):
        j = min(i + w, n - 1)
        rates = []
        for l in laps:
            g = l.grid
            if len(g["t"]) <= j:
                continue
            dd = (rg["d"][j] - rg["d"][i]) or 1
            rates.append(((g["t"][j] - rg["t"][j]) - (g["t"][i] - rg["t"][i])) / dd * 1000)
        out.append(_r(statistics.median(rates), 2) if rates else 0)
    return out


# ----------------------------------------------------------------- lap detail (for replay + traces)

TRACE_KEYS = ("d", "t", "speed_mph", "gas", "brake", "clutch", "steer", "steer_pct", "gear", "rpm", "x", "z", "beta",
              "g_lat", "g_lon", "handbrake")


def lap_detail(combo, best_file, lap_file, ref_file=None, frame_file=None):
    byfile, frame, corners = _load_laps(combo, frame_file or best_file, [lap_file, ref_file or best_file])
    if lap_file not in byfile:
        raise KeyError(lap_file)
    lap = byfile[lap_file]
    ref = byfile.get(ref_file or best_file) or frame
    slip_norm = ref.meta.get("slip_units") == "normalized"
    cut_text = la.hook(lap.game, "CUT_TEXT")
    rg, lg = ref.grid, lap.grid
    rows = []
    for c in corners:
        lm, rm = la.corner_metrics(lg, c, lap.device), la.corner_metrics(rg, c, ref.device)
        items = la.advice_items(lm, rm, cut_text, slip_norm, lap.device, ref.device)
        rows.append({"name": c["name"], "a_d": frame.grid["d"][c["a"]], "b_d": frame.grid["d"][c["b"]],
                     "apex_d": frame.grid["d"][c["apex"]],
                     "dt": _r(lm["seg_time"] - rm["seg_time"], 3),
                     "lap": _clean_metrics(lm), "ref": _clean_metrics(rm),
                     "tips": {f: la.apply_focus(items, f) for f in la.FOCUS_MODES}})

    def pack(g):
        return {k: [_r(v, 3) for v in g[k]] for k in TRACE_KEYS if k in g}

    return {
        "lap": {"file": lap.path.name, "label": lap.label, "time": lap.time_s, "valid": lap.valid,
                "recorded_valid": lap.recorded_valid, "validity": lap.validity, "pace_eligible": lap.pace_eligible,
                "session": lap.session, "number": lap.number, "grid": pack(lg),
                "habits": {k: _r(v, 3) for k, v in la.road_habits(lap).items()},
                "device": lap.device, "device_source": lap.meta.get("input_device_source", "none"),
                "device_detail": lap.meta.get("input_detail", "")},
        "ref": {"file": ref.path.name, "label": ref.label, "time": ref.time_s, "grid": pack(rg), "device": ref.device,
                "valid": ref.valid, "recorded_valid": ref.recorded_valid, "validity": ref.validity, "pace_eligible": ref.pace_eligible},
        "corners": rows,
        # definitions for this lap's input device (on a pad, pedal overlap is two triggers, not left-foot braking)
        "habit_checks": [{"key": c["key"], "unit": c["unit"], "na": la.habit_na_reason(c["key"], lap.device),
                          "focus": {f: list(la.habit_check_def(c, f, lap.device)) for f in la.FOCUS_MODES}} for c in la.HABIT_CHECKS],
    }


# ----------------------------------------------------------------- background analysis of everything

class Analyser:
    def __init__(self):
        self.lock = threading.Lock()
        self.combos = []
        self.results = {}
        self.status = {"state": "idle", "done": 0, "total": 0, "current": "", "errors": {}}
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def refresh(self):
        self._wake.set()

    def rescan(self):
        """Pick up new fingerprints now (after a re-map), without waiting for the background loop."""
        combos = scan()
        with self.lock:
            self.combos = combos

    def _loop(self):
        while True:
            try:
                combos = scan()
                with self.lock:
                    self.combos = combos
                todo = [c for c in combos if self.results.get(c["id"], {}).get("fingerprint") != c["fingerprint"]]
                self.status.update(state="working" if todo else "ready", done=0, total=len(todo))
                for c in sorted(todo, key=lambda c: -c["last"]):
                    self.status["current"] = c["folder"]
                    try:
                        r = analyse_combo(c)
                        with self.lock:
                            self.results[c["id"]] = r
                        self.status["errors"].pop(c["id"], None)
                    except Exception as e:      # one bad folder must not stop the rest
                        self.status["errors"][c["id"]] = "{0}: {1}".format(type(e).__name__, e)
                    self.status["done"] += 1
                self.status.update(state="ready", current="")
            except Exception as e:
                self.status.update(state="error", current=str(e))
            self._wake.wait(20)        # re-scan for new laps every 20 s (headers only, cheap)
            self._wake.clear()

    def delete(self, cids, kind, dry_run=True):
        with self.lock:
            combos = [c for c in self.combos if c["id"] in cids]
            remaining = [c for c in self.combos if c["id"] not in cids]
        if len(combos) != len(set(cids)):
            raise KeyError(", ".join(sorted(set(cids) - {c["id"] for c in combos})))
        out = delete_combos(combos, kind, remaining, dry_run)
        if not dry_run:
            with self.lock:
                for c in combos:
                    self.results.pop(c["id"], None)
            self.rescan()
            self.refresh()
        return out

    def combo(self, cid):
        with self.lock:
            c = next((c for c in self.combos if c["id"] == cid), None)
        if c is None:
            raise KeyError(cid)
        return c

    def result(self, cid):
        c = self.combo(cid)
        r = self.results.get(cid)
        if r is None or r.get("fingerprint") != c["fingerprint"]:
            r = analyse_combo(c)
            with self.lock:
                self.results[cid] = r
        return c, r
