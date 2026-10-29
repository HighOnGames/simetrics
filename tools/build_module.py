"""Package a game module for a module registry.

    py -3.12 tools/build_module.py ac                       # dist/ac-<version>.zip + dist/registry.json
    py -3.12 tools/build_module.py ac fh6 --out dist --url-base https://github.com/<owner>/<repo>/releases/download/modules/

The zip has one top folder named after the module id and leaves out caches, dot files and the
manifest's "preserve" files (user settings). It is built the same way every time (sorted names, fixed
timestamps), so the same files give the same SHA-256. The registry entry gets the version, core_api,
url, sha256, size and date. A version already in the registry with a different hash is refused:
publish a new version number instead. Stdlib only.
"""

import argparse
import hashlib
import io
import json
import sys
import time
import zipfile
from pathlib import Path

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO / "app"))
import modman  # noqa: E402


def build_zip(folder, mid):
    """(bytes, manifest) of a module folder's package."""
    man = modman.read_manifest(folder)
    if not man or man.get("id") != mid:
        raise SystemExit("{0}: module.json is missing or its id isn't {1!r}".format(folder, mid))
    files = modman.module_files(folder, man.get("preserve") or ())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, path in sorted(files.items()):
            info = zipfile.ZipInfo("{0}/{1}".format(mid, rel), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, path.read_bytes())
    return buf.getvalue(), man


def add_to_registry(reg, man, entry):
    mods = reg.setdefault("modules", [])
    e = next((e for e in mods if e["id"] == man["id"]), None)
    if e is None:
        e = {"id": man["id"], "versions": []}
        mods.append(e)
    e.update({k: man[k] for k in ("name", "short", "description") if man.get(k)})
    old = next((v for v in e["versions"] if v["version"] == entry["version"]), None)
    if old and old["sha256"] != entry["sha256"]:
        raise SystemExit("{0} {1} is already in the registry with different files: raise the version in module.json"
                         .format(man["id"], entry["version"]))
    if not old:
        e["versions"].append(entry)
        e["versions"].sort(key=lambda v: modman.version_key(v["version"]))
    mods.sort(key=lambda e: e["id"])
    return reg


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ids", nargs="+", help="module ids (folders in modules/)")
    p.add_argument("--modules", default=str(STUDIO / "modules"), help="the modules folder")
    p.add_argument("--out", default=str(STUDIO / "dist"), help="where the zips go")
    p.add_argument("--registry", help="registry file to add to (default: <out>/registry.json)")
    p.add_argument("--url-base", default="", help="prefix for the zip's url (default: next to the registry)")
    p.add_argument("--name", default="Simetrics modules", help="registry name for a new registry")
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    reg_path = Path(args.registry) if args.registry else out / "registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.is_file() else \
        {"format": modman.REGISTRY_FORMAT, "name": args.name, "modules": []}
    for mid in args.ids:
        data, man = build_zip(Path(args.modules) / mid, mid)
        name = "{0}-{1}.zip".format(mid, man["version"])
        (out / name).write_bytes(data)
        entry = {"version": str(man["version"]), "core_api": int(man.get("core_api", 1)), "url": args.url_base + name,
                 "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "released": time.strftime("%Y-%m-%d")}
        add_to_registry(reg, man, entry)
        print("{0}  {1} bytes  sha256 {2}".format(out / name, entry["size"], entry["sha256"]))
    modman.check_registry(reg)
    reg_path.write_text(json.dumps(reg, indent=1) + "\n", encoding="utf-8")
    print("registry:", reg_path)


if __name__ == "__main__":
    main()
