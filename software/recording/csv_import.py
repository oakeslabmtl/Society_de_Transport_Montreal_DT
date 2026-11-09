import csv
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from recording import RecordingError
from recording.store import DEFAULT_POLL_INTERVAL, KEY_TYPES, dedupe_by_vehicle, sort_routes

logger = logging.getLogger("CsvImport")

# Columns that tell when a row was polled; they are not part of the vehicle entity.
OBJECT_ID_COLUMN = "_id"
POLL_TIME_COLUMN = "poll_time"
# A MongoDB ObjectId: 24 hex digits, the first 8 are the insert time (unix seconds)
OBJECT_ID_PATTERN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{24}(?![0-9a-fA-F])")

# Other GTFS-realtime keys that are numbers in the live messages. Any other unknown column stays a string.
EXTRA_KEY_TYPES = {
    "vehicle.trip.direction_id": int,
    "vehicle.trip.schedule_relationship": int,
    "vehicle.occupancy_percentage": int,
    "vehicle.vehicle.wheelchair_accessible": int,
    "vehicle.position.odometer": float,
}


@dataclass
class CsvPolls:
    polls: list[tuple[float, list[dict]]]   # (poll_time, entities), oldest first
    mode: str                               # how poll times were found: '_id', 'poll_time' or 'vehicle.timestamp'
    rows: int
    duplicates: int = 0
    routes: list[str] = field(default_factory=list)


def read_csv_polls(csv_path:Path, cluster_gap:float=5.0, interval:float=DEFAULT_POLL_INTERVAL) -> CsvPolls:
    """Reads a MongoDB CSV export of the live messages and regroups its rows into polls.

    - `_id` column (MongoDB ObjectId): its first 8 hex digits are the insert time. Rows inserted less than
      `cluster_gap` seconds apart belong to the same poll (one insert can straddle a second boundary).
    - else a `poll_time` column (unix seconds) is used as is,
    - else rows are bucketed by `vehicle.timestamp` into `interval`-second polls (approximation).
    Values are converted to the types of the live messages; empty cells are dropped (= field absent).
    """
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise RecordingError(f"CSV file not found: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        columns = reader.fieldnames or []
        rows = [(line, row) for line, row in enumerate(reader, start=2)]
    if not rows:
        raise RecordingError(f"{csv_path.name} has no data rows.")

    # (poll time, sort key, line, row); rows of one MongoDB insert are ordered by their full ObjectId
    if OBJECT_ID_COLUMN in columns:
        mode = OBJECT_ID_COLUMN
        keyed = []
        for line, row in rows:
            object_id = _object_id_hex(row, line)
            keyed.append((int(object_id[:8], 16), object_id, line, row))
    elif POLL_TIME_COLUMN in columns:
        mode = POLL_TIME_COLUMN
        keyed = [(_number(row, POLL_TIME_COLUMN, line), "", line, row) for line, row in rows]
    elif "vehicle.timestamp" in columns:
        mode = "vehicle.timestamp"
        logger.warning(f"{csv_path.name} has no '_id' nor 'poll_time' column: grouping rows into {interval:g}-second polls "
                       f"by vehicle.timestamp. Playback timing is approximate.")
        keyed = [(math.floor(_number(row, "vehicle.timestamp", line) / interval) * interval, "", line, row)
                 for line, row in rows]
    else:
        raise RecordingError(f"{csv_path.name} needs an '_id' (MongoDB export), 'poll_time' or 'vehicle.timestamp' column "
                             f"to know when each position was recorded. Columns found: {', '.join(columns)}")

    keyed.sort(key=lambda item: item[:3])
    gap = cluster_gap if mode == OBJECT_ID_COLUMN else 0
    groups = []     # [first time, last time, entities]
    for time, _, line, row in keyed:
        entity = _to_entity(row, line)
        if groups and time - groups[-1][1] <= gap:
            groups[-1][1] = time
            groups[-1][2].append(entity)
        else:
            groups.append([time, time, [entity]])

    result = CsvPolls(polls=[], mode=mode, rows=len(rows))
    routes = set()
    for first_time, _, entities in groups:
        unique = dedupe_by_vehicle(entities)
        result.duplicates += len(entities) - len(unique)
        routes.update(str(e["vehicle.trip.route_id"]) for e in unique if "vehicle.trip.route_id" in e)
        result.polls.append((float(first_time), unique))
    result.routes = sort_routes(routes)
    return result


def coerce(key:str, value:str):
    """CSV text -> the python type the live message uses for this key (str when unknown)."""
    kind = KEY_TYPES.get(key) or EXTRA_KEY_TYPES.get(key) or str
    if kind is str:
        return value
    if kind is float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(value)
        return number
    try:
        return int(value)
    except ValueError:
        number = float(value)       # "3.0" -> 3
        if not number.is_integer():
            raise
        return int(number)


def _to_entity(row:dict, line:int) -> dict:
    entity = {}
    for key, value in row.items():
        if key in (None, OBJECT_ID_COLUMN, POLL_TIME_COLUMN) or value is None:
            continue
        value = value.strip()
        if value == "" or value.lower() == "nan":
            continue
        try:
            entity[key] = coerce(key, value)
        except ValueError:
            raise RecordingError(f"line {line}: column '{key}' should be a number but is '{value}'.")
    return entity


def _object_id_hex(row:dict, line:int) -> str:
    """The 24 hex digits of the ObjectId, written as plain hex (Compass), ObjectId(...) (mongoexport) or {"$oid": ...}."""
    cell = (row.get(OBJECT_ID_COLUMN) or "").strip()
    match = OBJECT_ID_PATTERN.search(cell)
    if not match:
        raise RecordingError(f"line {line}: '{cell}' in column _id is not a MongoDB ObjectId.")
    return match.group().lower()


def _number(row:dict, column:str, line:int) -> float:
    try:
        return float(row[column])
    except (TypeError, ValueError):
        raise RecordingError(f"line {line}: column '{column}' should be a number but is '{row[column]}'.")
