"""Acceptance checks for the analysis briefs (from the 2026-10-28 ChatGPT review). Run:

    py -3.12 app/check_briefs.py

Uses the real laps in data/. Exits non-zero if a check fails.
"""

import json
import subprocess
import sys
import time

import engine
import prompts

fails = []


def check(name, ok, detail=""):
    print("{0} {1}{2}".format("PASS" if ok else "FAIL", name, (": " + detail) if detail and not ok else ""))
    if not ok:
        fails.append(name)


a = engine.Analyser()
time.sleep(1)
while a.status["state"] != "ready":
    time.sleep(0.5)

SIL = "fh6~car2870@silverstone"
if SIL in {c["id"] for c in a.combos}:
    b = prompts.build_brief(a, "combo", "mix", SIL)
    s, h = b["session"], b["history"]
    check("history: 1 matching session, 0 earlier", (h["matching_sessions"], h["earlier_sessions"]) == (1, 0))
    check("no between-session question", "earlier sessions listed" not in " ".join(b["questions"]))
    check("7 laps, 7 rewind-free, 1 standing, none without major incidents", (s["laps"], s["valid"], s["standing_starts"], s["clean"]) == (7, 7, 1, 0), str(s))
    check("comparison = 6 rolling valid laps, L1 excluded as standing start",
          len(b["comparison"]["laps"]) == 6 and b["comparison"]["excluded"] == [{"file": "261027-170340_L01_2-27-929.csv", "lap": "L1", "reason": "standing start"}])
    gaps = {x["corner"]: x["gap_s"] for x in b["corners"]}
    check("gaps T6 1.576, T7 1.028, T8 1.015", (gaps["T6"], gaps["T7"], gaps["T8"]) == (1.576, 1.028, 1.015), str(gaps))
    t6 = next(x for x in b["corners"] if x["corner"] == "T6")
    check("T6 fastest section L4, best lap L7, distinct times",
          t6["fastest_section"]["lap"] == "L4" and t6["best_lap_section"]["lap"] == "L7"
          and t6["fastest_section"]["time_s"] != t6["best_lap_section"]["time_s"], json.dumps(t6))
    check("theoretical labelled illustrative", "illustrative" in s["theoretical_note"])
    check("corner map id present", b["corner_map"]["id"].startswith(SIL + "#v"))

    # CLI and app agree (same population, same aggregation, same corner map)
    out = subprocess.run([sys.executable, str(engine.STUDIO_DIR / "analysis" / "lap_analysis.py"), "--root", str(engine.DATA_DIR / "fh6"),
                          "--game", "fh6", "report", "--combo", "car2870@silverstone", "--session", "261027-170340", "--json"],
                         capture_output=True, text=True, encoding="utf-8")
    cli = json.loads(out.stdout)
    same = all(abs(c["gap_s"] - x["gap_s"]) < 0.0015 and c["fastest_lap"] == x["fastest_section"]["file"]
               for c, x in zip(cli["corners"], b["corners"]))
    check("CLI --json matches the app brief (gaps, fastest laps)", same and len(cli["corners"]) == len(b["corners"]))
    check("CLI theoretical matches", abs(cli["theoretical_s"] - s["theoretical_s"]) < 0.002)
    check("CLI uses the pinned corner map", cli["corner_frame"]["file"] == b["corner_map"]["frame_file"])

    road = prompts.build_brief(a, "combo", "road", SIL)
    check("road goal: no track/sim-only differences in a session brief",
          not any(d["scope"] == "track/sim only" for x in road["corners"] for d in x["common_differences_vs_best_lap"]))
    c, r = a.result(SIL)
    lb = prompts.build_brief(a, "lap", "road", SIL, r["laps"][4]["file"])
    check("road goal: no track-only differences in a lap brief",
          not any("[track" in t for x in lb["corners"] for t in x["measured_differences"]))
    check("missing channels reported as not recorded", "yaw_rate" in lb["quality"]["channels_not_recorded"])
    check("pedal overlap claims no foot", "foot" in " ".join(prompts.DEFINITIONS) and "left-foot" not in prompts.render(lb).lower())

    # input device: recorded per lap, the same in the app, the briefs and the CLI
    check("session brief names the input device", s["input_device"]["device"] == cli["input_device"] == r["sessions"][-1]["device"],
          "{0} / {1}".format(s["input_device"], cli["input_device"]))
    check("lap brief names the lap's and the reference's device",
          lb["lap"]["input_device"]["device"] in engine.la.DEVICES and lb["reference"]["input_device"]["device"] in engine.la.DEVICES)
    check("input device in the prompt text", "Input device: " + s["input_device"]["label"].lower() in prompts.render(b))

# every lap and combo carries a known device; combos list them most laps first
check("every analysed lap has an input device",
      all(l["device"] in engine.la.DEVICES for r in a.results.values() for l in r["laps"]))
check("every combo lists its devices", all(c["devices"] and c["device"] == c["devices"][0]["device"] for c in a.combos))
if any(c["game"] == "ac" for c in a.combos):
    check("AC laps so far: wheel (Logitech G920, tagged)", all(c["device"] == "wheel" for c in a.combos if c["game"] == "ac"))

# device-aware analysis on synthetic wheel / gamepad / keyboard laps
td = subprocess.run([sys.executable, str(engine.STUDIO_DIR / "analysis" / "test_devices.py")], capture_output=True, text=True, encoding="utf-8")
check("synthetic device checks (analysis/test_devices.py)", td.returncode == 0, td.stdout[-600:])
tm = subprocess.run([sys.executable, str(engine.STUDIO_DIR / "analysis" / "test_modules.py")], capture_output=True, text=True, encoding="utf-8")
check("game modules load, fail safely and can be removed (analysis/test_modules.py)", tm.returncode == 0, tm.stderr[-600:])
tmm = subprocess.run([sys.executable, str(engine.STUDIO_DIR / "app" / "test_modman.py")], capture_output=True, text=True, encoding="utf-8")
check("modules install, update, roll back and refuse bad packages (app/test_modman.py)", tmm.returncode == 0, tmm.stderr[-600:])
check("ac and fh6 modules installed, none failed", list(engine.GAMES)[:2] == ["ac", "fh6"] and not engine.la.MODULE_ERRORS, str(engine.la.MODULE_ERRORS))

# invalid-only data: explicit fallback, no silent "best valid"
for cid in ("fh6~car2470@sprint-2",):
    if cid in a.results:
        b = prompts.build_brief(a, "combo", "mix", cid)
        check("invalid-only session states its diagnostic fallback population", b["comparison"]["population"] == "all laps for diagnostics only (none is pace-eligible)",
              b["comparison"]["population"])

# filter fidelity: the selection brief covers exactly the chosen combos and dates
ids = {c["id"] for c in a.combos if c["game"] == "fh6"}
pick = set(sorted(ids)[:2])
if pick:
    sb = prompts.build_brief(a, "habits", "mix", game="fh6", combo_ids=pick, since=1)
    check("selection keeps combo filter", {x["identity"]["combo_id"] for x in sb["combos"]} == pick and sb["selection"]["combo_ids"] == sorted(pick))
else:
    print("SKIP selection combo filter: no FH6 fixture")
since = time.time() - 3600
sb2 = prompts.build_brief(a, "library", "mix", since=since)
check("selection keeps date filter", sb2["selection"]["sessions"] == 0 or all(
    p["start"] and p["start"] >= time.strftime("%Y-%m-%d %H:%M", time.localtime(since)) for x in sb2["combos"] for p in x["sessions"]))

# same display name across games stays separate
names = {}
for c in a.combos:
    names.setdefault(c["track_name"].lower(), set()).add(c["id"])
check("combo ids are game-qualified", all(cid.split("~")[0] in engine.GAMES for v in names.values() for cid in v))

print("\n{0} check(s) failed".format(len(fails)) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
