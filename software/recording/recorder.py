import logging
import math
import os
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable
import requests
from recording import RecordingError
from recording.store import RecordingStore, dedupe_by_vehicle, vehicle_key

logger = logging.getLogger("Recorder")

# A fetcher returns (GTFS-rt header timestamp or None, flattened vehicle entities); it raises on failure.
Fetcher = Callable[[], tuple[int | None, list[dict]]]

# Polls in a row that could not be written to the recording (disk full...) before recording stops.
MAX_FAILED_SAVES = 3

MISSING_KEY = ("STM_API_KEY is not set. Recording needs your STM developer API key "
               "(https://portail.developpeurs.stm.info/apihub). Set it, then run the command again:\n"
               "  PowerShell:  $env:STM_API_KEY = \"<your key>\"\n"
               "  cmd:         set STM_API_KEY=<your key>\n"
               "  bash/zsh:    export STM_API_KEY=<your key>")


@dataclass
class PollResult:
    number: int
    poll_time: float
    elapsed: float
    vehicle_count: int = 0
    route_counts: Counter = field(default_factory=Counter)
    error: str | None = None


@dataclass
class RecordResult:
    polls: int = 0
    errors: int = 0
    vehicles: set = field(default_factory=set)
    elapsed: float = 0.0
    interrupted: bool = False


def stm_fetcher(route_ids:list[int], timeout:float) -> tuple[Fetcher, str]:
    """Fetcher for the live STM API, same filtering/flattening as the DataIngestionService. Returns (fetch, url)."""
    if not os.environ.get("STM_API_KEY"):
        raise RecordingError(MISSING_KEY)
    try:
        from communication.protocol import STM_API_URL, STM_API_HEADER
    except KeyError:
        raise RecordingError(MISSING_KEY)
    from communication import stm_api

    def fetch():
        feed = stm_api.fetch_feed(STM_API_URL, STM_API_HEADER, timeout=timeout, check_status=True)
        entities = stm_api.flatten_entities(stm_api.filter_vehicle_entities(feed, route_ids))
        return (feed.header.timestamp if feed.header.HasField("timestamp") else None), entities

    return fetch, STM_API_URL


def describe_error(error:Exception) -> str:
    """Short, key-free description of a failed fetch."""
    if isinstance(error, requests.HTTPError) and error.response is not None:
        status = error.response.status_code
        hint = " (check that STM_API_KEY is valid)" if status in (401, 403) else ""
        return f"HTTP {status} {error.response.reason}{hint}"
    if isinstance(error, requests.Timeout):
        return "the STM API did not answer in time"
    if isinstance(error, requests.ConnectionError):
        return "cannot reach the STM API (no network?)"
    if type(error).__name__ == "DecodeError":
        return "the answer was not a GTFS-realtime feed"
    return f"{type(error).__name__}: {error}"


def record(store:RecordingStore, fetch:Fetcher, interval:float, minutes:float=None,
           on_poll:Callable[[PollResult], None]=None,
           clock=time.monotonic, sleep=time.sleep, now=time.time) -> RecordResult:
    """Polls every `interval` seconds for `minutes` (until Ctrl+C if None), one transaction per poll.

    Ticks are scheduled from the start time (no drift); a fetch slower than the interval skips the missed ticks.
    A failed fetch is stored as an 'error' poll and recording goes on. Ctrl+C stops cleanly.
    A poll that cannot be saved is skipped; after MAX_FAILED_SAVES of them in a row, RecordingError stops the recording.
    """
    result = RecordResult()
    started = clock()
    tick = 0
    failed_saves = 0
    try:
        while minutes is None or tick * interval <= minutes * 60 + 1e-6:
            wait = started + tick * interval - clock()
            if wait > 0:
                sleep(wait)

            poll_time = now()
            poll = PollResult(number=result.polls + 1, poll_time=poll_time, elapsed=clock() - started)
            try:
                feed_timestamp, entities = fetch()
            except Exception as error:
                poll.error = describe_error(error)
                logger.debug(f"Fetch failed: {poll.error}")
                entities = None
            try:
                if entities is None:
                    store.add_error_poll(poll_time, poll.error)
                else:
                    entities = dedupe_by_vehicle(entities)
                    store.add_poll(poll_time, entities, feed_timestamp)
                    poll.vehicle_count = len(entities)
                    poll.route_counts = Counter(str(e.get("vehicle.trip.route_id")) for e in entities)
                    result.vehicles.update(vehicle_key(e) for e in entities)
                failed_saves = 0
            except sqlite3.Error as error:
                # e.g. the file stayed locked by another program, or the disk is full: this poll is lost, not the others
                store.rollback()
                failed_saves += 1
                save_error = f"{type(error).__name__}: {error}"
                poll.error = f"could not save this poll to {store.path.name} ({save_error})"
                logger.warning(f"Poll {poll.number} could not be saved to {store.path}: {save_error}")
            if poll.error:
                result.errors += 1
            result.polls += 1
            if failed_saves >= MAX_FAILED_SAVES:
                raise RecordingError(f"Recording stopped: the last {failed_saves} polls could not be saved to {store.path} "
                                     f"({save_error}). Check that the disk is not full and that no other program keeps "
                                     f"the file open. What was saved before is kept ({result.polls - result.errors} ok polls).")
            if on_poll:
                on_poll(poll)

            next_tick = max(tick + 1, math.ceil((clock() - started) / interval))
            if next_tick > tick + 1:
                logger.warning(f"Poll took longer than the {interval:g} s interval; skipped {next_tick - tick - 1} poll(s).")
            tick = next_tick
    except KeyboardInterrupt:
        result.interrupted = True
    result.elapsed = clock() - started
    return result
