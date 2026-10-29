"""Regression checks for the AC embedded logger without launching or changing the game."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

LOGGER = Path(__file__).resolve().parents[1] / "logger" / "LapLogger" / "LapLogger.py"


class LoggerTests(unittest.TestCase):
    def setUp(self):
        self.state = {"lap": 0, "time": 59000, "invalid": 0, "tyres": 0}
        self.saved = []
        names = ("LapCount LapTime LastLap LapInvalidated WorldPosition AccG SlipAngle SlipRatio "
                 "NormalizedSplinePosition SpeedKMH SpeedMPH Gas Brake Clutch Steer Gear RPM "
                 "LocalVelocity LocalAngularVelocity NdSlip CurrentTyresCoreTemp SuspensionTravel").split()
        cs = types.SimpleNamespace(**dict((name, name) for name in names))
        ac = types.ModuleType("ac")
        ac.getCarState = self.car_state
        ac.getLastSplits = lambda _: []
        ac.getDriverName = lambda _: "test"
        ac.getCarName = lambda _: "car"
        ac.getTrackName = lambda _: "track"
        ac.getTrackConfiguration = lambda _: ""
        ac.getTrackLength = lambda _: 2600.0
        ac.setText = lambda *args: None
        ac.log = lambda *args: None
        ac.console = lambda *args: None
        sys.modules["ac"] = ac
        sys.modules["acsys"] = types.SimpleNamespace(CS=cs)
        spec = importlib.util.spec_from_file_location("ac_logger_regression", LOGGER)
        self.logger = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.logger)
        self.logger.threading = None
        self.logger.combo_dir = lambda: "."
        self.logger.write_lap = lambda path, meta, rows: self.saved.append((path, dict(meta), list(rows)))
        state = self.state

        class Monitor:
            def read(self, ac_module, lap_t, lap_count):
                return {"tyres_out": state["tyres"], "penalty_s": 0.0, "flag": 0,
                        "is_in_pit": False, "source": "test.tyres_out"}

            def close(self):
                pass

        self.logger.quality_monitor = Monitor()
        self.logger.acUpdate(0.01)  # Initial partial lap is deliberately ignored.
        self.state.update(lap=1, time=0)
        self.logger.acUpdate(0.005)

    def car_state(self, car, key):
        if key == "LapCount":
            return self.state["lap"]
        if key == "LapTime":
            return self.state["time"]
        if key == "LastLap":
            return 6000
        if key == "LapInvalidated":
            return self.state["invalid"]
        if key == "WorldPosition":
            return self.state["time"] / 20.0, 0.0, 0.0
        if key in ("AccG", "LocalAngularVelocity"):
            return [0.0] * 3
        if key == "LocalVelocity":
            return [0.0, 0.0, 20.0]
        if key in ("SlipAngle", "SlipRatio", "NdSlip", "CurrentTyresCoreTemp", "SuspensionTravel"):
            return [0.0] * 4
        return {"NormalizedSplinePosition": self.state["time"] / 6000.0,
                "SpeedKMH": 72, "SpeedMPH": 44.74, "Gas": 0.5, "Brake": 0,
                "Clutch": 1, "Steer": 0, "Gear": 4, "RPM": 4000}.get(key, 0)

    def finish(self):
        for tick in range(2, 1201):
            self.state["time"] = tick * 5
            self.logger.acUpdate(0.005)
        self.state.update(lap=2, time=0, invalid=0, tyres=0)
        self.logger.acUpdate(0.005)
        self.assertEqual(len(self.saved), 1)
        return self.saved[0]

    def test_transient_game_cut_between_samples_is_latched(self):
        self.state.update(time=5, invalid=1)
        self.logger.acUpdate(0.005)  # Too soon to write a CSV sample.
        self.state["invalid"] = 0
        path, meta, rows = self.finish()
        self.assertEqual(meta["valid"], 0)
        self.assertIn("_INVALID", path)
        self.assertTrue(meta["game_invalid_seen"])
        self.assertTrue(all(row[self.logger.INVALID_COL] == "1" for row in rows))
        self.assertEqual(meta["track_limits_coverage"], "complete")

    def test_off_track_evidence_is_independent_of_game_flag(self):
        self.state.update(time=5, tyres=3)
        self.logger.acUpdate(0.005)
        self.state["tyres"] = 0
        path, meta, rows = self.finish()
        self.assertEqual(meta["valid"], 1)  # Raw game flag is deliberately preserved.
        self.assertEqual(meta["off_track_events"], 1)
        self.assertFalse(meta["game_invalid_seen"])
        track_col = self.logger.COLUMNS.index("track_limits_exceeded")
        self.assertTrue(all(row[track_col] == "1" for row in rows))
        self.assertTrue(all(len(row) == len(self.logger.COLUMNS) for row in rows))
        self.assertEqual(self.logger.lap_quality.metadata()["off_track_events"], 0)
        self.assertFalse(self.logger.lap_invalid)  # Next lap starts with fresh state.

    def test_missing_native_signal_does_not_become_confirmed_clear(self):
        original = self.logger.ac.getCarState

        def unavailable(car, key):
            if key == "LapInvalidated":
                raise RuntimeError("signal unavailable")
            return original(car, key)

        self.logger.ac.getCarState = unavailable
        path, meta, rows = self.finish()
        self.assertEqual(meta["game_validity_coverage"], "partial")
        game_col = self.logger.COLUMNS.index("game_invalid")
        self.assertTrue(all(row[game_col] == "nan" for row in rows))

    def test_failed_update_marks_coverage_partial(self):
        original = self.logger.ac.getCarState

        def unavailable(car, key):
            if key == "LapTime":
                raise RuntimeError("one missed update")
            return original(car, key)

        self.logger.ac.getCarState = unavailable
        self.logger.acUpdate(0.005)
        self.logger.ac.getCarState = original
        path, meta, rows = self.finish()
        self.assertEqual(meta["track_limits_coverage"], "partial")
        self.assertEqual(meta["game_validity_coverage"], "partial")

    def test_save_failure_does_not_contaminate_next_lap(self):
        original = self.logger.save_lap

        def fail(*args):
            raise RuntimeError("write unavailable")

        self.logger.save_lap = fail
        for tick in range(1, 1201):
            self.state["time"] = tick * 5
            self.logger.acUpdate(0.005)
        self.state.update(lap=2, time=0)
        self.logger.acUpdate(0.005)
        self.assertEqual(self.logger.lap_count_prev, 2)
        self.assertTrue(self.logger.lap_observed_start)
        self.assertEqual(len(self.logger.rows), 0)
        self.assertEqual(self.logger.lap_quality.frames, 1)
        self.logger.save_lap = original
        self.finish_for_lap(3)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0][1]["lap_number"], 2)

    def test_last_pre_line_transient_is_retained_in_completed_header(self):
        for tick in range(1, 1201):
            self.state["time"] = tick * 5
            self.logger.acUpdate(0.005)
        self.state.update(time=6005, invalid=1, tyres=4)
        self.logger.acUpdate(0.005)
        self.state.update(lap=2, time=0, invalid=0, tyres=0)
        self.logger.acUpdate(0.005)
        self.assertEqual(len(self.saved), 1)
        path, meta, rows = self.saved[0]
        self.assertEqual(meta["valid"], 0)
        self.assertEqual(meta["off_track_events"], 1)
        self.assertEqual(rows[-1][self.logger.INVALID_COL], "0")
        self.assertFalse(self.logger.lap_invalid)

    def test_pause_resume_does_not_write_an_incomplete_lap(self):
        self.logger.on_toggle()
        self.assertFalse(self.logger.recording)
        self.logger.on_toggle()
        self.assertTrue(self.logger.recording)
        self.assertFalse(self.logger.lap_observed_start)
        for tick in range(1, 1201):
            self.state["time"] = tick * 5
            self.logger.acUpdate(0.005)
        self.state.update(lap=2, time=0)
        self.logger.acUpdate(0.005)
        self.assertFalse(self.saved)
        self.assertTrue(self.logger.lap_observed_start)

    def finish_for_lap(self, next_lap):
        for tick in range(1, 1201):
            self.state["time"] = tick * 5
            self.logger.acUpdate(0.005)
        self.state.update(lap=next_lap, time=0)
        self.logger.acUpdate(0.005)


if __name__ == "__main__":
    unittest.main()
