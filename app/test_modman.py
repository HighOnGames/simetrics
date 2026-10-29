"""Module install / update / rollback / remove checks (app/modman.py, tools/build_module.py). Run:

    py -3.12 app/test_modman.py

Works on copies of the real modules in a temporary folder; the real modules/ and data/ are only read.
"""

import functools
import hashlib
import http.server
import json
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDIO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(STUDIO / "analysis"))
sys.path.insert(0, str(STUDIO / "tools"))
import build_module  # noqa: E402
import lap_analysis as la  # noqa: E402
import modman  # noqa: E402

INI = "logger/LapLogger/laplogger.ini"


def tree(folder):
    """{relative path: sha256} of every file under a folder (caches left out)."""
    folder = Path(folder)
    return {p.relative_to(folder).as_posix(): modman.sha256_file(p) for p in sorted(folder.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


class Modman(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmm_"))
        self.mods = self.tmp / "modules"
        self.src = self.tmp / "src"            # where test versions of modules are edited before packaging
        for mid in ("ac", "fh6"):
            for d in (self.mods, self.src):
                shutil.copytree(la.MODULES_DIR / mid, d / mid, ignore=shutil.ignore_patterns("__pycache__"))
        (self.mods / "ac" / INI).write_text("[LAPLOGGER]\ndata_dir=C:\\my laps\n", encoding="utf-8")
        self.reg_dir = self.tmp / "reg"
        self.reg_dir.mkdir()
        self.reg_path = self.reg_dir / "registry.json"
        self.publish("ac")
        self.publish("fh6")
        self.busy = []
        self.m = modman.Manager(self.mods, self.tmp / "work", la.CORE_API, busy_check=lambda b: list(self.busy))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------- helpers
    def publish(self, mid, version=None, core_api=None, adapter_extra=None):
        """Package src/<mid> (optionally as another version) into the test registry."""
        d = self.src / mid
        man = json.loads((d / "module.json").read_text(encoding="utf-8"))
        if version:
            man["version"] = version
        if core_api:
            man["core_api"] = core_api
        if version or core_api:
            (d / "module.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
        if adapter_extra is not None:
            with open(d / "adapter.py", "a", encoding="utf-8") as f:
                f.write(adapter_extra)
        data, man = build_module.build_zip(d, mid)
        name = "{0}-{1}.zip".format(mid, man["version"])
        (self.reg_dir / name).write_bytes(data)
        reg = json.loads(self.reg_path.read_text(encoding="utf-8")) if self.reg_path.exists() else \
            {"format": 1, "name": "test", "modules": []}
        build_module.add_to_registry(reg, man, {"version": man["version"], "core_api": int(man.get("core_api", 1)),
                                                "url": name, "sha256": hashlib.sha256(data).hexdigest(),
                                                "size": len(data), "released": "2026-10-29"})
        self.reg_path.write_text(json.dumps(reg), encoding="utf-8")
        return data

    def edit_registry(self, fn):
        reg = json.loads(self.reg_path.read_text(encoding="utf-8"))
        fn(reg)
        self.reg_path.write_text(json.dumps(reg), encoding="utf-8")

    def reg(self):
        return self.m.fetch_registry(str(self.reg_path))

    def assertRefused(self, text, fn, *args):
        before = tree(self.mods)
        with self.assertRaises(modman.ModuleError) as cm:
            fn(*args)
        self.assertIn(text, str(cm.exception))
        self.assertEqual(tree(self.mods), before, "modules/ changed after a refused action")
        self.assertFalse((self.tmp / "work" / "staging").exists())

    # ---------------------------------------------------------- tests
    def test_packages_are_reproducible_and_leave_out_user_files(self):
        a, _ = build_module.build_zip(self.mods / "ac", "ac")
        b, _ = build_module.build_zip(self.mods / "ac", "ac")
        self.assertEqual(a, b)
        names = zipfile.ZipFile(self.reg_dir / "ac-1.0.0.zip").namelist()
        self.assertIn("ac/module.json", names)
        self.assertNotIn("ac/" + INI, names)
        self.assertFalse([n for n in names if "__pycache__" in n or not n.startswith("ac/")])

    def test_remove_then_install_round_trip_gives_identical_files(self):
        original = tree(la.MODULES_DIR / "fh6")
        self.m.apply("remove", "fh6")
        self.assertNotIn("fh6", self.m.installed())
        self.assertEqual(len(self.m.backups("fh6")), 1)
        st = self.m.status(self.reg())
        row = next(r for r in st["modules"] if r["id"] == "fh6")
        self.assertEqual((row["installed"], row["available"], row["update"]), (None, "1.0.0", False))
        self.m.apply("install", "fh6")
        now = tree(self.mods / "fh6")
        self.assertEqual(now.pop(modman.INSTALL_RECORD) is not None, True)
        self.assertEqual(now, original)
        self.assertEqual(self.m.changed_files("fh6"), [])
        self.assertTrue(self.m.status()["modules"][1]["by_studio"])

    def test_update_keeps_user_settings_and_can_roll_back(self):
        self.publish("ac", "1.0.1")
        st = self.m.status(self.reg())
        self.assertTrue(next(r for r in st["modules"] if r["id"] == "ac")["update"])
        p = self.m.plan("update", "ac")
        self.assertEqual((p["from"], p["to"]), ("1.0.0", "1.0.1"))
        self.assertIn("wasn't installed by Simetrics", p["warnings"][0])
        self.m.apply("update", "ac")
        self.assertEqual(self.m.installed()["ac"]["version"], "1.0.1")
        self.assertIn("my laps", (self.mods / "ac" / INI).read_text(encoding="utf-8"))
        (self.mods / "ac" / INI).write_text("[LAPLOGGER]\ndata_dir=D:\\moved\n", encoding="utf-8")
        b = self.m.backups("ac")
        self.assertEqual([x["version"] for x in b], ["1.0.0"])
        self.m.apply("rollback", "ac", backup=b[0]["name"])
        self.assertEqual(self.m.installed()["ac"]["version"], "1.0.0")
        self.assertIn("moved", (self.mods / "ac" / INI).read_text(encoding="utf-8"))   # the newest settings win
        self.assertEqual([x["version"] for x in self.m.backups("ac")], ["1.0.1"])
        log = (self.tmp / "work" / "modules.log").read_text(encoding="utf-8")
        self.assertIn("update ac: 1.0.0 -> 1.0.1", log)
        self.assertIn("rollback ac: 1.0.1 -> 1.0.0", log)

    def test_wrong_hash_or_size_is_refused(self):
        self.publish("ac", "1.0.1")
        self.edit_registry(lambda r: r["modules"][0]["versions"][-1].update(sha256="0" * 64))
        self.reg()
        self.assertRefused("doesn't match the registry", self.m.apply, "update", "ac")
        self.edit_registry(lambda r: r["modules"][0]["versions"][-1].update(size=100))
        self.reg()
        self.assertRefused("larger than expected", self.m.apply, "update", "ac")
        self.assertEqual(self.m.backups("ac"), [])

    def test_unsafe_packages_are_refused(self):
        def zipped(entries, link=None):
            p = self.tmp / "bad.zip"
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("fh6/module.json", json.dumps({"id": "fh6"}))
                for n in entries:
                    z.writestr(n, "x")
                if link:
                    info = zipfile.ZipInfo(link)
                    info.external_attr = 0o120777 << 16
                    z.writestr(info, "../../outside")
            return p
        cases = {"../evil.py": "unsafe path", "fh6/../../evil.py": "unsafe path", "/abs.py": "unsafe path",
                 "fh6/C:/x.py": "unsafe path", "fh6\\..\\x.py": "unsafe path", "other/x.py": "inside a fh6/ folder",
                 "fh6/CON.txt": "unsafe path", "fh6/name. ": "unsafe path"}
        for name, text in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(modman.ModuleError) as cm:
                    modman.unpack(zipped([name]), self.tmp / "staging", "fh6")
                self.assertIn(text, str(cm.exception))
        with self.assertRaises(modman.ModuleError) as cm:
            modman.unpack(zipped([], link="fh6/link"), self.tmp / "staging", "fh6")
        self.assertIn("link", str(cm.exception))
        self.assertFalse((self.tmp / "evil.py").exists() or (self.tmp.parent / "evil.py").exists())
        (self.tmp / "notzip.zip").write_bytes(b"hello")
        with self.assertRaises(modman.ModuleError):
            modman.unpack(self.tmp / "notzip.zip", self.tmp / "staging", "fh6")

    def test_package_bigger_than_the_unpack_cap_is_refused(self):
        p = self.tmp / "bomb.zip"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("fh6/module.json", "{}")
            z.writestr("fh6/big.bin", b"\0" * (modman.MAX_UNPACKED + 1))
        with self.assertRaises(modman.ModuleError) as cm:
            modman.unpack(p, self.tmp / "staging", "fh6")
        self.assertIn("unpacks to more than", str(cm.exception))

    def test_module_for_a_newer_studio_is_not_offered(self):
        self.publish("fh6", "2.0.0", core_api=la.CORE_API + 1)
        st = self.m.status(self.reg())
        row = next(r for r in st["modules"] if r["id"] == "fh6")
        self.assertEqual((row["available"], row["update"], row["needs_newer_core"]), ("1.0.0", False, True))
        self.assertRefused("needs a newer Simetrics", self.m.apply, "update", "fh6", "2.0.0")

    def test_broken_module_is_rolled_back(self):
        self.publish("fh6", "1.0.1", adapter_extra="\nraise RuntimeError('broken release')\n")
        self.reg()
        self.assertRefused("broken release", self.m.apply, "update", "fh6")
        self.assertEqual(self.m.installed()["fh6"]["version"], "1.0.0")

    def test_package_with_the_wrong_version_inside_is_refused(self):
        data = self.publish("fh6", "1.0.1")
        self.edit_registry(lambda r: r["modules"][1]["versions"][-1].update(version="1.0.2"))
        self.reg()
        self.assertTrue(data)
        self.assertRefused("not fh6 1.0.2", self.m.apply, "update", "fh6")

    def test_busy_module_is_not_changed(self):
        self.publish("ac", "1.0.1")
        self.reg()
        self.busy = ["acs.exe"]
        self.assertEqual(self.m.plan("update", "ac")["busy"], ["acs.exe"])
        self.assertRefused("close acs.exe first", self.m.apply, "update", "ac")
        self.assertRefused("close acs.exe first", self.m.apply, "remove", "ac")

    def test_local_changes_are_reported_before_an_update(self):
        self.m.apply("remove", "fh6")
        self.reg()
        self.m.apply("install", "fh6")
        with open(self.mods / "fh6" / "adapter.py", "a", encoding="utf-8") as f:
            f.write("\n# my tweak\n")
        self.publish("fh6", "1.0.1")
        self.reg()
        w = self.m.plan("update", "fh6")["warnings"]
        self.assertIn("adapter.py", w[0])
        self.m.apply("update", "fh6")
        kept = Path(self.m.backups("fh6")[0]["folder"]) / "adapter.py"
        self.assertIn("my tweak", kept.read_text(encoding="utf-8"))

    def test_old_backups_are_pruned(self):
        for i in range(1, 6):
            self.publish("fh6", "1.0.{0}".format(i))
            self.reg()
            self.m.apply("update", "fh6")
        self.assertEqual([b["version"] for b in self.m.backups("fh6")], ["1.0.4", "1.0.3", "1.0.2"])

    def test_registry_checks(self):
        for bad in ({}, {"format": 2, "modules": []}, {"format": 1, "modules": [{"id": "Bad!", "versions": []}]},
                    {"format": 1, "modules": [{"id": "a", "versions": [{"version": "1"}]}]},
                    {"format": 1, "modules": [{"id": "a", "versions": []}, {"id": "a", "versions": []}]}):
            with self.subTest(bad=bad):
                with self.assertRaises(modman.ModuleError):
                    modman.check_registry(bad)
        with self.assertRaises(modman.ModuleError) as cm:
            modman._read("http://example.com/registry.json", 100)
        self.assertIn("https only", str(cm.exception))
        self.assertEqual(sorted(["1.10.0", "1.9.2", "1.0.0-beta", "1.0.0"], key=modman.version_key),
                         ["1.0.0-beta", "1.0.0", "1.9.2", "1.10.0"])

    def test_install_over_http_from_this_pc(self):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(self.reg_dir))
        handler.log_message = lambda *a: None
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.m.apply("remove", "fh6")
            reg = self.m.fetch_registry("http://127.0.0.1:{0}/registry.json".format(srv.server_address[1]))
            self.m.apply("install", "fh6", reg=reg)
            self.assertEqual(self.m.installed()["fh6"]["record"]["source"], reg["_source"])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
