from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from recording import RecordingError
from recording.store import Poll

# A pause between two successful polls longer than GAP_FACTOR x the poll interval is a gap.
GAP_FACTOR = 2.5


@dataclass
class Window:
    polls: list[Poll]

    @property
    def start_time(self) -> float:
        return self.polls[0].poll_time

    @property
    def end_time(self) -> float:
        return self.polls[-1].poll_time

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time

    @property
    def observations(self) -> int:
        return sum(poll.vehicle_count for poll in self.polls)


def max_gap(interval:float) -> float:
    return GAP_FACTOR * interval


def find_gaps(polls:list[Poll], interval:float) -> list[tuple[float, float]]:
    """(last poll before, first poll after) for every gap between successful polls."""
    return [(a.poll_time, b.poll_time) for a, b in zip(polls, polls[1:]) if b.poll_time - a.poll_time > max_gap(interval)]


def contiguous_spans(polls:list[Poll], interval:float) -> list[Window]:
    spans, current = [], []
    for poll in polls:
        if current and poll.poll_time - current[-1].poll_time > max_gap(interval):
            spans.append(Window(current))
            current = []
        current.append(poll)
    if current:
        spans.append(Window(current))
    return spans


def best_windows(polls:list[Poll], minutes:float, interval:float, limit:int=1) -> list[Window]:
    """Gap-free windows of `minutes` with the most vehicle observations (non-overlapping, best first, ties -> earliest)."""
    seconds = minutes * 60
    candidates = []
    for span in contiguous_spans(polls, interval):
        times = [poll.poll_time for poll in span.polls]
        sums = [0]
        for poll in span.polls:
            sums.append(sums[-1] + poll.vehicle_count)
        last = 0
        for first in range(len(times)):
            last = max(last, first)
            while last + 1 < len(times) and times[last + 1] - times[first] <= seconds:
                last += 1
            # The window must really reach the requested length, not stop early at the end of the span
            if times[first] + seconds - times[last] <= max_gap(interval):
                candidates.append((sums[last + 1] - sums[first], times[first], first, last, span))

    candidates.sort(key=lambda c: (-c[0], c[1]))
    chosen = []
    for _, _, first, last, span in candidates:
        window = Window(span.polls[first:last + 1])
        if all(window.end_time < other.start_time or window.start_time > other.end_time for other in chosen):
            chosen.append(window)
            if len(chosen) == limit:
                break
    return chosen


def select_window(polls:list[Poll], interval:float, tz:str, start:float=None, minutes:float=None) -> tuple[Window, list[str]]:
    """Export window rules. Returns the window and warnings for the user."""
    if not polls:
        raise RecordingError("This recording has no successful polls, there is nothing to export.")
    if minutes is not None and minutes <= 0:
        raise RecordingError("--minutes must be greater than 0.")

    if start is None and minutes is None:
        window = Window(polls)
        warnings = gap_warnings(polls, interval, tz)
    elif start is None:
        windows = best_windows(polls, minutes, interval)
        if not windows:
            longest = max(contiguous_spans(polls, interval), key=lambda span: span.duration)
            raise RecordingError(
                f"There is no gap-free stretch of {minutes:g} min in this recording. The longest one is "
                f"{format_span(longest, tz)} ({format_duration(longest.duration)}); "
                f"use a smaller --minutes, or pick a window with --start (gaps are then allowed).")
        window, warnings = windows[0], []
    else:
        end = float("inf") if minutes is None else start + minutes * 60
        selected = [poll for poll in polls if start <= poll.poll_time <= end]
        if not selected:
            raise RecordingError(f"No data in the requested window. This recording covers {format_span(Window(polls), tz)}; "
                                 f"run `info` to see its gaps.")
        window = Window(selected)
        warnings = gap_warnings(selected, interval, tz)
        # The edges of the window can fall outside the data, or in a gap, too
        missing = []
        if window.start_time - start > max_gap(interval):
            where = "starts before the recording" if start < polls[0].poll_time else "starts in a gap"
            missing.append(f"{where} (no data {format_clocks(start, window.start_time, tz)})")
        if minutes is not None and end - window.end_time > max_gap(interval):
            if end > polls[-1].poll_time:
                missing.append(f"goes past the end of the recording (no data after {format_clock(window.end_time, tz)})")
            else:
                missing.append(f"ends in a gap (no data {format_clocks(window.end_time, end, tz)})")
        if missing:
            warnings.append(f"The window {' and '.join(missing)}; the replay is shorter than requested: "
                            f"{format_span(window, tz)} ({format_duration(window.duration)}).")
    return window, warnings + clock_change_warnings(window, tz)


def gap_warnings(polls:list[Poll], interval:float, tz:str) -> list[str]:
    gaps = find_gaps(polls, interval)
    if not gaps:
        return []
    before, after = max(gaps, key=lambda gap: gap[1] - gap[0])
    longest = f"{format_clock(before, tz)} -> {format_clock(after, tz)}, {format_duration(after - before)}"
    return [f"The window contains {len(gaps)} gap(s) in the data (longest: {longest}); "
            f"buses will freeze or disappear there."]


def clock_change_warnings(window:Window, tz:str) -> list[str]:
    """The replay has one UTC offset (the one at its start): its clock is wrong after a daylight saving time change."""
    offset = utc_offset_seconds(window.start_time, tz)
    if utc_offset_seconds(window.end_time, tz) == offset:
        return []
    change = next(poll for poll in window.polls if utc_offset_seconds(poll.poll_time, tz) != offset)
    hours = abs(utc_offset_seconds(change.poll_time, tz) - offset) / 3600
    return [f"The window crosses a daylight saving time change: from {format_clock(change.poll_time, tz)} on, "
            f"the clock shown by the viewer is {hours:g} h off (bus movements are not affected)."]


def parse_local_time(text:str, tz:str, polls:list[Poll]) -> float:
    """'HH:MM' (on the recording's date) or 'YYYY-MM-DD HH:MM' in the recording's timezone -> unix seconds."""
    zone = ZoneInfo(tz)
    text = text.strip().replace("T", " ")
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=zone).timestamp()
        except ValueError:
            pass
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            clock = datetime.strptime(text, pattern).time()
            break
        except ValueError:
            pass
    else:
        raise RecordingError(f"Can't read --start '{text}'. Use HH:MM (e.g. 15:30) or 'YYYY-MM-DD HH:MM'.")

    first_day = local_datetime(polls[0].poll_time, tz).date()
    moment = datetime.combine(first_day, clock, tzinfo=zone)
    # Recordings that cross midnight: an early HH:MM belongs to the next day
    if moment.timestamp() < polls[0].poll_time and (moment + timedelta(days=1)).timestamp() <= polls[-1].poll_time:
        moment += timedelta(days=1)
    return moment.timestamp()


def start_option(timestamp:float, tz:str, polls:list[Poll]) -> str:
    """--start value for `timestamp`: HH:MM, or "YYYY-MM-DD HH:MM" when the recording spans several dates
    (an HH:MM alone could then mean another day, see parse_local_time)."""
    moment = local_datetime(timestamp, tz)
    if local_datetime(polls[0].poll_time, tz).date() == local_datetime(polls[-1].poll_time, tz).date():
        return f"{moment:%H:%M}"
    return f'"{moment:%Y-%m-%d %H:%M}"'


def local_datetime(timestamp:float, tz:str) -> datetime:
    return datetime.fromtimestamp(timestamp, ZoneInfo(tz))


def utc_offset_seconds(timestamp:float, tz:str) -> int:
    return int(local_datetime(timestamp, tz).utcoffset().total_seconds())


def format_clock(timestamp:float, tz:str, seconds:bool=False) -> str:
    return local_datetime(timestamp, tz).strftime("%H:%M:%S" if seconds else "%H:%M")


def format_clocks(start:float, end:float, tz:str) -> str:
    """'10:30 -> 11:03', with seconds when both fall in the same minute ('11:03:00 -> 11:03:39')."""
    seconds = format_clock(start, tz) == format_clock(end, tz)
    return f"{format_clock(start, tz, seconds)} -> {format_clock(end, tz, seconds)}"


def format_span(window:Window, tz:str, seconds:bool=False) -> str:
    """'Sat 2025-03-01 15:02 -> 16:02' (the date is repeated only when the span crosses midnight)."""
    start, end = local_datetime(window.start_time, tz), local_datetime(window.end_time, tz)
    clock = "%H:%M:%S" if seconds else "%H:%M"
    end_format = clock if start.date() == end.date() else f"%a %Y-%m-%d {clock}"
    return f"{start.strftime(f'%a %Y-%m-%d {clock}')} -> {end.strftime(end_format)}"


def format_duration(seconds:float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min" if minutes else f"{seconds:.0f} s"
    return f"{minutes // 60} h {minutes % 60:02d} min"
