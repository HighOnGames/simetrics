# LapLogger lap CSV schema (shared by all games)

One CSV file per lap or run. Writers:

| game | writer | data folder |
|------|--------|-------------|
| `ac`  | `modules/ac/logger/LapLogger/LapLogger.py` (in-game app; `assettocorsa/apps/python/LapLogger` is a junction to it, made by `modules/ac/logger/install_ac_app.bat`) | `data/ac/` |
| `fh6` | `modules/fh6/logger/fh6_laplogger.py` (UDP "Data Out" listener) | `data/fh6/` |

Each game is a module in `modules/<id>/` (see `modules/README.md`); a new game adds a row here.

All paths are relative to the Simetrics folder: the loggers, the analysis and the lap data all live in it.
Before 2026-10-28 laps were saved under `Documents/Assetto Corsa/laplogger/` and `Documents/Forza LapLogger/laplogger/`.

Reader: `lap_analysis.py` (same folder as this file), `--game ac|fh6`.
Files with no `game` key come from AC LapLogger 1.1 or older; treat them as `ac` (the module with `headerless_laps`).

## Layout

```
data/<game>/<car>@<track>/<session>_L<NN>_<m-ss-mmm>[_INVALID].csv
data/<game>/reports/               HTML reports from lap_analysis.py
data/fh6/routes.json, cars.json    FH6 route and car names
```

Each file starts with `# key=value` lines (sorted by key), then one header row, then one row per sample.
Samples are in time order. AC logs at about 50 Hz; FH6 sends about 46 Hz. Analyse by distance, not by sample index.

## Meta keys

| key | all games | meaning |
|-----|-----------|---------|
| `game` | yes* | `ac`, `fh6` |
| `logger_version` | yes | writer version |
| `car`, `track`, `layout` | yes | names; `layout` may be empty. FH6: `car` comes from `cars.json` (default `car<ordinal>`), `track` is the route name from `routes.json` (`circuit-N`, `sprint-N`, `freeroam-N` until renamed) |
| `driver` | yes | may be empty (FH6) |
| `session` | yes | `yymmdd-HHMMSS` when the logger started; a group of laps driven together |
| `lap_number` | yes | 1-based, counting within session + car@track folder |
| `lap_time_ms` | yes | official lap/run time (the game's own timer where it has one) |
| `valid` | yes | `1` / `0`, see *Invalid* below |
| `track_length_m` | yes | length of the game's track spline; `0.0` = unknown (AC track without AI line, all FH6) |
| `sample_hz` | yes | sample rate (AC: nominal; FH6: measured over the lap) |
| `splits_ms` | yes | `;`-separated sector times (AC), empty if none |
| `closed` | fh6 | `1` = lap that ends where it started, `0` = point-to-point run. Missing = `1` |
| `kind` | fh6 | `circuit`, `sprint`, `freeroam_loop`, `freeroam_sprint` |
| `standing_start` | fh6 | `1` for lap 1 of a race or a sprint (started from the grid) |
| `finish_check` | fh6 sprints | how the finish was confirmed: `route` (ended within 50 m of the route's known finish), `speed` (first run of a route: ended at > 20 mph and not braking hard), `flag` (race clock froze) |
| `race_lap` | fh6 | the game's own lap number within the race |
| `rewinds` | fh6 | number of rewinds/teleports in this lap |
| `run_distance_m` | fh6 | distance driven in this lap (from positions) |
| `start_x`, `start_z` | fh6 | where the lap/run started (world metres) |
| `car_ordinal`, `car_class`, `car_pi`, `drivetrain` | fh6 | Forza car id, class (0=D … 7=X), PI, drivetrain (0 FWD, 1 RWD, 2 AWD) |
| `steer_lock_deg` | fh6 | wheel rotation used to convert steering input to degrees |
| `slip_units` | fh6 | `normalized` = `sa_*`/`sr_*` are Forza's normalised slip (see the slip note). Missing = AC units |
| `input_device` | yes (AC 1.4+, FH6 1.3+) | `wheel`, `gamepad`, `keyboard` (includes mouse steering) or `unknown`. Missing = `unknown`. See *Input device* below |
| `input_device_source` | yes | how it was decided: `game_config` (AC `cfg/controls.ini`), `setting` (FH6 `--device` or Simetrics' `fh6_input_device`), `heuristic` (from the lap's own data), `user` (added later with `lap_analysis.py tag-device`), `none` |
| `input_device_heuristic`, `input_device_evidence` | fh6 | what the data suggests (also recorded when a setting decided, so a wrong setting shows up) and the measurements behind it |
| `input_detail` | when known | hardware or mode, e.g. `Logitech G920` (AC: the steering controller's name), `XInput`, `mouse steering` |
| `input_method_raw`, `clutch_axis`, `auto_clutch` | ac 1.4+ | AC's `INPUT_METHOD` as written; `1` = a clutch axis is mapped (wheels); AC's auto-clutch assist (`cfg/assists.ini`) |
| `input_device_tagged` | back-filled laps | date `tag-device` added the device |
| `rpm_max`, `rpm_idle` | fh6 1.2+ | the engine's redline and idle as the game reports them (for shift-point analysis). AC: not available to Python apps; use the highest `rpm` seen |
| `game_validity_source`, `game_validity_coverage`, `game_invalid_seen` | ac 1.5+ | Native `ac.LapInvalidated` source, coverage (`complete`, `partial`, `unavailable`) and latched positive invalidation. The signal is polled every app update, independently of CSV sampling. A zero is not an authoritative certification that the game accepted a lap. |
| `track_limits_source`, `track_limits_coverage`, `track_limits_rule` | ac 1.5+ | Independent wheels-out source and monitoring coverage. The explicit coaching rule is `more_than_2_tyres`; it is an observation rule, separate from the game's penalty/validity decision. Missing or failed reads remain unavailable. |
| `off_track_events`, `off_track_s`, `max_tyres_out` | ac 1.5+ | Observed excursions under that rule, estimated duration and maximum wheels out. Unknown monitoring is not zero excursions. A return to known on-track state separates episodes; missing reads do not create extra episodes. Known pit states are excluded. |
| `penalty_seen`, `penalty_max_s` | ac 1.5+ | Whether a positive game penalty or penalty flag was observed, and greatest observed penalty time. These do not identify the reason for a penalty. |
| `game_allowed_tyres_out`, `game_penalties` | ac 1.5+ when readable | Snapshot of the session's game configuration. Recorded separately from the independent observation rule; not altered by the logger. |

\* missing in AC files written before logger 1.2.

## Columns

Common columns, in this order, for every game:

| column | unit / range | definition |
|--------|--------------|------------|
| `t_ms` | ms | time since the lap/run started, on the game's lap timer where available |
| `pos` | 0..1 | normalised position along the track spline; `0` when the game has none (derive from `x`/`z`, see below) |
| `speed` | km/h | ground speed (kept in km/h because the analysis thresholds are tuned for it) |
| `speed_mph` | mph | the same speed in mph (show this to the user) |
| `gas` | 0..1 | throttle pedal |
| `brake` | 0..1 | brake pedal |
| `clutch` | 0..1 | **1 = clutch engaged (pedal up)**, 0 = pedal fully down (AC convention; Forza's 0..255 is inverted) |
| `steer` | degrees | **steering-wheel angle, + = right**. AC reports it directly. FH6 sends a -127..127 input, converted as `input/127 × steer_lock_deg/2` |
| `gear` | int | **-1 = reverse, 0 = neutral, 1 = 1st** … (Forza: 0 = R and 11 = N, remapped). FH6 also reports N for about 0.1 s during every shift: ignore 1-3 sample N blips when counting shifts |
| `rpm` | rpm | engine speed |
| `x`, `y`, `z` | m | world position in a **right-handed, y-up frame (AC's)**: drawn as a map, x points right and z points down. FH6's world is left-handed, so its z is negated. Positions are comparable only within one game and track |
| `g_lat` | g | lateral acceleration, **< 0 when turning right** (AC convention; Forza `-accX/9.80665`) |
| `g_lon` | g | longitudinal acceleration, **+ = accelerating**, − = braking |
| `sa_fl` … `sa_rr` | degrees (AC) / normalised (FH6) | tyre slip angle, front-left, front-right, rear-left, rear-right (see the slip note) |
| `sr_fl` … `sr_rr` | ratio (AC) / normalised (FH6) | tyre slip ratio, 0 = rolling freely, + = wheelspin, − = locking (see the slip note) |
| `invalid` | 0/1 | 1 from the moment the lap became invalid (stays 1 for the rest of the lap) |

Extra columns follow the common ones. Readers must look columns up by name and ignore any they don't know.
A value of `nan` means that game build didn't provide the channel.

**Shared extras** (AC LapLogger 1.3+, FH6 LapLogger 1.2+; same names, units and signs):

| column | unit | definition |
|--------|------|------------|
| `beta` | degrees | **body slip / drift angle**: direction of travel vs where the nose points. + = sliding to the right, 0 below 3 m/s. It has the same sign as `g_lat` in a corner. This is the cross-game slide measure. FH6 also has it from logger 1.1. |
| `yaw_rate` | deg/s | how fast the car rotates, **+ = turning right** (opposite sign to `g_lat`) |
| `ns_fl` … `ns_rr` | normalised | combined tyre slip, 1 = at the grip limit (AC `NdSlip`; FH6 `TireCombinedSlip`, which equals √(sa² + sr²) of its normalised values). The closest thing to a per-tyre "over the limit" measure that both games share. Values below about 1.5 compare across games; above that, read it only as "sliding": FH6 peaks around 100, while AC goes up to about 1e6 when a lightly loaded wheel locks (`sr` = −1). Clamp it (for example to 3) or use "metres with `ns` > 1" when comparing |
| `tyre_t_fl` … `tyre_t_rr` | °C | tyre temperature (AC: core; FH6: its single `TireTemp`, converted from °F). The layers differ, so compare trends, not absolute values |
| `susp_fl` … `susp_rr` | m | suspension travel. Zero points differ between games, so compare changes (weight transfer, kerbs), not levels |

**Game-only extras:**

| column | game | definition |
|--------|------|------------|
| `handbrake` | fh6 | 0..1 |
| `tyres_out` | ac 1.5+ | Count of wheels reported outside track, 0–4; `nan` if unavailable. CSP's optional direct getter is preferred, with original-AC shared physics memory as fallback. |
| `penalty_s`, `flag` | ac 1.5+ | Original-AC graphics shared-memory penalty time and game flag. `nan` means unavailable. A penalty flag is not automatically a track cut. |
| `game_invalid` | ac 1.5+ | Raw native invalidation sample: 0/1, or `nan` if the source failed. The older `invalid` column is the within-lap latch, so a transient invalidation cannot disappear. |
| `track_limits_exceeded` | ac 1.5+ | Within-lap latch for the independent more-than-two-wheels rule. A positive observation remains 1 for the lap. Unknown is `nan` until a usable observation exists. |
| `steer_in` | fh6 | raw steering input, -1..1 (+ = right) |
| `line_offset` | fh6 1.2+ | Forza `NormalizedDrivingLine`, -1..1: distance from the game's racing line (meaning of the sign not verified) |
| `ai_brake_diff` | fh6 1.2+ | Forza `NormalizedAIBrakeDifference`, -1..1: your braking compared with the AI's (meaning not verified) |

AC 1.3 signs (`beta` from `LocalVelocity`, `yaw_rate` from `LocalAngularVelocity`) were **confirmed on 14 real laps** (Adam Cup @ drift, 2026-10-27). On every lap, heading rebuilt from x/z minus `beta` differentiates to `yaw_rate` (r = 0.98 to 0.995, median error about 1 deg/s, which held up through a ±90° spin). `corr(yaw_rate, g_lat)` is −0.54 to −0.97, `corr(beta, g_lat)` is mostly positive, and `beta` is 0.0–0.2° on straights. FH6's signs were confirmed on real data (correlation −0.79 for `yaw_rate`, +0.40 for `beta`).

## Channel matrix across games

What each game can provide, so every new logger targets the same channels. iRacing entries are the
planned mapping from its SDK variables and must be verified when that logger is built.

| channel | AC | FH6 | iRacing (planned) | notes |
|---------|----|-----|-------------------|-------|
| time, speed, pedals, gear, rpm | yes | yes | `LapCurrentLapTime`, `Speed`, `Throttle`, `Brake`, `Clutch`, `Gear`, `RPM` | iRacing `Clutch`: 1 = engaged, the same as AC (verify) |
| steering wheel angle | yes (deg) | input × lock | `SteeringWheelAngle` (rad, + = left: negate) | |
| track position `pos` | AI spline (if any) | none: project x/z | `LapDistPct` directly | iRacing has no live world x/z; use `LapDistPct` × track length |
| world x/y/z | yes | yes (z negated) | no (only `Lat`/`Lon` in .ibt files) | maps for iRacing would come from lat/lon |
| `g_lat`, `g_lon` | yes | yes | `LatAccel`, `LongAccel` (m/s²) | check the signs |
| `beta` | 1.3 | yes | from `VelocityX`/`VelocityY` (body frame) | **the main cross-game slide metric** |
| `yaw_rate` | 1.3 | yes | `YawRate` (rad/s, + = left: negate) | |
| slip angle / ratio per tyre | deg / ratio | normalised | not exposed | compare within a game only |
| `ns_*` normalised slip | 1.3 | yes | not exposed | |
| tyre temperature | 1.3 (core) | yes (one value) | only updated in the pits while live | |
| suspension travel | 1.3 | yes | `LFshockDefl` … | |
| handbrake | no Python API | yes | `HandbrakeRaw` | |
| lap validity / off-track | Native invalidation plus independent wheels-out and penalty evidence (1.5+) | rewind/reset evidence only | `PlayerTrackSurface` off-track / incidents | Game validity, track observations and incidents are separate dimensions. |
| ABS / TC working | no | no | `BrakeABSactive` | game-specific |
| assists in use | not in the API (AC 1.4 records `auto_clutch` from `assists.ini`) | not sent | partly in session info | record by hand per session for fair comparisons |
| input device | `controls.ini` `INPUT_METHOD` | not sent: setting or heuristic | not in the SDK (setting or heuristic) | `input_device` |
| redline | no | `rpm_max` | `DriverInfo` `DriverCarRedLine` | |

## Input device

The loggers never read the controller: they record what the game reports **after its own input mapping**
(`gas`, `brake`, `clutch`, `steer`, and FH6's `steer_in` / `handbrake`). Any device the game supports gives the
same columns. `input_device` says which device it was, so the data can be judged fairly:

| device | pedal pressure | steering | how it's judged |
|--------|----------------|----------|-----------------|
| `wheel` | yes | degrees of wheel rotation | everything, as before |
| `gamepad` | yes (analogue triggers) | stick position: `steer` degrees are only "input × an assumed lock" | brake/throttle checks apply; steering only as **% of full lock** (`steer_in`, or AC `steer` ÷ the lap's largest angle), never compared with wheel laps |
| `keyboard` | **no** (keys are on/off) | steps, or follows the mouse | pressure checks (peak brake input, soft braking) skipped; brake points, coasting, apex speed, full-throttle point and the line still apply |
| `unknown` | assumed | assumed degrees | judged like a wheel (laps logged before the device was recorded) |

Detection:

* **AC:** `Documents/Assetto Corsa/cfg/controls.ini`, `[HEADER] INPUT_METHOD`, read when the session starts (AC only
  reloads it then): `WHEEL` → `wheel`, `X360` → `gamepad`, `KEYBOARD` → `keyboard` (`[KEYBOARD] MOUSE_STEER=1` →
  `input_detail=mouse steering`). These three are what AC and Content Manager write; any other value is kept in
  `input_method_raw` and becomes `gamepad` if it names a pad, else `unknown`.
* **FH6:** the telemetry doesn't say. `fh6_laplogger.py --device wheel|gamepad|keyboard` (or `fh6_input_device` in
  `data/studio/settings.json`) sets it; otherwise each lap is classified from its data by `lap_analysis.classify_device()`:
  * keyboard: throttle (and brake) on/off: under 8% (12%) of the pressed samples between 10% and 90%;
  * gamepad: steering that snaps from over 40% to the centre, or flicks from the centre to over 60%, within 80 ms, at least
    twice a minute (a released thumbstick; a wheel can't turn that fast);
  * wheel: analogue throttle (or brake, on flat-out laps) and no snaps (under once a minute);
  * otherwise `unknown`. On the real data so far (23 FH6 laps, 58 AC laps on a Logitech G920) every lap came out `wheel`.
    A game that smooths a pad's stick before sending it looks like a wheel, so a setting beats the heuristic.
* **Older laps:** `lap_analysis.py [--game fh6] tag-device --device wheel|gamepad|keyboard|auto [--combo ...] [--session ...]`
  writes the keys into the header (samples and file time unchanged). Readers never guess on the fly: a lap without the key is `unknown`.

What changes in the analysis (`lap_analysis.py`, the same in the app and the briefs):

* `corner_metrics(g, corner, device)`: `brake_peak` is `null` for keyboard; `steer_max` / `steer_work` (degrees) are `null`
  for gamepad and keyboard, and `steer_max_pct` / `steer_work_pct` (% of full lock) are given for every lap that has steering.
* `advice_items(..., device, ref_device)`: pedal pressure (`brake_soft`) and steering (`steer_corrections`) are only compared
  between laps on the same kind of device (never pressure on a keyboard); a pad is compared in % of lock. Distance and timing
  tips compare across devices.
* `HABIT_CHECKS`: on a gamepad or keyboard, brake + throttle overlap is two triggers or keys, not left-foot braking, so under
  `road` it is information only, and under `race` it is brake dragging (over 0.5 s). Clutch checks only apply when a clutch is
  in use: a wheel without auto-clutch, or a clutch that actually goes down while moving.
* Sessions are judged by the device most of their laps were driven with (`dominant_device()`).

`analysis/test_devices.py` checks all of this on synthetic wheel, gamepad and keyboard laps.

## Invalid laps

The recorded `valid` header is preserved separately from the analysis's validity assessment:

* **AC:** `valid=0` / a positive `invalid` sample is evidence of native game invalidation. Legacy `valid=1` records only that this one signal did not report invalidation; it cannot certify that no cut occurred. Those laps are **unverified**, not retroactively invalid. Version 1.5 also records independent off-track and penalty evidence and coverage. A fully monitored lap with no issues is labelled **no issue observed**, not certified by the game's timing system. Observed off-track or penalty laps are excluded from meaningful pace populations even if the native header still says 1.
* **FH6:** a rewind was used (race clock went backwards), or in free roam the car was teleported
  (rewind / reset to road). Horizon has no track-limits rule. Pausing during a free-roam run drops the run instead.

Laps that were not seen from start to finish (out-laps, logger started mid-lap, restarts) are never written.

Completed invalid laps are retained, including their elapsed time and samples. Finishing a lap and having a recorded time does not mean the game accepted it for best-lap timing. Original AC shared memory has wheels-out and penalty fields; ACC's `isValidLap` field must not be assumed to exist in original AC. Layout reference: https://github.com/ac-custom-shaders-patch/acc-extension-apps/blob/master/apps/python/AccExtHelper/sim_info.py

**Incident-free is a separate assessment.** Public reports use “without detected major incidents” rather than claiming “clean.” Spin candidates, big slowdowns, reset/teleport candidates and serious data discontinuities exclude a lap from the incident-free typical-lap population. An unverified historical fallback is explicitly labelled. Track observations cannot be reconstructed from old missing channels, and a period with an unavailable source never silently becomes confirmed on-track.

## Laps vs runs

* **AC:** a lap is from one crossing of the start line to the next, so `closed=1`.
* **FH6 circuit race:** split by the game's lap counter, so `closed=1`. Lap 1 has `standing_start=1`.
* **FH6 sprint race:** one run from the start to the finish, so `closed=0`.
* **FH6 free roam:** the driver sets lines with hotkeys. F9 sets a start/finish line, giving `freeroam_loop` laps with `closed=1`. F10 adds a finish line, giving `freeroam_sprint` runs with `closed=0`.

## Track position when `pos` is 0

When `track_length_m` is 0 or `pos` doesn't span the lap, `lap_analysis.py` projects every sample's
`x`/`z` onto the reference lap's driven path to get distance along the lap. Closed laps wrap around;
`closed=0` runs use the open path. A cross-game tool should do the same rather than trusting `t_ms` or sample index.

## Units for display

Show speeds in **mph** and distances (brake points, corner positions) in **metres**.

## Coaching focus (road / race / mix)

The analysis judges the same data differently depending on what the driver wants, set by one switch
(`lap_analysis.py report --focus road|race|mix`, default `mix`). A UI toggle should pass the same value.

| focus | for | habit checks | corner tips |
|-------|-----|--------------|-------------|
| `road` | improving real-world driving | judged for the road (`Road habits`) | lap-time-only tips hidden |
| `race` | lap time in the sim | judged for racing (`Technique checks`) | all tips |
| `mix` | both | judged for the road | all tips; lap-time-only ones labelled `(track)` |

* Every corner tip from `advice_items()` has an `id`, `text` and `scope`. The scope is `race` (only about lap time:
  brake points, apex and exit speed, throttle timing, coasting in gear, cuts) or `both` (also a road habit:
  soft braking, sliding, steering corrections, clutch held down). `apply_focus(items, focus)` filters and labels them.
* `HABIT_CHECKS` gives every per-lap habit measurement its own label, warning threshold and meaning per focus.
  For example, brake + throttle overlap is **left-foot braking** under `road` (warn above 0.1 s per lap). Under
  `race`, left-foot braking is normal, so only **brake dragging** (overlap above 0.5 s per lap) is flagged.
  Handbrake pulls are a warning under `road` and information only under `race`.
* Coasting in gear (off both pedals) is engine braking: good anticipation on the road but slow on track, so it is `race` scope.
  Coasting with the **clutch down** or **in neutral** is flagged in every focus.
* To add a rule, tag it with a scope, or give it a definition per focus. Never give road-unsafe advice
  (left-foot braking, handbrake turns, trail braking) without a `race` scope.

## Slip note

* **AC:** `sa_*` are the physics slip angles in degrees, and `sr_*` the slip ratios.
* **FH6** (`slip_units=normalized`): Forza sends its own *normalised* slip: 0 = no slip, **1.0 = at the
  tyre's grip limit**, > 1 = past it, sliding or spinning. In real FH6 data, `|sa|` has a median of about 0.2,
  wheelspin at launch reaches `sr` 15–22, and lock-ups reach about −22. This can't be converted to degrees or a
  true ratio without the tyre model, so it is stored as it is. `lap_analysis.py` uses 1.0 as its "rear sliding"
  threshold for FH6, and 8 degrees for AC.
* To compare across games, use the **trend within a game** (for example, rear slip vs your own reference lap),
  or `beta` / `g_lat`, which use the same units everywhere.
* In both games, the sign of `sa_*` depends on the side; compare magnitudes.

## How FH6 fields were verified

These were checked against real FH6 packets on 2026-10-27, from a 3-minute capture plus two logged race laps:

* The packets are 324 bytes, and the dash block starts at byte 244 (FH4/FH5 layout). The dash `speed` equals |velocity|, and integrated speed matches the distance from position to within 0.1%.
* All-zero packets (`IsRaceOn` 0) arrive while the game is paused or in a menu.
* `CurrentRaceTime` also counts in free roam. A race is detected when `RacePosition` > 0, `CurrentLap` > 0 or `LapNumber` > 0. `CurrentLap` is the lap timer, and it reads 0 for one packet at each line crossing.
* At the finish, FH6 cuts straight to all-zero packets (the finish cutscene), with no frozen clock first. It resumes in free roam about 36 s later, with the car where it crossed the line. Leaving through the pause menu looks the same in the packets, so a sprint's completeness is judged from the last live packet, as described for `finish_check`.
* In the sprint tested (5.8 km, 8 cars), `LapNumber` stayed 0 throughout.
* Heading is (sin yaw, cos yaw) in the game's x/z. Yaw increases when `AngularVelocityY` > 0, `AccelerationX` > 0 and steer > 0 (84–96% sign agreement while turning). This matches steer −127 = left and local X = right, so the world is left-handed. The AC frame was checked by overlaying a lap on the track's `map.png` using `map.ini`.
* Gears: reverse is 0 (rolling backwards), and neutral is 11. Clutch and handbrake run 0–255 (255 = pressed).

## Session statistics, corner maps and briefs (Simetrics)

* **One definition everywhere.** `lap_analysis.comparison_laps()` picks the laps used for corner statistics:
  valid laps that aren't standing starts, falling back explicitly to all valid laps, then all laps.
  `corner_stats()` gives each corner's **gap = median section time minus the fastest section time** over those laps,
  and `theoretical_best()` adds the fastest sections together (illustrative only). The CLI report, the app and its
  exported briefs all use these, so their numbers agree.
* **Pinned corner maps.** `data/studio/corner_maps.json` pins, per combo id (`<game>~<car@track folder>`), the lap that
  defines the distance frame (x/z projection) and the corner numbering, with a version. A faster lap stays the time
  reference but doesn't renumber corners. Re-mapping (in the app) makes a new version; corner labels only match within
  one corner-map id. `lap_analysis.py` reads the same file.
* **Briefs.** The app's ✦ AI prompt and `/api/brief` give a `laplogger-brief` v1 JSON: identity (ids and labels),
  history counts, the comparison population and exclusions, the best lap and the fastest section per corner, values in
  the user's display units, data quality (sample rate, largest gap, speed-vs-position distance check, channels not
  recorded vs constant) and channel provenance (direct / derived / proxy). Unrecorded values are null, never 0.
* **CLI JSON.** `lap_analysis.py report --json` prints the same statistics as JSON (km/h and metres) without writing an
  HTML report; `--out <folder>` writes the HTML elsewhere. `--combo` prefers an exact folder-name match.
* `app/check_briefs.py` runs acceptance checks (history, populations, references, CLI agreement, road scope, missing
  channels, filters) against the real laps.
