from collections import Counter
from recording.paths import display_path
from recording.store import RecordingStore, sort_routes
from recording.timeline import (Window, best_windows, contiguous_spans, find_gaps, format_clock, format_duration,
                                format_span, max_gap)

# GTFS-realtime VehiclePosition.OccupancyStatus
OCCUPANCY_LABELS = {
    0: "EMPTY",
    1: "MANY_SEATS_AVAILABLE",
    2: "FEW_SEATS_AVAILABLE",
    3: "STANDING_ROOM_ONLY",
    4: "CRUSHED_STANDING_ROOM_ONLY",
    5: "FULL",
    6: "NOT_ACCEPTING_PASSENGERS",
    7: "NO_DATA_AVAILABLE",
    8: "NOT_BOARDABLE",
}


def describe(store:RecordingStore, minutes:float=60, top:int=3) -> tuple[list[str], Window | None]:
    """Human summary of a recording, and its best gap-free window of `minutes` (None if there is none)."""
    meta = store.meta
    tz = meta.get("timezone", "America/Montreal")
    interval = meta.get("poll_interval_s", 15.0)
    polls = store.polls()
    ok = [poll for poll in polls if poll.status == "ok"]
    failed = [poll for poll in polls if poll.status != "ok"]
    lines = []

    def add(label, text=""):
        lines.append(f"{label:<12}{text}".rstrip())

    add("Name", meta.get("name", store.path.stem))
    add("File", f"{display_path(store.path)} ({store.path.stat().st_size / 1e6:.1f} MB)")
    add("Source", f"{meta.get('source', '?')} ({meta.get('source_detail', '')})")
    add("Routes", ", ".join(sort_routes(meta.get("route_ids") or [])) or "all routes")
    if not ok:
        add("Polls", f"{len(polls)} ({len(failed)} failed), no data yet")
        return lines, None

    span = Window(ok)
    add("Recorded", f"{format_span(span, tz, seconds=True)} {tz} ({format_duration(span.duration)})")
    add("Polls", f"{len(polls):,} ({len(ok):,} ok, {len(failed)} failed), every {interval:g} s")
    for message, count in Counter(poll.error for poll in failed).most_common(3):
        add("", f"failed {count}x: {message}")

    gaps = find_gaps(ok, interval)
    add("Gaps", f"{len(gaps)} longer than {max_gap(interval):g} s" + (":" if gaps else ""))
    for before, after in gaps:
        add("", f"{format_clock(before, tz, True)} -> {format_clock(after, tz, True)}  ({format_duration(after - before)} without data)")

    add("Vehicles", f"{store.vehicle_count()} distinct, {span.observations:,} positions")
    route_counts = store.route_counts()
    add("Per route", "   ".join(f"{route}: {route_counts[route]:,}" for route in sort_routes(r for r in route_counts if r)))

    add("Occupancy")
    occupancy = store.occupancy_counts()
    total = sum(occupancy.values())
    for status, count in occupancy.items():
        label = "(not reported)" if status is None else f"{status} {OCCUPANCY_LABELS.get(status, 'UNKNOWN')}"
        add("", f"{label:<30}{count:>9,}  {100 * count / total:5.1f} %")

    windows = best_windows(ok, minutes, interval, limit=top)
    if windows:
        lines.append(f"Best {minutes:g}-min windows (most bus positions, no gaps):")
        for number, window in enumerate(windows, start=1):
            vehicle_text = f"{window.observations / len(window.polls):.0f} buses on average"
            lines.append(f"  {number}. {format_span(window, tz)}   {window.observations:,} positions, {vehicle_text}")
    else:
        longest = max(contiguous_spans(ok, interval), key=lambda s: s.duration)
        lines.append(f"No gap-free {minutes:g}-min window; the longest gap-free stretch is "
                     f"{format_span(longest, tz)} ({format_duration(longest.duration)}).")
    return lines, (windows[0] if windows else None)
