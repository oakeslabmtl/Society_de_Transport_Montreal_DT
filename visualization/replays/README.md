# Replays

Every `*.json` file in this folder is bundled into the demo build (`build_demo.bat`) and listed in the viewer's replay menu. Make them with the recording tool (`python software\recording export ...`, see "Record & replay demo" in the root README). Files dropped later into the `replays\` folder next to `STM DT Demo.exe` are picked up at the next start, without a rebuild.

Keep only real replays here: a broken `.json` is skipped at startup, but it still shows up in the menu.

The bundled replay is `2026-10-28_wednesday_downtown_routes-24-51-55-80-129-165_1610-1710.json`: Wednesday 28 October 2026, 16:10 to 17:10, downtown routes 24, 51, 55, 80, 129 and 165, recorded live from the STM API. The root README says how it was made.

## Which replay plays at startup

The replay menu lists every replay found, in this order:

1. A replay given on the command line after `--`, e.g. `"STM DT Demo.exe" -- --replay="<path>"` (Godot ignores user arguments placed before `--`). When there is one, only it is loaded at startup.
2. The files in the `replays\` folder next to `STM DT Demo.exe`, marked "(file)": newest recording first, by `start_time`.
3. The replays bundled from this folder: newest recording first.

The first one that loads plays at startup. Titles (`name`) do not change the order. Replays with the same `start_time`, and files whose `start_time` cannot be read (listed last), are sorted by file name. The viewer reads `name` and `start_time` from the part of the file before `"frames"`, so keep them there (the exporter does); a tool that re-sorts the keys alphabetically moves them after `frames`.

## Format: `stm-dt-replay`, version 1

UTF-8 JSON, written compact. Example (indented, one entity shown):

```json
{
  "format": "stm-dt-replay",
  "version": 1,
  "name": "Wednesday 28 October 2026 · downtown rush hour, routes 24, 51, 55, 80, 129, 165",
  "created_at": "2026-10-28T23:02:17Z",
  "source": {"type": "stm-api", "detail": "https://api.stm.info/pub/od/gtfs-rt/ic/v2/vehiclePositions"},
  "timezone": "America/Montreal",
  "utc_offset_seconds": -14400,
  "start_time": 1793218248.8132923,
  "end_time": 1793221848.8122008,
  "duration": 3599.998908519745,
  "poll_interval": 15.0,
  "routes": [24, 51, 55, 80, 129, 165],
  "vehicle_count": 118,
  "frames": [
    {"t": 0.0, "data": [
      {"id": "40902", "vehicle.trip.trip_id": "302127345", "vehicle.trip.start_date": "20261028",
       "vehicle.trip.route_id": "80", "vehicle.position.latitude": 45.52375030517578,
       "vehicle.position.longitude": -73.60565948486328, "vehicle.position.bearing": 0.0,
       "vehicle.position.speed": 0.0, "vehicle.current_stop_sequence": 14, "vehicle.current_status": 1,
       "vehicle.timestamp": 1793218232, "vehicle.congestion_level": 0, "vehicle.vehicle.id": "40902",
       "vehicle.occupancy_status": 2, "vehicle.trip.direction_id": 1, "vehicle.stop_id": "51503",
       "vehicle.vehicle.wheelchair_accessible": 2}
    ]}
  ]
}
```

| Key | Meaning |
| --- | --- |
| `format`, `version` | Must be `"stm-dt-replay"` and `<= 1`. Other files are rejected. |
| `name` | Title shown in the replay menu. If it is missing, the file name is used. |
| `source` | Where the data came from: `stm-api` (live recording) or `csv-import`. |
| `timezone`, `utc_offset_seconds` | Used for the clock shown in the viewer. Local time = `start_time + t + utc_offset_seconds`. The offset is taken at `start_time`, so after a daylight saving time change inside the replay the clock is 1 h off (the exporter warns about it). |
| `start_time`, `end_time` | Unix seconds of the first and last poll. `duration = end_time - start_time`. |
| `poll_interval` | Seconds between polls, usually 15. |
| `routes` | Routes the viewer subscribes to: sorted ints, or strings if a route id is not numeric. |
| `frames[].t` | Seconds since `start_time`. Ascending, and the first frame is `0`. |
| `frames[].data` | One full snapshot at that poll. It has the same flattened GTFS-realtime entities (dotted keys, same types) as the live RabbitMQ message `stm.bus.routes.updates`. A bus missing from a frame is not in service at that moment. `data` may be empty. |

Entity keys are those of the live feed: `id`, `vehicle.trip.trip_id`, `vehicle.trip.start_time`, `vehicle.trip.start_date`, `vehicle.trip.route_id`, `vehicle.position.latitude`, `vehicle.position.longitude`, `vehicle.position.bearing`, `vehicle.position.speed`, `vehicle.current_stop_sequence`, `vehicle.current_status`, `vehicle.timestamp`, `vehicle.vehicle.id`, `vehicle.occupancy_status` (GTFS-RT enum 0-8), `vehicle.congestion_level`. Fields the feed did not send are absent, not `null`. A position of (0, 0) means the bus had no GPS fix; the viewer keeps that bus at its last position. Other keys may also appear (e.g. `vehicle.trip.direction_id`, `vehicle.stop_id`).

Readers ignore unknown top-level keys, so new data (like `forecast` below) can be added without a new version. `version` is bumped only for breaking changes.

## Occupancy forecast: key `forecast` (optional)

`.venv-forecast\Scripts\python software\recording forecast <replay>.json` (the forecast environment) adds precomputed occupancy forecasts made with Timestral (see "Occupancy colors, chart and forecasts" in the root README); the viewer shows them in the chart of the selected bus. A replay without this key still plays: the chart then shows the observed occupancy only. The key is written after `frames`, so `name` and `start_time` stay before `"frames"`, where the viewer looks for them. Example (indented, one bus and one forecast shown, 20 points shortened to 3):

```json
"forecast": {
  "format": "stm-dt-occupancy-forecast",
  "version": 2,
  "model": "mistralai/Timestral-28M",
  "model_name": "Timestral-28M + calibration",
  "created_at": "2026-10-30T16:29:19Z",
  "target": "vehicle.occupancy_status",
  "levels": [1, 2, 3, 5],
  "step": 15.0,
  "point_every": 30.0,
  "horizon": 600.0,
  "origin_every": 60.0,
  "min_history": 120.0,
  "quantiles": [0.1, 0.5, 0.9],
  "metrics": {"evaluated_points": 98071, "mae": 0.295, "mae_last_value": 0.29, "mae_at_horizon": 0.52,
              "mae_last_value_at_horizon": 0.51, "accuracy": 0.74, "accuracy_last_value": 0.743,
              "accuracy_at_horizon": 0.563, "accuracy_last_value_at_horizon": 0.567, "coverage": 0.893},
  "metrics_raw": {"evaluated_points": 98071, "mae": 0.313, "mae_last_value": 0.29, "mae_at_horizon": 0.546,
                  "mae_last_value_at_horizon": 0.51, "accuracy": 0.73, "accuracy_last_value": 0.743,
                  "accuracy_at_horizon": 0.55, "accuracy_last_value_at_horizon": 0.567, "coverage": 0.814},
  "calibration": {"method": "Walk-forward: ...", "min_points": 200, "min_buses": 20, "cell_min_points": 100,
                  "cell_min_buses": 10, "calibrated_share": 0.869, "first_t": [299.991, 299.991, 359.998, ...],
                  "history": {"t": [119.99, ...], "w": [[null, ...], ...]},
                  "final": [{"examples": 5453, "buses": 114, "w": 0.081, "low_offset": [0.0, 0.0, 0.0, 0.0],
                             "high_offset": [0.0, 0.0, 0.0, 0.0]}, ...]},
  "vehicles": {
    "40902": [
      {"t": 119.99, "q": [[0.4, 0.3, 0.1], [1.0, 1.0, 1.0], [1.6, 1.7, 1.9]]}
    ]
  }
}
```

| Key | Meaning |
| --- | --- |
| `format`, `version` | Must be `"stm-dt-occupancy-forecast"` and `2`. Otherwise the viewer ignores the whole forecast (a version 1 forecast, which stored status values instead of ranks, asks to run `forecast` again). |
| `model`, `model_name` | The model that made it: Hugging Face repo id (or model folder), and a short name for display. |
| `target` | The forecast entity value: `vehicle.occupancy_status`. |
| `levels` | The occupancy statuses found in the replay (anywhere in it, so a status first seen later is already on the scale of earlier forecasts), ascending: the scale of the forecast. Value `r` (a rank, possibly fractional) stands for status `levels[r]`: here 0 = `MANY_SEATS_AVAILABLE` (1), 1 = `FEW_SEATS_AVAILABLE` (2), 2 = `STANDING_ROOM_ONLY` (3), 3 = `FULL` (5). |
| `step` | Seconds per model time step: the replay's `poll_interval`. |
| `point_every`, `horizon` | Each forecast has `N = horizon / point_every` points, at `t + point_every * (k + 1)` for `k = 0 .. N-1`. `point_every` is a multiple of `step`. |
| `origin_every`, `min_history` | A forecast is made every `origin_every` seconds of replay time, for each bus with `min_history` seconds of history since its first known status and a known status at that time. |
| `quantiles` | Levels of the rows of `q`: 10%, median and 90%, so `q[0]`..`q[2]` is the 80% range. |
| `metrics` | How good the stored forecasts were on this replay, compared with what was observed (`null` when nothing could be compared): `evaluated_points` (points with an observed status at their time), `mae` (mean absolute error of the median, in ranks: steps between statuses), `mae_last_value` (same for the "last observed status" baseline), `accuracy` (share of points where the median rounded to the nearest rank, .5 up, is the observed status) and `accuracy_last_value` (same for the baseline), the same four for the last point only (`*_at_horizon`), and `coverage` (share of observed statuses inside the 80% range). |
| `metrics_raw` | Optional, present when the stored forecast is calibrated: the same scores for the raw model output. `model_name` then ends with " + calibration". |
| `calibration` | Optional, present when the stored forecast is calibrated (walk-forward: each forecast point learns, before each forecast time, from the earlier forecasts whose outcome is known). `method` (a sentence), the thresholds `min_points`, `min_buses` (a point is calibrated once it has that many past outcomes from that many buses), `cell_min_points`, `cell_min_buses` (for range offsets per status at the forecast time), `calibrated_share` (share of the stored points that are calibrated), `first_t` (per point: the `t` of its first calibrated forecast, or `null`), `history` (`t`: the forecast times; `w`: per time, the trust weight of each point, or `null` while it is raw) and `final` (per point, at the last forecast time: `examples`, `buses`, `w`, and `low_offset` / `high_offset` per rank, or `null`). The calibrated median is `L + w * (model median - L)`, L the rank at the forecast time; the range ends are moved out by the offsets. The viewer ignores this key if it is malformed. |
| `vehicles` | Bus id (string, the same as `vehicle.vehicle.id`) -> its forecasts, sorted by `t`. |
| `vehicles[].t` | When the forecast was made: a frame time, on the same timeline as `frames[].t` (unix time = `start_time + t`). It uses only what was observed until then. |
| `vehicles[].q` | `q[j][k]` = quantile `quantiles[j]` of the rank at `t + point_every * (k + 1)`: rounded to 2 decimals, between 0 and `len(levels) - 1`, and `q[0][k] <= q[1][k] <= q[2][k]`. |

Occupancy statuses are the GTFS-realtime `OccupancyStatus` values. The spec says they are not a linear scale and that a feed may publish only some of them: STM sends `MANY_SEATS_AVAILABLE` (1), `FEW_SEATS_AVAILABLE` (2), `STANDING_ROOM_ONLY` (3) and `FULL` (5), never `EMPTY` (0) or `CRUSHED_STANDING_ROOM_ONLY` (4). So the forecast works on the ranks of the statuses found in the replay, and the viewer's chart has one evenly spaced row per status found. Values 6 and 8 (`NOT_ACCEPTING_PASSENGERS`, `NOT_BOARDABLE`) are shown on the bus but are not a level; 7 (`NO_DATA_AVAILABLE`) and missing values are "no data". None of them is forecast or used to score the forecasts.
