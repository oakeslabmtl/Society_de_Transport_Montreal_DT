import contextlib
import csv
import io
import unittest
from fixtures import T0, TempDirTestCase, typed
from recording import RecordingError
from recording.cli import main
from recording.csv_import import read_csv_polls
from recording.store import RecordingStore

COLUMNS = ["_id", "id", "vehicle.trip.trip_id", "vehicle.trip.start_time", "vehicle.trip.start_date", "vehicle.trip.route_id",
           "vehicle.position.latitude", "vehicle.position.longitude", "vehicle.position.speed",
           "vehicle.current_stop_sequence", "vehicle.current_status", "vehicle.timestamp", "vehicle.vehicle.id",
           "vehicle.occupancy_status", "vehicle.position.bearing", "vehicle.congestion_level"]


def object_id(insert_time:int, counter:int) -> str:
    return f"{insert_time:08x}d878057919{counter:06x}"


def row(insert_time:int, counter:int, vehicle:str, route:str, timestamp:int, bearing:str="39", speed:str="0", **extra) -> dict:
    values = {"_id": object_id(insert_time, counter), "id": vehicle, "vehicle.trip.trip_id": "281567850",
              "vehicle.trip.start_time": "08:30:00", "vehicle.trip.start_date": "20250301", "vehicle.trip.route_id": route,
              "vehicle.position.latitude": "45.568321228027344", "vehicle.position.longitude": "-73.65330505371094",
              "vehicle.position.speed": speed, "vehicle.current_stop_sequence": "32", "vehicle.current_status": "1",
              "vehicle.timestamp": str(timestamp), "vehicle.vehicle.id": vehicle, "vehicle.occupancy_status": "3",
              "vehicle.position.bearing": bearing, "vehicle.congestion_level": ""}
    values.update(extra)
    return values


class CsvImportTest(TempDirTestCase):

    def write_csv(self, rows:list[dict], columns:list[str]=None, name="buses.csv"):
        path = self.tmp / name
        columns = columns or list(rows[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def sample_rows(self) -> list[dict]:
        return [
            row(T0, 1, "41053", "140", T0 - 3, bearing=""),
            row(T0, 2, "31841", "121", T0 - 5, speed="10.000080108642578"),
            row(T0 + 15, 3, "41053", "140", T0 + 12),
            row(T0 + 15, 4, "31841", "121", T0 + 11),
            # one poll whose insert straddled a second boundary: T0+30 and T0+31 are the same poll
            row(T0 + 30, 5, "41053", "140", T0 + 27),
            row(T0 + 31, 6, "31841", "121", T0 + 26),
            # the same vehicle twice in one poll: keep the newest position
            row(T0 + 31, 7, "41053", "140", T0 + 29, bearing="45"),
            # 63 minutes later
            row(T0 + 30 + 3812, 8, "41053", "140", T0 + 3840),
        ]

    def test_object_id_clustering_with_straddle_and_duplicates(self):
        rows = self.sample_rows()
        result = read_csv_polls(self.write_csv(list(reversed(rows)), COLUMNS))  # file order must not matter
        self.assertEqual(result.mode, "_id")
        self.assertEqual([time for time, _ in result.polls], [T0, T0 + 15, T0 + 30, T0 + 3842])
        straddle = result.polls[2][1]
        self.assertEqual(sorted(e["id"] for e in straddle), ["31841", "41053"])
        self.assertEqual(next(e for e in straddle if e["id"] == "41053")["vehicle.timestamp"], T0 + 29)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.routes, ["121", "140"])

    def test_object_ids_written_by_mongoexport_or_as_extended_json(self):
        rows = self.sample_rows()
        expected = read_csv_polls(self.write_csv(rows, COLUMNS))
        for form in ("ObjectId({})", '{{"$oid": "{}"}}', "{}"):      # mongoexport, extended JSON, plain (upper case)
            with self.subTest(form=form):
                wrapped = [dict(r, _id=form.format(r["_id"].upper())) for r in rows]
                result = read_csv_polls(self.write_csv(wrapped, COLUMNS, name="wrapped.csv"))
                self.assertEqual(typed(result.polls), typed(expected.polls))
                self.assertEqual(result.duplicates, expected.duplicates)
        for junk in ("ObjectId(not-an-id)", "67c3132c", "67c3132cd878057919b26d7d0"):
            with self.subTest(junk=junk), self.assertRaisesRegex(RecordingError, r"line 2: .* in column _id is not a MongoDB ObjectId"):
                read_csv_polls(self.write_csv([dict(rows[0], _id=junk)], COLUMNS, name="junk.csv"))

    def test_values_get_live_types_and_empty_cells_are_dropped(self):
        rows = self.sample_rows()[:2]
        rows[0]["note"] = "check"
        result = read_csv_polls(self.write_csv(rows, COLUMNS + ["note"]))
        first = {e["id"]: e for e in result.polls[0][1]}["41053"]
        self.assertEqual(typed(first), typed({
            "id": "41053", "vehicle.trip.trip_id": "281567850", "vehicle.trip.start_time": "08:30:00",
            "vehicle.trip.start_date": "20250301", "vehicle.trip.route_id": "140",
            "vehicle.position.latitude": 45.568321228027344, "vehicle.position.longitude": -73.65330505371094,
            "vehicle.position.speed": 0.0, "vehicle.current_stop_sequence": 32, "vehicle.current_status": 1,
            "vehicle.timestamp": T0 - 3, "vehicle.vehicle.id": "41053", "vehicle.occupancy_status": 3,
            "note": "check"}))   # no _id, no bearing, no congestion_level, unknown column kept as text

    def test_poll_time_column_is_used_when_there_is_no_id(self):
        rows = [dict(row(0, 0, "a", "45", T0 - 2), poll_time=str(T0)), dict(row(0, 0, "b", "45", T0 - 1), poll_time=str(T0)),
                dict(row(0, 0, "a", "45", T0 + 13), poll_time=str(T0 + 15))]
        result = read_csv_polls(self.write_csv(rows, COLUMNS[1:] + ["poll_time"]))
        self.assertEqual(result.mode, "poll_time")
        self.assertEqual([(time, len(entities)) for time, entities in result.polls], [(T0, 2), (T0 + 15, 1)])
        self.assertNotIn("poll_time", result.polls[0][1][0])

    def test_falls_back_to_vehicle_timestamp_buckets_with_a_warning(self):
        rows = [row(0, 0, "a", "45", T0 + 1), row(0, 0, "b", "45", T0 + 14), row(0, 0, "a", "45", T0 + 16)]
        with self.assertLogs("CsvImport", "WARNING"):
            result = read_csv_polls(self.write_csv(rows, COLUMNS[1:]), interval=15)
        self.assertEqual(result.mode, "vehicle.timestamp")
        self.assertEqual([(time, len(entities)) for time, entities in result.polls], [(T0, 2), (T0 + 15, 1)])

    def test_bad_values_give_a_friendly_error(self):
        rows = self.sample_rows()[:2]
        rows[1]["vehicle.timestamp"] = "yesterday"
        with self.assertRaisesRegex(RecordingError, r"line 3: column 'vehicle.timestamp'"):
            read_csv_polls(self.write_csv(rows, COLUMNS))
        with self.assertRaisesRegex(RecordingError, "not found"):
            read_csv_polls(self.tmp / "nope.csv")

    def test_cli_import_writes_a_recording(self):
        csv_path = self.write_csv(self.sample_rows(), COLUMNS)
        out = self.tmp / "imported.sqlite"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["import-csv", str(csv_path), "--out", str(out), "--name", "Test day"]), 0)
        self.assertIn("4 polls", stdout.getvalue())
        self.assertIn("info", stdout.getvalue())      # next step hint
        with RecordingStore.open(out) as store:
            meta = store.meta
            self.assertEqual((meta["source"], meta["name"], meta["route_ids"]), ("csv-import", "Test day", ["121", "140"]))
            self.assertEqual(meta["source_detail"], "buses.csv")   # outside the repo: file name only
            self.assertEqual([p.vehicle_count for p in store.polls()], [2, 2, 2, 1])
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["import-csv", str(csv_path), "--out", str(out)]), 2)
        self.assertIn("--force", stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
