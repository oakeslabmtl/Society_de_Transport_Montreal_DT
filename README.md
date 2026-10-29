# Société de Transport de Montréal Bus Digital Twin
Project for a digital twin (DT) for the Société de Transport de Montréal (STM) bus routes monitoring

## Pre-Requisites

1. [Git](https://git-scm.com/)
2. [Python](https://www.python.org/)
3. [Jupyter](https://jupyter.org/)
4. [Docker](https://www.docker.com/)
5. [RabbitMQ](https://www.rabbitmq.com/)
6. [MongoDB](https://www.mongodb.com/)
7. [Godot](https://godotengine.org/)

## Architecture

![](architecture.png)


## STM API Data
- The STM API uses the [GTFS](https://gtfs.org/) protocol for data exchange.
- The data dictionary for the API can be found [here](https://developers.google.com/transit/gtfs-realtime/reference?hl=en)


## STM API OAuth

The Digital Twin requires an API key for the [STM API](https://portail.developpeurs.stm.info/apihub/#/login). After sign up and getting an account ready,
[create an application](https://portail.developpeurs.stm.info/apihub/#/applications/create) within the API portal to get access to an API Key.

- For `Mac/Linux`:
1. Run the command in your terminal, replacing `APIKey` with your API key:
```
echo "export STM_API_KEY='APIKey'" >> ~/.zshrc
```
2. Update the shell with the new variable by running the command:
```
source ~/.zshrc
```
3. Confirm that you have set your environment variable by running the following command and checking the output:
```
echo $STM_API_KEY
```

- For `Windows`:
1. Run the command in your terminal, replacing `APIKey` with your API key (it applies to terminals opened afterwards):
```
setx STM_API_KEY "APIKey"
```
2. Open a new terminal. To set it for the current terminal only, use `$env:STM_API_KEY = "APIKey"` (PowerShell), `set STM_API_KEY=APIKey` (cmd) or `export STM_API_KEY=APIKey` (Git Bash).


## Run Data Ingestion Service

- Navigate to the `root` folder of the project and run the command to install all Python dependencies:
```
pip install -r requirements.txt
```

- Then, navigate to the `software` folder and run the command to start the `data ingestion service`:
```
python -m startup.start_data_ingestion_service
```

- or, add the parameter after the module script to the command to run the `data ingestion service` for a select number of bus routes, i.e.: buses `121, 45, and 41` only:
```
python -m startup.start_data_ingestion_service "121, 45, 41"
```


## Run Telemetry Service

- Open a terminal window and launch `Jupyter Notebook`;
- On the Jupyter tree browser page navigate to the `services` folder of the project;
- Run the cells on [`Telemetry-Valdiation-Service`](services/Telemetry-Validation-Service.ipynb) notebook;


## Run Notification Service

- Open a terminal window and launch `Jupyter Notebook`;
- On the Jupyter tree browser page navigate to the `services` folder of the project;
- Follow the steps to launch the [`Telemetry-Valdiation-Service`](services/Telemetry-Validation-Service.ipynb) first;
- Once sure that the `Telemetry-Valdiation-Service` service is running, then navigate to [`Notification-Service.ipynb`](services/Notification-Service.ipynb) notebook;
- Run the cells on the notebook;
- Update values in the [configuration file](services/startup.conf) to send the emails to an account other than the default.


## Run Visualization Service

- Complete the `Run Data Ingestion Service` step
- Open visualization/project.godot with Godot 4.7.1 (C#/.NET version)
- Press the play button (the button is onn the top right corner of the window)
- If RabbitMQ is not reachable, the viewer plays a recording instead (see `Record & replay demo` below)


## Record & replay demo

The 3D viewer can play back a recorded hour of STM buses instead of the live feed, with no RabbitMQ, MongoDB, Docker or network. This gives you a demo you can send as a zip: unzip it and double-click the exe.

The 3D model covers downtown Montréal only (roughly latitude 45.48 to 45.53, longitude -73.63 to -73.54). Buses outside it float over empty ground, so record routes that run through downtown. In a scan on a Wednesday afternoon (28 October 2026, around 16:10), the routes with the most buses on the map were 51, 165, 24, 55, 129, 107, 80 and 747.

### Build and share the demo

- Double-click `build_demo.bat` at the repository root. From a terminal (cmd or PowerShell), run `.\build_demo.bat --no-pause` or `powershell -ExecutionPolicy Bypass -File build_demo.ps1 -NoPause`. The build takes a few minutes.
- The build needs:
  - Godot 4.7.1 .NET. The script finds `Godot_v4.7.1-stable_mono_win64_console.exe` on the Desktop, in Downloads, in Program Files or on the `PATH`. Otherwise, give its path: `powershell -ExecutionPolicy Bypass -File build_demo.ps1 -Godot "<path>"`, or save it once with `setx GODOT_BIN "<path>"`, then open a new terminal or double-click `build_demo.bat` again.
  - Its export templates (in Godot: Editor > Manage Export Templates).
  - The .NET SDK 8 or newer. The .NET runtime alone is not enough.
- Result: `visualization/Build/STM-DT-Demo.zip` (about 365 MB, 500 MB unzipped). The unzipped version is in `visualization/Build/Demo/`, so you can try it directly. Build output and its log (`visualization/Build/build_demo.log`) are not tracked by git.
- To use the zip:
  1. Unzip it (right-click > Extract All).
  2. Double-click `STM DT Demo.exe`. Keep `data_Below MTL_windows_x86_64` (the bundled .NET runtime) and `replays` next to it.
  3. If Windows SmartScreen warns about an unknown app, click "More info" > "Run anyway". The exe is not signed.
- Top left: the legend (with checkboxes for the bus stops and the reference points), the **Lines** panel and, once you click a bus, its occupancy chart. Bottom bar: replay menu, Restart, Play/Pause, speed (1x to 60x), timeline, and the local time of the recording.
- Finding the buses: in replay mode the camera starts over the busiest group of buses on the map. In the bundled hour, about 55 of the roughly 95 buses in service at any moment are on the 3D map. The others are on parts of their routes outside the model (routes 55 and 80, for example, run far north of it). To reach them, right-click for free-fly and move with WASD (Space and Ctrl to go up and down); right-click again to get the cursor back.

### Record a new hour

You need Python 3.10+ and the dependencies: `pip install -r requirements.txt` from the repository root. The commands below run from the repository root; use your virtual environment's Python if you have one (e.g. `.venv\Scripts\python.exe` instead of `python`). From the `software` folder, use `python -m recording ...` and prefix the paths with `..\` (e.g. `..\recordings\<name>.sqlite`). Add `--help` to any command for all its options.

1. **Record.** Only `STM_API_KEY` is needed (see [STM API OAuth](#stm-api-oauth)). Pick routes that run through downtown (see above), for example:
   ```
   python software\recording record --routes "24,51,55,80,129,165" --minutes 60
   ```
   It polls the STM API every 15 s into `recordings\stm_<date>_<time>_routes-24-51-55-80-129-165.sqlite` (`--name` chooses another name). Without `--minutes`, it records until Ctrl+C. Ctrl+C always stops cleanly, because every poll is saved as it happens. Failed polls are kept as gaps and skipped later.

   `recordings\` is not tracked by git, and a live recording cannot be made again: keep a copy of the ones you want to keep.

   You can also convert an older MongoDB CSV export instead of recording. This one can be recreated at any time, but its routes (45, 121 and 140) are off the 3D map:
   ```
   python software\recording import-csv datasets\20250301-buses.csv --name 2025-03-01_saturday_routes-121-45-140
   ```
2. **Check it.** This shows the time span, gaps, buses per route, occupancy and the best gap-free hours:
   ```
   python software\recording info recordings\<name>.sqlite
   ```
3. **Export a replay.** This writes the best gap-free hour to `visualization\replays\<name>_<HHMM>-<HHMM>.json`. The `--name` is the title shown in the viewer.
   ```
   python software\recording export recordings\<name>.sqlite --minutes 60 --name "Weekday rush hour, downtown routes 24, 51, 55, 80, 129, 165"
   ```
   To choose the window yourself, use `--start 16:30 --minutes 60` (local time of the recording). Add `--routes "24,51"` to keep only some routes. With the forecast environment (see [Occupancy colors, chart and forecasts](#occupancy-colors-chart-and-forecasts)), add `--forecast` to also precompute the occupancy forecasts.
4. **Rebuild the zip**: double-click `build_demo.bat` (or run `.\build_demo.bat`). Every `*.json` in `visualization\replays\` is bundled into the exe.

`import-csv`, `info` and `export` need no API key, network, RabbitMQ, MongoDB or Docker.

### Which replay plays at startup

The replay menu lists every replay found, in this order:

1. A replay given on the command line after `--`, e.g. `"STM DT Demo.exe" -- --replay="<path>"` (Godot ignores user arguments placed before `--`). When there is one, only it is loaded at startup.
2. The `*.json` files in the `replays` folder next to `STM DT Demo.exe`, marked "(file)": newest recording first, by the start time of the replay.
3. The replays bundled into the exe (from `visualization\replays\`): newest recording first.

The first one that loads plays at startup; one that cannot be read is skipped with a message. Titles do not change the order. Replays with the same start time, and files whose start time cannot be read (listed last), are sorted by file name.

### Add replays without rebuilding

Copy an exported `.json` into the `replays` folder next to `STM DT Demo.exe` in the unzipped demo, then start the exe again. It is listed before the replays built into the exe, which stay available in the menu. Each build recreates `visualization\Build\Demo`, so keep the replays you want to bundle in `visualization\replays`.

### Occupancy colors, chart and forecasts

- Each bus's beacon is colored by its occupancy: the GTFS-realtime [`OccupancyStatus`](https://gtfs.org/documentation/realtime/reference/#enum-occupancystatus) of the feed, named with the spec's wording. The colors go from green to red: dark green for many seats available, yellow for few seats available, orange for standing room only, dark red for full, and grey for no data. Each color also has its own lightness, so color-blind viewers can still tell them apart.
- The spec has nine statuses. Six of them, from `EMPTY` (0) to `FULL` (5), say how full the bus is: the viewer charts and forecasts those. `NOT_ACCEPTING_PASSENGERS` (6) and `NOT_BOARDABLE` (8) are shown on the bus in purple but not charted or forecast; `NO_DATA_AVAILABLE` (7) and a missing status are "no data". The spec also says a feed may publish only some statuses and that consumers must not assume they follow a linear scale. STM sends only four: many seats available, few seats available, standing room only and full (values 1, 2, 3 and 5). It never sends empty (0) or crushed standing room only (4). The legend at the top left and the chart therefore list only the statuses found in the data. The legend's two checkboxes show or hide the teal bus-stop poles and the blue reference points, which are not buses. The reference points (the poles of known coordinates that place the buses on the 3D map) are hidden at start.
- Click a bus to open its chart: one row per status, its occupancy over the last 20 minutes and, when the replay has a forecast, the next 10 minutes (dashed median and its 80% range). In a replay, **Show actual** draws what really happened next. Close the chart with **×**. The selected bus has a blue outline, the color of its chart. Clicking another bus selects it instead. In a replay, a bus the feed skips for up to 3 polls in a row (45 s) stays where it was, so it stays selected; after that, or at once after a jump on the timeline, it is removed until it reports again.
- The forecasts are made with [Timestral](https://github.com/mistralai/timestral), a time-series model by Mistral AI, and stored in the replay file in advance: the demo exe runs no model and needs no Python. Every minute of replay time, each bus with at least 2 minutes of occupancy history gets a forecast of the next 10 minutes (a point every 30 s), made only from what was observed until then. The model works on the ranks of the statuses found, not on their values: for STM, many seats available is step 0 and full is step 3. That scale is the statuses found in the whole replay.
- Each forecast is then **calibrated walk-forward**: before each forecast time, every forecast point (+30 s to +10 min) learns from the earlier forecasts whose outcome is already known, never from the future. Two things are learned, both easy to explain:
  - **A trust weight w** (0 to 1): the forecast median is the current status plus w times Timestral's predicted change. w is the least-squares fit of the changes that really happened on the changes Timestral predicted.
  - **An honest 80% range** (split conformal prediction): the range is widened until, in the past, each of its ends held 90% of the outcomes, so that it holds at least 8 outcomes in 10 (89% on the bundled hour).

  A point needs 200 past outcomes from 20 buses first, so the first 5 to 14 minutes of a replay show raw Timestral (the chart says "calibration still learning"). `forecast --no-calibration` stores the raw model output.
- To add (or redo) the forecast of a replay, use a separate environment, so the main one stays small (Timestral needs PyTorch). From the repository root:
  ```
  uv venv .venv-forecast --python 3.12
  uv pip install --python .venv-forecast -r software\recording\requirements-forecast.in
  .venv-forecast\Scripts\python software\recording forecast visualization\replays\<file>.json
  ```
  Without uv: `python -m venv .venv-forecast`, then `.venv-forecast\Scripts\python -m pip install -r software\recording\requirements-forecast.in`. The first run downloads the model to the Hugging Face cache (Timestral-28M, 111 MB; `--model 5M`, `123M` or `209M` for the other sizes, 21 MB to 837 MB; add `--offline` once it is cached). For the bundled hour it takes from about 20 seconds to 1.5 minutes on a laptop CPU and adds about 1.6 MB to the file, which is rewritten in place. Then rebuild the zip, or copy the file into the `replays` folder of an unzipped demo. `export --forecast` exports and forecasts in one go. Timestral's code is under Apache-2.0 and its weights under a Modified MIT license.
- What the numbers mean: `forecast` prints three scores (the first two also 10 minutes ahead), for the stored (calibrated) forecast, the raw model output and a "no change" baseline that repeats the last observed status:
  - how often the median, rounded to a status, is the observed status ("right");
  - its average error, in steps between statuses;
  - the coverage: the share of observed statuses inside the 80% range.

  Occupancy changes only a few times per bus per hour, so "no change" is hard to beat. A zero-width range at the last observed status would already hold 74% of the outcomes on the bundled hour.
- Results on the bundled hour:

  | | calibrated (stored) | raw Timestral-28M | "no change" |
  |---|---|---|---|
  | right | 74.0% | 73.0% | 74.3% |
  | right, 10 min ahead | 56.3% | 55.0% | 56.7% |
  | average error (steps) | 0.295 | 0.313 | 0.290 |
  | 80% range holds | 89% | 81% | – |

  - The calibration makes Timestral more accurate (+1.1 points, 95% interval [+0.7, +1.4]) but not better than "no change" (−0.25 points [−0.36, −0.14]). From about minute 14 (once every point is calibrated), the dashed median stays at the current status: the calibration learned that "it stays the same" is the best guess on this hour. Its trust in Timestral's predicted changes peaks at 56% (2.5 minutes ahead, minute 7) and ends at 12% 10 minutes ahead.
  - The range is honest now: it holds 89% of the outcomes (86% 10 minutes ahead, against 72% raw), and 59% of the outcomes where the status changed (29% raw). The price is a range about 1.7 times wider.

  The chart's footer shows the model, how often it is right and whether it beats "no change"; hover the footer for the other scores and what the calibration learned.

### What we tried to make the forecast better (for students)

The question: could correlations between buses, or where a bus is, predict its occupancy better? Everything was tested on the bundled hour, walk-forward (each forecast only learns from the past), with a decision rule fixed before looking at the results: a method wins only if it is right more often than "no change", with a 95% interval above 0 (2,000 resamplings of whole buses).

- **The data does hold real patterns.**
  - The bus ahead: when the previous bus of the same line got fuller after passing this bus's stop, this bus got fuller 10 minutes later in 46% of cases, and emptier in only 7% (correlation +0.33).
  - Trip ends: the status changes twice as often when a bus starts a new trip (47% vs 24%), and a new trip usually starts with many seats available (71%).
  - Metro stations on the way: more changes (+12 points), but not in one direction.
- **But correlation is not forecasting.** Used as predictors, they did not win:
  - Adding the bus ahead's change to the calibrated forecast: 10 minutes ahead, −1.7 points against "no change", worse than the calibrated forecast alone (−0.3).
  - A location-aware logistic regression (current status, trip progress, stations ahead, bus ahead, Timestral's output): right 73.4%, −0.9 points against "no change". It did predict better probabilities and a better range, but what it learned about trip position did not match the data.
  - Giving Timestral a longer history, or the other buses' occupancy as extra inputs, did not help either (previous study).
- **What did help is calibration** (above): it fixes Timestral's own weaknesses, overconfident changes and a too-narrow range, using only its past results.
- **Limits.** One rush hour of one Wednesday, 6 lines. The intervals are optimistic: they resample buses without refitting the walk-forward models. Some buses report "many seats available" for a whole hour (probably a stuck sensor), which flatters "no change". Trip ends could only be guessed: the 2025 timetable in `datasets\` matches none of the 2026 trips.
- **Next steps** need more data: record several weekday hours (morning and evening peaks) with `record`, then train on some days and test on others, and use a current STM timetable for trip ends.

### Lines and their stops

- **Lines** (under the legend) has one button per line: filled while its buses and stops are shown, outlined while they are hidden.
  - Click a line to hide it, click again to show it. Double-click a line to show only that one, and **All** shows every line again.
  - To add a line, type its number in the **+ line** field and press Enter (several at once: `24, 51`). In a replay, only the recorded lines can be added; another number gets a note ("Line 45 is not in this recording"). Live, a line shows buses only if the ingestion service publishes it: `python -m startup.start_data_ingestion_service` (from `software`) publishes every route, or only the routes given on its command line (e.g. `121,45,41`).
  - A selected bus whose line gets hidden is released.
- The poles are the bus stops of the shown lines, in both directions. When a bus is selected, the stops of its line in its direction of travel become thicker, and the other stops fade. The legend names that line and its direction, e.g. "Stops of route 165 (Nord)". The highlight follows the bus when it turns around at a terminus.
- The stops come from `visualization/assets/route_stops.json`. It lists, for every STM bus route and direction, the stops of all its trips (branches and short turns included). It is made from the GTFS files in `datasets\` (STM schedule of 2025) with:
  ```
  python software\recording route-stops
  ```
  To refresh it, download STM's current GTFS (`gtfs_stm.zip` on stm.info) and run `python software\recording route-stops --gtfs <path to gtfs_stm.zip or its folder>`, then rebuild the demo. With the 2025 files, 89 to 100% of the stops reported by the buses of the bundled hour (October 2026) are on their line's list. It takes 10 to 45 seconds. The live feed's trip ids change with each schedule, so the viewer matches a bus to its stops by route and direction.

### Notes

- The bundled replay is `visualization/replays/2026-10-28_wednesday_downtown_routes-24-51-55-80-129-165_1610-1710.json` (14.4 MB), titled "Wednesday 28 October 2026 · downtown rush hour, routes 24, 51, 55, 80, 129, 165". It covers Wednesday 28 October 2026, 16:10 to 17:10 local time: 241 frames, 118 buses, about 95 of them in service at any moment and about 55 on the 3D map. It includes each bus's occupancy (in this rush hour about 35% many seats available, 28% few seats, 31% standing room only and 5% full), and its Timestral-28M occupancy forecast, calibrated walk-forward.
  - It was recorded live with `python software\recording record --routes "24,51,55,80,129,165" --minutes 63 --name 2026-10-28_wednesday_downtown_routes-24-51-55-80-129-165`, then exported with `python software\recording export recordings\2026-10-28_wednesday_downtown_routes-24-51-55-80-129-165.sqlite --minutes 60 --name "Wednesday 28 October 2026 · downtown rush hour, routes 24, 51, 55, 80, 129, 165"`, and its forecast was added with `.venv-forecast\Scripts\python software\recording forecast` on that file (default settings). `export` does not replace an existing file unless you add `--force`.
  - Its source, `recordings\2026-10-28_wednesday_downtown_routes-24-51-55-80-129-165.sqlite`, is a live capture. It is not in git and cannot be recreated, so back it up.
- Optional extra replay: Saturday 1 March 2025, routes 45, 121 and 140, from `datasets\20250301-buses.csv`. It is not bundled because none of its buses are ever on the 3D map (right-click for free-fly to reach them). To try it, export it straight into the `replays` folder of an unzipped demo (dropped files come first, so it then plays at startup):
  ```
  python software\recording import-csv datasets\20250301-buses.csv --name 2025-03-01_saturday_routes-121-45-140
  python software\recording export recordings\2025-03-01_saturday_routes-121-45-140.sqlite --minutes 60 --name "Saturday 1 March 2025 · routes 45, 121, 140" --out "<unzipped demo>\replays\2025-03-01_saturday_1520-1619.json"
  ```
  Skip `import-csv` if `recordings\2025-03-01_saturday_routes-121-45-140.sqlite` already exists. Without `--out`, the export goes to `visualization\replays\` and the next build bundles it.
- Replay format: see [`visualization/replays/README.md`](visualization/replays/README.md). Recordings are SQLite files (schema version 1, see `software/recording/store.py`), so you can export other windows later.
- Live and recorded data use the same code: `software/communication/stm_api.py` is shared by the data ingestion service and the recorder, so a replay frame is the same message the live service publishes.
- From the Godot editor the viewer still runs live. If RabbitMQ is not reachable, it falls back to the bundled replay and says so. To force a replay: `<Godot console exe> --path visualization -- --replay`, or `-- --replay=<path to a replay .json>`.
- Recording tests: `cd software` then `..\.venv\Scripts\python.exe -m unittest discover -s tests -v`.


## Authors

- [Vanny Katabarwa](mailto:vanny-nicole.kayirangwa-katabarwa@polymtl.ca?subject[Github]%LOG6953FE-STM-Digital%Twin)
- [Kerian Fiter](mailto:kerian.fiter@polymtl.ca?subject[Github]%LOG6953FE-STM-Digital%Twin)
- [Carlos Pambo](mailto:carlos.pambo@polymtl.ca?subject[Github]%LOG6953FE-STM-Digital%Twin)