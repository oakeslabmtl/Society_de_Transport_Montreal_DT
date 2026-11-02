extends Control

## Replay transport bar (built in code), anchored to the bottom of the screen: one row (recording, Restart, Play, speed,
## timeline, then the recording's local time and the position in it), and a second one only while there is a message.
## In live mode only the LIVE badge is shown.

signal replay_selected(path: String)
signal layout_changed() # The bar's size changed (the settings panel stays above it)

const SPEEDS := [1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
const PANEL_COLOR := Color(0.08, 0.08, 0.08, 0.66)
const LIVE_COLOR := Color(0.1, 0.6, 0.25)
const ERROR_COLOR := Color(1.0, 0.45, 0.4)
const INFO_COLOR := Color(0.85, 0.85, 0.85)
const MARGIN := 16
const TIME_FONT_SIZE := 13
const TIME_SAMPLE := "Wed 0000-00-00 00:00:00 · 00:00 / 00:00" # Widest clock text: the timeline does not jitter
const SHORT_TIME_SAMPLE := "00:00:00 · 00:00 / 00:00" # Without the date, when the row would not fit the window

var player: Node = null
var live := false

var _bar: PanelContainer
var _badge: Label
var _replay_list: OptionButton
var _restart_button: Button
var _play_button: Button
var _speed_list: OptionButton
var _timeline: HSlider
var _time_label: Label
var _message_label: Label
var _replay_widgets: Array[Control] = []
var _paths: Array[String] = []
var _dragging := false
var _pending_seek := -1.0 # Timeline value to seek to while dragging (applied once per frame)
var _clock_widths := Vector2.ZERO # Of TIME_SAMPLE and SHORT_TIME_SAMPLE
var _short_clock := false

func _init() -> void:
	name = "ReplayControls"
	mouse_filter = MOUSE_FILTER_IGNORE
	_build()

func _ready() -> void:
	set_anchors_and_offsets_preset(PRESET_FULL_RECT)
	var font := _time_label.get_theme_font("font")
	for i in 2:
		_clock_widths[i] = ceilf(font.get_string_size([TIME_SAMPLE, SHORT_TIME_SAMPLE][i], HORIZONTAL_ALIGNMENT_LEFT, -1, TIME_FONT_SIZE).x)
	resized.connect(_relayout)
	_relayout()

func setup(replay_player: Node) -> void:
	player = replay_player
	player.state_changed.connect(_on_state_changed)
	player.replay_loaded.connect(_on_replay_loaded)
	_refresh_buttons()

func _process(_delta: float) -> void:
	if live or player == null or not player.has_frames():
		return
	if _pending_seek >= 0.0:
		player.seek(_pending_seek)
		_pending_seek = -1.0
	if not _dragging:
		_timeline.set_value_no_signal(player.get_display_time())
	_update_time_label()

func set_live(is_live: bool) -> void:
	live = is_live
	_badge.visible = live
	for widget in _replay_widgets:
		widget.visible = not live
	_message_label.visible = not live and _message_label.text != ""
	_relayout()

func set_replays(entries: Array, selected_path: String) -> void:
	_replay_list.clear()
	_paths.clear()
	for entry in entries:
		var label: String = entry["name"]
		if not entry["builtin"]:
			label += "  (file)"
		_replay_list.add_item(label)
		_replay_list.set_item_tooltip(_replay_list.item_count - 1, entry["path"])
		_paths.append(entry["path"])
	if _paths.is_empty():
		_replay_list.add_item("No replays found")
		_replay_list.set_item_disabled(0, true)
		_replay_list.disabled = true
	else:
		_replay_list.disabled = false
		# add_item selected the first entry: show nothing selected unless selected_path is listed,
		# otherwise picking that entry after a failed load would not emit item_selected
		_replay_list.select(-1)
		_select_path(selected_path)
		if _replay_list.selected < 0:
			_replay_list.text = "Choose a recording"

## Screen rectangle of the bar (self-test: the panels must not overlap it)
func bar_rect() -> Rect2:
	return _bar.get_global_rect()

## A message on a second row ("" removes the row)
func show_message(text: String, is_error := false) -> void:
	_message_label.text = text
	_message_label.tooltip_text = text
	_message_label.add_theme_color_override("font_color", ERROR_COLOR if is_error else INFO_COLOR)
	_message_label.visible = not live and text != ""
	_relayout()

func _build() -> void:
	_bar = PanelContainer.new()
	_bar.name = "Bar"
	# Clicks and scrolls on the bar (padding and labels included) must not reach the 3D view behind it
	_bar.mouse_filter = Control.MOUSE_FILTER_STOP
	_bar.mouse_force_pass_scroll_events = false
	var panel_style := StyleBoxFlat.new()
	panel_style.bg_color = PANEL_COLOR
	panel_style.set_content_margin_all(8)
	panel_style.set_corner_radius_all(4)
	_bar.add_theme_stylebox_override("panel", panel_style)
	add_child(_bar)

	var rows := VBoxContainer.new()
	_bar.add_child(rows)

	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 8)
	rows.add_child(row)

	# Live mode's only content
	_badge = Label.new()
	_badge.text = " LIVE "
	_badge.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	_badge.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	_badge.visible = false
	var badge_style := StyleBoxFlat.new()
	badge_style.bg_color = LIVE_COLOR
	badge_style.set_content_margin_all(4)
	badge_style.set_corner_radius_all(3)
	_badge.add_theme_stylebox_override("normal", badge_style)
	_badge.add_theme_color_override("font_color", Color.WHITE)
	row.add_child(_badge)

	# The replay list and the timeline share the free width (the bar must fit narrow windows); a long title ends in "…"
	_replay_list = OptionButton.new()
	_replay_list.fit_to_longest_item = false
	_replay_list.clip_text = true
	_replay_list.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_replay_list.custom_minimum_size.x = 160
	_replay_list.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_replay_list.tooltip_text = "Recording to play"
	_replay_list.item_selected.connect(_on_replay_list_selected)
	_add_replay_widget(row, _replay_list)

	_restart_button = Button.new()
	_restart_button.text = "Restart"
	_restart_button.pressed.connect(_on_restart_pressed)
	_add_replay_widget(row, _restart_button)

	_play_button = Button.new()
	_play_button.text = "Pause"
	_play_button.custom_minimum_size.x = 64
	_play_button.pressed.connect(_on_play_pressed)
	_add_replay_widget(row, _play_button)

	_speed_list = OptionButton.new()
	for x in SPEEDS:
		_speed_list.add_item("%dx" % int(x))
	_speed_list.tooltip_text = "Playback speed"
	_speed_list.item_selected.connect(_on_speed_selected)
	_add_replay_widget(row, _speed_list)

	_timeline = HSlider.new()
	_timeline.min_value = 0.0
	_timeline.max_value = 1.0
	_timeline.step = 0.0
	_timeline.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_timeline.size_flags_stretch_ratio = 1.4
	_timeline.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	_timeline.custom_minimum_size.x = 80
	_timeline.value_changed.connect(_on_timeline_changed)
	_timeline.drag_started.connect(_on_drag_started)
	_timeline.drag_ended.connect(_on_drag_ended)
	_add_replay_widget(row, _timeline)

	# The recording's local time and the position in it, at the end of the row
	_time_label = Label.new()
	_time_label.text = "--"
	_time_label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	_time_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	_time_label.add_theme_font_size_override("font_size", TIME_FONT_SIZE)
	_time_label.add_theme_color_override("font_color", INFO_COLOR)
	_add_replay_widget(row, _time_label)

	# Second row, only while there is a message. Single line (autowrap would make the bar's minimum height depend on its
	# width)
	_message_label = Label.new()
	_message_label.clip_text = true
	_message_label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_message_label.mouse_filter = Control.MOUSE_FILTER_PASS
	_message_label.visible = false
	rows.add_child(_message_label)

func _add_replay_widget(row: HBoxContainer, widget: Control) -> void:
	widget.focus_mode = Control.FOCUS_NONE
	row.add_child(widget)
	_replay_widgets.append(widget)

func _relayout() -> void:
	if not is_inside_tree():
		return
	# In a narrow window the clock drops the date, so the row still fits
	_time_label.custom_minimum_size.x = _clock_widths.x
	_short_clock = not live and _bar.get_combined_minimum_size().x > size.x - 2 * MARGIN
	if _short_clock:
		_time_label.custom_minimum_size.x = _clock_widths.y
	if player != null:
		_update_time_label()
	# Re-anchor after content changes so the bar keeps hugging the bottom edge
	_bar.reset_size()
	if live:
		_bar.set_anchors_and_offsets_preset(PRESET_BOTTOM_LEFT, PRESET_MODE_MINSIZE, MARGIN)
	else:
		_bar.set_anchors_and_offsets_preset(PRESET_BOTTOM_WIDE, PRESET_MODE_MINSIZE, MARGIN)
	_bar.grow_vertical = GROW_DIRECTION_BEGIN
	layout_changed.emit()

func _select_path(path: String) -> void:
	var index := _paths.find(path)
	if index >= 0:
		_replay_list.select(index)

func _update_time_label() -> void:
	var clock: String = player.get_clock_text()
	if _short_clock and clock.length() > 8:
		clock = clock.right(8) # The time of day
	_time_label.text = "%s · %s / %s" % [clock, player.format_mmss(player.get_display_time()), player.format_mmss(player.duration)]

func _refresh_buttons() -> void:
	var has_replay: bool = player != null and player.has_frames()
	_restart_button.disabled = not has_replay
	_play_button.disabled = not has_replay
	_timeline.editable = has_replay and player.duration > 0.0
	if player == null:
		return
	_play_button.text = "Pause" if player.playing else "Play"
	var best := 0
	for i in SPEEDS.size():
		if absf(SPEEDS[i] - player.speed) < absf(SPEEDS[best] - player.speed):
			best = i
	_speed_list.select(best)

func _on_state_changed() -> void:
	_refresh_buttons()

func _on_replay_loaded(_meta: Dictionary) -> void:
	# Reset first: a smaller max_value would clamp the old value and emit value_changed (= seek)
	_timeline.set_value_no_signal(0.0)
	_timeline.max_value = maxf(player.duration, 0.001)
	_pending_seek = -1.0
	_select_path(player.loaded_path)
	show_message("")
	_refresh_buttons()
	_update_time_label()

func _on_replay_list_selected(index: int) -> void:
	if index >= 0 and index < _paths.size():
		replay_selected.emit(_paths[index])

func _on_timeline_changed(value: float) -> void:
	if not player or not player.has_frames():
		return
	if _dragging:
		# A drag changes the value on every mouse motion: seek at most once per frame (in _process)
		_pending_seek = value
	else:
		player.seek(value)

func _on_drag_started() -> void:
	_dragging = true

func _on_drag_ended(_value_changed: bool) -> void:
	_dragging = false
	if _pending_seek >= 0.0 and player and player.has_frames():
		player.seek(_pending_seek)
	_pending_seek = -1.0

func _on_restart_pressed() -> void:
	if player:
		player.restart()

func _on_play_pressed() -> void:
	if player:
		player.toggle_playing()

func _on_speed_selected(index: int) -> void:
	if player and index >= 0 and index < SPEEDS.size():
		player.set_speed(SPEEDS[index])
