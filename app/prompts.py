"""Analysis briefs for AI agents: one structured brief (downloadable as JSON) and the copy-ready prompt
text rendered from it, so both always carry the same numbers.

A brief is self-contained: identity (game, car, route ids and labels), how much history exists, the
comparison definitions and populations (which laps, which were excluded and why), references (best lap,
fastest section per corner, corner-map version), the measurements in the user's display units, data
quality and channel provenance, and what is unknown. The model is asked to explain these facts, not to
rediscover or recompute them. The goal (road / mix / race) decides which measured differences are listed
(lap_analysis scopes) and what the model may suggest; the app itself gives no advice.
"""

import json
import re
import sys
import time

import engine
import trends

BRIEF_FORMAT, BRIEF_VERSION = "laplogger-brief", 2

GOALS = {
    "road": "My goal is to become a better, safer real-world driver. Only suggest things that are safe and sensible "
            "in a normal road car on public roads. Lap-time techniques (brake points, corner speeds, racing lines, "
            "throttle timing) are track/sim only: don't present them as road advice.",
    "race": "My goal is lap time in the sim. Track and racing techniques are fine.",
    "mix": "I want both: faster laps in the sim and habits that carry over to real-world driving. Label every "
           "suggestion either TRACK/SIM ONLY or ROAD-TRANSFERABLE HABIT.",
}

DEFINITIONS = [
    "Corners T1, T2, ... are found by LapLogger from speed minima on the corner-map lap; they are not official "
    "corner names. Labels only match between briefs with the same corner-map id.",
    "Distances are along the lap from the start line.",
    "Min speed = lowest speed between a corner's boundaries (not a geometric apex).",
    "Peak brake input = highest brake pedal position (0-100%), not brake pressure.",
    "Pedal overlap = brake and throttle both above 10% at once; which foot was used is unknown.",
    "Input device (wheel, controller or keyboard) is what the logger recorded for each lap, with how it was decided. On a "
    "controller, brake and throttle are two triggers and steering is a thumbstick, so steering is given as % of full lock "
    "(degrees are unavailable) and pedal overlap says nothing about either foot. On a keyboard the keys are on/off, so "
    "peak brake input is unavailable; brake points, coasting, speeds and throttle points still apply. Pressure and "
    "steering are only compared between laps on the same kind of device.",
    "Spin candidate = slide angle over 60°; big slowdown = under 45% of the reference speed at the same spot and "
    "at least 40 km/h down. Neither proves a full spin or a collision.",
    "Off both pedals = throttle under 10% and footbrake under 5% (handbrake not counted).",
    "A value shown as unavailable was not recorded; it is not zero.",
    " ".join(x for x in ("Track status is separate from incidents: observed-clear means no breach was observed with "
                         "complete monitoring, not game certification.", engine.la.notes("track_status")) if x),
    "Without detected major incidents means a rolling lap with no detected spin candidate, big slowdown, known "
    "invalid/off-track/penalty state or serious data discontinuity. It does not prove an incident-free or legal lap.",
    "Game invalid, observed off-track and penalty lap counts are independent observations and can overlap on the same lap.",
]
UNKNOWN = ["assists (ABS, traction control, stability, gearbox) unless stated", "car setup / tune",
           "traffic and other cars", "weather and track conditions",
           "wheel or controller settings (rotation, deadzones, sensitivity, force feedback)"]


def _device(d, source=None, detail=None):
    out = {"device": d, "label": engine.DEVICE_LABELS.get(d, d)}
    if source:
        out["source"] = source
    if detail:
        out["detail"] = detail
    return out


class Units:
    def __init__(self):
        u = engine.units()
        self.mph = u["speed"] == "mph"
        self.ft = u["distance"] == "ft"
        self.sl = "mph" if self.mph else "km/h"
        self.dl = "ft" if self.ft else "m"

    def kmh(self, v, d=1):
        return None if v is None else round(v / 1.609344 if self.mph else v, d)

    def mphv(self, v, d=1):
        return None if v is None else round(v if self.mph else v * 1.609344, d)

    def m(self, v, d=0):
        return None if v is None else round(v * 3.28084 if self.ft else v, d)

    def text(self, s):
        """lap_analysis texts carry '12 m', '5 mph' or '40 km/h'."""
        s = re.sub(r"(\d+(?:\.\d+)?) (mph|km/h)", lambda g: "{0:.0f} {1}".format(
            self.mphv(float(g.group(1))) if g.group(2) == "mph" else self.kmh(float(g.group(1))), self.sl), s)
        return re.sub(r"(\d+(?:\.\d+)?) m\b", lambda g: "{0:.0f} {1}".format(self.m(float(g.group(1))), self.dl), s)

    @staticmethod
    def device_label(key, label, device):
        """Pedal overlap on a controller or keyboard is two triggers or keys, not two pedals."""
        if key == "two_feet_s" and device in ("gamepad", "keyboard"):
            return "Brake + throttle overlap ({0})".format("triggers" if device == "gamepad" else "keys")
        return label

    def metric(self, key, value):
        """(label, value in display units, unit, decimals)."""
        _k, label, unit, dec, scale, _g, _d = next(x for x in trends.METRICS if x[0] == key)
        if unit in ("m", "m/lap"):
            return label, self.m(value, dec), unit.replace("m", self.dl), dec
        return label, round(value * scale, dec), unit, dec


def _pct(v):
    return None if v is None else round(v)


def _t(s):
    return "-" if s is None else "{0}:{1:06.3f}".format(int(s // 60), s % 60)


def _date(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else None


def _num(v, unit="", d=0):
    if v is None:
        return "unavailable"
    return "{0:.{1}f}{2}".format(v, d, (" " + unit) if unit and not unit.startswith("%") else unit)


def _identity(c):
    raw_track = c["track"] + ("-" + c["layout"] if c.get("layout") else "")
    out = {"combo_id": c["id"], "game": c["game"], "game_name": c["game_name"], "car_id": c["car"], "car_label": c["car_name"],
           "route_id": raw_track, "route_label": c["track_name"], "event": c.get("event"),
           "label_note": "labels are display names{0}; ids are stable".format(
               " ({0})".format(engine.la.notes("route_names", "; ")) if engine.la.notes("route_names") else "")}
    extra = engine.la.hook(c["game"], "brief_identity")
    if extra:
        out.update(extra(c))
    if c.get("warnings"):
        out["warnings"] = c["warnings"]
    return out


def _cmd(game, folder, session=None, lap=None, goal="mix"):
    parts = ['"{0}"'.format(sys.executable), '"{0}"'.format(engine.STUDIO_DIR / "analysis" / "lap_analysis.py"),
             "--root", '"{0}"'.format(engine.DATA_DIR / game), "--game", game, "report", "--combo", '"{0}"'.format(folder)]
    if session:
        parts += ["--session", session]
    if lap is not None:
        parts += ["--lap", str(lap)]
    return " ".join(parts + ["--focus", goal, "--json"])


def _quality(q, U):
    if not q:
        return None
    unavailable = sorted(k for k, v in q["channels"].items() if v in ("missing", "unavailable"))
    constant = sorted(k for k, v in q["channels"].items() if v == "constant")
    return {"laps_checked": q["laps"], "sample_rate_hz": q["hz"], "max_sample_gap_ms": q["max_gap_ms"],
            "speed_vs_position_distance_pct": q["distance_check_pct"],
            "channels_not_recorded": unavailable, "channels_constant": constant,
            "laps_with_warnings": q["laps_with_warnings"],
            "note": "continuity and distance checks support timing and position; they don't verify game physics, "
                    "track limits or real-road transfer"}


def _common(page, goal, U, game):
    return {"format": BRIEF_FORMAT, "version": BRIEF_VERSION, "engine_version": engine.ENGINE_VERSION,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "page": page, "goal": goal, "goal_instruction": GOALS[goal],
            "units": {"speed": U.sl, "distance": U.dl, "time": "s"}, "definitions": [U.text(d) for d in DEFINITIONS],
            "unknown": UNKNOWN}


def _diffs(lap_rows, k, goal):
    counts = {}
    for l in lap_rows:
        for tid, scope in (l["tips"][k] if k < len(l["tips"]) else []):
            if goal == "road" and scope == "race":
                continue
            counts[(tid, scope)] = counts.get((tid, scope), 0) + 1
    need = max(2, len(lap_rows) * 0.4)
    return [{"id": t, "text": trends.TIP_PHRASES.get(t, t), "laps": n, "of": len(lap_rows),
             "scope": "track/sim only" if sc == "race" else "track and road"}
            for (t, sc), n in sorted(counts.items(), key=lambda x: -x[1]) if n >= need][:3]


def session_brief(analyser, cid, session, goal, U):
    c, r = analyser.result(cid)
    s = next((s for s in r["sessions"] if s["session"] == session), r["sessions"][-1])
    laps = [l for l in r["laps"] if l["session"] == s["session"]]
    cmp_rows = [l for l in laps if l["file"] in set(s["population"]["files"])]
    earlier = [x for x in r["sessions"] if x["start"] < s["start"]]
    best = next((l for l in r["laps"] if l["file"] == r["ref"]), None)
    corners = []
    for k, cn in enumerate(r["corners"]):
        sc = s["corners"][k]
        bm = best["corners"][k] if best else None
        corners.append({
            "corner": cn["name"], "from": U.m(cn["a_d"]), "to": U.m(cn["b_d"]),
            "gap_s": sc["loss"], "median_s": sc.get("median_s"), "n_laps": sc.get("n"),
            "fastest_section": {"lap": sc.get("fastest_lap"), "file": sc.get("fastest_file"), "time_s": sc.get("fastest_s"),
                                "min_speed": U.mphv(sc.get("fastest_min_mph"))},
            "best_lap_section": {"lap": "L{0}".format(best["number"]), "time_s": round(bm["seg_time"], 3),
                                 "min_speed": U.kmh(bm["min_speed"])} if bm else None,
            "median_min_speed": U.mphv(sc["min_mph"]), "min_speed_sd": U.mphv(sc["min_mph_sd"]),
            "brake_at": U.m(sc["brake_at"]), "brake_at_sd": U.m(sc["brake_sd"], 1), "braking_laps": sc.get("braking_laps"),
            "peak_brake_input_pct": None if sc["brake_peak"] is None else round(sc["brake_peak"] * 100),
            "steering_max_deg": sc["steer_max"], "steering_max_pct_of_lock": sc.get("steer_max_pct"),
            "slide_peak_deg": sc["beta_peak"], "gear": sc["gear"],
            "common_differences_vs_best_lap": _diffs(cmp_rows, k, goal),
        })
    per_lap = []
    for key, v in s["habits"].items():
        if v is None or not any(m[0] == key for m in trends.METRICS):
            continue
        label, val, unit, dec = U.metric(key, v)
        per_lap.append({"id": key, "label": U.device_label(key, label, s.get("device")), "value": val, "unit": unit,
                        "aggregation": "mean per lap" if key in ("handbrake_pulls", "incidents") else "median per lap",
                        "laps": "all laps of the session"})
    b = {"identity": _identity(c),
         "corner_map": r["corner_set"],
         "session": {"id": s["session"], "start": _date(s["start"]), "laps": len(laps), "valid": s["valid"],
                     "input_device": _device(s.get("device", "unknown")),
                     "input_devices": [{"device": d["device"], "label": d["label"], "laps": d["laps"]} for d in s.get("devices", [])],
                     "standing_starts": s.get("standing", 0), "clean": s["clean"],
                     "without_detected_major_incidents": s["clean"],
                     "invalid": s.get("invalid", 0), "off_track": s.get("off_track", 0),
                     "penalty": s.get("penalty", 0), "unverified": s.get("unverified", 0),
                     "best_s": s["best"], "typical_s": s["typical"],
                     "best_role": s.get("best_role", "recorded best; validity unverified"),
                     "typical_definition": s["typical_definition"] + ": " + (", ".join(s.get("typical_laps", [])) or "none"),
                     "theoretical_s": s["theoretical"],
                     "theoretical_note": "fastest section times from different laps added together: illustrative only, not a "
                                         "drivable lap, and corner gaps can't be added up as guaranteed time to gain"},
         "history": {"matching_sessions": len(r["sessions"]), "earlier_sessions": len(earlier),
                     "between_session_comparison_available": bool(earlier),
                     "earlier": [{"id": x["session"], "start": _date(x["start"]), "laps": x["laps"], "best_s": x["best"],
                                  "typical_s": x["typical"]} for x in earlier[-6:]]},
         "comparison": {"population": s["population"]["description"], "laps": s["population"]["laps"],
                        "excluded": s["population"]["excluded"],
                        "gap_definition": "median section time over the comparison laps minus the fastest of them (the same as lap_analysis.py)"},
         "reference": {"best_lap": {"lap": "L{0}".format(best["number"]) if best else None, "session": best["session"] if best else None,
                                    "file": r["ref"], "time_s": r["ref_time"], "role": r["reference_role"]},
                       "corner_map_lap": {"lap": r["corner_set"]["frame_lap"], "file": r["corner_set"]["frame_file"],
                                          "role": "defines distances and corner numbering"}},
         "corners": corners, "per_lap": per_lap,
         "quality": _quality(s.get("quality"), U), "provenance": r.get("provenance", {}),
         "track": {"length": U.m(r["length_m"]), "closed": r["closed"]}}
    files = {"lap_folder": str(engine.DATA_DIR / c["game"] / c["folder"]),
             "drill_down_command": _cmd(c["game"], c["folder"], s["session"], goal=goal)}
    ask = ["Which corners cost the most time in this session, and what do the measurements show is different there?",
           "What changed compared with the earlier sessions listed?" if earlier else
           "How consistent are the laps within this session? (There are no earlier sessions on this car and route, "
           "so don't describe changes over time or substitute other cars, routes or games.)",
           "Give up to three priorities, most important first."]
    return b, files, ask, c["game"]


def lap_brief(analyser, cid, lap, ref, goal, U):
    c, r = analyser.result(cid)
    d = engine.lap_detail(c, r["ref"], lap, ref, r["corner_set"]["frame_file"])
    L, R = d["lap"], d["ref"]
    row = next((x for x in r["laps"] if x["file"] == L["file"]), {})
    rrow = next((x for x in r["laps"] if x["file"] == R["file"]), {})
    corners = []
    ld, rd = L.get("device", "unknown"), R.get("device", "unknown")
    wheelish = ("wheel", "unknown")
    # pedal pressure and steering are only paired when both laps were on the same kind of device
    same = ld == rd or (ld in wheelish and rd in wheelish)

    def peak(m):
        return None if m.get("brake_at") is None or m.get("brake_peak") is None else round(m["brake_peak"] * 100)

    for cn in d["corners"]:
        lm, rm = cn["lap"], cn["ref"]
        corners.append({
            "corner": cn["name"], "from": U.m(cn["a_d"]), "to": U.m(cn["b_d"]), "time_diff_s": cn["dt"],
            "min_speed": [U.kmh(lm["min_speed"]), U.kmh(rm["min_speed"])],
            "brake_at": [U.m(lm.get("brake_at")), U.m(rm.get("brake_at"))],
            "peak_brake_input_pct": [peak(lm), peak(rm) if same else None],
            "full_throttle_at": [U.m(lm.get("full_gas_at")), U.m(rm.get("full_gas_at"))],
            "steering_max_deg": [lm.get("steer_max"), rm.get("steer_max") if same else None],
            "steering_max_pct_of_lock": [_pct(lm.get("steer_max_pct")), _pct(rm.get("steer_max_pct")) if same else None],
            "slide_peak_deg": [lm.get("beta_peak"), rm.get("beta_peak")],
            "gear": [lm.get("gear"), rm.get("gear")],
            "measured_differences": [U.text(t.replace(" (track)", " [track/sim only]")) for t in cn["tips"][goal]],
        })
    vals = dict(L["habits"], **(row.get("totals") or {}))
    whole = []
    for key, label, *_rest in trends.METRICS:
        if key not in vals:
            continue
        if vals[key] is None:
            whole.append({"id": key, "label": label, "value": None, "unit": ""})
        else:
            lab, val, unit, _dec = U.metric(key, vals[key])
            whole.append({"id": key, "label": lab, "value": val, "unit": unit})
    q = row.get("quality") or {}
    b = {"identity": _identity(c), "corner_map": r["corner_set"],
         "lap": {"lap": "L{0}".format(L["number"]), "session": L["session"], "file": L["file"], "time_s": L["time"], "valid": L["valid"],
                 "recorded_valid": L.get("recorded_valid"), "validity": L.get("validity"), "pace_eligible": L.get("pace_eligible"),
                 "input_device": _device(L.get("device", "unknown"), L.get("device_source"), L.get("device_detail")),
                 "standing_start": row.get("standing", False),
                 "incidents": [{"kind": "spin candidate" if i["kind"] == "spin" else "big slowdown", "at": U.m(i["d"]), "corner": i["corner"] or None}
                               for i in row.get("incidents", [])]},
         "reference": {"lap": "L{0}".format(rrow.get("number", "?")), "session": rrow.get("session"), "file": R["file"], "time_s": R["time"],
                       "role": r["reference_role"] if R["file"] == r["ref"] else "chosen by the user; " + (R.get("validity") or {}).get("label", "validity unverified"),
                       "validity": R.get("validity"),
                       "input_device": _device(R.get("device", "unknown")),
                       "standing_start": rrow.get("standing", False)},
         "time_diff_s": round(L["time"] - R["time"], 3),
         "pair_order": "each pair is [this lap, reference]" + ("" if same else
                       "; the reference was driven on a different input device, so its peak brake input and steering are "
                       "left out (null): only distances, speeds and timing are compared"),
         "corners": corners, "whole_lap": whole,
         "quality": {"sample_rate_hz": q.get("hz"), "max_sample_gap_ms": q.get("max_gap_ms"),
                     "speed_vs_position_distance_pct": q.get("distance_check_pct"), "warnings": q.get("warnings", []),
                     "channels_not_recorded": sorted(k for k, v in (q.get("channels") or {}).items() if v in ("missing", "unavailable"))},
         "provenance": r.get("provenance", {})}
    files = {"lap_file": str(engine.DATA_DIR / c["game"] / c["folder"] / L["file"]),
             "reference_file": str(engine.DATA_DIR / c["game"] / c["folder"] / R["file"]),
             "drill_down_command": _cmd(c["game"], c["folder"], L["session"], L["number"], goal)}
    ask = ["Go corner by corner: where does this lap gain or lose time against the reference, and what in the measurements explains it?",
           "Give up to three priorities for next time, most time first."]
    return b, files, ask, c["game"]


def selection_brief(analyser, page, game, combo_ids, since, label, U):
    o = trends.overview(analyser, game=game or None, combo_ids=combo_ids or None, since=since or 0)
    with analyser.lock:
        combos = {c["id"]: c for c in analyser.combos}
        results = dict(analyser.results)
    picked = sorted((combos[x] for x in {p["combo"] for p in o["points"]} if x in combos), key=lambda c: (-c["last"], c["id"]))
    items = []
    for c in picked[:8]:
        pts = [p for p in o["points"] if p["combo"] == c["id"]]
        sess = []
        for p in pts[-4:]:
            m = []
            for key, v in p["values"].items():
                if v is not None and any(x[0] == key for x in trends.METRICS):
                    lab, val, unit, _d = U.metric(key, v)
                    m.append({"id": key, "label": lab, "value": val, "unit": unit})
            sess.append({"id": p["session"], "start": _date(p["start"]), "laps": p["laps"], "best_s": p["best"],
                         "input_device": _device(p.get("device", "unknown")),
                         "typical_s": p["typical"], "per_lap": m})
        items.append({"identity": _identity(c), "sessions_in_selection": len(pts), "best_lap_s": c["best"],
                      "corner_map": (results.get(c["id"]) or {}).get("corner_set", {}).get("id"), "sessions": sess})
    b = {"selection": {"page": page, "game": game or "all", "combo_ids": sorted(combo_ids) if combo_ids else "all",
                       "since": _date(since) if since else "all dates", "label": label,
                       "combos": len(picked), "sessions": len(o["points"]),
                       "listed": min(8, len(picked)), "per_lap_aggregation": "median per lap (handbrake pulls and incidents: mean per lap)"},
         "combos": items}
    multi = any(x["sessions_in_selection"] > 1 for x in items)
    ask = (["Which habits change between sessions on the same car and route, for better or worse?"] if multi else
           ["No car/route here has more than one session in this selection, so don't describe trends over time."]) + [
          "Which measurements look similar or different across cars, routes and games? Only compare like with like and "
          "say when a comparison isn't meaningful (different game, car class or route).",
          "Give up to three priorities, most important first."]
    files = {"lap_folder": str(engine.DATA_DIR)}
    return b, files, ask, game


def build_brief(analyser, page, goal="mix", cid=None, lap=None, ref=None, session=None, game=None,
                combo_ids=None, since=0, label=None):
    goal = goal if goal in GOALS else "mix"
    U = Units()
    if page == "lap" and cid and lap:
        body, files, ask, g = lap_brief(analyser, cid, lap, ref, goal, U)
    elif page == "combo" and cid:
        body, files, ask, g = session_brief(analyser, cid, session, goal, U)
    else:
        body, files, ask, g = selection_brief(analyser, page, game, combo_ids, since, label, U)
    b = _common(page, goal, U, g)
    b.update(body)
    b["files"] = files
    b["questions"] = ask
    b["response_rules"] = [
        "For each priority: name the corner and its distance range, quote at least two numbers from the brief, and keep "
        "what was measured separate from your explanation of why and from the practice you suggest.",
        "Use the numbers as given; don't recompute or convert them. If the data can't support a point, say so instead "
        "of guessing, and don't invent extra priorities."]
    if goal != "race":
        b["response_rules"].append("Mark each suggestion TRACK/SIM ONLY or ROAD-TRANSFERABLE HABIT.")
    return b


# ----------------------------------------------------------------- text rendering

def _corner_line_session(x, U):
    fs, bl = x["fastest_section"], x["best_lap_section"]
    s = "  {0} ({1}-{2} {3}): median gap {4:.2f} s | fastest section {5} {6:.2f} s, min speed {7} {8}".format(
        x["corner"], _num(x["from"]), _num(x["to"]), U.dl, x["gap_s"] or 0, fs["lap"], fs["time_s"] or 0, _num(fs["min_speed"]), U.sl)
    if bl and bl["lap"] != fs["lap"]:
        s += " | best lap {0} {1:.2f} s, min speed {2} {3}".format(bl["lap"], bl["time_s"], _num(bl["min_speed"]), U.sl)
    s += " | median min speed {0} {1} ±{2} | brake at {3}{4} ({5}/{6} laps braked) | peak brake input {7} | steering {8} | gear {9}".format(
        _num(x["median_min_speed"]), U.sl, _num(x["min_speed_sd"]),
        "none (lift)" if x["brake_at"] is None else _num(x["brake_at"], U.dl),
        "" if x["brake_at_sd"] is None else " ±" + _num(x["brake_at_sd"], U.dl), x["braking_laps"], x["n_laps"],
        _num(x["peak_brake_input_pct"], "%"),
        _num(x["steering_max_deg"]) + "°" if x["steering_max_deg"] is not None or x.get("steering_max_pct_of_lock") is None
        else _num(x["steering_max_pct_of_lock"], "%") + " of full lock", x["gear"])
    if x["common_differences_vs_best_lap"]:
        s += " | vs best lap, on most laps: " + "; ".join("{0} ({1}/{2}){3}".format(
            d["text"], d["laps"], d["of"], " [track/sim only]" if d["scope"] == "track/sim only" else "")
            for d in x["common_differences_vs_best_lap"])
    return s


def _quality_lines(q):
    if not q:
        return []
    out = ["Data quality: {0} Hz, largest gap between samples {1} ms, speed-integrated vs position distance {2}%.".format(
        _num(q.get("sample_rate_hz"), "", 1), _num(q.get("max_sample_gap_ms")), _num(q.get("speed_vs_position_distance_pct"), "", 2))]
    if q.get("channels_not_recorded"):
        out.append("Not recorded for these laps (unavailable, not zero): " + ", ".join(q["channels_not_recorded"]) + ".")
    if q.get("channels_constant"):
        out.append("Recorded but never changed: " + ", ".join(q["channels_constant"]) + ".")
    for w in q.get("laps_with_warnings") or []:
        out.append("Warning, {0}: {1}".format(w["lap"], "; ".join(w["warnings"])))
    for w in q.get("warnings") or []:
        out.append("Warning: " + w)
    return out


def render(b, variant="agent"):
    U = Units()
    out = ["I log my driving in sim racing games with Simetrics. Please analyse the measurements below "
           "(brief {0} v{1}).".format(b["format"], b["version"]), "", b["goal_instruction"], ""]
    if "identity" in b:
        i = b["identity"]
        out.append("Game: {0} ({1}). Car: {2} (id {3}). Route: {4} (id {5}). Combo id: {6}.".format(
            i["game_name"], i["game"], i["car_label"], i["car_id"], i["route_label"], i["route_id"], i["combo_id"]))
        lines = engine.la.hook(i["game"], "brief_identity_lines")
        out += lines(i) if lines else []
        for w in i.get("warnings", []):
            out.append("Warning: " + w)
        cm = b["corner_map"]
        out.append("Corner map {0}: distances and corners from {1} ({2}); {3}.".format(cm["id"], cm["frame_lap"], cm["frame_file"], cm["method"]))
    if b["page"] == "combo":
        s, h, cp = b["session"], b["history"], b["comparison"]
        out += ["", "Session {0} ({1}): {2} recorded laps, {4} standing start(s), {5} without detected major incidents. Input device: {6}.".format(
                    s["id"], s["start"], s["laps"], s["valid"], s["standing_starts"], s["clean"],
                    ", ".join("{0} ({1} laps)".format(d["label"].lower(), d["laps"]) for d in s["input_devices"])
                    if len(s["input_devices"]) > 1 else s["input_device"]["label"].lower()),
                "Track status: {0} observed-clear / rewind-free (not game certification), {1} unverified, {2} game invalid/rewind, {3} observed off-track, {4} penalty. Off-track and penalty counts can overlap with game-invalid laps.".format(
                    s["valid"], s["unverified"], s["invalid"], s["off_track"], s["penalty"]),
                "History: {0} matching session(s) on this car and route; {1} earlier.{2}".format(
                    h["matching_sessions"], h["earlier_sessions"],
                    "" if h["between_session_comparison_available"] else " Between-session comparison is NOT possible."),
                "Times: session best {0}; typical {1} ({2}); reference lap {3} {4}.".format(
                    _t(s["best_s"]), _t(s["typical_s"]), s["typical_definition"], b["reference"]["best_lap"]["lap"], _t(b["reference"]["best_lap"]["time_s"])),
                "Best scope: {0}. Reference: {1}.".format(s["best_role"], b["reference"]["best_lap"]["role"]),
                "Theoretical {0}: {1}.".format(_t(s["theoretical_s"]), s["theoretical_note"]),
                "Comparison laps: {0} ({1}){2}. Gap = {3}.".format(
                    ", ".join(cp["laps"]), cp["population"],
                    "; excluded " + ", ".join("{0} ({1})".format(e["lap"], e["reason"]) for e in cp["excluded"]) if cp["excluded"] else "",
                    cp["gap_definition"]),
                "", "Corners ({0} {1} {2}):".format(_num(b["track"]["length"]), U.dl, "lap" if b["track"]["closed"] else "run")]
        out += [_corner_line_session(x, U) for x in b["corners"]]
        out += ["", "Per lap in this session (all laps; median, except handbrake pulls and incidents: mean):"]
        out += ["  {0}: {1}".format(m["label"], _num(m["value"], m["unit"], 2 if m["unit"] in ("s/lap", "per lap") else 0)) for m in b["per_lap"]]
        if h["earlier"]:
            out += ["", "Earlier sessions on this car and route:"] + [
                "  {0} ({1}): {2} laps, best {3}, typical {4}".format(e["id"], e["start"], e["laps"], _t(e["best_s"]), _t(e["typical_s"])) for e in h["earlier"]]
    elif b["page"] == "lap":
        L, R = b["lap"], b["reference"]
        out += ["", "Lap {0} of session {1}: {2}{3}{4}. File {5}. Input device: {6}{7}.".format(
                    L["lap"], L["session"], _t(L["time_s"]), ", " + (L.get("validity") or {}).get("label", "validity unverified"), ", standing start" if L["standing_start"] else "", L["file"],
                    L["input_device"]["label"].lower(), " ({0})".format(L["input_device"]["detail"]) if L["input_device"].get("detail") else ""),
                "Reference {0} of session {1}: {2} ({3}){4}. File {5}. Input device: {6}. Difference {7:+.3f} s.".format(
                    R["lap"], R["session"], _t(R["time_s"]), R["role"], ", standing start" if R["standing_start"] else "", R["file"],
                    R["input_device"]["label"].lower(), b["time_diff_s"]),
                "", "Corners ({0}):".format(b["pair_order"])]
        for x in b["corners"]:
            bits = ["  {0} ({1}-{2} {3}): {4:+.2f} s".format(x["corner"], _num(x["from"]), _num(x["to"]), U.dl, x["time_diff_s"] or 0),
                    "min speed {0}/{1} {2}".format(_num(x["min_speed"][0]), _num(x["min_speed"][1]), U.sl),
                    "brake at {0}/{1}".format(*("none" if v is None else _num(v, U.dl) for v in x["brake_at"])),
                    "peak brake input {0}/{1}".format(*("-" if v is None else "{0}%".format(v) for v in x["peak_brake_input_pct"])),
                    "full throttle at {0}/{1}".format(*(_num(v, U.dl) for v in x["full_throttle_at"])),
                    "steering {0}/{1}°".format(*(_num(v) for v in x["steering_max_deg"])) if None not in x["steering_max_deg"]
                    else "steering {0}/{1} of full lock".format(*(_num(v, "%") for v in x["steering_max_pct_of_lock"]))]
            if x["slide_peak_deg"][0] is not None:
                bits.append("slide {0}/{1}°".format(*(_num(v) for v in x["slide_peak_deg"])))
            bits.append("gear {0}/{1}".format(*x["gear"]))
            if x["measured_differences"]:
                bits.append("measured differences: " + "; ".join(x["measured_differences"]))
            out.append(" | ".join(bits))
        out += ["", "Whole lap:"] + ["  {0}: {1}".format(m["label"], _num(m["value"], m["unit"], 2 if m["unit"] == "s/lap" else 0)) for m in b["whole_lap"]]
        if L["incidents"]:
            out.append("  Incidents: " + ", ".join("{0} at {1} {2} ({3})".format(i["kind"], _num(i["at"]), U.dl, i["corner"] or "-") for i in L["incidents"]))
    else:
        sel = b["selection"]
        out.append("Selection: {0}; {1}; {2} car/track combination(s), {3} sessions{4}.".format(
            sel["game"], sel["since"], sel["combos"], sel["sessions"], " ({0})".format(sel["label"]) if sel.get("label") else ""))
        for cmb in b["combos"]:
            i = cmb["identity"]
            out += ["", "{0} @ {1} ({2}; combo id {3}; corner map {4}): {5} session(s), best lap {6}.".format(
                i["car_label"], i["route_label"], i["game_name"], i["combo_id"], cmb["corner_map"], cmb["sessions_in_selection"], _t(cmb["best_lap_s"]))]
            for s in cmb["sessions"]:
                out.append("  Session {0} ({1}, {2}): {3} laps, best {4}, typical {5}".format(
                    s["id"], s["start"], s["input_device"]["label"].lower(), s["laps"], _t(s["best_s"]), _t(s["typical_s"])))
                out += ["    {0}: {1}".format(m["label"], _num(m["value"], m["unit"], 2 if m["unit"] in ("s/lap", "per lap") else 0)) for m in s["per_lap"]]
        if sel["combos"] > sel["listed"]:
            out.append("(and {0} more car/track combinations not listed)".format(sel["combos"] - sel["listed"]))
        out.append("Per-lap values are {0}.".format(sel["per_lap_aggregation"]))
    out += [""] + _quality_lines(b.get("quality"))
    out += ["", "How the numbers are measured:"] + ["- " + d for d in b["definitions"]]
    for k, v in (b.get("provenance") or {}).items():
        out.append("- {0}: {1}".format(k, v))
    out += ["- Units: speeds in {0}, distances in {1}.".format(b["units"]["speed"], "feet" if b["units"]["distance"] == "ft" else "metres"),
            "- Unknown: " + "; ".join(b["unknown"]) + "."]
    if variant == "agent":
        out += ["", "If you need more detail (optional: everything above is already computed):"]
        out += ["- {0}: {1}".format(k.replace("_", " "), v) for k, v in b["files"].items()]
        out.append("- Column definitions: {0}".format(engine.STUDIO_DIR / "analysis" / "LAP_CSV_SCHEMA.md"))
    else:
        out += ["", "If you need more detail, ask me to upload a lap file."]
    out += ["", "What I'd like:"] + ["- " + q for q in b["questions"]] + ["- " + r for r in b["response_rules"]]
    return "\n".join(out)


def build(analyser, page, goal="mix", variant="agent", cid=None, lap=None, ref=None, session=None, game=None,
          combo_ids=None, since=0, label=None):
    return render(build_brief(analyser, page, goal, cid, lap, ref, session, game, combo_ids, since, label), variant)


def brief_json(*args, **kw):
    return json.dumps(build_brief(*args, **kw), indent=1, ensure_ascii=False)
