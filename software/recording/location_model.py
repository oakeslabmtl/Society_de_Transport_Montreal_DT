"""The location model: an occupancy forecast that learns, from the replay itself, how statuses change with where the bus is.

Timestral (forecast.py) only sees each bus's own occupancy history. This model also knows where the bus is: how far
along its trip, whether a metro station is just ahead, and how the bus ahead on the same line changed after this stop.
It is a multinomial logistic regression over the ranks of the statuses found (see forecast.py), trained walk-forward:
before the forecasts made at a time t, it is refitted on the earlier forecast points whose outcome is known at t, so a
stored forecast never uses what happened after it. Until it has learned from enough outcomes, it forecasts "no change".

It uses the plan, format and scores of forecast.py, so its forecast and Timestral's can be compared on a replay.
Needs numpy only (no Timestral or PyTorch). The trip position needs vehicle.trip.direction_id and
vehicle.current_stop_sequence; stations and the bus ahead also need vehicle.stop_id and the stop lists of route-stops
(visualization/assets/route_stops.json). Without them, the model only uses the bus's status history.
"""
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple
from recording import RecordingError
from recording.forecast import (HIGH, LOW, MEDIAN, QUANTILES, ForecastPlan, ForecastResult, OccupancyGrid, forecast_result,
                                frame_slots, score, vehicle_id)     # first: explains how to install numpy when it is missing
from recording.paths import display_path
import numpy as np

MODEL = "recording/location-logit"
MODEL_NAME = "Location model"

HORIZON_UNIT = 30.0                 # s: log_horizon = ln(seconds ahead / 30), horizon = seconds ahead / 600
HORIZON_SCALE = 600.0
PROGRESS_WINDOW = 300.0             # s of the bus's recent progress along its trip, to project where it will be
MIN_PROGRESS = 60.0                 # s: with less of the current trip seen, the projection holds the current stop
TRIP_START_STOPS = 4                # trip_start: current_stop_sequence <= 4
TRIP_END_STOPS = 3                  # trip_end: at most 3 stops before the estimated last stop of the trip
HISTORY_CAP = 40.0                  # min (never_changed_x_history)
CHANGE_CAP = 20.0                   # min (time_since_change)
STATION_PREFIX = "Station "         # a metro station's stop name in the STM GTFS, e.g. "Station Berri-UQAM"
INSET = 0.49                        # the 80% range ends stay inside the bin of their status
CHUNK = 32768                       # rows per block when fitting (bounds memory on long replays)

# Features of a forecast point, all known at the forecast time. x_change multiply |d|: how likely a change is;
# x_direction multiply d: which way (d = candidate rank - rank at the forecast time).
FEATURES = {
    "log_horizon": "ln(seconds ahead / 30)",
    "horizon": "seconds ahead / 600",
    "never_changed": "1 if the bus's status has not changed since it was first seen",
    "never_changed_x_history": "never_changed x min(minutes since first seen, 40) / 10",
    "time_since_change": "min(minutes since its last status change, 20) / 10 (0 if none seen)",
    "station_ahead": "1 if a metro station is on its line from the current stop to where it is projected to be",
    "trip_start": f"1 if current_stop_sequence <= {TRIP_START_STOPS} (start of a trip)",
    "trip_end": f"1 if within {TRIP_END_STOPS} stops of the trip's last stop (the highest stop sequence seen so far "
                f"on this line and direction)",
    "trip_boundary": "1 if it is projected to reach that last stop by then (a new trip, often emptier, may start)",
    "bus_ahead_change": "the status change of the previous bus on the line after it left this stop, over as long, "
                        "until now (0 if none)",
    "bus_ahead_abs_change": "|bus_ahead_change|",
    "no_bus_ahead": "1 if no earlier bus of the line is known to have passed this stop",
    "position_unknown": "1 if the trip position or the stop's place on the line is unknown",
    "trip_progress": "current_stop_sequence / the trip's last stop - 0.5",
}
CHANGE_FEATURES = ["log_horizon", "horizon", "never_changed", "never_changed_x_history", "time_since_change",
                   "station_ahead", "trip_start", "trip_end", "trip_boundary", "bus_ahead_abs_change", "no_bus_ahead",
                   "position_unknown"]
DIRECTION_FEATURES = ["bus_ahead_change", "station_ahead", "trip_start", "trip_end", "trip_boundary", "trip_progress"]
FEATURE_NAMES = list(FEATURES)

METHOD = ("Walk-forward multinomial logistic regression: before each forecast time, it is refitted on the earlier "
          "forecast points whose outcome is known. For a bus at rank r, a candidate rank c (d = c - r) has the utility "
          "alpha_c + |d| (gamma_r + sum beta_f x_f) + jump [|d| >= 2] + d sum delta_f x_f: beta (change features) says "
          "how a feature makes a change more or less likely, delta (direction features) which way. Ridge penalty "
          "lambda on beta and delta. The median is the median status of the predicted distribution; the ends of the 80% "
          "range are its 10% and 90% quantiles, spread uniformly within each status. 'No change' while too few outcomes "
          "are known.")

logger = logging.getLogger("LocationModel")


@dataclass(frozen=True)
class LocationModelSettings:
    """Ridge penalty and how much a forecast point needs to have learned before the model replaces "no change"."""
    lam: float = 1e-4                   # on the change and direction coefficients (1e-6 on the others)
    min_points: int = 2000              # past forecast points with a known outcome
    min_buses: int = 20                 # ... from at least this many buses
    min_points_per_point: int = 200     # ... of which this many at the same horizon
    min_points_per_status: int = 200    # ... and this many from buses at the same status at the forecast time


@dataclass
class TripGrid:
    """Where every vehicle is on each slot of the occupancy grid (both are built on forecast.frame_slots())."""
    vehicles: list[str]                 # sorted vehicle ids: the row of each array
    rank: np.ndarray                    # (vehicles, slots) the forecast's ranks, NaN = no data
    trip: np.ndarray                    # (vehicles, slots) trip number, -1 = unknown
    group: np.ndarray                   # (vehicles, slots) line + direction number, -1 = unknown
    seq: np.ndarray                     # (vehicles, slots) vehicle.current_stop_sequence, NaN = unknown
    stop: np.ndarray                    # (vehicles, slots) place of vehicle.stop_id in its line's stop list, -1 = unknown
    stations: list                      # per group: bool array, is each stop of its list a metro station (None: no list)

    @property
    def slots(self) -> int:
        return self.rank.shape[1]


def load_route_stops(path:Path) -> dict | None:
    """The stop lists made by route-stops, or None when the file is missing, or unreadable (with a warning)."""
    path = Path(path)
    try:
        route_stops = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        logger.warning(f"Stop lists not read ({display_path(path)}: {error}): stations and the bus ahead are unknown.")
        return None
    if not isinstance(route_stops, dict) or not isinstance(route_stops.get("routes"), dict):
        logger.warning(f"{display_path(path)} has no stop lists: stations and the bus ahead are unknown.")
        return None
    return route_stops


def _stop_list(route_stops:dict | None, route:str, direction:int) -> list[str] | None:
    try:
        stops = route_stops["routes"][route]["directions"][str(direction)]["stops"]
    except (KeyError, TypeError):
        return None
    return [str(stop) for stop in stops] if isinstance(stops, list) and stops else None


def _is_station(route_stops:dict, stop:str) -> bool:
    info = (route_stops.get("stops") or {}).get(stop)
    return isinstance(info, dict) and str(info.get("name", "")).startswith(STATION_PREFIX)


def build_trip_grid(replay:dict, grid:OccupancyGrid, route_stops:dict | None) -> TripGrid:
    """Trip, line + direction, stop sequence and place on the line's stop list of every vehicle on each grid slot."""
    vehicles = sorted(grid.levels)
    row_of = {vehicle: row for row, vehicle in enumerate(vehicles)}
    shape = (len(vehicles), grid.slots)
    rank = np.stack([grid.levels[vehicle].astype(np.float64) for vehicle in vehicles]) if vehicles else np.empty(shape)
    trip, group, stop = (np.full(shape, -1, dtype=np.int64) for _ in range(3))
    seq = np.full(shape, np.nan)
    trips: dict[str, int] = {}
    groups: dict[tuple[str, int], int] = {}
    places: list[dict | None] = []          # per group: stop id -> place in its list (the last one if listed twice)
    stations: list[np.ndarray | None] = []
    for slot, frame in frame_slots(replay, grid.step):
        for entity in frame.get("data") or ():
            vehicle = vehicle_id(entity)
            if vehicle is None:
                continue
            row = row_of[vehicle]
            trip_id = entity.get("vehicle.trip.trip_id")
            trip[row, slot] = -1 if trip_id is None else trips.setdefault(str(trip_id), len(trips))
            sequence = entity.get("vehicle.current_stop_sequence")
            ok = isinstance(sequence, (int, float)) and not isinstance(sequence, bool) and math.isfinite(sequence)
            seq[row, slot] = float(sequence) if ok else np.nan
            group[row, slot] = stop[row, slot] = -1
            route, direction = entity.get("vehicle.trip.route_id"), entity.get("vehicle.trip.direction_id")
            if route is None or direction is None or isinstance(direction, bool):
                continue
            try:
                key = (str(route), int(direction))
            except (TypeError, ValueError):
                continue
            if key not in groups:
                groups[key] = len(groups)
                stops = _stop_list(route_stops, *key)
                places.append({stop_id: place for place, stop_id in enumerate(stops)} if stops else None)
                stations.append(np.array([_is_station(route_stops, s) for s in stops]) if stops else None)
            number = groups[key]
            group[row, slot] = number
            stop_id = entity.get("vehicle.stop_id")
            if stop_id is not None and places[number] is not None:
                stop[row, slot] = places[number].get(str(stop_id), -1)
    return TripGrid(vehicles, rank, trip, group, seq, stop, stations)


def _forward_fill(values:np.ndarray) -> np.ndarray:
    """Each row's last known value at every slot (NaN before the first one)."""
    known = np.isfinite(values)
    last = np.where(known, np.arange(values.shape[1])[None, :], 0)
    np.maximum.accumulate(last, axis=1, out=last)
    filled = np.take_along_axis(values, last, axis=1)
    filled[~np.maximum.accumulate(known, axis=1)] = np.nan
    return filled


def _causal_last_stop(tg:TripGrid, groups:np.ndarray, origins:np.ndarray) -> np.ndarray:
    """Highest current_stop_sequence seen on each forecast's line + direction (any bus) at slots strictly before its
    forecast time: where that direction's trips end, as known then. NaN if none yet."""
    count = int(tg.group.max()) + 1 if tg.group.size else 0
    best = np.full((max(count, 1), tg.slots + 1), -np.inf)       # column s + 1: slot s
    rows, slots = np.nonzero((tg.group >= 0) & np.isfinite(tg.seq))
    np.maximum.at(best, (tg.group[rows, slots], slots + 1), tg.seq[rows, slots])
    np.maximum.accumulate(best, axis=1, out=best)
    out = np.where(groups >= 0, best[np.maximum(groups, 0), origins], -np.inf)      # column origin: slots < origin
    return np.where(np.isfinite(out), out, np.nan)


def _progress_rate(trip:np.ndarray, values:np.ndarray, known:np.ndarray, slot:int, window:int, minimum:int) -> float:
    """Progress per slot (never negative) from the earliest known value of the current trip in the last `window`
    slots; 0 when that value is less than `minimum` slots old."""
    for start in range(max(0, slot - window), slot):
        if trip[start] == trip[slot] and known[start]:
            return max(0.0, (values[slot] - values[start]) / (slot - start)) if slot - start >= minimum else 0.0
    return 0.0


def _bus_ahead(tg:TripGrid, fv:np.ndarray, fo:np.ndarray, point_slots:int, points:int) -> np.ndarray:
    """(forecasts, points) change of the bus ahead, NaN if none: among the other buses of the line + direction that
    passed this bus's stop before the forecast time, the last one to pass; its status change from its last known status
    before passing, over the point's horizon, cut at the slot before the forecast time."""
    # Per group: one segment per (vehicle, trip) seen on a known stop, in vehicle id order, then by first slot
    tables = {}
    for row in range(len(tg.vehicles)):
        on_stop = np.nonzero((tg.group[row] >= 0) & (tg.stop[row] >= 0))[0]
        keys: dict[tuple[int, int], list[int]] = {}
        for slot in on_stop:
            keys.setdefault((int(tg.group[row, slot]), int(tg.trip[row, slot])), []).append(int(slot))
        for (number, _), slots in keys.items():
            slots = np.array(slots)
            places, ranks = tg.stop[row, slots], tg.rank[row, slots]
            stops = len(tg.stations[number])
            # First position past each stop s (its place first exceeds s), and the last known status before it
            passed = np.searchsorted(np.maximum.accumulate(places), np.arange(stops), side="right")
            known = np.where(np.isfinite(ranks), np.arange(len(slots)), -1)
            np.maximum.accumulate(known, out=known)
            before = known[np.maximum(passed - 1, 0)]
            valid = (passed < len(slots)) & (passed > 0) & (before >= 0)
            pass_slot = np.where(valid, slots[np.minimum(passed, len(slots) - 1)], np.iinfo(np.int64).max)
            table = tables.setdefault(number, {"vehicle": [], "pass": [], "level": [], "slot": []})
            table["vehicle"].append(row)
            table["pass"].append(pass_slot)
            table["level"].append(np.where(valid, ranks[np.maximum(before, 0)], np.nan))
            table["slot"].append(np.where(valid, slots[np.maximum(before, 0)], 0))
    tables = {number: {key: np.array(value) for key, value in table.items()} for number, table in tables.items()}

    filled = _forward_fill(tg.rank)
    steps = point_slots * np.arange(1, points + 1)
    out = np.full((fv.size, points), np.nan)
    for n, (row, origin) in enumerate(zip(fv, fo)):
        number, place = tg.group[row, origin], tg.stop[row, origin]
        if number < 0 or place < 0 or number not in tables:
            continue
        table = tables[number]
        passes = table["pass"][:, place]
        candidate = (passes < origin) & (table["vehicle"] != row)
        if not candidate.any():
            continue
        best = int(np.where(candidate, passes, -1).argmax())        # the latest; the first one listed on a tie
        ahead, level, start = table["vehicle"][best], table["level"][best, place], table["slot"][best, place]
        out[n] = filled[ahead, np.minimum(start + steps, origin - 1)] - level
    return out


class LocationFeatures(NamedTuple):
    """Per forecast of the plan, in plan order (for slot, vehicles in plan.origins: for vehicle in vehicles)."""
    rows: np.ndarray                    # (forecasts,) the bus's row in the TripGrid
    slots: np.ndarray                   # (forecasts,) the grid slot of the forecast time
    rank: np.ndarray                    # (forecasts,) the bus's rank then
    x: np.ndarray                       # (forecasts, points, len(FEATURE_NAMES)) the features of each point
    position: np.ndarray                # (forecasts,) trip position known: direction and stop sequence
    placed: np.ndarray                  # (forecasts,) stop known: its place on the line's stop list (stations, bus ahead)


def location_features(plan:ForecastPlan, tg:TripGrid) -> LocationFeatures:
    """The features of every point of every forecast of the plan, all known at its forecast time."""
    grid, points, point_slots = plan.grid, plan.points, plan.point_slots
    step = grid.step
    row_of = {vehicle: row for row, vehicle in enumerate(tg.vehicles)}
    fv = np.array([row_of[vehicle] for _, vehicles in plan.origins for vehicle in vehicles], dtype=np.int64)
    fo = np.array([slot for slot, vehicles in plan.origins for _ in vehicles], dtype=np.int64)
    n = fv.size
    seconds = point_slots * step * np.arange(1, points + 1)
    x = {name: np.zeros((n, points)) for name in FEATURE_NAMES}
    x["log_horizon"][:] = np.log(seconds / HORIZON_UNIT)
    x["horizon"][:] = seconds / HORIZON_SCALE

    # Status history: time since the last change (among known statuses, up to the forecast time included)
    since = np.full(n, np.nan)
    for row in np.unique(fv):
        series = tg.rank[row]
        known = np.nonzero(np.isfinite(series))[0]
        changes = known[1:][series[known[1:]] != series[known[:-1]]]
        if not changes.size:
            continue
        mine = np.nonzero(fv == row)[0]
        last = np.searchsorted(changes, fo[mine], side="right") - 1
        since[mine] = np.where(last >= 0, fo[mine] - changes[np.maximum(last, 0)], np.nan)
    history = fo - np.array([plan.first_slots[tg.vehicles[row]] for row in fv], dtype=np.int64)
    never = np.isnan(since)
    minutes = step / 60.0
    x["never_changed"][:] = never[:, None]
    x["never_changed_x_history"][:] = (never * np.minimum(history * minutes, HISTORY_CAP) / 10.0)[:, None]
    x["time_since_change"][:] = np.where(never, 0.0, np.minimum(np.nan_to_num(since) * minutes, CHANGE_CAP) / 10.0)[:, None]

    # Trip position, the trip's last stop as known then, and where the bus will be (its recent progress)
    seq_o, group_o, place_o = tg.seq[fv, fo], tg.group[fv, fo], tg.stop[fv, fo]
    last_stop = np.fmax(_causal_last_stop(tg, group_o, fo), seq_o)
    position = np.isfinite(seq_o) & (group_o >= 0) & np.isfinite(last_stop)
    placed = (place_o >= 0) & (group_o >= 0)
    window, minimum = max(1, round(PROGRESS_WINDOW / step)), max(1, round(MIN_PROGRESS / step))
    steps = point_slots * np.arange(1, points + 1)
    projected = np.full((n, points), np.nan)
    station = np.zeros((n, points))
    cumulative = [None if s is None else np.concatenate([[0], np.cumsum(s)]) for s in tg.stations]
    for j in range(n):
        row, origin = fv[j], fo[j]
        if position[j]:
            rate = _progress_rate(tg.trip[row], tg.seq[row], np.isfinite(tg.seq[row]), origin, window, minimum)
            projected[j] = seq_o[j] + rate * steps
        if placed[j]:
            rate = _progress_rate(tg.trip[row], tg.stop[row], tg.stop[row] >= 0, origin, window, minimum)
            stations = cumulative[group_o[j]]
            reach = np.minimum(np.floor(place_o[j] + rate * steps), len(stations) - 2).astype(np.int64)
            station[j] = (stations[reach + 1] - stations[place_o[j]]) > 0
    p = position[:, None]
    x["trip_start"][:] = p & (seq_o <= TRIP_START_STOPS)[:, None]
    x["trip_end"][:] = p & (last_stop - seq_o <= TRIP_END_STOPS)[:, None]
    x["trip_boundary"][:] = p & (projected >= last_stop[:, None])
    with np.errstate(divide="ignore", invalid="ignore"):
        progress = np.clip(seq_o / last_stop, 0, 1) - 0.5
    x["trip_progress"][:] = np.where(position & (last_stop > 0), progress, 0.0)[:, None]
    x["station_ahead"][:] = np.where(placed[:, None], station, 0.0)
    x["position_unknown"][:] = ~(position & placed)[:, None]

    # The bus ahead on the line
    ahead = _bus_ahead(tg, fv, fo, point_slots, points)
    x["no_bus_ahead"][:] = ~np.isfinite(ahead)
    x["bus_ahead_change"][:] = np.nan_to_num(ahead)
    x["bus_ahead_abs_change"][:] = np.abs(x["bus_ahead_change"])

    features = np.stack([x[name] for name in FEATURE_NAMES], axis=2)
    if not np.isfinite(features).all():
        raise RecordingError("The location model computed an invalid feature (a bug): please report it.")
    return LocationFeatures(fv, fo, tg.rank[fv, fo], features, position, placed)


class StatusLogit:
    """Conditional multinomial logit over `classes` ranks (see METHOD), on a fixed set of forecast points: their rank
    at the forecast time, features, outcome and weight (1: outcome known, 0: not, the point is ignored). fit() takes
    ranges of rows, proba() row numbers. Parameters, in order: alpha (ranks 1..), gamma (per rank at the forecast
    time), jump, beta (CHANGE_FEATURES), delta (DIRECTION_FEATURES)."""

    DESIGN_BUDGET = 1 << 29             # bytes: up to this size, the design matrix of all points is built once

    def __init__(self, classes:int, rank:np.ndarray, x:np.ndarray, y:np.ndarray, weight:np.ndarray):
        self.classes, self.rank, self.x, self.y, self.weight = classes, rank, x, y, weight
        self.change = [FEATURE_NAMES.index(name) for name in CHANGE_FEATURES]
        self.direction = [FEATURE_NAMES.index(name) for name in DIRECTION_FEATURES]
        self.size = 2 * classes + len(self.change) + len(self.direction)
        self.penalized = np.arange(self.size) >= 2 * classes
        fits = rank.size * classes * self.size * 8 <= self.DESIGN_BUDGET
        self._design = self.design(rank, x) if fits else None       # else built block by block at each use

    def design(self, rank:np.ndarray, x:np.ndarray) -> np.ndarray:
        """(rows,) ranks at the forecast time and (rows, features) -> (rows, classes, parameters)."""
        n, c = rank.size, np.arange(self.classes, dtype=np.float64)[None, :]
        d = c - rank[:, None]
        distance = np.abs(d)
        out = np.empty((n, self.classes, self.size))
        column = 0
        for candidate in range(1, self.classes):
            out[:, :, column] = c == candidate
            column += 1
        for level in range(self.classes):
            out[:, :, column] = (rank[:, None] == level) * distance
            column += 1
        out[:, :, column] = distance >= 2
        column += 1
        for indices, sign in ((self.change, distance), (self.direction, d)):
            for i in indices:
                np.multiply(x[:, None, i], sign, out=out[:, :, column])
                column += 1
        return out

    def _rows(self, index) -> np.ndarray:
        """The design of the rows `index` (a slice or row numbers)."""
        return self._design[index] if self._design is not None else self.design(self.rank[index], self.x[index])

    def _evaluate(self, theta:np.ndarray, ranges:list, reg:np.ndarray, n:float, derivatives:bool):
        """Objective at theta and, with `derivatives`, its gradient and Hessian, over the weighted rows of `ranges`."""
        p = self.size
        value, grad, hess = 0.0, np.zeros(p), np.zeros((p, p))
        for begin, end in ranges:
            for start in range(begin, end, CHUNK):
                rows = slice(start, min(start + CHUNK, end))
                design, target, weight = self._rows(rows), self.y[rows], self.weight[rows]
                picked = np.arange(target.size), target
                u = design @ theta
                top = u.max(axis=1)
                exp = np.exp(u - top[:, None])
                total = exp.sum(axis=1)
                value += float(weight @ (top + np.log(total) - u[picked]))
                if derivatives:
                    prob = exp / total[:, None]
                    mean = np.einsum("nc,ncp->np", prob, design)
                    grad += weight @ (mean - design[picked])
                    flat = (design * np.sqrt(prob * weight[:, None])[:, :, None]).reshape(-1, p)
                    hess += flat.T @ flat - (mean * weight[:, None]).T @ mean      # weighted covariances under prob
        value = value / n + 0.5 * float(reg @ (theta * theta))
        return value, grad / n + reg * theta, hess / n + np.diag(reg)

    def fit(self, ranges:list[tuple[int, int]], lam:float, init:np.ndarray=None) -> np.ndarray:
        """Penalized maximum likelihood on the weighted rows of `ranges` (mean negative log-likelihood + lam/2
        ||beta, delta||^2, 1e-6 on the others): Newton's method with backtracking, from `init` (warm start)."""
        n = float(sum(self.weight[begin:end].sum() for begin, end in ranges))
        reg = np.where(self.penalized, lam, 1e-6)
        theta = np.zeros(self.size) if init is None else init.copy()
        value, grad, hess = self._evaluate(theta, ranges, reg, n, True)
        for _ in range(100):
            step = np.linalg.solve(hess, grad)
            decrement = float(grad @ step)
            if decrement < 1e-10:
                break
            t, derivatives = 1.0, None
            while True:
                candidate = theta - t * step
                if t == 1.0:        # a full step is usually taken: get its derivatives in the same pass
                    candidate_value, *derivatives = self._evaluate(candidate, ranges, reg, n, True)
                else:
                    candidate_value, derivatives = self._evaluate(candidate, ranges, reg, n, False)[0], None
                if candidate_value <= value - 0.25 * t * decrement or t < 1e-10:
                    break
                t *= 0.5
            theta, value = candidate, candidate_value
            grad, hess = derivatives if derivatives else self._evaluate(theta, ranges, reg, n, True)[1:]
        return theta

    def proba(self, rows:np.ndarray, theta:np.ndarray) -> np.ndarray:
        return _softmax(self._rows(rows) @ theta)

    def coefficients(self, theta:np.ndarray) -> dict:
        """The fitted parameters by name, for the replay (rounded)."""
        def r(value):
            return round(float(value), 4)
        c = self.classes
        return {"alpha": [r(v) for v in theta[:c - 1]], "gamma": [r(v) for v in theta[c - 1:2 * c - 1]],
                "jump": r(theta[2 * c - 1]),
                "beta": {name: r(v) for name, v in zip(CHANGE_FEATURES, theta[2 * c:2 * c + len(self.change)])},
                "delta": {name: r(v) for name, v in zip(DIRECTION_FEATURES, theta[2 * c + len(self.change):])}}


def _softmax(u:np.ndarray) -> np.ndarray:
    e = np.exp(u - u.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def quantiles(prob:np.ndarray, top_rank:int) -> np.ndarray:
    """(rows, classes) probabilities -> (rows, len(QUANTILES)) ranks. The median is the median rank m itself (what
    minimizes the expected error in steps); the other quantiles are spread uniformly within each rank's bin
    [c - 0.5, c + 0.5], kept inside the bin of their rank. Clipped to [0, top_rank], sorted, rounded to 2 decimals."""
    n = prob.shape[0]
    cdf = np.cumsum(prob, axis=1)
    cdf[:, -1] = 1.0
    rows = np.arange(n)
    values = []
    for q in QUANTILES:
        m = np.minimum((cdf < q).sum(axis=1), prob.shape[1] - 1)
        if q == 0.5:
            values.append(m.astype(np.float64))
            continue
        below = np.where(m > 0, cdf[rows, np.maximum(m - 1, 0)], 0.0)
        value = m - 0.5 + (q - below) / np.maximum(prob[rows, m], 1e-300)
        values.append(np.clip(value, m - INSET, m + INSET))
    return np.round(np.sort(np.clip(np.stack(values, axis=1), 0, top_rank), axis=1), 2)


def compute_location_forecast(plan:ForecastPlan, replay:dict, route_stops:dict | None,
                              settings:LocationModelSettings=LocationModelSettings(),
                              on_progress:Callable[[int, int, float, int], None]=None) -> ForecastResult:
    """The location model's forecast for every (bus, forecast time) of the plan, trained walk-forward, scored like
    compute_forecast. on_progress(origins done, origins, origin t, buses) is called after each origin."""
    grid, points, point_slots, top_rank = plan.grid, plan.points, plan.point_slots, plan.grid.top_rank
    tg = build_trip_grid(replay, grid, route_stops)
    features = location_features(plan, tg)

    # Every forecast point, flattened point by point (row k * n + forecast): all the points of one horizon whose outcome
    # is known at a time are then one range of rows, as the forecasts are in time order
    n, classes = features.rank.size, top_rank + 1
    k = np.repeat(np.arange(points), n)
    target = np.tile(features.slots, points) + point_slots * (k + 1)
    bus_row = np.tile(features.rows, points)
    observed = np.full(n * points, np.nan)
    inside = target < grid.slots
    observed[inside] = tg.rank[bus_row[inside], target[inside]]
    point_rank, x = np.tile(features.rank, points), features.x.transpose(1, 0, 2).reshape(n * points, -1)
    scored = np.isfinite(observed)
    y = np.where(scored, observed, 0).astype(np.int64)
    model = StatusLogit(classes, point_rank, x, y, scored.astype(np.float64))
    # Outcomes known by a time: counts per range (prefix sums), per status, and when each bus first has one
    known_before = np.concatenate([[0], np.cumsum(scored)])
    status_before = np.stack([np.concatenate([[0], np.cumsum(scored & (point_rank == r))]) for r in range(classes)])
    first_known = np.full(len(tg.vehicles), np.iinfo(np.int64).max)
    np.minimum.at(first_known, bus_row[scored], target[scored])

    prob = np.full((n * points, classes), np.nan)
    active = np.zeros(n * points, dtype=bool)
    first_t: list[float | None] = [None] * points
    history_t, history_examples = [], []
    theta, final = None, None
    begin = 0
    for number, (slot, vehicles) in enumerate(plan.origins, start=1):
        t = float(grid.times[slot])
        here = (np.arange(points)[:, None] * n + np.arange(begin, begin + len(vehicles))[None, :]).ravel()
        begin += len(vehicles)
        # Training points: target slot <= this slot, i.e. forecast slot <= slot - point_slots * (k + 1)
        known = np.searchsorted(features.slots, slot - point_slots * np.arange(1, points + 1), side="right")
        ranges = [(h * n, h * n + int(known[h])) for h in range(points) if known[h]]
        per_point = np.array([known_before[h * n + known[h]] - known_before[h * n] for h in range(points)])
        per_status = sum((status_before[:, end] - status_before[:, start] for start, end in ranges), np.zeros(classes))
        examples, buses = int(per_point.sum()), int((first_known <= slot).sum())
        history_t.append(t)
        history_examples.append(examples)
        if classes > 1 and examples >= settings.min_points and buses >= settings.min_buses:
            ok = (per_point[k[here]] >= settings.min_points_per_point) & \
                 (per_status[point_rank[here].astype(np.int64)] >= settings.min_points_per_status)
            if ok.any():
                theta = model.fit(ranges, settings.lam, init=theta)
                final = {"examples": examples, "buses": buses, "coefficients": model.coefficients(theta)}
                use = here[ok]
                prob[use] = model.proba(use, theta)
                active[use] = True
                for h in np.unique(k[use]):
                    if first_t[h] is None:
                        first_t[h] = t
        if on_progress:
            on_progress(number, len(plan.origins), t, len(vehicles))

    # Stored ranks: the model's quantiles where it was active, else "no change"
    stored = np.repeat(point_rank[:, None], len(QUANTILES), axis=1)
    if active.any():
        stored[active] = quantiles(prob[active], top_rank)
    metrics = score(observed[scored], stored[scored, MEDIAN], stored[scored, LOW], stored[scored, HIGH],
                    point_rank[scored], (k == points - 1)[scored])

    by_bus: dict[str, list] = {}
    blocks = stored.reshape(points, n, len(QUANTILES)).transpose(1, 0, 2)       # (forecasts, points, quantiles)
    for row, slot, block in zip(features.rows, features.slots, blocks):
        by_bus.setdefault(tg.vehicles[row], []).append({"t": float(grid.times[slot]), "q": block.T.tolist()})
    training = {
        "method": METHOD,
        "features": FEATURES,
        "change_features": CHANGE_FEATURES,
        "direction_features": DIRECTION_FEATURES,
        "lambda": settings.lam,
        "min_points": settings.min_points,
        "min_buses": settings.min_buses,
        "min_points_per_point": settings.min_points_per_point,
        "min_points_per_status": settings.min_points_per_status,
        "position_known": round(float(features.position.mean()), 3),    # share of forecasts: trip position known
        "stop_known": round(float(features.placed.mean()), 3),          # ... stop known on its line's stop list
        "model_share": round(float(active.mean()), 3),
        "first_t": first_t,             # per point: replay t of its first forecast by the model, null if never
        "history": {"t": history_t, "examples": history_examples},
        "final": final,                 # what it had learned at its last fit, null if it never had enough
    }
    return forecast_result(plan, MODEL, MODEL_NAME, metrics, {"training": training}, by_bus)
