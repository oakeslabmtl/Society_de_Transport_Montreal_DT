extends Camera3D

const WEB_LOCK_GRACE_MS := 1000 # Web: the browser has this long to grant the mouse capture free-fly asked for

@export var move_speed: float = 5.0
@export var target_distance: float = 30.0
@export var min_distance: float = 10.0
@export var max_distance: float = 200.0
@export var zoom_speed: float = 10.0
@export var rotation_speed: float = 0.005
@export var fly_speed: float = 50.0  # Speed for free-fly movement
@export var fly_speed_min: float = 1.0  # Minimum fly speed
@export var fly_speed_max: float = 250.0  # Maximum fly speed
@export var fly_speed_change: float = 5.0  # How much speed changes per scroll

var selected_object: Node3D = null
var original_position: Vector3
var original_rotation: Quaternion
var tween: Tween
var yaw: float = 0.0
var pitch: float = 0.0
var rotating: bool = false
var free_flying: bool = false  # Flag to toggle free-fly mode
var _lock_requested_ms := 0 # When free-fly last captured the mouse (on the web, the browser grants it a moment later)

func _ready():
	original_position = global_transform.origin
	original_rotation = global_transform.basis.get_rotation_quaternion()
	# On the web, Ctrl+W (forward and down) would close the browser's tab: Shift goes down instead
	if OS.has_feature("web"):
		InputMap.action_erase_events("cam_move_down")
		var shift := InputEventKey.new()
		shift.physical_keycode = KEY_SHIFT
		InputMap.action_add_event("cam_move_down", shift)

## Clicks and zoom are handled here rather than in _input, so the UI (replay bar, controls panel)
## consumes its own clicks and scrolls before they reach the camera
func _unhandled_input(event):
	if event is InputEventMouseButton:
		if event.pressed:
			# A click in the 3D view leaves the "+ line" field, so the E/Q keys reach the camera again
			get_viewport().gui_release_focus()
		if event.button_index == MOUSE_BUTTON_LEFT and event.pressed:
			handle_object_selection(event.position)
		elif event.button_index == MOUSE_BUTTON_MIDDLE and event.pressed:
			rotating = true  # Start rotating with middle mouse button
		elif event.button_index == MOUSE_BUTTON_RIGHT and event.pressed:
			# Toggle free-fly mode with right mouse button
			free_flying = !free_flying
			if free_flying:
				Input.set_mouse_mode(Input.MOUSE_MODE_CAPTURED)
				_lock_requested_ms = Time.get_ticks_msec()
				# Cancel object selection when entering free-fly mode
				if is_instance_valid(selected_object) and selected_object.has_method("_on_unselected"):
					selected_object._on_unselected()
				selected_object = null
			else:
				Input.set_mouse_mode(Input.MOUSE_MODE_VISIBLE)

	if free_flying:
		if event.is_action_pressed("cam_zoom_in"):
			fly_speed = clamp(fly_speed + fly_speed_change, fly_speed_min, fly_speed_max)
			print("Fly speed: ", fly_speed)  # Feedback for the user
		elif event.is_action_pressed("cam_zoom_out"):
			fly_speed = clamp(fly_speed - fly_speed_change, fly_speed_min, fly_speed_max)
			print("Fly speed: ", fly_speed)
	else:
		# Zooming with Mouse Scroll
		if event.is_action_pressed("cam_zoom_in"):
			target_distance = max(min_distance, target_distance - zoom_speed)
		elif event.is_action_pressed("cam_zoom_out"):
			target_distance = min(max_distance, target_distance + zoom_speed)

func _input(event):
	# The release always stops rotating, even when it happens over the UI
	if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_MIDDLE and not event.pressed:
		rotating = false

	# Rotate camera when middle mouse button is held and moved
	if event is InputEventMouseMotion:
		if rotating:
			# Use consistent rotation_speed without converting to radians here
			# since we're working with screen pixels
			yaw -= event.relative.x * rotation_speed  
			pitch -= event.relative.y * rotation_speed
			
			# Clamp pitch to prevent camera flipping
			pitch = clamp(pitch, -PI/2 + 0.1, PI/2 - 0.1)
		elif free_flying:
			# Free-fly rotation
			yaw -= event.relative.x * rotation_speed
			pitch -= event.relative.y * rotation_speed
			
			# Clamp pitch to prevent camera flipping
			pitch = clamp(pitch, -PI/2 + 0.1, PI/2 - 0.1)
			
			# Apply rotation directly in free-fly mode
			var q_yaw = Quaternion(Vector3.UP, yaw)
			var q_pitch = Quaternion(Vector3.RIGHT, pitch)
			var rotation_quat = q_yaw * q_pitch
			global_transform.basis = Basis(rotation_quat)

func _process(delta):
	# The followed bus can be removed (replay seek / end of service)
	if selected_object != null and not is_instance_valid(selected_object):
		selected_object = null
	# On the web, the browser releases the mouse by itself (Esc) or may refuse it: free-fly ends too, else every cursor
	# move would turn the camera
	if free_flying and OS.has_feature("web") and DisplayServer.mouse_get_mode() != DisplayServer.MOUSE_MODE_CAPTURED \
			and Time.get_ticks_msec() - _lock_requested_ms > WEB_LOCK_GRACE_MS:
		free_flying = false
		Input.set_mouse_mode(Input.MOUSE_MODE_VISIBLE)
	if selected_object:
		var target_position = selected_object.global_transform.origin
		
		# Use quaternions for more stable rotations
		var q_yaw = Quaternion(Vector3.UP, yaw)
		var q_pitch = Quaternion(Vector3.RIGHT, pitch)
		
		# Combine rotations in the correct order
		var rotation_quat = q_yaw * q_pitch
		
		# Convert to basis
		var rotation_basis = Basis(rotation_quat)
		
		# Calculate the new camera position
		var offset = rotation_basis * Vector3(0, 0, target_distance)
		var new_position = target_position + offset
		
		# Smoothly move the camera
		global_transform.origin = global_transform.origin.lerp(new_position, move_speed * delta)
		
		# Make the camera look at the target using the same rotation
		global_transform.basis = rotation_basis.looking_at(target_position - new_position)
	elif free_flying:
		# Free-fly movement
		handle_free_fly_movement(delta)

func handle_free_fly_movement(delta):
	var input_dir = Vector3()
	
	# Get input direction
	if Input.is_action_pressed("cam_move_forward"):  # W key
		input_dir.z -= 1
	if Input.is_action_pressed("cam_move_backward"):  # S key
		input_dir.z += 1
	if Input.is_action_pressed("cam_move_left"):  # A key
		input_dir.x -= 1
	if Input.is_action_pressed("cam_move_right"):  # D key
		input_dir.x += 1
	if Input.is_action_pressed("cam_move_up"):  # Space
		input_dir.y += 1
	if Input.is_action_pressed("cam_move_down"):  # Ctrl (Shift on the web)
		input_dir.y -= 1
	
	# Normalize to prevent faster diagonal movement
	if input_dir.length_squared() > 0:
		input_dir = input_dir.normalized()
	
	# Transform input direction to camera's local space
	var movement = global_transform.basis * input_dir * fly_speed * delta
	
	# Apply movement
	global_transform.origin += movement

func handle_object_selection(mouse_pos: Vector2):
	var space_state = get_world_3d().direct_space_state
	var from = project_ray_origin(mouse_pos)
	var to = from + project_ray_normal(mouse_pos) * 10000  # Extend ray
	var query = PhysicsRayQueryParameters3D.create(from, to)
	var result = space_state.intersect_ray(query)
	
	if result and result.has("collider"):
		var clicked_object = result["collider"].get_parent().get_parent()
		if clicked_object and clicked_object.has_method("_on_selected"):
			select_object(clicked_object)
		else:
			reset_camera()  # Clicked something else → reset
	else:
		reset_camera()  # Clicked empty space → reset

## Highlights an object (a bus) and follows it; the previously selected one is released
func select_object(target: Node3D):
	target._on_selected()
	lock_camera_to(target)
	# Exit free-fly mode when object is selected, and give the cursor back (the chart and the line buttons need it)
	if free_flying:
		Input.set_mouse_mode(Input.MOUSE_MODE_VISIBLE)
	free_flying = false

func lock_camera_to(target: Node3D):
	# Selecting another bus releases the previous one (its outline goes back to normal)
	if is_instance_valid(selected_object) and selected_object != target and selected_object.has_method("_on_unselected"):
		selected_object._on_unselected()
	selected_object = target
	
	# Calculate initial yaw and pitch based on current camera position
	var direction = (global_transform.origin - target.global_transform.origin).normalized()
	
	# Calculate proper initial angles
	yaw = atan2(direction.x, direction.z)
	pitch = -asin(clamp(direction.y, -1.0, 1.0))  # Negative because camera pitch works opposite

func reset_camera():
	if is_instance_valid(selected_object) and selected_object.has_method("_on_unselected"):
		selected_object._on_unselected()
	selected_object = null
	rotating = false

## Places the camera distance meters away from target, looking at it with the given yaw/pitch (radians,
## same convention as free-fly, so free-fly continues smoothly from this view)
func frame_target(target: Vector3, distance: float, view_yaw: float, view_pitch: float):
	yaw = view_yaw
	pitch = view_pitch
	var view_basis = Basis(Quaternion(Vector3.UP, yaw) * Quaternion(Vector3.RIGHT, pitch))
	global_transform = Transform3D(view_basis, target + view_basis.z * distance)
