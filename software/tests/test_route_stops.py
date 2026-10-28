import contextlib
import csv
import io
import json
import struct
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from unittest import mock
from fixtures import TempDirTestCase
from recording import RecordingError, route_stops
from recording.cli import main
from recording.route_stops import build_route_stops, merge_patterns, write_route_stops

ROUTES = [  # route_id, route_long_name, route_type
    ("1", "Ligne 1 - Verte", "1"),          # metro: left out
    ("24", "Sherbrooke", "3"),
    ("51", "Édouard-Montpetit", "3"),
    ("99", "Sans voyage", "3"),             # bus route without trips: left out
]
TRIPS = {  # trip_id: route_id, direction_id, headsign, stops
    "t1": ("24", "0", "Est", "A B C D E"),
    "t2": ("24", "0", "Est", "A B C D E"),
    "t3": ("24", "0", "Est", "A B C D E"),
    "t4": ("24", "0", "Est (garage)", "A B X D E F"),     # X replaces C, F after the terminus
    "t5": ("24", "0", "Est (garage)", "W A B"),           # starts before A
    "t6": ("24", "1", "Ouest", "E D C B A"),
    "t7": ("24", "1", "Ouest", "E D C B A"),
    "t8": ("51", "0", "Station B", "G H"),
    "t9": ("51", "0", "Station A", "G H"),
    "t10": ("51", "1", "Boucle", "H G H"),                # a loop: ends where it starts
    "t11": ("24", "0", "Est", ""),                        # no stop times: not counted
    "m1": ("1", "0", "Montmorency", "M1 M2"),
}
STOP_NAMES = {"A": "Sherbrooke / Saint-Denis", "B": "Sherbrooke / Saint-Hubert", "C": "Sherbrooke / Berri",
              "D": "Sherbrooke / Amherst", "E": "Sherbrooke / Papineau", "F": "Garage Frontenac", "W": "Sherbrooke / Bleury",
              "X": "Ontario / Berri", "G": "Édouard-Montpetit / Côte-des-Neiges", "H": "Station Université-de-Montréal",
              "M1": "Station Berri-UQAM", "M2": "Station Montmorency", "Z": "Arrêt sans voyage"}
EXPECTED_ROUTES = {
    "24": {"name": "Sherbrooke", "directions": {
        "0": {"headsign": "Est", "trips": 5, "stops": ["W", "A", "B", "X", "C", "D", "E", "F"]},
        "1": {"headsign": "Ouest", "trips": 2, "stops": ["E", "D", "C", "B", "A"]}}},
    "51": {"name": "Édouard-Montpetit", "directions": {
        "0": {"headsign": "Station A", "trips": 2, "stops": ["G", "H"]},     # tie: the first name
        "1": {"headsign": "Boucle", "trips": 1, "stops": ["H", "G"]}}},
}


def stop_position(index:int) -> tuple[str, str]:
    return f"{45.5 + index / 1000 + 0.00000037:.9f}", f"{-73.6 + index / 1000 - 0.00000062:.9f}"


def gtfs_tables(trips:dict=None) -> dict[str, list[dict]]:
    """GTFS file name -> rows (dicts with the STM's columns)."""
    trips = TRIPS if trips is None else trips
    routes = [{"route_id": route_id, "agency_id": "STM", "route_short_name": route_id, "route_long_name": name,
               "route_type": route_type, "route_url": ""} for route_id, name, route_type in ROUTES]
    trip_rows = [{"route_id": route_id, "service_id": "25N", "trip_id": trip_id, "trip_headsign": headsign,
                  "direction_id": direction, "shape_id": ""} for trip_id, (route_id, direction, headsign, _) in trips.items()]
    stops = []
    for index, (stop_id, name) in enumerate(STOP_NAMES.items()):
        lat, lon = stop_position(index)
        stops.append({"stop_id": stop_id, "stop_code": str(10000 + index), "stop_name": name, "stop_lat": lat,
                      "stop_lon": lon, "location_type": "0"})
    stop_times = []
    for trip_id, (_, _, _, stop_ids) in trips.items():
        for sequence, stop_id in enumerate(stop_ids.split(), start=1):
            stop_times.append({"trip_id": trip_id, "arrival_time": f"08:{sequence:02d}:00",
                               "departure_time": f"08:{sequence:02d}:00", "stop_id": stop_id, "stop_sequence": str(sequence)})
    # The rows of a trip don't have to be together nor in stop_sequence order: move the end of t1 last, reversed
    moved = [row for row in stop_times if row["trip_id"] == "t1" and int(row["stop_sequence"]) >= 3]
    stop_times = [row for row in stop_times if row not in moved] + moved[::-1]
    return {"routes.txt": routes, "trips.txt": trip_rows, "stops.txt": stops, "stop_times.txt": stop_times}


def unused_stops(count:int) -> list[dict]:
    """`count` stops no trip uses (ASCII, about 50 bytes each in stops.txt): 200 of them fill more than the 8 KB that
    are decoded when the header is read, so what comes after is only read while the rows are."""
    return [{"stop_id": f"P{index}", "stop_code": str(20000 + index), "stop_name": f"Rue Ontario / {index}e Avenue",
             "stop_lat": stop_position(index)[0], "stop_lon": stop_position(index)[1], "location_type": "0"}
            for index in range(count)]


def damage_zip_member(path:Path, name:str, value:int):
    """Overwrite the first byte of the data of the member `name` in the .zip at `path`."""
    with zipfile.ZipFile(path) as archive:
        offset = archive.getinfo(name).header_offset
    data = bytearray(path.read_bytes())
    name_length, extra_length = struct.unpack_from("<HH", data, offset + 26)     # local file header
    data[offset + 30 + name_length + extra_length] = value
    path.write_bytes(bytes(data))


def csv_text(rows:list[dict], columns:list[str]=None) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns or list(rows[0]), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def comparable(data:dict) -> dict:
    """The output without what changes with the run (time) and the source path."""
    return {key: value for key, value in data.items() if key not in ("created_at", "source")}


class RouteStopsTest(TempDirTestCase):

    def write_folder(self, name="gtfs", tables:dict=None, zip_stop_times=False, encoding="utf-8") -> Path:
        """A GTFS folder like datasets/: stop_times.txt, or stop_times.zip holding it (with macOS junk, as in datasets/)."""
        folder = self.tmp / name
        folder.mkdir()
        for file_name, rows in (tables or gtfs_tables()).items():
            text = csv_text(rows)
            if zip_stop_times and file_name == "stop_times.txt":
                with zipfile.ZipFile(folder / "stop_times.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("stop_times.txt", text.encode(encoding))
                    archive.writestr("__MACOSX/._stop_times.txt", b"\x00\x05\x16\x07 Mac OS X")
            else:
                (folder / file_name).write_bytes(text.encode(encoding))
        return folder

    def write_zip(self, name="gtfs_stm.zip", tables:dict=None, prefix="", compression=zipfile.ZIP_DEFLATED) -> Path:
        """A GTFS .zip like the STM's gtfs_stm.zip (files at the root, or in the folder `prefix`)."""
        path = self.tmp / name
        with zipfile.ZipFile(path, "w", compression) as archive:
            for file_name, rows in (tables or gtfs_tables()).items():
                archive.writestr(prefix + file_name, csv_text(rows).encode("utf-8"))
        return path

    def test_stops_of_each_route_and_direction(self):
        result = build_route_stops(self.write_folder())
        data = result.data
        self.assertEqual((data["format"], data["version"], data["source"]), ("stm-dt-route-stops", 1, "gtfs"))
        self.assertRegex(data["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(data["routes"], EXPECTED_ROUTES)      # no metro (route_type 1), no bus route without trips
        self.assertEqual(sorted(data["stops"]), ["A", "B", "C", "D", "E", "F", "G", "H", "W", "X"])  # no M1, M2, Z
        self.assertEqual(data["stops"]["G"], {"name": "Édouard-Montpetit / Côte-des-Neiges", "lat": 45.508, "lon": -73.592001})
        self.assertEqual(data["stops"]["A"], {"name": "Sherbrooke / Saint-Denis", "lat": 45.5, "lon": -73.600001})
        self.assertEqual((result.routes, result.directions, result.stops, result.trips, result.patterns, result.stop_times,
                          result.missing_stops), (2, 4, 10, 10, 6, 43, 0))

    def test_same_result_from_every_gtfs_layout(self):
        expected = comparable(build_route_stops(self.write_folder()).data)
        layouts = {
            "folder with stop_times.zip": lambda: self.write_folder("zipped", zip_stop_times=True),
            "GTFS .zip": lambda: self.write_zip(),
            "GTFS .zip with a folder inside": lambda: self.write_zip("nested.zip", prefix="gtfs_stm/"),
            "files with a BOM": lambda: self.write_folder("bom", encoding="utf-8-sig"),
            "stop_times.zip with a BOM": lambda: self.write_folder("bom_zipped", zip_stop_times=True, encoding="utf-8-sig"),
        }
        for layout, write in layouts.items():
            with self.subTest(layout=layout):
                gtfs = write()
                data = build_route_stops(gtfs).data
                self.assertEqual(comparable(data), expected)
                self.assertEqual(data["source"], gtfs.name)          # outside the repo: the name only

    def test_stop_times_txt_is_preferred_to_stop_times_zip(self):
        folder = self.write_folder(zip_stop_times=True)
        tables = gtfs_tables({trip_id: trip for trip_id, trip in TRIPS.items() if trip_id == "t8"})
        (folder / "stop_times.txt").write_text(csv_text(tables["stop_times.txt"]), encoding="utf-8")
        self.assertEqual(list(build_route_stops(folder).data["routes"]), ["51"])

    def test_merged_order_does_not_depend_on_the_order_of_the_trips(self):
        patterns = {("A", "B", "C", "D", "E"): 3, ("A", "B", "X", "D", "E", "F"): 1, ("W", "A", "B"): 1}
        expected = ["W", "A", "B", "X", "C", "D", "E", "F"]
        self.assertEqual(merge_patterns(Counter(patterns)), expected)
        self.assertEqual(merge_patterns(Counter(dict(reversed(list(patterns.items()))))), expected)
        # equal trips: the longest pattern gives the order, then the smallest stop ids
        self.assertEqual(merge_patterns(Counter({("A", "C"): 2, ("A", "B", "C"): 2})), ["A", "B", "C"])
        self.assertEqual(merge_patterns(Counter({("B", "C"): 2, ("A", "C"): 2})), ["A", "B", "C"])
        # a pattern sharing no stop goes at the end
        self.assertEqual(merge_patterns(Counter({("A", "B"): 2, ("Y", "Z"): 1})), ["A", "B", "Y", "Z"])

    def test_stops_missing_from_stops_txt_are_left_out_with_a_warning(self):
        tables = gtfs_tables()
        tables["stops.txt"] = [row for row in tables["stops.txt"] if row["stop_id"] != "X"]
        with self.assertLogs("RouteStops", "WARNING") as logs:
            result = build_route_stops(self.write_folder(tables=tables))
        self.assertIn("1 stop(s) of stop_times.txt are not in stops.txt and were left out: X", logs.output[0])
        self.assertEqual(result.data["routes"]["24"]["directions"]["0"]["stops"], ["W", "A", "B", "C", "D", "E", "F"])
        self.assertEqual(result.missing_stops, 1)

    def test_missing_files_and_columns_are_friendly_errors(self):
        with self.assertRaisesRegex(RecordingError, "GTFS not found"):
            build_route_stops(self.tmp / "nope")
        (self.tmp / "notes.csv").write_text("a,b\n", encoding="utf-8")
        with self.assertRaisesRegex(RecordingError, "neither a GTFS folder nor a GTFS .zip"):
            build_route_stops(self.tmp / "notes.csv")

        folder = self.write_folder("no_stops")
        (folder / "stops.txt").unlink()
        with self.assertRaisesRegex(RecordingError, r"stops.txt not found in .*no_stops \(nor stops.zip\)"):
            build_route_stops(folder)
        folder = self.write_folder("no_stop_times")
        (folder / "stop_times.txt").unlink()
        with self.assertRaisesRegex(RecordingError, r"stop_times.txt not found in .*no_stop_times \(nor stop_times.zip\)"):
            build_route_stops(folder)
        tables = gtfs_tables()
        del tables["trips.txt"]
        with self.assertRaisesRegex(RecordingError, "trips.txt not found in gtfs_stm.zip"):
            build_route_stops(self.write_zip(tables=tables))

        tables = gtfs_tables()
        folder = self.write_folder("no_direction_id")
        columns = [column for column in tables["trips.txt"][0] if column != "direction_id"]
        (folder / "trips.txt").write_text(csv_text(tables["trips.txt"], columns), encoding="utf-8")
        with self.assertRaisesRegex(RecordingError, r"trips.txt of no_direction_id has no direction_id column "
                                                    r"\(columns found: route_id, service_id, trip_id, trip_headsign, shape_id\)"):
            build_route_stops(folder)

    def test_bad_values_are_friendly_errors(self):
        tables = gtfs_tables()
        tables["stop_times.txt"][3]["stop_sequence"] = "fourth"
        with self.assertRaisesRegex(RecordingError, r"stop_times.txt line 5: stop_sequence should be a whole number "
                                                    r"but is 'fourth'"):
            build_route_stops(self.write_folder("bad_sequence", tables=tables))
        for index, lat in enumerate(("", "NaN", "inf", "1e400", "95.5")):
            with self.subTest(lat=lat):
                tables = gtfs_tables()
                tables["stops.txt"][1]["stop_lat"] = lat
                with self.assertRaisesRegex(RecordingError, r"stops.txt line 3: stop B has no valid stop_lat/stop_lon"):
                    build_route_stops(self.write_folder(f"bad_position_{index}", tables=tables))

    def test_short_rows_have_empty_cells(self):
        tables = gtfs_tables()
        folder = self.write_folder("short_rows", tables=tables)
        # route 24 without its route_type: not a bus route; trip t10 without its direction_id: left out
        short = {"routes.txt": ("route_id", "24", "24,STM"), "trips.txt": ("trip_id", "t10", "51,25N,t10,Boucle")}
        for file_name, (column, value, line) in short.items():
            rows = [row for row in tables[file_name] if row[column] != value]
            (folder / file_name).write_bytes((csv_text(rows) + line + "\r\n").encode("utf-8"))
        with self.assertLogs("RouteStops", "WARNING") as logs:
            result = build_route_stops(folder)
        self.assertIn("1 bus trip(s) of trips.txt have no direction_id and were left out.", logs.output[0])
        self.assertEqual(result.data["routes"], {"51": {"name": "Édouard-Montpetit",
                                                        "directions": {"0": EXPECTED_ROUTES["51"]["directions"]["0"]}}})

        folder = self.write_folder("short_stop_time", tables=tables)
        (folder / "stop_times.txt").write_bytes((csv_text(tables["stop_times.txt"]) + "t8,08:03:00\r\n").encode("utf-8"))
        with self.assertRaisesRegex(RecordingError, r"stop_times.txt line 45: stop_sequence should be a whole number "
                                                    r"but is ''"):
            build_route_stops(folder)

    def test_files_that_are_not_utf8_csv_are_friendly_errors(self):
        tables = gtfs_tables()
        # stops.txt saved in Windows-1252: the É of "Édouard-Montpetit" decoded with the header, or later with the rows
        for name, stops in (("cp1252", tables["stops.txt"]), ("cp1252_far", unused_stops(200) + tables["stops.txt"])):
            with self.subTest(name=name):
                folder = self.write_folder(name, tables=tables)
                (folder / "stops.txt").write_bytes(csv_text(stops).encode("cp1252"))
                with self.assertRaisesRegex(RecordingError, rf"^stops.txt of {name} is not UTF-8 text \(byte 0xc9: "
                                                            rf"invalid continuation byte\); save it as UTF-8\.$"):
                    build_route_stops(folder)
        # a stray quote: the rest of the file becomes one field, larger than the csv module allows
        folder = self.write_folder("stray_quote", tables=tables)
        text = csv_text(tables["stops.txt"] + unused_stops(3000)).replace(",Sherbrooke / Saint-Denis,",
                                                                          ',"Sherbrooke / Saint-Denis,')
        (folder / "stops.txt").write_bytes(text.encode("utf-8"))
        with self.assertRaisesRegex(RecordingError, r"^stops.txt of stray_quote is not a valid CSV file "
                                                    r"\(field larger than field limit \(131072\)\)\.$"):
            build_route_stops(folder)

    def test_damaged_zips_are_friendly_errors(self):
        # a stop_times.zip that is not a .zip (e.g. an error page saved by a failed download)
        folder = self.write_folder("not_a_zip", zip_stop_times=True)
        (folder / "stop_times.zip").write_bytes(b"<html><body>404 Not Found</body></html>\n")
        with self.assertRaisesRegex(RecordingError, r"^stop_times.zip is damaged or is not a .zip file "
                                                    r"\(File is not a zip file\)\.$"):
            build_route_stops(folder)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["route-stops", "--gtfs", str(folder), "--out", str(self.tmp / "out.json")]), 2)
        self.assertIn("error: stop_times.zip is damaged or is not a .zip file", stderr.getvalue())

        # damaged data inside a .zip shows when it is decompressed: here with the header ...
        path = self.write_zip("bad_data.zip")
        damage_zip_member(path, "stop_times.txt", 0xff)       # an invalid deflate block type
        with self.assertRaisesRegex(RecordingError, r"^bad_data.zip is damaged or is not a .zip file "
                                                    r"\(Error -3 while decompressing data: invalid block type\)\.$"):
            build_route_stops(path)
        # ... or at the end of the file, with the rows (the CRC is checked once all the data is read)
        tables = gtfs_tables()
        tables["stops.txt"] = unused_stops(200) + tables["stops.txt"]
        path = self.write_zip("bad_crc.zip", tables=tables, compression=zipfile.ZIP_STORED)
        path.write_bytes(path.read_bytes().replace(b"Sherbrooke / Papineau", b"Sherbrooke / Papinea_"))
        with self.assertRaisesRegex(RecordingError, r"^bad_crc.zip is damaged or is not a .zip file "
                                                    r"\(Bad CRC-32 for file 'stops.txt'\)\.$"):
            build_route_stops(path)
        # a compression method Python cannot read: every member marked Deflate64 (method 9), as 7-Zip may write
        path = self.write_zip("deflate64.zip")
        data = bytearray(path.read_bytes())
        for signature, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):     # local headers, central directory
            start = data.find(signature)
            while start >= 0:
                data[start + offset:start + offset + 2] = (9).to_bytes(2, "little")
                start = data.find(signature, start + 4)
        path.write_bytes(bytes(data))
        with self.assertRaisesRegex(RecordingError, r"^deflate64.zip uses a compression method Python cannot read .*: "
                                                    r"re-zip it with standard Deflate, or unzip it and pass the folder\.$"):
            build_route_stops(path)
        self.assertEqual(list(self.tmp.glob("**/*.json")), [])

    def test_no_bus_trip_is_an_error(self):
        tables = gtfs_tables()
        tables["routes.txt"] = [row for row in tables["routes.txt"] if row["route_type"] != "3"]
        with self.assertRaisesRegex(RecordingError, r"routes.txt of metro_only has no bus route \(route_type 3\)"):
            build_route_stops(self.write_folder("metro_only", tables=tables))
        tables = gtfs_tables({"m1": TRIPS["m1"], "t11": TRIPS["t11"]})
        with self.assertRaisesRegex(RecordingError, "No bus trip of no_bus_stop_times has stop times"):
            build_route_stops(self.write_folder("no_bus_stop_times", tables=tables))

    def test_progress_is_reported_while_reading_stop_times(self):
        calls = []
        with mock.patch.object(route_stops, "PROGRESS_ROWS", 10):
            build_route_stops(self.write_folder(zip_stop_times=True), on_progress=lambda *call: calls.append(call))
        self.assertEqual([rows for rows, _ in calls], [10, 20, 30, 40, 43])
        fractions = [fraction for _, fraction in calls]
        self.assertEqual(fractions[-1], 1.0)
        self.assertTrue(all(0 < fraction <= 1 for fraction in fractions))

    def test_written_file_is_compact_sorted_and_reproducible(self):
        gtfs = self.write_folder()
        first, second = self.tmp / "first.json", self.tmp / "out" / "second.json"
        data = build_route_stops(gtfs).data
        size = write_route_stops(data, first)
        write_route_stops(dict(build_route_stops(gtfs).data, created_at=data["created_at"]), second)
        raw = first.read_bytes()
        self.assertEqual(size, len(raw))
        self.assertEqual(raw, second.read_bytes())
        text = raw.decode("utf-8")
        self.assertEqual(text, json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
        self.assertTrue(text.startswith('{"created_at":'))
        self.assertIn("Édouard-Montpetit", text)
        self.assertNotIn("\r", text)
        self.assertEqual(list(self.tmp.glob("**/*.tmp")), [])

    def test_cli_writes_the_route_stops_file(self):
        gtfs = self.write_zip()
        out = self.tmp / "assets" / "route_stops.json"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["route-stops", "--gtfs", str(gtfs), "--out", str(out)]), 0)
        printed = stdout.getvalue()
        self.assertIn("[100%]          43 stop times", printed)
        self.assertIn("Saved the stops of 2 bus routes (4 directions)", printed)
        self.assertIn("10 bus trips (6 distinct stop patterns)", printed)
        self.assertIn("copy the file to", printed)          # not the viewer's file: how to use it
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((data["source"], data["routes"]), ("gtfs_stm.zip", EXPECTED_ROUTES))

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["route-stops", "--gtfs", str(gtfs), "--out", str(out)]), 0)
        self.assertIn("replaced the previous file", stdout.getvalue())
        for argv, message in ((["--gtfs", str(gtfs), "--out", str(self.tmp / "stops.csv")], "--out must end with .json"),
                              (["--gtfs", str(self.tmp / "nope"), "--out", str(out)], "GTFS not found")):
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(["route-stops"] + argv), 2)
            self.assertIn(message, stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
