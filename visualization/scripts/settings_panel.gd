extends Control

## The top-right corner: the toolbar (frame rate, Legend, Settings, Hide UI) and, under it, the display settings
## (quality.gd) or a short note. Hide UI (or the H key) hides every panel; a "Show UI" button then shows while the mouse
## moves. Built in code like occupancy_panel.gd; main.gd applies the settings.

signal settings_changed() # A display setting changed (already saved in quality): apply them
signal legend_toggled(shown: bool)
signal panel_toggled(open: bool) # The settings take the legend's place while open
signal ui_hidden_changed(hidden: bool)
signal column_changed(top: float, width: float) # Where the legend column starts and the right column's widest panel

const Quality = preload("res://scripts/quality.gd")
const OccupancyPanelScript = preload("res://scripts/occupancy_panel.gd") # Its colors and builders
const MARGIN := 16
const PANEL_WIDTH := 270 # With the chart (~475 px) and the margins, fits an 800 px wide window
const MIN_PANEL_HEIGHT := 120.0 # The settings scroll when the window is shorter than they are
const FPS_REFRESH := 0.5 # Seconds between two frame-rate readings
const NOTE_SECONDS := 15.0
const SHOW_BUTTON_SECONDS := 2.5 # The "Show UI" button hides this long after the mouse stops
const LABELS := {
	"shadows": "Shadows", "view_distance": "View distance", "render_scale": "Resolution", "antialiasing": "Anti-aliasing",
	"ambient_occlusion": "Ambient occlusion", "glow": "Glow", "headlights": "Bus headlights",
	"fps_limit": "Frame rate", "show_fps": "Show frame rate",
}
const TOOLTIPS := {
	"shadows": "The sun's shadows: how far from the camera, with how many levels of detail and how soft",
	"view_distance": "How far the city is drawn (a haze hides where it stops; the buses' beacons and the stops stay): saves the most in wide views",
	"render_scale": "Draws the 3D view at a lower resolution and upscales it: the panels stay sharp, the city softens",
	"antialiasing": "Smooths jagged edges. FXAA costs little but blurs a bit; MSAA is sharper and costs more",
	"ambient_occlusion": "Darkens corners and narrow streets",
	"glow": "Bright surfaces glow a little",
	"headlights": "Two lights in front of each bus",
	"fps_limit": "A lower cap keeps a laptop cooler and quieter",
	"show_fps": "Frames drawn per second, in this toolbar",
}
const NEEDS_FORWARD_PLUS := "Not available with the OpenGL renderer (safe mode, or no Vulkan / Direct3D 12)"
const NOT_ON_WEB := "Not available in the web version (WebGL)"

var quality = null # quality.gd
var ui_hidden := false

var _toolbar: PanelContainer
var _tools: HBoxContainer
var _fps_label: Label
var _legend_button: Button
var _settings_button: Button
var _note_panel: PanelContainer
var _note: Label
var _panel: PanelContainer
var _scroll: ScrollContainer
var _rows: VBoxContainer
var _preset_list: OptionButton
var _controls := {} # Setting -> OptionButton or CheckBox
var _info: Label
var _show_panel: PanelContainer
var _bottom_limit := Callable() # -> y the settings must stay above (the replay bar's top)
var _fps_wait := 0.0
var _note_wait := 0.0
var _show_wait := 0.0

func _init() -> void:
	name = "SettingsPanel"
	mouse_filter = MOUSE_FILTER_IGNORE
	_build()

func _ready() -> void:
	set_anchors_and_offsets_preset(PRESET_FULL_RECT)
	resized.connect(_relayout)
	_toolbar.minimum_size_changed.connect(_relayout)
	_relayout()

## bottom_limit: returns the y the settings must stay above (they scroll when the window is too short)
func setup(quality_settings, bottom_limit: Callable) -> void:
	quality = quality_settings
	_bottom_limit = bottom_limit
	_legend_button.set_pressed_no_signal(quality.values["legend"])
	var web := OS.has_feature("web")
	_info.text = "%s · %s. Recommended with this renderer: %s.%s" % [RenderingServer.get_video_adapter_name(),
		"Vulkan / Direct3D 12" if Quality.is_forward_plus() else ("WebGL 2" if web else "OpenGL (Compatibility)"),
		Quality.preset_label(Quality.recommended_preset()),
		"" if quality.is_saved() else (" Changes apply to this visit only (?quality= in the address): they are not saved." \
			if web else " Changes apply to this run only (safe mode or --quality): they are not saved.")]
	refresh()
	if quality.note != "":
		show_note(quality.note)

## Shows the current settings (after a change, or the frame-rate check lowered them). A value the renderer does not
## have shows as what is drawn instead (no ambient occlusion)
func refresh() -> void:
	_preset_list.select(Quality.PRESETS.find(quality.preset) if quality.preset != Quality.CUSTOM else Quality.PRESETS.size())
	for key in _controls:
		var control: Control = _controls[key]
		var value = quality.values[key]
		if control is CheckBox:
			control.set_pressed_no_signal(value and Quality.is_available(key, value))
		else:
			var choices: Array = Quality.CHOICES[key]
			if not Quality.is_available(key, value):
				value = choices[0][0]
			for i in choices.size():
				if choices[i][0] == value:
					control.select(i)
	_fps_label.visible = quality.values["show_fps"]

## A note under the toolbar for a while (e.g. the quality was lowered at startup)
func show_note(text: String) -> void:
	_note.text = text
	_note_panel.visible = text != "" and not ui_hidden
	_note_wait = NOTE_SECONDS
	_relayout()

## The replay bar changed height: the settings fit above it again
func fit_above_bar() -> void:
	_relayout()

func is_panel_open() -> bool:
	return _panel.visible

## Open, the settings take the legend's place (and the note's: it has done its job)
func set_panel_open(open: bool) -> void:
	_panel.visible = open and not ui_hidden
	if _panel.visible:
		_note_panel.visible = false
	_settings_button.set_pressed_no_signal(_panel.visible)
	_relayout()
	panel_toggled.emit(_panel.visible)

## Hides every panel (main.gd hides its own) and shows the "Show UI" button while the mouse moves
func set_ui_hidden(hidden: bool) -> void:
	if hidden == ui_hidden:
		return
	ui_hidden = hidden
	_toolbar.visible = not hidden
	if hidden:
		set_panel_open(false)
		_note_panel.visible = false
	_show_panel.visible = hidden
	_show_wait = SHOW_BUTTON_SECONDS
	ui_hidden_changed.emit(hidden)

## Screen rectangles of the toolbar, the note, the settings and the "Show UI" button when shown (self-test)
func rects() -> Dictionary:
	var shown := {}
	for control: Control in [_toolbar, _note_panel, _panel, _show_panel]:
		if control.is_visible_in_tree():
			shown[String(control.name).to_lower()] = control.get_global_rect()
	return shown

func show_button_visible() -> bool:
	return _show_panel.visible

## Picks a preset as the list would (self-test)
func select_preset(preset: String) -> void:
	var index := Quality.PRESETS.find(preset)
	_preset_list.select(index)
	_preset_list.item_selected.emit(index)

func _process(delta: float) -> void:
	if _fps_label.visible:
		_fps_wait -= delta
		if _fps_wait <= 0.0:
			_fps_wait = FPS_REFRESH
			_fps_label.text = "%d fps" % roundi(Engine.get_frames_per_second())
	if _note_panel.visible:
		_note_wait -= delta
		if _note_wait <= 0.0:
			_note_panel.visible = false
			_relayout()
	if _show_panel.visible and not _show_panel.get_global_rect().has_point(get_global_mouse_position()):
		_show_wait -= delta
		if _show_wait <= 0.0:
			_show_panel.visible = false

## While the UI is hidden, moving the cursor shows the "Show UI" button. Free-fly (captured mouse) and orbiting (a button
## held) do not: they move the camera, the view stays clean. Nor do the motion events Windows sends for a still cursor
func _input(event: InputEvent) -> void:
	if ui_hidden and event is InputEventMouseMotion and event.relative != Vector2.ZERO and event.button_mask == 0 \
			and Input.mouse_mode != Input.MOUSE_MODE_CAPTURED:
		_show_panel.visible = true
		_show_wait = SHOW_BUTTON_SECONDS

## H hides or shows the UI. Typing in a text field (the "+ line" field) never gets here: the field takes the key
func _unhandled_key_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and not event.echo and event.keycode == KEY_H:
		set_ui_hidden(not ui_hidden)
		get_viewport().set_input_as_handled()

func _build() -> void:
	# Toolbar, on the panels' dark surface (its buttons are outlined: unreadable on the bright city). Clicks on it must
	# not reach the 3D view (a click there deselects the bus)
	_toolbar = OccupancyPanelScript.make_panel(OccupancyPanelScript.LEGEND_COLOR)
	_toolbar.name = "Toolbar"
	add_child(_toolbar)
	_tools = HBoxContainer.new()
	_tools.add_theme_constant_override("separation", 6)
	_toolbar.add_child(_tools)
	_fps_label = OccupancyPanelScript.make_label("", 12, OccupancyPanelScript.INK_SECONDARY)
	_fps_label.custom_minimum_size.x = 52 # "144 fps": the toolbar does not jitter as the number changes
	_fps_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	_fps_label.visible = false
	_tools.add_child(_fps_label)
	_legend_button = _make_tool_button("Legend", "Show or hide the legend")
	_legend_button.toggled.connect(func(on: bool): legend_toggled.emit(on))
	_settings_button = _make_tool_button("Settings", "Display quality: shadows, view distance, resolution...")
	_settings_button.toggled.connect(set_panel_open)
	var hide_button := _make_tool_button("Hide UI", "Hide every panel (H). Move the mouse to bring back the Show UI button")
	hide_button.toggle_mode = false
	hide_button.pressed.connect(set_ui_hidden.bind(true))

	_note_panel = OccupancyPanelScript.make_panel(OccupancyPanelScript.LEGEND_COLOR)
	_note_panel.name = "Note"
	_note_panel.custom_minimum_size.x = PANEL_WIDTH
	_note_panel.visible = false
	add_child(_note_panel)
	_note = OccupancyPanelScript.make_label("", 12, OccupancyPanelScript.INK_PRIMARY)
	_note.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_note.custom_minimum_size.x = PANEL_WIDTH - 2 * OccupancyPanelScript.PADDING # Wraps at the panel's width (its height follows)
	_note_panel.add_child(_note)
	_note_panel.mouse_filter = MOUSE_FILTER_IGNORE
	_note.mouse_filter = MOUSE_FILTER_IGNORE

	_build_panel()

	# Shown instead of everything while the UI is hidden, when the mouse moves
	_show_panel = OccupancyPanelScript.make_panel(OccupancyPanelScript.LEGEND_COLOR)
	_show_panel.name = "ShowUI"
	_show_panel.visible = false
	add_child(_show_panel)
	var show_button := OccupancyPanelScript.make_chip("Show UI")
	show_button.toggle_mode = false
	show_button.tooltip_text = "Show the panels again (H)"
	show_button.pressed.connect(set_ui_hidden.bind(false))
	_show_panel.add_child(show_button)

## The settings: a preset, then each setting (changing one makes the preset "Custom")
func _build_panel() -> void:
	_panel = OccupancyPanelScript.make_panel(OccupancyPanelScript.PANEL_COLOR)
	_panel.name = "Settings"
	_panel.custom_minimum_size.x = PANEL_WIDTH
	_panel.mouse_filter = MOUSE_FILTER_STOP
	_panel.mouse_force_pass_scroll_events = false # The wheel over the panel must not zoom the camera
	_panel.visible = false
	add_child(_panel)
	_scroll = ScrollContainer.new()
	_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	_panel.add_child(_scroll)
	_rows = VBoxContainer.new()
	_rows.add_theme_constant_override("separation", 6)
	_rows.size_flags_horizontal = SIZE_EXPAND_FILL
	_scroll.add_child(_rows)

	var header := HBoxContainer.new()
	_rows.add_child(header)
	var title := OccupancyPanelScript.make_label("Display quality", 15, OccupancyPanelScript.INK_PRIMARY)
	title.size_flags_horizontal = SIZE_EXPAND_FILL
	header.add_child(title)
	var close_button := Button.new()
	close_button.text = "×"
	close_button.flat = true
	close_button.focus_mode = FOCUS_NONE
	close_button.tooltip_text = "Close the settings"
	close_button.add_theme_font_size_override("font_size", 16)
	close_button.add_theme_color_override("font_color", OccupancyPanelScript.INK_SECONDARY)
	close_button.add_theme_color_override("font_hover_color", OccupancyPanelScript.INK_PRIMARY)
	close_button.pressed.connect(set_panel_open.bind(false))
	header.add_child(close_button)

	var grid := GridContainer.new()
	grid.columns = 2
	grid.add_theme_constant_override("h_separation", 10)
	grid.add_theme_constant_override("v_separation", 4)
	_rows.add_child(grid)
	_preset_list = _add_option_row(grid, "Preset", "Every setting at once. Lower presets keep the view smooth on a less powerful computer")
	for preset in Quality.PRESETS:
		_preset_list.add_item(Quality.preset_label(preset))
	_preset_list.add_item(Quality.preset_label(Quality.CUSTOM))
	_preset_list.set_item_disabled(Quality.PRESETS.size(), true) # Shown when the settings match no preset
	_preset_list.item_selected.connect(_on_preset_selected)
	for key in ["shadows", "view_distance", "render_scale", "antialiasing", "ambient_occlusion", "glow", "headlights",
			"fps_limit", "show_fps"]:
		if Quality.CHOICES.has(key):
			var list := _add_option_row(grid, LABELS[key], TOOLTIPS[key])
			var choices: Array = Quality.CHOICES[key]
			for i in choices.size():
				list.add_item(choices[i][1])
				if not Quality.is_available(key, choices[i][0]):
					list.set_item_disabled(i, true)
					list.set_item_tooltip(i, NOT_ON_WEB if OS.has_feature("web") else NEEDS_FORWARD_PLUS)
			list.item_selected.connect(func(index: int): _change(key, choices[index][0]))
			_controls[key] = list
		else:
			var label := OccupancyPanelScript.make_label(LABELS[key], 12, OccupancyPanelScript.INK_PRIMARY)
			label.tooltip_text = TOOLTIPS[key]
			label.mouse_filter = MOUSE_FILTER_PASS
			grid.add_child(label)
			var check := OccupancyPanelScript.make_check("", null, TOOLTIPS[key])
			if not Quality.is_available(key, true):
				check.disabled = true
				check.tooltip_text = NOT_ON_WEB if OS.has_feature("web") else NEEDS_FORWARD_PLUS
			check.toggled.connect(func(on: bool): _change(key, on))
			grid.add_child(check)
			_controls[key] = check

	_info = OccupancyPanelScript.make_label("", 12, OccupancyPanelScript.INK_SECONDARY)
	_info.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_info.custom_minimum_size.x = PANEL_WIDTH - 2 * OccupancyPanelScript.PADDING - 12 # Wraps at the panel's width, a scroll bar beside it
	_rows.add_child(_info)
	var recommended := OccupancyPanelScript.make_chip("Use the recommended preset")
	recommended.toggle_mode = false
	recommended.size_flags_horizontal = SIZE_SHRINK_BEGIN
	recommended.pressed.connect(func(): _on_preset_selected(Quality.PRESETS.find(Quality.recommended_preset())))
	_rows.add_child(recommended)

func _add_option_row(grid: GridContainer, text: String, tooltip: String) -> OptionButton:
	var label := OccupancyPanelScript.make_label(text, 12, OccupancyPanelScript.INK_PRIMARY)
	label.tooltip_text = tooltip
	label.mouse_filter = MOUSE_FILTER_PASS
	grid.add_child(label)
	var list := OptionButton.new()
	list.focus_mode = FOCUS_NONE
	list.tooltip_text = tooltip
	list.size_flags_horizontal = SIZE_EXPAND_FILL
	list.add_theme_font_size_override("font_size", 12)
	grid.add_child(list)
	return list

func _make_tool_button(text: String, tooltip: String) -> Button:
	var button := OccupancyPanelScript.make_chip(text)
	button.tooltip_text = tooltip
	_tools.add_child(button)
	return button

func _on_preset_selected(index: int) -> void:
	if index < 0 or index >= Quality.PRESETS.size():
		return
	quality.set_preset(Quality.PRESETS[index])
	refresh()
	settings_changed.emit()

func _change(key: String, value) -> void:
	quality.set_value(key, value)
	refresh()
	settings_changed.emit()

## The toolbar at the top right, the note or the settings under it (above the replay bar: they scroll if needed); tells
## main.gd where the legend column starts and how wide the right column is, so the left column keeps clear of it
func _relayout() -> void:
	if not is_inside_tree():
		return
	_toolbar.reset_size()
	_toolbar.position = Vector2(size.x - MARGIN - _toolbar.size.x, MARGIN)
	var y := _toolbar.position.y + _toolbar.size.y + OccupancyPanelScript.PADDING
	_note_panel.reset_size()
	_note_panel.position = Vector2(size.x - MARGIN - _note_panel.size.x, y)
	if _note_panel.visible:
		y += _note_panel.size.y + OccupancyPanelScript.PADDING
	var bottom: float = _bottom_limit.call() if _bottom_limit.is_valid() else size.y
	var room := bottom - OccupancyPanelScript.PADDING - y - 2.0 * OccupancyPanelScript.PADDING
	_scroll.custom_minimum_size = Vector2(PANEL_WIDTH - 2 * OccupancyPanelScript.PADDING,
		clampf(_rows.get_combined_minimum_size().y, 0.0, maxf(room, MIN_PANEL_HEIGHT)))
	_panel.reset_size()
	_panel.position = Vector2(size.x - MARGIN - _panel.size.x, y)
	_show_panel.reset_size()
	_show_panel.position = Vector2(size.x - MARGIN - _show_panel.size.x, MARGIN)
	var width := _toolbar.size.x
	for panel: Control in [_note_panel, _panel]:
		if panel.visible:
			width = maxf(width, panel.size.x)
	column_changed.emit(y, width)
