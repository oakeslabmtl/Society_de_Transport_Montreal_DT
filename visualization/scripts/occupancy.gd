extends RefCounted

## Bus occupancy statuses (GTFS-RT VehiclePosition.OccupancyStatus, stored in "vehicle.occupancy_status"):
## labels, colors and the shared beacon materials. Static helpers only.
## The spec says the statuses are not a linear scale and that a feed may use only some of them (STM sends 1, 2, 3
## and 5): the chart shows the statuses found in the data, one row each, and forecasts are ranks among them.

const KEY := "vehicle.occupancy_status"
const NO_DATA := -1 # Missing, unknown, or NO_DATA_AVAILABLE (7)
const MAX_LEVEL := 5 # FULL: 0..5 say how full the bus is, 6 and 8 that it cannot be boarded
const NOT_ACCEPTING_PASSENGERS := 6
const NO_DATA_AVAILABLE := 7
const NOT_BOARDABLE := 8
## Spec names, by value
const ENUM_NAMES := ["EMPTY", "MANY_SEATS_AVAILABLE", "FEW_SEATS_AVAILABLE", "STANDING_ROOM_ONLY", "CRUSHED_STANDING_ROOM_ONLY",
	"FULL", "NOT_ACCEPTING_PASSENGERS", "NO_DATA_AVAILABLE", "NOT_BOARDABLE"]
## Labels (the spec's wording), by value
const LABELS := ["Empty", "Many seats available", "Few seats available", "Standing room only", "Crushed standing room only",
	"Full", "Not accepting passengers", "No data", "Not boardable"]
const NO_DATA_LABEL := "No data"
## Fixed color per status, green (empty) to red (full); 6 and 8 (cannot board) purple. The statuses STM sends (1, 2, 3,
## 5) and "no data" stay apart for color-blind viewers too (lightness differs: OKLab ΔE ≥ 11 under protan/deutan)
const COLORS := [Color("#66da85"), Color("#02721c"), Color("#fed25f"), Color("#f27700"), Color("#d02e1e"), Color("#77010f"),
	Color("#6b359b"), Color("#868686"), Color("#6b359b")]
const NO_DATA_COLOR := Color("#868686")
# Godot's filmic tonemap curve (exposure bias 2 baked into A and B, as in Godot's tonemap shader)
const FILMIC_A := 0.22 * 4.0
const FILMIC_B := 0.30 * 2.0
const FILMIC_C := 0.10
const FILMIC_D := 0.20
const FILMIC_E := 0.01
const FILMIC_F := 0.30

static var _materials := {} # status (NO_DATA included) -> StandardMaterial3D, shared by all buses
static var _environment: Environment = null # Scene environment the beacons are rendered with
static var _tonemap := [] # [mode, white, exposure] the material colors were computed for

## Occupancy status 0..8 of an entity dictionary or of a raw value (int, float, numeric string or enum name such as
## "FULL"); NO_DATA (-1) for NO_DATA_AVAILABLE (7), missing or garbage values
static func status_of(value) -> int:
	if value is Dictionary:
		value = value.get(KEY)
	if value is String:
		var text: String = value.strip_edges()
		var named := ENUM_NAMES.find(text.to_upper())
		if named >= 0:
			return named if named != NO_DATA_AVAILABLE else NO_DATA
		if not text.is_valid_float():
			return NO_DATA
		value = text.to_float()
	if value is float and value == floorf(value) and value >= 0.0 and value <= NOT_BOARDABLE:
		value = int(value)
	if value is int and value >= 0 and value <= NOT_BOARDABLE and value != NO_DATA_AVAILABLE:
		return value
	return NO_DATA

## True for the statuses that say how full the bus is (0..5): the ones the chart and the forecast use
static func is_level(status: int) -> bool:
	return status >= 0 and status <= MAX_LEVEL

static func label(status: int) -> String:
	return LABELS[status] if status >= 0 and status < LABELS.size() else NO_DATA_LABEL

static func color(status: int) -> Color:
	return COLORS[status] if status >= 0 and status < COLORS.size() else NO_DATA_COLOR

## Sets the environment whose tonemapping the beacon colors compensate (main.gd, before the first bus)
static func set_environment(environment: Environment) -> void:
	_environment = environment
	_update_materials()

## Beacon material of a status: unshaded and opaque, one cached instance per status. Its albedo is the inverse of the
## scene's tonemapping, so the beacon shows the legend color on screen
static func material(status: int) -> StandardMaterial3D:
	_update_materials()
	var key := status if status >= 0 and status < COLORS.size() else NO_DATA
	if not _materials.has(key):
		var mat := StandardMaterial3D.new()
		mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
		mat.albedo_color = screen_albedo(color(key))
		_materials[key] = mat
	return _materials[key]

## Recolors the cached materials in place (buses keep theirs) when the tonemap settings changed
static func _update_materials() -> void:
	var settings := [-1, 1.0, 1.0]
	if _environment != null:
		settings = [_environment.tonemap_mode, _environment.tonemap_white, _environment.tonemap_exposure]
	if settings == _tonemap:
		return
	_tonemap = settings
	for key in _materials:
		_materials[key].albedo_color = screen_albedo(color(key))

## Albedo of an unshaded material that the current tonemapping turns into this color on screen: filmic is inverted per
## channel, linear only needs the exposure undone, any other tonemapper gets the color as is
static func screen_albedo(color: Color) -> Color:
	_update_materials()
	var mode: int = _tonemap[0]
	var white: float = _tonemap[1]
	var exposure: float = _tonemap[2]
	if exposure <= 0.0 or white <= 0.0:
		return color
	var target := color.srgb_to_linear()
	if mode == Environment.TONE_MAPPER_LINEAR:
		return Color(target.r / exposure, target.g / exposure, target.b / exposure).linear_to_srgb()
	if mode == Environment.TONE_MAPPER_FILMIC:
		return Color(_inverse_filmic(target.r, white, exposure), _inverse_filmic(target.g, white, exposure),
			_inverse_filmic(target.b, white, exposure)).linear_to_srgb()
	return color

static func _filmic(x: float) -> float:
	return (x * (FILMIC_A * x + FILMIC_C * FILMIC_B) + FILMIC_D * FILMIC_E) / (x * (FILMIC_A * x + FILMIC_B) + FILMIC_D * FILMIC_F) - FILMIC_E / FILMIC_F

## Linear value x with filmic(x * exposure) / filmic(white) = target (bisection: the curve is increasing)
static func _inverse_filmic(target: float, white: float, exposure: float) -> float:
	var white_out := _filmic(white)
	var low := 0.0
	var high := white / exposure
	for i in 30:
		var middle := (low + high) * 0.5
		if _filmic(middle * exposure) / white_out < target:
			low = middle
		else:
			high = middle
	return (low + high) * 0.5
