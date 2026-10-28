import csv
import io
import json
import logging
import os
import zipfile
import zlib
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from recording import RecordingError
from recording.paths import repo_relative
from recording.store import utc_now_iso

# The stops of every bus line, for the Godot viewer to highlight those of the selected bus's line.
# Keyed by route and direction_id: the trip ids of the live feed change with every GTFS, these do not.
# Readers ignore unknown top-level keys; bump VERSION only for breaking changes.
FORMAT = "stm-dt-route-stops"
VERSION = 1

BUS_ROUTE_TYPE = "3"
# Columns each GTFS file needs; trip_headsign, route_long_name and route_short_name are optional.
REQUIRED_COLUMNS = {
    "routes.txt": ("route_id", "route_type"),
    "trips.txt": ("route_id", "trip_id", "direction_id"),
    "stops.txt": ("stop_id", "stop_name", "stop_lat", "stop_lon"),
    "stop_times.txt": ("trip_id", "stop_id", "stop_sequence"),
}
# stop_times rows between two progress reports (the STM's file has about 6.5 million)
PROGRESS_ROWS = 1_000_000

logger = logging.getLogger("RouteStops")


@dataclass
class RouteStopsResult:
    data: dict
    routes: int
    directions: int
    stops: int
    trips: int          # bus trips with stop times
    patterns: int       # distinct stop sequences of those trips
    stop_times: int     # rows read from stop_times.txt
    missing_stops: int  # stop ids of stop_times.txt not in stops.txt (left out)


def build_route_stops(gtfs:Path, on_progress=None) -> RouteStopsResult:
    """The stops of every bus route and direction of a static GTFS.

    `gtfs` is a folder with routes.txt, trips.txt, stops.txt and stop_times.txt or stop_times.zip (like datasets/),
    or a GTFS .zip holding those files (like the STM's gtfs_stm.zip). A direction lists the stops of all its trips:
    those of its most frequent stop pattern in order, then the stops only found in other patterns (see merge_patterns).
    on_progress(rows, fraction) is called every PROGRESS_ROWS rows of stop_times.txt and at the end; fraction is the
    share of the file read (None if unknown).
    """
    gtfs = Path(gtfs)
    if not gtfs.exists():
        raise RecordingError(f"GTFS not found: {gtfs}")
    route_names = _read_bus_routes(gtfs)
    if not route_names:
        raise RecordingError(f"routes.txt of {gtfs.name} has no bus route (route_type {BUS_ROUTE_TYPE}).")
    trips = _read_trips(gtfs, route_names)
    trip_patterns, rows = _read_trip_patterns(gtfs, trips, on_progress)

    # (route, direction) -> stop pattern -> trips; the same for headsigns
    patterns, headsigns = defaultdict(Counter), defaultdict(Counter)
    stop_ids = {}       # pattern with stop sequences -> its stop ids, each once (a loop ends where it starts)
    for trip_id, pattern in trip_patterns.items():
        route_id, direction, headsign = trips[trip_id]
        if pattern not in stop_ids:
            stop_ids[pattern] = tuple(dict.fromkeys(stop for _, stop in pattern))
        patterns[route_id, direction][stop_ids[pattern]] += 1
        headsigns[route_id, direction][headsign] += 1
    if not patterns:
        raise RecordingError(f"No bus trip of {gtfs.name} has stop times (bus routes have route_type {BUS_ROUTE_TYPE} "
                             f"in routes.txt; their trips are in trips.txt, their stops in stop_times.txt).")

    stops = _read_stops(gtfs, {stop for pattern in stop_ids.values() for stop in pattern})
    missing = set()
    routes = {}
    for (route_id, direction), counts in patterns.items():
        merged = merge_patterns(counts)
        missing.update(stop for stop in merged if stop not in stops)
        route = routes.setdefault(route_id, {"name": route_names[route_id], "directions": {}})
        route["directions"][direction] = {
            "headsign": min(headsigns[route_id, direction].items(), key=lambda item: (-item[1], item[0]))[0],
            "trips": sum(counts.values()),
            "stops": [stop for stop in merged if stop in stops],
        }
    if missing:
        logger.warning(f"{len(missing)} stop(s) of stop_times.txt are not in stops.txt and were left out: "
                       f"{', '.join(sorted(missing)[:10])}{', ...' if len(missing) > 10 else ''}")

    data = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": utc_now_iso(),
        "source": repo_relative(gtfs),
        "stops": stops,
        "routes": routes,
    }
    return RouteStopsResult(data, routes=len(routes), directions=len(patterns), stops=len(stops),
                            trips=len(trip_patterns), patterns=len(stop_ids), stop_times=rows, missing_stops=len(missing))


def merge_patterns(patterns:Counter) -> list[str]:
    """The stops of several stop patterns (tuples of stop ids -> number of trips) in one list, each once.

    The most frequent pattern gives the order; the stops only found in other patterns (most frequent first) are
    inserted right after the stop before them in the first pattern where they appear. A stop that starts its pattern
    goes before the next stop already listed; the stops of a pattern sharing none are appended.
    Ties: more stops first, then the smallest stop ids, so the result does not depend on the file order.
    """
    ranked = sorted(patterns, key=lambda pattern: (-patterns[pattern], -len(pattern), pattern))
    merged = list(ranked[0])
    listed = set(merged)
    for pattern in ranked[1:]:
        for index, stop in enumerate(pattern):
            if stop in listed:
                continue
            if index > 0:
                position = merged.index(pattern[index - 1]) + 1
            else:
                following = next((other for other in pattern if other in listed), None)
                position = merged.index(following) if following is not None else len(merged)
            merged.insert(position, stop)
            listed.add(stop)
    return merged


def write_route_stops(data:dict, path:Path) -> int:
    """Compact UTF-8 JSON with sorted keys (same input, same file), written atomically. Returns the file size in bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(text.encode("utf-8"))
    os.replace(temporary, path)
    return path.stat().st_size


def _read_bus_routes(gtfs:Path) -> dict[str, str]:
    """route_id -> name (route_long_name, else route_short_name) of the bus routes."""
    with _read_table(gtfs, "routes.txt") as (reader, _, _):
        return {row["route_id"]: row.get("route_long_name") or row.get("route_short_name") or ""
                for row in reader if row["route_type"].strip() == BUS_ROUTE_TYPE}


def _read_trips(gtfs:Path, route_names:dict) -> dict[str, tuple[str, str, str]]:
    """trip_id -> (route_id, direction_id, headsign) of the trips of the bus routes."""
    trips, shared = {}, {}      # shared: one string object per distinct value (170,000 trips in the STM's GTFS)
    without_direction = 0
    with _read_table(gtfs, "trips.txt") as (reader, _, _):
        for row in reader:
            if row["route_id"] not in route_names:
                continue
            direction = row["direction_id"].strip()
            if not direction:
                without_direction += 1
                continue
            values = (row["route_id"], direction, row.get("trip_headsign") or "")
            trips[row["trip_id"]] = tuple(shared.setdefault(value, value) for value in values)
    if without_direction:
        logger.warning(f"{without_direction} bus trip(s) of trips.txt have no direction_id and were left out.")
    return trips


def _read_trip_patterns(gtfs:Path, trips:dict, on_progress=None) -> tuple[dict[str, tuple], int]:
    """trip_id -> its ((stop_sequence, stop_id), ...) in stop_sequence order, for the trips of `trips`; and the rows read.

    Streams stop_times.txt: the rows of a trip are usually together, but this is not required. Trips with the same
    stops share one tuple (a few thousand distinct patterns for 170,000 trips), which keeps the memory low.
    """
    shared, by_trip = {}, {}
    current_trip, current = None, []

    def add(trip_id, stops:list):
        if not stops:
            return
        if trip_id in by_trip:      # the trip's rows are not all together: merge with those seen earlier
            stops = stops + list(by_trip[trip_id])
        pattern = tuple(sorted(stops))
        by_trip[trip_id] = shared.setdefault(pattern, pattern)

    rows = 0
    with _read_table(gtfs, "stop_times.txt") as (reader, binary, size):
        for row in reader:
            rows += 1
            if on_progress and rows % PROGRESS_ROWS == 0:
                on_progress(rows, _fraction_read(binary, size))
            trip_id = row["trip_id"]
            if trip_id not in trips:
                continue
            if trip_id != current_trip:
                add(current_trip, current)
                current_trip, current = trip_id, []
            try:
                sequence = int(row["stop_sequence"])
            except (TypeError, ValueError):
                raise RecordingError(f"stop_times.txt line {reader.line_num}: stop_sequence should be a whole number "
                                     f"but is '{row['stop_sequence']}'.")
            current.append((sequence, row["stop_id"]))
        add(current_trip, current)
    if on_progress:
        on_progress(rows, 1.0)
    return by_trip, rows


def _read_stops(gtfs:Path, wanted:set) -> dict[str, dict]:
    """stop_id -> {"name", "lat", "lon"} of the `wanted` stops (those of stops.txt only)."""
    stops = {}
    with _read_table(gtfs, "stops.txt") as (reader, _, _):
        for row in reader:
            if row["stop_id"] not in wanted:
                continue
            try:
                lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):     # also rejects NaN and infinities
                    raise ValueError
                position = {"lat": round(lat, 6), "lon": round(lon, 6)}
            except (TypeError, ValueError):
                raise RecordingError(f"stops.txt line {reader.line_num}: stop {row['stop_id']} has no valid "
                                     f"stop_lat/stop_lon ('{row['stop_lat']}', '{row['stop_lon']}').")
            stops[row["stop_id"]] = {"name": row["stop_name"], **position}
    return stops


@contextmanager
def _read_table(gtfs:Path, name:str):
    """(csv.DictReader, binary file, size in bytes) of the GTFS file `name`, after checking its columns.

    The cells missing from a short row are "". Text that is not UTF-8 or not CSV is a RecordingError, whether it shows
    in the header or while the caller reads the rows (the exception is then raised at the yield).
    """
    try:
        with _open_binary(gtfs, name) as (binary, size):
            with io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as text:
                reader = csv.DictReader(text, restval="")
                columns = reader.fieldnames or []
                missing = [column for column in REQUIRED_COLUMNS[name] if column not in columns]
                if missing:
                    raise RecordingError(f"{name} of {gtfs.name} has no {', '.join(missing)} column "
                                         f"(columns found: {', '.join(columns) or 'none'}).")
                yield reader, binary, size
    except UnicodeDecodeError as error:
        raise RecordingError(f"{name} of {gtfs.name} is not UTF-8 text (byte 0x{error.object[error.start]:02x}: "
                             f"{error.reason}); save it as UTF-8.") from error
    except csv.Error as error:     # e.g. a stray quote: the rest of the file becomes one field, too large
        raise RecordingError(f"{name} of {gtfs.name} is not a valid CSV file ({error}).") from error


@contextmanager
def _open_binary(gtfs:Path, name:str):
    """(binary file, size) of `name` in a GTFS folder (or in <stem>.zip next to where it would be) or in a GTFS .zip."""
    if gtfs.is_dir():
        path = gtfs / name
        if path.is_file():
            with open(path, "rb") as file:
                yield file, path.stat().st_size
            return
        zipped = path.with_suffix(".zip")
        if not zipped.is_file():
            raise RecordingError(f"{name} not found in {gtfs} (nor {zipped.name}).")
        archive_path, where = zipped, zipped.name
    elif zipfile.is_zipfile(gtfs):
        archive_path, where = gtfs, gtfs.name
    else:
        raise RecordingError(f"{gtfs} is neither a GTFS folder nor a GTFS .zip.")

    try:
        with zipfile.ZipFile(archive_path) as archive:
            member = _zip_member(archive, name)
            if member is None:
                raise RecordingError(f"{name} not found in {where}.")
            with archive.open(member) as file:
                yield file, member.file_size
    except (zipfile.BadZipFile, zlib.error, EOFError) as error:   # damaged data shows while it is read: also at the yield
        raise RecordingError(f"{where} is damaged or is not a .zip file ({error}).") from error
    except NotImplementedError as error:    # e.g. Deflate64 (7-Zip's "Ultra" setting)
        raise RecordingError(f"{where} uses a compression method Python cannot read ({error}): re-zip it with standard "
                             f"Deflate, or unzip it and pass the folder.") from error


def _zip_member(archive:zipfile.ZipFile, name:str) -> zipfile.ZipInfo | None:
    """The member called `name`, at the root or else in the shallowest folder (macOS '__MACOSX/' copies excluded)."""
    members = [info for info in archive.infolist() if not info.is_dir() and not info.filename.startswith("__MACOSX/")
               and info.filename.rsplit("/", 1)[-1] == name]
    return min(members, key=lambda info: (info.filename.count("/"), info.filename), default=None)


def _fraction_read(binary, size:int) -> float | None:
    try:
        return min(1.0, binary.tell() / size) if size else None
    except (OSError, ValueError):
        return None
