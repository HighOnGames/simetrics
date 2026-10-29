"""Read-only AC quality evidence tests; no running game or real mappings needed."""

import importlib.util
from pathlib import Path
import struct
import unittest


HELPER = Path(__file__).resolve().parents[1] / "logger" / "LapLogger" / "quality_monitor.py"
spec = importlib.util.spec_from_file_location("quality_monitor", str(HELPER))
qm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qm)


class EmptyShared:
    def read(self, lap_t, lap_count):
        return {}

    def close(self):
        pass


class FakeMap(bytearray):
    closed = False

    def close(self):
        self.closed = True


def mappings(status=2, clock=1000, laps=2, tyres=3):
    graphics = FakeMap(280)
    physics = FakeMap(248)
    struct.pack_into("<iii", graphics, 0, 101, status, 0)
    struct.pack_into("<i", graphics, 132, laps)
    struct.pack_into("<i", graphics, 140, clock)
    struct.pack_into("<i", graphics, 160, 0)
    struct.pack_into("<f", graphics, 264, 2.5)
    struct.pack_into("<i", graphics, 268, 6)
    struct.pack_into("<i", physics, 0, 222)
    struct.pack_into("<i", physics, 244, tyres)
    maps = {"acpmf_graphics": graphics, "acpmf_physics": physics}
    return maps, qm.SharedMemoryReader(lambda name, size: maps[name])


def observation(tyres, pit=False, source="test", penalty=None, flag=None):
    return {"tyres_out": tyres, "source": source, "is_in_pit": pit,
            "penalty_s": penalty, "flag": flag}


class ReaderTests(unittest.TestCase):
    def test_missing_signal_is_unknown(self):
        monitor = qm.QualityMonitor(EmptyShared())
        value = monitor.read(object(), 1000, 2)
        self.assertIsNone(value["tyres_out"])
        self.assertIsNone(value["penalty_s"])
        self.assertIsNone(value["source"])

    def test_optional_csp_zero_is_a_real_measurement(self):
        class AC:
            @staticmethod
            def ext_getNumberOfTyresOut(car):
                self.assertEqual(car, 0)
                return 0
        value = qm.QualityMonitor(EmptyShared()).read(AC(), 1000, 2)
        self.assertEqual(value["tyres_out"], 0)
        self.assertEqual(value["source"], "csp.ext_getNumberOfTyresOut")

    def test_optional_csp_no_argument_signature(self):
        class AC:
            @staticmethod
            def ext_getNumberOfTyresOut():
                return 4
        self.assertEqual(qm.QualityMonitor(EmptyShared()).read(AC(), 1000, 2)["tyres_out"], 4)

    def test_invalid_or_failed_csp_uses_independent_fallback(self):
        for bad in (float("nan"), float("inf"), -1, 5, 1.5, "0"):
            class AC:
                @staticmethod
                def ext_getNumberOfTyresOut(car):
                    return bad
            maps, reader = mappings()
            value = qm.QualityMonitor(reader).read(AC(), 1000, 2)
            self.assertEqual(value["tyres_out"], 3)
            self.assertEqual(value["source"], "shared_memory.physics")

        class BrokenAC:
            @staticmethod
            def ext_getNumberOfTyresOut(car):
                raise RuntimeError("not supported")
        value = qm.QualityMonitor(EmptyShared()).read(BrokenAC(), 1000, 2)
        self.assertIsNone(value["tyres_out"])

    def test_zero_mappings_are_not_a_clean_measurement(self):
        reader = qm.SharedMemoryReader(lambda name, size: FakeMap(size))
        self.assertIsNone(reader.read(1000, 2)["tyres_out"])

    def test_prefix_offsets_and_same_packet_sampling(self):
        maps, reader = mappings()
        first = reader.read(1000, 2)
        self.assertEqual(first["tyres_out"], 3)
        self.assertEqual(first["penalty_s"], 2.5)
        self.assertEqual(first["flag"], 6)
        self.assertIs(first["is_in_pit"], False)
        self.assertEqual(reader.read(1100, 2)["tyres_out"], 3)
        reader.close()
        self.assertTrue(all(handle.closed for handle in maps.values()))

    def test_replay_or_lap_clock_mismatch_is_rejected(self):
        for status, clock, laps in ((1, 1000, 2), (3, 1000, 2), (2, 4000, 2), (2, 1000, 1)):
            maps, reader = mappings(status, clock, laps)
            self.assertIsNone(reader.read(1000, 2)["tyres_out"])

    def test_frozen_graphics_is_rejected_without_rejecting_brief_repeat(self):
        maps, reader = mappings()
        self.assertEqual(reader.read(1000, 2)["tyres_out"], 3)
        self.assertEqual(reader.read(1200, 2)["tyres_out"], 3)
        self.assertIsNone(reader.read(2101, 2)["tyres_out"])

    def test_pit_lane_is_identified_independently_of_pit_box(self):
        maps, reader = mappings()
        struct.pack_into("<i", maps["acpmf_graphics"], 276, 1)
        self.assertIs(reader.read(1000, 2)["is_in_pit"], True)

    def test_frozen_physics_is_rejected_while_graphics_updates(self):
        maps, reader = mappings()
        self.assertEqual(reader.read(1000, 2)["tyres_out"], 3)
        struct.pack_into("<i", maps["acpmf_graphics"], 0, 102)
        struct.pack_into("<i", maps["acpmf_graphics"], 140, 2101)
        value = reader.read(2101, 2)
        self.assertIsNone(value["tyres_out"])
        self.assertEqual(value["penalty_s"], 2.5)


class AccumulatorTests(unittest.TestCase):
    def test_transient_native_and_excursion_latch_separately(self):
        quality = qm.LapQuality()
        quality.observe(observation(0), 0.02, 0)
        self.assertIs(quality.observe(observation(3), 0.02, 0), True)
        quality.observe(observation(0), 0.02, 1)
        quality.observe(observation(0), 0.02, 0)
        self.assertTrue(quality.track_limits_exceeded)
        self.assertTrue(quality.game_invalid_seen)
        meta = quality.metadata()
        self.assertEqual(meta["off_track_events"], 1)
        self.assertEqual(meta["off_track_s"], 0.02)
        self.assertEqual(meta["track_limits_coverage"], "complete")
        self.assertEqual(meta["game_validity_coverage"], "complete")
        quality.reset()
        self.assertFalse(quality.track_limits_exceeded)
        self.assertFalse(quality.game_invalid_seen)
        self.assertEqual(quality.metadata()["off_track_events"], 0)
        self.assertEqual(quality.metadata()["track_limits_coverage"], "unavailable")

    def test_excursion_does_not_claim_game_invalid(self):
        quality = qm.LapQuality()
        quality.observe(observation(4), 0.02, 0)
        meta = quality.metadata()
        self.assertEqual(meta["track_limits_exceeded"], 1)
        self.assertEqual(meta["game_invalid_seen"], 0)

    def test_unknown_gap_does_not_double_count_one_excursion(self):
        quality = qm.LapQuality()
        for tyres in (3, None, 4, 0, 3):
            quality.observe(observation(tyres), 0.02, 0)
        meta = quality.metadata()
        self.assertEqual(meta["off_track_events"], 2)
        self.assertEqual(meta["off_track_s"], 0.06)
        self.assertEqual(meta["track_limits_coverage"], "partial")

    def test_absent_evidence_does_not_imply_clean_coverage(self):
        quality = qm.LapQuality()
        self.assertIsNone(quality.observe({}, 0.02))
        meta = quality.metadata()
        self.assertEqual(meta["track_limits_coverage"], "unavailable")
        self.assertEqual(meta["game_validity_coverage"], "unavailable")
        self.assertEqual(meta["max_tyres_out"], "unknown")
        quality.observe(observation(0), 0.02, 0)
        self.assertEqual(quality.metadata()["track_limits_coverage"], "partial")
        self.assertEqual(quality.metadata()["game_validity_coverage"], "partial")

    def test_long_gap_is_capped_and_coverage_partial(self):
        quality = qm.LapQuality()
        quality.observe(observation(4), 5.0, 0)
        meta = quality.metadata()
        self.assertEqual(meta["off_track_s"], qm.MAX_FRAME_S)
        self.assertEqual(meta["track_limits_coverage"], "partial")
        self.assertEqual(meta["game_validity_coverage"], "partial")

    def test_known_pits_are_excluded_and_penalty_is_independent(self):
        quality = qm.LapQuality()
        self.assertIs(quality.observe(observation(4, pit=True, penalty=2.0), 0.02, 0), False)
        quality.observe(observation(0), 0.02, 0)
        meta = quality.metadata()
        self.assertEqual(meta["off_track_events"], 0)
        self.assertEqual(meta["off_track_s"], 0)
        self.assertEqual(meta["max_tyres_out"], 0)
        self.assertEqual(meta["track_limits_coverage"], "complete")
        self.assertEqual(meta["penalty_seen"], 1)
        self.assertEqual(meta["game_invalid_seen"], 0)

    def test_boundary_uses_separate_lap_accumulators(self):
        previous = qm.LapQuality()
        previous.observe(observation(3), 0.02, 1)
        saved = previous.metadata()
        previous.reset()
        previous.observe(observation(0), 0.02, 0)
        self.assertEqual(saved["game_invalid_seen"], 1)
        self.assertEqual(saved["track_limits_exceeded"], 1)
        self.assertEqual(previous.metadata()["game_invalid_seen"], 0)
        self.assertEqual(previous.metadata()["track_limits_exceeded"], 0)

    def test_mixed_sources_and_invalid_native_signal(self):
        quality = qm.LapQuality()
        quality.observe(observation(0, source="csp"), 0.02, 0)
        quality.observe(observation(0, source="shared_memory"), 0.02, float("nan"))
        self.assertEqual(quality.metadata()["track_limits_source"], "mixed")
        self.assertEqual(quality.metadata()["game_validity_coverage"], "partial")

    def test_penalty_peak_distinguishes_known_zero_from_missing(self):
        quality = qm.LapQuality()
        quality.observe(observation(0, flag=6), 0.02, 0)
        self.assertEqual(quality.metadata()["penalty_max_s"], "unknown")
        self.assertEqual(quality.metadata()["penalty_seen"], 1)
        quality.observe(observation(0, penalty=0.0), 0.02, 0)
        self.assertEqual(quality.metadata()["penalty_max_s"], 0.0)
        quality.observe(observation(0, penalty=2.5), 0.02, 0)
        quality.observe(observation(0, penalty=1.0), 0.02, 0)
        self.assertEqual(quality.metadata()["penalty_max_s"], 2.5)
        quality.reset()
        self.assertEqual(quality.metadata()["penalty_max_s"], "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
