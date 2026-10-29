extends Control

## The top-left column: the legend (occupancy colors, and checkboxes for the scene's stop and reference-point poles),
## the Lines panel (show or hide each line, add one by number) and the chart of the selected bus: its observed occupancy,
## the precomputed forecast (dashed median and range) and, in replays, optionally what really happened.
## Built in code like replay_controls.gd; main.gd feeds it the selected bus and the clock.

signal close_requested()
signal lines_hidden_changed(hidden: PackedStringArray) # Lines whose buses and stops are hidden; empty = all shown
signal lines_added(routes: PackedStringArray) # Route numbers typed in the Lines panel's field
signal layer_toggled(layer: String, shown: bool) # "stops" or "references"

const Occupancy = preload("res://scripts/occupancy.gd")
const LEGEND_COLOR := Color(0.08, 0.08, 0.08, 0.66) # Same surface as the replay bar
const PANEL_COLOR := Color(0.08, 0.08, 0.08, 1.0) # Opaque: beacons behind the chart would show through the plot as streaks
const RING_COLOR := Color(0.08, 0.08, 0.08) # Ring around the hover dots
# On-screen colors of the scene's other poles: the stops are unshaded (main.gd sets them), the reference points are lit
# and shaded (sampled from screenshots; reference_point.tscn sets Color(0, 0.333, 1))
const STOP_COLOR := Color("#2db1c9")
const OTHER_STOP_COLOR := Color("#b4c3c6") # Stops of the other lines while a bus is selected
const REFERENCE_COLOR := Color("#031b77")
const INK_PRIMARY := Color("#f0efec")
const INK_SECONDARY := Color("#c3c2b7")
const INK_MUTED := Color("#898781")
const GRID_COLOR := Color("#2c2c2a")
const AXIS_COLOR := Color("#383835")
const SERIES_COLOR := Color("#3987e5") # Observed and forecast: same bus, same color (solid vs dashed); also its outline
const BAND_ALPHA := 0.22
const TOOLTIP_COLOR := Color("#1a1a19")
const CHIP_BORDER := Color("#5c5b56")
const CHIP_ON_TEXT := Color("#141413")
const MARGIN := 16
const PADDING := 8
const LEGEND_SEPARATION := 12
const CHART_SIZE := Vector2(500, 280) # Narrower in a narrow window
const AXIS_FONT_SIZE := 11
const PAST_SECONDS := 1200.0 # Shown before now
const DEFAULT_HORIZON := 600.0 # Shown after now when there is no forecast
const TICK_SECONDS := 300.0
const GAP_SECONDS := 90.0 # The observed line breaks where two observations are further apart
const PLOT_TOP := 14.0 # Room above the plot for the "now" label
const PLOT_BOTTOM := 16.0 # Room below the plot for the time labels
const LEVEL_PAD := 6.0 # Keeps the top and bottom rows (lines and hover dots) inside the clipped plot
const SNAP_PIXELS := 12.0 # Hover snaps to the nearest observation or forecast point within this distance
const TIME_EPSILON := 0.01 # Seconds: frame and origin times may differ by rounding

var data = null # OccupancyData (occupancy_data.gd)
var vehicle_id := "" # Bus shown in the chart, "" when the chart is hidden

var _now := 0.0
var _utc_offset := 0
var _is_replay := false
var _x_start := 0.0
var _x_end := 1.0
var _hover_x := -1.0 # Mouse x in plot-area coordinates, < 0 when not hovering
var _hover_time := -1.0 # Snapped time under the mouse, < 0 when none
var _statuses_version := -1 # data.statuses_version the legend and the rows were built for
var _rows := PackedInt32Array() # Statuses with a chart row, bottom (emptiest) to top
var _row_of := {} # status -> row index
# Series of the shown bus at the current time (rebuilt by _rebuild, only drawn by the draw callbacks)
var _observed_times := PackedFloat64Array()
var _observed_statuses := PackedInt32Array()
var _actual_times := PackedFloat64Array()
var _actual_statuses := PackedInt32Array()
var _forecast = null # Dictionary from OccupancyData.forecast_at(), or null
var _fc_times := PackedFloat64Array() # Forecast anchors: the origin (at the observed status) then each point
var _fc_lo := PackedFloat64Array() # Ranks (see OccupancyData.forecast_levels), drawn at _row_of_rank
var _fc_median := PackedFloat64Array()
var _fc_hi := PackedFloat64Array()
var _history_points := 0
var _forecast_points := 0
var _message := ""
var _future_note := "" # Shown in the empty plot area after now when there is nothing to draw there
var _calibration_note := "" # Shown at the top right of the plot while the shown forecast is partly uncalibrated
var _label_column := 0.0
var _hidden := {} # route -> true: lines whose buses and stops are hidden

var _legend: PanelContainer
var _legend_flow: HFlowContainer
var _status_items: Array[Control] = []
var _markers: HBoxContainer
var _line_stop_item: HBoxContainer
var _line_stop_label: Label
var _stop_check: CheckBox
var _reference_check: CheckBox
var _lines: PanelContainer
var _lines_flow: HFlowContainer
var _all_button: Button
var _line_buttons := {} # route -> Button
var _add_field: LineEdit
var _lines_note: Label
var _chart: PanelContainer
var _title: Label
var _status_swatch: Control
var _status_label: Label
var _plot: Control
var _area: Control
var _forecast_keys: HBoxContainer
var _range_label: Label
var _actual_key: Control
var _actual_check: CheckBox
var _footer: Label
var _tooltip: PanelContainer
var _tip_first: Label
var _tip_second: Label

func _init() -> void:
	name = "OccupancyPanel"
	mouse_filter = MOUSE_FILTER_IGNORE
	_build()

func _ready() -> void:
	set_anchors_and_offsets_preset(PRESET_FULL_RECT)
	resized.connect(_relayout)
	# The legend's and the line bar's heights are known once their lines are wrapped: place what is under them again then
	_legend.minimum_size_changed.connect(_relayout)
	_lines.minimum_size_changed.connect(_relayout)
	_relayout()

func setup(occupancy_data) -> void:
	data = occupancy_data
	_sync_statuses()

## A click anywhere outside the "+ line" field leaves it, so the camera's keys (E/Q zoom, WASD) work again. The buttons
## do not take the focus, so clicking them would not
func _input(event: InputEvent) -> void:
	if event is InputEventMouseButton and event.pressed and _add_field.has_focus() \
			and not _add_field.get_global_rect().has_point(event.position):
		_add_field.release_focus()

## Sets the legend's stop and reference-point checkboxes to what the scene shows. line_stops: what the highlighted stops
## are (e.g. "Stops of route 51 (Nord)"), "" when no line is highlighted; the other stops are then faded
func set_marker_legend(stops_visible: bool, references_visible: bool, line_stops := "") -> void:
	var highlighted := stops_visible and line_stops != ""
	_line_stop_item.visible = highlighted
	_line_stop_label.text = line_stops
	_stop_check.set_pressed_no_signal(stops_visible)
	_stop_check.text = "Other stops" if highlighted else "Bus stops"
	_stop_check.icon = _swatch_texture(OTHER_STOP_COLOR if highlighted else STOP_COLOR)
	_reference_check.set_pressed_no_signal(references_visible)
	_relayout()

## One button per line (ascending numbers); names: route -> long name (tooltips). Lines still listed stay hidden
func set_routes(routes: PackedStringArray, names := {}) -> void:
	for button in _line_buttons.values():
		_lines_flow.remove_child(button)
		button.queue_free()
	_line_buttons = {}
	var sorted_routes := Array(routes)
	sorted_routes.sort_custom(func(a, b): return a.to_int() < b.to_int() if a.to_int() != b.to_int() else a < b)
	var index := _all_button.get_index() + 1
	for route in sorted_routes:
		var button := _make_chip(route)
		var long_name: String = names.get(route, "")
		button.tooltip_text = "Route %s%s\nClick: show or hide this line. Double-click: only this line." % [route, " · " + long_name if long_name != "" else ""]
		button.pressed.connect(_toggle_line.bind(route))
		button.gui_input.connect(_on_line_input.bind(route))
		_lines_flow.add_child(button)
		_lines_flow.move_child(button, index) # Before the "add a line" field
		index += 1
		_line_buttons[route] = button
	var kept := {}
	for route in _hidden:
		if _line_buttons.has(route):
			kept[route] = true
	_set_hidden(kept)
	_relayout()

## Lines whose buses and stops are hidden (sorted)
func hidden_lines() -> PackedStringArray:
	var routes := PackedStringArray(_hidden.keys())
	routes.sort()
	return routes

## Shows these lines again (e.g. typed in the field while hidden)
func show_lines(routes: PackedStringArray) -> void:
	var wanted := _hidden.duplicate()
	for route in routes:
		wanted.erase(route)
	_set_hidden(wanted)

## A short message under the line buttons ("" hides it), e.g. why a typed line was not added
func show_lines_note(text: String) -> void:
	_lines_note.text = text
	_lines_note.tooltip_text = text
	_lines_note.visible = text != ""
	_relayout()

## Button of a line, null when it has none (self-test)
func line_button(route: String) -> Button:
	return _line_buttons.get(route)

## The "All" button (self-test)
func all_button() -> Button:
	return _all_button

## Types text into the "add a line" field and presses Enter, as a user would (self-test)
func submit_line_text(text: String) -> void:
	_add_field.text = text
	_add_field.text_submitted.emit(text)

## The legend checkbox of a layer: "stops" or "references" (self-test)
func layer_check(layer: String) -> CheckBox:
	return _stop_check if layer == "stops" else _reference_check

## The note under the line buttons (self-test)
func lines_note() -> String:
	return _lines_note.text if _lines_note.visible else ""

## Shows the chart of a bus (vehicle id = bus node name)
func show_bus(id: String) -> void:
	vehicle_id = id
	_hover_x = -1.0
	_chart.visible = true
	_relayout()
	_rebuild()

func hide_chart() -> void:
	vehicle_id = ""
	_hover_x = -1.0
	_hover_time = -1.0
	_chart.visible = false
	_tooltip.visible = false

## Sets the chart clock: now as unix time, the UTC offset of the time labels, replay or live
func set_time(now_unix: float, utc_offset_seconds: int, is_replay: bool) -> void:
	_now = now_unix
	_utc_offset = utc_offset_seconds
	_is_replay = is_replay
	_sync_statuses()
	if _chart.visible:
		_rebuild()

## Redraws after the data store changed (e.g. a new replay was loaded)
func refresh() -> void:
	_sync_statuses()
	if _chart.visible:
		_rebuild()

func set_show_actual(enabled: bool) -> void:
	_actual_check.button_pressed = enabled

## Hovers the chart at a time, as the mouse would (used for the self-test screenshot)
func hover_time(unix_time: float) -> void:
	if _chart.visible:
		_hover_x = _x_of(unix_time)
		_rebuild()

## What the chart currently shows (self-test)
func chart_stats() -> Dictionary:
	var rows := PackedStringArray()
	for status in _rows:
		rows.append(Occupancy.label(status))
	return {"visible": _chart.visible, "vehicle": vehicle_id, "history_points": _history_points if _chart.visible else 0,
		"forecast_points": _forecast_points if _chart.visible else 0, "message": _message if _chart.visible else "",
		"rows": rows, "legend": legend_labels(), "calibration_note": _calibration_note if _chart.visible else "",
		"footer_tooltip": _footer.tooltip_text if _chart.visible else ""}

## Labels of the legend's occupancy entries, in order (self-test)
func legend_labels() -> PackedStringArray:
	var labels := PackedStringArray()
	for item in _status_items:
		labels.append((item.get_child(1) as Label).text)
	return labels

func _build() -> void:
	# Legend strip: display only, clicks go through to the 3D view. It wraps onto more lines in narrow windows
	_legend = _make_panel(LEGEND_COLOR)
	_legend.name = "Legend"
	add_child(_legend)
	_legend_flow = HFlowContainer.new()
	_legend_flow.add_theme_constant_override("h_separation", LEGEND_SEPARATION)
	_legend_flow.add_theme_constant_override("v_separation", 4)
	_legend.add_child(_legend_flow)
	_legend_flow.add_child(_make_label("Bus occupancy", 13, INK_PRIMARY))
	# The status entries are inserted here (_sync_statuses), then the scene's other colored poles, set apart from the
	# occupancy colors (one flow item: they wrap together)
	_markers = HBoxContainer.new()
	_markers.add_theme_constant_override("separation", LEGEND_SEPARATION)
	_markers.add_child(VSeparator.new())
	_line_stop_item = _make_legend_item(STOP_COLOR, "", true)
	_line_stop_label = _line_stop_item.get_child(1) as Label
	_line_stop_item.visible = false
	_markers.add_child(_line_stop_item)
	_stop_check = _make_layer_check("Bus stops", STOP_COLOR, "Show or hide the bus stops of the lines shown")
	_stop_check.toggled.connect(func(on: bool): layer_toggled.emit("stops", on))
	_markers.add_child(_stop_check)
	_reference_check = _make_layer_check("Reference points", REFERENCE_COLOR,
		"Show or hide the reference points: the poles of known coordinates that place the buses on the 3D map")
	_reference_check.toggled.connect(func(on: bool): layer_toggled.emit("references", on))
	_markers.add_child(_reference_check)
	_legend_flow.add_child(_markers)
	# Display only, clicks go through to the 3D view, except on the two checkboxes
	_ignore_mouse(_legend)
	_stop_check.mouse_filter = MOUSE_FILTER_STOP
	_reference_check.mouse_filter = MOUSE_FILTER_STOP

	# Lines: one button per line, shown (filled) or hidden (outlined), and a field to add one. Clicks on the panel must
	# not reach the 3D view (a click there deselects the bus)
	_lines = _make_panel(LEGEND_COLOR)
	_lines.name = "Lines"
	_lines.mouse_filter = MOUSE_FILTER_STOP
	_lines.mouse_force_pass_scroll_events = false # The wheel over the panel must not zoom the camera
	add_child(_lines)
	_lines_flow = HFlowContainer.new()
	_lines_flow.add_theme_constant_override("h_separation", 6)
	_lines_flow.add_theme_constant_override("v_separation", 4)
	_lines.add_child(_lines_flow)
	var lines_label := _make_label("Lines", 13, INK_PRIMARY)
	lines_label.mouse_filter = MOUSE_FILTER_IGNORE
	_lines_flow.add_child(lines_label)
	_all_button = _make_chip("All")
	_all_button.tooltip_text = "Show every line"
	_all_button.pressed.connect(func(): _set_hidden({}))
	_lines_flow.add_child(_all_button)
	# The line buttons go here (set_routes)
	_add_field = LineEdit.new()
	_add_field.placeholder_text = "+ line"
	_add_field.custom_minimum_size.x = 64
	_add_field.add_theme_font_size_override("font_size", 12)
	_add_field.tooltip_text = "Add a line: type its number (several: 24, 51) and press Enter"
	_add_field.text_submitted.connect(_on_add_submitted)
	_lines_flow.add_child(_add_field)
	_lines_note = _make_label("", 12, INK_SECONDARY)
	_lines_note.mouse_filter = MOUSE_FILTER_PASS # Tooltip with the full text
	_lines_note.clip_text = true # Width capped to the window in _relayout
	_lines_note.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_lines_note.visible = false
	_lines_flow.add_child(_lines_note)

	# Chart panel: clicks and scrolls on it must not reach the 3D view (a click there deselects the bus)
	_chart = _make_panel(PANEL_COLOR)
	_chart.name = "Chart"
	_chart.mouse_filter = MOUSE_FILTER_STOP
	_chart.mouse_force_pass_scroll_events = false
	_chart.custom_minimum_size = CHART_SIZE
	_chart.visible = false
	add_child(_chart)
	var rows := VBoxContainer.new()
	rows.add_theme_constant_override("separation", 4)
	_chart.add_child(rows)

	var header := HBoxContainer.new()
	rows.add_child(header)
	_title = _make_label("", 15, INK_PRIMARY)
	_title.size_flags_horizontal = SIZE_EXPAND_FILL
	_title.clip_text = true
	_title.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	header.add_child(_title)
	var close_button := Button.new()
	close_button.text = "×"
	close_button.flat = true
	close_button.focus_mode = FOCUS_NONE
	close_button.tooltip_text = "Close the chart (releases the bus)"
	close_button.add_theme_font_size_override("font_size", 16)
	close_button.add_theme_color_override("font_color", INK_SECONDARY)
	close_button.add_theme_color_override("font_hover_color", INK_PRIMARY)
	close_button.add_theme_color_override("font_pressed_color", INK_PRIMARY)
	close_button.pressed.connect(func(): close_requested.emit())
	header.add_child(close_button)

	var status := HBoxContainer.new()
	status.add_theme_constant_override("separation", 6)
	rows.add_child(status)
	_status_swatch = _make_swatch(Occupancy.color(Occupancy.NO_DATA))
	status.add_child(_status_swatch)
	_status_label = _make_label("", 13, INK_PRIMARY)
	_status_label.size_flags_horizontal = SIZE_EXPAND_FILL
	_status_label.clip_text = true
	_status_label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_status_label.mouse_filter = MOUSE_FILTER_PASS # Tooltip with the full text
	status.add_child(_status_label)

	# The plot draws its axes; the clipped area inside it draws the series
	_plot = Control.new()
	_plot.size_flags_vertical = SIZE_EXPAND_FILL
	_plot.custom_minimum_size.y = 140
	_plot.mouse_filter = MOUSE_FILTER_PASS
	_plot.draw.connect(_draw_axes)
	_plot.gui_input.connect(_on_plot_input)
	_plot.mouse_exited.connect(_on_plot_mouse_exited)
	_plot.resized.connect(_layout_plot)
	rows.add_child(_plot)
	_area = Control.new()
	_area.clip_contents = true
	_area.mouse_filter = MOUSE_FILTER_IGNORE
	_area.draw.connect(_draw_series)
	_plot.add_child(_area)

	var key_row := HBoxContainer.new()
	key_row.add_theme_constant_override("separation", 12)
	rows.add_child(key_row)
	key_row.add_child(_make_key("solid", "Observed"))
	_forecast_keys = HBoxContainer.new()
	_forecast_keys.add_theme_constant_override("separation", 12)
	_forecast_keys.add_child(_make_key("dashed", "Forecast median"))
	var range_key := _make_key("band", "80% range")
	_range_label = range_key.get_child(1) as Label
	_forecast_keys.add_child(range_key)
	key_row.add_child(_forecast_keys)
	var spacer := Control.new()
	spacer.size_flags_horizontal = SIZE_EXPAND_FILL
	spacer.mouse_filter = MOUSE_FILTER_IGNORE
	key_row.add_child(spacer)
	# "Show actual", with the sample of the dotted line while it is drawn (no extra label: the row is full)
	var actual := HBoxContainer.new()
	actual.add_theme_constant_override("separation", 4)
	key_row.add_child(actual)
	_actual_key = _make_glyph("dotted")
	actual.add_child(_actual_key)
	_actual_check = CheckBox.new()
	_actual_check.text = "Show actual"
	_actual_check.focus_mode = FOCUS_NONE
	_actual_check.tooltip_text = "Also draw what really happened after now (dotted), to compare with the forecast"
	_actual_check.add_theme_font_size_override("font_size", 12)
	for color_name in ["font_color", "font_pressed_color", "font_focus_color"]:
		_actual_check.add_theme_color_override(color_name, INK_SECONDARY)
	for color_name in ["font_hover_color", "font_hover_pressed_color"]:
		_actual_check.add_theme_color_override(color_name, INK_PRIMARY)
	_actual_check.toggled.connect(func(_on: bool): refresh())
	actual.add_child(_actual_check)

	_footer = _make_label("", 12, INK_SECONDARY)
	_footer.clip_text = true
	_footer.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_footer.mouse_filter = MOUSE_FILTER_PASS # Tooltip with the full text
	rows.add_child(_footer)

	# Hover tooltip, above everything else
	_tooltip = PanelContainer.new()
	var tip_style := StyleBoxFlat.new()
	tip_style.bg_color = TOOLTIP_COLOR
	tip_style.border_color = AXIS_COLOR
	tip_style.set_border_width_all(1)
	tip_style.set_corner_radius_all(3)
	tip_style.content_margin_left = 6
	tip_style.content_margin_right = 6
	tip_style.content_margin_top = 3
	tip_style.content_margin_bottom = 3
	_tooltip.add_theme_stylebox_override("panel", tip_style)
	_tooltip.visible = false
	add_child(_tooltip)
	var tip_rows := VBoxContainer.new()
	tip_rows.add_theme_constant_override("separation", 0)
	_tooltip.add_child(tip_rows)
	_tip_first = _make_label("", 12, INK_PRIMARY)
	_tip_second = _make_label("", 12, INK_SECONDARY)
	tip_rows.add_child(_tip_first)
	tip_rows.add_child(_tip_second)
	_ignore_mouse(_tooltip)

func _make_panel(color: Color) -> PanelContainer:
	var panel := PanelContainer.new()
	var style := StyleBoxFlat.new()
	style.bg_color = color
	style.set_content_margin_all(PADDING)
	style.set_corner_radius_all(4)
	panel.add_theme_stylebox_override("panel", style)
	return panel

func _make_label(text: String, font_size: int, color: Color) -> Label:
	var label := Label.new()
	label.text = text
	label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	label.add_theme_font_size_override("font_size", font_size)
	label.add_theme_color_override("font_color", color)
	return label

## Legend entry: swatch and label (a taller swatch for the highlighted stops: their poles are thicker)
func _make_legend_item(color: Color, text: String, wide := false) -> HBoxContainer:
	var item := HBoxContainer.new()
	item.add_theme_constant_override("separation", 5)
	var swatch := _make_swatch(color)
	if wide:
		swatch.custom_minimum_size = Vector2(12, 16)
	item.add_child(swatch)
	item.add_child(_make_label(text, 13, INK_PRIMARY))
	return item

## 12x12 color square with a 1 px ring (the dark statuses are low-contrast on the panel)
func _make_swatch(color: Color) -> Control:
	var swatch := Control.new()
	swatch.custom_minimum_size = Vector2(12, 12)
	swatch.size_flags_vertical = SIZE_SHRINK_CENTER
	swatch.mouse_filter = MOUSE_FILTER_IGNORE
	swatch.set_meta("color", color)
	swatch.draw.connect(_draw_swatch.bind(swatch))
	return swatch

func _draw_swatch(swatch: Control) -> void:
	swatch.draw_rect(Rect2(Vector2.ZERO, swatch.size), swatch.get_meta("color"))
	swatch.draw_rect(Rect2(Vector2(0.5, 0.5), swatch.size - Vector2.ONE), INK_SECONDARY, false, 1.0)

## The same 12x12 swatch as a texture, for a checkbox icon
static func _swatch_texture(color: Color) -> ImageTexture:
	var image := Image.create(12, 12, false, Image.FORMAT_RGBA8)
	image.fill(INK_SECONDARY)
	image.fill_rect(Rect2i(1, 1, 10, 10), color)
	return ImageTexture.create_from_image(image)

## Legend checkbox of a layer of poles: box, color swatch and name
func _make_layer_check(text: String, color: Color, tooltip: String) -> CheckBox:
	var check := CheckBox.new()
	check.text = text
	check.icon = _swatch_texture(color)
	check.button_pressed = true
	check.focus_mode = FOCUS_NONE
	check.tooltip_text = tooltip
	check.add_theme_font_size_override("font_size", 13)
	check.add_theme_constant_override("h_separation", 5)
	for color_name in ["font_color", "font_pressed_color", "font_hover_color", "font_hover_pressed_color", "font_focus_color"]:
		check.add_theme_color_override(color_name, INK_PRIMARY)
	return check

## Line button: filled while the line is shown, outlined while it is hidden
func _make_chip(text: String) -> Button:
	var button := Button.new()
	button.text = text
	button.toggle_mode = true
	button.focus_mode = FOCUS_NONE
	button.add_theme_font_size_override("font_size", 12)
	for state: String in ["normal", "hover", "pressed", "hover_pressed"]:
		var style := StyleBoxFlat.new()
		var on := state.contains("pressed")
		style.bg_color = INK_PRIMARY if on else (Color(1, 1, 1, 0.08) if state == "hover" else Color(0, 0, 0, 0))
		style.border_color = INK_PRIMARY if on else CHIP_BORDER
		style.set_border_width_all(1)
		style.set_corner_radius_all(10)
		style.content_margin_left = 9
		style.content_margin_right = 9
		style.content_margin_top = 2
		style.content_margin_bottom = 2
		button.add_theme_stylebox_override(state, style)
	for color_name in ["font_color", "font_hover_color"]:
		button.add_theme_color_override(color_name, INK_PRIMARY)
	for color_name in ["font_pressed_color", "font_hover_pressed_color"]:
		button.add_theme_color_override(color_name, CHIP_ON_TEXT)
	return button

## Click: show or hide the line
func _toggle_line(route: String) -> void:
	var wanted := _hidden.duplicate()
	if wanted.has(route):
		wanted.erase(route)
	else:
		wanted[route] = true
	_set_hidden(wanted)

## Double-click: only this line. The event is taken before the button sees it, so the second click does not toggle
func _on_line_input(event: InputEvent, route: String) -> void:
	if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_LEFT and event.pressed and event.double_click:
		accept_event()
		var wanted := {}
		for other in _line_buttons:
			if other != route:
				wanted[other] = true
		_set_hidden(wanted)

## Enter in the field: the route numbers typed (any separators), for main.gd to add
func _on_add_submitted(text: String) -> void:
	var routes := PackedStringArray()
	for part in text.replace(",", " ").replace(";", " ").split(" ", false):
		var route := part.strip_edges()
		if route != "" and not routes.has(route):
			routes.append(route)
	_add_field.clear()
	_add_field.release_focus() # The camera's keys work again
	if not routes.is_empty():
		lines_added.emit(routes)

func _set_hidden(wanted: Dictionary) -> void:
	var changed: bool = wanted.size() != _hidden.size() or wanted.keys().any(func(route): return not _hidden.has(route))
	_hidden = wanted
	_all_button.set_pressed_no_signal(_hidden.is_empty())
	for route in _line_buttons:
		_line_buttons[route].set_pressed_no_signal(not _hidden.has(route))
	if changed:
		lines_hidden_changed.emit(hidden_lines())

## Chart key: a drawn sample of the series style and its label
func _make_key(style: String, text: String) -> HBoxContainer:
	var key := HBoxContainer.new()
	key.add_theme_constant_override("separation", 4)
	key.add_child(_make_glyph(style))
	key.add_child(_make_label(text, 12, INK_SECONDARY))
	return key

## Drawn sample of a series style (the line glyphs are not in every font)
func _make_glyph(style: String) -> Control:
	var glyph := Control.new()
	glyph.custom_minimum_size = Vector2(20, 12)
	glyph.size_flags_vertical = SIZE_SHRINK_CENTER
	glyph.mouse_filter = MOUSE_FILTER_IGNORE
	glyph.draw.connect(_draw_key_glyph.bind(glyph, style))
	return glyph

func _draw_key_glyph(glyph: Control, style: String) -> void:
	var y := glyph.size.y * 0.5
	match style:
		"solid":
			glyph.draw_line(Vector2(0, y), Vector2(glyph.size.x, y), SERIES_COLOR, 2.0)
		"dashed":
			_draw_dashed(glyph, PackedVector2Array([Vector2(0, y), Vector2(glyph.size.x, y)]), SERIES_COLOR, 2.0, 5.0, 3.0)
		"dotted":
			_draw_dashed(glyph, PackedVector2Array([Vector2(0, y), Vector2(glyph.size.x, y)]), INK_SECONDARY, 2.0, 2.0, 3.0)
		"band":
			glyph.draw_rect(Rect2(0, 1, glyph.size.x, glyph.size.y - 2), Color(SERIES_COLOR, BAND_ALPHA))

static func _ignore_mouse(control: Control) -> void:
	control.mouse_filter = MOUSE_FILTER_IGNORE
	for child in control.find_children("*", "Control", true, false):
		child.mouse_filter = MOUSE_FILTER_IGNORE

## Legend entries and chart rows for the statuses in the data (the spec lets a feed use only some of them: STM sends
## 1, 2, 3 and 5), rebuilt when the data store saw new ones
func _sync_statuses() -> void:
	if data == null or data.statuses_version == _statuses_version:
		return
	_statuses_version = data.statuses_version
	for item in _status_items:
		_legend_flow.remove_child(item)
		item.queue_free()
	_status_items = []
	var statuses: Array = data.statuses_seen.keys()
	statuses.sort()
	statuses.append(Occupancy.NO_DATA) # Always listed: a bus can lose its data
	var index := 1 # After the "Bus occupancy" title
	for status in statuses:
		var item := _make_legend_item(Occupancy.color(status), Occupancy.label(status))
		_ignore_mouse(item)
		_legend_flow.add_child(item)
		_legend_flow.move_child(item, index)
		_status_items.append(item)
		index += 1
	_rows = data.chart_levels()
	_row_of = {}
	for i in _rows.size():
		_row_of[_rows[i]] = i
	_label_column = 0.0
	if _plot.is_inside_tree():
		_layout_plot()
	_relayout()

## Legend, Lines panel (each wrapped when it does not fit on one line) and chart, in a column at the top left
func _relayout() -> void:
	if not is_inside_tree():
		return
	var room := size.x - 2.0 * MARGIN
	_legend.custom_minimum_size.x = maxf(minf(_line_width(_legend_flow, LEGEND_SEPARATION), room), 0.0)
	_legend.reset_size()
	_legend.position = Vector2(MARGIN, MARGIN)
	var y := MARGIN + _legend.size.y + PADDING
	# The note shows in full when it fits, else it ends with "…" (its tooltip has the full text)
	var note_font := _lines_note.get_theme_font("font")
	var note_width := note_font.get_string_size(_lines_note.text, HORIZONTAL_ALIGNMENT_LEFT, -1, 12).x
	_lines_note.custom_minimum_size.x = minf(ceilf(note_width), maxf(room - 2.0 * PADDING, 0.0))
	_lines.custom_minimum_size.x = maxf(minf(_line_width(_lines_flow, 6), room), 0.0)
	_lines.reset_size()
	_lines.position = Vector2(MARGIN, y)
	y += _lines.size.y + PADDING
	_chart.custom_minimum_size.x = clampf(room, 0.0, CHART_SIZE.x) # The key row keeps it readable (~470 px)
	_chart.reset_size()
	_chart.position = Vector2(MARGIN, y)

## Width of a panel's flow container on a single line (a flow container's minimum is its widest item)
func _line_width(flow: HFlowContainer, separation: int) -> float:
	var width := 2.0 * PADDING
	var count := 0
	for child in flow.get_children():
		if child is Control and child.visible:
			width += ceilf(child.get_combined_minimum_size().x)
			count += 1
	return width + maxi(count - 1, 0) * separation

## Places the clipped series area inside the plot, right of the row labels
func _layout_plot() -> void:
	if _label_column <= 0.0:
		var font := _plot.get_theme_font("font", "Label")
		for status in _rows:
			_label_column = maxf(_label_column, font.get_string_size(Occupancy.label(status), HORIZONTAL_ALIGNMENT_LEFT, -1, AXIS_FONT_SIZE).x)
		_label_column = ceilf(_label_column) + 8.0
	_area.position = Vector2(_label_column, PLOT_TOP)
	_area.size = Vector2(maxf(_plot.size.x - _label_column - 4.0, 1.0), maxf(_plot.size.y - PLOT_TOP - PLOT_BOTTOM, 1.0))

## Plot-area x of a unix time (computed in 64-bit before it becomes a Vector2 coordinate)
func _x_of(unix_time: float) -> float:
	return (unix_time - _x_start) / (_x_end - _x_start) * _area.size.x

## Plot-area y of a row (fractional for forecasts); the rows are evenly spaced, the emptiest at the bottom
func _y_of(row: float) -> float:
	var top_row := float(_rows.size() - 1)
	if top_row <= 0.0:
		return _area.size.y * 0.5
	return LEVEL_PAD + (top_row - clampf(row, 0.0, top_row)) / top_row * (_area.size.y - 2.0 * LEVEL_PAD)

## Row of a status, -1 when it has none (no data, or a status that is not a level: not accepting passengers...)
func _row_of_status(status: int) -> int:
	return _row_of.get(status, -1)

## Row of a forecast rank: between the rows of the two statuses it lies between
func _row_of_rank(rank: float) -> float:
	var levels: PackedInt32Array = data.forecast_levels
	if levels.is_empty():
		return 0.0
	var clamped := clampf(rank, 0.0, levels.size() - 1)
	var below := floori(clamped)
	var above := mini(below + 1, levels.size() - 1)
	var row_below := float(_row_of_status(levels[below]))
	return lerpf(row_below, float(_row_of_status(levels[above])), clamped - below)

## Local wall-clock time of a unix time, "HH:MM" or "HH:MM:SS"
func _clock(unix_time: float, with_seconds := false) -> String:
	var d := Time.get_datetime_dict_from_unix_time(int(floor(unix_time)) + _utc_offset)
	if with_seconds:
		return "%02d:%02d:%02d" % [d["hour"], d["minute"], d["second"]]
	return "%02d:%02d" % [d["hour"], d["minute"]]

## Recomputes the shown series, texts and hover for the current bus and time, then redraws
func _rebuild() -> void:
	if vehicle_id == "" or data == null:
		return
	_actual_check.visible = _is_replay
	_actual_key.visible = _is_replay and _actual_check.button_pressed
	_forecast_keys.visible = data.has_forecast
	_range_label.text = "%d%% range" % data.range_percent()
	_forecast = data.forecast_at(vehicle_id, _now)
	_x_start = _now - PAST_SECONDS
	_x_end = _now + (data.horizon if data.has_forecast else DEFAULT_HORIZON)

	# Observed up to now, starting with the observation before the window (the line enters from the left)
	var times: PackedFloat64Array = data.history_times(vehicle_id)
	var statuses: PackedInt32Array = data.history_statuses(vehicle_id)
	var first := maxi(times.bsearch(_x_start) - 1, 0)
	var after_now := times.bsearch(_now + TIME_EPSILON, false)
	_observed_times = times.slice(first, after_now)
	_observed_statuses = statuses.slice(first, after_now)
	_history_points = 0
	for i in _observed_times.size():
		if _observed_times[i] >= _x_start and _row_of_status(_observed_statuses[i]) >= 0:
			_history_points += 1
	# What happened after now (replay only), from the observation in effect at now
	_actual_times = PackedFloat64Array()
	_actual_statuses = PackedInt32Array()
	if _is_replay and _actual_check.button_pressed:
		var last := times.bsearch(_x_end, false)
		_actual_times = times.slice(maxi(after_now - 1, 0), last)
		_actual_statuses = statuses.slice(maxi(after_now - 1, 0), last)

	# Forecast anchors (ranks): the origin at the observed status (zero-width range), then the stored points
	_fc_times = PackedFloat64Array()
	_fc_lo = PackedFloat64Array()
	_fc_median = PackedFloat64Array()
	_fc_hi = PackedFloat64Array()
	_forecast_points = 0
	if _forecast != null:
		var origin_rank: int = data.forecast_levels.find(data.status_at(vehicle_id, _forecast["origin"]))
		if origin_rank >= 0:
			_fc_times.append(_forecast["origin"])
			_fc_lo.append(origin_rank)
			_fc_median.append(origin_rank)
			_fc_hi.append(origin_rank)
		_fc_times.append_array(_forecast["times"])
		_fc_lo.append_array(_forecast["lo"])
		_fc_median.append_array(_forecast["median"])
		_fc_hi.append_array(_forecast["hi"])
		for t in _forecast["times"]:
			if t >= _x_start and t <= _x_end + TIME_EPSILON:
				_forecast_points += 1
	# Nothing to draw after now: say why there, not only in the footer
	_future_note = ""
	if _fc_times.is_empty() and _actual_times.is_empty():
		_future_note = "No forecast yet" if _is_replay and data.has_forecast and _too_little_history() else "No forecast"
	# Early in a calibrated replay, some points are still the raw model output
	_calibration_note = "calibration still learning" if _forecast != null and data.calibration_learning(_forecast["origin"]) else ""

	_update_texts()
	_update_hover()
	_plot.queue_redraw()
	_area.queue_redraw()

func _update_texts() -> void:
	var route: String = data.route_at(vehicle_id, _now)
	_title.text = "Bus %s · Route %s" % [vehicle_id, route] if route != "" else "Bus %s" % vehicle_id
	var status_now: int = data.status_at(vehicle_id, _now)
	_status_swatch.set_meta("color", Occupancy.color(status_now))
	_status_swatch.queue_redraw()
	var status := "Now: %s" % Occupancy.label(status_now)
	if _forecast != null:
		status += " · by %s likely %s" % [_clock(_fc_times[_fc_times.size() - 1]), Occupancy.label(data.status_of_rank(_fc_median[_fc_median.size() - 1]))]
	_status_label.text = status
	_status_label.tooltip_text = status
	_message = _footer_text()
	_footer.text = _message
	_footer.tooltip_text = _message if _forecast == null else _metrics_text()

func _footer_text() -> String:
	if not _is_replay:
		return "Live: forecasts are precomputed for replays"
	if data.forecast_error != "":
		return data.forecast_error
	if not data.forecast_in_replay:
		return "No forecast in this replay (see README: Occupancy colors, chart and forecasts)"
	if _forecast != null:
		var text: String = data.model_name
		var accuracy = data.metrics.get("accuracy")
		var baseline = data.metrics.get("accuracy_last_value")
		if _is_number(accuracy) and _is_number(baseline):
			# Computed from the metrics, never assumed: the model may well not beat "no change"
			var verdict := "better than" if float(accuracy) > float(baseline) + 0.005 else "no better than"
			var decimals := _decimals_apart(accuracy, baseline)
			text += " · right %s, %s “no change” (%s)" % [_percent(accuracy, decimals), verdict, _percent(baseline, decimals)]
		elif _is_number(accuracy):
			text += " · right %s of the time" % _percent(accuracy)
		return text
	if _too_little_history():
		return "No forecast for this bus yet (needs %s of history)" % _duration_text(data.min_history)
	return "No forecast for this bus at this time"

## Footer tooltip: what the scores mean, the error in steps and, for a calibrated forecast, what the calibration did
## (numbers from this replay's forecast)
func _metrics_text() -> String:
	var lines := PackedStringArray()
	var calibrated: bool = not data.metrics_raw.is_empty()
	# Without the raw scores, the scores shown are those of model_name as stored (calibrated or not)
	var model: String = data.model_name.trim_suffix(" + calibration") if calibrated else data.model_name
	lines.append("Forecast precomputed with %s%s; scores over the whole replay." % [model, ", then calibrated" if calibrated else ""])
	lines.append("“Right”: the median rounded to a status is the observed status. “No change” repeats the last observed status.")
	if calibrated:
		var raw_accuracy = data.metrics_raw.get("accuracy")
		var raw_coverage = data.metrics_raw.get("coverage")
		var accuracy = data.metrics.get("accuracy")
		var coverage = data.metrics.get("coverage")
		if _is_number(raw_accuracy) and _is_number(raw_coverage) and _is_number(accuracy) and _is_number(coverage):
			var decimals := _decimals_apart(raw_accuracy, accuracy)
			lines.append("Raw %s: right %s, its %d%% range held %s. Calibrated: right %s, the range holds %s." % [model,
				_percent(raw_accuracy, decimals), data.range_percent(), _percent(raw_coverage), _percent(accuracy, decimals), _percent(coverage)])
	else:
		var coverage = data.metrics.get("coverage")
		if _is_number(coverage):
			lines.append("The %d%% range holds %s of the observed statuses." % [data.range_percent(), _percent(coverage)])
	var mae = data.metrics.get("mae")
	var mae_baseline = data.metrics.get("mae_last_value")
	if _is_number(mae) and _is_number(mae_baseline):
		var digits := "%.3f" if "%.2f" % mae == "%.2f" % mae_baseline else "%.2f" # 0.295 vs 0.290, not 0.29 vs 0.29
		lines.append(("Average error: " + digits + " steps between statuses (“no change”: " + digits + ").") % [mae, mae_baseline])
	var at_horizon = data.metrics.get("accuracy_at_horizon")
	var baseline_at_horizon = data.metrics.get("accuracy_last_value_at_horizon")
	if _is_number(at_horizon) and _is_number(baseline_at_horizon):
		var decimals := _decimals_apart(at_horizon, baseline_at_horizon)
		lines.append("%s ahead: right %s (“no change”: %s)." % [_duration_text(data.horizon), _percent(at_horizon, decimals), _percent(baseline_at_horizon, decimals)])
	lines.append_array(_calibration_lines())
	return "\n".join(lines)

## What the calibration learned, for students: it only looks back, how much it trusts the model's predicted changes
func _calibration_lines() -> PackedStringArray:
	var lines := PackedStringArray()
	if data.calibration.is_empty():
		return lines
	var final: Array = data.calibration["final"]
	var last = final[final.size() - 1]
	if last is Dictionary:
		lines.append("The calibration only looks back: before each forecast it learns from the earlier forecasts whose outcome is known (by the end, %s at %s ahead, from %d buses)." % [_thousands(int(last["examples"])), _duration_text(data.horizon), int(last["buses"])])
	# Its trust in the model's predicted changes: the highest weight it reached, and where it ended at the horizon
	var history: Dictionary = data.calibration["history"]
	var best := -1.0
	var best_point := 0
	var best_time := 0.0
	for row_index in history["w"].size():
		var row: Array = history["w"][row_index]
		for point in row.size():
			if row[point] != null and float(row[point]) > best:
				best = float(row[point])
				best_point = point
				best_time = float(history["t"][row_index])
	if best >= 0.0 and last is Dictionary:
		var final_weight := float(last["w"])
		# Why the weight ended where it did: read from the weight, not assumed from the bundled hour
		var reason := "most buses keep their status" if final_weight < 0.5 else "its predicted changes mostly happen"
		lines.append("It trusts %s's predicted changes up to %s (%s ahead, %d min into the replay); by the end, %s at %s ahead: %s." % [
			data.model_name.trim_suffix(" + calibration"), _percent(best), _duration_text(data.point_every * (best_point + 1)),
			roundi(best_time / 60.0), _percent(final_weight), _duration_text(data.horizon), reason])
	lines.append("Its range is widened until each end held 90% of past outcomes, so that it holds at least 8 outcomes in 10.")
	return lines

## "74%", or "74.0%" with decimals = 1 (x.5 rounds up, like the forecast tool)
static func _percent(value, decimals := 0) -> String:
	var scale := pow(10.0, decimals)
	return ("%." + str(decimals) + "f%%") % (floorf(float(value) * 100.0 * scale + 0.5) / scale)

## "4,382"
static func _thousands(value: int) -> String:
	var digits := str(absi(value))
	var text := ""
	while digits.length() > 3:
		text = "," + digits.right(3) + text
		digits = digits.left(digits.length() - 3)
	return ("-" if value < 0 else "") + digits + text

## 1 decimal when two shares round to the same whole percent (74.0% vs 74.3%, not 74% vs 74%), else 0
static func _decimals_apart(a, b) -> int:
	return 1 if _percent(a) == _percent(b) else 0

static func _is_number(value) -> bool:
	return value is float or value is int

## True when the bus has been observed for less than the forecast's minimum history
func _too_little_history() -> bool:
	var first_known: float = data.first_known_time(vehicle_id, _now)
	return first_known < 0.0 or _now - first_known < data.min_history

## "10 min", "2.5 min", "30 s"
static func _duration_text(seconds: float) -> String:
	if seconds >= 60.0 and is_equal_approx(fmod(seconds, 60.0), 0.0):
		return "%d min" % roundi(seconds / 60.0)
	if seconds >= 60.0 and is_equal_approx(fmod(seconds, 30.0), 0.0):
		return "%.1f min" % (seconds / 60.0)
	return "%d s" % roundi(seconds)

## Snaps the hovered x to the nearest observation or forecast point and fills the tooltip
func _update_hover() -> void:
	_hover_time = -1.0
	if _hover_x < 0.0 or vehicle_id == "":
		_tooltip.visible = false
		return
	var best_distance := SNAP_PIXELS
	for candidates in [_observed_times, _actual_times, _fc_times]:
		for t in candidates:
			var distance := absf(_x_of(t) - _hover_x)
			if t >= _x_start and t <= _x_end and distance <= best_distance:
				best_distance = distance
				_hover_time = t
	if _hover_time < 0.0:
		_tooltip.visible = false
		return

	var first_line := _clock(_hover_time, true)
	var observed := _observed_status_at(_hover_time)
	if observed != -2:
		first_line += " · %s: %s" % ["Observed" if _hover_time <= _now + TIME_EPSILON else "Actual", Occupancy.label(observed)]
	var second_line := ""
	var forecast := _forecast_at_time(_hover_time)
	if not forecast.is_empty():
		var likely := Occupancy.label(data.status_of_rank(forecast["median"]))
		var low := Occupancy.label(data.status_of_rank(forecast["lo"]))
		var high := Occupancy.label(data.status_of_rank(forecast["hi"]))
		var range_text := low if low == high else "%s to %s" % [low, high]
		second_line = "Forecast: likely %s (%d%% range: %s)" % [likely, data.range_percent(), range_text]
	_tip_first.text = first_line
	_tip_second.text = second_line
	_tip_second.visible = second_line != ""

	# Above the plot, over the header and status line (it never covers a series, and the "now" label stays
	# visible): right of the crosshair, else left of it, kept inside the chart (inside the window if it is wider)
	_tooltip.visible = true
	_tooltip.reset_size()
	var tip_size := _tooltip.get_combined_minimum_size()
	var area_origin := _area.global_position - global_position
	var crosshair_x := area_origin.x + _x_of(_hover_time)
	var left := _chart.position.x + PADDING
	var right := _chart.position.x + _chart.size.x - PADDING
	if tip_size.x > right - left:
		left = MARGIN
		right = size.x - MARGIN
	var x := crosshair_x + 10.0
	if x + tip_size.x > right:
		x = crosshair_x - 10.0 - tip_size.x
	x = clampf(x, left, maxf(right - tip_size.x, left))
	var y := clampf(area_origin.y - PLOT_TOP - 2.0 - tip_size.y, 0.0, maxf(size.y - tip_size.y, 0.0))
	_tooltip.position = Vector2(x, y)

## Observed status at a time (step function), Occupancy.NO_DATA for a "no data" observation, -2 when
## there is nothing to show (no observation close enough, or after now without "Show actual")
func _observed_status_at(unix_time: float) -> int:
	var after_now := unix_time > _now + TIME_EPSILON
	var times: PackedFloat64Array = _actual_times if after_now else _observed_times
	var statuses: PackedInt32Array = _actual_statuses if after_now else _observed_statuses
	var index := times.bsearch(unix_time + TIME_EPSILON, false) - 1
	if index < 0 or unix_time - times[index] > GAP_SECONDS:
		return -2
	return statuses[index]

## Forecast median and range at a time (linear between the anchors), as ranks, {} outside the forecast
func _forecast_at_time(unix_time: float) -> Dictionary:
	if _fc_times.is_empty() or unix_time < _fc_times[0] - TIME_EPSILON or unix_time > _fc_times[_fc_times.size() - 1] + TIME_EPSILON:
		return {}
	var index := clampi(_fc_times.bsearch(unix_time, false) - 1, 0, _fc_times.size() - 1)
	if index == _fc_times.size() - 1:
		return {"lo": _fc_lo[index], "median": _fc_median[index], "hi": _fc_hi[index]}
	var weight := clampf((unix_time - _fc_times[index]) / (_fc_times[index + 1] - _fc_times[index]), 0.0, 1.0)
	return {"lo": lerpf(_fc_lo[index], _fc_lo[index + 1], weight), "median": lerpf(_fc_median[index], _fc_median[index + 1], weight),
		"hi": lerpf(_fc_hi[index], _fc_hi[index + 1], weight)}

func _on_plot_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion:
		var position_in_area: Vector2 = event.position - _area.position
		var inside := position_in_area.x >= 0.0 and position_in_area.x <= _area.size.x and position_in_area.y >= -PLOT_TOP and position_in_area.y <= _area.size.y
		_hover_x = position_in_area.x if inside else -1.0
		_update_hover()
		_area.queue_redraw()

func _on_plot_mouse_exited() -> void:
	_hover_x = -1.0
	_update_hover()
	_area.queue_redraw()

## Row labels, gridlines, time axis and the "now" line (the series area is drawn on top)
func _draw_axes() -> void:
	if vehicle_id == "":
		return
	var font := _plot.get_theme_font("font", "Label")
	var ascent := font.get_ascent(AXIS_FONT_SIZE)
	var text_middle := (ascent - font.get_descent(AXIS_FONT_SIZE)) * 0.5
	var rect := Rect2(_area.position, _area.size)
	for row in _rows.size():
		var y := roundf(rect.position.y + _y_of(row)) + 0.5
		_plot.draw_line(Vector2(rect.position.x, y), Vector2(rect.end.x, y), GRID_COLOR, 1.0)
		_plot.draw_string(font, Vector2(0, y + text_middle), Occupancy.label(_rows[row]), HORIZONTAL_ALIGNMENT_RIGHT, rect.position.x - 6.0, AXIS_FONT_SIZE, INK_MUTED)
	_plot.draw_line(Vector2(rect.position.x, rect.end.y), Vector2(rect.end.x, rect.end.y), AXIS_COLOR, 1.0)

	# Ticks every 5 minutes of local time
	var tick := ceilf((_x_start + _utc_offset) / TICK_SECONDS) * TICK_SECONDS - _utc_offset
	while tick <= _x_end + TIME_EPSILON:
		var x := roundf(rect.position.x + _x_of(tick)) + 0.5
		_plot.draw_line(Vector2(x, rect.end.y), Vector2(x, rect.end.y + 3.0), AXIS_COLOR, 1.0)
		var text := _clock(tick)
		var width := font.get_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, -1, AXIS_FONT_SIZE).x
		var text_x := clampf(x - width * 0.5, rect.position.x - 4.0, _plot.size.x - width)
		_plot.draw_string(font, Vector2(text_x, rect.end.y + 3.0 + ascent), text, HORIZONTAL_ALIGNMENT_LEFT, -1, AXIS_FONT_SIZE, INK_MUTED)
		tick += TICK_SECONDS

	var now_x := roundf(rect.position.x + _x_of(_now)) + 0.5
	_plot.draw_line(Vector2(now_x, rect.position.y), Vector2(now_x, rect.end.y), INK_MUTED, 1.0)
	var now_width := font.get_string_size("now", HORIZONTAL_ALIGNMENT_LEFT, -1, AXIS_FONT_SIZE).x
	_plot.draw_string(font, Vector2(now_x - now_width * 0.5, rect.position.y - 4.0), "now", HORIZONTAL_ALIGNMENT_LEFT, -1, AXIS_FONT_SIZE, INK_MUTED)

## Forecast range, observed (solid), forecast median (dashed) and actual (dotted) lines, hover marks
func _draw_series() -> void:
	if vehicle_id == "":
		return
	# Range: one quad per segment (a triangle from the zero-width origin); no polygon triangulation
	var band_color := Color(SERIES_COLOR, BAND_ALPHA)
	for i in _fc_times.size() - 1:
		var xa := _x_of(_fc_times[i])
		var xb := _x_of(_fc_times[i + 1])
		var lo_a := _y_of(_row_of_rank(_fc_lo[i]))
		var hi_a := _y_of(_row_of_rank(_fc_hi[i]))
		var lo_b := _y_of(_row_of_rank(_fc_lo[i + 1]))
		var hi_b := _y_of(_row_of_rank(_fc_hi[i + 1]))
		if lo_a - hi_a < 0.25 and lo_b - hi_b < 0.25:
			continue
		var points := PackedVector2Array([Vector2(xa, lo_a), Vector2(xb, lo_b), Vector2(xb, hi_b)])
		if lo_a - hi_a >= 0.25:
			points.append(Vector2(xa, hi_a))
		var colors := PackedColorArray()
		colors.resize(points.size())
		colors.fill(band_color)
		_area.draw_primitive(points, colors, PackedVector2Array())

	for run in _step_runs(_observed_times, _observed_statuses, _x_start, _now):
		_area.draw_polyline(run, SERIES_COLOR, 2.0)
	if _fc_times.size() >= 2:
		var median := PackedVector2Array()
		for i in _fc_times.size():
			median.append(Vector2(_x_of(_fc_times[i]), _y_of(_row_of_rank(_fc_median[i]))))
		_draw_dashed(_area, median, SERIES_COLOR, 2.0, 5.0, 3.0)
	# Actual on top of the median: where they agree it shows as dots on the dashes
	for run in _step_runs(_actual_times, _actual_statuses, _now, _x_end):
		_draw_dashed(_area, run, INK_SECONDARY, 2.0, 2.0, 3.0)

	if _future_note != "":
		var font := _area.get_theme_font("font", "Label")
		var x0 := _x_of(_now) + 6.0
		var width := _area.size.x - x0 - 4.0
		if width > 40.0:
			var baseline := _area.size.y * 0.5 + (font.get_ascent(AXIS_FONT_SIZE) - font.get_descent(AXIS_FONT_SIZE)) * 0.5
			_area.draw_string(font, Vector2(x0, baseline), _future_note, HORIZONTAL_ALIGNMENT_CENTER, width, AXIS_FONT_SIZE, INK_SECONDARY)
	if _calibration_note != "":
		var font := _area.get_theme_font("font", "Label")
		_area.draw_string(font, Vector2(0, font.get_ascent(AXIS_FONT_SIZE) + 2.0), _calibration_note, HORIZONTAL_ALIGNMENT_RIGHT,
			_area.size.x - 4.0, AXIS_FONT_SIZE, INK_MUTED)

	if _hover_time >= 0.0:
		var x := roundf(_x_of(_hover_time)) + 0.5
		_area.draw_line(Vector2(x, 0), Vector2(x, _area.size.y), Color(INK_SECONDARY, 0.6), 1.0)
		var observed_row := _row_of_status(_observed_status_at(_hover_time))
		if observed_row >= 0:
			_draw_dot(Vector2(x, _y_of(observed_row)), SERIES_COLOR if _hover_time <= _now + TIME_EPSILON else INK_SECONDARY)
		var forecast := _forecast_at_time(_hover_time)
		if not forecast.is_empty():
			_draw_dot(Vector2(x, _y_of(_row_of_rank(forecast["median"]))), SERIES_COLOR)

func _draw_dot(center: Vector2, color: Color) -> void:
	_area.draw_circle(center, 6.0, RING_COLOR)
	_area.draw_circle(center, 4.0, color)

## Polylines (one per unbroken run) of the step line through the observations, between two times.
## A run breaks at observations without a row (no data, not accepting passengers...) and at gaps longer than
## GAP_SECONDS (where the status is drawn for GAP_SECONDS after the last observation, as for the latest one)
func _step_runs(times: PackedFloat64Array, statuses: PackedInt32Array, from_time: float, to_time: float) -> Array[PackedVector2Array]:
	var runs: Array[PackedVector2Array] = []
	var run := PackedVector2Array()
	for i in times.size():
		var is_last := i + 1 >= times.size()
		var connected := not is_last and times[i + 1] - times[i] <= GAP_SECONDS
		var start := maxf(times[i], from_time)
		var end := minf(times[i + 1] if connected else times[i] + GAP_SECONDS, to_time)
		var row := _row_of_status(statuses[i])
		if row >= 0 and end >= start:
			var y := _y_of(row)
			run.append(Vector2(_x_of(start), y))
			if end > start:
				run.append(Vector2(_x_of(end), y))
		if row < 0 or not connected or end >= to_time:
			if run.size() >= 2:
				runs.append(run)
			run = PackedVector2Array()
			if end >= to_time:
				break
	if run.size() >= 2:
		runs.append(run)
	return runs

## Dashed polyline whose dash pattern runs on across the vertices (short segments stay dashed)
static func _draw_dashed(canvas: CanvasItem, points: PackedVector2Array, color: Color, width: float, dash: float, gap: float) -> void:
	var drawing := true
	var left := dash
	for i in points.size() - 1:
		var a := points[i]
		var b := points[i + 1]
		var length := a.distance_to(b)
		var done := 0.0
		while done < length:
			var piece := minf(left, length - done)
			if drawing:
				canvas.draw_line(a.lerp(b, done / length), a.lerp(b, (done + piece) / length), color, width, true)
			done += piece
			left -= piece
			if left <= 0.0:
				drawing = not drawing
				left = dash if drawing else gap
