extends SceneTree

## Bakes res://assets/map_heights.res (scripts/map_heights.gd) from the city map: at every grid point, a ray straight
## down on the map's triangles, from where the buses used to cast their ground ray (y 115 in the world, 100 above a new
## bus). The map's collision is built here (the imported map has none). Run it again after changing the map:
##   <Godot console exe> --headless --path visualization -s res://tools/bake_map_heights.gd
## It takes a few minutes. Options: -- --cell=<world units> --out=<path>
## Grid spacing: at 0.35 (1.9 m), 90% of the bundled replay's bus positions are within 0.07 units of the height the
## collision meshes gave them and 95% within 0.25; 2% are more than 1 unit off (by a wall, the nearest grid point is
## on its other side), for a 16.5 MB file. At 0.25: 0.04 and 97%, 28 MB; at 0.5: 0.14 and 93%, 10 MB.

const MapHeights = preload("res://scripts/map_heights.gd")
const MAIN_SCENE := "res://Main.tscn"
const MAP_NODE := "Map" # The map model is this node's first child in Main.tscn (main.gd finds it the same way)
const OUTPUT := "res://assets/map_heights.res"
const CELL_WORLD := 0.35 # Grid spacing, world units (about 1.9 m: the scene's units are about 5.3 m)
const HEIGHT_STEP_WORLD := 0.01 # Heights are rounded to this (5 cm): finer steps are noise and do not compress
const COLLISION_CELL_WORLD := 1.0 # The click shape's spacing
const RAY_TOP_WORLD := 115.0 # The buses' old ground ray: from 100 above a bus placed at y 15 (main.gd)...
const RAY_LENGTH_WORLD := 20000.0 # ...and 20000 long

func _initialize() -> void:
	_run.call_deferred()

func _run() -> void:
	var started := Time.get_ticks_msec()
	var model = _map_model()
	if model == null:
		quit(1)
		return
	var map_to_world: Transform3D = model["transform"]
	var scale := map_to_world.basis.get_scale().x
	# The rays go down the map's own y axis, and the grid's spacing is the same along x and z: the model must stand up,
	# turned about y at most, and be scaled the same along every axis
	if not map_to_world.basis.get_scale().is_equal_approx(Vector3.ONE * scale) \
			or not (map_to_world.basis.y / scale).is_equal_approx(Vector3.UP):
		push_error("The map model is tilted or not scaled evenly: the grid cannot be baked")
		quit(1)
		return
	var cell := float(_arg("cell", str(CELL_WORLD))) / scale
	var output := _arg("out", OUTPUT)
	var to_map := map_to_world.affine_inverse()
	var top := (to_map * Vector3(0, RAY_TOP_WORLD, 0)).y
	var bottom := (to_map * Vector3(0, RAY_TOP_WORLD - RAY_LENGTH_WORLD, 0)).y

	# The map in its own space, with a collision mesh per piece (as the importer's "physics" option made them)
	var map: Node3D = (model["scene"] as PackedScene).instantiate()
	root.add_child(map)
	var bounds := AABB()
	var first := true
	for mesh: MeshInstance3D in map.find_children("*", "MeshInstance3D", true, false):
		if mesh.find_children("*", "CollisionShape3D", true, false).is_empty():
			mesh.create_trimesh_collision()
		var box: AABB = mesh.global_transform * mesh.get_aabb()
		bounds = box if first else bounds.merge(box)
		first = false
	await physics_frame
	await physics_frame

	# A ray down at every grid point: the height of the first surface it meets (NAN: none)
	var result := MapHeights.new()
	result.cell = cell
	result.origin = Vector2(bounds.position.x - cell, bounds.position.z - cell)
	result.width = ceili(bounds.size.x / cell) + 3
	result.depth = ceili(bounds.size.z / cell) + 3
	var heights := PackedFloat32Array()
	heights.resize(result.width * result.depth)
	var space := root.world_3d.direct_space_state
	var query := PhysicsRayQueryParameters3D.new()
	var low := INF
	var high := -INF
	for z in result.depth:
		var map_z := result.origin.y + z * cell
		for x in result.width:
			query.from = Vector3(result.origin.x + x * cell, top, map_z)
			query.to = Vector3(query.from.x, bottom, map_z)
			var hit := space.intersect_ray(query)
			var height: float = NAN if hit.is_empty() else hit["position"].y
			heights[z * result.width + x] = height
			if not is_nan(height):
				low = minf(low, height)
				high = maxf(high, height)
		if z % 500 == 0:
			print("row %d / %d" % [z, result.depth])

	# 16-bit codes (0: no ground)
	result.height_min = low
	result.height_step = maxf(HEIGHT_STEP_WORLD / scale, (high - low) / 65534.0)
	var codes := PackedByteArray()
	codes.resize(2 * heights.size())
	var hits := 0
	for i in heights.size():
		if not is_nan(heights[i]):
			codes.encode_u16(2 * i, 1 + roundi((heights[i] - low) / result.height_step))
			hits += 1
	result.codes = codes

	# The click shape: every stride-th point, at the lowest ground within one of its cells around it (along x, then z).
	# Between a street point and a roof point, the shape slopes: this puts that slope inside the building, so a bus
	# against a wall stays clickable
	var stride := maxi(roundi(COLLISION_CELL_WORLD / (cell * scale)), 1)
	var collision_width := (result.width - 1) / stride + 1
	var collision_depth := (result.depth - 1) / stride + 1
	var lowest_along_x := PackedFloat32Array()
	lowest_along_x.resize(collision_width * result.depth)
	for z in result.depth:
		for cx in collision_width:
			var lowest := INF
			for x in range(maxi((cx - 1) * stride, 0), mini((cx + 1) * stride, result.width - 1) + 1):
				var height := heights[z * result.width + x]
				if not is_nan(height):
					lowest = minf(lowest, height)
			lowest_along_x[z * collision_width + cx] = lowest
	var collision := PackedFloat32Array()
	for cz in collision_depth:
		for cx in collision_width:
			var lowest := INF
			for z in range(maxi((cz - 1) * stride, 0), mini((cz + 1) * stride, result.depth - 1) + 1):
				lowest = minf(lowest, lowest_along_x[z * collision_width + cx])
			collision.append(MapHeights.HOLE if lowest == INF else lowest / (cell * stride))
	result.collision_stride = stride
	result.collision_width = collision_width
	result.collision_depth = collision_depth
	result.collision_heights = collision

	var error := ResourceSaver.save(result, output, ResourceSaver.FLAG_COMPRESS)
	print("%s: %d x %d points (%.3f map units, %.2f world), %d on the city, click shape %d x %d, %.0f s, %s" % [output,
		result.width, result.depth, cell, cell * scale, hits, result.collision_width, result.collision_depth,
		(Time.get_ticks_msec() - started) / 1000.0, error_string(error)])
	quit(0 if error == OK else 1)

## A user argument (-- --name=value) or its default
func _arg(name: String, default: String) -> String:
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--%s=" % name):
			return argument.substr(name.length() + 3)
	return default

## The map model in Main.tscn, read without running the scene: {"scene": its PackedScene, "transform": its node's and
## every parent's transforms}; null, with an error, when the scene has none
func _map_model() -> Variant:
	var state := (load(MAIN_SCENE) as PackedScene).get_state()
	var transforms := {} # Every node's path -> its transform (identity when the scene sets none)
	var model := ""
	var scene: PackedScene = null
	for i in state.get_node_count():
		var path := String(state.get_node_path(i)).trim_prefix("./")
		transforms[path] = Transform3D.IDENTITY
		for p in state.get_node_property_count(i):
			if state.get_node_property_name(i, p) == "transform":
				transforms[path] = state.get_node_property_value(i, p)
		if model == "" and String(state.get_node_path(i, true)).trim_prefix("./") == MAP_NODE:
			model = path
			scene = state.get_node_instance(i)
	if scene == null:
		push_error("%s has no map model (an instanced scene, first child of its %s node)" % [MAIN_SCENE, MAP_NODE])
		return null
	var result: Transform3D = transforms["."]
	var path := ""
	for part in model.split("/"):
		path = part if path == "" else path + "/" + part
		result = result * transforms[path]
	return {"scene": scene, "transform": result}
