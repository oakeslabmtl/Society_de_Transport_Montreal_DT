extends RefCounted

## Display quality: the presets, the user's settings (saved in user://settings.cfg) and applying them to the scene.
## Measured at 1920x1080 on an Intel UHD (integrated GPU): the shadows and how far the city is drawn cost the most. The
## map's level of detail is no setting: since the map was welded (software/tools/weld_map.py) it has levels of detail, but
## a coarser LOD threshold than the engine's 1 pixel saved at most 6%. Distances are in scene units: about 5.3 m each (the
## reference points: 599 units for 3.19 km).

const SETTINGS_PATH := "user://settings.cfg"
const PRESETS: Array[String] = ["minimum", "low", "medium", "high", "ultra"]
const CUSTOM := "custom"
## What each preset sets; the other settings (frame rate, panels) are the user's
const PRESET_VALUES := {
	"minimum": {"shadows": "off", "view_distance": 500, "render_scale": 0.6, "antialiasing": "off",
		"ambient_occlusion": false, "glow": false, "headlights": false},
	"low": {"shadows": "low", "view_distance": 800, "render_scale": 0.75, "antialiasing": "off",
		"ambient_occlusion": false, "glow": false, "headlights": false},
	"medium": {"shadows": "medium", "view_distance": 0, "render_scale": 1.0, "antialiasing": "off",
		"ambient_occlusion": false, "glow": true, "headlights": false},
	"high": {"shadows": "high", "view_distance": 0, "render_scale": 1.0, "antialiasing": "off",
		"ambient_occlusion": false, "glow": true, "headlights": true},
	"ultra": {"shadows": "ultra", "view_distance": 0, "render_scale": 1.0, "antialiasing": "msaa4",
		"ambient_occlusion": true, "glow": true, "headlights": true},
}
const OTHER_DEFAULTS := {"fps_limit": "screen", "show_fps": false, "legend": true}
## The sun's shadows: cascades, distance from the camera (scene units), shadow map size and edge softness
const SHADOWS := {
	"low": {"mode": DirectionalLight3D.SHADOW_PARALLEL_2_SPLITS, "distance": 250.0, "atlas": 2048,
		"filter": RenderingServer.SHADOW_QUALITY_SOFT_VERY_LOW},
	"medium": {"mode": DirectionalLight3D.SHADOW_PARALLEL_2_SPLITS, "distance": 400.0, "atlas": 2048,
		"filter": RenderingServer.SHADOW_QUALITY_SOFT_LOW},
	"high": {"mode": DirectionalLight3D.SHADOW_PARALLEL_4_SPLITS, "distance": 600.0, "atlas": 4096,
		"filter": RenderingServer.SHADOW_QUALITY_SOFT_MEDIUM},
	"ultra": {"mode": DirectionalLight3D.SHADOW_PARALLEL_4_SPLITS, "distance": 1000.0, "atlas": 4096,
		"filter": RenderingServer.SHADOW_QUALITY_SOFT_HIGH},
}
## The values a setting can take, with their labels (settings panel)
const CHOICES := {
	"shadows": [["off", "Off"], ["low", "Low"], ["medium", "Medium"], ["high", "High"], ["ultra", "Ultra"]],
	"view_distance": [[500, "2.7 km"], [650, "3.5 km"], [800, "4.3 km"], [0, "Whole city"]], # Scene units, km labels
	"render_scale": [[0.6, "60%"], [0.75, "75%"], [0.85, "85%"], [1.0, "100%"]],
	"antialiasing": [["off", "Off"], ["fxaa", "FXAA"], ["msaa2", "MSAA 2x"], ["msaa4", "MSAA 4x"]],
	"fps_limit": [["screen", "Screen rate"], ["60", "60 fps"], ["30", "30 fps"]],
}
const FOG_START := 0.6 # Share of the view distance where the haze starts (it hides where the city stops being drawn)

var values := {} # Setting -> value: the preset's ones, the user's and OTHER_DEFAULTS
var preset := "high" # One of PRESETS, or CUSTOM once a setting differs from every preset
## Chosen from the GPU at the first start, not by the user: main.gd may still lower it if the view is not smooth
var auto := false
var note := "" # Why the settings changed at startup ("" when they did not)
var _path := "" # Where the settings are saved, "" = not saved (self-test, --quality)
## Whether this run keeps the "running" marker (the exported viewer). Another run (the editor's, whose Stop button kills
## it) writes back the marker it found: it must neither make the demo restart lower nor clear the demo's real crash
var _crash_check := false
var _running_found := false

## Reads the saved settings (the GPU's recommended preset when there are none). path "": nothing is read or saved.
## forced_preset (--quality=...): this preset for this run only, nothing is saved. crash_check: a previous run that did
## not end normally (a crash, or a killed process) starts lower
func load_settings(path: String, forced_preset := "", crash_check := false) -> void:
	var config := ConfigFile.new()
	var loaded := path != "" and config.load(path) == OK
	var saved := str(config.get_value("display", "preset", ""))
	_running_found = config.get_value("session", "running", false) == true
	var first_start := not (saved in PRESETS or saved == CUSTOM)
	values = OTHER_DEFAULTS.duplicate()
	_set_preset_values(recommended_preset() if first_start else (saved if saved != CUSTOM else "medium"))
	for key in values:
		var section := "display" if PRESET_VALUES["medium"].has(key) else "ui"
		var value = config.get_value(section, key, values[key])
		if is_valid(key, value) and not (first_start and section == "display"):
			values[key] = value
	preset = _matching_preset()
	auto = first_start or config.get_value("display", "auto", false) == true
	_path = path
	_crash_check = crash_check
	if forced_preset in PRESETS:
		_set_preset_values(forced_preset)
		auto = false
		_path = ""
	elif crash_check and loaded and _running_found:
		# Custom settings weigh about what their shadows do ("off": Minimum)
		var level := PRESETS.find(preset) if preset != CUSTOM else PRESETS.find("minimum" if values["shadows"] == "off" else values["shadows"])
		var lower := "low" if level > PRESETS.find("low") else "minimum"
		_set_preset_values(lower)
		if level > 0:
			note = "The viewer did not close normally last time: quality set to %s. Change it in Settings." % preset_label(lower)
		else:
			note = "The viewer did not close normally last time, at Minimum quality. If it happens again, start it with \"Start in safe mode.bat\"."
	save()

## Saves the settings; running: this run has not ended yet (cleared when the viewer closes normally)
func save(running := true) -> void:
	if _path == "":
		return
	var config := ConfigFile.new()
	config.set_value("display", "preset", preset)
	config.set_value("display", "auto", auto)
	for key in values:
		config.set_value("display" if PRESET_VALUES["medium"].has(key) else "ui", key, values[key])
	config.set_value("session", "running", running if _crash_check else _running_found)
	config.save(_path)

## False for a run whose settings are not saved (--quality, self-test)
func is_saved() -> bool:
	return _path != ""

func set_preset(name: String) -> void:
	_set_preset_values(name)
	auto = false
	save()

func set_value(key: String, value) -> void:
	if not is_valid(key, value):
		return
	values[key] = value
	preset = _matching_preset()
	if PRESET_VALUES["medium"].has(key):
		auto = false
	save()

## One preset lower (the frame-rate check of the first start); false when already the lowest
func step_down() -> bool:
	var level := PRESETS.find(preset)
	if level <= 0:
		return false
	_set_preset_values(PRESETS[level - 1])
	save()
	return true

## The GPU's preset: High on a dedicated GPU, Medium on an integrated one, Low on the OpenGL renderer (no Vulkan nor
## Direct3D 12: an old or basic GPU, or safe mode), Minimum on a software or virtual GPU
static func recommended_preset() -> String:
	if not is_forward_plus():
		return "low"
	match RenderingServer.get_video_adapter_type():
		RenderingDevice.DEVICE_TYPE_DISCRETE_GPU:
			return "high"
		RenderingDevice.DEVICE_TYPE_CPU, RenderingDevice.DEVICE_TYPE_VIRTUAL_GPU:
			return "minimum"
	return "medium"

## Forward+ (Vulkan or Direct3D 12). The Compatibility renderer (OpenGL, the engine's fallback) has no ambient
## occlusion and no FSR upscaling
static func is_forward_plus() -> bool:
	return RenderingServer.get_current_rendering_method() == "forward_plus"

## False for a setting only Forward+ has, on the Compatibility renderer
static func is_available(key: String, value) -> bool:
	if is_forward_plus():
		return true
	return not (key == "ambient_occlusion" and value == true)

static func is_valid(key: String, value) -> bool:
	if CHOICES.has(key):
		return CHOICES[key].any(func(choice): return typeof(choice[0]) == typeof(value) and choice[0] == value)
	return (PRESET_VALUES["medium"].has(key) or OTHER_DEFAULTS.has(key)) and value is bool

static func preset_label(name: String) -> String:
	return "Custom" if name == CUSTOM else name.capitalize()

## Shadows, upscaling, anti-aliasing, effects, haze and frame-rate cap
func apply_render(viewport: Viewport, light: DirectionalLight3D, environment: Environment) -> void:
	var shadows = SHADOWS.get(values["shadows"])
	light.shadow_enabled = shadows != null
	if shadows != null:
		light.directional_shadow_mode = shadows["mode"]
		light.directional_shadow_max_distance = shadows["distance"]
		RenderingServer.directional_shadow_atlas_set_size(shadows["atlas"], true)
		RenderingServer.directional_soft_shadow_filter_set_quality(shadows["filter"])
	var scale: float = values["render_scale"]
	# On the web, the canvas has the screen's device pixels (two per CSS pixel on a Retina screen): the 3D view is drawn
	# per CSS pixel, as on an ordinary screen (the browser's 3D is the slowest part). Never finer than the preset's own
	# scale: a page zoomed out has less than one device pixel per CSS pixel
	if OS.has_feature("web"):
		scale = clampf(scale / DisplayServer.screen_get_scale(), 0.25, scale)
	viewport.scaling_3d_mode = Viewport.SCALING_3D_MODE_FSR if scale < 1.0 and is_forward_plus() else Viewport.SCALING_3D_MODE_BILINEAR
	viewport.scaling_3d_scale = scale
	viewport.msaa_3d = {"msaa2": Viewport.MSAA_2X, "msaa4": Viewport.MSAA_4X}.get(values["antialiasing"], Viewport.MSAA_DISABLED)
	viewport.screen_space_aa = Viewport.SCREEN_SPACE_AA_FXAA if values["antialiasing"] == "fxaa" else Viewport.SCREEN_SPACE_AA_DISABLED
	environment.ssao_enabled = values["ambient_occlusion"] and is_forward_plus()
	environment.glow_enabled = values["glow"]
	# Where the city stops being drawn, a haze of the horizon's color hides the edge. It covers the sky too: behind the
	# edge there is only haze (else the sky's dark ground and the far stops' poles would show)
	var distance: float = values["view_distance"]
	environment.fog_enabled = distance > 0.0
	if distance > 0.0:
		environment.fog_mode = Environment.FOG_MODE_DEPTH
		environment.fog_depth_begin = distance * FOG_START
		environment.fog_depth_end = distance
		environment.fog_density = 1.0
		environment.fog_sky_affect = 1.0
		var sky_material = environment.sky.sky_material if environment.sky != null else null
		if sky_material is ProceduralSkyMaterial:
			environment.fog_light_color = sky_material.sky_horizon_color
	Engine.max_fps = 0 if values["fps_limit"] == "screen" else int(values["fps_limit"])

## How far the city is drawn (0: the whole city). A map chunk is skipped once its center is further than the view
## distance plus its own radius: by then the haze covers all of it, none pops out of view
func apply_view_distance(chunks: Array) -> void:
	var distance: float = values["view_distance"]
	for chunk: GeometryInstance3D in chunks:
		var radius := (chunk.global_transform * chunk.get_aabb()).size.length() * 0.5
		chunk.visibility_range_end = distance + radius if distance > 0.0 else 0.0

func _set_preset_values(name: String) -> void:
	preset = name
	for key in PRESET_VALUES[name]:
		values[key] = PRESET_VALUES[name][key]

## The preset these values are, else CUSTOM
func _matching_preset() -> String:
	for name in PRESETS:
		if PRESET_VALUES[name].keys().all(func(key): return values.get(key) == PRESET_VALUES[name][key]):
			return name
	return CUSTOM
