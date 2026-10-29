"""Assetto Corsa module for Simetrics: the hooks the core calls for AC laps.

Interface: modules/README.md. Everything here is optional for the core; a missing hook gets the
core default. Stdlib only. Car/track pictures and the AI line are read straight from the AC install
(ac_install in data/studio/settings.json), never copied.
"""

import json
import os
import re
import struct
from pathlib import Path

ID = "ac"
STUDIO_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INSTALL = r"C:\Program Files (x86)\Steam\steamapps\common\assettocorsa"

# validity: AC's own invalid flag plus LapLogger's independent tyres-outside observation are the core
# default, so classify_validity, VALIDITY_SCOPE, PACE_SCOPE, REFERENCE_ROLE and CUT_TEXT are not overridden.
REFERENCE_OPTIONS = {"tc": "AC's saved best lap"}


# ----------------------------------------------------------------- locations

def _settings_file():
    try:
        with open(STUDIO_DIR / "data" / "studio" / "settings.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def install_dir(settings=None):
    """AC install: ac_install in Simetrics settings, else the Steam default."""
    p = Path((settings if settings is not None else _settings_file()).get("ac_install", "") or ".")
    if (p / "content" / "tracks").is_dir():
        return p
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "content" / "tracks").is_dir():
            return p
    return Path(DEFAULT_INSTALL)


def docs_dir():
    """Documents/Assetto Corsa (AC's own saves: ctelemetry, cfg), or None."""
    candidates = []
    for var in ("OneDrive", "OneDriveConsumer"):
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]) / "Documents" / "Assetto Corsa")
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    candidates += [home / "OneDrive" / "Documents" / "Assetto Corsa", home / "Documents" / "Assetto Corsa"]
    return next((c for c in candidates if c.is_dir()), None)


def legacy_data_dir():
    """Where laps were saved before Simetrics (2026-10-28)."""
    d = docs_dir()
    return d / "laplogger" if d else None


# ----------------------------------------------------------------- AC's saved best lap (--ref tc)

def read_fast_lane(track, layout):
    """World x/z points and total length of the track's AI line, or None."""
    base = install_dir() / "content" / "tracks" / track
    path = (base / layout / "ai" / "fast_lane.ai") if layout else (base / "ai" / "fast_lane.ai")
    if not path.is_file():
        return None
    b = path.read_bytes()
    if len(b) < 36:
        return None
    _version, count, _lap_time, _ = struct.unpack_from("<4i", b, 0)
    pts = [struct.unpack_from("<4f", b, 16 + 20 * i) for i in range(count)]
    return {"x": [p[0] for p in pts], "z": [p[2] for p in pts], "length": pts[-1][3]}


def load_tc(core, path):
    """AC's own best-lap trace (ctelemetry/player/*.tc): pos, speed, gas, brake, gear only."""
    b = Path(path).read_bytes()
    o = 4
    strings = []
    for _ in range(4):
        n = struct.unpack_from("<i", b, o)[0]
        strings.append(b[o + 4:o + 4 + n].decode("latin1"))
        o += 4 + n
    driver, track, car, layout = strings
    lap_ms, count = struct.unpack_from("<2i", b, o)
    o += 8
    samples = [struct.unpack_from("<i4f", b, o + 20 * i) for i in range(count)]
    fl = read_fast_lane(track, layout)
    length = fl["length"] if fl else 0.0
    cols = {"pos": [s[1] for s in samples], "speed": [s[2] for s in samples],
            "gas": [s[3] for s in samples], "brake": [s[4] for s in samples],
            "gear": [s[0] - 1 for s in samples]}
    # no timestamps in .tc: integrate distance / speed, then scale to the lap time
    t = [0.0]
    for i in range(1, count):
        dd = max(cols["pos"][i] - cols["pos"][i - 1], 0.0) * length
        v = max((cols["speed"][i] + cols["speed"][i - 1]) / 2 / 3.6, 1.0)
        t.append(t[-1] + dd / v)
    k = (lap_ms / 1000.0) / t[-1] if t[-1] else 1.0
    cols["t_ms"] = [x * k * 1000 for x in t]
    meta = {"car": car, "track": track, "layout": layout, "driver": driver,
            "lap_time_ms": str(lap_ms), "valid": "1", "session": "AC-best",
            "lap_number": "0", "track_length_m": str(length)}
    return core.Lap(path, meta, cols)


def reference_lap(core, name, laps):
    """--ref tc: AC's saved best lap for this track/layout, or None."""
    if name != "tc" or not laps:
        return None
    m = laps[0].meta
    stem = "_{0}_{1}_.tc".format(m["track"], m.get("layout", ""))
    d = docs_dir()
    tcs = [p for p in (d / "ctelemetry" / "player").glob("*.tc") if p.name.endswith(stem)] if d else []
    return load_tc(core, tcs[0]) if tcs else None


# ----------------------------------------------------------------- display info (names, classes, pictures)

def _loose_json(path):
    """AC ui_*.json files often contain raw control characters / BOMs."""
    try:
        t = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        return json.loads(re.sub(r"[\x00-\x1f]", " ", t), strict=False)
    except (OSError, ValueError):
        return {}


def _content(settings):
    return Path(settings.get("ac_install") or DEFAULT_INSTALL) / "content"


def track_info(content, track, layout):
    base = content / "tracks" / track
    ui = base / "ui" / layout if layout else base / "ui"
    d = _loose_json(ui / "ui_track.json")
    tags = [t.lower() for t in d.get("tags", [])]
    if "drift" in tags or "#drift" in tags:
        event = "Drift"
    elif "touge" in tags or "hillclimb" in tags:
        event = "Touge / hillclimb"
    elif "drag" in tags:
        event = "Drag"
    elif "highway" in tags:
        event = "Highway"
    elif "a2b" in tags:
        event = "Point to point"
    else:
        event = "Circuit"
    mp = (base / layout if layout else base)
    ini = {}
    ini_path = mp / "data" / "map.ini"
    if ini_path.is_file():
        for line in ini_path.read_text(errors="replace").splitlines():
            k, _, v = line.partition("=")
            try:
                ini[k.strip().upper()] = float(v)
            except ValueError:
                pass
    return {
        "name": d.get("name") or track.replace("_", " ").title(),
        "event": event,
        "country": d.get("country", ""),
        "preview": (ui / "preview.png").is_file(),
        "outline": (ui / "outline.png").is_file(),
        "map": (mp / "map.png").is_file() and bool(ini),
        "map_ini": ini,
        "length": d.get("length", ""),
    }


def car_info(content, car):
    base = content / "cars" / car
    d = _loose_json(base / "ui" / "ui_car.json")
    tags = d.get("tags", [])
    low = [t.lower() for t in tags]
    groups = [t[1:].strip() for t in tags if t.startswith("#")]
    group = groups[0] if groups else ("Race car" if d.get("class", "").lower() == "race" else "Road car")
    if group.isupper() or len(group) <= 4:
        label = group
    else:
        label = group[:1].upper() + group[1:]
    drive = next((x.upper() for x in ("fwd", "rwd", "awd") if x in low), "")
    skins = []
    sk = base / "skins"
    if sk.is_dir():
        skins = sorted((p for p in sk.iterdir() if (p / "preview.jpg").is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    specs = d.get("specs", {})
    return {
        "name": d.get("name") or car,
        "brand": d.get("brand", ""),
        "class": label,
        "drivetrain": drive,
        "skins": [p.name for p in skins],
        "badge": (base / "ui" / "badge.png").is_file(),
        "specs": {k: specs.get(k, "") for k in ("bhp", "weight", "pwratio")},
        "year": d.get("year", ""),
    }


_info_cache = {}


def display_info(game, car, track, layout, lapmeta, lib, settings):
    """Display info for a car@track folder, merged with the user's library edits (lib)."""
    key = (car, track, layout)
    if key not in _info_cache:
        content = _content(settings)
        _info_cache[key] = (car_info(content, car), track_info(content, track, layout))
    ci, ti = _info_cache[key]
    car_key, track_key = "{0}:{1}".format(game, car), "{0}:{1}:{2}".format(game, track, layout)
    cl = lib["cars"].get(car_key, {})
    tl = lib["tracks"].get(track_key, {})
    skin = cl.get("skin") if cl.get("skin") in ci["skins"] else (ci["skins"][0] if ci["skins"] else None)
    car_img = cl.get("image") or ("/img/{0}/car/{1}/{2}".format(game, car, skin) if skin else None)
    track_img = tl.get("image") or ("/img/{0}/track/{1}/{2}".format(game, track, layout or "-") if ti["preview"] else None)
    return {
        "car_key": car_key, "track_key": track_key,
        "car_name": cl.get("name") or ci["name"], "brand": ci["brand"],
        "track_name": tl.get("name") or ti["name"] + (" ({0})".format(layout) if layout else ""),
        "event": ti["event"], "discipline": "",
        "car_class": ci["class"], "class_sort": ci["class"],
        "drivetrain": ci["drivetrain"],
        "car_img": car_img, "track_img": track_img,
        "skins": ci["skins"], "skin": skin,
        "map": ({"url": "/img/{0}/map/{1}/{2}".format(game, track, layout or "-"), "ini": ti["map_ini"]} if ti["map"] else None),
        "car_detail": " · ".join(x for x in (ci["specs"].get("bhp"), ci["specs"].get("weight"), str(ci["year"] or "")) if x),
    }


def image_file(parts, settings):
    """/img/ac/<parts>: track|outline|map/<track>/<layout or ->, car/<car>/<skin>, badge/<car>.
    The server has already rejected unsafe path parts."""
    content = _content(settings)
    what = parts[0] if parts else ""
    if what in ("track", "outline", "map") and len(parts) >= 2:
        track, layout = parts[1], parts[2] if len(parts) > 2 else "-"
        base = content / "tracks" / track
        if what == "map":
            return (base / layout if layout != "-" else base) / "map.png"
        ui = base / "ui" / layout if layout != "-" else base / "ui"
        return ui / ("preview.png" if what == "track" else "outline.png")
    if what == "car" and len(parts) == 3:
        return content / "cars" / parts[1] / "skins" / parts[2] / "preview.jpg"
    if what == "badge" and len(parts) == 2:
        return content / "cars" / parts[1] / "ui" / "badge.png"
    return None
