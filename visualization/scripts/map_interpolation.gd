extends Node

@onready var reference_points: Array[Node] = $ReferencePoints.get_children()

# Reference points copied into flat arrays: reading node properties is slow, and the
# triangulation runs for every bus position update and every stop
var _ref_lat := PackedFloat64Array()
var _ref_lon := PackedFloat64Array()
var _ref_x := PackedFloat64Array()
var _ref_z := PackedFloat64Array()

func _ready():
	for point in reference_points:
		_ref_lat.append(point.latitude)
		_ref_lon.append(point.longitude)
		_ref_x.append(point.global_position.x)
		_ref_z.append(point.global_position.z)

## The map position (x, z) of a latitude/longitude: barycentric interpolation between the three nearest reference
## points (Vector2.ZERO, with an error, when they are on one line)
func get_position_by_closest_triangulation(latitude: float, longitude: float) -> Vector2:
	# The three nearest points in one pass (no per-call sort): i0 nearest, then i1, then i2
	var i0 := -1
	var i1 := -1
	var i2 := -1
	var d0 := INF
	var d1 := INF
	var d2 := INF
	for index in _ref_lat.size():
		var delta_lat := _ref_lat[index] - latitude
		var delta_lon := _ref_lon[index] - longitude
		var distance := delta_lat * delta_lat + delta_lon * delta_lon
		if distance < d0:
			i2 = i1; d2 = d1
			i1 = i0; d1 = d0
			i0 = index; d0 = distance
		elif distance < d1:
			i2 = i1; d2 = d1
			i1 = index; d1 = distance
		elif distance < d2:
			i2 = index; d2 = distance
	return _interpolate(latitude, longitude, i0, i1, i2)

## Barycentric interpolation inside the triangle of reference points a, b and c
func _interpolate(lat: float, lon: float, a: int, b: int, c: int) -> Vector2:
	var denom := (_ref_lat[b] - _ref_lat[c]) * (_ref_lon[a] - _ref_lon[c]) \
		+ (_ref_lon[c] - _ref_lon[b]) * (_ref_lat[a] - _ref_lat[c])
	if denom == 0:
		push_error("Reference points must not be collinear.")
		return Vector2.ZERO
	var w1 := ((_ref_lat[b] - _ref_lat[c]) * (lon - _ref_lon[c]) + (_ref_lon[c] - _ref_lon[b]) * (lat - _ref_lat[c])) / denom
	var w2 := ((_ref_lat[c] - _ref_lat[a]) * (lon - _ref_lon[c]) + (_ref_lon[a] - _ref_lon[c]) * (lat - _ref_lat[c])) / denom
	var w3 := 1.0 - w1 - w2
	return Vector2(w1 * _ref_x[a] + w2 * _ref_x[b] + w3 * _ref_x[c], w1 * _ref_z[a] + w2 * _ref_z[b] + w3 * _ref_z[c])
