"""Occupancy forecasts for replays: what the viewer's bus chart shows after the observed occupancy.

The demo exe runs no model: forecasts are precomputed here with Timestral (a time-series model by Mistral AI) and
stored in the replay under the key "forecast" (format: visualization/replays/README.md). Every `origin_every`
seconds of replay time, each bus with `min_history` seconds of occupancy history gets a forecast of the next
`horizon` seconds, made only from what was observed until then.

The forecast works on ranks, not on the GTFS-realtime occupancy values: the spec says those are not a linear scale and that a
feed may use only some of them (STM sends 1, 2, 3 and 5). Rank 0 is the emptiest status the replay contains, rank 1 the next
one, and so on; the forecast stores which statuses its ranks stand for. The scale is the statuses found in the whole replay
(a status first seen later is already on it for earlier forecasts); the history given to the model ends at the forecast time.

Needs numpy; Timestral and PyTorch only to load the model (see requirements-forecast.in).
"""
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from recording import NothingToForecast, RecordingError
from recording.paths import display_path, repo_relative
from recording.replay import FORMAT as REPLAY_FORMAT, VERSION as REPLAY_VERSION
from recording.store import utc_now_iso

FORMAT = "stm-dt-occupancy-forecast"
VERSION = 2                         # 2: values are ranks of the statuses in "levels" (1 stored the status values)
TARGET = "vehicle.occupancy_status"
QUANTILES = (0.1, 0.5, 0.9)         # 80% range around the median
MAX_LEVEL = 5                       # FULL
MIN_CONTEXT_VALUES = 4              # known levels a bus needs in its history to be forecast
MAX_CONTEXT = 32768                 # most recent time steps given to the model (Timestral's trained context length)
MAX_BATCH = 512                     # series per model call
DEFAULT_STEP = 15.0                 # replay poll_interval when the file has none
DEFAULT_POINT_EVERY = 30.0          # seconds between two stored forecast points

DEFAULT_MODEL = "28M"
MODEL_REPO = "mistralai/Timestral-{size}"
MODEL_SIZES = {"5M": "21 MB", "28M": "111 MB", "123M": "490 MB", "209M": "837 MB"}   # download size of the weights

# GTFS-realtime VehiclePosition.OccupancyStatus values that are a level. 6-8 (not accepting passengers, no data,
# not boardable), a missing value and anything else are "no data".
LEVELS = {"EMPTY": 0, "MANY_SEATS_AVAILABLE": 1, "FEW_SEATS_AVAILABLE": 2, "STANDING_ROOM_ONLY": 3,
          "CRUSHED_STANDING_ROOM_ONLY": 4, "FULL": 5}

INSTALL_STEPS = """\
Install them in a separate environment so the main one stays small (PyTorch alone is a few hundred MB).
From the repository root:
  uv venv .venv-forecast --python 3.12
  uv pip install --python .venv-forecast -r software\\recording\\requirements-forecast.in
then run the forecast with that environment's Python:
  .venv-forecast\\Scripts\\python software\\recording forecast <replay>.json
(without uv: `python -m venv .venv-forecast`, then
 `.venv-forecast\\Scripts\\python -m pip install -r software\\recording\\requirements-forecast.in`)"""

logger = logging.getLogger("Forecast")


def missing_module_error(error:ImportError) -> RecordingError:
    module = error.name or str(error)
    return RecordingError(f"The forecast needs numpy, Timestral and PyTorch, but '{module}' is not installed in this "
                          f"Python ({display_path(sys.executable)}).\n{INSTALL_STEPS}")


# numpy is the only dependency of this module: without it, `forecast` explains how to install everything
try:
    import numpy as np
except ImportError as import_error:
    raise missing_module_error(import_error) from import_error


@dataclass(frozen=True)
class ForecastSettings:
    """When and how far ahead to forecast, in seconds of replay time."""
    horizon: float = 600.0              # rounded down to a whole number of points
    origin_every: float = 60.0          # rounded to a whole number of poll intervals
    min_history: float = 120.0
    point_every: float | None = None    # must be a multiple of the poll interval; None: 30 s, rounded to one


@dataclass(frozen=True)
class CalibrationSettings:
    """Walk-forward calibration of the stored forecast (see Calibrator); below the thresholds, raw Timestral is kept."""
    enabled: bool = True
    min_points: int = 200               # past forecasts with a known outcome a horizon needs before it is calibrated
    min_buses: int = 20                 # ... from at least this many buses
    cell_min_points: int = 100          # for a horizon's range offsets of its own per status at the forecast time
    cell_min_buses: int = 10


RANGE_TAIL = 0.9                        # each end of the 80% range is calibrated so it holds 90% of past outcomes


@dataclass
class OccupancyGrid:
    """Occupancy of every vehicle on a regular grid of `step` seconds; slot 0 is the first frame."""
    step: float
    times: np.ndarray                   # (slots,) `t` of the frame on each slot, NaN when no frame fell on it
    levels: dict[str, np.ndarray]       # vehicle id -> (slots,) float32 rank in `statuses`, NaN = no data or not in that frame
    statuses: list[int]                 # the occupancy statuses (0-5) found in the replay, ascending: rank -> status

    @property
    def slots(self) -> int:
        return len(self.times)


@dataclass
class ForecastPlan:
    """What will be forecast, decided before the model is loaded."""
    grid: OccupancyGrid
    settings: ForecastSettings          # the values actually used: whole numbers of grid steps
    origin_slots: int                   # grid slots between two forecast times
    point_slots: int                    # grid slots between two stored points
    points: int                         # stored points per forecast
    origins: list[tuple[int, list[str]]]    # (slot, vehicles forecast there), ascending
    first_slots: dict[str, int]         # vehicle -> slot of its first known level

    @property
    def prediction_length(self) -> int:
        return self.points * self.point_slots

    @property
    def vehicles(self) -> int:
        return len({vehicle for _, vehicles in self.origins for vehicle in vehicles})

    @property
    def forecasts(self) -> int:
        return sum(len(vehicles) for _, vehicles in self.origins)

    def context(self, vehicle:str, slot:int) -> np.ndarray:
        """The history the model sees for a forecast at `slot`: levels from the first known one to `slot` included."""
        start = max(self.first_slots[vehicle], slot + 1 - MAX_CONTEXT)
        return self.grid.levels[vehicle][start:slot + 1]


@dataclass
class ForecastResult:
    forecast: dict                      # the value of the replay's "forecast" key
    vehicles: int
    origins: int
    forecasts: int                      # (bus, forecast time) pairs
    points: int                         # stored forecast points
    skipped: int                        # forecasts dropped because the model returned NaN or infinity

    @property
    def metrics(self) -> dict:
        return self.forecast["metrics"]

    @property
    def metrics_raw(self) -> dict | None:
        """Scores of the uncalibrated model output (None when nothing was calibrated)."""
        return self.forecast.get("metrics_raw")


class Calibrator:
    """Walk-forward calibration of the model's forecasts, one horizon (forecast point) at a time.

    Before the forecasts of an origin are made, each horizon is fitted on the past forecasts of that horizon whose
    outcome is already known (target time <= the origin), so a stored forecast never uses what happened after it.
      - Median: L + w * (model median - L), L the rank at the forecast time. w in [0, 1] is the least-squares weight of
        the model's predicted move against the moves that happened: how much the model's moves can be trusted.
      - 80% range: widened (never narrowed) by split conformal offsets, so that each end held 90% of past outcomes;
        per rank at the forecast time when that rank has enough examples, else for the whole horizon.
    A horizon with too few examples keeps the raw model output.
    """

    def __init__(self, points:int, top_rank:int, settings:CalibrationSettings):
        self.points, self.top, self.settings = points, top_rank, settings
        # Per horizon, chunks of past examples: target slot, vehicle number, rank at the origin, raw median, low and
        # high, and the observed rank at the target (read when added: the target-slot filter of fit() keeps it unseen)
        self._chunks = [[] for _ in range(points)]
        self._vehicle_numbers: dict[str, int] = {}
        self.params: list[dict | None] = [None] * points

    def add(self, vehicle:str, target_slots:np.ndarray, rank:float, raw:np.ndarray, observed:np.ndarray):
        """Records a raw forecast (Q x points, low/median/high) and the observed ranks of its points (NaN: unknown)."""
        number = self._vehicle_numbers.setdefault(vehicle, len(self._vehicle_numbers))
        for h in np.nonzero(np.isfinite(observed))[0]:
            self._chunks[h].append((target_slots[h], number, rank, raw[1][h], raw[0][h], raw[2][h], observed[h]))

    def fit(self, origin_slot:int):
        """Fits every horizon on its examples whose target slot is <= origin_slot (self.params[h], None: raw)."""
        for h in range(self.points):
            self.params[h] = None
            if not self.settings.enabled or not self._chunks[h]:
                continue
            target, vehicle, rank, median, low, high, observed = (np.array(column) for column in zip(*self._chunks[h]))
            past = target <= origin_slot
            n, buses = int(past.sum()), int(np.unique(vehicle[past]).size)
            if n < self.settings.min_points or buses < self.settings.min_buses:
                continue
            rank, median, low, high, observed, vehicle = (a[past] for a in (rank, median, low, high, observed, vehicle))
            move, happened = median - rank, observed - rank
            sxx = float((move * move).sum())
            weight = float(np.clip((move * happened).sum() / sxx, 0.0, 1.0)) if sxx > 0 else 0.0
            low_score, high_score = low - observed, observed - high
            offsets = [(conformal_offset(low_score), conformal_offset(high_score))] * (self.top + 1)
            for r in range(self.top + 1):
                cell = rank == r
                if cell.sum() >= self.settings.cell_min_points and \
                        np.unique(vehicle[cell]).size >= self.settings.cell_min_buses:
                    offsets[r] = (conformal_offset(low_score[cell]), conformal_offset(high_score[cell]))
            self.params[h] = {"examples": n, "buses": buses, "w": weight, "offsets": offsets}

    def apply(self, rank:float, raw:np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(stored low/median/high rows, calibrated flag per point) for a raw forecast made at the last fit()."""
        stored, calibrated = raw.copy(), np.zeros(self.points, dtype=bool)
        r = int(rank)
        for h, params in enumerate(self.params):
            if params is None:
                continue
            low_offset, high_offset = params["offsets"][r]
            median = round(float(np.clip(rank + params["w"] * (raw[1][h] - rank), 0, self.top)), 2)
            # Not sorted: low <= median <= high by construction (sorting would swap the median with a range end)
            stored[0][h] = round(float(np.clip(min(raw[0][h] - low_offset, median), 0, self.top)), 2)
            stored[1][h] = median
            stored[2][h] = round(float(np.clip(max(raw[2][h] + high_offset, median), 0, self.top)), 2)
            calibrated[h] = True
        return stored, calibrated


def conformal_offset(scores:np.ndarray, level:float=RANGE_TAIL) -> float:
    """Split conformal offset: the ceil((n + 1) * level)-th smallest score, floored at 0 (the range is only widened);
    infinite (the range end goes to the end of the scale) when there are too few scores for that rank to exist."""
    n = scores.size
    j = -(-(n + 1) * round(level * 10) // 10)       # exact integer ceil((n + 1) * level) for level in tenths
    if n == 0 or j > n:
        return math.inf
    return max(float(np.partition(scores, j - 1)[j - 1]), 0.0)


class Forecaster(Protocol):
    """What compute_forecast needs from a model: TimestralAdapter, or a fake in the tests."""
    model: str                          # stored in the replay: Hugging Face repo id or model folder
    model_name: str                     # short display name, e.g. "Timestral-28M"

    def predict(self, context:np.ndarray, prediction_length:int, quantiles:tuple[float, ...]) -> np.ndarray:
        """(B, T) float32 histories (NaN = no data, left-padded) -> (B, len(quantiles), prediction_length)."""


def occupancy_level(value) -> float:
    """Occupancy status -> level 0 (empty) to 5 (full), NaN for no data (6-8, missing, anything else).
    Accepts an int, a float, a numeric string or the enum name ("FULL")."""
    if isinstance(value, str):
        text = value.strip()
        if text.upper() in LEVELS:
            return float(LEVELS[text.upper()])
        try:
            value = float(text)
        except ValueError:
            return math.nan
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return math.nan
    return float(value) if value == int(value) and 0 <= value <= MAX_LEVEL else math.nan


def vehicle_id(entity:dict) -> str | None:
    vehicle = entity.get("vehicle.vehicle.id", entity.get("id"))
    return None if vehicle is None else str(vehicle)


def read_replay(path:Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise RecordingError(f"Replay not found: {display_path(path)}")
    try:
        replay = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecordingError(f"{display_path(path)} is not a JSON file ({error}).") from error
    if not isinstance(replay, dict) or replay.get("format") != REPLAY_FORMAT:
        raise RecordingError(f"{display_path(path)} is not a replay (format {REPLAY_FORMAT}); make one with `export`.")
    version = replay.get("version")
    if not isinstance(version, (int, float)) or version > REPLAY_VERSION:
        raise RecordingError(f"{display_path(path)} is a replay version {version}; this tool reads version "
                             f"{REPLAY_VERSION} and older.")
    if not isinstance(replay.get("start_time"), (int, float)):
        raise RecordingError(f"{display_path(path)} has no start_time.")
    if not isinstance(replay.get("frames"), list) or not replay["frames"]:
        raise RecordingError(f"{display_path(path)} has no frames: there is nothing to forecast.")
    return replay


def build_grid(replay:dict) -> OccupancyGrid:
    """slot = round((t - t0) / poll_interval). A vehicle missing from a frame, or a slot without a frame, is NaN.
    When two frames fall on one slot, the later one replaces the earlier one. Levels are ranks among the statuses found."""
    step = replay.get("poll_interval") or DEFAULT_STEP
    if not isinstance(step, (int, float)) or step <= 0:
        raise RecordingError(f"The replay's poll_interval must be a number of seconds greater than 0 (got {step!r}).")
    try:
        frames = sorted(replay["frames"], key=lambda frame: float(frame["t"]))
        t0 = float(frames[0]["t"])
        slots = [int(round((float(frame["t"]) - t0) / step)) for frame in frames]
    except (KeyError, TypeError, ValueError) as error:
        raise RecordingError(f"The replay has a frame without a valid time `t` ({error!r}).") from error

    times = np.full(slots[-1] + 1, np.nan)
    levels: dict[str, np.ndarray] = {}
    for slot, frame in zip(slots, frames):
        if not math.isnan(times[slot]):
            for series in levels.values():
                series[slot] = np.nan
        times[slot] = float(frame["t"])
        for entity in frame.get("data") or ():
            vehicle = vehicle_id(entity)
            if vehicle is None:
                continue
            if vehicle not in levels:
                levels[vehicle] = np.full(len(times), np.nan, dtype=np.float32)
            levels[vehicle][slot] = occupancy_level(entity.get(TARGET))
    # Status -> rank (NaN stays NaN: the lookup's last entry)
    statuses = sorted({int(value) for series in levels.values() for value in series[np.isfinite(series)]})
    rank_of = np.full(MAX_LEVEL + 2, np.nan, dtype=np.float32)
    rank_of[statuses] = np.arange(len(statuses))
    for vehicle, series in levels.items():
        levels[vehicle] = rank_of[np.where(np.isfinite(series), series, MAX_LEVEL + 1).astype(int)]
    return OccupancyGrid(float(step), times, levels, statuses)


def plan_forecast(replay:dict, settings:ForecastSettings=ForecastSettings()) -> ForecastPlan:
    """Forecast times and buses. A bus is forecast at an origin (a frame every `origin_every` seconds) when its level
    there is known, it has `min_history` seconds of history since its first known level and at least
    MIN_CONTEXT_VALUES known levels."""
    grid = build_grid(replay)
    step = grid.step
    if settings.horizon <= 0 or settings.origin_every <= 0 or settings.min_history < 0:
        raise RecordingError("The horizon and the time between forecasts must be greater than 0, "
                             "the minimum history at least 0.")
    if settings.point_every is None:
        point_slots = max(1, round(DEFAULT_POINT_EVERY / step))
    else:
        point_slots = round(settings.point_every / step)
        if point_slots < 1 or not math.isclose(point_slots * step, settings.point_every, abs_tol=1e-6):
            raise RecordingError(f"Forecast points must be a whole number of poll intervals apart: "
                                 f"{settings.point_every:g} s is not a multiple of {step:g} s.")
    point_every = point_slots * step
    points = math.floor(settings.horizon / point_every + 1e-9)
    if points < 1:
        raise RecordingError(f"The horizon ({settings.horizon:g} s) must be at least one forecast point "
                             f"({point_every:g} s).")
    origin_slots = max(1, round(settings.origin_every / step))
    effective = ForecastSettings(horizon=points * point_every, origin_every=origin_slots * step,
                                 min_history=float(settings.min_history), point_every=point_every)

    first_slots, known_counts = {}, {}
    for vehicle, series in grid.levels.items():
        known = np.isfinite(series)
        if known.any():
            first_slots[vehicle] = int(known.argmax())
            known_counts[vehicle] = np.cumsum(known)
    if not first_slots:
        raise NothingToForecast(f"No bus in this replay reports its occupancy ({TARGET}): there is nothing to forecast.")

    origins = []
    for slot in range(0, grid.slots, origin_slots):
        if math.isnan(grid.times[slot]):
            continue
        vehicles = []
        for vehicle in sorted(first_slots):
            first = first_slots[vehicle]
            if math.isnan(grid.levels[vehicle][slot]) or (slot - first) * step < settings.min_history - 1e-6:
                continue
            counts = known_counts[vehicle]
            window_start = max(first, slot + 1 - MAX_CONTEXT)
            if counts[slot] - (counts[window_start - 1] if window_start > 0 else 0) >= MIN_CONTEXT_VALUES:
                vehicles.append(vehicle)
        if vehicles:
            origins.append((slot, vehicles))
    if not origins:
        raise RecordingError(f"No bus has {settings.min_history / 60:g} min of occupancy history (and "
                             f"{MIN_CONTEXT_VALUES} known levels) in this replay: there is nothing to forecast. "
                             f"Use a smaller --min-history-minutes, or a longer replay.")
    return ForecastPlan(grid, effective, origin_slots, point_slots, points, origins, first_slots)


def compute_forecast(plan:ForecastPlan, forecaster:Forecaster,
                     on_progress:Callable[[int, int, float, int], None]=None,
                     calibration:CalibrationSettings=CalibrationSettings()) -> ForecastResult:
    """Runs the model at every origin of the plan (all its buses in one batch), calibrates its output walk-forward
    (Calibrator) and scores the stored points, and the raw model output, against what was observed.
    on_progress(origins done, origins, origin t, buses) is called after each origin."""
    grid, length = plan.grid, plan.prediction_length
    top_rank = max(len(grid.statuses) - 1, 0)
    kept = np.arange(1, plan.points + 1) * plan.point_slots - 1          # model steps at origin + point_every * (k + 1)
    median, low, high = QUANTILES.index(0.5), 0, len(QUANTILES) - 1
    calibrator = Calibrator(plan.points, top_rank, calibration)
    stored: dict[str, list] = {}
    columns = ("observed", "median", "low", "high", "last", "at_horizon")
    scores, raw_scores = {name: [] for name in columns}, {name: [] for name in columns}
    history_t, history_w = [], []
    first_t: list[float | None] = [None] * plan.points
    calibrated_points = skipped = 0

    for number, (slot, vehicles) in enumerate(plan.origins, start=1):
        t = float(grid.times[slot])
        calibrator.fit(slot)        # on the forecasts whose outcome is known at this slot
        history_t.append(t)
        history_w.append([None if params is None else round(params["w"], 3) for params in calibrator.params])
        for h, params in enumerate(calibrator.params):
            if params is not None and first_t[h] is None:
                first_t[h] = t
        targets = slot + plan.point_slots * np.arange(1, plan.points + 1)     # grid slot of each point
        inside = targets < grid.slots
        for start in range(0, len(vehicles), MAX_BATCH):
            batch_vehicles = vehicles[start:start + MAX_BATCH]
            contexts = [plan.context(vehicle, slot) for vehicle in batch_vehicles]
            batch = np.full((len(contexts), max(len(context) for context in contexts)), np.nan, dtype=np.float32)
            for row, context in zip(batch, contexts):
                row[len(row) - len(context):] = context
            try:
                values = forecaster.predict(batch, length, QUANTILES)
            # ValueError, e.g. Timestral: "forecast produced NaN values; the input is degenerate";
            # RuntimeError: PyTorch, e.g. out of memory or a device error
            except (ValueError, RuntimeError, MemoryError) as error:
                raise RecordingError(f"{forecaster.model_name} failed on the forecast at t={t:g} s "
                                     f"({len(batch_vehicles)} buses): {type(error).__name__}: {error}") from error
            expected = (len(batch_vehicles), len(QUANTILES), length)
            if np.shape(values) != expected:
                raise RecordingError(f"{forecaster.model_name} returned an array of shape {np.shape(values)}, "
                                     f"expected {expected}.")
            # float64 first: float32 values would not round to 2 decimals in the JSON
            values = np.asarray(values, dtype=np.float64)[:, :, kept]
            finite = np.isfinite(values).all(axis=(1, 2))      # before clipping, which would turn an infinity into a rank
            values = np.round(np.sort(np.clip(values, 0, top_rank), axis=1), 2)

            for vehicle, raw, is_finite in zip(batch_vehicles, values, finite):
                if not is_finite:
                    skipped += 1
                    continue
                rank = float(grid.levels[vehicle][slot])
                quantiles, calibrated = calibrator.apply(rank, raw)
                calibrated_points += int(calibrated.sum())
                stored.setdefault(vehicle, []).append({"t": t, "q": quantiles.tolist()})
                observed = np.full(plan.points, np.nan)
                observed[inside] = grid.levels[vehicle][targets[inside]]
                calibrator.add(vehicle, targets, rank, raw, observed)
                known = np.isfinite(observed)
                for into, rows in ((scores, quantiles), (raw_scores, raw)):
                    into["observed"].append(observed[known])
                    into["median"].append(rows[median][known])
                    into["low"].append(rows[low][known])
                    into["high"].append(rows[high][known])
                    into["last"].append(np.full(int(known.sum()), rank))
                    into["at_horizon"].append((np.arange(plan.points) == plan.points - 1)[known])
        if on_progress:
            on_progress(number, len(plan.origins), t, len(vehicles))
    if skipped:
        logger.warning(f"Dropped {skipped} forecast(s) with NaN or infinite values.")

    def scored(parts:dict) -> dict:
        return score(**{name: np.concatenate(values) if values else np.empty(0) for name, values in parts.items()})

    settings = plan.settings
    forecasts = sum(len(entries) for entries in stored.values())
    forecast = {
        "format": FORMAT,
        "version": VERSION,
        "model": forecaster.model,
        "model_name": f"{forecaster.model_name} + calibration" if calibrated_points else forecaster.model_name,
        "created_at": utc_now_iso(),
        "target": TARGET,
        "levels": list(grid.statuses),      # forecast value r (a rank) stands for status levels[r]
        "step": grid.step,
        "point_every": settings.point_every,
        "horizon": settings.horizon,
        "origin_every": settings.origin_every,
        "min_history": settings.min_history,
        "quantiles": list(QUANTILES),
        "metrics": scored(scores),
    }
    if calibrated_points:
        forecast["metrics_raw"] = scored(raw_scores)
        forecast["calibration"] = calibration_info(calibrator, calibration, calibrated_points / (forecasts * plan.points),
                                                   first_t, history_t, history_w)
    forecast["vehicles"] = {vehicle: stored[vehicle] for vehicle in sorted(stored)}
    origins = len({entry["t"] for entries in stored.values() for entry in entries})
    return ForecastResult(forecast, len(stored), origins, forecasts, forecasts * plan.points, skipped)


def calibration_info(calibrator:Calibrator, settings:CalibrationSettings, calibrated_share:float, first_t:list,
                     history_t:list, history_w:list) -> dict:
    """The replay's "calibration" key: how it was done, and what it learned (for the viewer and for students)."""
    def offset(value:float) -> float | None:
        return round(value, 2) if math.isfinite(value) else None

    final = []
    for params in calibrator.params:
        if params is None:
            final.append(None)
            continue
        final.append({"examples": params["examples"], "buses": params["buses"], "w": round(params["w"], 3),
                      "low_offset": [offset(low) for low, _ in params["offsets"]],
                      "high_offset": [offset(high) for _, high in params["offsets"]]})
    return {
        "method": "Walk-forward: before each forecast time, each forecast point (horizon) learns from the past "
                  "forecasts whose outcome is known. The median is the current rank plus w times the model's predicted "
                  "move (w in [0, 1], least squares); the 80% range is widened by split conformal offsets so that each "
                  "end held 90% of past outcomes (per rank at the forecast time when it has enough examples).",
        "min_points": settings.min_points,
        "min_buses": settings.min_buses,
        "cell_min_points": settings.cell_min_points,
        "cell_min_buses": settings.cell_min_buses,
        "calibrated_share": round(calibrated_share, 3),
        "first_t": first_t,             # per point: replay t of its first calibrated forecast, null if never
        "history": {"t": history_t, "w": history_w},
        "final": final,                 # per point: what it had learned at the last forecast time
    }


def score(observed:np.ndarray, median:np.ndarray, low:np.ndarray, high:np.ndarray, last:np.ndarray,
          at_horizon:np.ndarray) -> dict:
    """Mean absolute error (in ranks: steps between statuses) of the median and of the last observed status, how often
    the median rounded to a status is the observed one (accuracy), and the share of observed statuses inside [low, high];
    None when there is nothing to evaluate."""
    def mean(values:np.ndarray) -> float | None:
        return round(float(values.mean()), 3) if values.size else None

    at_horizon = at_horizon.astype(bool)
    model_error, last_error = np.abs(median - observed), np.abs(last - observed)
    model_right = (np.floor(median + 0.5) == observed).astype(float)        # x.5 rounds up, like the viewer
    last_right = (last == observed).astype(float)
    return {
        "evaluated_points": int(observed.size),
        "mae": mean(model_error),
        "mae_last_value": mean(last_error),
        "mae_at_horizon": mean(model_error[at_horizon]),
        "mae_last_value_at_horizon": mean(last_error[at_horizon]),
        "accuracy": mean(model_right),
        "accuracy_last_value": mean(last_right),
        "accuracy_at_horizon": mean(model_right[at_horizon]),
        "accuracy_last_value_at_horizon": mean(last_right[at_horizon]),
        "coverage": mean(((low <= observed) & (observed <= high)).astype(float)),
    }


def set_forecast(replay:dict, forecast:dict) -> dict | None:
    """Stores `forecast` as the last key, after "frames": the viewer reads "name" and "start_time" from the start of
    the file, before "frames". Returns the forecast it replaces, if any."""
    previous = replay.pop("forecast", None)
    replay["frames"] = replay.pop("frames")
    replay["forecast"] = forecast
    return previous


def resolve_model(model:str) -> str:
    """'28M' (any case) -> 'mistralai/Timestral-28M'; a Hugging Face repo id (org/name) or a local folder is kept."""
    text = model.strip()
    if text.upper() in MODEL_SIZES:
        return MODEL_REPO.format(size=text.upper())
    if "/" in text or Path(text).expanduser().is_dir():
        return text
    raise RecordingError(f"Unknown --model '{model}': use {', '.join(MODEL_SIZES)}, a Hugging Face repo id "
                         f"(org/name) or a folder with config.json and model.safetensors.")


def model_name(model_id:str) -> str:
    """'mistralai/Timestral-28M' -> 'Timestral-28M'; a model folder -> its name."""
    return Path(model_id.rstrip("/\\")).name or model_id


def load_forecaster(model:str=DEFAULT_MODEL, device:str="cpu", offline:bool=False,
                    on_loading:Callable[[], None]=None) -> "TimestralAdapter":
    """Loads Timestral once (downloaded to the Hugging Face cache on first use, unless `offline`). on_loading() is
    called once Timestral and PyTorch are imported, before the model itself is loaded."""
    return TimestralAdapter(resolve_model(model), device, offline, on_loading)


class TimestralAdapter:
    """The only code that uses the Timestral API (timestral 1.0.0, see requirements-forecast.in)."""

    def __init__(self, model_id:str, device:str="cpu", offline:bool=False, on_loading:Callable[[], None]=None):
        try:
            from timestral import TimestralForecaster
        except ImportError as error:
            raise missing_module_error(error) from error
        except OSError as error:        # Windows: a PyTorch DLL did not load, e.g. no Visual C++ runtime
            raise RecordingError(f"Timestral is installed in this Python ({display_path(sys.executable)}), but PyTorch "
                                 f"could not be loaded: {error}\nInstall the Microsoft Visual C++ Redistributable "
                                 f"(https://aka.ms/vs/17/release/vc_redist.x64.exe), or recreate the forecast "
                                 f"environment.\n{INSTALL_STEPS}") from error
        if on_loading:
            on_loading()
        try:
            self.timestral = TimestralForecaster.from_pretrained(model_id, device=device, local_files_only=offline)
        except Exception as error:
            logger.debug("Model load failed", exc_info=True)
            if device.strip().lower() != "cpu" and not isinstance(error, OSError):     # OSError: cache miss, network
                hint = (f" Check --device {device}: this PyTorch must be built for it (on Windows, the requirements "
                        f"install the CPU-only build). Use --device cpu (the default), or install a PyTorch build for "
                        f"{device} in the forecast environment.")
            elif offline:
                hint = " With --offline, the model must already be in the Hugging Face cache: run once without --offline."
            else:
                hint = " The first run downloads it: check the network connection and the --model value."
            raise RecordingError(f"Could not load the forecast model {model_id} on {device} "
                                 f"({type(error).__name__}: {error}).{hint}") from error
        # Stored in the replay: no personal folders in shared files
        self.model = repo_relative(Path(model_id)) if Path(model_id).expanduser().is_dir() else model_id
        self.model_name = model_name(model_id)

    def predict(self, context:np.ndarray, prediction_length:int, quantiles:tuple[float, ...]) -> np.ndarray:
        output = self.timestral.forecast(context[:, None, :], prediction_length)    # (B, 1, T) -> (B, 1, H, levels)
        values = output.quantiles.detach().cpu().numpy()[:, 0]                       # (B, H, levels), sorted by level
        # output.quantile(q) only accepts the model's exact levels (0.01, 0.05, 0.1, ...): take the nearest ones
        levels = np.asarray(output.quantile_levels)
        nearest = [int(np.abs(levels - q).argmin()) for q in quantiles]
        return np.moveaxis(values[..., nearest], -1, 1)                               # (B, Q, H)
