# Simetrics

Driving telemetry for sim racing games, in one folder: a game-agnostic core (lap analysis and a local web
app) plus one module per game (Assetto Corsa and Forza Horizon 6 so far), each with its own logger. The web app shows your laps, an animated replay against your best lap,
corner-by-corner breakdowns and how your habits change over time. Units are a setting (⚙): metric,
imperial, or mph + metres. Needs Python 3.8+ (stdlib only, nothing to install).

## Folder layout

```
Start Simetrics.bat        opens the app in your browser (http://127.0.0.1:8765)
app/                    the web app (server.py, engine.py, trends.py, web/)
analysis/               lap_analysis.py (corner analysis, HTML reports, module loader), LAP_CSV_SCHEMA.md
modules/                one folder per game (see modules/README.md for the adapter interface)
  ac/                   Assetto Corsa: module.json, adapter.py, logger/ (in-game app + install_ac_app.bat)
  fh6/                  Forza Horizon 6: module.json, adapter.py, logger/ (start_logger.bat, probe.bat)
registry/               the module registry (registry.json + module zips) that the Modules page installs from
tools/                  build_module.py: packages a module for the registry
data/ac/, data/fh6/     your laps, one CSV per lap: <car>@<track>/<session>_L<NN>_<time>.csv
data/studio/            app settings, your names and pictures, analysis cache
```

Everything personal is in `data/`, which git ignores.

## Setup

1. **Assetto Corsa:** run `modules/ac/logger/install_ac_app.bat` once. AC only loads apps from its own
   `apps/python` folder, so this links `apps/python/LapLogger` to `modules/ac/logger/LapLogger` (a directory
   junction, so there is still only one copy) and tells the app to save laps to `data/ac`.
   Pass the AC folder if it isn't the Steam default: `install_ac_app.bat "D:\Games\assettocorsa"`.
   Then turn the app on in AC: Settings > General > UI Modules > LapLogger.
2. **Forza Horizon 6:** in the game, Settings > HUD and Gameplay > Data Out = On, IP 127.0.0.1,
   port 5300. Run `modules/fh6/logger/start_logger.bat` while you drive. Laps go to `data/fh6`.
3. Run `Start Simetrics.bat`.

Only want one game? Move the other module's folder out of `modules/` and restart Simetrics. That game leaves
the library, filters and settings; its laps stay in `data/`, and putting the folder back brings them back.
The **Modules** page lists installed modules, their versions and compatibility, and any lap folders
without a module.

## Installing and updating game modules

On the **Modules** page, *Check for updates* reads the module registry (`registry/registry.json` in this
repository; nothing is contacted until you press it). From there you can install a game, update one, remove
one, or roll back to a saved version. Each change:

1. downloads the module zip (size-capped) and checks its SHA-256 against the registry;
2. unpacks it into a staging folder, refusing paths outside it, links and oversized archives;
3. checks its `module.json` matches the registry, then test-loads it in a separate Python process;
4. refuses while the game or its logger is running (for example AC, or the FH6 logger);
5. moves the current folder to `data/studio/modman/backups/` and the new one into `modules/`, copying
   settings that live inside the module (such as the AC app's `laplogger.ini`) across;
6. test-loads it again in place, and puts the old folder back if that fails. Simetrics then restarts by itself.

Removing a module also moves it to the backups, so it can be restored. The last three versions of each
module are kept. Your laps in `data/` are never touched, and every change is logged to
`data/studio/modman/modules.log`. Modules run Python code on your PC, so only use a registry you trust:
the hash proves you got the zip the registry lists, and the registry's trust rests on this GitHub repository.

**Wheel, controller or keyboard:** any device the game supports works, because the loggers record what the
game reports after its own input mapping. Each lap records which device it was (`input_device`), so the
analysis judges it fairly: a keyboard has no pedal pressure, and a controller's steering is a stick, not a
wheel angle. AC reads it from its own input settings. FH6 doesn't report it, so pass
`start_logger.bat --device wheel|gamepad|keyboard` (or set `"fh6_input_device"` in `data/studio/settings.json`);
without that, each lap is detected from its pedal and steering data. Laps logged before this have no device:
tag them with `py -3.12 analysis/lap_analysis.py [--game fh6] tag-device --device wheel` (or `--device auto`).

If AC is not in the Steam default folder, set `ac_install` in `data/studio/settings.json` (used for track
maps and car pictures, which are read from the game and never copied).

## In the app

- **Library:** cards are grouped by input device (wheel, then controller, then keyboard & mouse), with a
  device filter when you've driven on more than one. Pick a game, then filter by event type (AC: circuit, drift, touge, ...; FH6: circuit,
  sprint, free roam), class, car, and under *More filters* drivetrain, date and FH6 event type.
  Group by track or by car. ✎ on a card renames it, sets an FH6 route's type (Road, Street, Dirt,
  Cross Country, Touge, Drag) or adds a picture (resized to ~30 KB). Its *Delete…* button
  permanently removes that track's (or car's) laps with their reports, analysis cache and corner map
  (no Recycle Bin). If several cars/tracks are involved you can untick the ones to keep; the confirm
  button stays off for 2 seconds and "No" is the default. The FH6 route definition in
  `data/fh6/routes.json` is kept, so the logger still recognises the route.
- **Car / track page:** best, typical (median of eligible laps without detected major incidents) and theoretical-best times, lap times
  over time, a map coloured by where you lose time, the corner table, every lap.
- **Lap page:** replay with the reference ghost (space = play/pause, arrows = ±2 s), speed / delta /
  throttle / brake / steering / gear traces, measured differences per corner, pedal and gear data.
- **Trends:** pedal, braking, steering and gear measurements per session for every game, the biggest
  time gaps to your own best corners, and what changed since the previous session.
- The app only measures: it gives no advice, because what's right depends on the driver, the car and the
  goal. **✦ Copy AI prompt** (top right, plus on every car/track and lap page) gives a ready-to-paste
  prompt for any AI agent, filled in for the page you're on (all sessions, one car/track session, or one
  lap vs its reference). Pick your goal (road driving, lap time, or both) and whether you're pasting into an
  agent that can read files on this PC (it gets the file paths and the `lap_analysis.py` command) or a chat
  app (it gets the numbers inline). The free tiers of ChatGPT, Claude or Gemini are enough.
- **Skins:** the look lives in `app/web/skins/` (`carbon.css` now). A skin only overrides colour tokens,
  the background and the chrome; chart colours stay the same. Copy `carbon.css` to make another.

Command-line reports still work: `modules/ac/report.bat`, `modules/fh6/report.bat`.
`lap_analysis.py report --json` prints the same statistics the app uses (see `analysis/LAP_CSV_SCHEMA.md`),
and **Download JSON brief** in the AI prompt dialog saves the brief behind the copied prompt. Corner numbering is
pinned per car/track (`data/studio/corner_maps.json`), so T numbers don't shift when you set a new best lap; re-map
from the car/track page if you want. `py -3.12 app/check_briefs.py` checks the briefs against your laps.

## Lap validity and off-track recording

Assetto Corsa logger 1.5 retains every completed observed lap, including invalid laps and their elapsed times.
It monitors the native invalidation signal every update, and records independent wheels-out observations,
game penalties and source coverage. More than two wheels outside is an explicit observation rule used by
LapLogger; it is kept separate from the game's own cut decision and configured penalty rules.

Reports distinguish game-invalid, observed off-track, penalty, no issue observed and unverified recordings.
Older AC recordings with `valid=1` are unverified because they only checked one signal. Their samples and
times stay available, with any historical comparison fallback clearly labelled. Forza's rewind/reset evidence
does not certify track limits either.

“Without detected major incidents” excludes major slowdowns as well as spin candidates. Reset/teleport
candidates and serious recording discontinuities are excluded from meaningful pace comparisons. This replaces
the earlier broad “clean” label, which could include slowdowns and resets.

After updating the Python app, start a new AC driving session to load it. A short on-track lap followed by
a deliberate excursion in a solo practice session can verify the live channels and the separate reported
statuses. Automated regression checks live in `modules/ac/tests/test_ac_quality_monitor.py`, `modules/ac/tests/test_ac_logger.py`
and the reporting quality tests. The independent channels are optional; failure is reported as unavailable.
