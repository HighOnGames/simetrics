"""Validity/continuity regressions. Writes only isolated temporary data under this checkout.

Optional real-data regression: set LAPLOGGER_TEST_DATA to a read-only source data/ folder.
The test copies the Audi fixture before analysing it; original logs and caches are never written.
"""
import contextlib
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO / "app"))
import engine
import prompts
import server

la = engine.la
MONITORED = {"game_validity_source": "ac.LapInvalidated", "game_validity_coverage": "complete",
             "track_limits_source": "ac.NumberOfTyresOut", "track_limits_coverage": "complete",
             "track_limits_rule": "more_than_2_tyres", "off_track_events": "0", "off_track_s": "0"}


def lap(number=1, seconds=60, monitored=True, **changes):
    meta = {"game": "ac", "valid": "1", "lap_number": str(number), "session": "261029-120000",
            "lap_time_ms": str(round(seconds * 1000)), "track_length_m": "2600.3"}
    if monitored:
        meta.update(MONITORED)
    meta.update(changes)
    cols = {"t_ms": [0, 100], "pos": [0, 0.001], "speed": [36, 36], "x": [0, 1], "z": [0, 0],
            "invalid": [0, 0], "game_invalid": [0, 0], "track_limits_exceeded": [0, 0], "tyres_out": [0, 0]}
    return la.Lap("fixture_L{0:02d}.csv".format(number), meta, cols)


@contextlib.contextmanager
def isolated_data(root):
    old = {name: getattr(engine, name) for name in ("DATA_DIR", "STATE_DIR", "CACHE_DIR", "IMAGES_DIR", "LIBRARY_FILE", "SETTINGS_FILE")}
    old_la = la.STUDIO_DIR
    old_settings = copy.deepcopy(engine.SETTINGS)
    engine.DATA_DIR = root / "data"
    engine.STATE_DIR = engine.DATA_DIR / "studio"
    engine.CACHE_DIR = engine.STATE_DIR / "cache"
    engine.IMAGES_DIR = engine.STATE_DIR / "images"
    engine.LIBRARY_FILE = engine.STATE_DIR / "library.json"
    engine.SETTINGS_FILE = engine.STATE_DIR / "settings.json"
    engine.STATE_DIR.mkdir(parents=True, exist_ok=True)
    la.STUDIO_DIR = root
    engine._grid_cache.clear()
    engine._lap_cache.clear()
    try:
        yield
    finally:
        for name, value in old.items():
            setattr(engine, name, value)
        la.STUDIO_DIR = old_la
        engine.SETTINGS.clear()
        engine.SETTINGS.update(old_settings)
        engine._grid_cache.clear()
        engine._lap_cache.clear()


class ValidityTests(unittest.TestCase):
    def test_monitored_means_observed_clear_not_certified(self):
        value = lap()
        self.assertIs(value.valid, True)
        self.assertEqual(value.validity["status"], "observed_clear")
        self.assertIn("not game certification", value.validity["label"])

    def test_legacy_and_missing_header_are_unknown(self):
        self.assertIsNone(lap(monitored=False).valid)
        self.assertIs(lap(monitored=False).recorded_valid, True)
        self.assertIsNone(lap(monitored=False, valid="unknown").valid)

    def test_both_sources_need_complete_coverage(self):
        for change in ({"game_validity_coverage": "partial"}, {"track_limits_coverage": "partial"},
                       {"game_validity_source": "unavailable"}, {"track_limits_source": "unavailable"}):
            self.assertIsNone(lap(**change).valid)
        value = lap(track_limits_source="unavailable", track_limits_coverage="unavailable")
        self.assertIsNone(value.validity["off_track_events"])
        self.assertIsNone(value.validity["off_track"])

    def test_invalid_sample_overrides_positive_header(self):
        original = lap()
        cols = copy.deepcopy(original.cols)
        cols["invalid"] = [0, 1]
        value = la.Lap(original.path, original.meta, cols)
        self.assertIs(value.recorded_valid, True)
        self.assertIs(value.valid, False)
        self.assertEqual(value.validity["status"], "invalid")

    def test_native_header_invalid_and_independent_offtrack_are_distinct(self):
        self.assertEqual(lap(valid="0").validity["status"], "invalid")
        value = lap(off_track_events="1")
        self.assertEqual(value.validity["status"], "off_track")
        self.assertIs(value.validity["game_invalid"], False)
        self.assertFalse(value.pace_eligible)

    def test_pit_counts_do_not_override_explicit_suppressed_latch(self):
        original = lap()
        cols = copy.deepcopy(original.cols)
        cols["tyres_out"] = [4, 4]
        value = la.Lap(original.path, original.meta, cols)
        self.assertEqual(value.validity["status"], "observed_clear")
        cols["track_limits_exceeded"][-1] = 1
        value = la.Lap(original.path, original.meta, cols)
        self.assertEqual(value.validity["status"], "off_track")

    def test_penalty_is_separate_from_game_invalidation(self):
        value = lap(penalty_seen="1")
        self.assertEqual(value.validity["status"], "penalty")
        self.assertIs(value.validity["game_invalid"], False)
        self.assertFalse(value.pace_eligible)

    def test_raw_penalty_flag_overrides_a_clear_header(self):
        original = lap(penalty_seen="0")
        cols = copy.deepcopy(original.cols)
        cols["flag"] = [0, 6]
        value = la.Lap(original.path, original.meta, cols)
        self.assertEqual(value.validity["status"], "penalty")
        self.assertFalse(value.pace_eligible)

    def test_native_invalid_offtrack_and_penalty_all_remain_counted(self):
        value = lap(valid="0", off_track_events="2", off_track_s="1.25", penalty_seen="1")
        self.assertEqual(value.validity["status"], "invalid")
        self.assertTrue(value.validity["off_track"])
        self.assertTrue(value.validity["penalty_observed"])
        counts = engine._status_counts([value])
        self.assertEqual((counts["invalid"], counts["off_track"], counts["penalty"]), (1, 1, 1))
        self.assertEqual(value.validity["off_track_events"], 2)
        self.assertEqual(value.validity["off_track_s"], 1.25)

    def test_forza_is_only_rewind_free(self):
        value = lap(monitored=False, game="fh6", rewinds="0")
        self.assertEqual(value.validity["status"], "rewind_free")
        self.assertIn("track limits unverified", value.validity["label"])
        self.assertEqual(lap(monitored=False, game="fh6", rewinds="1").validity["status"], "invalid")

    def test_reset_is_excluded_before_resampling(self):
        original = lap(seconds=1)
        cols = copy.deepcopy(original.cols)
        cols["x"][-1] = 105
        reset = la.Lap(original.path, original.meta, cols)
        self.assertFalse(reset.pace_eligible)
        self.assertEqual(len(reset.continuity["position_discontinuities"]), 1)
        normal = lap(number=2, seconds=60)
        self.assertIs(la.select_reference([reset, normal]), normal)
        self.assertEqual(la.comparison_laps([reset, normal])[0], [normal])

    def test_reference_prefers_observed_clear_then_explicit_unknown_fallback(self):
        unknown = lap(number=1, seconds=40, monitored=False)
        clear = lap(number=2, seconds=60)
        invalid = lap(number=3, seconds=20, valid="0")
        self.assertIs(la.select_reference([unknown, clear, invalid]), clear)
        self.assertEqual(la.comparison_laps([unknown, clear, invalid])[0], [clear])
        self.assertIs(la.select_reference([unknown, invalid]), unknown)
        self.assertIn("unverified", la.comparison_laps([unknown, invalid])[1])
        self.assertIn("diagnostics only", la.comparison_laps([invalid])[1])
        self.assertIsNone(engine._best_time([invalid]))
        self.assertIn("diagnostic", la.reference_role(invalid))

    def test_typical_population_cannot_include_slowdown_or_reset(self):
        clear = lap(number=1)
        slow = lap(number=2)
        rows = {clear.path.name: {"clean": True}, slow.path.name: {"clean": False}}
        selected, label = engine._typical_population([clear, slow], rows)
        self.assertEqual(selected, [clear])
        self.assertIn("without detected major incidents", label)

    def test_library_index_uses_analysed_best_and_validity_not_header_best(self):
        original = server.analyser
        result = {"outline": [], "closed": True, "typical": 62.0, "theoretical": 59.0, "best": 60.0,
                  "reference_role": "fastest recorded lap; track validity unverified", "valid": 0,
                  "invalid": 0, "off_track": 0, "penalty": 0, "unverified": 1}
        server.analyser = SimpleNamespace(lock=threading.Lock(), combos=[{"id": "ac~fixture", "best": 1.0, "valid": 1}],
                                          results={"ac~fixture": result}, status={"state": "ready"})
        try:
            combo = server.index_payload()["combos"][0]
            self.assertEqual(combo["best"], 60.0)
            self.assertEqual(combo["reference_role"], result["reference_role"])
            self.assertEqual(combo["valid"], 0)
            self.assertEqual(combo["unverified"], 1)
        finally:
            server.analyser = original


@unittest.skipUnless(os.environ.get("LAPLOGGER_TEST_DATA"), "set LAPLOGGER_TEST_DATA for copied real-data regression")
class CopiedAudiRegression(unittest.TestCase):
    def test_old_audi_laps_are_unverified_and_reset_is_excluded(self):
        source = Path(os.environ["LAPLOGGER_TEST_DATA"])
        folder = "ks_audi_r18_etron_quattro@ks_silverstone-national"
        target_parent = STUDIO / "data" / "quality-tests"
        target_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target_parent) as temp:
            root = Path(temp)
            with isolated_data(root):
                dest = engine.DATA_DIR / "ac" / folder
                dest.mkdir(parents=True)
                files = sorted((source / "ac" / folder).glob("261029-094128*.csv"))
                self.assertEqual(len(files), 16)
                for path in files:
                    shutil.copy2(path, dest / path.name)
                pins = json.loads((source / "studio" / "corner_maps.json").read_text(encoding="utf-8"))
                cid = "ac~" + folder
                (engine.STATE_DIR / "corner_maps.json").write_text(json.dumps({cid: pins[cid]}), encoding="utf-8")
                combo = next(c for c in engine.scan() if c["id"] == cid)
                result = engine.analyse_combo(combo)
                session = result["sessions"][0]
                self.assertEqual((session["laps"], session["valid"], session["unverified"], session["clean"]), (16, 0, 16, 9))
                self.assertEqual(result["best"], 54.136)
                self.assertIn("unverified", result["reference_role"])
                reset = next(r for r in result["laps"] if r["number"] == 7)
                self.assertFalse(reset["pace_eligible"])
                self.assertFalse(reset["clean"])
                self.assertEqual(reset["quality"]["repeated_timestamps"], 57)
                self.assertTrue(reset["quality"]["position_discontinuities"])
                self.assertTrue(all(row["valid"] is None and row["recorded_valid"] is True for row in result["laps"]))
                class Cached:
                    def result(self, requested):
                        self_requested = requested
                        assert self_requested == cid
                        return combo, result
                brief = prompts.build_brief(Cached(), "combo", "mix", cid, session="261029-094128")
                text = prompts.render(brief)
                self.assertIn("16 unverified", text)
                self.assertIn("9 without detected major incidents", text)
                self.assertNotIn("16 clean", text)
                self.assertNotIn("fastest valid lap", text)
                self.assertEqual(brief["session"]["without_detected_major_incidents"], 9)
                cli = subprocess.run([sys.executable, "-B", str(STUDIO / "analysis" / "lap_analysis.py"), "--root", str(engine.DATA_DIR / "ac"),
                                      "--game", "ac", "report", "--combo", folder, "--session", "261029-094128", "--json"],
                                     capture_output=True, text=True, check=True)
                report = json.loads(cli.stdout)
                self.assertEqual(report["reference"], result["ref"])
                self.assertEqual(report["reference_role"], result["reference_role"])
                self.assertNotIn(reset["file"], report["comparison"]["laps"])
                self.assertEqual(sorted(report["comparison"]["laps"]), sorted(session["population"]["files"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
