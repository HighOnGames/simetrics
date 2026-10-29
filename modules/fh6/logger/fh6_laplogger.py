"""FH6 LapLogger: records Forza Horizon 6 telemetry ("Data Out" UDP) into the same per-lap CSV
format as the Assetto Corsa LapLogger, so lap_analysis.py --game fh6 can coach from it.

In FH6: Settings > HUD and Gameplay > Data Out = On, IP 127.0.0.1, port 5300.
Run with the desktop Python (3.8+, stdlib only):

    py -3.12 fh6_laplogger.py                    # log races and hotkey runs
    py -3.12 fh6_laplogger.py probe              # live decoded values, to check the packet layout
    py -3.12 fh6_laplogger.py --car "Adam Rocks" # name the car (remembered in cars.json)

What becomes a lap / run (one CSV each, in Simetrics/data/fh6/<car>@<route>/):
  circuit race   every lap, split by the game's lap counter (closed=1; lap 1 is a standing start)
  sprint race    the whole race, start to finish (closed=0)
  free roam      F9  = put a start/finish line here; every pass through it ends a lap
                 F10 = put a finish line here: the route becomes point-to-point, the run since
                       the start line is saved, and the next run starts at the start line again
                 F11 = clear the lines and drop the unfinished run
A rewind marks the lap invalid; pausing during a free-roam run drops it. Leaving a race through the
pause menu drops the unfinished run.

Verified against real FH6 packets (2026-10-27): 324-byte packets, dash block at byte 244 (FH4/FH5
layout), ~46 Hz, all-zero packets while paused or in menus. race_time also counts in free roam, so a
race is detected from race_pos > 0. Gear 0 = R, 11 = N (also shown for ~0.1 s during every shift).
Slip angle / slip ratio are Forza's normalised values (1 = at the grip limit), kept as they are.
FH6's world is left-handed: z is negated so x/z match AC's frame (x right, z down on a map).

Input device: FH6 doesn't say whether you drive with a wheel, a controller or a keyboard. Set it with
--device wheel|gamepad|keyboard (or "fh6_input_device" in data/studio/settings.json); with neither, or
--device auto, every lap is classified from its own data (lap_analysis.classify_device). The lap records
input_device and input_device_source (setting / heuristic), plus what the data suggested.

Routes have no names in the telemetry: they get auto names (circuit-1, sprint-2, freeroam-3) in
data/fh6/routes.json, matched by where they start. Edit the names there (and rename the folder)
to give them real names. Columns and units: see Simetrics/analysis/LAP_CSV_SCHEMA.md.
"""

import argparse
import json
import math
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

VERSION = "1.3"
G = 9.80665
MPH_PER_MS = 2.2369363
HERE = Path(__file__).resolve().parent
STUDIO = HERE.parents[2]            # Simetrics (this file is in modules/fh6/logger/)

COLUMNS = ["t_ms", "pos", "speed", "speed_mph", "gas", "brake", "clutch", "steer", "gear",
           "rpm", "x", "y", "z", "g_lat", "g_lon",
           "sa_fl", "sa_fr", "sa_rl", "sa_rr",
           "sr_fl", "sr_fr", "sr_rl", "sr_rr", "invalid",
           # shared extras (same names and units as AC LapLogger 1.3)
           "beta", "yaw_rate",
           "ns_fl", "ns_fr", "ns_rl", "ns_rr",
           "tyre_t_fl", "tyre_t_fr", "tyre_t_rl", "tyre_t_rr",
           "susp_fl", "susp_fr", "susp_rl", "susp_rr",
           # Forza-only extras
           "handbrake", "steer_in", "line_offset", "ai_brake_diff"]
WHEELS = ("fl", "fr", "rl", "rr")

# ----------------------------------------------------------------- packet layout
# "Sled" block (232 bytes), identical in every Forza game since FM7.
SLED = [("is_race_on", "i"), ("timestamp_ms", "I"), ("rpm_max", "f"), ("rpm_idle", "f"), ("rpm", "f"),
        ("acc_x", "f"), ("acc_y", "f"), ("acc_z", "f"), ("vel_x", "f"), ("vel_y", "f"), ("vel_z", "f"),
        ("ang_x", "f"), ("ang_y", "f"), ("ang_z", "f"), ("yaw", "f"), ("pitch", "f"), ("roll", "f")]
for _grp, _t in [("susp_n", "f"), ("slip_ratio", "f"), ("wheel_rot", "f"), ("rumble", "i"), ("puddle", "f"),
                 ("surf_rumble", "f"), ("slip_angle", "f"), ("comb_slip", "f"), ("susp_m", "f")]:
    SLED += [("{0}_{1}".format(_grp, w), _t) for w in ("fl", "fr", "rl", "rr")]
SLED += [("car_ordinal", "i"), ("car_class", "i"), ("car_pi", "i"), ("drivetrain", "i"), ("cylinders", "i")]
# "Dash" block. FH4/FH5 put it at byte 244 (12 unknown bytes after the sled), FM7 at 232.
DASH = [("pos_x", "f"), ("pos_y", "f"), ("pos_z", "f"), ("speed", "f"), ("power", "f"), ("torque", "f"),
        ("tyre_t_fl", "f"), ("tyre_t_fr", "f"), ("tyre_t_rl", "f"), ("tyre_t_rr", "f"),
        ("boost", "f"), ("fuel", "f"), ("distance", "f"), ("best_lap", "f"), ("last_lap", "f"),
        ("cur_lap", "f"), ("race_time", "f"), ("lap_no", "H"), ("race_pos", "B"),
        ("accel", "B"), ("brake", "B"), ("clutch", "B"), ("handbrake", "B"), ("gear", "B"),
        ("steer", "b"), ("driving_line", "b"), ("ai_brake_diff", "b")]
SLED_FMT = "<" + "".join(t for _, t in SLED)
DASH_FMT = "<" + "".join(t for _, t in DASH)
SLED_NAMES = [n for n, _ in SLED]
DASH_NAMES = [n for n, _ in DASH]
DASH_SIZE = struct.calcsize(DASH_FMT)
KNOWN_DASH_OFFSET = {324: 244, 311: 232, 331: 232}


def decode(b, dash_off):
    d = dict(zip(SLED_NAMES, struct.unpack_from(SLED_FMT, b, 0)))
    d.update(zip(DASH_NAMES, struct.unpack_from(DASH_FMT, b, dash_off)))
    return d


class Layout:
    """Finds where the dash block starts: the dash 'speed' float must equal |velocity|.
    Verified offsets are remembered per packet size in layout.json."""

    def __init__(self, path):
        self.path = path
        try:
            self.known = {int(k): v for k, v in json.loads(path.read_text()).items()}
        except (OSError, ValueError):
            self.known = {}
        self.pending = []

    def offset(self, b):
        n = len(b)
        if n in self.known:
            return self.known[n]
        vx, vy, vz = struct.unpack_from("<3f", b, 32)
        if math.sqrt(vx * vx + vy * vy + vz * vz) > 3:
            self.pending.append(b)
        if len(self.pending) < 60:
            return None
        found = None
        for off in range(232, n - DASH_SIZE + 1, 4):
            ok = 0
            for p in self.pending:
                v = math.sqrt(sum(x * x for x in struct.unpack_from("<3f", p, 32)))
                sp = struct.unpack_from("<f", p, off + 12)[0]
                ok += abs(sp - v) < 0.02 * v + 0.05
            if ok >= 0.95 * len(self.pending):
                found = off
                break
        self.pending = []
        if found is None:
            print("! {0}-byte packets: could not find the dash block; keep driving".format(n))
            return None
        print("Packet layout: {0} bytes, dash block at byte {1}{2}".format(
            n, found, "" if KNOWN_DASH_OFFSET.get(n) == found else " (new layout)"))
        self.known[n] = found
        try:
            self.path.write_text(json.dumps({str(k): v for k, v in self.known.items()}, indent=1))
        except OSError:
            pass
        return found


# ----------------------------------------------------------------- conversion to the shared schema

def gear_of(raw):
    # Forza: 0 = reverse, 1.. = gears, 11 = neutral. Shared schema: -1 = R, 0 = N, 1 = 1st.
    if raw == 0:
        return -1
    return 0 if raw >= 11 else raw


def sample_row(d, t_ms, invalid, lock_deg):
    """d must already be in the shared frame (Logger.on_packet negates z)."""
    sp = d["speed"]
    st = d["steer"] / 127.0
    # body slip angle: direction of travel vs the nose, degrees, + = sliding to the right
    beta = math.degrees(math.atan2(d["vel_x"], d["vel_z"])) if sp > 3 else 0.0
    vals = [
        str(int(t_ms)),
        "0.000000",                                   # no track spline in Forza: derive from x/z
        "{0:.2f}".format(sp * 3.6),
        "{0:.2f}".format(sp * MPH_PER_MS),
        "{0:.3f}".format(d["accel"] / 255.0),
        "{0:.3f}".format(d["brake"] / 255.0),
        "{0:.3f}".format(1.0 - d["clutch"] / 255.0),  # AC convention: 1 = engaged (pedal up)
        "{0:.1f}".format(st * lock_deg / 2.0),        # steering wheel angle, degrees, + = right
        str(gear_of(d["gear"])),
        str(int(d["rpm"])),
        "{0:.2f}".format(d["pos_x"]), "{0:.2f}".format(d["pos_y"]), "{0:.2f}".format(d["pos_z"]),
        "{0:.3f}".format(-d["acc_x"] / G),             # AC convention: g_lat < 0 turning right
        "{0:.3f}".format(d["acc_z"] / G),              # + = accelerating
    ]
    # Forza's normalised slip (1 = at the grip limit); it can't be converted to degrees / ratio
    vals += ["{0:.3f}".format(d["slip_angle_" + w]) for w in ("fl", "fr", "rl", "rr")]
    vals += ["{0:.3f}".format(d["slip_ratio_" + w]) for w in ("fl", "fr", "rl", "rr")]
    vals.append("1" if invalid else "0")
    vals += ["{0:.1f}".format(beta),
             "{0:.1f}".format(math.degrees(d["ang_y"]))]                 # deg/s, + = turning right
    vals += ["{0:.3f}".format(d["comb_slip_" + w]) for w in WHEELS]    # normalised combined slip
    vals += ["{0:.1f}".format((d["tyre_t_" + w] - 32) / 1.8) for w in WHEELS]   # FH6 sends deg F
    vals += ["{0:.4f}".format(d["susp_m_" + w]) for w in WHEELS]
    vals += ["{0:.3f}".format(d["handbrake"] / 255.0), "{0:.3f}".format(st),
             "{0:.3f}".format(d["driving_line"] / 127.0), "{0:.3f}".format(d["ai_brake_diff"] / 127.0)]
    return vals


# ----------------------------------------------------------------- input device

DEVICE_CHOICES = ("auto", "wheel", "gamepad", "keyboard")


def _lap_analysis():
    """lap_analysis.py from Simetrics/analysis (for the device heuristic), or None."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("lap_analysis", STUDIO / "analysis" / "lap_analysis.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:      # the logger must keep logging without it
        print("! device heuristic unavailable ({0}): laps get input_device from --device only".format(e))
        return None


def device_setting(args):
    """--device, else fh6_input_device in data/studio/settings.json, else auto."""
    if args.device:
        return args.device
    try:
        v = json.loads((STUDIO / "data" / "studio" / "settings.json").read_text(encoding="utf-8")).get("fh6_input_device")
        return v if v in DEVICE_CHOICES else "auto"
    except (OSError, ValueError):
        return "auto"


def device_meta(setting, la, rows, meta):
    """input_device meta for one lap: the setting if one is given, else the heuristic. The heuristic's
    answer is always recorded too, so a wrong setting shows up."""
    guess, evidence = "unknown", ""
    if la is not None:
        try:
            cols = {k: [float(r[i]) for r in rows] for i, k in enumerate(COLUMNS)}
            guess, evidence = la.classify_device(la.Lap("run.csv", dict(meta, lap_time_ms=meta["lap_time_ms"]), cols))
        except Exception as e:
            evidence = "heuristic failed: {0}".format(e)
    if setting != "auto":
        out = {"input_device": setting, "input_device_source": "setting", "input_device_heuristic": guess}
    else:
        out = {"input_device": guess, "input_device_source": "heuristic" if la is not None else "none"}
    out["input_device_evidence"] = evidence
    return out


# ----------------------------------------------------------------- storage

def docs_dir():
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    cands = [Path(os.environ[v]) / "Documents" for v in ("OneDrive", "OneDriveConsumer") if os.environ.get(v)]
    cands += [home / "OneDrive" / "Documents", home / "Documents"]
    return next((c for c in cands if c.is_dir()), HERE)


def safe_name(s):
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in s)


def fmt_time(ms):
    ms = int(ms)
    return "{0}:{1:02d}.{2:03d}".format(ms // 60000, (ms // 1000) % 60, ms % 1000)


class Store:
    def __init__(self, root, session, car_override, track_override):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.session = session
        self.car_override = car_override
        self.track_override = track_override
        self.cars = self._load("cars.json", {})
        self.routes = self._load("routes.json", [])
        self.lap_counters = {}

    def _load(self, name, default):
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _save(self, name, obj):
        (self.root / name).write_text(json.dumps(obj, indent=1), encoding="utf-8")

    def car_name(self, ordinal):
        key = str(ordinal)
        if self.car_override and self.cars.get(key) != self.car_override:
            self.cars[key] = self.car_override
            self._save("cars.json", self.cars)
        return self.cars.get(key, "car{0}".format(ordinal))

    def known_finish(self, kind, x, z):
        """Finish point of a known route of this kind starting within 40 m, or None."""
        for r in self.routes:
            if r["kind"] == kind and "fx" in r and math.hypot(r["x"] - x, r["z"] - z) < 40:
                return r["fx"], r["fz"]
        return None

    def route_name(self, kind, x, z, fx=None, fz=None):
        """Same kind starting within 40 m (and, for sprints, finishing within 50 m)."""
        if self.track_override:
            return self.track_override
        for r in self.routes:
            if r["kind"] == kind and math.hypot(r["x"] - x, r["z"] - z) < 40:
                if fx is None or ("fx" in r and math.hypot(r["fx"] - fx, r["fz"] - fz) < 50):
                    return r["name"]
        prefix = kind.split("_")[0]
        n = 1 + sum(1 for r in self.routes if r["name"].startswith(prefix + "-"))
        r = {"name": "{0}-{1}".format(prefix, n), "kind": kind, "x": round(x, 1), "z": round(z, 1)}
        if fx is not None:
            r["fx"], r["fz"] = round(fx, 1), round(fz, 1)
        self.routes.append(r)
        self._save("routes.json", self.routes)
        return r["name"]

    def save(self, rows, meta, car, route):
        d = self.root / "{0}@{1}".format(safe_name(car), safe_name(route))
        d.mkdir(exist_ok=True)
        if d not in self.lap_counters:
            nums = [int(p.name.split("_L")[1][:2]) for p in d.glob(self.session + "_L*.csv")]
            self.lap_counters[d] = max(nums, default=0)
        self.lap_counters[d] += 1
        n = self.lap_counters[d]
        meta = dict(meta, car=car, track=route, layout="", session=self.session, lap_number=n)
        name = "{0}_L{1:02d}_{2}{3}.csv".format(
            self.session, n, fmt_time(meta["lap_time_ms"]).replace(":", "-").replace(".", "-"),
            "" if meta["valid"] else "_INVALID")
        path = d / name
        tmp = path.with_suffix(".part")
        with open(tmp, "w", newline="\n") as f:
            for k in sorted(meta):
                f.write("# {0}={1}\n".format(k, meta[k]))
            f.write(",".join(COLUMNS) + "\n")
            for r in rows:
                f.write(",".join(r) + "\n")
        os.replace(tmp, path)
        return path


# ----------------------------------------------------------------- feedback / hotkeys

def beep(freq, ms=120, times=1):
    try:
        import winsound
    except ImportError:
        return

    def job():
        for _ in range(times):
            winsound.Beep(freq, ms)
    threading.Thread(target=job, daemon=True).start()


class Hotkeys:
    VK = {"F9": 0x78, "F10": 0x79, "F11": 0x7A}

    def __init__(self):
        try:
            import ctypes
            self.get = ctypes.windll.user32.GetAsyncKeyState
        except (ImportError, AttributeError):
            self.get = None
        self.down = {k: False for k in self.VK}

    def pressed(self):
        out = []
        if self.get is None:
            return out
        for k, vk in self.VK.items():
            now = bool(self.get(vk) & 0x8000)
            if now and not self.down[k]:
                out.append(k)
            self.down[k] = now
        return out


# ----------------------------------------------------------------- run recording

class Run:
    def __init__(self, kind, t0_ms, d):
        self.kind = kind            # circuit / sprint / freeroam_loop / freeroam_sprint
        self.t0 = t0_ms
        self.standing = False       # lap 1 of a race: standing start from the grid
        self.rows = []
        self.times = []             # float ms, parallel to rows (for rewind truncation)
        self.invalid = False
        self.rewinds = 0
        self.start = (d["pos_x"], d["pos_z"])
        self.dist0 = d["distance"]
        self.last_xyz = (d["pos_x"], d["pos_y"], d["pos_z"])
        self.distance = 0.0

    def add(self, t_ms, d, lock):
        if self.times and t_ms < self.times[-1] - 50:
            # rewind: throw away the samples that are being replayed
            while self.times and self.times[-1] >= t_ms:
                self.times.pop()
                self.rows.pop()
            self.invalid = True
            self.rewinds += 1
        elif self.times and t_ms <= self.times[-1]:
            return
        xyz = (d["pos_x"], d["pos_y"], d["pos_z"])
        self.distance += math.sqrt(sum((a - b) ** 2 for a, b in zip(xyz, self.last_xyz)))
        self.last_xyz = xyz
        self.times.append(t_ms)
        self.rows.append(sample_row(d, t_ms - self.t0, self.invalid, lock))


class Logger:
    def __init__(self, args, store):
        self.args = args
        self.store = store
        self.run = None
        self.race_active = False
        self.race_lap_prev = 0
        self.race_t_prev = 0.0
        self.race_frozen_since = None
        self.laps_in_race = 0
        self.wait_race_exit = False     # after the flag: ignore the frozen race until it is left
        self.last_pause = 0.0
        self.gate = None            # free roam start/finish line: (x, z, hx, hz)
        self.finish = None          # free roam finish line for point-to-point
        self.gate_side = None
        self.finish_side = None
        self.prev = None
        self.saved = 0
        self.device = device_setting(args)
        self.la = _lap_analysis()

    # --- helpers
    def status(self, msg):
        print(time.strftime("%H:%M:%S ") + msg, flush=True)

    def base_meta(self, d, run, lap_ms):
        return {
            "logger_version": VERSION, "game": "fh6", "driver": "",
            "car_ordinal": d["car_ordinal"], "car_class": d["car_class"], "car_pi": d["car_pi"],
            "rpm_max": int(round(d["rpm_max"])), "rpm_idle": int(round(d["rpm_idle"])),
            "drivetrain": d["drivetrain"], "track_length_m": "0.0", "lap_time_ms": int(round(lap_ms)),
            "valid": 0 if run.invalid else 1, "splits_ms": "", "slip_units": "normalized",
            "sample_hz": int(round((len(run.times) - 1) / max((run.times[-1] - run.times[0]) / 1000.0, 1e-3))),
            "closed": 1 if run.kind in ("circuit", "freeroam_loop") else 0, "kind": run.kind,
            "run_distance_m": "{0:.1f}".format(run.distance), "rewinds": run.rewinds,
            "start_x": "{0:.1f}".format(run.start[0]), "start_z": "{0:.1f}".format(run.start[1]),
            "steer_lock_deg": self.args.wheel_lock, "standing_start": 1 if run.standing else 0,
        }

    def finish_run(self, d, lap_ms, route_xz, extra=None, finish_xz=(None, None)):
        run = self.run
        if run is None or len(run.rows) < 60 * 5 or run.distance < 100:
            return None
        lap_ms = int(round(lap_ms))
        meta = self.base_meta(d, run, lap_ms)
        meta.update(extra or {})
        meta.update(device_meta(self.device, self.la, run.rows, meta))
        if meta.get("input_device_heuristic") not in (None, "unknown", meta["input_device"]):
            self.status("! input device set to {0}, but this lap looks like {1} ({2})".format(
                meta["input_device"], meta["input_device_heuristic"], meta["input_device_evidence"]))
        car = self.store.car_name(d["car_ordinal"])
        route = self.store.route_name(run.kind, route_xz[0], route_xz[1], *finish_xz)
        path = self.store.save(run.rows, meta, car, route)
        self.saved += 1
        self.status("Saved {0} {1}  {2}  {3}  ({4})".format(
            route, fmt_time(lap_ms), "valid" if meta["valid"] else "INVALID (rewind)", meta["input_device"], path.name))
        beep(1000, 90, 2)
        return path

    # --- races (game lap counter / race timer)
    def on_race_packet(self, d):
        """A race is on while the game shows a race position (race_time also runs in free roam).
        Returns False in free roam."""
        rt = d["race_time"]
        # the lap timer only runs in events; race_pos may be 0 in solo events, and the lap timer
        # reads 0 for a packet at each line crossing (when lap_no has just ticked up)
        in_race = d["race_pos"] > 0 or d["cur_lap"] > 0 or d["lap_no"] > 0
        if self.wait_race_exit:
            if in_race and abs(rt - self.race_t_prev) < 0.5:
                return True
            self.wait_race_exit = False
        if not self.race_active:
            if not in_race:
                return False
            self.race_active = True
            self.race_lap_prev = d["lap_no"]
            self.laps_in_race = 0
            self.race_frozen_since = None
            self.race_t_prev = rt
            self.gate = self.finish = None
            if rt < 1.0:
                self.run = Run("circuit", 0.0, d)
                self.run.standing = True
                self.status("Race started")
            else:
                self.run = None     # joined mid-race: wait for the next lap
                self.status("Race in progress: logging from the next lap")
        if not in_race:
            # FH6 cuts to all-zero packets the moment you cross the finish (the clock does not
            # freeze first) and comes back in free roam; quitting via the pause menu looks the
            # same, so end_race judges completeness from the last live packet (self.prev)
            self.end_race(self.prev, finished=None)
            return False
        if rt < 1.0 and self.race_t_prev > 3:
            self.status("Race restarted: unfinished run dropped")
            self.race_active = False
            self.run = None
            return self.on_race_packet(d)
        if d["lap_no"] > self.race_lap_prev:
            # crossed the line: the lap that just ended
            if self.run is not None and self.run.times:
                lap_ms = d["last_lap"] * 1000 if d["last_lap"] > 0 else self.run.times[-1]
                if abs(lap_ms - self.run.times[-1]) > 2000:
                    lap_ms = self.run.times[-1]
                # key the route on the lap's last sample (at the line): on the final lap the
                # packet that ticks the counter comes during the finish cutscene, car parked elsewhere
                line = (self.run.last_xyz[0], self.run.last_xyz[2])
                self.finish_run(d, lap_ms, line,
                                {"standing_start": 1 if self.run.standing else 0,
                                 "race_lap": self.race_lap_prev + 1})
            self.laps_in_race += 1
            self.race_lap_prev = d["lap_no"]
            self.run = Run("circuit", 0.0, d)
        elif d["lap_no"] < self.race_lap_prev:
            # rewound back over the line: this lap's start was not seen cleanly
            self.race_lap_prev = d["lap_no"]
            self.run = None
        # race clock frozen while driving = finished (Horizon keeps the car rolling after the line);
        # a clock going backwards is a rewind, handled by Run.add
        if abs(rt - self.race_t_prev) < 1e-4 and d["speed"] > 3:
            if self.race_frozen_since is None:
                self.race_frozen_since = time.time()
            elif time.time() - self.race_frozen_since > 1.0:
                self.end_race(d, finished=True)
                self.wait_race_exit = True
                return True
        else:
            self.race_frozen_since = None
        if self.run is not None:
            self.run.add(d["cur_lap"] * 1000, d, self.args.wheel_lock)
        self.race_t_prev = rt
        return True

    def end_race(self, d, finished):
        """finished: True (clock froze at the flag) or None (race just ended: judge from d,
        the last live packet)."""
        run = self.run
        if run is not None and run.times:
            if self.laps_in_race > 0:
                # final lap of a circuit, if the counter did not tick at the flag: must end at the line
                if math.hypot(d["pos_x"] - run.start[0], d["pos_z"] - run.start[1]) < 30:
                    self.finish_run(d, run.times[-1], (run.last_xyz[0], run.last_xyz[2]),
                                    {"race_lap": self.race_lap_prev + 1})
                else:
                    self.status("Race left mid-lap: unfinished lap dropped")
            else:
                run.kind = "sprint"
                fin = self.store.known_finish("sprint", run.start[0], run.start[1])
                if fin is not None:
                    ok, how = math.hypot(d["pos_x"] - fin[0], d["pos_z"] - fin[1]) < 50, "route"
                else:
                    # first run of this route: you cross a finish line at speed and not braking;
                    # people pause to quit after stopping or crashing
                    ok = finished or (d["speed"] > 9 and d["brake"] < 128 and run.distance > 500)
                    how = "flag" if finished else "speed"
                if ok:
                    self.finish_run(d, run.times[-1], run.start, {"finish_check": how},
                                    finish_xz=(d["pos_x"], d["pos_z"]))
                else:
                    self.status("Race left before the finish: run dropped")
        self.status("Race over")
        self.race_active = False
        self.run = None

    # --- free roam (hotkey lines)
    @staticmethod
    def side(line, d):
        x, z, hx, hz = line
        dx, dz = d["pos_x"] - x, d["pos_z"] - z
        along = dx * hx + dz * hz
        lateral = abs(-dx * hz + dz * hx)
        return along >= 0, lateral < 20

    def make_line(self, d):
        vx, vz = d["pos_x"] - self.prev["pos_x"], d["pos_z"] - self.prev["pos_z"]
        n = math.hypot(vx, vz)
        if n < 1e-3:
            # standing still: use the yaw (game heading = (sin yaw, cos yaw); z is negated here)
            vx, vz, n = math.sin(d["yaw"]), -math.cos(d["yaw"]), 1.0
        return (d["pos_x"], d["pos_z"], vx / n, vz / n)

    def on_key(self, key, d):
        if key == "F9":
            self.gate, self.finish = self.make_line(d), None
            self.gate_side = (True, True)
            self.run = Run("freeroam_loop", d["timestamp_ms"], d)
            self.status("Start/finish line set; lap timing from here (F10 = finish line, F11 = stop)")
            beep(880)
        elif key == "F10" and self.gate is not None:
            self.finish = self.make_line(d)
            self.finish_side = (True, True)
            if self.run is not None and self.run.times:
                self.run.kind = "freeroam_sprint"
                self.finish_run(d, self.run.times[-1] - self.run.t0, (self.gate[0], self.gate[1]),
                                finish_xz=(self.finish[0], self.finish[1]))
            self.run = None
            self.status("Finish line set: drive back through the start line for the next run")
            beep(660)
        elif key == "F11":
            self.gate = self.finish = None
            self.run = None
            self.status("Lines cleared, run dropped")
            beep(440, 200)

    def on_freeroam_packet(self, d):
        if self.gate is None:
            return
        t = float(d["timestamp_ms"])
        if self.run is not None and self.run.times:
            jump = math.hypot(d["pos_x"] - self.prev["pos_x"], d["pos_z"] - self.prev["pos_z"])
            dt = max((t - self.run.times[-1]) / 1000.0, 1 / 60.0)
            if jump > 20 + d["speed"] * dt * 2:
                self.run.invalid = True     # rewind / reset to road teleports the car
                self.run.rewinds += 1
        side, near = self.side(self.gate, d)
        crossed = side and not self.gate_side[0] and near
        self.gate_side = (side, near)
        if self.finish is not None:
            fside, fnear = self.side(self.finish, d)
            fcrossed = fside and not self.finish_side[0] and fnear
            self.finish_side = (fside, fnear)
            if fcrossed and self.run is not None:
                self.run.add(t, d, self.args.wheel_lock)
                self.finish_run(d, t - self.run.t0, (self.gate[0], self.gate[1]),
                                finish_xz=(self.finish[0], self.finish[1]))
                self.run = None
                return
            if crossed:
                self.run = Run("freeroam_sprint", t, d)
        elif crossed:
            if self.run is not None:
                self.run.add(t, d, self.args.wheel_lock)
                self.finish_run(d, t - self.run.t0, (self.gate[0], self.gate[1]))
            self.run = Run("freeroam_loop", t, d)
        if self.run is not None:
            self.run.add(t, d, self.args.wheel_lock)

    def on_packet(self, d, keys):
        if d["is_race_on"] == 0:
            # paused / menus / loading: FH6 sends all-zero packets
            self.last_pause = time.time()
            if self.run is not None and not self.race_active and self.gate is not None:
                self.status("Paused: unfinished run dropped")
                self.run = None
            return
        d["pos_z"] = -d["pos_z"]    # FH6's world is left-handed; the shared frame is AC's
        if self.prev is None:
            self.prev = d
        for k in keys:
            if not self.race_active:
                self.on_key(k, d)
        if not self.on_race_packet(d):
            self.on_freeroam_packet(d)
        self.prev = d


# ----------------------------------------------------------------- main loops

def open_socket(args):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    s.bind((args.bind, args.port))
    s.settimeout(0.5)
    return s


def packets(args, layout, raw=None):
    s = open_socket(args)
    print("Listening on {0}:{1} - set FH6 Data Out to 127.0.0.1 port {1}".format(args.bind, args.port), flush=True)
    seen = False
    while True:
        try:
            b, _ = s.recvfrom(2048)
        except socket.timeout:
            yield None
            continue
        if raw is not None:
            raw.write(struct.pack("<dI", time.time(), len(b)) + b)
        if not seen:
            print("Receiving {0}-byte packets".format(len(b)), flush=True)
            seen = True
        if len(b) < 232 + DASH_SIZE:
            continue
        off = layout.offset(b)
        if off is None:
            yield None
            continue
        yield decode(b, off)


def run_logger(args):
    root = Path(args.root) if args.root else STUDIO / "data" / "fh6"
    store = Store(root, time.strftime("%y%m%d-%H%M%S"), args.car, args.track)
    logger = Logger(args, store)
    keys = Hotkeys()
    layout = Layout(HERE / "layout.json")
    raw = open(args.raw, "ab") if args.raw else None
    print("FH6 LapLogger {0} - laps go to {1}".format(VERSION, root))
    print("Input device: {0}".format("detected from each lap's data (set it with --device wheel|gamepad|keyboard)"
                                     if logger.device == "auto" else logger.device))
    print("Races are split automatically. Free roam: F9 start/finish line, F10 finish line, F11 stop. Ctrl+C quits.")
    try:
        for d in packets(args, layout, raw):
            k = keys.pressed()
            if d is None:
                if k:
                    print("(hotkey ignored: no telemetry yet)")
                continue
            logger.on_packet(d, k)
    except KeyboardInterrupt:
        print("\nStopped. {0} laps/runs saved this session.".format(logger.saved))
    finally:
        if raw:
            raw.close()


def run_probe(args):
    layout = Layout(HERE / "layout.json")
    last = 0.0
    raw = open(args.raw, "ab") if args.raw else None
    try:
        for d in packets(args, layout, raw):
            if d is None or time.time() - last < 0.2:
                continue
            last = time.time()
            print("\ron={is_race_on} {mph:5.1f}mph rpm={rpm:5.0f} gear={g:>2} gas={accel:3d} brk={brake:3d} "
                  "clu={clutch:3d} hb={handbrake:3d} steer={steer:4d} xyz=({pos_x:.0f},{pos_y:.0f},{pos_z:.0f}) "
                  "lap={lap_no} cur={cur_lap:.1f} race={race_time:.1f} car={car_ordinal}  ".format(
                      mph=d["speed"] * MPH_PER_MS, g=d["gear"], **d), end="", flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        if raw:
            raw.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", nargs="?", default="log", choices=["log", "probe"])
    p.add_argument("--port", type=int, default=5300)
    p.add_argument("--bind", default="127.0.0.1", help="address to listen on (0.0.0.0 for another PC/console)")
    p.add_argument("--root", help="lap data folder (default: Simetrics/data/fh6)")
    p.add_argument("--car", help="name for the car you are driving (remembered per car in cars.json)")
    p.add_argument("--track", help="force this route name for everything logged in this session")
    p.add_argument("--wheel-lock", type=int, default=900,
                   help="wheel rotation in degrees, to turn Forza's -127..127 steering into degrees (default 900)")
    p.add_argument("--device", choices=DEVICE_CHOICES,
                   help="what you drive with (FH6 doesn't say): wheel, gamepad, keyboard, or auto = detect from each lap "
                        "(default: fh6_input_device in data/studio/settings.json, else auto)")
    p.add_argument("--raw", help="also append raw packets to this file (for debugging)")
    args = p.parse_args()
    (run_probe if args.mode == "probe" else run_logger)(args)


if __name__ == "__main__":
    main()
