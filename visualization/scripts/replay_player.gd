class_name ReplayPlayer
extends Node

## Plays back a recorded "stm-dt-replay" JSON file: each recorded poll is emitted as a frame
## with exactly the same entity data as a live RabbitMQ message.

signal frame(data: Array, is_seek: bool)
signal replay_loaded(meta: Dictionary)
signal state_changed()

const FORMAT := "stm-dt-replay"
const MAX_VERSION := 1
const BUILTIN_DIR := "res://replays"
const EXTERNAL_DIR_NAME := "replays"
const HEADER_BYTES := 65536
const MAX_STEP := 0.25 # Max real seconds advanced per frame (avoids jumps after a hitch)
const WEEKDAYS := ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
const WEB_QUERY_ARGS := ["quality"] # The user arguments a web page's address may give (?quality=minimum)

var current_time := 0.0
var duration := 0.0
var playing := false
var speed := 1.0
var loop := true
var meta: Dictionary = {}
var routes: Array[int] = []
var last_error := ""
var loaded_path := ""
var start_time := 0.0
var utc_offset_seconds := 0
var poll_interval := 15.0
var external_dir := ""
var external_scanned := false

var _frames: Array = []
var _times := PackedFloat64Array()
var _frame_index := -1
var _name_regex := RegEx.create_from_string("\"name\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"")
var _start_time_regex := RegEx.create_from_string("\"start_time\"\\s*:\\s*(-?[0-9][0-9.eE+-]*)")

func _process(delta: float) -> void:
	if not playing or _times.is_empty():
		return
	current_time += minf(delta, MAX_STEP) * speed
	if current_time >= duration:
		if loop:
			# Hold the last frame for one poll interval, then start over
			if current_time >= duration + clampf(poll_interval, 1.0, 60.0):
				seek(0.0)
				return
		else:
			current_time = duration
			_emit_current_frame()
			pause()
			return
	_emit_current_frame()

## Value of a "--name=value" user argument (after "--" on the command line).
## Returns null when absent and "" for a bare "--name" flag.
static func get_user_arg(arg_name: String) -> Variant:
	var prefix := "--" + arg_name
	var args := OS.get_cmdline_user_args()
	# A web page has no command line: its address's ?quality=<preset> is the safe mode (main.gd)
	if OS.has_feature("web") and arg_name in WEB_QUERY_ARGS:
		var query = JavaScriptBridge.eval("location.search", true)
		if query is String:
			for pair in query.trim_prefix("?").split("&", false):
				args.append("--" + pair.uri_decode())
	for arg in args:
		if arg == prefix:
			return ""
		if arg.begins_with(prefix + "="):
			return arg.substr(prefix.length() + 1).strip_edges().trim_prefix("\"").trim_suffix("\"")
	return null

## Path given with --replay=<path>, or "" when none
static func get_cmdline_replay_path() -> String:
	var value = get_user_arg("replay")
	return value if value is String else ""

## Lists available replays in this order (the first one that loads is played at startup):
## 1. the --replay=<path> file, if any,
## 2. external files in <exe folder>/replays/*.json (not scanned from the editor),
## 3. bundled files in res://replays/*.json.
## Within groups 2 and 3, the newest recording comes first (header "start_time", descending);
## files whose start_time cannot be read come after, sorted by file name.
## Entries: {path, name, builtin, start_time (null when unknown)}
func discover() -> Array:
	external_dir = OS.get_executable_path().get_base_dir().path_join(EXTERNAL_DIR_NAME)
	# Running from the editor binary, the executable folder is the Godot install, not a build; a web page has no folder
	external_scanned = not OS.has_feature("editor") and not OS.has_feature("web")
	var entries: Array = []
	if external_scanned:
		entries.append_array(_scan_dir(external_dir, false))
	entries.append_array(_scan_dir(BUILTIN_DIR, true))

	var arg_path := get_cmdline_replay_path()
	if arg_path != "" and FileAccess.file_exists(arg_path):
		var arg_entry = null
		for entry in entries:
			if _same_path(entry["path"], arg_path):
				arg_entry = entry
		if arg_entry == null:
			arg_entry = _make_entry(arg_path, false)
		else:
			entries.erase(arg_entry)
		entries.push_front(arg_entry)
	return entries

func load_replay(path: String) -> bool:
	last_error = ""
	if path == "":
		return _fail("No replay file given")
	if not FileAccess.file_exists(path):
		return _fail("Replay file not found: " + path)
	var text := FileAccess.get_file_as_string(path)
	if text == "":
		return _fail("Could not read replay file %s (%s)" % [path, error_string(FileAccess.get_open_error())])
	if text.unicode_at(0) == 0xFEFF: # UTF-8 BOM (no BOM literal: exported scripts lose it)
		text = text.substr(1)
	var json := JSON.new()
	if json.parse(text) != OK:
		return _fail("Invalid JSON in %s (line %d: %s)" % [path.get_file(), json.get_error_line(), json.get_error_message()])
	text = "" # 4 bytes per character (57 MB for the bundled hour): freed before the scene is rebuilt
	var root = json.data
	if not root is Dictionary:
		return _fail("%s is not a replay file (expected a JSON object)" % path.get_file())
	if root.get("format") != FORMAT:
		return _fail("%s is not a replay file (format is %s, expected \"%s\")" % [path.get_file(), str(root.get("format")), FORMAT])
	var version = root.get("version")
	if not (version is float or version is int) or int(version) < 1 or int(version) > MAX_VERSION:
		return _fail("%s has unsupported replay version %s (this build reads up to version %d)" % [path.get_file(), str(version), MAX_VERSION])

	var raw_frames = root.get("frames")
	if not raw_frames is Array:
		return _fail("%s has no \"frames\" list" % path.get_file())
	var pairs: Array = []
	var ascending := true
	for raw in raw_frames:
		if not raw is Dictionary:
			continue
		var t = raw.get("t")
		var data = raw.get("data")
		if not (t is float or t is int) or not data is Array:
			continue
		if not pairs.is_empty() and float(t) < pairs[-1][0]:
			ascending = false
		pairs.append([float(t), data])
	if pairs.is_empty():
		return _fail("%s contains no usable frames" % path.get_file())
	if not ascending:
		pairs.sort_custom(func(a, b): return a[0] < b[0])

	# The file is valid: replace the current replay
	var first_t: float = pairs[0][0]
	_frames = []
	_times = PackedFloat64Array()
	for pair in pairs:
		_times.append(pair[0] - first_t)
		_frames.append(pair[1])
	duration = _times[_times.size() - 1]
	start_time = to_float(root.get("start_time"), 0.0) + first_t
	utc_offset_seconds = int(to_float(root.get("utc_offset_seconds"), 0.0))
	poll_interval = to_float(root.get("poll_interval"), 15.0)
	routes = _read_routes(root.get("routes"))
	root.erase("frames")
	meta = root
	loaded_path = path
	current_time = 0.0
	_frame_index = -1
	replay_loaded.emit(meta)
	seek(0.0)
	return true

func has_frames() -> bool:
	return not _times.is_empty()

func frame_count() -> int:
	return _times.size()

## Frame times in seconds from the first frame (unix time = start_time + frame time)
func get_frame_times() -> PackedFloat64Array:
	return _times

## Entities of one frame (shared with playback: read only)
func get_frame_data(index: int) -> Array:
	return _frames[index]

func play() -> void:
	if _times.is_empty():
		return
	if not loop and current_time >= duration:
		seek(0.0)
	playing = true
	state_changed.emit()

func pause() -> void:
	playing = false
	state_changed.emit()

func toggle_playing() -> void:
	if playing:
		pause()
	else:
		play()

func seek(t: float) -> void:
	if _times.is_empty():
		return
	current_time = clampf(t, 0.0, duration)
	_frame_index = _index_at(current_time)
	frame.emit(_frames[_frame_index], true)
	state_changed.emit()

func set_speed(x: float) -> void:
	speed = maxf(x, 0.0)
	state_changed.emit()

func restart() -> void:
	seek(0.0)
	play()

## Playback position clamped to the replay (current_time runs past the end while holding the last frame)
func get_display_time() -> float:
	return clampf(current_time, 0.0, duration)

## Simulated wall clock in the recording's local time, e.g. "Sat 2025-03-01 15:42:10"
func get_clock_text() -> String:
	if _times.is_empty():
		return "--"
	var unix_time := int(floor(start_time + get_display_time())) + utc_offset_seconds
	var d := Time.get_datetime_dict_from_unix_time(unix_time)
	return "%s %04d-%02d-%02d %02d:%02d:%02d" % [WEEKDAYS[int(d["weekday"]) % 7], d["year"], d["month"], d["day"], d["hour"], d["minute"], d["second"]]

static func format_mmss(seconds: float) -> String:
	var total := int(floor(maxf(seconds, 0.0)))
	return "%02d:%02d" % [floori(total / 60.0), total % 60]

func _emit_current_frame() -> void:
	var index := _index_at(current_time)
	if index != _frame_index:
		_frame_index = index
		frame.emit(_frames[index], false)

func _index_at(t: float) -> int:
	return clampi(_times.bsearch(t, false) - 1, 0, _times.size() - 1)

func _fail(message: String) -> bool:
	last_error = message
	push_warning("Replay: " + message)
	return false

func _read_routes(raw_routes) -> Array[int]:
	var result: Array[int] = []
	if raw_routes is Array:
		for route in raw_routes:
			var route_text := str(int(route)) if (route is float or route is int) else str(route)
			if route_text.is_valid_int() and not result.has(int(route_text)):
				result.append(int(route_text))
	if result.is_empty():
		# Fall back to the routes present in the data
		for frame_data in _frames:
			for entity in frame_data:
				if entity is Dictionary and entity.has("vehicle.trip.route_id"):
					var route_text := str(entity["vehicle.trip.route_id"]).get_slice(".", 0)
					if route_text.is_valid_int() and not result.has(int(route_text)):
						result.append(int(route_text))
	result.sort()
	return result

func _scan_dir(dir_path: String, builtin: bool) -> Array:
	var entries: Array = []
	if not DirAccess.dir_exists_absolute(dir_path):
		return entries
	for file_name in DirAccess.get_files_at(dir_path):
		if file_name.get_extension().to_lower() == "json":
			entries.append(_make_entry(dir_path.path_join(file_name), builtin))
	entries.sort_custom(_newest_first)
	return entries

## Sort order of discover(): known start_time descending, then unknown ones by file name
func _newest_first(a: Dictionary, b: Dictionary) -> bool:
	var a_known: bool = a["start_time"] != null
	var b_known: bool = b["start_time"] != null
	if a_known != b_known:
		return a_known
	if a_known and a["start_time"] != b["start_time"]:
		return a["start_time"] > b["start_time"]
	var order: int = a["path"].get_file().naturalnocasecmp_to(b["path"].get_file())
	return order < 0 if order != 0 else a["path"] < b["path"]

func _make_entry(path: String, builtin: bool) -> Dictionary:
	var header := _read_header(path)
	var display_name: String = header["name"]
	if display_name == "":
		display_name = path.get_file().get_basename()
	return {"path": path, "name": display_name, "builtin": builtin, "start_time": header["start_time"]}

## Reads the "name" and "start_time" fields from the beginning of a replay file without parsing
## the whole file. Returns {name: String ("" when unknown), start_time: float or null}
func _read_header(path: String) -> Dictionary:
	var header := {"name": "", "start_time": null}
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		return header
	var buffer := file.get_buffer(mini(HEADER_BYTES, file.get_length()))
	file.close()
	# Cut at an ASCII byte so a multi-byte UTF-8 character is never split
	var cut := buffer.size()
	while cut > 0 and buffer[cut - 1] >= 0x80:
		cut -= 1
	var head := buffer.slice(0, cut).get_string_from_utf8()
	# Only top-level fields count, and the writer puts them before "frames"
	var frames_at := head.find("\"frames\"")
	var found := _name_regex.search(head)
	if found != null and (frames_at < 0 or found.get_start() < frames_at):
		var json := JSON.new()
		if json.parse("\"" + found.get_string(1) + "\"") == OK and json.data is String:
			header["name"] = json.data.strip_edges()
	found = _start_time_regex.search(head)
	if found != null and (frames_at < 0 or found.get_start() < frames_at) and found.get_string(1).is_valid_float():
		header["start_time"] = found.get_string(1).to_float()
	return header

static func _same_path(a: String, b: String) -> bool:
	return a.simplify_path().replace("\\", "/").to_lower() == b.simplify_path().replace("\\", "/").to_lower()

## A JSON number or a numeric string as a float, else default_value
static func to_float(value, default_value: float) -> float:
	if value is float or value is int:
		return float(value)
	if value is String and value.is_valid_float():
		return value.to_float()
	return default_value
