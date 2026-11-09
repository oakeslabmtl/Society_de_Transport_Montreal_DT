import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo
from fixtures import SOFTWARE_DIR, T0, TempDirTestCase, entity, make_recording, run_cli, typed
from recording import RecordingError
from recording.cli import main
from recording.store import Poll
from recording.timeline import best_windows, parse_local_time, select_window, start_option

TZ = "America/Montreal"


def local(*args) -> float:
    """Unix time of a local date/time in Montreal, e.g. local(2025, 3, 1, 15, 0)."""
    return datetime(*args, tzinfo=ZoneInfo(TZ)).timestamp()


def polls(start:float, minutes:float, vehicles:int, interval:float=15.0, first_id:int=1) -> list[Poll]:
    count = int(minutes * 60 / interval) + 1
    return [Poll(first_id + i, start + i * interval, None, vehicles, "ok", None) for i in range(count)]


class WindowSelectionTest(unittest.TestCase):

    def setUp(self):
        # 09:00-09:50 very busy, then a 63-minute gap, then 10:53-12:53 quieter but gap-free.
        # Ignoring the gap (or accepting a window that stops early) would pick the busy morning.
        self.morning = polls(T0 - 6 * 3600, 50, vehicles=40)
        self.afternoon_start = self.morning[-1].poll_time + 63 * 60
        self.afternoon = polls(self.afternoon_start, 120, vehicles=10, first_id=1000)
        self.afternoon[200].vehicle_count = 25     # 50 min after 10:53: a slightly busier moment

    def test_best_window_avoids_the_gap(self):
        best = best_windows(self.morning + self.afternoon, 60, 15)[0]
        self.assertGreaterEqual(best.start_time, self.afternoon_start)
        self.assertAlmostEqual(best.duration, 3600, delta=15)
        self.assertEqual(len(best.polls), 241)
        self.assertIn(self.afternoon[200], best.polls)
        self.assertEqual(best.start_time, self.afternoon_start)    # ties -> earliest

    def test_several_best_windows_do_not_overlap(self):
        windows = best_windows(self.afternoon, 30, 15, limit=3)
        self.assertEqual(len(windows), 3)
        for a in windows:
            for b in windows:
                if a is not b:
                    self.assertTrue(a.end_time < b.start_time or b.end_time < a.start_time)

    def test_no_window_long_enough_is_a_friendly_error(self):
        with self.assertRaisesRegex(RecordingError, r"longest one is .*09:00 -> 09:50 \(50 min\)"):
            select_window(self.morning, 15, TZ, minutes=60)

    def test_start_and_minutes_rules(self):
        all_polls = self.morning + self.afternoon
        window, warnings = select_window(all_polls, 15, TZ)
        self.assertEqual(len(window.polls), len(all_polls))
        start = parse_local_time("09:10", TZ, all_polls)
        window, warnings = select_window(all_polls, 15, TZ, start=start, minutes=10)
        self.assertEqual((window.start_time, window.duration, warnings), (start, 600, []))
        window, warnings = select_window(all_polls, 15, TZ, start=start, minutes=120)
        self.assertTrue(any("gap" in w for w in warnings))
        window, warnings = select_window(all_polls, 15, TZ, start=parse_local_time("2025-03-01 12:00", TZ, all_polls))
        self.assertEqual((window.end_time, warnings), (all_polls[-1].poll_time, []))

    def test_whole_recording_export_warns_about_gaps(self):
        _, warnings = select_window(self.morning + self.afternoon, 15, TZ)
        self.assertEqual(warnings, ["The window contains 1 gap(s) in the data (longest: 09:50 -> 10:53, 1 h 03 min); "
                                    "buses will freeze or disappear there."])
        self.assertEqual(select_window(self.afternoon, 15, TZ)[1], [])

    def test_window_edges_outside_the_data_are_warned(self):
        all_polls = self.morning + self.afternoon
        cases = {  # --start, --minutes -> expected warning (the morning is 09:00-09:50, the afternoon 10:53-12:53)
            ("08:30", 60): "The window starts before the recording (no data 08:30 -> 09:00); the replay is shorter than "
                           "requested: Sat 2025-03-01 09:00 -> 09:30 (30 min).",
            ("10:00", 60): "The window starts in a gap (no data 10:00 -> 10:53); the replay is shorter than requested: "
                           "Sat 2025-03-01 10:53 -> 11:00 (7 min).",
            ("09:40", 30): "The window ends in a gap (no data 09:50 -> 10:10); the replay is shorter than requested: "
                           "Sat 2025-03-01 09:40 -> 09:50 (10 min).",
            ("12:30", 60): "The window goes past the end of the recording (no data after 12:53); the replay is shorter "
                           "than requested: Sat 2025-03-01 12:30 -> 12:53 (23 min).",
            ("09:10", 10): None,
            ("11:00", 60): None,
        }
        for (start, minutes), expected in cases.items():
            with self.subTest(start=start, minutes=minutes):
                _, warnings = select_window(all_polls, 15, TZ, parse_local_time(start, TZ, all_polls), minutes)
                self.assertEqual(warnings, [expected] if expected else [])
        late_start = polls(local(2025, 3, 1, 11, 3, 39), 30, vehicles=5)      # seconds are shown when HH:MM is the same
        _, warnings = select_window(late_start, 15, TZ, parse_local_time("11:03", TZ, late_start), 10)
        self.assertIn("starts before the recording (no data 11:03:00 -> 11:03:39)", warnings[0])

    def test_daylight_saving_change_inside_the_window_is_warned(self):
        night = polls(local(2025, 11, 2, 0, 30), 120, vehicles=5)    # 00:30 EDT -> 01:30 EST (clocks go back at 02:00)
        for minutes in (None, 100):
            with self.subTest(minutes=minutes):
                _, warnings = select_window(night, 15, TZ, minutes=minutes)
                self.assertEqual(warnings, ["The window crosses a daylight saving time change: from 01:00 on, the clock "
                                            "shown by the viewer is 1 h off (bus movements are not affected)."])
        self.assertEqual(select_window(night, 15, TZ, minutes=60)[1], [])          # 00:30-01:30 EDT, before the change

    def test_suggested_start_names_the_day_when_the_recording_spans_several_days(self):
        self.assertEqual(start_option(T0, TZ, polls(T0 - 3600, 120, 1)), "15:00")
        late_night = polls(local(2025, 10, 27, 22, 0), 240, 1)                      # Mon 22:00 -> Tue 02:00
        self.assertEqual(start_option(local(2025, 10, 28, 0, 30), TZ, late_night), '"2025-10-28 00:30"')
        # Mon 14:00 -> Tue 16:00: "15:00" alone means Monday, but the busy hour is on Tuesday
        two_days = polls(local(2025, 10, 27, 14, 0), 26 * 60, 5, interval=60)
        busy = local(2025, 10, 28, 15, 0)
        option = start_option(busy, TZ, two_days)
        self.assertEqual(option, '"2025-10-28 15:00"')
        self.assertEqual(parse_local_time(option.strip('"'), TZ, two_days), busy)
        self.assertEqual(parse_local_time("15:00", TZ, two_days), local(2025, 10, 27, 15, 0))

    def test_local_time_parsing(self):
        self.assertEqual(parse_local_time("15:00", TZ, polls(T0 - 3600, 120, 1)), T0)
        self.assertEqual(parse_local_time("2025-03-01 15:00:00", TZ, polls(T0, 1, 1)), T0)
        late_night = polls(T0 + 8 * 3600, 120, 1)     # 23:00 -> 01:00
        self.assertEqual(parse_local_time("00:30", TZ, late_night), T0 + 9.5 * 3600)
        with self.assertRaisesRegex(RecordingError, "HH:MM"):
            parse_local_time("half past three", TZ, late_night)


class ReplayExportTest(TempDirTestCase):

    def setUp(self):
        super().setUp()
        recorded = []
        for i in range(9):
            poll_time = T0 + 15 * i
            if i == 4:
                recorded.append((poll_time, "cannot reach the STM API (no network?)"))
                continue
            data = [entity("41053", "121", poll_time - 4, latitude=45.5 + i / 1000),
                    entity("31841", "45", poll_time - 2, **{"vehicle.trip.direction_id": 1})]
            del data[1]["vehicle.position.bearing"]
            recorded.append((poll_time, data))
        self.recording = make_recording(self.tmp / "day.sqlite", recorded)

    def export(self, *args) -> tuple[dict, str, bytes]:
        out = self.tmp / "replay.json"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(["export", str(self.recording), "--out", str(out), *args])
        self.assertEqual(code, 0)
        raw = out.read_bytes()
        return json.loads(raw.decode("utf-8")), stdout.getvalue(), raw

    def test_replay_shape(self):
        replay, stdout, raw = self.export("--name", "Saturday · test")
        self.assertEqual((replay["format"], replay["version"], replay["name"]), ("stm-dt-replay", 1, "Saturday · test"))
        self.assertEqual(replay["source"], {"type": "csv-import", "detail": "tests"})
        self.assertEqual((replay["timezone"], replay["utc_offset_seconds"]), (TZ, -18000))
        self.assertEqual((replay["start_time"], replay["end_time"], replay["duration"]), (T0, T0 + 120, 120))
        self.assertEqual((replay["poll_interval"], replay["routes"], replay["vehicle_count"]), (15.0, [45, 121], 2))
        self.assertTrue(replay["created_at"].endswith("Z"))

        times = [frame["t"] for frame in replay["frames"]]
        self.assertEqual(times, [0, 15, 30, 45, 75, 90, 105, 120])   # the failed poll (t=60) is a gap, not a frame
        first = replay["frames"][0]["data"]
        self.assertEqual([e["vehicle.vehicle.id"] for e in first], ["31841", "41053"])   # newest first, like live
        expected = entity("31841", "45", T0 - 2, **{"vehicle.trip.direction_id": 1})
        del expected["vehicle.position.bearing"]
        self.assertEqual(typed(first[0]), typed(expected))
        self.assertEqual(raw, json.dumps(replay, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        self.assertIn("Next steps", stdout)

    def test_best_window_and_route_filter(self):
        replay, _, _ = self.export("--minutes", "1", "--routes", "45")
        self.assertEqual(replay["routes"], [45])
        self.assertEqual(replay["frames"][0]["t"], 0)
        self.assertAlmostEqual(replay["duration"], 60, delta=15)
        self.assertTrue(all(e["vehicle.trip.route_id"] == "45" for f in replay["frames"] for e in f["data"]))

    def test_start_window(self):
        replay, _, _ = self.export("--start", "15:01", "--minutes", "0.5")
        self.assertEqual((replay["start_time"], len(replay["frames"])), (T0 + 60 + 15, 2))

    def test_default_name_and_output_folder(self):
        replays_dir = self.tmp / "replays"
        with mock.patch("recording.cli.REPLAYS_DIR", replays_dir), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["export", str(self.recording)]), 0)
        files = list(replays_dir.glob("*.json"))
        self.assertEqual([f.name for f in files], ["day_1500-1502.json"])
        name = json.loads(files[0].read_text(encoding="utf-8"))["name"]
        self.assertEqual(name, "Sat 1 Mar 2025, 15:00-15:02 · routes 45, 121")

    def test_export_replaces_an_existing_file_only_with_force(self):
        _, _, first = self.export("--name", "First")
        out = self.tmp / "replay.json"
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["export", str(self.recording), "--out", str(out), "--name", "Second"]), 2)
        self.assertIn("--force", stderr.getvalue())
        self.assertEqual(out.read_bytes(), first)
        replay, stdout, _ = self.export("--name", "Second", "--force")
        self.assertEqual(replay["name"], "Second")
        self.assertIn("replaced the previous file", stdout)

    def test_info_and_export_work_without_stm_api_key(self):
        environment = {k: v for k, v in os.environ.items() if k != "STM_API_KEY"}
        with mock.patch.dict(os.environ, environment, clear=True):
            replay, _, _ = self.export()
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(["info", str(self.recording), "--minutes", "1"]), 0)
        self.assertEqual(len(replay["frames"]), 8)
        text = stdout.getvalue()
        for expected in ("Routes      45, 121", "9 (8 ok, 1 failed)", "cannot reach the STM API", "1 MANY_SEATS_AVAILABLE",
                         "Best 1-min windows", "export"):
            self.assertIn(expected, text)

    def test_export_errors_are_friendly(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["export", str(self.tmp / "missing.sqlite")]), 2)
            self.assertEqual(main(["export", str(self.recording), "--routes", "999"]), 2)
            self.assertEqual(main(["export", str(self.recording), "--start", "08:00", "--minutes", "5"]), 2)
            self.assertEqual(main(["export", str(self.recording), "--out", str(self.tmp / "replay.txt")]), 2)
            self.assertEqual(main(["info", str(self.recording), "--minutes", "0"]), 2)
        self.assertEqual(stderr.getvalue().count("error: "), 5)
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_a_failed_write_is_friendly_and_leaves_no_temporary_file(self):
        out = self.tmp / "replay.json"
        out.mkdir()         # cannot be replaced by a file: PermissionError on Windows, IsADirectoryError elsewhere
        code, _, stderr = run_cli("export", str(self.recording), "--out", str(out), "--force")
        self.assertEqual(code, 2)
        self.assertIn("error: Could not write", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertFalse((self.tmp / "replay.json.tmp").exists())


class CliOutputTest(TempDirTestCase):

    def test_info_suggests_a_dated_start_for_a_recording_over_several_days(self):
        recorded, poll_time = [], local(2025, 10, 27, 14, 0)
        while poll_time <= local(2025, 10, 28, 16, 0):
            buses = 3 if poll_time >= local(2025, 10, 28, 15, 0) else 1     # the busy hour is on the second day
            recorded.append((poll_time, [entity(str(v), "24", int(poll_time)) for v in range(buses)]))
            poll_time += 900
        path = make_recording(self.tmp / "two_days.sqlite", recorded, interval=900, route_ids=["24"])
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["info", str(path)]), 0)
        self.assertIn("1. Tue 2025-10-28 15:00 -> 16:00", stdout.getvalue())
        self.assertIn('--start "2025-10-28 15:00" --minutes 60', stdout.getvalue())

    def test_errors_are_utf8_when_stderr_is_redirected(self):
        # Without a console, Windows would write stderr in its ANSI code page (é as one byte, ж as ж)
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with open(self.tmp / "stderr.txt", "wb") as stderr:
            run = subprocess.run([sys.executable, str(SOFTWARE_DIR / "recording"), "info", str(self.tmp / "missing_é_ж.sqlite")],
                                 stdout=subprocess.DEVNULL, stderr=stderr, env=env, timeout=60)
        self.assertEqual(run.returncode, 2)
        text = (self.tmp / "stderr.txt").read_bytes().decode("utf-8")
        self.assertIn("error: Recording not found:", text)
        self.assertIn("missing_é_ж.sqlite", text)


if __name__ == '__main__':
    unittest.main()
