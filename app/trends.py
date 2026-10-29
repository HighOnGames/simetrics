"""Session-by-session measurements across games, and the biggest gaps and changes.

Data only: no thresholds, verdicts or advice. Whether a number is good depends on the driver,
the car and the goal, so interpretation is left to the driver or to an AI agent (see prompts.py).
Every measurement has the same definition in every game.
"""

import statistics

import engine
from engine import _r

# key, label, unit, decimals, scale, group, definition (what is measured, never what to do)
METRICS = [
    ("two_feet_s", "Pedal overlap (brake + throttle)", "s/lap", 2, 1, "Pedals",
     "Time per lap with brake and throttle both above 10% (clutch up). Which foot was used is unknown; on a controller "
     "or keyboard it is two triggers or keys."),
    ("clutch_coast_s", "Clutch down while rolling", "s/lap", 2, 1, "Pedals",
     "Time per lap in spells longer than 0.5 s with the clutch pedal down above 16 km/h."),
    ("neutral_coast_s", "Rolling in neutral", "s/lap", 2, 1, "Pedals",
     "Time per lap in spells longer than 0.5 s in neutral above 16 km/h."),
    ("handbrake_pulls", "Handbrake pulls", "per lap", 1, 1, "Pedals",
     " ".join(x for x in ("Handbrake applications while moving.", engine.la.notes("handbrake")) if x)),
    ("brake_peak", "Peak brake input", "%", 0, 100, "Braking",
     "Median over the braking zones of the highest brake pedal position (pedal input, not brake pressure). "
     "Not measured on a keyboard (keys are on/off)."),
    ("brake_sd", "Brake point spread", "m", 1, 1, "Braking",
     "How far the brake point moves from lap to lap (standard deviation, median over corners)."),
    ("incidents", "Spin candidates and big slowdowns", "per lap", 2, 1, "Control",
     "Slide angle over 60° (spin candidate), or under 45% of the reference lap's speed at the same spot and at "
     "least 40 km/h down. Neither proves a full spin or a collision."),
    ("slide_m", "Distance sliding", "m/lap", 0, 1, "Control",
     "Metres per lap with a slide angle over 10° (the direction of travel vs where the nose points)."),
    ("coast_pct", "Off both pedals", "% of lap", 0, 1, "Pedals",
     "Share of the lap distance with throttle under 10% and footbrake under 5% (handbrake not counted), above 20 km/h."),
    ("full_pct", "Full throttle", "% of lap", 0, 1, "Pedals",
     "Share of the lap distance with the throttle at 90% or more."),
    ("shifts", "Gear changes", "per lap", 0, 1, "Gears",
     "Gear changes per lap (neutral blips under 0.15 s ignored)."),
    ("pace_pct", "Typical lap vs your best", "% off best", 1, 1, "Pace",
     "Median rolling lap without detected major incidents or continuity failures, compared with your eligible best. Track-validity scope is reported separately."),
]


# short names for lap_analysis.advice_items ids: what was measured vs the reference lap
TIP_PHRASES = {
    "brake_early": "brakes earlier", "brake_late": "brakes later",
    "brake_late_overslow": "brakes later, lower min speed", "brake_not_needed": "brakes where the best lap only lifts",
    "apex_slow": "lower min speed", "apex_fast": "higher min speed",
    "brake_soft": "lower peak brake input", "gas_late": "full throttle later", "gas_never_full": "no full throttle on exit",
    "coast_more": "more coasting", "exit_slow": "slower exit", "rear_slide": "more rear slip",
    "steer_corrections": "more steering input variation", "big_slide": "bigger slide",
    "clutch_coast": "clutch down into the corner", "cut": "game invalid flag / rewind recorded",
    "off_track": "more than two tyres outside track observed",
}


def metric_defs():
    return [{"key": k, "label": l, "unit": u, "dec": d, "scale": s, "group": g, "def": df}
            for k, l, u, d, s, g, df in METRICS]


def overview(analyser, game=None, combo_ids=None, since=0):
    """One point per session per car@track for every metric, plus gaps and changes."""
    with analyser.lock:
        combos = {c["id"]: c for c in analyser.combos}
        results = dict(analyser.results)
    pick = [cid for cid in combos if cid in results
            and (not game or combos[cid]["game"] == game)
            and (not combo_ids or cid in combo_ids)]
    points = []
    for cid in pick:
        c, r = combos[cid], results[cid]
        for s in r["sessions"]:
            if s["start"] < since:
                continue
            h = dict(s["habits"])
            h["pace_pct"] = _r(100.0 * (s["typical"] / r["best"] - 1), 2) if s["typical"] and r["best"] else None
            points.append({"combo": cid, "game": c["game"], "session": s["session"], "start": s["start"],
                           "device": s.get("device", "unknown"),
                           "laps": s["laps"], "best": s["best"], "typical": s["typical"], "values": h})
    points.sort(key=lambda p: p["start"])
    return {"metrics": metric_defs(), "points": points,
            "changes": changes(points),
            "gaps": corner_gaps(points, results),
            "key_corners": key_corners(pick, results, since)}


def changes(points):
    """Per car@track: each measurement in its latest session vs the session before it (same car@track,
    so the comparison is like for like). Sorted by the size of the relative change."""
    out = []
    for cid in {p["combo"] for p in points}:
        ps = [p for p in points if p["combo"] == cid]
        if len(ps) < 2:
            continue
        cur, prev = ps[-1], ps[-2]
        for k, label, unit, dec, scale, _g, _d in METRICS:
            a, b = prev["values"].get(k), cur["values"].get(k)
            if a is None or b is None or round(a * scale, dec) == round(b * scale, dec):
                continue
            rel = abs(b - a) / max(abs(a), abs(b), 1e-9)
            out.append({"combo": cid, "metric": k, "before": a, "now": b, "rel": round(rel, 3),
                        "session": cur["session"], "prev_session": prev["session"]})
        if cur["typical"] and prev["typical"]:
            out.append({"combo": cid, "metric": "typical_lap", "before": prev["typical"], "now": cur["typical"],
                        "rel": round(abs(cur["typical"] - prev["typical"]) / prev["typical"] * 20, 3),
                        "session": cur["session"], "prev_session": prev["session"]})
    out.sort(key=lambda x: -x["rel"])
    return out[:14]


def corner_gaps(points, results):
    """Latest session of the three most recently driven car@tracks: the corners with the biggest
    median time gap to your own best time through them, with the most common measured
    differences from lap_analysis (e.g. "braking 12 m earlier") counted over the session's laps."""
    seen = []
    for p in reversed(points):
        if p["combo"] not in seen:
            seen.append(p["combo"])
        if len(seen) == 3:
            break
    out = []
    for cid in seen:
        r = results[cid]
        last = max(p["start"] for p in points if p["combo"] == cid)
        s = next((s for s in r["sessions"] if s["start"] == last), r["sessions"][-1])
        population = set(s.get("population", {}).get("files", []))
        laps = [l for l in r["laps"] if l["session"] == s["session"] and l["file"] in population]
        rows = []
        for k, sc in enumerate(s["corners"]):
            counts = {}
            for l in laps:
                for tid, _scope in (l["tips"][k] if k < len(l["tips"]) else []):
                    counts[tid] = counts.get(tid, 0) + 1
            rows.append({"corner": r["corners"][k]["name"], "k": k, "loss": sc["loss"] or 0,
                         "diffs": sorted(([t, n] for t, n in counts.items() if n >= max(2, len(laps) * 0.4)), key=lambda x: -x[1])[:3]})
        rows.sort(key=lambda x: -x["loss"])
        out.append({"combo": cid, "session": s["session"], "laps": len(laps), "only": len(r["laps"]) == 1,
                    "total_gap": _r(sum(x["loss"] for x in rows), 2), "corners": [x for x in rows[:3] if x["loss"] > 0.05]})
    return out


def key_corners(pick, results, since):
    """Per car@track: the 3 corners with the biggest time gap over the filtered sessions, with
    steering lock, brake point spread, gap and apex speed per session."""
    out = []
    for cid in pick:
        r = results[cid]
        sess = [s for s in r["sessions"] if s["start"] >= since and s["best"] is not None]
        if not sess or not r["corners"]:
            continue
        avg = [statistics.mean([s["corners"][k]["loss"] or 0 for s in sess]) for k in range(len(r["corners"]))]
        for k in sorted(sorted(range(len(avg)), key=lambda k: -avg[k])[:3]):
            # steer_max is None for controller / keyboard sessions (no wheel angle); steer_max_pct is % of full lock
            out.append({"combo": cid, "corner": r["corners"][k]["name"], "ref_steer": r["corners"][k]["ref"].get("steer_max"),
                        "series": [{"session": s["session"], "start": s["start"], "device": s.get("device", "unknown"),
                                    "steer_max": s["corners"][k]["steer_max"], "steer_max_pct": s["corners"][k].get("steer_max_pct"),
                                    "brake_sd": s["corners"][k]["brake_sd"], "loss": s["corners"][k]["loss"],
                                    "min_mph": s["corners"][k]["min_mph"]} for s in sess]})
    return out
