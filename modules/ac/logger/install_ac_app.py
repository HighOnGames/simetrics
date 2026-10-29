"""Install the AC LapLogger in-game app from Simetrics.

AC only loads Python apps from <AC>/apps/python/<Name>/, so this makes
<AC>/apps/python/LapLogger a directory junction to modules/ac/logger/LapLogger in this folder
(one copy of the app, kept with Simetrics) and writes modules/ac/logger/LapLogger/laplogger.ini
so laps are saved to Simetrics' data/ac folder.

    py -3.12 install_ac_app.py ["C:\\path\\to\\assettocorsa"]

An existing real (non-junction) apps/python/LapLogger folder is moved to data/studio/backup/.
Then enable the app once in AC: Settings > General > UI Modules > LapLogger (or cfg/python.ini).
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDIO = HERE.parents[2]          # Simetrics (this file is in modules/ac/logger/)
APP_SRC = HERE / "LapLogger"
DATA = STUDIO / "data" / "ac"
SETTINGS = STUDIO / "data" / "studio" / "settings.json"
DEFAULT_AC = Path(r"C:\Program Files (x86)\Steam\steamapps\common\assettocorsa")


def is_junction(p):
    try:
        return bool(os.lstat(p).st_file_attributes & 0x400)     # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def find_ac(argv):
    cands = [Path(argv[1])] if len(argv) > 1 else []
    try:
        cands.append(Path(json.loads(SETTINGS.read_text(encoding="utf-8")).get("ac_install", "")))
    except (OSError, ValueError):
        pass
    cands.append(DEFAULT_AC)
    for c in cands:
        if (c / "content" / "tracks").is_dir() and (c / "apps" / "python").is_dir():
            return c
    sys.exit("Assetto Corsa not found. Run: py -3.12 install_ac_app.py \"C:\\path\\to\\assettocorsa\"")


def main():
    ac = find_ac(sys.argv)
    DATA.mkdir(parents=True, exist_ok=True)
    (APP_SRC / "laplogger.ini").write_text(
        "; written by install_ac_app.py - where the in-game app saves laps\ndata_dir={0}\n".format(DATA), encoding="utf-8")
    target = ac / "apps" / "python" / "LapLogger"
    if is_junction(target):
        if Path(os.path.realpath(target)) == APP_SRC.resolve():
            print("Already linked:", target, "->", APP_SRC)
        else:
            os.rmdir(target)            # removes the link only, never the folder it points to
    elif target.exists():
        backup = STUDIO / "data" / "studio" / "backup" / time.strftime("ac_app_%y%m%d-%H%M%S")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(backup))
        print("Moved the old in-game app folder to", backup)
    if not target.exists():
        subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(APP_SRC)], check=True)
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    docs = [Path(os.environ[v]) / "Documents" for v in ("OneDrive", "OneDriveConsumer") if os.environ.get(v)]
    inis = [d / "Assetto Corsa" / "cfg" / "python.ini" for d in docs + [home / "OneDrive" / "Documents", home / "Documents"]]
    enabled = any(i.is_file() and "[LAPLOGGER]" in i.read_text(errors="replace").upper() for i in inis)
    print("Laps will be saved to", DATA)
    if not enabled:
        print("Enable the app in AC: Settings > General > UI Modules > LapLogger.")
    if SETTINGS.parent.is_dir():
        try:
            s = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
        except ValueError:
            s = {}
        s["ac_install"] = str(ac)
        SETTINGS.write_text(json.dumps(s, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
