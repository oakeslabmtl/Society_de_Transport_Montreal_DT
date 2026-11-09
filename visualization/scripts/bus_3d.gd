extends Node3D

class_name  Bus3D

const Occupancy = preload("res://scripts/occupancy.gd")
const MapHeights = preload("res://scripts/map_heights.gd")
const DEFAULT_OUTLINE := Color(0.97, 0.97, 0.97)
const NO_GROUND_Y := 20.0 # Height of a bus where there is no city under it
const FOLLOW_SPEED := 4.0 # How fast the bus glides to its new position (share of the way per second, at 1x)
## How fast the ghost reaches the position extrapolated from the last two updates: 1 / 0.07 = 14 s at 1x, about one
## feed poll
const GHOST_RATE := 0.07

@onready var ghost = $ghost
@onready var ghost_mesh = $ghost/ghost_mesh
@onready var bus = $bus
@onready var bus_mesh = $bus/bus_mesh
@onready var body: StaticBody3D = $bus/StaticBody3D
var target_position : Vector3 = position
var estimated_position : Vector3 = position
var last_position : Vector3 = position
var selected := false
var outline_color : Color # The selected bus's outline (set in _ready)
var original_outline_color : Color
var occupancy_status := -2 # Not applied yet (-1 = no data), so the first set_occupancy always colors the beacon
var route_id := "" # Route of the bus's latest update ("" unknown)
var direction_id := -1 # Direction (0 or 1) of its trip, -1 unknown
var filtered_out := false # Hidden by the line filter
var _collision_layer := 1
var _ghost_t := 0.0 # The ghost's way from the bus to estimated_position (0 to 1)

# Replay speed multiplier (1.0 in live mode), scales the smoothing so buses keep up with fast playback
static var playback_speed := 1.0
static var ground: MapHeights = null # The city's height grid (main.gd): the buses stand on it

func _ready() -> void:
	var outline = bus_mesh.get_surface_override_material(0).next_pass.get_shader_parameter("outline_color")
	original_outline_color = outline if outline is Color else DEFAULT_OUTLINE
	# The beacon and the ghost are markers: their shadows would streak across the city
	$marker.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	ghost_mesh.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	# The selected bus's outline has the color of its chart; the outline shader is unshaded: undo the tonemapping
	outline_color = Occupancy.screen_albedo(Occupancy.SELECTED_COLOR)
	_collision_layer = body.collision_layer
	if occupancy_status == -2:
		set_occupancy(Occupancy.NO_DATA)

## Every frame, not every physics tick: the camera follows the bus every frame (no judder on 120/144 Hz screens)
func _process(delta):
	_ghost_t = minf(_ghost_t + delta * GHOST_RATE * playback_speed, 1.0)
	ghost.global_position = bus.global_position.lerp(estimated_position, _ghost_t)

	# Lerps the bus to the new position when a new target position is set
	position = position.lerp(target_position, minf(delta * FOLLOW_SPEED * playback_speed, 1.0))
	# Settled: nothing moves until the next update (most buses, between two polls), so the transforms are not sent again
	# every frame
	if _ghost_t >= 1.0 and position.distance_squared_to(target_position) < 1e-6:
		position = target_position
		set_process(false)

## snap: jump straight to the target (new bus, replay seek) instead of gliding there
func on_update(target: Vector3, snap := false):
	# If a new position was determined to move to
	if target.x != target_position.x or target.z != target_position.z:
		target.y = ground_y(target)
		# Glide from the previous target, at the new ground height (one lookup in the height grid is enough)
		last_position = Vector3(target_position.x, target.y, target_position.z)
		target_position = target
		_ghost_t = 0.0
		estimated_position = target + (target - last_position)
		if not snap:
			face_towards(estimated_position)
	if snap:
		position = target_position
		estimated_position = target_position
		_ghost_t = 1.0
		if is_inside_tree():
			ghost.global_position = estimated_position
	set_process(true)

## Colors the beacon above the bus by occupancy status (Occupancy.NO_DATA = unknown). The beacon mesh's
## material is shared by all buses: only this instance's override changes
func set_occupancy(status: int) -> void:
	if status == occupancy_status:
		return
	occupancy_status = status
	$marker.material_override = Occupancy.material(status)

## Display quality (quality.gd): the two headlights
func set_headlights(shown: bool) -> void:
	$SpotLight3D.visible = shown
	$SpotLight3D2.visible = shown

## Hides the bus (line filter) and makes it unclickable: the camera's click raycast would still hit its body
func set_filtered_out(hidden: bool) -> void:
	if hidden == filtered_out:
		return
	filtered_out = hidden
	visible = not hidden
	body.collision_layer = 0 if hidden else _collision_layer

## Turns the bus towards a point, ignoring height (no-op when the point is right above/below the bus)
func face_towards(point: Vector3):
	var direction = point - global_position
	# is_equal_approx is the check look_at() fails on (its tolerance grows with the coordinates)
	if Vector2(direction.x, direction.z).length_squared() > 0.0001 and not global_position.is_equal_approx(point):
		look_at(point, Vector3.UP)
		rotation.x = 0

## The ground's height under a point: the city's height grid (map_heights.gd, baked with the ray this function cast on
## the map's collision meshes); NO_GROUND_Y where there is no city
func ground_y(point: Vector3) -> float:
	var height := ground.height_at(point) if ground != null else NAN
	return height if not is_nan(height) else NO_GROUND_Y

func _on_selected():
	selected = true
	var mat = bus_mesh.get_surface_override_material(0).next_pass
	# Change material or highlight the object (the outline is also thicker)
	if mat:
		mat.set_shader_parameter("outline_color", outline_color)
		mat.set_shader_parameter("outline_width", 2)

func _on_unselected():
	selected = false
	var mat = bus_mesh.get_surface_override_material(0).next_pass
	if mat:
		mat.set_shader_parameter("outline_color", original_outline_color)
		mat.set_shader_parameter("outline_width", 1)
