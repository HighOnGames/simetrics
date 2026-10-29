"""Forza Horizon 6 module for Simetrics: the hooks the core calls for FH6 laps.

Interface: modules/README.md. Stdlib only. FH6's "Data Out" stream has no track-limit flag, so the
only validity evidence is a rewind/reset seen by the logger; track limits stay unverified.
"""

import os
import re
from pathlib import Path

CLASSES = ["D", "C", "B", "A", "S1", "S2", "R", "X"]       # car_class 0..7 (6 not seen in real data yet)
KINDS = {"circuit": "Circuit", "sprint": "Sprint", "freeroam_loop": "Free roam", "freeroam_sprint": "Free roam"}
DRIVETRAINS = {0: "FWD", 1: "RWD", 2: "AWD"}

VALIDITY_SCOPE = "rewinds/resets only"
PACE_SCOPE = "rewind-free laps (track limits unverified)"
REFERENCE_ROLE = "fastest rewind-free lap; track limits unverified"
CUT_TEXT = "rewind used here"


def classify_validity(meta, obs):
    """(status, label, pace_eligible) once the game's own invalid flag is known to be clear."""
    if int(meta.get("rewinds", 0) or 0) > 0:
        return "invalid", "Rewind / reset recorded", False
    if obs["recorded"] is True:
        return "rewind_free", "No rewind recorded; track limits unverified", True
    return "unverified", "Validity unverified", None


def legacy_data_dir():
    """Where laps were saved before Simetrics (2026-10-28)."""
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    candidates = [Path(os.environ[v]) / "Documents" for v in ("OneDrive", "OneDriveConsumer") if os.environ.get(v)]
    candidates += [home / "OneDrive" / "Documents", home / "Documents"]
    return next((c / "Forza LapLogger" / "laplogger" for c in candidates if (c / "Forza LapLogger").is_dir()), None)


def display_info(game, car, track, layout, lapmeta, lib, settings):
    """Names come from the logger's cars.json / routes.json or the user's library edits; class, PI and
    drivetrain from the lap header. Pictures are the user's own uploads."""
    ordinal = lapmeta.get("car_ordinal", "")
    car_key = "{0}:{1}".format(game, ordinal or car)
    track_key = "{0}:{1}".format(game, track)
    cl = lib["cars"].get(car_key, {})
    tl = lib["tracks"].get(track_key, {})
    try:
        cls = CLASSES[int(lapmeta.get("car_class", -1))]
    except (ValueError, IndexError):
        cls = "?"
    pi = lapmeta.get("car_pi", "")
    try:
        drive = DRIVETRAINS.get(int(lapmeta.get("drivetrain", -1)), "")
    except ValueError:
        drive = ""
    default_name = car if not re.fullmatch(r"car\d+", car) else "Car #" + car[3:]
    return {
        "car_key": car_key, "track_key": track_key,
        "car_name": cl.get("name") or default_name, "brand": "",
        "track_name": tl.get("name") or track.replace("-", " ").title(),
        "event": KINDS.get(lapmeta.get("kind", ""), "Circuit"),
        "discipline": tl.get("discipline", ""),
        "car_class": cls, "class_sort": "{0}".format(CLASSES.index(cls) if cls in CLASSES else 9),
        "pi": pi, "drivetrain": drive,
        "car_img": cl.get("image"), "track_img": tl.get("image"),
        "skins": [], "skin": None, "map": None,
        "car_detail": " · ".join(x for x in ("{0} {1}".format(cls, pi).strip(), drive) if x),
    }


def brief_identity(combo):
    """Extra identity fields for AI briefs."""
    return {"car_class": combo.get("car_class"), "car_pi": combo.get("pi"), "drivetrain": combo.get("drivetrain") or None}


def brief_identity_lines(identity):
    """Extra lines for the rendered prompt, after the Game/Car/Route line."""
    if not identity.get("car_class"):
        return []
    return ["FH6 car class {0} {1}, {2}.".format(identity["car_class"], identity.get("car_pi") or "",
                                                 identity.get("drivetrain") or "drivetrain unknown")]
