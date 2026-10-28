extends Node3D

const ReplayPlayerScript = preload("res://scripts/replay_player.gd")
const ReplayControlsScript = preload("res://scripts/replay_controls.gd")
const Occupancy = preload("res://scripts/occupancy.gd")
const OccupancyDataScript = preload("res://scripts/occupancy_data.gd")
const OccupancyPanelScript = preload("res://scripts/occupancy_panel.gd")
const SELFTEST_SPEED := 60.0
const SELFTEST_SECONDS := 5.0
const SHOT_DELAY := 2.5 # Real seconds at the default speed before the self-test screenshot
const CHART_DELAY := 1.0 # Real seconds between selecting the self-test bus and reading its chart
const HEADING_STEP_DEGREES := 0.0003 # ~30 m, used to turn a snapped bus along its bearing
const VIEW_GROUP_RADIUS := 80.0 # Replay start view: buses closer than this (m) form one busy area
const VIEW_DISTANCE := 60.0 # Replay start view: camera distance (m) to that area (close: buses are small)
const VIEW_PITCH := -45.0 # Replay start view: degrees below the horizon (steep enough to see the streets)
const VIEW_CLEARANCE := 10.0 # Replay start view: the camera stays this far (m) above the tallest building
## Stops of every bus route per direction (software/recording route-stops, from the GTFS in datasets/)
const ROUTE_STOPS_PATH := "res://assets/route_stops.json"
const STOP_HIGHLIGHT_SCALE := Vector3(1.6, 1.0, 1.6) # Poles of the selected bus's line are thicker
const MISSED_FRAMES_KEPT := 3 # Replay: a bus absent from up to this many polls in a row (45 s) stays where it was
const HIDDEN_RELEASE_DELAY := 0.5 # Seconds before a selected bus whose line was hidden is released (see _on_lines_hidden_changed)

@onready var rmq_connection = $rmq_listener
@onready var map_interpolation = $MapInterpolation

var root
var key_point_scene = preload("res://key_point.tscn")
var bus_scene = preload("res://bus.tscn")
var bus_3D_scene = preload("res://bus_3D.tscn")
var bus_stop = preload("res://bus_stop.tscn")

var routes_subscribed = [41, 140, 45, 24, 121]

var buses := {} # vehicle id -> Bus3D
var bus_container: Node3D
var replay_mode := false
var replay_player: ReplayPlayerScript
var replay_controls: ReplayControlsScript
var occupancy_data: OccupancyDataScript
var occupancy_panel: OccupancyPanelScript
var replay_entries: Array = []
var _load_errors := PackedStringArray()
var _geo_cache := {} # vehicle id -> [latitude, longitude, map position] of its last applied update
var _missed_frames := {} # vehicle id -> consecutive replay frames the bus was absent from (up to MISSED_FRAMES_KEPT)
var _view_framed := false
var _route_stops = null # route_stops.json, loaded once: {"stops": {id: {name, lat, lon}}, "routes": {route: {name, directions}}}
var _stops: Array[Dictionary] = [] # Spawned stops: {"node", "pole", "routes": {route: true}, "lines": {"route|direction": true}}
var _stop_materials := {} # "normal" / "other" -> StandardMaterial3D
var _hidden_routes := {} # route -> true: lines hidden in the Lines panel (their buses and stops)
var _highlight_route := "" # Line of the selected bus whose stops are highlighted, "" when none
var _highlight_direction := -1 # ... its direction, -1 = both
var _highlighted_stops := 0 # Shown stops highlighted for the selected bus

var _selftest_path := ""
var _selftest_logger: SelftestLogger = null
var _frames_emitted := 0
var _max_buses := 0
var _max_buses_on_map := 0
var _shot_path := ""
var _shot_saved := ""
var _shot_error := ""
var _shot_chart_path := ""
var _shot_chart_saved := ""
var _chart_stats := {}
var _chart_bus_id := ""
var _selftest_checks := {} # Selection switch, stop highlight, Lines panel and layer checkbox results

func _ready() -> void:
	# Beacon colors compensate the scene's tonemapping (before any bus, i.e. any beacon material, exists)
	Occupancy.set_environment($WorldEnvironment.environment)
	root = get_tree().get_root()
	var selftest_arg = ReplayPlayerScript.get_user_arg("selftest")
	if selftest_arg is String and selftest_arg != "":
		_selftest_path = selftest_arg
		_selftest_logger = SelftestLogger.new()
		OS.add_logger(_selftest_logger)
		var shot_arg = ReplayPlayerScript.get_user_arg("shot")
		if shot_arg is String:
			_shot_path = shot_arg
		var shot_chart_arg = ReplayPlayerScript.get_user_arg("shot-chart")
		if shot_chart_arg is String:
			_shot_chart_path = shot_chart_arg

	bus_container = Node3D.new()
	bus_container.name = "Buses"
	add_child(bus_container)

	replay_player = ReplayPlayerScript.new()
	replay_player.name = "ReplayPlayer"
	add_child(replay_player)
	replay_player.frame.connect(_on_replay_frame)
	replay_player.replay_loaded.connect(_on_replay_loaded)
	replay_player.state_changed.connect(_on_replay_state_changed)

	replay_controls = ReplayControlsScript.new()
	add_child(replay_controls)
	replay_controls.setup(replay_player)
	replay_controls.replay_selected.connect(load_replay)

	# Legend (with the stop and reference-point checkboxes), Lines panel and the chart of the selected bus
	occupancy_data = OccupancyDataScript.new()
	occupancy_panel = OccupancyPanelScript.new()
	add_child(occupancy_panel)
	occupancy_panel.setup(occupancy_data)
	occupancy_panel.close_requested.connect(func(): $Camera3D.reset_camera())
	occupancy_panel.lines_hidden_changed.connect(_on_lines_hidden_changed)
	occupancy_panel.lines_added.connect(_on_lines_added)
	occupancy_panel.layer_toggled.connect(_on_layer_toggled)
	_update_marker_legend()

	replay_mode = OS.has_feature("demo") or ReplayPlayerScript.get_user_arg("replay") != null or _selftest_path != ""
	var live_failed := false
	if not replay_mode:
		var listening := false
		if rmq_connection.has_method("StartListening"):
			rmq_connection.connect("OnMessage", _on_message)
			listening = rmq_connection.StartListening()
			if not listening:
				rmq_connection.disconnect("OnMessage", _on_message)
		if not listening:
			push_warning("Live feed unavailable (RabbitMQ not reachable), falling back to replay mode")
			replay_mode = true
			live_failed = true

	replay_controls.set_live(not replay_mode)
	if replay_mode:
		start_replay_mode(live_failed)
	else:
		routes_updated()

	if _selftest_path != "":
		_run_selftest()

func _process(_delta: float) -> void:
	_sync_occupancy_panel()
	_sync_stop_highlight()

## Shows the chart of the selected bus and keeps its clock running
func _sync_occupancy_panel() -> void:
	if replay_mode:
		occupancy_panel.set_time(replay_player.start_time + replay_player.get_display_time(), replay_player.utc_offset_seconds, true)
	else:
		occupancy_panel.set_time(Time.get_unix_time_from_system(), _system_utc_offset(), false)
	var selected = $Camera3D.selected_object
	var vehicle_id := ""
	if is_instance_valid(selected) and selected is Bus3D and buses.get(String(selected.name)) == selected:
		vehicle_id = String(selected.name)
	if vehicle_id == occupancy_panel.vehicle_id:
		return
	if vehicle_id == "":
		occupancy_panel.hide_chart()
	else:
		occupancy_panel.show_bus(vehicle_id)

## Offset of the system time zone from UTC in seconds (daylight saving time included)
static func _system_utc_offset() -> int:
	var local_as_utc := Time.get_unix_time_from_datetime_dict(Time.get_datetime_dict_from_system())
	return roundi((local_as_utc - Time.get_unix_time_from_system()) / 60.0) * 60

## (Re)creates the stops of the subscribed routes, both directions, one pole per stop shared by its lines
func spawn_bus_stops():
	for child in $BusStops.get_children():
		$BusStops.remove_child(child) # Frees the names at once (queue_free alone keeps them for a frame)
		child.queue_free()
	_stops = []
	var data = _load_route_stops()
	var routes: Dictionary = data.get("routes", {})
	var stop_table: Dictionary = data.get("stops", {})
	var by_id := {}
	for route_id in routes_subscribed:
		var route := _route_text(route_id)
		var route_entry = routes.get(route)
		if not route_entry is Dictionary or not route_entry.get("directions") is Dictionary:
			continue
		var directions: Dictionary = route_entry["directions"]
		for direction in directions:
			var stop_ids = directions[direction].get("stops") if directions[direction] is Dictionary else null
			if not stop_ids is Array:
				continue
			for stop_id in stop_ids:
				var stop = by_id.get(stop_id)
				if stop == null:
					var info = stop_table.get(stop_id)
					if not info is Dictionary or not _is_number(info.get("lat")) or not _is_number(info.get("lon")):
						continue
					var node: Node3D = bus_stop.instantiate()
					node.name = "Stop %s" % stop_id
					var real_stop_pos: Vector2 = map_interpolation.get_position_by_closest_triangulation(float(info["lat"]), float(info["lon"]))
					node.position = Vector3(real_stop_pos[0], 0, real_stop_pos[1])
					$BusStops.add_child(node)
					stop = {"node": node, "pole": node.get_child(0), "routes": {}, "lines": {}}
					by_id[stop_id] = stop
					_stops.append(stop)
				stop["routes"][route] = true
				stop["lines"]["%s|%s" % [route, direction]] = true
	_apply_stop_styles()

## route_stops.json (an empty dictionary when it cannot be read: no stops then)
func _load_route_stops() -> Dictionary:
	if _route_stops == null:
		_route_stops = load_json_file(ROUTE_STOPS_PATH)
		if not _route_stops is Dictionary or not _route_stops.get("routes") is Dictionary or not _route_stops.get("stops") is Dictionary:
			push_warning("No bus stops: %s is not a route-stops file (make it with software/recording route-stops)" % ROUTE_STOPS_PATH)
			_route_stops = {}
	return _route_stops

## Highlights the stops of the selected bus's line (its direction when known) and fades the others; hides the stops
## whose lines are all hidden in the Lines panel
func _apply_stop_styles() -> void:
	if _stop_materials.is_empty():
		for key in ["normal", "other"]:
			var mat := StandardMaterial3D.new()
			mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
			mat.albedo_color = Occupancy.screen_albedo(OccupancyPanelScript.STOP_COLOR if key == "normal" else OccupancyPanelScript.OTHER_STOP_COLOR)
			_stop_materials[key] = mat
	var line := "%s|%d" % [_highlight_route, _highlight_direction]
	var highlighted := PackedByteArray()
	highlighted.resize(_stops.size())
	_highlighted_stops = 0
	for i in _stops.size():
		var stop := _stops[i]
		var node: Node3D = stop["node"]
		node.visible = stop["routes"].keys().any(func(route): return not _hidden_routes.has(route))
		if node.visible and _highlight_route != "" and (stop["lines"].has(line) if _highlight_direction >= 0 else stop["routes"].has(_highlight_route)):
			highlighted[i] = 1
			_highlighted_stops += 1
	# Faded only while some stops are highlighted (a line without stop data leaves them as they are)
	for i in _stops.size():
		var is_highlighted := highlighted[i] == 1
		_stops[i]["pole"].material_override = _stop_materials["normal" if is_highlighted or _highlighted_stops == 0 else "other"]
		_stops[i]["node"].scale = STOP_HIGHLIGHT_SCALE if is_highlighted else Vector3.ONE

## Follows the selected bus's line and direction (they change at a terminus, or when a vehicle starts another route)
func _sync_stop_highlight() -> void:
	var route := ""
	var direction := -1
	var selected = $Camera3D.selected_object
	if is_instance_valid(selected) and selected is Bus3D and buses.get(String(selected.name)) == selected:
		route = selected.route_id
		direction = selected.direction_id
	if route == _highlight_route and direction == _highlight_direction:
		return
	_highlight_route = route
	_highlight_direction = direction
	_apply_stop_styles()
	_update_marker_legend()

## Legend entries of the stops (with the highlighted line) and reference points that the scene shows
func _update_marker_legend() -> void:
	var line_stops := ""
	if _highlighted_stops > 0:
		line_stops = "Stops of route %s" % _highlight_route
		var headsign = _route_direction(_highlight_route, _highlight_direction).get("headsign", "")
		if headsign is String and headsign != "":
			line_stops += " (%s)" % headsign # STM's bus headsigns are mostly a direction: "Nord", "Est"...
	occupancy_panel.set_marker_legend($BusStops.visible, $MapInterpolation/ReferencePoints.visible, line_stops)

## route_stops.json entry of a route's direction ({} when unknown)
func _route_direction(route: String, direction: int) -> Dictionary:
	var route_entry = _load_route_stops().get("routes", {}).get(route)
	if direction < 0 or not route_entry is Dictionary or not route_entry.get("directions") is Dictionary:
		return {}
	var entry = route_entry["directions"].get(str(direction))
	return entry if entry is Dictionary else {}

## Legend checkboxes: shows or hides the bus stops or the reference points
func _on_layer_toggled(layer: String, shown: bool) -> void:
	var node: Node3D = $BusStops if layer == "stops" else $MapInterpolation/ReferencePoints
	node.visible = shown
	_update_marker_legend()

## Lines panel: hides the buses and stops of these lines; a hidden selected bus is released a moment later
func _on_lines_hidden_changed(routes: PackedStringArray) -> void:
	_hidden_routes = {}
	for route in routes:
		_hidden_routes[route] = true
	for bus_3d in buses.values():
		_apply_route_filter(bus_3d, false)
	_apply_stop_styles()
	_update_marker_legend()
	_release_hidden_selection_later()

## A double-click on the selected bus's line first hides that line (its first click), then shows only it: the bus is
## released only if its line is still hidden after HIDDEN_RELEASE_DELAY
func _release_hidden_selection_later() -> void:
	await get_tree().create_timer(HIDDEN_RELEASE_DELAY).timeout
	var selected = $Camera3D.selected_object
	if is_instance_valid(selected) and selected is Bus3D and selected.filtered_out:
		$Camera3D.reset_camera()

func _apply_route_filter(bus_3d: Bus3D, release := true) -> void:
	bus_3d.set_filtered_out(_hidden_routes.has(bus_3d.route_id))
	if release and bus_3d.filtered_out and $Camera3D.selected_object == bus_3d:
		$Camera3D.reset_camera()

## Lines typed in the Lines panel: listed and shown. A replay has only its recorded routes; live, a line shows buses
## only if the ingestion service publishes that route
func _on_lines_added(routes: PackedStringArray) -> void:
	var shown := PackedStringArray() # Listed now, and shown (a line already listed but hidden is shown again)
	var new_count := 0
	var not_recorded := PackedStringArray()
	var invalid := PackedStringArray()
	for text in routes:
		if not text.is_valid_int() or text.to_int() <= 0:
			invalid.append(text)
			continue
		var route_id := text.to_int()
		if replay_mode and replay_player.has_frames() and not replay_player.routes.has(route_id):
			not_recorded.append(str(route_id))
			continue
		if not route_id in routes_subscribed:
			routes_subscribed.append(route_id)
			new_count += 1
		shown.append(str(route_id))
	if new_count > 0:
		routes_updated()
	occupancy_panel.show_lines(shown)
	var notes := PackedStringArray()
	if not not_recorded.is_empty():
		notes.append("%s %s not in this recording" % [_plural("Line", not_recorded), "is" if not_recorded.size() == 1 else "are"])
	if not invalid.is_empty():
		notes.append("“%s”: not a line number" % "”, “".join(invalid))
	if not replay_mode and new_count > 0:
		notes.append("Live: buses show if the ingestion service publishes %s" % ("this line" if new_count == 1 else "these lines"))
	occupancy_panel.show_lines_note(". ".join(notes))

## "Line 45" / "Lines 45, 121"
static func _plural(word: String, items: PackedStringArray) -> String:
	return "%s%s %s" % [word, "" if items.size() == 1 else "s", ", ".join(items)]

## "51" for 51, 51.0 or "51"
static func _route_text(route) -> String:
	return str(int(route)) if (route is float and route == floorf(route)) or route is int else str(route)

static func _is_number(value) -> bool:
	return value is float or value is int

## Discovers the replays and plays the one given with --replay=<path> (strict), otherwise
## the first discovered one that loads (a broken drop-in file does not hide the bundled replay)
func start_replay_mode(live_failed := false) -> void:
	replay_entries = replay_player.discover()
	var candidates: Array = []
	var explicit_path := ReplayPlayerScript.get_cmdline_replay_path()
	if explicit_path != "":
		candidates.append(explicit_path)
	else:
		for entry in replay_entries:
			candidates.append(entry["path"])
	replay_controls.set_replays(replay_entries, candidates[0] if not candidates.is_empty() else "")
	if candidates.is_empty():
		var where: String = replay_player.external_dir if replay_player.external_scanned else ReplayPlayerScript.BUILTIN_DIR
		replay_controls.show_message("No replay found. Put stm-dt-replay *.json files in " + where, true)
		routes_updated()
		return
	_load_errors = load_first_replay(candidates)
	if not replay_player.has_frames():
		replay_controls.show_message("Could not load replay: " + "; ".join(_load_errors), true)
		routes_updated()
		return
	_view_framed = frame_busy_area()
	if not _load_errors.is_empty():
		replay_controls.show_message("Skipped unreadable replay: " + "; ".join(_load_errors), true)
	elif live_failed:
		replay_controls.show_message("Live feed unavailable (RabbitMQ not reachable): showing a recording instead.")

## Plays the first of paths that loads; returns the load errors of the ones skipped
func load_first_replay(paths: Array) -> PackedStringArray:
	var errors := PackedStringArray()
	for path in paths:
		if replay_player.load_replay(path):
			replay_player.play()
			replay_controls.set_replays(replay_entries, path)
			break
		errors.append(replay_player.last_error)
	return errors

func load_replay(path: String) -> bool:
	if replay_player.load_replay(path):
		replay_player.play()
		return true
	replay_controls.show_message("Could not load replay: " + replay_player.last_error, true)
	replay_controls.set_replays(replay_entries, replay_player.loaded_path)
	if not replay_player.has_frames():
		routes_updated()
	return false

func _on_replay_loaded(_meta: Dictionary) -> void:
	clear_buses()
	occupancy_panel.show_lines_note("") # About the previous replay's lines
	occupancy_data.load_replay(replay_player)
	occupancy_panel.refresh()
	if not replay_player.routes.is_empty():
		routes_subscribed = []
		for route_id in replay_player.routes:
			routes_subscribed.append(route_id)
	routes_updated()

func _on_replay_frame(data: Array, is_seek: bool) -> void:
	_frames_emitted += 1
	apply_bus_data(data, is_seek, true)
	_max_buses = maxi(_max_buses, buses.size())
	if _selftest_path != "":
		_max_buses_on_map = maxi(_max_buses_on_map, count_buses_on_map())

func _on_replay_state_changed() -> void:
	Bus3D.playback_speed = maxf(replay_player.speed, 1.0) if replay_mode else 1.0

func _on_message(message):
	var json := JSON.new()
	if json.parse(message) != OK or not json.data is Dictionary or not json.data.get('data') is Array:
		push_warning("Ignoring malformed RabbitMQ message")
		return
	occupancy_data.record_live(json.data['data'], Time.get_unix_time_from_system())
	apply_bus_data(json.data['data'])

## Moves (or creates) the bus of every entity on a subscribed route.
## snap: jump to the new positions (replay seek). prune: remove the buses absent from bus_data (at once on a seek,
## otherwise after MISSED_FRAMES_KEPT frames in a row).
func apply_bus_data(bus_data: Array, snap := false, prune := false) -> void:
	var debug_prints := OS.is_debug_build() and not replay_mode
	if debug_prints:
		print(bus_data)
	var updated := {}
	for entity in bus_data:
		if not entity is Dictionary:
			continue
		var route_id = entity.get('vehicle.trip.route_id')
		if route_id == null or not int(route_id) in routes_subscribed:
			continue
		var entity_id := _vehicle_key(entity)
		if entity_id == "" or not entity.has('vehicle.position.latitude') or not entity.has('vehicle.position.longitude'):
			continue
		updated[entity_id] = true
		# Occupancy and line first: they change even when the bus stays put or has no GPS fix
		var bus_3d: Bus3D = buses.get(entity_id)
		if bus_3d != null:
			_update_bus_state(bus_3d, entity)
		var latitude := _as_float(entity.get('vehicle.position.latitude'))
		var longitude := _as_float(entity.get('vehicle.position.longitude'))
		# (0, 0) means the bus has no GPS fix (seen in real feeds): keep it where it was. After a
		# seek "where it was" belongs to another time, so let the prune remove it until its next fix
		if absf(latitude) < 1.0 and absf(longitude) < 1.0:
			if snap:
				updated.erase(entity_id)
			continue
		# The feed often repeats a position: nothing to do then. A seek always places the bus, and
		# a new bus has no cache entry (entries are removed with their bus)
		var cached = _geo_cache.get(entity_id)
		var same_place: bool = cached != null and cached[0] == latitude and cached[1] == longitude
		if same_place and not snap:
			continue
		var is_new := bus_3d == null
		var bearing := _as_float(entity.get('vehicle.position.bearing', 0))
		var speed := _as_float(entity.get('vehicle.position.speed', 0))
		var has_bearing: bool = entity.has('vehicle.position.bearing')
		var raw_bearing := bearing
		bearing -= 20 # Manual corrective delta, I don't know why I need it
		if debug_prints:
			print("Latitude: " + str(latitude))
			print("Longitude: " + str(longitude))
			print("Bearing: " + str(bearing))
			print("Speed: " + str(speed))
		if is_new:
			bus_3d = bus_3D_scene.instantiate()
			bus_3d.name = entity_id
			bus_container.add_child(bus_3d)
			buses[entity_id] = bus_3d
			_update_bus_state(bus_3d, entity)
		var real_pos: Vector2 = cached[2] if same_place else map_interpolation.get_position_by_closest_triangulation(latitude, longitude)
		_geo_cache[entity_id] = [latitude, longitude, real_pos]
		# New buses appear directly at their position instead of flying in from the origin
		set_transform_from_geodata(bus_3d, latitude, longitude, bearing, snap or is_new, real_pos)
		if (snap or is_new) and has_bearing:
			face_bearing(bus_3d, latitude, longitude, raw_bearing)
	if prune:
		for vehicle_id in buses.keys():
			if updated.has(vehicle_id):
				_missed_frames.erase(vehicle_id)
				continue
			# The feed often skips a bus for a poll or two: keep it (and its selection) where it was for a moment.
			# A seek shows the moment as recorded
			var missed: int = _missed_frames.get(vehicle_id, 0) + 1
			if snap or missed > MISSED_FRAMES_KEPT:
				remove_bus(vehicle_id)
			else:
				_missed_frames[vehicle_id] = missed

## Occupancy, line and direction of a bus from its latest entity; hiding by line follows a route change
func _update_bus_state(bus_3d: Bus3D, entity: Dictionary) -> void:
	bus_3d.set_occupancy(Occupancy.status_of(entity))
	bus_3d.direction_id = OccupancyDataScript.direction_key(entity)
	var route := OccupancyDataScript.route_key(entity)
	if route != bus_3d.route_id:
		bus_3d.route_id = route
		_apply_route_filter(bus_3d)

func set_transform_from_geodata(object: Bus3D, lat: float, long: float, bearing: float, snap := false, real_pos = null):
	# Get the real x and z positions from coordinates (unless the caller already has them)
	if real_pos == null:
		real_pos = map_interpolation.get_position_by_closest_triangulation(lat, long)
	# Tell the bus object that a new message was received and send it data
	object.on_update(Vector3(real_pos[0], 15, real_pos[1]), Time.get_ticks_msec(), snap)

## Turns a bus along its GTFS bearing (degrees clockwise from north) using a point a few meters ahead
func face_bearing(object: Bus3D, lat: float, long: float, bearing_degrees: float):
	var heading := deg_to_rad(bearing_degrees)
	var ahead_lat := lat + HEADING_STEP_DEGREES * cos(heading)
	var ahead_long := long + HEADING_STEP_DEGREES * sin(heading) / maxf(cos(deg_to_rad(lat)), 0.01)
	var ahead: Vector2 = map_interpolation.get_position_by_closest_triangulation(ahead_lat, ahead_long)
	object.face_towards(Vector3(ahead.x, object.global_position.y, ahead.y))

func remove_bus(vehicle_id: String) -> void:
	var bus_3d: Bus3D = buses.get(vehicle_id)
	buses.erase(vehicle_id)
	_geo_cache.erase(vehicle_id)
	_missed_frames.erase(vehicle_id)
	if bus_3d == null or not is_instance_valid(bus_3d):
		return
	if $Camera3D.selected_object == bus_3d:
		$Camera3D.reset_camera()
	# remove_child first so the name is free immediately (queue_free alone keeps it for a frame)
	bus_container.remove_child(bus_3d)
	bus_3d.queue_free()

func clear_buses() -> void:
	for vehicle_id in buses.keys():
		remove_bus(vehicle_id)

## True when the point is above the modeled city: a ray straight down hits the map (buses are skipped)
func is_over_map(point: Vector3) -> bool:
	var origin := Vector3(point.x, 1000.0, point.z)
	var query := PhysicsRayQueryParameters3D.create(origin, origin + Vector3(0, -2000, 0))
	var space_state := get_world_3d().direct_space_state
	for attempt in 8:
		var hit := space_state.intersect_ray(query)
		if hit.is_empty():
			return false
		if $Map.is_ancestor_of(hit["collider"]):
			return true
		var excluded := query.exclude
		excluded.append(hit["rid"])
		query.exclude = excluded
	return false

func count_buses_on_map() -> int:
	var count := 0
	for bus_3d in buses.values():
		if is_over_map(bus_3d.target_position):
			count += 1
	return count

## Replay start view: points the camera at the busiest group of buses on the map, since the scene's
## default view may show none of the recorded buses. Returns false (camera unchanged) when no bus is on the map.
func frame_busy_area() -> bool:
	var points: Array[Vector3] = []
	for bus_3d in buses.values():
		if is_over_map(bus_3d.target_position):
			points.append(bus_3d.target_position)
	var best: Array[Vector3] = []
	for point in points:
		var group: Array[Vector3] = []
		for other in points:
			if Vector2(other.x - point.x, other.z - point.z).length() <= VIEW_GROUP_RADIUS:
				group.append(other)
		if group.size() > best.size():
			best = group
	if best.is_empty():
		return false
	var center := Vector3.ZERO
	for point in best:
		center += point
	center /= best.size()
	# Keep the scene camera's heading, unless the area is away from the middle of the map: then look
	# inwards so the city, not the void around it, fills the background
	var camera = $Camera3D
	var map_bounds := _map_bounds()
	var view_yaw: float = camera.global_transform.basis.get_euler(EULER_ORDER_YXZ).y
	var inwards := Vector2(map_bounds.get_center().x - center.x, map_bounds.get_center().z - center.z)
	if inwards.length() > VIEW_GROUP_RADIUS:
		view_yaw = atan2(-inwards.x, -inwards.y)
	# Back off if needed so the camera is above every building (never inside a tower)
	var view_pitch := deg_to_rad(VIEW_PITCH)
	var distance := maxf(VIEW_DISTANCE, (map_bounds.end.y + VIEW_CLEARANCE - center.y) / sin(-view_pitch))
	camera.reset_camera()
	camera.frame_target(center, distance, view_yaw, view_pitch)
	return true

## World-space bounding box of the city mesh
func _map_bounds() -> AABB:
	var bounds := AABB()
	var first := true
	for mesh in $Map.find_children("*", "MeshInstance3D", true, false):
		var box: AABB = mesh.global_transform * mesh.get_aabb()
		bounds = box if first else bounds.merge(box)
		first = false
	return bounds

func _vehicle_key(entity: Dictionary) -> String:
	return OccupancyDataScript.vehicle_key(entity)

func _as_float(value, default_value := 0.0) -> float:
	if value is float or value is int:
		return float(value)
	if value is String and value.is_valid_float():
		return value.to_float()
	return default_value

func load_json_file(file_path: String):
	var file := FileAccess.open(file_path, FileAccess.READ)
	if file:
		print("Found file")
		var content := file.get_as_text()
		file.close()
		
		var json := JSON.new()
		var parse_result := json.parse(content)
		
		if parse_result == 0:
			var data = json.get_data()
			return data
		else:
			push_error("Failed to parse JSON file")
	else:
		push_error("Failed to open file: " + file_path)
	
	print("Found no data")
	return {}

## The subscribed routes changed (replay loaded, line added): their stops, and one button each in the Lines panel
func routes_updated():
	spawn_bus_stops()
	# Line buttons for the subscribed routes, named after route_stops.json
	var routes := PackedStringArray()
	var names := {}
	var route_entries: Dictionary = _load_route_stops().get("routes", {})
	for route_id in routes_subscribed:
		var route := _route_text(route_id)
		if not routes.has(route):
			routes.append(route)
		var entry = route_entries.get(route)
		if entry is Dictionary and entry.get("name") is String:
			names[route] = entry["name"]
	# The buses of a route no longer subscribed go now (in a replay they would wait out MISSED_FRAMES_KEPT)
	for vehicle_id in buses.keys():
		if buses[vehicle_id].route_id != "" and not routes.has(buses[vehicle_id].route_id):
			remove_bus(vehicle_id)
	occupancy_panel.set_routes(routes, names)
	_update_marker_legend()

## Self-test (--selftest=<result.json>): plays the default replay at 60x for ~5 s with one seek,
## then pauses and selects a bus to check its occupancy chart, writes a JSON report and quits with
## 0 (ok) or 1. Used to check exported builds without stdout.
## With --shot=<file.png> (windowed runs), first saves a screenshot of the start view at the default speed;
## with --shot-chart=<file.png>, one with the chart shown (hovered at the forecast origin, "Show actual" on).
func _run_selftest() -> void:
	if replay_player.has_frames():
		if _shot_path != "":
			await get_tree().create_timer(SHOT_DELAY).timeout
			_shot_saved = await _save_screenshot(_shot_path)
		replay_player.set_speed(SELFTEST_SPEED)
		replay_player.play()
		await get_tree().create_timer(SELFTEST_SECONDS * 0.5).timeout
		replay_player.seek(replay_player.duration * 0.5)
		await get_tree().create_timer(SELFTEST_SECONDS * 0.5).timeout
		replay_player.pause()
		var chart_bus := _pick_chart_bus()
		if chart_bus != null:
			_chart_bus_id = String(chart_bus.name)
			var camera = $Camera3D
			camera.reset_camera()
			camera.select_object(chart_bus)
			await get_tree().create_timer(CHART_DELAY).timeout
			_chart_stats = occupancy_panel.chart_stats()
			if _shot_chart_path != "":
				var now := replay_player.start_time + replay_player.get_display_time()
				var forecast = occupancy_data.forecast_at(String(chart_bus.name), now)
				occupancy_panel.set_show_actual(true)
				occupancy_panel.hover_time(forecast["origin"] if forecast != null else now)
				await get_tree().create_timer(CHART_DELAY).timeout
				_shot_chart_saved = await _save_screenshot(_shot_chart_path)
			await _check_selection_and_lines(chart_bus)
	_finish_selftest()

## Self-test of the stops of the selected bus's line, of selecting another bus (the first one must be released), of the
## Lines panel and of the legend checkboxes; results in _selftest_checks. The replay is paused
func _check_selection_and_lines(first_bus: Bus3D) -> void:
	var camera = $Camera3D
	await get_tree().process_frame
	var route := first_bus.route_id
	_selftest_checks["stops_spawned"] = _stops.size()
	_selftest_checks["routes_with_stop_data"] = routes_subscribed.filter(func(route_id): return _load_route_stops().get("routes", {}).has(_route_text(route_id))).size()
	_selftest_checks["highlighted_stops"] = _highlighted_stops
	_selftest_checks["highlighted_line"] = "%s|%d" % [_highlight_route, _highlight_direction]
	_selftest_checks["line_has_stop_data"] = _load_route_stops().get("routes", {}).has(route)
	# The highlight is the first bus's line and direction, and only its stops are highlighted
	_selftest_checks["highlight_ok"] = _highlights_line(first_bus)

	var second: Bus3D = null
	var ids := buses.keys()
	ids.sort()
	for vehicle_id in ids:
		var other: Bus3D = buses[vehicle_id]
		if other == first_bus:
			continue
		if second == null:
			second = other
		if other.route_id != route or other.direction_id != first_bus.direction_id:
			second = other # Preferably another line or direction: the highlight must change
			break
	_selftest_checks["selection_switch_ok"] = true
	if second != null:
		camera.select_object(second)
		await get_tree().process_frame
		var outline = first_bus.bus_mesh.get_surface_override_material(0).next_pass.get_shader_parameter("outline_color")
		_selftest_checks["selection_switch_ok"] = not first_bus.selected and second.selected and outline == first_bus.original_outline_color \
			and camera.selected_object == second and occupancy_panel.vehicle_id == String(second.name)
		_selftest_checks["highlight_follows_ok"] = _highlights_line(second)

	# Lines panel, as a user clicks it: hide the first bus's line, show it again, double-click it (only this line), All,
	# hide it and type it in the field (shown again), type a line that is not recorded (a note), and double-click the line
	# of the selected bus (it stays selected). Every step must leave the buses and stops consistent with the hidden lines
	var steps := {}
	var settle := HIDDEN_RELEASE_DELAY + 0.2 # Past the delayed release of a hidden selected bus
	var button := occupancy_panel.line_button(route)
	if button == null:
		_selftest_checks["lines_error"] = "no line button for route %s" % route
	else:
		button.pressed.emit()
		await get_tree().create_timer(settle).timeout
		var released: bool = second == null or second.route_id != route or camera.selected_object != second
		steps["hide"] = occupancy_panel.hidden_lines() == PackedStringArray([route]) and released and _lines_consistent()
		_selftest_checks["hidden_buses"] = buses.values().filter(func(bus_3d): return not bus_3d.visible).size()
		button.pressed.emit()
		await get_tree().process_frame
		steps["show_again"] = occupancy_panel.hidden_lines().is_empty() and _lines_consistent()
		var double_click := InputEventMouseButton.new()
		double_click.button_index = MOUSE_BUTTON_LEFT
		double_click.pressed = true
		double_click.double_click = true
		button.gui_input.emit(double_click)
		await get_tree().process_frame
		var others := PackedStringArray()
		for route_id in routes_subscribed:
			if _route_text(route_id) != route:
				others.append(_route_text(route_id))
		others.sort()
		steps["only_this_line"] = occupancy_panel.hidden_lines() == others and _lines_consistent() 			and buses.values().any(func(bus_3d): return bus_3d.visible)
		_selftest_checks["only_line_buses_shown"] = buses.values().filter(func(bus_3d): return bus_3d.visible).size()
		occupancy_panel.all_button().pressed.emit()
		await get_tree().process_frame
		steps["all"] = occupancy_panel.hidden_lines().is_empty() and _lines_consistent()
		button.pressed.emit()
		occupancy_panel.submit_line_text(route)
		await get_tree().process_frame
		steps["typed_line_shown"] = occupancy_panel.hidden_lines().is_empty() and _lines_consistent() and occupancy_panel.lines_note() == ""
		occupancy_panel.submit_line_text("9999")
		await get_tree().process_frame
		_selftest_checks["unrecorded_line_note"] = occupancy_panel.lines_note()
		steps["unrecorded_line"] = occupancy_panel.line_button("9999") == null and occupancy_panel.lines_note().contains("9999")
		occupancy_panel.show_lines_note("")
		# A real double-click: the first click hides the line for a moment, then the double-click shows only it
		camera.select_object(first_bus)
		button.pressed.emit()
		button.gui_input.emit(double_click)
		await get_tree().create_timer(settle).timeout
		steps["double_click_keeps_selection"] = camera.selected_object == first_bus and first_bus.visible 			and occupancy_panel.hidden_lines() == others
		occupancy_panel.all_button().pressed.emit()
		await get_tree().process_frame
	_selftest_checks["lines_steps"] = steps
	_selftest_checks["lines_ok"] = button != null and steps.values().all(func(ok): return ok)

	# Legend checkboxes, as a user clicks them: stops off and on, reference points on and off (hidden at start)
	var layers := {}
	var stops_check := occupancy_panel.layer_check("stops")
	var references_check := occupancy_panel.layer_check("references")
	stops_check.button_pressed = false
	layers["stops_off"] = not $BusStops.visible
	stops_check.button_pressed = true
	layers["stops_on"] = $BusStops.visible
	layers["references_start_hidden"] = not $MapInterpolation/ReferencePoints.visible and not references_check.button_pressed
	references_check.button_pressed = true
	layers["references_on"] = $MapInterpolation/ReferencePoints.visible
	references_check.button_pressed = false
	layers["references_off"] = not $MapInterpolation/ReferencePoints.visible
	_selftest_checks["layers"] = layers
	_selftest_checks["layers_ok"] = layers.values().all(func(ok): return ok)

## Self-test: buses and stops match the lines hidden in the Lines panel. A bus is shown (and clickable) unless its line
## is hidden; a stop is shown while one of its lines is
func _lines_consistent() -> bool:
	for bus_3d in buses.values():
		var hidden: bool = _hidden_routes.has(bus_3d.route_id)
		if bus_3d.visible == hidden or (bus_3d.body.collision_layer == 0) != hidden:
			return false
	for stop in _stops:
		if stop["node"].visible != stop["routes"].keys().any(func(stop_route): return not _hidden_routes.has(stop_route)):
			return false
	return true

## Self-test: the highlighted stops are exactly the shown stops of this bus's line and direction (both directions when
## its direction is unknown)
func _highlights_line(bus_3d: Bus3D) -> bool:
	if _highlight_route != bus_3d.route_id or _highlight_direction != bus_3d.direction_id:
		return false
	var line := "%s|%d" % [bus_3d.route_id, bus_3d.direction_id]
	for stop in _stops:
		var on_line: bool = stop["lines"].has(line) if bus_3d.direction_id >= 0 else stop["routes"].has(bus_3d.route_id)
		if (stop["node"].scale == STOP_HIGHLIGHT_SCALE) != (on_line and stop["node"].visible):
			return false
	return true

## Self-test chart bus: preferably one with a current forecast, above the map (for the screenshot)
func _pick_chart_bus() -> Bus3D:
	var now := replay_player.start_time + replay_player.get_display_time()
	var best: Bus3D = null
	var best_score := 0
	var ids := buses.keys()
	ids.sort()
	for vehicle_id in ids:
		if not occupancy_data.has_history(vehicle_id):
			continue
		var bus_3d: Bus3D = buses[vehicle_id]
		var score := 1
		if occupancy_data.forecast_at(vehicle_id, now) != null:
			score += 4
		if is_over_map(bus_3d.target_position):
			score += 2
		if score > best_score:
			best = bus_3d
			best_score = score
	return best

## Alive buses whose beacon shows a known occupancy status
func count_buses_with_occupancy() -> int:
	var count := 0
	for bus_3d in buses.values():
		if bus_3d.occupancy_status != Occupancy.NO_DATA:
			count += 1
	return count

func _finish_selftest() -> void:
	var errors: Array = []
	var loaded := replay_player.has_frames()
	# Load errors of the replays tried at startup (skipped broken files, or the reason nothing loaded)
	errors.append_array(Array(_load_errors))
	if not loaded and _load_errors.is_empty():
		errors.append("No replay loaded (none found)")
	if loaded and _frames_emitted < 3:
		errors.append("Only %d frame(s) emitted" % _frames_emitted)
	if loaded and buses.is_empty():
		errors.append("No bus alive at the end of the self-test")
	if _shot_error != "":
		errors.append(_shot_error)
	var buses_with_occupancy := count_buses_with_occupancy()
	var occupancy_ok := true
	if loaded and occupancy_data.occupancy_known and buses_with_occupancy == 0:
		errors.append("The replay has occupancy levels but no bus shows one")
		occupancy_ok = false
	if loaded and occupancy_data.forecast_in_replay:
		if occupancy_data.forecast_error != "":
			errors.append(occupancy_data.forecast_error)
			occupancy_ok = false
		elif _chart_stats.get("forecast_points", 0) == 0:
			errors.append("The replay has a forecast but the chart of bus %s shows none (%s)" % [_chart_stats.get("vehicle", "-"), _chart_stats.get("message", "no bus selected")])
			occupancy_ok = false
	# The chart itself, with or without a forecast (the replay is paused: now is when the stats were taken)
	if loaded and not buses.is_empty():
		if not _chart_stats.get("visible", false) or _chart_stats.get("vehicle", "") != _chart_bus_id:
			errors.append("The chart is not shown for the selected bus %s (chart: visible=%s, bus \"%s\")" % [_chart_bus_id if _chart_bus_id != "" else "(none could be selected)", str(_chart_stats.get("visible", false)), _chart_stats.get("vehicle", "")])
			occupancy_ok = false
		elif Occupancy.is_level(occupancy_data.status_at(_chart_bus_id, replay_player.start_time + replay_player.get_display_time())) and _chart_stats.get("history_points", 0) == 0:
			errors.append("The chart of bus %s shows no occupancy history" % _chart_bus_id)
			occupancy_ok = false
	# Selecting another bus, the stops of the selected line and the line filter
	var lines_ok := true
	if loaded and _chart_bus_id != "":
		if not _selftest_checks.get("selection_switch_ok", false):
			errors.append("Selecting another bus did not release bus %s" % _chart_bus_id)
			lines_ok = false
		if _selftest_checks.get("routes_with_stop_data", 0) > 0 and _selftest_checks.get("stops_spawned", 0) == 0:
			errors.append("No bus stop was placed, though %s has stops for the replay's routes" % ROUTE_STOPS_PATH)
			lines_ok = false
		if _selftest_checks.get("line_has_stop_data", false) and (_selftest_checks.get("highlighted_stops", 0) == 0 \
				or not _selftest_checks.get("highlight_ok", false) or not _selftest_checks.get("highlight_follows_ok", true)):
			errors.append("The stops of the selected bus's line are not highlighted right: %s" % JSON.stringify(_selftest_checks))
			lines_ok = false
		if not _selftest_checks.get("lines_ok", false):
			errors.append("The Lines panel failed: %s" % JSON.stringify(_selftest_checks))
			lines_ok = false
		if not _selftest_checks.get("layers_ok", false):
			errors.append("The legend checkboxes failed: %s" % JSON.stringify(_selftest_checks.get("layers", {})))
			lines_ok = false
	var logged := _selftest_logger.take()
	errors.append_array(logged["errors"])
	var ok: bool = loaded and _frames_emitted >= 3 and not buses.is_empty() and occupancy_ok and lines_ok and logged["script_errors"] == 0

	var found: Array = []
	for entry in replay_entries:
		found.append({"name": entry["name"], "path": entry["path"], "builtin": entry["builtin"], "start_time": entry["start_time"]})
	var result := {
		"ok": ok,
		"mode": "replay" if replay_mode else "live",
		"replays_found": found,
		"external_dir": replay_player.external_dir,
		"external_scanned": replay_player.external_scanned,
		"loaded": replay_player.loaded_path,
		"replay_name": str(replay_player.meta.get("name", "")),
		"frame_count": replay_player.frame_count(),
		"duration": replay_player.duration,
		"frames_emitted": _frames_emitted,
		"buses_alive": buses.size(),
		"max_buses": _max_buses,
		# Buses above the modeled city (not part of ok: a replay may legitimately be off the map)
		"buses_on_map": count_buses_on_map(),
		"max_buses_on_map": _max_buses_on_map,
		"view_framed": _view_framed,
		"shot": _shot_saved,
		"buses_with_occupancy": buses_with_occupancy,
		"occupancy_in_replay": occupancy_data.occupancy_known,
		"forecast_in_replay": occupancy_data.forecast_in_replay,
		"forecast_error": occupancy_data.forecast_error,
		"forecast_model": occupancy_data.model_name,
		"forecast_vehicles": occupancy_data.vehicle_count,
		"forecast_origins": occupancy_data.origin_count,
		"chart_visible": _chart_stats.get("visible", false),
		"chart_vehicle": _chart_stats.get("vehicle", ""),
		"chart_history_points": _chart_stats.get("history_points", 0),
		"chart_forecast_points": _chart_stats.get("forecast_points", 0),
		"chart_message": _chart_stats.get("message", ""),
		"chart_rows": _chart_stats.get("rows", []),
		"legend": _chart_stats.get("legend", []),
		"lines": _selftest_checks,
		"shot_chart": _shot_chart_saved,
		"script_errors": logged["script_errors"],
		"errors": errors,
		"godot_version": Engine.get_version_info()["string"],
		"debug_build": OS.is_debug_build(),
		"demo_feature": OS.has_feature("demo"),
	}
	var exit_code := 0 if ok else 1
	DirAccess.make_dir_recursive_absolute(_selftest_path.get_base_dir())
	var file := FileAccess.open(_selftest_path, FileAccess.WRITE)
	if file:
		file.store_string(JSON.stringify(result, "  "))
		file.close()
	else:
		printerr("Self-test: cannot write " + _selftest_path)
		exit_code = 2
	OS.remove_logger(_selftest_logger)
	get_tree().quit(exit_code)

## Saves the current view as a PNG; returns the path, or "" (reason in _shot_error).
## Headless runs render nothing, so there is no screenshot then.
func _save_screenshot(path: String) -> String:
	if DisplayServer.get_name() == "headless":
		_shot_error = "Screenshot skipped: headless run (nothing is rendered)"
		return ""
	await RenderingServer.frame_post_draw
	var image := get_viewport().get_texture().get_image()
	if image == null or image.is_empty():
		_shot_error = "Screenshot failed: empty viewport image"
		return ""
	DirAccess.make_dir_recursive_absolute(path.get_base_dir())
	var save_error := image.save_png(path)
	if save_error != OK:
		_shot_error = "Screenshot failed: cannot write %s (%s)" % [path, error_string(save_error)]
		return ""
	return path

## Collects engine/script errors during the self-test (stdout is not available in exported builds)
class SelftestLogger extends Logger:
	var _mutex := Mutex.new()
	var _errors: Array[String] = []
	var _script_errors := 0

	func _log_error(function: String, file: String, line: int, code: String, rationale: String, _editor_notify: bool, error_type: int, _script_backtraces: Array[ScriptBacktrace]) -> void:
		if error_type == ERROR_TYPE_WARNING:
			return
		_mutex.lock()
		if _errors.size() < 50:
			_errors.append("%s: %s (%s:%d %s)" % ["SCRIPT ERROR" if error_type == ERROR_TYPE_SCRIPT else "ERROR", rationale if rationale != "" else code, file, line, function])
		if error_type == ERROR_TYPE_SCRIPT:
			_script_errors += 1
		_mutex.unlock()

	func take() -> Dictionary:
		_mutex.lock()
		var result := {"errors": _errors.duplicate(), "script_errors": _script_errors}
		_mutex.unlock()
		return result
