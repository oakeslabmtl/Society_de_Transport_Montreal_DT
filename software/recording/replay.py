import json
from dataclasses import dataclass
from pathlib import Path
from recording.paths import write_text_atomic
from recording.store import RecordingStore, sort_routes, utc_now_iso
from recording.timeline import Window, local_datetime, utc_offset_seconds

# The replay file is the contract with the Godot viewer (visualization/scripts/replay_player.gd).
# Readers ignore unknown top-level keys; bump VERSION only for breaking changes.
FORMAT = "stm-dt-replay"
VERSION = 1

# Godot needs these to place a bus; observations without them are skipped.
REQUIRED_KEYS = ("vehicle.vehicle.id", "vehicle.trip.route_id", "vehicle.position.latitude", "vehicle.position.longitude")


@dataclass
class ExportResult:
    replay: dict
    frames: int
    vehicles: int
    skipped: int


def build_replay(store:RecordingStore, window:Window, name:str=None, routes:list[str]=None) -> ExportResult:
    """Replay dict for the polls of `window`: one frame (a full snapshot of the recorded routes) per poll."""
    meta = store.meta
    tz = meta["timezone"]
    wanted = {str(route) for route in routes} if routes else None
    entities_by_poll = store.entities_by_poll(window.polls, sorted(wanted) if wanted else None)

    frames, vehicles, seen_routes, skipped = [], set(), set(), 0
    for poll in window.polls:
        data = []
        for entity in entities_by_poll[poll.poll_id]:
            # Also filtered in SQL, except the rows whose route is not in the route_id column (see entities_by_poll)
            if wanted is not None and str(entity.get("vehicle.trip.route_id")) not in wanted:
                continue
            if any(key not in entity for key in REQUIRED_KEYS):
                skipped += 1
                continue
            data.append(entity)
            vehicles.add(entity["vehicle.vehicle.id"])
            seen_routes.add(str(entity["vehicle.trip.route_id"]))
        frames.append({"t": round(poll.poll_time - window.start_time, 3), "data": data})

    route_list = sort_routes(wanted or meta.get("route_ids") or seen_routes)
    replay = {
        "format": FORMAT,
        "version": VERSION,
        "name": name or default_replay_name(window, tz, route_list),
        "created_at": utc_now_iso(),
        "source": {"type": meta.get("source", "unknown"), "detail": meta.get("source_detail", "")},
        "timezone": tz,
        "utc_offset_seconds": utc_offset_seconds(window.start_time, tz),
        "start_time": window.start_time,
        "end_time": window.end_time,
        "duration": window.duration,
        "poll_interval": meta["poll_interval_s"],
        "routes": [int(route) for route in route_list] if all(r.isdigit() for r in route_list) else route_list,
        "vehicle_count": len(vehicles),
        "frames": frames,
    }
    return ExportResult(replay, len(frames), len(vehicles), skipped)


def write_replay(replay:dict, path:Path) -> int:
    """Compact UTF-8 JSON, written atomically. Returns the file size in bytes."""
    return write_text_atomic(json.dumps(replay, separators=(",", ":"), ensure_ascii=False, allow_nan=False), path)


def default_replay_name(window:Window, tz:str, routes:list[str]) -> str:
    """e.g. 'Sat 1 Mar 2025, 15:02-16:02 · routes 45, 121, 140'"""
    start, end = local_datetime(window.start_time, tz), local_datetime(window.end_time, tz)
    day = f"{start:%a} {start.day} {start:%b %Y}"
    if len(routes) > 6:
        route_text = f"{len(routes)} routes"
    elif routes:
        route_text = ("route " if len(routes) == 1 else "routes ") + ", ".join(routes)
    else:
        route_text = "all routes"
    return f"{day}, {start:%H:%M}-{end:%H:%M} · {route_text}"


def default_replay_file_name(recording:Path, window:Window, tz:str) -> str:
    start, end = local_datetime(window.start_time, tz), local_datetime(window.end_time, tz)
    return f"{Path(recording).stem}_{start:%H%M}-{end:%H%M}.json"
