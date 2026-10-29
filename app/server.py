"""Simetrics: local web app for lap telemetry from the installed game modules (modules/<id>/).

    py -3.12 server.py            # serves http://127.0.0.1:8765 and opens the browser
    py -3.12 server.py --no-open

Only listens on 127.0.0.1. Stdlib only. Game pictures come from each module's image_file hook (read
from the game's own files, nothing is copied); pictures you add in the app are shrunk to ~30 KB.
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import engine
import modman
import prompts
import trends

WEB = Path(__file__).resolve().parent / "web"
SAFE = re.compile(r"^[\w .\-()+&']+$")
analyser = None
srv = None
RESTART = threading.Event()        # set after a module change: the server stops and starts again
MODMAN = modman.Manager(engine.la.MODULES_DIR, engine.STATE_DIR / "modman", engine.la.CORE_API)


def _safe(*parts):
    for p in parts:
        if not p or p in (".", "..") or not SAFE.match(p):
            raise KeyError(p)
    return parts


def index_payload():
    with analyser.lock:
        combos = [dict(c) for c in analyser.combos]
        results = dict(analyser.results)
    for c in combos:
        r = results.get(c["id"])
        c.pop("fingerprint", None)
        if r:
            c["outline"] = r["outline"]
            c["closed"] = r["closed"]
            c["typical"] = r["typical"]
            c["theoretical"] = r["theoretical"]
            # The analysed population excludes raw discontinuities and separates
            # unverified legacy recordings from observed-clear laps.
            c["best"] = r["best"]
            c["reference_role"] = r.get("reference_role")
            c["validity_counts"] = {key: r.get(key, 0) for key in ("valid", "invalid", "off_track", "penalty", "unverified")}
            c.update(c["validity_counts"])
            c["analysed"] = True
    return {"combos": combos, "status": analyser.status, "games": engine.GAMES, "units": engine.units(),
            "modules": {gid: module_ui(m) for gid, m in engine.MODULES.items()}, "data_dir": str(engine.DATA_DIR)}


# ---------------------------------------------------------- game modules

def module_ui(m):
    """What the web UI needs from a module's manifest."""
    man = m.manifest
    css = m.path / "web" / "module.css"
    return {"id": m.id, "name": m.name, "short": m.short, "version": m.version,
            "filters": man.get("filters") or {}, "ui": man.get("ui") or {},
            "css": "/modules/{0}/web/module.css".format(m.id) if css.is_file() else None}


def _laps_on_disk(folder):
    d = engine.DATA_DIR / folder
    return sum(1 for c in d.iterdir() if c.is_dir() and "@" in c.name for _ in c.glob("*.csv")) if d.is_dir() else 0


def modules_payload():
    """Installed modules, modules that failed to load, and lap folders no installed module reads."""
    la = engine.la
    installed = []
    for m in engine.MODULES.values():
        man = m.manifest
        installed.append({"id": m.id, "name": m.name, "short": m.short, "version": m.version, "core_api": m.core_api,
                          "compatible": True, "description": man.get("description", ""), "folder": str(m.path),
                          "data_folder": "data/" + m.data_folder, "laps": _laps_on_disk(m.data_folder),
                          "logger": man.get("logger") or {}, "hooks": sorted(k for k in la.DEFAULT_HOOKS if
                                                                              m.adapter is not None and hasattr(m.adapter, k))})
    failed = [dict(err, id=name, folder=str(la.MODULES_DIR / name)) for name, err in la.MODULE_ERRORS.items()]
    owned = {m.data_folder for m in engine.MODULES.values()} | {"studio"}
    orphans = []
    if engine.DATA_DIR.is_dir():
        for d in sorted(engine.DATA_DIR.iterdir()):
            if d.is_dir() and d.name not in owned and (n := _laps_on_disk(d.name)):
                orphans.append({"data_folder": "data/" + d.name, "laps": n})
    return {"core_api": la.CORE_API, "modules_dir": str(la.MODULES_DIR), "installed": installed,
            "failed": failed, "orphan_data": orphans, "manage": MODMAN.status(), "registry_source": registry_source(),
            "registry_setting": engine.SETTINGS.get("module_registry") or ""}


def registry_source():
    """The registry to read: settings.json module_registry (a URL, or a path relative to Simetrics folder)."""
    src = engine.SETTINGS.get("module_registry") or modman.DEFAULT_REGISTRY
    if src and not modman.is_url(src) and not Path(src).is_absolute():
        src = str(engine.STUDIO_DIR / src)
    return src


def restart_soon():
    """Stop serving after this response, so main() starts Simetrics again with the changed modules."""
    RESTART.set()
    threading.Timer(0.8, srv.shutdown).start()


def modules_css():
    """Each module's colour token (--<id>) for both themes, and its game badge / tab colours."""
    dark, light, rules = [], [], []
    for gid, m in engine.MODULES.items():
        c = m.manifest.get("colors") or {}
        if c.get("dark") or c.get("light"):
            dark.append("--{0}: {1};".format(gid, c.get("dark") or c.get("light")))
            light.append("--{0}: {1};".format(gid, c.get("light") or c.get("dark")))
            rules.append(".gl.{0} {{ background: var(--{0}); }} .badge.{0} {{ background: var(--{0}); color: #fff; }}".format(gid))
    return "\n".join(["/* generated from modules/<id>/module.json colors */",
                      ":root {{ {0} }}".format(" ".join(dark)),
                      '@media (prefers-color-scheme: light) {{ :root:not([data-theme="dark"]) {{ {0} }} }}'.format(" ".join(light)),
                      ':root[data-theme="light"] {{ {0} }}'.format(" ".join(light))] + rules) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "Simetrics/1"

    def log_message(self, fmt, *args):
        if "/api/" in self.path and not self.path.startswith("/api/status"):
            sys.stderr.write("{0} {1}\n".format(time.strftime("%H:%M:%S"), fmt % args))

    # ---------------------------------------------------------- responses
    def _send(self, code, body, ctype="application/json", cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":")).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, cache=True):
        path = Path(path)
        if not path.is_file():
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype, cache)

    # ---------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
        try:
            if not parts:
                return self._file(WEB / "index.html", cache=False)
            if parts[0] == "api":
                return self._api_get(parts[1:], q)
            if parts[0] == "img":
                return self._img(parts[1:])
            if parts[0] == "modules.css":
                return self._send(200, modules_css(), "text/css")
            if parts[0] == "modules" and len(parts) >= 4 and parts[1] in engine.MODULES and parts[2] == "web":
                _safe(*parts[3:])                   # a module's optional web/ files (skin CSS, panels)
                return self._file(engine.MODULES[parts[1]].path.joinpath("web", *parts[3:]), cache=False)
            _safe(*parts)
            return self._file(WEB.joinpath(*parts), cache=False)
        except KeyError as e:
            self._send(404, {"error": "not found: {0}".format(e)})
        except Exception as e:
            self._send(500, {"error": "{0}: {1}".format(type(e).__name__, e)})

    def _api_get(self, parts, q):
        name = parts[0] if parts else ""
        if name == "status":
            return self._send(200, analyser.status)
        if name == "modules":
            return self._send(200, modules_payload())
        if name == "index":
            return self._send(200, index_payload())
        if name == "combo":
            c, r = analyser.result(q["id"])
            c = dict(c)
            c.pop("fingerprint", None)
            return self._send(200, {"combo": c, "analysis": r, "tip_phrases": trends.TIP_PHRASES})
        if name == "lap":
            c, r = analyser.result(q["id"])
            return self._send(200, dict(engine.lap_detail(c, r["ref"], q["lap"], q.get("ref"), r["corner_set"]["frame_file"]),
                                         metrics=trends.metric_defs()))
        if name == "overview":
            ids = set(q["combos"].split(",")) if q.get("combos") else None
            return self._send(200, dict(trends.overview(analyser, q.get("game") or None, ids, float(q.get("since", 0) or 0)),
                                         tip_phrases=trends.TIP_PHRASES))
        if name == "brief":
            body = prompts.brief_json(analyser, q.get("page", "library"), q.get("goal", "mix"),
                                      q.get("id") or None, q.get("lap") or None, q.get("ref") or None, q.get("s") or None,
                                      q.get("game") or None, set(q["combos"].split(",")) if q.get("combos") else None,
                                      float(q.get("since", 0) or 0), q.get("label") or None)
            name = "laplogger-brief_{0}_{1}.json".format(q.get("page", "library"), time.strftime("%Y%m%d-%H%M%S"))
            self.send_response(200)
            data = body.encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="{0}"'.format(name))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if name == "prompt":
            text = prompts.build(analyser, q.get("page", "library"), q.get("goal", "mix"), q.get("variant", "agent"),
                                 q.get("id") or None, q.get("lap") or None, q.get("ref") or None, q.get("s") or None,
                                 q.get("game") or None, set(q["combos"].split(",")) if q.get("combos") else None,
                                 float(q.get("since", 0) or 0), q.get("label") or None)
            return self._send(200, {"text": text})
        raise KeyError(name)

    def _img(self, parts):
        kind = parts[0] if parts else ""
        if kind == "user":
            _safe(parts[1])
            return self._file(engine.IMAGES_DIR / parts[1])
        if kind in engine.MODULES and len(parts) >= 2:
            _safe(*parts[1:])
            fn = engine.MODULES[kind].hook("image_file")
            path = fn(parts[1:], engine.SETTINGS) if fn else None
            if path is not None:
                return self._file(path)
        raise KeyError("/".join(parts))

    # ---------------------------------------------------------- POST
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 8 * 1024 * 1024:
                return self._send(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            name = urlparse(self.path).path.strip("/").split("/")[-1]
            if name == "library":
                kind = body["kind"]
                if kind not in ("cars", "tracks"):
                    raise KeyError(kind)
                allowed = {"name", "discipline", "skin", "image"}
                fields = {k: v for k, v in body.get("fields", {}).items() if k in allowed}
                if fields.get("discipline") and fields["discipline"] not in engine.disciplines(str(body["key"]).split(":")[0]):
                    raise KeyError(fields["discipline"])
                entry = engine.update_library(kind, body["key"], fields)
                analyser.refresh()
                return self._send(200, {"ok": True, "entry": entry})
            if name == "image":
                kind, key = body["kind"], body["key"]
                if kind not in ("cars", "tracks"):
                    raise KeyError(kind)
                m = re.match(r"^data:image/(jpeg|png|webp);base64,(.+)$", body.get("data", ""), re.S)
                if not m:
                    raise ValueError("expected a JPEG/PNG/WebP data URL")
                raw = base64.b64decode(m.group(2))
                engine.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
                fname = "{0}_{1}.{2}".format(kind[:-1], re.sub(r"[^\w-]", "_", key), "jpg" if m.group(1) == "jpeg" else m.group(1))
                (engine.IMAGES_DIR / fname).write_bytes(raw)
                url = "/img/user/{0}?v={1}".format(fname, int(time.time()))
                entry = engine.update_library(kind, key, {"image": url})
                analyser.refresh()
                return self._send(200, {"ok": True, "entry": entry, "bytes": len(raw)})
            if name == "settings":
                u = body.get("units") or {}
                if any(u.get(k) not in v for k, v in engine.UNIT_CHOICES.items()):
                    raise ValueError("units must be speed kmh|mph and distance m|ft")
                engine.save_settings({"units": {k: u[k] for k in engine.UNIT_CHOICES}})
                return self._send(200, {"ok": True, "units": engine.units()})
            if name == "remap":
                c, r = analyser.result(body["id"])
                engine.remap(c["id"], r["ref"])
                analyser.refresh()
                time.sleep(0.3)
                analyser.rescan()
                c, r = analyser.result(body["id"])
                return self._send(200, {"ok": True, "corner_set": r["corner_set"]})
            if name == "delete":
                # two steps: a dry run lists what would go; only {"confirm": "delete"} removes files
                kind, ids = body["kind"], body.get("ids") or []
                if kind not in ("cars", "tracks") or not isinstance(ids, list) or not ids:
                    raise ValueError("expected kind cars|tracks and a list of combo ids")
                real = body.get("confirm") == "delete"
                return self._send(200, dict(analyser.delete(set(ids), kind, dry_run=not real), deleted=real))
            if name == "module_check":
                reg = MODMAN.fetch_registry(registry_source())
                return self._send(200, {"ok": True, "manage": MODMAN.status(reg)})
            if name == "module_registry":
                src = str(body.get("source") or "").strip()
                engine.save_settings({"module_registry": src})
                return self._send(200, {"ok": True, "registry_source": registry_source()})
            if name == "module":
                # two steps, like delete: without {"confirm": <action>} it only says what would happen
                action, mid = body.get("action"), body.get("id")
                args = (action, mid, body.get("version") or None, body.get("backup") or None)
                if body.get("confirm") != action:
                    return self._send(200, dict(MODMAN.plan(*args), done=False))
                if RESTART.is_set():
                    raise modman.ModuleError("Simetrics is restarting; try again in a moment")
                res = MODMAN.apply(*args)
                restart_soon()
                return self._send(200, dict(res, restarting=True))
            if name == "refresh":
                analyser.refresh()
                return self._send(200, {"ok": True})
            raise KeyError(name)
        except KeyError as e:
            self._send(404, {"error": "unknown: {0}".format(e)})
        except Exception as e:
            self._send(400, {"error": "{0}: {1}".format(type(e).__name__, e)})


def main():
    global analyser, srv
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=engine.SETTINGS.get("port", 8765))
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()
    analyser = engine.Analyser()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        url = "http://127.0.0.1:{0}/".format(args.port)
        print("Simetrics is already running - opening", url)
        if not args.no_open:
            webbrowser.open(url)
        return
    url = "http://127.0.0.1:{0}/".format(args.port)
    print("Simetrics on", url)
    print("Lap data:", engine.DATA_DIR)
    print("Close this window to stop it.")
    if not args.no_open:
        threading.Timer(0.6, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    if RESTART.is_set():
        print("Restarting Simetrics to load the changed modules...")
        sys.stdout.flush()
        if os.environ.get("SIMETRICS_SUPERVISED"):
            sys.exit(3)                 # Start Simetrics.bat starts it again
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--no-open", "--port", str(args.port)])


if __name__ == "__main__":
    main()
