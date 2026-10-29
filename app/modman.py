"""Install, update, roll back and remove game modules (modules/<id>/) from a module registry.

A registry is a JSON file (on GitHub, or a local file for testing):

    {"format": 1, "name": "...", "modules": [
      {"id": "ac", "name": "Assetto Corsa", "short": "AC", "description": "...",
       "versions": [{"version": "1.0.0", "core_api": 1, "url": "ac-1.0.0.zip",
                     "sha256": "<64 hex>", "size": 12345, "released": "2026-10-29"}]}]}

A version's url is https://, or relative to the registry. tools/build_module.py makes the zip
(one top folder named after the module id) and its registry entry.

Every change goes through the same steps, and stops with nothing changed if one fails:
download (size cap) -> SHA-256 must match the registry -> unpack into a staging folder (no paths
outside it, no links, size and file-count caps) -> module.json must match the registry -> the
module is test-loaded by the core's own loader in a separate Python process -> refuse while the
game or logger is running (manifest "busy") -> the current folder moves to
data/studio/modman/backups/<id>/ -> the new folder moves in, with the manifest's "preserve" files
(user settings such as laplogger.ini) copied across -> test-loaded again in place; if that fails
the old folder goes back. Remove also moves the folder to backups, and a backup can be rolled
back to. Laps in data/ are never touched. Stdlib only.
"""

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlparse
from urllib.request import Request, url2pathname, urlopen

REGISTRY_FORMAT = 1
DEFAULT_REGISTRY = "https://raw.githubusercontent.com/HighOnGames/simetrics/main/registry/registry.json"
MAX_ZIP = 50 * 1024 * 1024
MAX_UNPACKED = 200 * 1024 * 1024
MAX_FILES = 2000
MAX_REGISTRY = 2 * 1024 * 1024
KEEP_BACKUPS = 3
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
INSTALL_RECORD = ".install.json"
SKIP_NAMES = {"__pycache__", INSTALL_RECORD}
BAD_CHARS = set('<>:"|?*\\') | {chr(c) for c in range(32)}
WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.I)
ANALYSIS_DIR = Path(__file__).resolve().parent.parent / "analysis"

# Runs in a separate, isolated Python: the core's own loader on one module folder.
_LOAD_CHECK = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import lap_analysis as la
m, err = la._load_module(Path(sys.argv[2]))
print(json.dumps({"ok": m is not None, "error": (err or {}).get("error"),
                  "id": m.id if m else None, "version": m.version if m else None}))
"""


class ModuleError(Exception):
    """A module change that was refused or failed; nothing (or the old module) is in place."""


def version_key(v):
    """'1.10.0' sorts after '1.9.2', and a pre-release ('1.0.0-beta') before its release."""
    main, _, pre = str(v).partition("-")
    nums = [(1, int(p), "") if p.isdigit() else (0, 0, p) for p in re.split(r"[.+]", main)]
    return nums + [(0, 0, pre) if pre else (1, 0, "")]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def module_files(folder, preserve=()):
    """{relative posix path: Path} of a module's files, without caches, dot files and preserved user files."""
    folder = Path(folder)
    keep = {p.strip("/") for p in preserve}
    out = {}
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        parts = rel.split("/")
        if any(x in SKIP_NAMES or x.startswith(".") for x in parts) or p.suffix in (".pyc", ".tmp"):
            continue
        if p.is_file() and rel not in keep:
            out[rel] = p
    return out


def read_manifest(folder):
    try:
        return json.loads((Path(folder) / "module.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _retry(fn, *args):
    """OneDrive and virus scanners can hold a file for a moment; try a rename a few times."""
    for i in range(6):
        try:
            return fn(*args)
        except PermissionError:
            if i == 5:
                raise
            time.sleep(0.5)


def _running(busy):
    """Names of running processes that match a manifest "busy" entry ({"processes": [...], "command_line": [...]})."""
    if not busy:
        return []
    names = [n.lower() for n in busy.get("processes") or []]
    needles = [n.lower() for n in busy.get("command_line") or []]
    found = []
    try:
        if os.name == "nt":
            if names:
                out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20).stdout
                running = {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line}
                found += [n for n in names if n in running]
            if needles:
                ps = ("Get-CimInstance Win32_Process | Where-Object CommandLine | "
                      "ForEach-Object { $_.CommandLine }")
                out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                                     capture_output=True, text=True, timeout=30).stdout.lower()
                found += [n for n in needles if n in out]
        else:
            out = subprocess.run(["ps", "-eo", "comm=,args="], capture_output=True, text=True, timeout=20).stdout.lower()
            found += [n for n in names + needles if n in out]
    except (OSError, subprocess.SubprocessError):
        pass                                    # can't tell: don't block the user on a missing tool
    return found


class Manager:
    """Module changes for one modules/ folder. work_dir holds downloads, staging, backups and the log."""

    def __init__(self, modules_dir, work_dir, core_api, python=None, analysis_dir=ANALYSIS_DIR, busy_check=_running):
        self.modules_dir = Path(modules_dir)
        self.work = Path(work_dir)
        self.core_api = core_api
        self.python = python or sys.executable
        self.analysis_dir = Path(analysis_dir)
        self.busy_check = busy_check
        self.lock = threading.Lock()

    # ---------------------------------------------------------- paths and log
    @property
    def backups_dir(self):
        return self.work / "backups"

    def log(self, text):
        self.work.mkdir(parents=True, exist_ok=True)
        with open(self.work / "modules.log", "a", encoding="utf-8") as f:
            f.write("{0} {1}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), text))

    # ---------------------------------------------------------- registry
    def fetch_registry(self, source):
        """Read and check a registry from an https:// URL (http only for this PC), file:// URL or file path."""
        if not source:
            raise ModuleError("no module registry is set")
        raw = _read(source, MAX_REGISTRY)
        try:
            reg = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise ModuleError("the registry is not valid JSON: {0}".format(e))
        check_registry(reg)
        reg["_source"] = source
        reg["_fetched"] = time.time()
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "registry_cache.json").write_text(json.dumps(reg, indent=1), encoding="utf-8")
        self.log("registry read from {0}: {1} module(s)".format(source, len(reg["modules"])))
        return reg

    def cached_registry(self):
        try:
            return json.loads((self.work / "registry_cache.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def compatible(self, entry):
        """The registry entry's versions this core can load, newest first."""
        vs = [v for v in entry["versions"] if v["core_api"] <= self.core_api]
        return sorted(vs, key=lambda v: version_key(v["version"]), reverse=True)

    # ---------------------------------------------------------- state
    def installed(self):
        """{id: {"version", "record", "folder"}} read from module.json files (nothing is imported)."""
        out = {}
        if self.modules_dir.is_dir():
            for d in sorted(self.modules_dir.iterdir()):
                if d.is_dir() and not d.name.startswith((".", "_")):
                    man = read_manifest(d)
                    if man is not None:
                        out[d.name] = {"version": str(man.get("version", "0")), "folder": d, "manifest": man,
                                       "record": _read_json(d / INSTALL_RECORD)}
        return out

    def changed_files(self, mid):
        """Files changed, added or removed since the module was installed by Simetrics (None = not installed by Simetrics)."""
        cur = self.installed().get(mid)
        if not cur or not cur["record"]:
            return None
        rec = cur["record"].get("files") or {}
        now = module_files(cur["folder"], cur["manifest"].get("preserve") or ())
        changed = sorted(r for r, p in now.items() if rec.get(r) != sha256_file(p))
        return changed + sorted("removed: " + r for r in rec if r not in now)

    def backups(self, mid=None):
        """[{id, name, version, saved, folder}] newest first."""
        out = []
        ids = [mid] if mid else ([d.name for d in self.backups_dir.iterdir() if d.is_dir()] if self.backups_dir.is_dir() else [])
        for i in ids:
            d = self.backups_dir / i
            if not d.is_dir():
                continue
            for b in d.iterdir():
                man = read_manifest(b)
                if b.is_dir() and man is not None:
                    out.append({"id": i, "name": b.name, "version": str(man.get("version", "0")),
                                "saved": b.stat().st_mtime, "folder": str(b), "core_api": man.get("core_api", 1)})
        return sorted(out, key=lambda x: x["name"], reverse=True)

    def status(self, reg=None):
        """Per module: installed version, newest compatible registry version, update available, backups."""
        reg = reg or self.cached_registry()
        inst = self.installed()
        entries = {e["id"]: e for e in (reg or {}).get("modules", [])}
        rows = []
        saved = {b["id"] for b in self.backups()}
        for mid in sorted(set(inst) | set(entries) | saved):
            e, cur = entries.get(mid), inst.get(mid)
            comp = self.compatible(e) if e else []
            newest = comp[0]["version"] if comp else None
            all_newest = max((v["version"] for v in e["versions"]), key=version_key) if e and e["versions"] else None
            rows.append({
                "id": mid, "name": (e or {}).get("name") or (cur["manifest"].get("name") if cur else mid),
                "short": (e or {}).get("short") or (cur["manifest"].get("short") if cur else mid.upper()),
                "description": (e or {}).get("description") or (cur["manifest"].get("description", "") if cur else ""),
                "installed": cur["version"] if cur else None,
                "by_studio": bool(cur and cur["record"]),
                "in_registry": bool(e),
                "available": newest,
                "needs_newer_core": bool(all_newest and all_newest != newest and
                                         (not newest or version_key(all_newest) > version_key(newest))),
                "update": bool(cur and newest and version_key(newest) > version_key(cur["version"])),
                "versions": [v["version"] for v in comp],
                "backups": [{k: b[k] for k in ("name", "version", "saved", "core_api")} for b in self.backups(mid)],
            })
        return {"registry": {"source": (reg or {}).get("_source"), "name": (reg or {}).get("name"),
                             "fetched": (reg or {}).get("_fetched")} if reg else None,
                "modules": rows, "core_api": self.core_api, "keep_backups": KEEP_BACKUPS}

    # ---------------------------------------------------------- the one entry point
    def plan(self, action, mid, version=None, backup=None, reg=None):
        """What an action would do, without doing it: {action, id, from, to, warnings, busy}. Raises ModuleError."""
        if not ID_RE.match(str(mid)):
            raise ModuleError("not a module id: {0!r}".format(mid))
        cur = self.installed().get(mid)
        p = {"action": action, "id": mid, "from": cur["version"] if cur else None, "warnings": [], "busy": []}
        if action in ("install", "update"):
            reg = reg or self.cached_registry()
            v = self._pick(reg, mid, version)
            if action == "install" and cur:
                raise ModuleError("{0} is already installed (version {1}); use update".format(mid, cur["version"]))
            if action == "update" and not cur:
                raise ModuleError("{0} is not installed".format(mid))
            p.update(to=v["version"], size=v["size"], sha256=v["sha256"], source=reg.get("_source"))
        elif action == "rollback":
            b = next((b for b in self.backups(mid) if b["name"] == backup), None)
            if not b:
                raise ModuleError("no backup {0!r} of {1}".format(backup, mid))
            if int(b["core_api"]) > self.core_api:
                raise ModuleError("that backup needs a newer Simetrics (core API {0})".format(b["core_api"]))
            p.update(to=b["version"], backup=backup)
        elif action == "remove":
            if not cur:
                raise ModuleError("{0} is not installed".format(mid))
            p["to"] = None
            p["data_folder"] = "data/" + str(cur["manifest"].get("data_folder") or mid)
        else:
            raise ModuleError("unknown action {0!r}".format(action))
        if cur:
            changed = self.changed_files(mid)
            if changed is None:
                p["warnings"].append("This copy wasn't installed by Simetrics, so local changes can't be checked. "
                                     "The current folder is kept in the backups.")
            elif changed:
                p["warnings"].append("{0} file(s) changed since it was installed ({1}). They are kept in the backups."
                                     .format(len(changed), ", ".join(changed[:5]) + (" …" if len(changed) > 5 else "")))
            p["busy"] = self.busy_check((cur["manifest"].get("busy") or {}))
        return p

    def apply(self, action, mid, version=None, backup=None, reg=None):
        """Do an action. Returns the plan plus "done". Raises ModuleError with nothing changed (or the old module back)."""
        with self.lock:
            p = self.plan(action, mid, version, backup, reg)
            if p["busy"]:
                raise ModuleError("close {0} first: it uses this module".format(", ".join(p["busy"])))
            try:
                if action in ("install", "update"):
                    self._install(mid, self._pick(reg or self.cached_registry(), mid, version), reg or self.cached_registry())
                elif action == "rollback":
                    self._rollback(mid, backup)
                else:
                    self._remove(mid)
            except ModuleError as e:
                self.log("{0} {1} FAILED: {2}".format(action, mid, e))
                raise
            except Exception as e:
                self.log("{0} {1} FAILED: {2}: {3}".format(action, mid, type(e).__name__, e))
                raise ModuleError("{0}: {1}".format(type(e).__name__, e))
            finally:
                shutil.rmtree(self.work / "staging", ignore_errors=True)
            self.log("{0} {1}: {2} -> {3}".format(action, mid, p["from"], p["to"]))
            self._prune(mid)
            return dict(p, done=True)

    # ---------------------------------------------------------- steps
    def _pick(self, reg, mid, version):
        if not reg:
            raise ModuleError("check the registry first")
        e = next((e for e in reg["modules"] if e["id"] == mid), None)
        if not e:
            raise ModuleError("{0} is not in the registry".format(mid))
        vs = [v for v in e["versions"] if version in (None, v["version"])]
        if not vs:
            raise ModuleError("{0} {1} is not in the registry".format(mid, version))
        comp = self.compatible(dict(e, versions=vs))
        if not comp:
            raise ModuleError("{0} {1} needs a newer Simetrics (core API {2}; this one has {3})".format(
                mid, vs[0]["version"], min(v["core_api"] for v in vs), self.core_api))
        return comp[0]

    def _download(self, reg, v, dest):
        url = _resolve(reg.get("_source", ""), v["url"])
        data = _read(url, min(MAX_ZIP, v["size"]))
        if len(data) != v["size"]:
            raise ModuleError("download is {0} bytes; the registry says {1}".format(len(data), v["size"]))
        got = hashlib.sha256(data).hexdigest()
        if got != v["sha256"]:
            raise ModuleError("download hash {0}… doesn't match the registry ({1}…): not installed".format(got[:12], v["sha256"][:12]))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def _check_load(self, folder, mid, version):
        """The core's loader, in a separate isolated Python, on one module folder."""
        try:
            r = subprocess.run([self.python, "-I", "-c", _LOAD_CHECK, str(self.analysis_dir), str(folder)],
                               capture_output=True, text=True, timeout=60, cwd=str(self.work))
            res = json.loads(r.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            raise ModuleError("the module took over a minute to load")
        except (ValueError, IndexError):
            raise ModuleError("the module test-load crashed: {0}".format((r.stderr or "")[-300:].strip()))
        if not res["ok"]:
            raise ModuleError("the module doesn't load: {0}".format(res["error"]))
        if res["id"] != mid or (version and res["version"] != version):
            raise ModuleError("the package is {0} {1}, not {2} {3}".format(res["id"], res["version"], mid, version))

    def _install(self, mid, v, reg):
        dl = self._download(reg, v, self.work / "downloads" / "{0}-{1}.zip".format(mid, v["version"]))
        try:
            staged = unpack(dl, self.work / "staging", mid)
        finally:
            dl.unlink()
        self._check_load(staged, mid, v["version"])
        man = read_manifest(staged)
        files = module_files(staged, man.get("preserve") or ())
        record = {"version": v["version"], "sha256": v["sha256"], "source": reg.get("_source"), "installed": time.time(),
                  "files": {r: sha256_file(p) for r, p in files.items()}}
        (staged / INSTALL_RECORD).write_text(json.dumps(record, indent=1), encoding="utf-8")
        self._swap_in(mid, staged, man.get("preserve") or (), v["version"])

    def _rollback(self, mid, name):
        src = self.backups_dir / mid / name
        staged = self.work / "staging" / mid
        shutil.copytree(src, staged)                # the backup stays until the swap has worked
        man = read_manifest(staged)
        self._check_load(staged, mid, str(man.get("version", "0")))
        self._swap_in(mid, staged, man.get("preserve") or (), str(man.get("version", "0")))
        shutil.rmtree(src, ignore_errors=True)

    def _remove(self, mid):
        target = self.modules_dir / mid
        dest = self._backup_path(mid, read_manifest(target) or {})
        _retry(os.replace, target, dest)

    def _backup_path(self, mid, man):
        d = self.backups_dir / mid
        d.mkdir(parents=True, exist_ok=True)
        base = "{0}_v{1}".format(time.strftime("%Y%m%d-%H%M%S"), re.sub(r"[^\w.+-]", "_", str(man.get("version", "0"))))
        dest, n = d / base, 2
        while dest.exists():
            dest, n = d / "{0}-{1}".format(base, n), n + 1
        return dest

    def _swap_in(self, mid, staged, preserve, version):
        target = self.modules_dir / mid
        backup = None
        if target.exists():
            backup = self._backup_path(mid, read_manifest(target) or {})
            _retry(os.replace, target, backup)          # fails here = nothing changed
        try:
            self.modules_dir.mkdir(parents=True, exist_ok=True)
            _retry(os.replace, staged, target)
            if backup:
                for rel in preserve:                    # user settings that live inside the module
                    old = backup.joinpath(*PurePosixPath(rel.strip("/")).parts)
                    if old.is_file():
                        new = target.joinpath(*PurePosixPath(rel.strip("/")).parts)
                        new.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(old, new)
            self._check_load(target, mid, version)
        except Exception:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if backup:
                _retry(os.replace, backup, target)
            raise

    def _prune(self, mid):
        for b in self.backups(mid)[KEEP_BACKUPS:]:
            shutil.rmtree(b["folder"], ignore_errors=True)


# -------------------------------------------------------------- helpers

def check_registry(reg):
    if not isinstance(reg, dict) or reg.get("format") != REGISTRY_FORMAT or not isinstance(reg.get("modules"), list):
        raise ModuleError("not a Simetrics module registry (format {0})".format(REGISTRY_FORMAT))
    seen = set()
    for e in reg["modules"]:
        if not isinstance(e, dict) or not ID_RE.match(str(e.get("id"))) or e["id"] in seen:
            raise ModuleError("bad or repeated module id in the registry: {0!r}".format(e.get("id") if isinstance(e, dict) else e))
        seen.add(e["id"])
        if not isinstance(e.get("versions"), list):
            raise ModuleError("{0}: versions must be a list".format(e["id"]))
        vs = set()
        for v in e["versions"]:
            ok = (isinstance(v, dict) and VERSION_RE.match(str(v.get("version"))) and isinstance(v.get("core_api"), int)
                  and SHA_RE.match(str(v.get("sha256"))) and isinstance(v.get("size"), int) and 0 < v["size"] <= MAX_ZIP
                  and isinstance(v.get("url"), str) and v["url"] and v["version"] not in vs)
            if not ok:
                raise ModuleError("{0}: a version entry is incomplete or repeated: {1!r}".format(e["id"], v))
            vs.add(v["version"])


def is_url(s):
    return urlparse(str(s)).scheme in ("http", "https", "file")     # not C:\... (scheme "c")


def _resolve(base, url):
    if is_url(url):
        return url
    if is_url(base):
        return urljoin(base, url)
    return str((Path(base).parent / url).resolve())


def _read(source, limit):
    """Bytes from an https URL (http only to this PC), a file:// URL or a path; refuses more than limit bytes."""
    u = urlparse(source)
    if u.scheme in ("http", "https"):
        if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ModuleError("modules download over https only")
        req = Request(source, headers={"User-Agent": "Simetrics-modules/1"})
        try:
            with urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
                data = r.read(limit + 1)
        except OSError as e:
            raise ModuleError("couldn't download {0}: {1}".format(source, e))
    else:
        path = Path(url2pathname(u.path)) if u.scheme == "file" else Path(source)
        try:
            with open(path, "rb") as f:
                data = f.read(limit + 1)
        except OSError as e:
            raise ModuleError("couldn't read {0}: {1}".format(path, e))
    if len(data) > limit:
        raise ModuleError("{0} is larger than expected ({1} bytes max)".format(source, limit))
    return data


def _safe_member(name, mid):
    """The member's path parts under the top folder, or a ModuleError for anything that could escape it."""
    if not name or name.startswith("/") or any(c in BAD_CHARS for c in name):
        raise ModuleError("unsafe path in the package: {0!r}".format(name))
    parts = [p for p in name.split("/") if p]
    for p in parts:
        if p in (".", "..") or p.endswith((" ", ".")) or WINDOWS_RESERVED.match(p):
            raise ModuleError("unsafe path in the package: {0!r}".format(name))
    if not parts or parts[0] != mid:
        raise ModuleError("everything in the package must be inside a {0}/ folder (found {1!r})".format(mid, name))
    return parts[1:]


def unpack(zip_path, staging, mid):
    """Unpack a module zip into staging/<mid>/ safely. Returns that folder."""
    staging = Path(staging)
    shutil.rmtree(staging, ignore_errors=True)
    out = staging / mid
    out.mkdir(parents=True)
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise ModuleError("the download is not a zip file")
    with z:
        infos = z.infolist()
        if len(infos) > MAX_FILES:
            raise ModuleError("the package has more than {0} files".format(MAX_FILES))
        total = 0
        for info in infos:
            rel = _safe_member(info.filename, mid)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ModuleError("the package contains a link: {0!r}".format(info.filename))
            dest = out.joinpath(*rel) if rel else out
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            if not rel:
                raise ModuleError("unsafe path in the package: {0!r}".format(info.filename))
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as dst:
                while True:
                    block = src.read(1 << 20)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_UNPACKED:
                        raise ModuleError("the package unpacks to more than {0} MB".format(MAX_UNPACKED >> 20))
                    dst.write(block)
    if not (out / "module.json").is_file():
        raise ModuleError("the package has no {0}/module.json".format(mid))
    return out


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
