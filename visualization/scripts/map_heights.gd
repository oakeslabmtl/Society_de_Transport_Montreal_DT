extends Resource

## The city's ground: the height of its top surface on a regular grid, in the map model's own space (the GLB's). It
## places the buses on the ground and tells whether a point is over the modeled city; a coarser height-map collision
## shape stops clicks at the buildings. It replaces the map's collision meshes (5.4 million triangles, 72 MB in a
## build). Baked from the map by tools/bake_map_heights.gd: a ray down at every grid point, as the buses used to cast.

const HOLE := -100000.0 # Collision height (its cell units) where the city has no ground: a floor out of any ray's reach
const BAND_WORLD := 0.3 # Grid points closer than this in height (world units) are on the same surface

@export var cell := 1.0 # Grid spacing, map units
@export var origin := Vector2.ZERO # Map x and z of the first grid point
@export var width := 0 # Grid points along x
@export var depth := 0 # Grid points along z
@export var height_min := 0.0 # Map height of code 1
@export var height_step := 1.0 # Map height of one code step
@export var codes := PackedByteArray() # 16-bit height codes, row by row (z, then x): 0 where the city has no ground
@export var collision_stride := 1 # The click shape has every collision_stride-th point, at the lowest ground around it
@export var collision_width := 0
@export var collision_depth := 0
@export var collision_heights := PackedFloat32Array() # In the click shape's cell units, HOLE where there is no ground

var _to_world := Transform3D.IDENTITY # Map space -> world (set_map_transform)
var _to_map := Transform3D.IDENTITY
var _band := 0.0 # BAND_WORLD in height codes

## Where the map model is in the world (its node's global transform)
func set_map_transform(map_to_world: Transform3D) -> void:
	_to_world = map_to_world
	_to_map = map_to_world.affine_inverse()
	_band = BAND_WORLD / (height_step * map_to_world.basis.get_scale().y)

## The world height of the ground under a world point, NAN when there is no city under it: bilinear between the grid
## points around it with ground at about the height of the nearest one (so across a wall, that side's height alone)
func height_at(world: Vector3) -> float:
	var p := _to_map * world
	var gx := (p.x - origin.x) / cell
	var gz := (p.z - origin.y) / cell
	var x0 := floori(gx)
	var z0 := floori(gz)
	if x0 < 0 or z0 < 0 or x0 + 1 >= width or z0 + 1 >= depth:
		return NAN
	var fx := gx - x0
	var fz := gz - z0
	var corner_codes := PackedInt32Array()
	var weights := PackedFloat32Array()
	var nearest := -1
	for corner in 4:
		corner_codes.append(codes.decode_u16(2 * ((z0 + corner / 2) * width + x0 + corner % 2)))
		weights.append((fx if corner % 2 else 1.0 - fx) * (fz if corner / 2 else 1.0 - fz))
		if corner_codes[corner] > 0 and weights[corner] > 0.0 and (nearest < 0 or weights[corner] > weights[nearest]):
			nearest = corner
	if nearest < 0:
		return NAN
	var sum := 0.0
	var weight := 0.0
	for corner in 4:
		if corner_codes[corner] > 0 and absi(corner_codes[corner] - corner_codes[nearest]) <= _band:
			sum += corner_codes[corner] * weights[corner]
			weight += weights[corner]
	return (_to_world * Vector3(p.x, height_min + (sum / weight - 1.0) * height_step, p.z)).y

## A collision shape of the ground, for the clicks (rays from the camera): to add under the map model's node
func make_collision() -> CollisionShape3D:
	var shape := HeightMapShape3D.new()
	shape.map_width = collision_width
	shape.map_depth = collision_depth
	shape.map_data = collision_heights
	var node := CollisionShape3D.new()
	node.name = "Ground"
	node.shape = shape
	# The shape is centered and has cells of 1 unit: scaled to its grid's spacing (its heights are in those units)
	var spacing := cell * collision_stride
	node.transform = Transform3D(Basis.from_scale(Vector3.ONE * spacing),
		Vector3(origin.x + (collision_width - 1) * 0.5 * spacing, 0.0, origin.y + (collision_depth - 1) * 0.5 * spacing))
	return node
