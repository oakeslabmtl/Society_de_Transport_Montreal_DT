extends RefCounted

## Occupancy history and precomputed forecasts per vehicle, read by the occupancy chart and legend.
## Replay: built once per load from all frames and the replay's "forecast" key.
## Live: appended per RabbitMQ message (the last LIVE_KEEP_SECONDS per bus are kept).

const Occupancy = preload("res://scripts/occupancy.gd")
const FORECAST_FORMAT := "stm-dt-occupancy-forecast"
const FORECAST_VERSION := 2 # Values are ranks of the statuses in "levels"; version 1 stored status values
const LIVE_KEEP_SECONDS := 7200.0
const STALE_ORIGINS := 2.0 # A forecast older than this many origin intervals is not current any more

var occupancy_known := false # At least one observation has a level (status 0..5)
var statuses_seen := {} # status (Occupancy.NO_DATA excluded) -> true, for the legend
var statuses_version := 0 # Incremented when statuses_seen or the forecast's levels change (legend and chart rows)
var forecast_in_replay := false # The replay has a "forecast" key (valid or not)
var has_forecast := false # ... and it is valid
var forecast_error := "" # Why the forecast was ignored (human-readable), "" when valid or absent
var model_name := ""
var base_model_name := "" # model_name without " + calibration": the model itself
var quantiles := PackedFloat64Array()
var forecast_levels := PackedInt32Array() # Status of each forecast rank: rank r stands for forecast_levels[r]
var metrics := {} # Contract metrics; values are floats or null
var metrics_raw := {} # Same keys, for the model's raw output when the stored forecast is calibrated ({} otherwise)
var calibration := {} # The forecast's "calibration" key when valid ({} otherwise): what the calibration learned
var training := {} # The forecast's "training" key when valid ({} otherwise): how the location model learned
var _learned_from := PackedFloat64Array() # Per forecast point: unix time of its first calibrated or model forecast (INF: never)
var horizon := 600.0
var point_every := 30.0
var origin_every := 60.0
var min_history := 120.0
var vehicle_count := 0 # Vehicles with at least one forecast
var origin_count := 0 # Distinct forecast origins

var _history := {} # vehicle id -> {"times": PackedFloat64Array (unix), "statuses": PackedInt32Array (-1 = no data),
# "route": String, and in replays "route_times": PackedFloat64Array (unix),
# "routes": PackedStringArray (the route from that time on)}
var _forecasts := {} # vehicle id -> {"origins": PackedFloat64Array (unix, ascending), "rows": [[PackedFloat64Array per quantile], ...]}
var _median_index := 0

## Vehicle id of an entity as used for bus node names ("" when missing)
static func vehicle_key(entity: Dictionary) -> String:
	var value = entity.get("vehicle.vehicle.id", entity.get("id"))
	return "" if value == null else Occupancy.int_text(value)

## Route id of an entity as text ("51", not "51.0"; "" when missing)
static func route_key(entity: Dictionary) -> String:
	var route = entity.get("vehicle.trip.route_id")
	return "" if route == null else Occupancy.int_text(route)

## Direction (0 or 1) of an entity's trip, -1 when unknown
static func direction_key(entity: Dictionary) -> int:
	var direction = entity.get("vehicle.trip.direction_id")
	if direction is String and direction.is_valid_int():
		direction = direction.to_int()
	if (direction is int or direction is float) and (direction == 0 or direction == 1):
		return int(direction)
	return -1

func clear() -> void:
	_history = {}
	_forecasts = {}
	occupancy_known = false
	statuses_seen = {}
	statuses_version += 1
	forecast_in_replay = false
	_reset_forecast()

## Rebuilds the store from the replay loaded in player (a ReplayPlayer)
func load_replay(player: Node) -> void:
	clear()
	# Plain arrays while collecting (appending to a packed array stored in a dictionary copies it)
	var collected := {}
	var times: PackedFloat64Array = player.get_frame_times()
	for i in times.size():
		_collect(collected, player.get_frame_data(i), player.start_time + times[i])
	for vehicle_id in collected:
		var entry: Dictionary = collected[vehicle_id]
		_history[vehicle_id] = {"times": PackedFloat64Array(entry["times"]), "statuses": PackedInt32Array(entry["statuses"]),
			"route": entry["route"], "route_times": PackedFloat64Array(entry["route_times"]),
			"routes": PackedStringArray(entry["routes"])}
	_load_forecast(player.meta)
	statuses_version += 1

## Live mode: records the entities of one message, observed at now_unix
func record_live(entities: Array, now_unix: float) -> void:
	var collected := {}
	_collect(collected, entities, now_unix)
	var cutoff := now_unix - LIVE_KEEP_SECONDS
	for vehicle_id in collected:
		var entry: Dictionary = collected[vehicle_id]
		var history: Dictionary = _history.get(vehicle_id, {"times": PackedFloat64Array(), "statuses": PackedInt32Array()})
		var times: PackedFloat64Array = history["times"]
		var statuses: PackedInt32Array = history["statuses"]
		times.append_array(PackedFloat64Array(entry["times"]))
		statuses.append_array(PackedInt32Array(entry["statuses"]))
		_history[vehicle_id] = {"times": times, "statuses": statuses, "route": entry["route"]}
	# Forget what is older than the kept window (buses that left the feed included)
	for vehicle_id in _history.keys():
		var history: Dictionary = _history[vehicle_id]
		var times: PackedFloat64Array = history["times"]
		var statuses: PackedInt32Array = history["statuses"]
		var first := times.bsearch(cutoff)
		if first >= times.size():
			_history.erase(vehicle_id)
		elif first > 0:
			history["times"] = times.slice(first)
			history["statuses"] = statuses.slice(first)

func has_history(vehicle_id: String) -> bool:
	return _history.has(vehicle_id)

## Observation times (unix, ascending) of a vehicle
func history_times(vehicle_id: String) -> PackedFloat64Array:
	return _history[vehicle_id]["times"] if _history.has(vehicle_id) else PackedFloat64Array()

## Observed statuses of a vehicle, aligned with history_times (Occupancy.NO_DATA = unknown)
func history_statuses(vehicle_id: String) -> PackedInt32Array:
	return _history[vehicle_id]["statuses"] if _history.has(vehicle_id) else PackedInt32Array()

## Route id of the vehicle at unix_time (replay), or of its latest observation (live); "" when unknown
func route_at(vehicle_id: String, unix_time: float) -> String:
	if not _history.has(vehicle_id):
		return ""
	var history: Dictionary = _history[vehicle_id]
	if not history.has("route_times"):
		return history["route"]
	var routes: PackedStringArray = history["routes"]
	if routes.is_empty():
		return ""
	var route_times: PackedFloat64Array = history["route_times"]
	var index := route_times.bsearch(unix_time + Occupancy.TIME_EPSILON, false) - 1
	return routes[maxi(index, 0)]

## Status of the latest observation at or before unix_time (Occupancy.NO_DATA when none)
func status_at(vehicle_id: String, unix_time: float) -> int:
	var index := history_times(vehicle_id).bsearch(unix_time + Occupancy.TIME_EPSILON, false) - 1
	return history_statuses(vehicle_id)[index] if index >= 0 else Occupancy.NO_DATA

## Time of the vehicle's first observed level (status 0..5) at or before unix_time, or -1.0 when none
func first_known_time(vehicle_id: String, unix_time: float) -> float:
	var times := history_times(vehicle_id)
	var statuses := history_statuses(vehicle_id)
	for i in times.size():
		if times[i] > unix_time + Occupancy.TIME_EPSILON:
			break
		if Occupancy.is_level(statuses[i]):
			return times[i]
	return -1.0

## Statuses the chart has a row for, ascending: the levels (0..5) seen in the data and those of the forecast; all six
## before any is seen
func chart_levels() -> PackedInt32Array:
	var levels := {}
	for status in statuses_seen:
		if Occupancy.is_level(status):
			levels[status] = true
	for status in forecast_levels:
		levels[status] = true
	var result := PackedInt32Array(levels.keys())
	if result.is_empty():
		for status in Occupancy.MAX_LEVEL + 1:
			result.append(status)
	result.sort()
	return result

## Status a forecast value (a rank, possibly fractional) rounds to
func status_of_rank(rank: float) -> int:
	if forecast_levels.is_empty():
		return Occupancy.NO_DATA
	return forecast_levels[clampi(floori(rank + 0.5), 0, forecast_levels.size() - 1)]

## The vehicle's forecast made at the latest origin <= now_unix, or null when there is none or it is
## stale (older than STALE_ORIGINS origin intervals, or than the horizon if that is shorter: its points
## would all be in the past). Keys: origin (unix), times (unix, one per point), lo, median, hi
## (PackedFloat64Array, ranks: see status_of_rank)
func forecast_at(vehicle_id: String, now_unix: float):
	if not has_forecast or not _forecasts.has(vehicle_id):
		return null
	var entry: Dictionary = _forecasts[vehicle_id]
	var origins: PackedFloat64Array = entry["origins"]
	var index := origins.bsearch(now_unix + Occupancy.TIME_EPSILON, false) - 1
	if index < 0 or now_unix - origins[index] > minf(STALE_ORIGINS * origin_every, horizon):
		return null
	var rows: Array = entry["rows"][index]
	var times := PackedFloat64Array()
	for k in rows[0].size():
		times.append(origins[index] + point_every * (k + 1))
	return {"origin": origins[index], "times": times, "lo": rows[0], "median": rows[_median_index], "hi": rows[rows.size() - 1]}

## Width of the stored forecast range in percent (80 for quantiles 0.1..0.9)
func range_percent() -> int:
	return roundi((quantiles[quantiles.size() - 1] - quantiles[0]) * 100.0) if quantiles.size() > 1 else 0

## Appends each entity's status to collected[vehicle id] = {times: Array, statuses: Array, route: String (latest),
## route_times: Array, routes: Array (one entry per route change)}
func _collect(collected: Dictionary, entities: Array, unix_time: float) -> void:
	for entity in entities:
		if not entity is Dictionary:
			continue
		var vehicle_id := vehicle_key(entity)
		if vehicle_id == "":
			continue
		var status := Occupancy.status_of(entity)
		if status != Occupancy.NO_DATA and not statuses_seen.has(status):
			statuses_seen[status] = true
			statuses_version += 1
		if Occupancy.is_level(status):
			occupancy_known = true
		if not collected.has(vehicle_id):
			collected[vehicle_id] = {"times": [], "statuses": [], "route": "", "route_times": [], "routes": []}
		var entry: Dictionary = collected[vehicle_id]
		entry["times"].append(unix_time)
		entry["statuses"].append(status)
		var route_text := route_key(entity)
		if route_text != "":
			if route_text != entry["route"]:
				entry["route_times"].append(unix_time)
				entry["routes"].append(route_text)
			entry["route"] = route_text

func _reset_forecast() -> void:
	has_forecast = false
	forecast_error = ""
	model_name = ""
	base_model_name = ""
	quantiles = PackedFloat64Array()
	forecast_levels = PackedInt32Array()
	metrics = {}
	metrics_raw = {}
	calibration = {}
	training = {}
	_learned_from = PackedFloat64Array()
	vehicle_count = 0
	origin_count = 0
	_forecasts = {}
	_median_index = 0

## Reads meta["forecast"]; anything malformed drops the whole forecast and sets forecast_error
func _load_forecast(meta: Dictionary) -> void:
	_reset_forecast()
	forecast_in_replay = meta.has("forecast")
	if not forecast_in_replay:
		return
	var start_time = meta.get("start_time")
	var error := "the replay has no numeric \"start_time\"" if not Occupancy.is_number(start_time) else _parse_forecast(meta["forecast"], float(start_time))
	if error != "":
		_reset_forecast()
		forecast_error = "Forecast ignored: " + error
		push_warning("Occupancy: " + forecast_error)
		return
	has_forecast = true

## Validates and stores a forecast (format of visualization/replays/README.md); returns an error or ""
func _parse_forecast(raw, start_time: float) -> String:
	if not raw is Dictionary:
		return "\"forecast\" is not an object"
	if raw.get("format") != FORECAST_FORMAT:
		return "format is %s, expected \"%s\"" % [str(raw.get("format")), FORECAST_FORMAT]
	var version = raw.get("version")
	if Occupancy.is_number(version) and int(version) == 1:
		return "it was made by an older forecast tool (version 1): run the forecast command on the replay again"
	if not Occupancy.is_number(version) or int(version) != FORECAST_VERSION:
		return "unsupported forecast version %s (this build reads version %d)" % [str(version), FORECAST_VERSION]
	for key in ["point_every", "horizon", "origin_every"]:
		if not Occupancy.is_number(raw.get(key)) or float(raw.get(key)) <= 0.0:
			return "\"%s\" must be a positive number of seconds" % key
	if not Occupancy.is_number(raw.get("min_history", 0.0)) or float(raw.get("min_history", 0.0)) < 0.0:
		return "\"min_history\" must be a number of seconds"
	point_every = float(raw["point_every"])
	horizon = float(raw["horizon"])
	origin_every = float(raw["origin_every"])
	min_history = float(raw.get("min_history", 0.0))
	var point_count := roundi(horizon / point_every)
	if point_count < 1 or absf(point_count * point_every - horizon) > 0.01:
		return "horizon %s s is not a multiple of point_every %s s" % [Occupancy.int_text(horizon), Occupancy.int_text(point_every)]

	var raw_levels = raw.get("levels")
	if not raw_levels is Array or raw_levels.is_empty():
		return "\"levels\" must be a non-empty list of occupancy statuses"
	for value in raw_levels:
		if not Occupancy.is_number(value) or value != floorf(value) or not Occupancy.is_level(int(value)) or (not forecast_levels.is_empty() and int(value) <= forecast_levels[forecast_levels.size() - 1]):
			return "\"levels\" must be increasing occupancy statuses between 0 and %d, got %s" % [Occupancy.MAX_LEVEL, str(raw_levels)]
		forecast_levels.append(int(value))

	var raw_quantiles = raw.get("quantiles")
	if not raw_quantiles is Array or raw_quantiles.is_empty():
		return "\"quantiles\" must be a non-empty list"
	for value in raw_quantiles:
		if not Occupancy.is_number(value) or float(value) <= 0.0 or float(value) >= 1.0 or (not quantiles.is_empty() and float(value) <= quantiles[quantiles.size() - 1]):
			return "\"quantiles\" must be increasing numbers between 0 and 1, got %s" % str(raw_quantiles)
		quantiles.append(float(value))
	for j in quantiles.size():
		if absf(quantiles[j] - 0.5) < absf(quantiles[_median_index] - 0.5):
			_median_index = j

	var vehicles = raw.get("vehicles")
	if not vehicles is Dictionary:
		return "\"vehicles\" is not an object"
	var origins_seen := {}
	for vehicle_id in vehicles:
		var entries = vehicles[vehicle_id]
		if not entries is Array:
			return "vehicle %s: expected a list of forecasts" % vehicle_id
		var pairs: Array = []
		for i in entries.size():
			var entry = entries[i]
			if not entry is Dictionary or not Occupancy.is_number(entry.get("t")):
				return "vehicle %s, forecast %d: expected {\"t\": number, \"q\": [...]}" % [vehicle_id, i]
			var where := "vehicle %s at t=%s" % [vehicle_id, Occupancy.int_text(entry["t"])]
			var q = entry.get("q")
			if not q is Array or q.size() != quantiles.size():
				return "%s: \"q\" has %s rows, expected %d (one per quantile)" % [where, str(q.size()) if q is Array else "no", quantiles.size()]
			var rows: Array = []
			for j in q.size():
				var row = q[j]
				if not row is Array or row.size() != point_count:
					return "%s: q[%d] has %s values, expected %d" % [where, j, str(row.size()) if row is Array else "no", point_count]
				for value in row:
					if not Occupancy.is_number(value):
						return "%s: q[%d] contains %s (not a number)" % [where, j, str(value)]
				rows.append(PackedFloat64Array(row))
			pairs.append([start_time + float(entry["t"]), rows])
			origins_seen[float(entry["t"])] = true
		if pairs.is_empty():
			continue
		pairs.sort_custom(func(a, b): return a[0] < b[0])
		var origins := PackedFloat64Array()
		var all_rows: Array = []
		for pair in pairs:
			origins.append(pair[0])
			all_rows.append(pair[1])
		_forecasts[str(vehicle_id)] = {"origins": origins, "rows": all_rows}

	model_name = str(raw.get("model_name", raw.get("model", ""))).strip_edges()
	if model_name == "":
		model_name = "an unnamed model"
	base_model_name = model_name.trim_suffix(" + calibration")
	var raw_metrics = raw.get("metrics")
	metrics = raw_metrics if raw_metrics is Dictionary else {}
	vehicle_count = _forecasts.size()
	origin_count = origins_seen.size()
	# Optional (calibrated forecasts, the location model): never a reason to drop the forecast itself
	var raw_model_metrics = raw.get("metrics_raw")
	metrics_raw = raw_model_metrics if raw_model_metrics is Dictionary else {}
	for key in ["calibration", "training"]:
		if raw.has(key):
			var error := _parse_learning(key, raw[key], start_time, point_count)
			if error != "":
				_learned_from = PackedFloat64Array()
				push_warning("Occupancy: forecast %s details ignored: %s" % [key, error])
	return ""

## Reads the forecast's optional "calibration" or "training" key (what a calibration or the location model learned)
## into calibration or training, and _learned_from; returns an error or ""
func _parse_learning(key: String, raw, start_time: float, point_count: int) -> String:
	_learned_from = PackedFloat64Array()
	if not raw is Dictionary:
		return "not an object"
	var first_t = raw.get("first_t")
	if not first_t is Array or first_t.size() != point_count:
		return "\"first_t\" must have one entry per forecast point"
	for value in first_t:
		if value != null and not Occupancy.is_number(value):
			return "\"first_t\" must hold numbers or null"
		_learned_from.append(start_time + float(value) if value != null else INF)
	var per_time := "w" if key == "calibration" else "examples" # What history holds per forecast time
	var history = raw.get("history")
	if not history is Dictionary or not history.get("t") is Array or not history.get(per_time) is Array \
			or history["t"].size() != history[per_time].size() or history["t"].any(func(t): return not Occupancy.is_number(t)):
		return "\"history\" must have as many \"%s\" entries as times" % per_time
	var error := _check_calibration(raw, point_count) if key == "calibration" else _check_training(raw)
	if error == "":
		if key == "calibration":
			calibration = raw
		else:
			training = raw
	return error

static func _check_calibration(raw: Dictionary, point_count: int) -> String:
	for row in raw["history"]["w"]:
		if not row is Array or row.size() != point_count or row.any(func(w): return w != null and not Occupancy.is_number(w)):
			return "each \"history.w\" row must hold one number or null per forecast point"
	var final = raw.get("final")
	if not final is Array or final.size() != point_count:
		return "\"final\" must have one entry per forecast point"
	for entry in final:
		if entry != null and (not entry is Dictionary or not Occupancy.is_number(entry.get("w")) \
				or not Occupancy.is_number(entry.get("examples")) or not Occupancy.is_number(entry.get("buses"))):
			return "each \"final\" entry must be null or have numbers w, examples and buses"
	return ""

static func _check_training(raw: Dictionary) -> String:
	if raw["history"]["examples"].any(func(n): return not Occupancy.is_number(n)):
		return "\"history.examples\" must hold numbers"
	for key in ["min_points", "min_buses", "min_points_per_point", "min_points_per_status", "model_share", "position_known", "stop_known"]:
		if not Occupancy.is_number(raw.get(key)):
			return "\"%s\" must be a number" % key
	var final = raw.get("final")
	if final != null and (not final is Dictionary or not Occupancy.is_number(final.get("examples")) \
			or not Occupancy.is_number(final.get("buses"))):
		return "\"final\" must be null or have numbers examples and buses"
	return ""

## What the chart says while some points of the forecast made at origin_unix have not learned yet: they are the raw
## model output (calibration) or "no change" (location model); "" otherwise
func learning_note(origin_unix: float) -> String:
	for first in _learned_from:
		if origin_unix + Occupancy.TIME_EPSILON < first:
			return "calibration still learning" if not calibration.is_empty() else "model still learning"
	return ""

## The forecast's "calibration" or "training" key, whichever it has ({} if none)
func learned() -> Dictionary:
	return calibration if not calibration.is_empty() else training

