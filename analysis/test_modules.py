"""Game-module loading checks (core + modules/<id>/). Run:

    py -3.12 analysis/test_modules.py

Uses temporary module folders; the real modules/ and data/ are only read.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lap_analysis as la  # noqa: E402

REAL = la.MODULES_DIR


def fresh(mdir):
    la.MODULES_DIR = Path(mdir)
    la._MODULES = None
    return la.modules()


class ModuleLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llm_"))

    def tearDown(self):
        la.MODULES_DIR = REAL
        la._MODULES = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, mid, manifest, adapter=None):
        d = self.tmp / mid
        d.mkdir()
        (d / "module.json").write_text(json.dumps(manifest), encoding="utf-8")
        if adapter is not None:
            (d / "adapter.py").write_text(adapter, encoding="utf-8")
        return d

    def test_real_modules_load_in_order(self):
        mods = fresh(REAL)
        self.assertEqual(list(mods)[:2], ["ac", "fh6"])
        self.assertEqual(la.MODULE_ERRORS, {})
        self.assertEqual(la.headerless_game(), "ac")

    def test_broken_and_incompatible_modules_are_reported_not_fatal(self):
        self.write("good", {"id": "good", "name": "Good", "core_api": 1})
        self.write("newer", {"id": "newer", "name": "Newer", "core_api": la.CORE_API + 1})
        self.write("crash", {"id": "crash", "name": "Crash"}, "raise RuntimeError('boom')\n")
        self.write("wrongid", {"id": "other"})
        d = self.tmp / "badjson"
        d.mkdir()
        (d / "module.json").write_text("{not json", encoding="utf-8")
        mods = fresh(self.tmp)
        self.assertEqual(list(mods), ["good"])
        self.assertEqual(set(la.MODULE_ERRORS), {"newer", "crash", "wrongid", "badjson"})
        self.assertIn("core API", la.MODULE_ERRORS["newer"]["error"])
        self.assertIn("boom", la.MODULE_ERRORS["crash"]["error"])

    def test_missing_hooks_get_core_defaults(self):
        self.write("bare", {"id": "bare", "name": "Bare"})
        fresh(self.tmp)
        meta = {"game": "bare", "valid": "1", "rewinds": "3"}
        v = la.validity_info(meta)
        self.assertEqual(v["scope"], la.DEFAULT_HOOKS["VALIDITY_SCOPE"])
        self.assertEqual(v["status"], "unverified")          # rewinds mean nothing without the game's hook
        self.assertEqual(la.hook("bare", "CUT_TEXT"), la.DEFAULT_HOOKS["CUT_TEXT"])
        self.assertEqual(la.headerless_game(), "unknown")

    def test_module_hook_changes_validity(self):
        fresh(REAL)
        v = la.validity_info({"game": "fh6", "valid": "1", "rewinds": "2"})
        self.assertEqual((v["status"], v["valid"]), ("invalid", False))
        v = la.validity_info({"game": "fh6", "valid": "1", "rewinds": "0"})
        self.assertEqual((v["status"], v["scope"]), ("rewind_free", "rewinds/resets only"))
        v = la.validity_info({"valid": "1"})                       # no game= header: the headerless module (AC)
        self.assertEqual(v["scope"], "observed track limits and game flags")

    def test_removed_module_hides_the_game_but_keeps_data(self):
        shutil.copytree(REAL / "ac", self.tmp / "ac", ignore=shutil.ignore_patterns("__pycache__", "logger", "tests"))
        mods = fresh(self.tmp)
        self.assertEqual(list(mods), ["ac"])
        self.assertTrue((la.STUDIO_DIR / "data").is_dir())         # nothing under data/ is touched by loading


if __name__ == "__main__":
    unittest.main(verbosity=1)
