import contextlib
import io
import os
import re
import sqlite3
import unittest
from unittest import mock
import requests
from fixtures import T0, TempDirTestCase, entity, typed
from recording import RecordingError
from recording.cli import main, progress_line
from recording.recorder import describe_error, record
from recording.store import RecordingStore


class FakeTime:
    """Monotonic clock + sleep + wall clock that only move when told to: tests never really wait."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def wall(self):
        return T0 + self.now


class RecorderTest(TempDirTestCase):

    def setUp(self):
        super().setUp()
        self.time = FakeTime()
        self.store = RecordingStore.create(self.tmp / "rec.sqlite", {"source": "stm-api", "route_ids": ["45"],
                                                                      "poll_interval_s": 15})

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def fetcher(self, outcomes:list, durations:list=None):
        """Returns each outcome in turn (a list of entities, or an exception to raise), taking durations[i] seconds."""
        calls = iter(outcomes)
        durations = iter(durations or [])

        def fetch():
            self.time.now += next(durations, 0.5)
            outcome = next(calls)
            if isinstance(outcome, BaseException):
                raise outcome
            return T0, outcome
        return fetch

    def run_record(self, fetch, minutes=1.0, interval=15.0, **kwargs):
        return record(self.store, fetch, interval, minutes, clock=self.time.clock, sleep=self.time.sleep,
                      now=self.time.wall, **kwargs)

    def test_failed_fetch_becomes_an_error_poll_and_recording_continues(self):
        bus = entity("41053", "45", T0)
        seen = []
        fetch = self.fetcher([[bus], requests.ConnectionError("boom"), [bus], [bus], [bus]])
        result = self.run_record(fetch, on_poll=seen.append)

        polls = self.store.polls()
        self.assertEqual([p.poll_time - T0 for p in polls], [0, 15, 30, 45, 60])     # on schedule, no drift
        self.assertEqual([p.status for p in polls], ["ok", "error", "ok", "ok", "ok"])
        self.assertEqual(polls[1].error, "cannot reach the STM API (no network?)")
        self.assertEqual((result.polls, result.errors, result.vehicles, result.interrupted), (5, 1, {"41053"}, False))
        self.assertEqual([p.number for p in seen], [1, 2, 3, 4, 5])
        self.assertIn("FAILED", progress_line(seen[1], 1))
        self.assertIn("1 vehicles  45:1", progress_line(seen[0], 1))
        stored = self.store.entities_by_poll(polls)[polls[0].poll_id]
        self.assertEqual(typed(stored), typed([bus]))

    def test_slow_fetch_skips_missed_ticks_instead_of_drifting(self):
        with self.assertLogs("Recorder", "WARNING") as logs:
            self.run_record(self.fetcher([[]] * 10, durations=[20]))
        self.assertIn("skipped 1 poll", logs.output[0])
        self.assertEqual([p.poll_time - T0 for p in self.store.polls()], [0, 30, 45, 60])

    def test_ctrl_c_stops_cleanly_and_keeps_what_was_recorded(self):
        bus = entity("41053", "45", T0)
        result = self.run_record(self.fetcher([[bus], [bus], KeyboardInterrupt()]), minutes=None)
        self.assertTrue(result.interrupted)
        self.assertEqual(len(self.store.polls()), 2)

    def test_duplicate_vehicles_in_one_poll_keep_the_newest(self):
        self.run_record(self.fetcher([[entity("a", "45", T0 - 9, latitude=1.0), entity("a", "45", T0 - 3, latitude=2.0)]]),
                        minutes=0)
        poll = self.store.polls()[0]
        self.assertEqual(poll.vehicle_count, 1)
        self.assertEqual(self.store.entities_by_poll([poll])[poll.poll_id][0]["vehicle.position.latitude"], 2.0)

    def test_a_poll_that_cannot_be_saved_is_rolled_back_and_recording_continues(self):
        bus = entity("41053", "45", T0)
        add_poll, calls = self.store.add_poll, []

        def flaky_add_poll(poll_time, entities, feed_timestamp=None):
            calls.append(poll_time)
            if len(calls) == 2:     # its rows are inserted, then the commit fails (e.g. the file stayed locked)
                add_poll(poll_time, entities, feed_timestamp, commit=False)
                raise sqlite3.OperationalError("database is locked")
            return add_poll(poll_time, entities, feed_timestamp)

        seen = []
        with mock.patch.object(self.store, "add_poll", flaky_add_poll), self.assertLogs("Recorder", "WARNING") as logs:
            result = self.run_record(self.fetcher([[bus]] * 4), minutes=0.75, on_poll=seen.append)
        self.assertEqual((result.polls, result.errors, result.interrupted), (4, 1, False))
        # The failed poll's rows were rolled back, not committed with the next poll
        self.assertEqual([p.poll_time - T0 for p in self.store.polls()], [0, 30, 45])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM vehicle_positions").fetchone()[0], 3)
        self.assertIn("could not save this poll to rec.sqlite (OperationalError: database is locked)", seen[1].error)
        self.assertIn("FAILED: could not save", progress_line(seen[1], 1))
        self.assertIn("database is locked", logs.output[0])

    def test_recording_stops_when_polls_keep_failing_to_be_saved(self):
        bus = entity("41053", "45", T0)
        add_poll, full = self.store.add_poll, sqlite3.OperationalError("database or disk is full")

        def add_first_poll_only(poll_time, entities, feed_timestamp=None):
            if self.store.polls():
                raise full
            return add_poll(poll_time, entities, feed_timestamp)

        seen = []
        add_error_poll = mock.Mock(side_effect=full)
        with mock.patch.object(self.store, "add_poll", add_first_poll_only), \
                mock.patch.object(self.store, "add_error_poll", add_error_poll), self.assertLogs("Recorder", "WARNING"), \
                self.assertRaisesRegex(RecordingError, r"the last 3 polls could not be saved .*disk is full.* \(1 ok polls\)"):
            self.run_record(self.fetcher([[bus], requests.ConnectionError("boom"), [bus], [bus], [bus]]), minutes=1,
                            on_poll=seen.append)
        add_error_poll.assert_called_once()
        self.assertEqual(len(self.store.polls()), 1)        # what was saved before is kept
        self.assertEqual([p.number for p in seen], [1, 2, 3])


class RecorderErrorsTest(TempDirTestCase):

    def test_http_errors_are_described_without_secrets(self):
        response = requests.Response()
        response.status_code, response.reason = 403, "Forbidden"
        message = describe_error(requests.HTTPError(response=response))
        self.assertEqual(message, "HTTP 403 Forbidden (check that STM_API_KEY is valid)")
        self.assertEqual(describe_error(requests.Timeout()), "the STM API did not answer in time")

    def test_record_without_api_key_is_a_friendly_error(self):
        environment = {k: v for k, v in os.environ.items() if k != "STM_API_KEY"}
        out = self.tmp / "never.sqlite"
        with mock.patch.dict(os.environ, environment, clear=True), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["record", "--routes", "45", "--minutes", "1", "--out", str(out)]), 2)
        self.assertIn("STM_API_KEY is not set", stderr.getvalue())
        self.assertFalse(out.exists())

    def test_cli_record_prints_progress_and_can_export(self):
        fetch = lambda: (T0, [entity("41053", "45", T0), entity("31841", "121", T0 - 3)])
        out, replays = self.tmp / "rec.sqlite", self.tmp / "replays"
        with mock.patch("recording.recorder.stm_fetcher", return_value=(fetch, "https://example.test/feed")) as fetcher, \
                mock.patch("recording.cli.REPLAYS_DIR", replays), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(["record", "--routes", "121, 45", "--minutes", "0.01", "--interval", "0.2",
                         "--out", str(out), "--export"])
        self.assertEqual(code, 0)
        fetcher.assert_called_once_with([121, 45], timeout=0.2)
        text = stdout.getvalue()
        polls = int(re.search(r"Done: (\d+) polls", text).group(1))   # 4 (0.6 s / 0.2 s + 1), fewer if the machine is slow
        self.assertGreaterEqual(polls, 2)
        self.assertEqual(text.count(" vehicles  45:1  121:1"), polls)  # one progress line per poll
        self.assertIn("Next steps", text)                               # from the export
        with RecordingStore.open(out) as store:
            self.assertEqual((store.meta["source"], store.meta["route_ids"]), ("stm-api", ["121", "45"]))
        self.assertEqual(len(list(replays.glob("rec_*.json"))), 1)

    def test_bad_routes_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["record", "--routes", "45,abc", "--out", str(self.tmp / "x.sqlite")]), 2)
        self.assertIn("route ids are numbers", stderr.getvalue())

    def test_cli_record_stops_with_a_friendly_error_when_polls_cannot_be_saved(self):
        fetch = lambda: (T0, [entity("41053", "45", T0)])
        out = self.tmp / "rec.sqlite"
        with mock.patch("recording.recorder.stm_fetcher", return_value=(fetch, "https://example.test/feed")), \
                mock.patch("recording.store.RecordingStore.add_poll", side_effect=sqlite3.OperationalError("disk I/O error")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr, \
                self.assertLogs("Recorder", "WARNING"):
            code = main(["record", "--routes", "45", "--minutes", "1", "--interval", "0.1", "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertIn("error: Recording stopped: the last 3 polls could not be saved", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(stdout.getvalue().count("FAILED: could not save this poll"), 2)
        self.assertTrue(out.exists())

    def test_help_suggests_routes_that_are_on_the_3d_map(self):
        for command in (["--help"], ["record", "--help"]):
            with self.subTest(command=command), contextlib.redirect_stdout(io.StringIO()) as stdout, \
                    self.assertRaises(SystemExit):
                main(command)
            self.assertIn('--routes "24,51,55,80,129,165"', stdout.getvalue())
            self.assertNotIn("121,45,140", stdout.getvalue())
        self.assertIn("the 3D map covers downtown Montreal only", " ".join(stdout.getvalue().split()))  # help is wrapped


if __name__ == '__main__':
    unittest.main()
