extends Node3D

class_name  Bus3D

const Occupancy = preload("res://scripts/occupancy.gd")
const DEFAULT_OUTLINE := Color(0.97, 0.97, 0.97)

@export var ray_length : float = 20000.0  # Length of the ray
@export var outline_color : Color

@onready var ghost = $ghost
@onready var ghost_mesh = $ghost/ghost_mesh
@onready var bus = $bus
@onready var bus_mesh = $bus/bus_mesh
@onready var body: StaticBody3D = $bus/StaticBody3D
var target_position : Vector3 = position
var estimated_position : Vector3 = position
var last_position : Vector3 = position
var last_message_time: float
var selected = false
var original_outline_color : Color
var occupancy_status := -2 # Not applied yet (-1 = no data), so the first set_occupancy always colors the beacon
var route_id := "" # Route of the bus's latest update ("" unknown)
var direction_id := -1 # Direction (0 or 1) of its trip, -1 unknown
var filtered_out := false # Hidden by the line filter
var _collision_layer := 1

@export var FOLLOW_SPEED = 4.0
var t = 0.0
# Replay speed multiplier (1.0 in live mode), scales the smoothing so buses keep up with fast playback
static var playback_speed := 1.0

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

func _physics_process(delta):
	t += delta * 0.07 * playback_speed
	t = min(t,1)
	ghost.global_position = bus.global_position.lerp(estimated_position, t)

	# Lerps the bus to the new position when a new target position is set
	position = position.lerp(target_position, min(delta * FOLLOW_SPEED * playback_speed, 1.0))

## snap: jump straight to the target (new bus, replay seek) instead of gliding there
func on_update(_target_pos: Vector3, _time: float, snap := false):
	# If a new position was determined to move to
	if _target_pos.x != target_position.x and _target_pos.z != target_position.z:
		_target_pos.y = ground_y(_target_pos + Vector3(0, 100, 0))
		# Glide from the previous target, at the new ground height (one raycast is enough)
		last_position = Vector3(target_position.x, _target_pos.y, target_position.z)
		target_position = _target_pos
		t = 0
		estimated_position = _target_pos + (_target_pos - last_position)
		if not snap:
			face_towards(estimated_position)
	last_message_time = _time
	if snap:
		position = target_position
		estimated_position = target_position
		t = 1
		if is_inside_tree():
			ghost.global_position = estimated_position

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

## Get the ground position downwards from the origin
func ground_y(origin: Vector3):
	var space_state = get_world_3d().direct_space_state
	var end_point = origin - Vector3(0, ray_length, 0)  # Cast downwards
	
	var query = PhysicsRayQueryParameters3D.create(origin, end_point)
	query.set_exclude([$bus/StaticBody3D])
	var result = space_state.intersect_ray(query)
	
	if result:
		return result.position.y
	else:
		return 20.0

func _on_selected():
	selected = true
	var mat = bus_mesh.get_surface_override_material(0).next_pass
	print("Object Selected: ", name)
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
