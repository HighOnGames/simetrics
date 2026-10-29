##############################################################
# LapLogger - records every lap you drive to a CSV file so it
# can be analysed afterwards (Simetrics: analysis/lap_analysis.py).
#
# Runs on AC's embedded Python 3.3: no f-strings, stdlib only.
# Output: <data_dir>/<car>@<track>/. data_dir comes from laplogger.ini next
# to this file, written by Simetrics' install_ac_app.bat (the
# Simetrics' data/ac folder). Without it: Documents/Assetto Corsa/laplogger/
# This folder lives in Simetrics/modules/ac/logger/LapLogger; AC's
# apps/python/LapLogger is a junction to it.
#############################################################

import ac
import acsys
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quality_monitor import QualityMonitor, LapQuality

try:
    import threading
except ImportError:
    threading = None

VERSION = "1.5"
SAMPLE_HZ = 50.0

# shared lap CSV schema: see Simetrics/analysis/LAP_CSV_SCHEMA.md
COLUMNS = ["t_ms", "pos", "speed", "speed_mph", "gas", "brake", "clutch", "steer", "gear",
           "rpm", "x", "y", "z", "g_lat", "g_lon",
           "sa_fl", "sa_fr", "sa_rl", "sa_rr",
           "sr_fl", "sr_fr", "sr_rl", "sr_rr", "invalid",
           "beta", "yaw_rate",
           "ns_fl", "ns_fr", "ns_rl", "ns_rr",
           "tyre_t_fl", "tyre_t_fr", "tyre_t_rl", "tyre_t_rr",
           "susp_fl", "susp_fr", "susp_rl", "susp_rr",
           "tyres_out", "penalty_s", "flag", "game_invalid", "track_limits_exceeded"]
INVALID_COL = COLUMNS.index("invalid")
PHYSICS_EXTRA_COUNT = COLUMNS.index("tyres_out") - COLUMNS.index("beta")

app_window = 0
status_label = 0
last_label = 0
count_label = 0
toggle_button = 0

recording = True
sample_acc = 0.0
out_dir = ""
session_stamp = ""
session_input = {}           # input_meta() at session start

rows = []
lap_count_prev = -1
lap_t_prev = 0
lap_observed_start = False   # True once we saw this lap begin at the line
lap_invalid = False
laps_saved = 0
update_errors = 0
quality_monitor = None
lap_quality = LapQuality()


def ac_docs_dir():
    candidates = []
    for var in ("OneDrive", "OneDriveConsumer"):
        base = os.environ.get(var)
        if base:
            candidates.append(os.path.join(base, "Documents", "Assetto Corsa"))
    profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    candidates.append(os.path.join(profile, "OneDrive", "Documents", "Assetto Corsa"))
    candidates.append(os.path.join(profile, "Documents", "Assetto Corsa"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def read_ini(path):
    """{SECTION: {KEY: value}} from an AC .ini file (upper-cased names); {} if unreadable."""
    out, sec = {}, None
    try:
        with open(path) as f:
            for line in f:
                line = line.split(";")[0].strip()
                if line.startswith("[") and line.endswith("]"):
                    sec = out.setdefault(line[1:-1].strip().upper(), {})
                elif sec is not None and "=" in line:
                    k, _, v = line.partition("=")
                    sec[k.strip().upper()] = v.strip()
    except (IOError, OSError):
        pass
    return out


# controls.ini [HEADER] INPUT_METHOD -> shared schema input_device
INPUT_METHODS = {"WHEEL": "wheel", "X360": "gamepad", "XBOX": "gamepad", "DS4": "gamepad",
                 "DUALSHOCK": "gamepad", "GAMEPAD": "gamepad", "KEYBOARD": "keyboard", "MOUSE": "keyboard"}


def input_meta():
    """Which device AC is set to read (cfg/controls.ini, read once at session start: AC itself only
    reloads it when a session starts), plus the auto-clutch assist (cfg/assists.ini)."""
    cfg = os.path.join(ac_docs_dir(), "cfg")
    ctl = read_ini(os.path.join(cfg, "controls.ini"))
    method = ctl.get("HEADER", {}).get("INPUT_METHOD", "").strip().upper()
    meta = {"input_device": INPUT_METHODS.get(method, "unknown"),
            "input_device_source": "game_config" if method else "none",
            "input_method_raw": method}
    if meta["input_device"] == "unknown" and method:
        # a value this logger doesn't know yet: guess from the name, keep the raw value
        meta["input_device"] = "gamepad" if ("PAD" in method or "360" in method) else "unknown"
    detail = ""
    if meta["input_device"] == "wheel":
        joy = ctl.get("STEER", {}).get("JOY", "")
        detail = ctl.get("CONTROLLERS", {}).get("CON" + joy, "") if joy not in ("", "-1") else ""
        clutch = ctl.get("CLUTCH", {})
        meta["clutch_axis"] = 1 if clutch.get("JOY", "-1") not in ("", "-1") and clutch.get("AXLE", "-1") not in ("", "-1") else 0
    elif meta["input_device"] == "keyboard":
        detail = "mouse steering" if ctl.get("KEYBOARD", {}).get("MOUSE_STEER", "0") == "1" else "keyboard steering"
    elif meta["input_device"] == "gamepad":
        detail = "XInput"
    meta["input_detail"] = safe_meta(detail)
    auto = read_ini(os.path.join(cfg, "assists.ini")).get("ASSISTS", {}).get("AUTO_CLUTCH")
    if auto in ("0", "1"):
        meta["auto_clutch"] = auto
    return meta


def safe_meta(s):
    return "".join(ch for ch in s if ch not in "\r\n=,").strip()


def data_dir():
    """data_dir= from laplogger.ini next to this file, else Documents/Assetto Corsa/laplogger."""
    ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "laplogger.ini")
    try:
        with open(ini) as f:
            for line in f:
                k, _, v = line.partition("=")
                if k.strip().lower() == "data_dir" and v.strip():
                    return v.strip()
    except (IOError, OSError):
        pass
    return os.path.join(ac_docs_dir(), "laplogger")


def safe_name(s):
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in s)


def fmt_time(ms):
    ms = int(ms)
    return "{0}:{1:02d}.{2:03d}".format(ms // 60000, (ms // 1000) % 60, ms % 1000)


def log(msg):
    ac.log("LapLogger: " + msg)
    ac.console("LapLogger: " + msg)


def combo_dir():
    track = ac.getTrackName(0)
    layout = ac.getTrackConfiguration(0)
    if layout:
        track = track + "-" + layout
    d = os.path.join(out_dir, safe_name(ac.getCarName(0)) + "@" + safe_name(track))
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def write_lap(path, meta, lap_rows):
    tmp = path + ".part"
    with open(tmp, "w") as f:
        for k in sorted(meta):
            f.write("# {0}={1}\n".format(k, meta[k]))
        f.write(",".join(COLUMNS) + "\n")
        for r in lap_rows:
            f.write(",".join(r) + "\n")
    os.rename(tmp, path)


def save_lap(lap_rows, invalid, lap_number, quality_meta=None):
    global laps_saved
    last_t = int(lap_rows[-1][0])
    # LastLap can lag the lap counter by a frame; fall back to the last sample
    lap_ms = ac.getCarState(0, acsys.CS.LastLap)
    if lap_ms <= 0 or abs(lap_ms - last_t) > 2000:
        lap_ms = last_t
    try:
        splits = ac.getLastSplits(0)
        splits = ";".join(str(int(s)) for s in splits)
    except Exception:
        splits = ""
    meta = {
        "logger_version": VERSION,
        "game": "ac",
        "driver": ac.getDriverName(0),
        "car": ac.getCarName(0),
        "track": ac.getTrackName(0),
        "layout": ac.getTrackConfiguration(0),
        "track_length_m": "{0:.1f}".format(ac.getTrackLength(0)),
        "session": session_stamp,
        "lap_number": lap_number,
        "lap_time_ms": int(lap_ms),
        "valid": 0 if invalid else 1,
        "splits_ms": splits,
        "sample_hz": int(SAMPLE_HZ),
    }
    meta.update(session_input)
    if quality_meta:
        meta.update(quality_meta)
    name = "{0}_L{1:02d}_{2}{3}.csv".format(
        session_stamp, lap_number, fmt_time(lap_ms).replace(":", "-").replace(".", "-"),
        "_INVALID" if invalid else "")
    path = os.path.join(combo_dir(), name)

    def job():
        try:
            write_lap(path, meta, lap_rows)
        except Exception as e:
            log("write failed: " + repr(e))

    if threading is not None:
        threading.Thread(target=job).start()
    else:
        job()
    laps_saved += 1
    status = "GAME INVALID" if invalid else "not verified"
    if quality_meta and not invalid:
        if int(quality_meta.get("off_track_events", 0)):
            status = "OFF TRACK"
        elif quality_meta.get("penalty_seen"):
            status = "PENALTY"
        elif not invalid and quality_meta.get("track_limits_coverage") == "complete":
            status = "no issue observed"
    ac.setText(last_label, "Last: {0}  {1}".format(fmt_time(lap_ms), status))
    ac.setText(count_label, "Laps saved: {0}".format(laps_saved))


def per_wheel(key):
    try:
        v = ac.getCarState(0, key)
        return list(v)[:4] if hasattr(v, "__len__") and len(v) >= 4 else [float(v)] * 4
    except Exception:
        return [0.0] * 4


def optional_vec(name):
    """Extra channels: None if this AC build lacks them, so a missing one can never stop a lap
    being recorded (it is written as nan)."""
    key = getattr(acsys.CS, name, None)
    if key is None:
        return None
    try:
        return list(ac.getCarState(0, key))
    except Exception:
        return None


def extra_values():
    vals = []
    v = optional_vec("LocalVelocity")      # car frame: x = left, z = forward (same frame as AccG)
    if v is not None and math.hypot(v[0], v[2]) > 3:
        # body slip angle, + = sliding to the right
        vals.append("{0:.1f}".format(math.degrees(math.atan2(-v[0], v[2]))))
    else:
        vals.append("0.0" if v is not None else "nan")
    w = optional_vec("LocalAngularVelocity")
    # rad/s about +y (up, right-handed): turning right is negative; schema: deg/s, + = right
    vals.append("{0:.1f}".format(-math.degrees(w[1])) if w is not None else "nan")
    for name, fmt in (("NdSlip", "{0:.3f}"), ("CurrentTyresCoreTemp", "{0:.1f}"), ("SuspensionTravel", "{0:.4f}")):
        v = optional_vec(name)
        vals += [fmt.format(x) for x in v[:4]] if v is not None and len(v) >= 4 else ["nan"] * 4
    return vals


def read_sample(lap_t, observation=None, game_invalid=None):
    cs = acsys.CS
    x, y, z = ac.getCarState(0, cs.WorldPosition)
    gx, gy, gz = ac.getCarState(0, cs.AccG)
    sa = per_wheel(cs.SlipAngle)
    sr = per_wheel(cs.SlipRatio)
    vals = [
        str(int(lap_t)),
        "{0:.6f}".format(ac.getCarState(0, cs.NormalizedSplinePosition)),
        "{0:.2f}".format(ac.getCarState(0, cs.SpeedKMH)),
        "{0:.2f}".format(ac.getCarState(0, cs.SpeedMPH)),
        "{0:.3f}".format(ac.getCarState(0, cs.Gas)),
        "{0:.3f}".format(ac.getCarState(0, cs.Brake)),
        "{0:.3f}".format(ac.getCarState(0, cs.Clutch)),
        "{0:.1f}".format(ac.getCarState(0, cs.Steer)),
        str(int(ac.getCarState(0, cs.Gear)) - 1),   # -1 = R, 0 = N, 1 = 1st
        str(int(ac.getCarState(0, cs.RPM))),
        "{0:.2f}".format(x), "{0:.2f}".format(y), "{0:.2f}".format(z),
        "{0:.3f}".format(gx), "{0:.3f}".format(gz),
    ]
    vals += ["{0:.2f}".format(v) for v in sa]
    vals += ["{0:.3f}".format(v) for v in sr]
    # Preserve a one-update invalidation even if it occurs between CSV samples.
    vals.append("1" if lap_invalid else "0")
    try:
        vals += extra_values()
    except Exception:
        vals += ["nan"] * PHYSICS_EXTRA_COUNT
    observation = observation or {}
    for key in ("tyres_out", "penalty_s", "flag"):
        value = observation.get(key)
        vals.append("nan" if value is None else str(value))
    vals.append("nan" if game_invalid is None else str(int(bool(game_invalid))))
    vals.append("1" if lap_quality.track_limits_exceeded else
                "0" if observation.get("tyres_out") is not None else "nan")
    return vals


def on_toggle(*args):
    global recording, rows, lap_observed_start, lap_invalid
    recording = not recording
    rows = []
    lap_observed_start = False   # the lap in progress is now incomplete
    lap_invalid = False
    lap_quality.reset()
    ac.setText(toggle_button, "Pause" if recording else "Resume")
    ac.setText(status_label, "Recording" if recording else "Paused")


def acMain(ac_version):
    global app_window, status_label, last_label, count_label, toggle_button
    global out_dir, session_stamp, session_input, quality_monitor
    quality_monitor = QualityMonitor()
    out_dir = data_dir()
    session_stamp = time.strftime("%y%m%d-%H%M%S")
    try:
        session_input = input_meta()
    except Exception as e:
        session_input = {"input_device": "unknown", "input_device_source": "none"}
        log("could not read controls.ini: " + repr(e))
    race = read_ini(os.path.join(ac_docs_dir(), "cfg", "race.ini"))
    # Preserve the game's configured rule separately from our observed >2-tyre rule.
    for section, key, meta_key in (("LAP_INVALIDATOR", "ALLOWED_TYRES_OUT", "game_allowed_tyres_out"),
                                   ("RACE", "PENALTIES", "game_penalties")):
        value = race.get(section, {}).get(key)
        if value is not None:
            session_input[meta_key] = safe_meta(value)

    app_window = ac.newApp("LapLogger")
    ac.setSize(app_window, 220, 120)
    status_label = ac.addLabel(app_window, "Waiting for the start line")
    ac.setPosition(status_label, 10, 30)
    last_label = ac.addLabel(app_window, "Last: -")
    ac.setPosition(last_label, 10, 52)
    count_label = ac.addLabel(app_window, "Laps saved: 0")
    ac.setPosition(count_label, 10, 74)
    toggle_button = ac.addButton(app_window, "Pause")
    ac.setPosition(toggle_button, 140, 90)
    ac.setSize(toggle_button, 70, 22)
    ac.addOnClickedListener(toggle_button, on_toggle)
    log("v{0} writing to {1}, input device: {2}".format(VERSION, out_dir, session_input.get("input_device")))
    return "LapLogger"


def acUpdate(deltaT):
    global sample_acc, rows, lap_count_prev, lap_t_prev, lap_observed_start
    global lap_invalid, update_errors, session_stamp, quality_monitor
    if not recording:
        return
    pending_save = None
    quality_observed = False
    try:
        lap_count = ac.getCarState(0, acsys.CS.LapCount)
        lap_t = ac.getCarState(0, acsys.CS.LapTime)

        if lap_count_prev < 0 or lap_count < lap_count_prev:
            # first frame, or the session restarted
            if lap_count_prev >= 0:
                # new stamp so lap numbers don't repeat within one session
                session_stamp = time.strftime("%y%m%d-%H%M%S")
            rows = []
            lap_observed_start = False
            lap_invalid = False
            lap_quality.reset()
            sample_acc = 0.0
        elif lap_count > lap_count_prev:
            if lap_observed_start and lap_count == lap_count_prev + 1 and len(rows) > SAMPLE_HZ * 5:
                pending_save = (rows, lap_invalid, lap_count_prev, lap_quality.metadata())
            rows = []
            lap_observed_start = True
            lap_invalid = False
            lap_quality.reset()
            sample_acc = 0.0
            ac.setText(status_label, "Recording lap {0}".format(lap_count + 1))
        elif lap_t + 1000 < lap_t_prev:
            # timer jumped back without a lap: back to pits / reset
            rows = []
            lap_observed_start = False
            lap_invalid = False
            lap_quality.reset()
            sample_acc = 0.0
            ac.setText(status_label, "Reset - waiting for the line")

        lap_count_prev = lap_count
        lap_t_prev = lap_t
        if lap_observed_start:
            # Validity is monitored every app update, independently of CSV sampling.
            # An unavailable source stays unknown rather than silently becoming clear.
            game_invalid = None
            try:
                value = ac.getCarState(0, acsys.CS.LapInvalidated)
                if value in (0, 1, False, True):
                    game_invalid = bool(value)
            except Exception:
                pass
            if quality_monitor is None:
                quality_monitor = QualityMonitor()
            observation = quality_monitor.read(ac, lap_t, lap_count)
            quality_observed = True
            lap_quality.observe(observation, deltaT, game_invalid=game_invalid)
            if game_invalid:
                lap_invalid = True
            sample_acc += deltaT
            if sample_acc < 1.0 / SAMPLE_HZ:
                return
            sample_acc = 0.0
            sample = read_sample(lap_t, observation, game_invalid)
            rows.append(sample)
    except Exception as e:
        if lap_observed_start and not quality_observed:
            # A failed update is missing evidence, not a completely monitored frame.
            lap_quality.observe({}, deltaT, game_invalid=None)
        update_errors += 1
        if update_errors <= 3:
            log("update error: " + repr(e))
    finally:
        if pending_save is not None:
            try:
                # State has already advanced: a save/UI failure cannot carry the
                # completed lap's invalidation into the next lap or lose its start.
                save_lap(*pending_save)
            except Exception as e:
                log("could not save completed lap: " + repr(e))


def acShutdown():
    if quality_monitor is not None:
        quality_monitor.close()
