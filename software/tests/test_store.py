import contextlib
import sqlite3
import unittest
from unittest import mock
from fixtures import T0, TempDirTestCase, entity, make_recording, typed
from recording import RecordingError
from recording.store import POLLS_PER_QUERY, RecordingStore, dedupe_by_vehicle, entity_to_row, row_to_entity, sort_routes


class StoreRoundTripTest(TempDirTestCase):

    def round_trip(self, entities:list[dict]) -> list[dict]:
        path = make_recording(self.tmp / "r.sqlite", [(T0, entities)])
        with RecordingStore.open(path) as store:
            polls = store.polls()
            return store.entities_by_poll(polls)[polls[0].poll_id]

    def test_entity_round_trip_is_lossless_including_unknown_keys(self):
        original = [
            entity("41053", "121", T0 - 5, **{"vehicle.trip.direction_id": 1, "vehicle.vehicle.label": "4105",
                                              "vehicle.congestion_level": 3, "multi": [1, "a"]}),
            entity("31841", "45", T0 - 9),
        ]
        del original[1]["vehicle.position.bearing"]         # absent stays absent (no None, no NaN)
        self.assertEqual(typed(self.round_trip(original)), typed(original))

    def test_unexpected_types_survive_through_extra_json(self):
        odd = entity("41053", "121", T0, **{"vehicle.position.speed": 0, "vehicle.current_status": "2", "flag": True})
        self.assertEqual(typed(self.round_trip([odd])), typed([odd]))
        self.assertIn("vehicle.position.speed", entity_to_row(odd)["extra_json"])

    def test_entity_without_vehicle_id_uses_entity_id_and_stays_without_it(self):
        anonymous = entity("e-7", "45", T0)
        del anonymous["vehicle.vehicle.id"]
        self.assertEqual(entity_to_row(anonymous)["vehicle_id"], "e-7")
        self.assertEqual(typed(self.round_trip([anonymous])), typed([anonymous]))

    def test_entities_come_back_newest_first_like_live_messages(self):
        result = self.round_trip([entity("a", "45", T0 - 30), entity("b", "45", T0 - 2), entity("c", "45", T0 - 10)])
        self.assertEqual([e["id"] for e in result], ["b", "c", "a"])

    def test_entities_of_many_polls_are_read_in_several_queries(self):
        recorded = [(T0 + 15 * i, [entity("a", "45", T0 + 15 * i - 9, latitude=45.5 + i / 1000), entity("b", "45", T0 + 15 * i - 2)])
                    for i in range(2 * POLLS_PER_QUERY + 5)]
        path = make_recording(self.tmp / "r.sqlite", recorded)
        with RecordingStore.open(path) as store:
            wanted = store.polls()[3::2]
            result = store.entities_by_poll(wanted)
        self.assertEqual(sorted(result), [poll.poll_id for poll in wanted])
        for poll in wanted:
            i = round((poll.poll_time - T0) / 15)
            self.assertEqual(typed(result[poll.poll_id]), typed(list(reversed(recorded[i][1]))))   # newest first

    def test_reading_a_recording_does_not_block_the_recorder(self):
        # An export of a long recording used to hold the read lock while converting every row: the recorder's
        # commits then failed with "database is locked". Each query is now fetched before its rows are converted.
        path = make_recording(self.tmp / "r.sqlite", [(T0 + 15 * i, [entity("a", "45", T0)]) for i in range(POLLS_PER_QUERY + 5)])
        writer = sqlite3.connect(path, timeout=0)      # gives up at once if the file is locked

        def convert_while_recording(row):
            writer.execute("INSERT INTO polls (poll_time, vehicle_count) VALUES (?, 0)", (T0 + 10 ** 6,))
            writer.commit()
            return row_to_entity(row)

        with contextlib.closing(writer), RecordingStore.open(path) as store, \
                mock.patch("recording.store.row_to_entity", convert_while_recording):
            result = store.entities_by_poll(store.polls())
        self.assertEqual(sum(len(entities) for entities in result.values()), POLLS_PER_QUERY + 5)

    def test_poll_rows_and_vehicle_count(self):
        path = make_recording(self.tmp / "r.sqlite", [(T0, [entity("a", "45", T0)]), (T0 + 15, "HTTP 503 Service Unavailable")])
        with RecordingStore.open(path) as store:
            ok, failed = store.polls()
            self.assertEqual((ok.status, ok.vehicle_count, ok.error), ("ok", 1, None))
            self.assertEqual((failed.status, failed.vehicle_count, failed.error), ("error", 0, "HTTP 503 Service Unavailable"))
            self.assertEqual(store.meta["route_ids"], ["45", "121"])
            self.assertEqual(store.meta["poll_interval_s"], 15.0)
            self.assertEqual(store.meta["schema_version"], "1")


class StoreFilesTest(TempDirTestCase):

    def test_create_refuses_to_overwrite_unless_forced(self):
        path = make_recording(self.tmp / "r.sqlite", [])
        with self.assertRaises(RecordingError):
            RecordingStore.create(path, {})
        RecordingStore.create(path, {"name": "again"}, overwrite=True).close()
        with RecordingStore.open(path) as store:
            self.assertEqual(store.meta["name"], "again")

    def test_open_gives_friendly_errors(self):
        with self.assertRaisesRegex(RecordingError, "not found"):
            RecordingStore.open(self.tmp / "missing.sqlite")
        (self.tmp / "notes.sqlite").write_text("hello")
        with self.assertRaisesRegex(RecordingError, "not a recording"):
            RecordingStore.open(self.tmp / "notes.sqlite")

    def test_open_is_read_only(self):
        path = make_recording(self.tmp / "r.sqlite", [])
        with RecordingStore.open(path) as store:
            with self.assertRaises(sqlite3.OperationalError):
                store.add_error_poll(T0, "x")


class HelpersTest(unittest.TestCase):

    def test_dedupe_keeps_newest_position_per_vehicle(self):
        result = dedupe_by_vehicle([entity("a", "45", 100, latitude=1.0), entity("b", "45", 100),
                                    entity("a", "45", 130, latitude=2.0), entity("a", "45", 120, latitude=3.0)])
        self.assertEqual([(e["id"], e["vehicle.position.latitude"]) for e in result], [("a", 2.0), ("b", 45.5)])

    def test_sort_routes(self):
        self.assertEqual(sort_routes(["140", "45", "121", "X1", 45]), ["45", "121", "140", "X1"])


if __name__ == '__main__':
    unittest.main()
