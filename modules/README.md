# Game modules

Simetrics is a game-agnostic core (`app/`, `analysis/`) plus one module per game in
`modules/<id>/`. The core contains no game names or game branches: the library, filters, colours,
pictures, validity wording and AI briefs all come from the installed modules. Install only the games
you play. Removing a module folder hides that game; its laps stay in `data/<data_folder>/`, and putting
the module back brings them back.

```
modules/<id>/
  module.json     manifest (required)
  adapter.py      hooks (optional; stdlib only)
  logger/         the part that records laps (in-game app, telemetry listener...)
  report.bat      optional command-line report shortcut
  tests/          optional module tests
  web/module.css  optional extra CSS (served at /modules/<id>/web/module.css)
```

At startup the core reads every `modules/*/module.json` (folders starting with `.` or `_` are
skipped), checks `core_api`, and imports `adapter.py`. A module that fails to load is listed on the
Modules page with its error; it never stops Simetrics or the other modules.

## module.json

| key | required | meaning |
|-----|----------|---------|
| `id` | yes | lower-case letters, digits, `-`, `_`; must equal the folder name. Used in combo ids (`<id>~<car>@<track>`), library keys (`<id>:...`), `/img/<id>/...` and the CSS token `--<id>` |
| `name`, `short` | yes | display name and badge text (`Assetto Corsa`, `AC`) |
| `version` | yes | the module's own version |
| `core_api` | yes | the minimum core API it needs (this core: **1**). Newer than the core = not loaded, shown as incompatible |
| `order` | no | display order (lower first; default 100) |
| `data_folder` | no | lap folder under `data/` (default: the id) |
| `headerless_laps` | no | `true` if lap files without a `game=` header line belong to this game (one module at most) |
| `colors` | no | `{"dark": "#…", "light": "#…"}`: the game colour in both themes |
| `settings` | no | default values added to `data/studio/settings.json` (e.g. where the game is installed) |
| `description` | no | one line for the Modules page |
| `logger` | no | `{"kind", "path", "install" or "start", "steps": [...]}`, shown on the Modules page |
| `filters` | no | `disciplines` (route types the user can set), `discipline_label`, `discipline_tip` |
| `ui` | no | `empty_hint` (shown when there are no laps), `picture_hint` (edit dialog), `class_colors` ({class: colour}) |
| `notes` | no | wording joined from every module: `track_status` and `handbrake` (AI brief and metric definitions), `route_names` (brief label note). A note is a string, or `{"order": n, "text": "..."}` to place it before or after other modules' notes |
| `provenance` | no | {channel: where the value comes from in this game}, copied into the AI briefs |
| `preserve` | no | files inside the module that hold the user's settings (e.g. `logger/LapLogger/laplogger.ini`): left out of packages and copied across on update and rollback |
| `busy` | no | `{"processes": ["acs.exe"], "command_line": ["fh6_laplogger.py"]}`: install, update and remove are refused while a matching process runs |

## adapter.py hooks

All hooks are optional. A missing hook gets the core default (`DEFAULT_HOOKS` in
`analysis/lap_analysis.py`). Hooks receive plain data and return plain data. Hooks that build laps
get the core module as their first argument, so an adapter never imports the core.

| hook | used by | signature, and the default when it's missing |
|------|---------|----------------------------------|
| `classify_validity` | validity of every lap | `(meta, obs) -> (status, label, pace_eligible)` or `None`. Called only when the game's own invalid flag is clear. `obs` has `recorded`, `outside`, `penalty`, `coverage`, `source` and `game_source`. Default: observed tyres outside, penalty, then observed-clear or unverified |
| `VALIDITY_SCOPE` | lap validity | text, default `observed track limits and game flags` |
| `PACE_SCOPE` | comparison population label | default `observed-clear laps (not game certification)` |
| `REFERENCE_ROLE` | reference lap label | default `fastest observed-clear lap (not game certification)` |
| `CUT_TEXT` | per-corner difference when the lap was invalidated there | default `lap invalidated here` |
| `REFERENCE_OPTIONS`, `reference_lap` | `lap_analysis.py report --ref <name>` | `{name: description}` and `(core, name, laps) -> Lap or None` |
| `legacy_data_dir` | CLI default folder when `data/<folder>` doesn't exist | `() -> Path or None` |
| `display_info` | library names, classes, pictures | `(game, car, track, layout, lapmeta, lib, settings) -> dict` with `car_key`, `track_key`, `car_name`, `brand`, `track_name`, `event`, `discipline`, `car_class`, `class_sort`, `drivetrain`, `car_img`, `track_img`, `skins`, `skin`, `map`, `car_detail` (optionally `pi`). `lib` is the user's library edits. Default: names from the lap header |
| `image_file` | `/img/<id>/<parts...>` | `(parts, settings) -> Path or None`. The server has already rejected unsafe path parts |
| `brief_identity` | AI brief identity | `(combo) -> dict` of extra fields |
| `brief_identity_lines` | rendered AI prompt | `(identity) -> [str]` |

The lap file format is shared by all games (`analysis/LAP_CSV_SCHEMA.md`). A logger writes laps to
`data/<data_folder>/<car>@<track>/` with `game=<id>` in each file header. The core then handles
corners, comparisons, habits, devices, units, the replay and the AI prompt.

## Publishing a module

```
py -3.12 tools/build_module.py <id> --out registry
```

writes `registry/<id>-<version>.zip` and adds the version to `registry/registry.json` (SHA-256, size,
`core_api`, date). The zip is built the same way every time and leaves out caches, dot files and the
`preserve` files. A version number can't be republished with different files: raise `version` in
`module.json` first. Commit both files; the Modules page offers the newest version whose `core_api` the
user's Simetrics supports. Registry format:

```json
{"format": 1, "name": "...", "modules": [
  {"id": "ac", "name": "...", "short": "AC", "description": "...",
   "versions": [{"version": "1.0.0", "core_api": 1, "url": "ac-1.0.0.zip",
                 "sha256": "<64 hex>", "size": 20766, "released": "2026-10-29"}]}]}
```

A `url` is https://, or relative to the registry file. Simetrics records what it installed in
`modules/<id>/.install.json`, so it can warn about files changed by hand before an update.

## Tests

`py -3.12 analysis/test_modules.py` checks loading, failures, defaults and removal.
`py -3.12 app/test_modman.py` checks packaging, install, update, rollback, remove and the refusals
(wrong hash or size, unsafe zips, too-new modules, broken modules, a running game). Module tests live
in `modules/<id>/tests/`.
