"""Checks the input-device handling of lap_analysis.py on synthetic laps (no game or real laps needed):

    py -3.12 analysis/test_devices.py

Builds a small track (4 corners), drives it three times with the same line and speeds on a wheel, a
gamepad and a keyboard (only the input shapes differ), writes the laps as lap CSVs to a temporary
folder and checks device detection, the device-aware corner metrics, tips and habit checks, and the
CLI report. Exits non-zero if a check fails.
"""

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lap_analysis as la  # noqa: E402

COLUMNS = ["t_ms", "pos", "speed", "speed_mph", "gas", "brake", "clutch", "steer", "gear", "rpm", "x", "y", "z",
           "g_lat", "g_lon", "sa_fl", "sa_fr", "sa_rl", "sa_rr", "sr_fl", "sr_fr", "sr_rl", "sr_rr", "invalid",
           "beta", "yaw_rate", "handbrake", "steer_in"]
fails = []


def check(name, ok, detail=""):
    print("{0} {1}{2}".format("PASS" if ok else "FAIL", name, (": " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def track():
    """Rounded rectangle 400 x 200 m, corner radius 25 m: (s, x, z, curvature) every 0.5 m."""
    W, H, R = 400.0, 200.0, 25.0
    segs = [("line", W - 2 * R), ("arc", R), ("line", H - 2 * R), ("arc", R)] * 2
    pts, x, z, hdg, s = [], 0.0, 0.0, 0.0, 0.0
    for kind, v in segs:
        n = int((v if kind == "line" else v * math.pi / 2) / 0.5)
        for _ in range(n):
            k = 1.0 / v if kind == "arc" else 0.0
            pts.append((s, x, z, k))
            hdg += k * 0.5
            x += 0.5 * math.cos(hdg)
            z += 0.5 * math.sin(hdg)
            s += 0.5
    # start/finish line halfway down the first straight, so no corner sits on the line
    k0 = int((W - 2 * R) / 2 / 0.5)
    pts = pts[k0:] + pts[:k0]
    return [(i * 0.5, x, z, k) for i, (_s, x, z, k) in enumerate(pts)]


def drive(device, pts, lap_s_scale=1.0):
    """One lap at 50 Hz. Speeds: 150 km/h on straights, 70 in corners; brake 60 m before each corner."""
    L = pts[-1][0]
    corners = [p[0] for i, p in enumerate(pts) if p[3] and not pts[i - 1][3]]

    def v_at(s):
        v = 150.0
        for c in corners:
            d = c - s
            if 0 <= d < 60:
                v = min(v, 70 + (150 - 70) * d / 60)
            elif -40 < d < 0:                              # in the corner (~40 m)
                v = min(v, 70)
            elif -110 < d <= -40:
                v = min(v, 70 + (150 - 70) * (-40 - d) / 70)
        return v * lap_s_scale

    rows, t, s, i = [], 0.0, 0.0, 0
    steer_kb, pad_hold = 0.0, 0.0
    while s < L:
        while i + 1 < len(pts) and pts[i + 1][0] <= s:
            i += 1
        _s, x, z, k = pts[i]
        v = v_at(s)
        dv = v_at(s + 5) - v
        gas = max(0.0, min(1.0, 0.25 + dv / 4)) if dv >= 0 else 0.0
        brake = max(0.0, min(1.0, -dv / 6)) if dv < -0.5 else 0.0
        # wheel: steering-wheel angle from the curvature, smoothed (ratio 15, wheelbase 2.5 m)
        ahead = [p[3] for p in pts[i:i + 20]] + [p[3] for p in pts[max(0, i - 20):i]]
        wheel_deg = math.degrees(math.atan(2.5 * sum(ahead) / max(len(ahead), 1))) * 15
        sn = wheel_deg / 450.0                       # -1..1 of a 900 deg wheel
        if device == "keyboard":
            gas = 1.0 if gas > 0.3 else 0.0
            brake = 1.0 if brake > 0.1 else 0.0
            target = (1.0 if abs(sn) > 0.12 else 0.5 if abs(sn) > 0.03 else 0.0) * (1 if sn >= 0 else -1)
            steer_kb += max(-0.08, min(0.08, target - steer_kb))       # keys ramp at 4 locks/s
            sn = steer_kb
        elif device == "gamepad":
            # thumb flicks the stick out and lets it snap back to the centre
            pad_hold = (0.8 if abs(sn) > 0.03 else 0.0) * (1 if sn >= 0 else -1)
            sn = pad_hold
        steer_in = max(-1.0, min(1.0, sn))
        rows.append([int(t * 1000), 0.0, v, v * la.MPH, gas, brake, 1.0, steer_in * 450.0, 3, 4000,
                     x, 0.0, z, 0.0, 0.0] + [0.0] * 8 + [0, 0.0, 0.0, 0.0, steer_in])
        dt = 0.02
        t += dt
        s += v / 3.6 * dt
    return rows, t


def write(folder, name, rows, t_s, meta):
    folder.mkdir(parents=True, exist_ok=True)
    m = dict({"game": "fh6", "car": "synthetic", "track": "rect", "layout": "", "session": "261101-120000",
              "lap_time_ms": int(t_s * 1000), "valid": 1, "track_length_m": "0.0", "sample_hz": 50, "closed": 1,
              "slip_units": "normalized", "steer_lock_deg": 900, "logger_version": "test"}, **meta)
    with open(folder / name, "w", newline="\n") as f:
        for k in sorted(m):
            f.write("# {0}={1}\n".format(k, m[k]))
        f.write(",".join(COLUMNS) + "\n")
        for r in rows:
            f.write(",".join("{0:.4f}".format(v) if isinstance(v, float) else str(v) for v in r) + "\n")
    return folder / name


def main():
    pts = track()
    tmp_root = HERE.parent / "data" / "test_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="laplogger_devices_", dir=tmp_root))
    files = {}
    for dev in ("wheel", "gamepad", "keyboard"):
        for n, scale in ((1, 1.0), (2, 0.97), (3, 0.99)):
            rows, t_s = drive(dev, pts, scale)
            files[(dev, n)] = write(tmp / "fh6" / "synthetic@rect-{0}".format(dev),
                                    "261101-120000_L{0:02d}_x.csv".format(n), rows, t_s,
                                    {"lap_number": n, "input_device": dev, "input_device_source": "setting"})
    laps = {k: la.load_csv(p) for k, p in files.items()}

    # 1. detection from the data alone
    for dev in ("wheel", "gamepad", "keyboard"):
        got, ev = la.classify_device(laps[(dev, 1)])
        check("classify_device: synthetic {0} lap".format(dev), got == dev, "{0} ({1})".format(got, ev))

    # 2. corner metrics per device (one shared distance frame, from the wheel lap)
    frame = laps[("wheel", 1)]
    length = la.project_positions(frame, list(laps.values()))
    for l in laps.values():
        la.prepare(l, length)
    corners = la.find_corners(frame.grid)
    check("4 corners found on the synthetic track", len(corners) == 4, len(corners))
    m = {k: [la.corner_metrics(l.grid, c, l.device) for c in corners] for k, l in laps.items()}
    kb, pad, wh = m[("keyboard", 1)][0], m[("gamepad", 1)][0], m[("wheel", 1)][0]
    check("keyboard: no peak brake input (keys are on/off)", kb["brake_peak"] is None, kb["brake_peak"])
    check("keyboard: brake point still measured", kb["brake_at"] is not None)
    check("keyboard: full-throttle point still measured", kb["full_gas_at"] is not None)
    check("gamepad: peak brake input measured (triggers have travel)", pad["brake_peak"] is not None)
    check("gamepad: no steering degrees, steering in % of lock", pad["steer_max"] is None and pad["steer_max_pct"] > 50, pad)
    check("wheel: steering degrees kept", wh["steer_max"] is not None and wh["steer_max"] > 30, wh.get("steer_max"))

    # 3. tips: pressure and steering only compared on the same kind of device
    soft = dict(m[("gamepad", 2)][0], brake_peak=0.5)
    ids = [t["id"] for t in la.advice_items(soft, dict(wh, brake_peak=0.95), device="gamepad", ref_device="wheel")]
    check("gamepad lap vs wheel reference: no brake-pressure or steering comparison",
          "brake_soft" not in ids and "steer_corrections" not in ids, ids)
    ids = [t["id"] for t in la.advice_items(soft, dict(m[("gamepad", 1)][0], brake_peak=0.95), device="gamepad", ref_device="gamepad")]
    check("gamepad vs gamepad: soft trigger braking is compared", "brake_soft" in ids, ids)
    busy = dict(m[("gamepad", 2)][0], steer_work_pct=400.0)
    ids = [t["id"] for t in la.advice_items(busy, m[("gamepad", 1)][0], device="gamepad", ref_device="gamepad")]
    check("gamepad vs gamepad: steering corrections judged in % of lock", "steer_corrections" in ids, ids)
    ids = [t["id"] for t in la.advice_items(m[("keyboard", 2)][0], m[("keyboard", 1)][0], device="keyboard", ref_device="keyboard")]
    check("keyboard vs keyboard: no brake-pressure tip", "brake_soft" not in ids, ids)
    early = dict(m[("keyboard", 2)][0], brake_at=(kb["brake_at"] or 0) - 20)
    ids = [t["id"] for t in la.advice_items(early, wh, device="keyboard", ref_device="wheel")]
    check("keyboard vs wheel: brake points still compared (distance)", "brake_early" in ids, ids)

    # 4. habit checks per device and focus
    tf = next(c for c in la.HABIT_CHECKS if c["key"] == "two_feet_s")
    for dev in ("gamepad", "keyboard"):
        lab, warn, meaning = la.habit_check_def(tf, "road", dev)
        check("{0}, road focus: overlap is not called left-foot braking and not a road warning".format(dev),
              "foot" not in lab.lower() and warn is None, lab)
        check("{0}, race focus: brake dragging still flagged".format(dev), la.habit_check_def(tf, "race", dev)[1] == 0.5)
    check("wheel, road focus: left-foot braking warning kept", "left-foot" in la.habit_check_def(tf, "road", "wheel")[0])
    h = la.road_habits(laps[("gamepad", 1)])
    check("gamepad without a clutch: clutch check not applicable", h["clutch_coast_s"] is None, h)
    check("wheel: clutch check measured", la.road_habits(laps[("wheel", 1)])["clutch_coast_s"] is not None)
    _lines, card = la.road_report([laps[("gamepad", n)] for n in (1, 2, 3)], None, "road")
    check("road report names the device and the clutch reason", "controller" in card and "no clutch used" in " ".join(_lines), _lines)

    # 5. CLI report on the synthetic folders
    for dev in ("keyboard", "gamepad"):
        out = subprocess.run([sys.executable, str(HERE / "lap_analysis.py"), "--root", str(tmp / "fh6"), "--game", "fh6",
                              "report", "--combo", "synthetic@rect-" + dev, "--session", "all", "--json"],
                             capture_output=True, text=True, encoding="utf-8")
        try:
            j = json.loads(out.stdout)
        except ValueError:
            check("CLI --json runs for {0} laps".format(dev), False, out.stderr[-400:])
            continue
        check("CLI --json: {0} laps report input_device".format(dev),
              j["input_device"] == dev and all(l["input_device"] == dev for l in j["laps"]))
        if dev == "keyboard":
            check("CLI --json: keyboard corners have no peak brake input", all(c["brake_peak_input"] is None for c in j["corners"]))
        else:
            check("CLI --json: gamepad corners have steering % but no degrees",
                  all(c["steer_max_deg"] is None and c["steer_max_pct"] is not None for c in j["corners"]))

    # 6. tag-device back-fill keeps the samples and the file time
    p = files[("wheel", 3)]
    before, st = p.read_text().split("t_ms,", 1)[1], p.stat().st_mtime
    la.write_meta(p, {"input_device": "wheel", "input_device_source": "user"})
    check("write_meta keeps samples and modification time", p.read_text().split("t_ms,", 1)[1] == before and p.stat().st_mtime == st)

    print("\n{0} check(s) failed".format(len(fails)) if fails else "\nall device checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
