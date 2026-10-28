import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from recording import RecordingError

SCHEMA_VERSION = "1"
TIMEZONE = "America/Montreal"
# How long a connection waits for another one to release the file (e.g. a recording and an `export` at the same time)
LOCK_TIMEOUT_S = 60.0
# Polls read per query: each query holds a read lock that blocks the recorder's commits, so keep them short
POLLS_PER_QUERY = 20

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE polls (
    poll_id INTEGER PRIMARY KEY,
    poll_time REAL NOT NULL,
    feed_timestamp INTEGER,
    vehicle_count INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    error TEXT
);
CREATE INDEX idx_polls_time ON polls(poll_time);
CREATE TABLE vehicle_positions (
    poll_id INTEGER NOT NULL REFERENCES polls(poll_id),
    vehicle_id TEXT NOT NULL,
    entity_id TEXT, route_id TEXT, trip_id TEXT, trip_start_time TEXT, trip_start_date TEXT,
    latitude REAL, longitude REAL, bearing REAL, speed REAL,
    current_stop_sequence INTEGER, current_status INTEGER, vehicle_timestamp INTEGER,
    occupancy_status INTEGER, congestion_level INTEGER,
    extra_json TEXT,
    PRIMARY KEY (poll_id, vehicle_id)
);
CREATE INDEX idx_vp_route_ts ON vehicle_positions(route_id, vehicle_timestamp);
"""

# (column, flattened entity key, python type), in the key order of the live messages.
FIELDS = [
    ("entity_id", "id", str),
    ("trip_id", "vehicle.trip.trip_id", str),
    ("trip_start_time", "vehicle.trip.start_time", str),
    ("trip_start_date", "vehicle.trip.start_date", str),
    ("route_id", "vehicle.trip.route_id", str),
    ("latitude", "vehicle.position.latitude", float),
    ("longitude", "vehicle.position.longitude", float),
    ("bearing", "vehicle.position.bearing", float),
    ("speed", "vehicle.position.speed", float),
    ("current_stop_sequence", "vehicle.current_stop_sequence", int),
    ("current_status", "vehicle.current_status", int),
    ("vehicle_timestamp", "vehicle.timestamp", int),
    ("congestion_level", "vehicle.congestion_level", int),
    ("vehicle_id", "vehicle.vehicle.id", str),
    ("occupancy_status", "vehicle.occupancy_status", int),
]
KEY_TYPES = {key: kind for _, key, kind in FIELDS}
COLUMN_BY_KEY = {key: column for column, key, _ in FIELDS}
VEHICLE_ID_KEY = "vehicle.vehicle.id"
# Stored in extra_json when an entity had no vehicle.vehicle.id (vehicle_id then holds the entity id).
NO_VEHICLE_ID = "~no_vehicle_id"
COLUMNS = [column for column, _, _ in FIELDS] + ["extra_json"]


@dataclass
class Poll:
    poll_id: int
    poll_time: float
    feed_timestamp: int | None
    vehicle_count: int
    status: str
    error: str | None


def entity_to_row(entity:dict) -> dict:
    """Flattened entity -> column values. Keys without a column (or with an unexpected type) go to extra_json."""
    row, extra = {}, {}
    for key, value in entity.items():
        if key in COLUMN_BY_KEY and type(value) is KEY_TYPES[key]:
            row[COLUMN_BY_KEY[key]] = value
        else:
            extra[key] = value

    if "vehicle_id" not in row:
        vehicle_id = entity.get(VEHICLE_ID_KEY, entity.get("id"))
        if vehicle_id is None:
            raise RecordingError(f"Vehicle position without 'vehicle.vehicle.id' nor 'id': {entity}")
        row["vehicle_id"] = str(vehicle_id)
        if VEHICLE_ID_KEY not in entity:
            extra[NO_VEHICLE_ID] = True

    row["extra_json"] = json.dumps(extra, ensure_ascii=False) if extra else None
    return row


def row_to_entity(row:sqlite3.Row) -> dict:
    """Inverse of entity_to_row: gives back exactly the original flattened entity (keys and types)."""
    extra = json.loads(row["extra_json"]) if row["extra_json"] else {}
    entity = {}
    for column, key, _ in FIELDS:
        if row[column] is not None and not (column == "vehicle_id" and extra.get(NO_VEHICLE_ID)):
            entity[key] = row[column]
    extra.pop(NO_VEHICLE_ID, None)
    entity.update(extra)
    return entity


def vehicle_key(entity:dict) -> str:
    return str(entity.get(VEHICLE_ID_KEY, entity.get("id")))


def dedupe_by_vehicle(entities:list[dict]) -> list[dict]:
    """One entity per vehicle: keeps the most recent vehicle.timestamp (first one on ties), in first-seen order."""
    kept = {}
    for entity in entities:
        key = vehicle_key(entity)
        previous = kept.get(key)
        if previous is None or _timestamp(entity) > _timestamp(previous):
            kept[key] = entity
    return list(kept.values())


def sort_routes(routes) -> list[str]:
    """Route ids as strings, numeric ones first in numeric order: ['45', '121', '140']."""
    routes = {str(route) for route in routes}
    return sorted(routes, key=lambda route: (0, int(route), "") if route.isdigit() else (1, 0, route))


def _timestamp(entity:dict) -> int:
    value = entity.get("vehicle.timestamp")
    return value if isinstance(value, (int, float)) else 0


class RecordingStore:

    def __init__(self, path:Path, connection:sqlite3.Connection):
        self.path = Path(path)
        self.connection = connection
        self.connection.row_factory = sqlite3.Row


    @classmethod
    def create(cls, path:Path, meta:dict, overwrite:bool=False) -> "RecordingStore":
        path = Path(path)
        if path.exists():
            if not overwrite:
                raise RecordingError(f"{path} already exists. Choose another --name/--out, or add --force to replace it.")
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)

        # A commit waits (up to LOCK_TIMEOUT_S) for an `info`/`export` reading the file instead of failing
        store = cls(path, sqlite3.connect(path, timeout=LOCK_TIMEOUT_S))
        store.connection.executescript(SCHEMA)
        values = {"schema_version": SCHEMA_VERSION, "created_at": utc_now_iso(), "timezone": TIMEZONE, **meta}
        values["route_ids"] = json.dumps([str(route) for route in values.get("route_ids", [])])
        store.connection.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                                     [(key, str(value)) for key, value in values.items()])
        store.connection.commit()
        return store


    @classmethod
    def open(cls, path:Path) -> "RecordingStore":
        path = Path(path)
        if not path.is_file():
            raise RecordingError(f"Recording not found: {path}")
        # Read-only: safe to run `info`/`export` while a recording is still in progress
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=LOCK_TIMEOUT_S)
        store = cls(path, connection)
        try:
            version = store.meta.get("schema_version")
        except sqlite3.DatabaseError:
            connection.close()
            raise RecordingError(f"{path} is not a recording made by `python -m recording` (not an SQLite file with a meta table).")
        if version != SCHEMA_VERSION:
            connection.close()
            raise RecordingError(f"{path} uses recording schema version {version}; this tool reads version {SCHEMA_VERSION}.")
        return store


    def close(self):
        self.connection.close()


    def __enter__(self):
        return self


    def __exit__(self, *exc):
        self.close()


    @property
    def meta(self) -> dict:
        meta = {row["key"]: row["value"] for row in self.connection.execute("SELECT key, value FROM meta")}
        if "route_ids" in meta:
            meta["route_ids"] = json.loads(meta["route_ids"])
        if "poll_interval_s" in meta:
            meta["poll_interval_s"] = float(meta["poll_interval_s"])
        return meta


    def add_poll(self, poll_time:float, entities:list[dict], feed_timestamp:int=None, commit:bool=True) -> int:
        """Stores one successful poll (its own transaction unless commit=False)."""
        rows = [entity_to_row(entity) for entity in entities]
        cursor = self.connection.execute(
            "INSERT INTO polls (poll_time, feed_timestamp, vehicle_count, status) VALUES (?, ?, ?, 'ok')",
            (poll_time, feed_timestamp, len({row["vehicle_id"] for row in rows})))
        poll_id = cursor.lastrowid
        self.connection.executemany(
            f"INSERT OR REPLACE INTO vehicle_positions (poll_id, {', '.join(COLUMNS)}) "
            f"VALUES (?, {', '.join('?' * len(COLUMNS))})",
            [(poll_id, *(row.get(column) for column in COLUMNS)) for row in rows])
        if commit:
            self.connection.commit()
        return poll_id


    def add_error_poll(self, poll_time:float, error:str) -> int:
        """A failed fetch: a gap in the recording, not "zero buses"."""
        cursor = self.connection.execute(
            "INSERT INTO polls (poll_time, vehicle_count, status, error) VALUES (?, 0, 'error', ?)", (poll_time, error))
        self.connection.commit()
        return cursor.lastrowid


    def commit(self):
        self.connection.commit()


    def rollback(self):
        """Drops what a failed add_poll left uncommitted, so it is not saved with the next poll."""
        try:
            self.connection.rollback()
        except sqlite3.Error:
            pass


    def polls(self, status:str=None) -> list[Poll]:
        query = "SELECT poll_id, poll_time, feed_timestamp, vehicle_count, status, error FROM polls"
        args = ()
        if status:
            query += " WHERE status = ?"
            args = (status,)
        return [Poll(*row) for row in self.connection.execute(query + " ORDER BY poll_time, poll_id", args)]


    def entities_by_poll(self, polls:list[Poll]) -> dict[int, list[dict]]:
        """Entities of the given polls, newest vehicle.timestamp first (the order of the live messages)."""
        if not polls:
            return {}
        wanted = sorted({poll.poll_id for poll in polls})
        result = {poll_id: [] for poll_id in wanted}
        # Short queries, fully fetched before converting: a recording in progress can commit in between
        for first in range(0, len(wanted), POLLS_PER_QUERY):
            chunk = wanted[first:first + POLLS_PER_QUERY]
            rows = self.connection.execute(
                f"SELECT * FROM vehicle_positions WHERE poll_id IN ({', '.join('?' * len(chunk))}) "
                "ORDER BY poll_id, vehicle_timestamp DESC, rowid", chunk).fetchall()
            for row in rows:
                result[row["poll_id"]].append(row_to_entity(row))
        return result


    def route_counts(self) -> dict[str, int]:
        rows = self.connection.execute("SELECT route_id, COUNT(*) FROM vehicle_positions GROUP BY route_id")
        return {route: count for route, count in rows}


    def occupancy_counts(self) -> dict[int, int]:
        rows = self.connection.execute(
            "SELECT occupancy_status, COUNT(*) FROM vehicle_positions GROUP BY occupancy_status ORDER BY occupancy_status")
        return {status: count for status, count in rows}


    def vehicle_count(self) -> int:
        return self.connection.execute("SELECT COUNT(DISTINCT vehicle_id) FROM vehicle_positions").fetchone()[0]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
