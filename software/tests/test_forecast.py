import contextlib
import functools
import importlib.abc
import io
import json
import math
import re
import subprocess
import sys
import types
import unittest
from fractions import Fraction
from unittest import mock
import numpy as np
from fixtures import SOFTWARE_DIR, T0, TempDirTestCase, entity, make_recording, replay_dict, run_cli
from recording import RecordingError, forecast
from recording.cli import build_parser, main
from recording.forecast import (QUANTILES, CalibrationSettings, ForecastSettings, build_grid, compute_forecast,
                                conformal_offset, load_forecaster, model_name, occupancy_level, plan_forecast,
                                resolve_model, score, set_forecast)
from recording.replay import write_replay

MISSING = object()      # make_replay: the entity has no vehicle.occupancy_status key
NaN = math.nan


def make_replay(frames:list[dict], step:float=15.0, times:list[float]=None) -> dict:
    """frames: one {vehicle id: occupancy status} per frame, `step` seconds apart unless `times` is given."""
    replay_frames = []
    for i, statuses in enumerate(frames):
        t = float(times[i] if times is not None else i * step)
        data = []
        for vehicle, status in statuses.items():
            bus = entity(vehicle, "24", T0 + int(t))
            if status is MISSING:
                del bus["vehicle.occupancy_status"]
            else:
                bus["vehicle.occupancy_status"] = status
            data.append(bus)
        replay_frames.append({"t": t, "data": data})
    return replay_dict(replay_frames, step, "Test · forecast", [24], len({vehicle for statuses in frames for vehicle in statuses}))


def series_replay(series:dict[str, list], step:float=15.0, skip_slots:tuple=()) -> dict:
    """series: vehicle -> status on each grid slot (None: not in that frame). Slots in `skip_slots` get no frame."""
    slots = [slot for slot in range(max(map(len, series.values()))) if slot not in skip_slots]
    frames = [{vehicle: statuses[slot] for vehicle, statuses in series.items()
               if slot < len(statuses) and statuses[slot] is not None} for slot in slots]
    return make_replay(frames, step, [slot * step for slot in slots])


def levels(statuses:list) -> list[float]:
    """What the model must see for these statuses when the replay contains all of 0-5 (rank = status): 0-5 as floats,
    anything else NaN (written independently of the code)."""
    return [float(s) if isinstance(s, int) and 0 <= s <= 5 else NaN for s in statuses]


class FakeForecaster:
    """Median = last known level, 80% range = median -+ band (float32, like Timestral). Records every call.
    `fn(context, prediction_length, quantiles)` replaces that forecast."""
    model, model_name = "tests/fake-forecaster", "Fake-1M"

    def __init__(self, band:float=0.6, fn=None):
        self.band, self.fn, self.calls = band, fn, []

    def predict(self, context, prediction_length, quantiles):
        self.calls.append((context.copy(), prediction_length, tuple(quantiles)))
        if self.fn:
            return self.fn(context, prediction_length, quantiles)
        last = np.array([row[np.isfinite(row)][-1] for row in context], dtype=np.float32)
        offsets = np.array([-self.band, 0.0, self.band], dtype=np.float32)
        return np.broadcast_to(last[:, None, None] + offsets[None, :, None],
                               (len(context), len(quantiles), prediction_length)).astype(np.float32)


TIMESTRAL_LEVELS = (0.01, 0.05) + tuple(round(0.1 + 0.05 * i, 2) for i in range(17)) + (0.95, 0.99)


def stub_timestral(calls:list, fail:Exception=None) -> types.SimpleNamespace:
    """A `timestral` module without PyTorch, with Timestral's 21 quantile levels: level j forecasts the value j, so the
    levels the adapter picks come back as values. `calls` records each model load and context shape; `fail` is raised
    by the model load."""
    class Tensor:
        def __init__(self, array):
            self.array = array

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self.array

    class Model:
        def forecast(self, context, prediction_length):
            calls.append(context.shape)
            values = np.arange(len(TIMESTRAL_LEVELS), dtype=np.float32)
            return types.SimpleNamespace(quantile_levels=TIMESTRAL_LEVELS, quantiles=Tensor(np.broadcast_to(
                values, (context.shape[0], context.shape[1], prediction_length, len(values))).copy()))

    class TimestralForecaster:
        @staticmethod
        def from_pretrained(model_id, device="cpu", local_files_only=False, **_):
            calls.append(("from_pretrained", model_id, device, local_files_only))
            if fail:
                raise fail
            return Model()

    return types.SimpleNamespace(TimestralForecaster=TimestralForecaster)


class OccupancyLevelTest(unittest.TestCase):

    def test_levels_and_no_data(self):
        for value, expected in ((0, 0), (1, 1), (5, 5), (2.0, 2), ("3", 3), (" 4 ", 4), ("FULL", 5), ("full", 5),
                                ("MANY_SEATS_AVAILABLE", 1), ("CRUSHED_STANDING_ROOM_ONLY", 4)):
            with self.subTest(value=value):
                self.assertEqual(occupancy_level(value), expected)
        for value in (6, 7, 8, None, "garbage", "NO_DATA_AVAILABLE", "", -1, 2.5, "2.5", True, NaN, [1], {"a": 1}):
            with self.subTest(value=value):
                self.assertTrue(math.isnan(occupancy_level(value)))


class GridTest(unittest.TestCase):

    def test_slots_with_a_missing_poll_and_two_frames_on_one_slot(self):
        times = [0, 15.2, 29.9, 60.1, 61.0, 75]     # no frame at 45 s (slot 3); 60.1 and 61.0 both round to slot 4
        frames = [{"a": 1, "b": 2}, {"a": 2, "b": "FULL"}, {"a": 7, "b": MISSING}, {"a": 3, "b": 3}, {"a": "4"},
                  {"a": "garbage", "b": None}]
        grid = build_grid(make_replay(frames, times=times))
        self.assertEqual((grid.step, grid.slots), (15.0, 6))
        np.testing.assert_array_equal(grid.times, [0, 15.2, 29.9, NaN, 61.0, 75])
        # Statuses 1, 2, 4 and 5 are found ("3" only on the losing frame of slot 4): ranks 0, 1, 2 and 3
        self.assertEqual(grid.statuses, [1, 2, 4, 5])
        np.testing.assert_array_equal(grid.levels["a"], [0, 1, NaN, NaN, 2, NaN])
        np.testing.assert_array_equal(grid.levels["b"], [1, 3, NaN, NaN, NaN, NaN])   # the later frame of slot 4 wins
        self.assertEqual(grid.levels["a"].dtype, np.float32)

    def test_levels_are_ranks_of_the_statuses_found(self):
        # STM sends 1, 2, 3 and 5 (never 0 or 4): the scale has 4 steps, "full" is one step above "standing room only"
        grid = build_grid(series_replay({"a": [1, 2, 3, 5, 7, 3], "b": [5, 5, None, 2]}))
        self.assertEqual(grid.statuses, [1, 2, 3, 5])
        np.testing.assert_array_equal(grid.levels["a"], [0, 1, 2, 3, NaN, 2])
        np.testing.assert_array_equal(grid.levels["b"], [3, 3, NaN, 1, NaN, NaN])
        self.assertEqual(build_grid(series_replay({"a": [7, 8, None]})).statuses, [])

    def test_vehicle_ids_are_strings_and_fall_back_to_the_entity_id(self):
        replay = make_replay([{"41053": 1}])
        bus = replay["frames"][0]["data"][0]
        del bus["vehicle.vehicle.id"]
        bus["id"] = 41053
        self.assertEqual(list(build_grid(replay).levels), ["41053"])


class PlanTest(unittest.TestCase):

    def setUp(self):
        # 13 slots of 15 s, no frame on slot 4 (60 s)
        self.replay = series_replay({
            "a": [1] * 13,
            "b": [None, None] + [2] * 11,                      # appears on slot 2
            "c": [1, 7, 7, 7, 7, 7, 7, 7, 1, 7, 7, 7, 1],      # only 3 known levels
            "d": [1] * 8 + [8] + [1] * 4,                      # unknown on slot 8
        }, skip_slots=(4,))

    def test_origin_spacing_and_min_history(self):
        plan = plan_forecast(self.replay, ForecastSettings(min_history=120))
        # Origins every 4 slots (60 s); a bus needs 8 slots (120 s) since its first level, 4 known levels and a known
        # level at the origin. Slot 4 has no frame.
        self.assertEqual(plan.origins, [(8, ["a"]), (12, ["a", "b", "d"])])
        self.assertEqual((plan.vehicles, plan.forecasts), (3, 4))
        plan = plan_forecast(self.replay, ForecastSettings(min_history=0))
        self.assertEqual(plan.origins, [(8, ["a", "b"]), (12, ["a", "b", "d"])])
        plan = plan_forecast(self.replay, ForecastSettings(origin_every=120))
        self.assertEqual((plan.origins, plan.settings.origin_every), ([(8, ["a"])], 120))
        self.assertEqual(plan_forecast(self.replay, ForecastSettings(origin_every=50)).settings.origin_every, 45)

    def test_point_spacing_and_count(self):
        plan = plan_forecast(self.replay)
        self.assertEqual((plan.points, plan.point_slots, plan.prediction_length), (20, 2, 40))
        self.assertEqual((plan.settings.horizon, plan.settings.point_every), (600, 30))
        plan = plan_forecast(self.replay, ForecastSettings(horizon=610, point_every=45))
        self.assertEqual((plan.points, plan.point_slots, plan.settings.horizon), (13, 3, 585))
        plan = plan_forecast(series_replay({"a": [1] * 13}, step=20))         # 30 s is not a multiple of 20 s
        self.assertEqual((plan.settings.point_every, plan.points, plan.settings.horizon), (40, 15, 600))
        with self.assertRaisesRegex(RecordingError, "25 s is not a multiple of 15 s"):
            plan_forecast(self.replay, ForecastSettings(point_every=25))
        with self.assertRaisesRegex(RecordingError, "at least one forecast point"):
            plan_forecast(self.replay, ForecastSettings(horizon=20))

    def test_nothing_to_forecast_is_a_friendly_error(self):
        with self.assertRaisesRegex(RecordingError, "reports its occupancy"):
            plan_forecast(series_replay({"a": [7] * 13}))
        with self.assertRaisesRegex(RecordingError, "No bus has 5 min of occupancy history"):
            plan_forecast(self.replay, ForecastSettings(min_history=300))


class ComputeTest(unittest.TestCase):

    def test_the_model_sees_nothing_after_the_forecast_time(self):
        a = [1, 2, 3, 2, 1, 2, 3, 4, 5, 4, 3, 2, 1, 0, 1, 2, 3]
        b = [None, None, None, 3, 7, 3, 4, None, 4, 5, 5, 0, 0, 5, 5, 5, 5]     # from slot 3, unknown on 4, absent on 7
        replay = series_replay({"b": b, "a": a})
        settings = ForecastSettings(horizon=60, origin_every=60, min_history=60)
        fake = FakeForecaster()
        result = compute_forecast(plan_forecast(replay, settings), fake)

        # Origins every 4 slots; "a" from slot 4 on (60 s of history), "b" from slot 8 on (its level on 4 is unknown)
        expected = {4: [a[:5]], 8: [a[:9], b[3:9]], 12: [a[:13], b[3:13]], 16: [a[:17], b[3:17]]}
        self.assertEqual(len(fake.calls), len(expected))
        for (context, length, quantiles), (origin, rows) in zip(fake.calls, expected.items()):
            with self.subTest(origin=origin):
                self.assertEqual((context.dtype, length, quantiles), (np.float32, 4, QUANTILES))
                width = max(map(len, rows))
                padded = [[NaN] * (width - len(row)) + levels(row) for row in rows]
                np.testing.assert_array_equal(context, np.array(padded, dtype=np.float32))
        self.assertEqual([entry["t"] for entry in result.forecast["vehicles"]["a"]], [60.0, 120.0, 180.0, 240.0])
        self.assertEqual([entry["t"] for entry in result.forecast["vehicles"]["b"]], [120.0, 180.0, 240.0])
        self.assertEqual(list(result.forecast["vehicles"]), ["a", "b"])

        with mock.patch.object(forecast, "MAX_CONTEXT", 6):          # only the 6 most recent steps
            fake = FakeForecaster()
            compute_forecast(plan_forecast(replay, settings), fake)
        np.testing.assert_array_equal(fake.calls[2][0], np.array([levels(a[7:13]), levels(b[7:13])], dtype=np.float32))

        with mock.patch.object(forecast, "MAX_BATCH", 1):
            fake = FakeForecaster()
            self.assertEqual(compute_forecast(plan_forecast(replay, settings), fake).forecast["vehicles"],
                             result.forecast["vehicles"])
        self.assertEqual(len(fake.calls), 7)

    def test_origins_are_the_frame_times(self):
        jitter = [0.0, 0.01, -0.012, 0.004, -0.003] * 5         # real polls are a few ms off the 15 s grid,
        times = [5 + 15 * i + jitter[i] for i in range(25)]   # and the first frame is not at t = 0
        replay = make_replay([{"a": 1 + i % 3} for i in range(25)], times=times)
        result = compute_forecast(plan_forecast(replay), FakeForecaster())
        self.assertEqual([entry["t"] for entry in result.forecast["vehicles"]["a"]],
                         [times[8], times[12], times[16], times[20], times[24]])

    def test_stored_points_are_rounded_clipped_and_ordered(self):
        def reversed_and_out_of_range(context, length, quantiles):
            steps = np.arange(length, dtype=np.float32) / 10                    # 0.0, 0.1, ... 3.9 for step 0 ... 39
            rows = [steps + np.float32(2.123456), steps, steps - np.float32(0.333333)]    # high, median, low
            return np.broadcast_to(np.stack(rows), (len(context), 3, length)).astype(np.float32)

        progress = []
        replay = series_replay({"a": [0, 1, 2, 3, 4, 5] * 5})        # all six statuses: ranks 0-5
        result = compute_forecast(plan_forecast(replay), FakeForecaster(fn=reversed_and_out_of_range),
                                  on_progress=lambda *args: progress.append(args))
        entries = result.forecast["vehicles"]["a"]
        self.assertEqual([entry["t"] for entry in entries], [120.0, 180.0, 240.0, 300.0, 360.0, 420.0])
        self.assertEqual(progress[0], (1, 6, 120.0, 1))
        self.assertEqual((result.vehicles, result.origins, result.forecasts, result.points), (1, 6, 6, 120))

        steps = [np.float32(step) / np.float32(10) for step in range(1, 40, 2)]   # kept model steps: +30 s ... +600 s
        expected = [[round(max(0.0, float(s - np.float32(0.333333))), 2) for s in steps],
                    [round(float(s), 2) for s in steps],
                    [round(min(5.0, float(s + np.float32(2.123456))), 2) for s in steps]]
        self.assertEqual(entries[0]["q"], expected)
        self.assertEqual((expected[0][0], expected[1][0], expected[2][-1]), (0.0, 0.1, 5.0))
        for value in (value for entry in entries for row in entry["q"] for value in row):
            self.assertIs(type(value), float)
            self.assertEqual(value, round(value, 2))
        self.assertIsNone(re.search(r"\d\.\d{3}", json.dumps(entries)))

    def test_forecasts_are_ranks_clipped_to_the_statuses_found(self):
        too_high = lambda context, length, quantiles: np.broadcast_to(
            np.array([2.5, 3.2, 9.0], dtype=np.float32)[None, :, None], (len(context), 3, length))
        fake = FakeForecaster(fn=too_high)
        result = compute_forecast(plan_forecast(series_replay({"a": [1, 2, 3, 5] * 4})), fake)
        self.assertEqual(result.forecast["levels"], [1, 2, 3, 5])        # rank r stands for status levels[r]
        np.testing.assert_array_equal(fake.calls[0][0], [[0, 1, 2, 3] * 2 + [0]])      # the model sees ranks
        self.assertEqual({tuple(row[:1]) for entry in result.forecast["vehicles"]["a"] for row in entry["q"]},
                         {(2.5,), (3.0,)})                                       # clipped to rank 3 ("full")

    def test_metrics_on_a_hand_computed_example(self):
        # Statuses 1, 2, 3 and 5 are ranks 0-3: the series is 0 0 0 0 1 1 2 2 0 - 3 in ranks.
        # Origins on slots 4 (last rank 1) and 8 (last rank 0); points every 2 slots, 2 points ahead.
        # Observed: slot 6 -> 2, slot 8 -> 0, slot 10 -> 3, slot 12 is after the end. Median 2.5, range 1.5-3.
        replay = series_replay({"a": [1, 1, 1, 1, 2, 2, 3, 3, 1, None, 5]})
        constant = lambda context, length, quantiles: np.broadcast_to(
            np.array([1.5, 2.5, 3.0], dtype=np.float32)[None, :, None], (len(context), 3, length))
        settings = ForecastSettings(horizon=60, origin_every=60, min_history=60)
        result = compute_forecast(plan_forecast(replay, settings), FakeForecaster(fn=constant))
        self.assertEqual(result.forecast["levels"], [1, 2, 3, 5])
        self.assertEqual(result.metrics, {
            "evaluated_points": 3,
            "mae": 1.167,                       # (|2.5-2| + |2.5-0| + |2.5-3|) / 3
            "mae_last_value": 1.667,            # (|1-2| + |1-0| + |0-3|) / 3
            "mae_at_horizon": 2.5,              # slot 8 only (origin 4 + 2 points)
            "mae_last_value_at_horizon": 1.0,
            "accuracy": 0.333,                  # 2.5 rounds to 3: right on slot 10 only
            "accuracy_last_value": 0.0,
            "accuracy_at_horizon": 0.0,
            "accuracy_last_value_at_horizon": 0.0,
            "coverage": 0.667,                  # 2 and 3 are inside [1.5, 3], 0 is not
        })

    def test_accuracy_rounds_half_up_like_the_viewer(self):
        # 1.5 -> 2 (right), 2.5 -> 3, 0.5 -> 1, 1.49 -> 1 (right); rounding half to even would make all four right
        median = np.array([1.5, 2.5, 0.5, 1.49])
        metrics = score(observed=np.array([2.0, 2.0, 0.0, 1.0]), median=median, low=median, high=median,
                        last=np.array([2.0, 1.0, 0.0, 1.0]), at_horizon=np.array([False, False, False, True]))
        self.assertEqual((metrics["accuracy"], metrics["accuracy_last_value"], metrics["accuracy_at_horizon"]),
                         (0.5, 0.75, 1.0))

    def test_metrics_without_anything_to_compare_are_null(self):
        replay = series_replay({"a": [1] * 9})       # the only forecast (slot 8) is at the end of the replay
        result = compute_forecast(plan_forecast(replay), FakeForecaster())
        self.assertEqual(result.metrics, {"evaluated_points": 0, "mae": None, "mae_last_value": None,
                                          "mae_at_horizon": None, "mae_last_value_at_horizon": None, "accuracy": None,
                                          "accuracy_last_value": None, "accuracy_at_horizon": None,
                                          "accuracy_last_value_at_horizon": None, "coverage": None})
        json.dumps(result.forecast, allow_nan=False)

    def test_last_value_forecast_scores_like_the_baseline(self):
        replay = series_replay({"a": [1, 1, 2, 2, 3, 3, 2, 1] * 6, "b": [None] * 5 + [3, 3, 4, 7, 4, 5, 4] * 6})
        metrics = compute_forecast(plan_forecast(replay), FakeForecaster()).metrics
        self.assertGreater(metrics["evaluated_points"], 100)
        self.assertEqual(metrics["mae"], metrics["mae_last_value"])
        self.assertEqual(metrics["mae_at_horizon"], metrics["mae_last_value_at_horizon"])
        self.assertEqual(metrics["accuracy"], metrics["accuracy_last_value"])
        self.assertEqual(metrics["accuracy_at_horizon"], metrics["accuracy_last_value_at_horizon"])

    def test_bad_model_output(self):
        replay = series_replay({"a": [1] * 13, "b": [2] * 13, "c": [3] * 13})
        plan = plan_forecast(replay)
        # An infinity must be dropped like a NaN, not clipped into "Full"/"Empty"
        nan_for_b_inf_for_c = lambda context, length, quantiles: np.stack(
            [np.full((3, length), 1.0), np.full((3, length), NaN), np.full((3, length), math.inf)]).astype(np.float32)
        with self.assertLogs("Forecast", "WARNING"):
            result = compute_forecast(plan, FakeForecaster(fn=nan_for_b_inf_for_c))
        self.assertEqual((list(result.forecast["vehicles"]), result.skipped), (["a"], 4))
        with self.assertRaisesRegex(RecordingError, r"returned an array of shape \(3, 3, 39\), expected \(3, 3, 40\)"):
            compute_forecast(plan, FakeForecaster(fn=lambda context, length, quantiles: np.zeros((3, 3, length - 1))))

        def degenerate(context, length, quantiles):
            raise ValueError("forecast produced 3 NaN values; the input is degenerate")
        with self.assertRaisesRegex(RecordingError, r"Fake-1M failed on the forecast at t=120 s \(3 buses\): .*degenerate"):
            compute_forecast(plan, FakeForecaster(fn=degenerate))


class CalibrationTest(unittest.TestCase):
    """Walk-forward calibration (Calibrator), with thresholds low enough for a few test buses."""
    SETTINGS = CalibrationSettings(min_points=6, min_buses=2, cell_min_points=1000)     # one range offset per horizon

    def setUp(self):
        # 4 buses moving through statuses 1, 2, 3 and 5 (ranks 0-3); a forecast every 2 slots, 4 points 2 slots apart
        pattern = [1, 1, 2, 2, 3, 3, 2, 1, 2, 3, 5, 3]
        self.series = {f"b{i}": (pattern[i:] + pattern[:i]) * 3 for i in range(4)}
        self.plan = plan_forecast(series_replay(self.series), ForecastSettings(horizon=120, origin_every=30,
                                                                               min_history=30))

    @staticmethod
    def predicts_a_rise(context, length, quantiles):
        last = np.array([row[np.isfinite(row)][-1] for row in context], dtype=np.float32)
        median = last[:, None] + 0.8 * np.arange(1, length + 1, dtype=np.float32)[None, :] / length
        return np.stack([median - 0.5, median, median + 0.5], axis=1).astype(np.float32)

    def run_forecast(self, plan, settings:CalibrationSettings) -> dict:
        return compute_forecast(plan, FakeForecaster(fn=self.predicts_a_rise), calibration=settings).forecast

    def check_walk_forward(self, settings:CalibrationSettings) -> tuple[dict, dict, int, int]:
        """Recomputes every stored point by hand from the raw forecasts, in time order as the forecast tool goes.
        Returns the raw and calibrated forecasts, the points checked, and how many of them used the range offsets of
        their status at the forecast time that differ from those of the whole horizon."""
        def offset(scores:list) -> float:                   # conformal: the 90% tail of n + 1, never narrowing
            k = math.ceil(Fraction(9, 10) * (len(scores) + 1))
            return max(sorted(scores)[k - 1], 0) if k <= len(scores) else math.inf

        def offsets(examples:list) -> tuple[float, float]:
            return offset([e[4] - e[6] for e in examples]), offset([e[6] - e[5] for e in examples])

        raw = self.run_forecast(self.plan, CalibrationSettings(enabled=False))
        calibrated = self.run_forecast(self.plan, settings)
        grid, top = self.plan.grid, 3
        entries = {(vehicle, entry["t"]): entry["q"] for vehicle, items in raw["vehicles"].items() for entry in items}
        stored = {(vehicle, entry["t"]): entry["q"] for vehicle, items in calibrated["vehicles"].items() for entry in items}
        examples = [[] for _ in range(self.plan.points)]    # past raw forecasts: (target slot, bus, L, median, low, high, y)
        checked = per_status = 0
        for slot, vehicles in self.plan.origins:
            t = float(grid.times[slot])
            for h in range(self.plan.points):
                past = [e for e in examples[h] if e[0] <= slot]
                for vehicle in vehicles:
                    q_raw, q = entries[(vehicle, t)], stored[(vehicle, t)]
                    level = float(grid.levels[vehicle][slot])
                    if len(past) < settings.min_points or len({e[1] for e in past}) < settings.min_buses:
                        self.assertEqual([row[h] for row in q], [row[h] for row in q_raw])
                        continue
                    move = np.array([e[3] - e[2] for e in past])
                    happened = np.array([e[6] - e[2] for e in past])
                    w = float(np.clip((move * happened).sum() / (move * move).sum(), 0, 1))
                    q_low, q_high = offsets(past)
                    same_status = [e for e in past if e[2] == level]     # same status at the forecast time
                    if len(same_status) >= settings.cell_min_points and \
                            len({e[1] for e in same_status}) >= settings.cell_min_buses:
                        per_status += offsets(same_status) != (q_low, q_high)
                        q_low, q_high = offsets(same_status)
                    median = round(min(max(level + w * (q_raw[1][h] - level), 0), top), 2)
                    expected = (round(min(max(min(q_raw[0][h] - q_low, median), 0), top), 2), median,
                                round(min(max(max(q_raw[2][h] + q_high, median), 0), top), 2))
                    self.assertEqual((q[0][h], q[1][h], q[2][h]), expected, (vehicle, slot, h))
                    checked += 1
            for vehicle in vehicles:                        # recorded after the forecasts of this origin
                q_raw = entries[(vehicle, t)]
                for h in range(self.plan.points):
                    target = slot + self.plan.point_slots * (h + 1)
                    if target < grid.slots and np.isfinite(grid.levels[vehicle][target]):
                        examples[h].append((target, vehicle, float(grid.levels[vehicle][slot]), q_raw[1][h], q_raw[0][h],
                                            q_raw[2][h], float(grid.levels[vehicle][target])))
        return raw, calibrated, checked, per_status

    def test_range_offsets_per_status_at_the_forecast_time(self):
        # Low thresholds: statuses with enough past examples get their own range offsets, the others the horizon's
        _, _, checked, per_status = self.check_walk_forward(
            CalibrationSettings(min_points=6, min_buses=2, cell_min_points=10, cell_min_buses=2))
        self.assertGreater(checked, 50)
        self.assertGreater(per_status, 20)

    def test_stored_forecasts_follow_the_walk_forward_formulas(self):
        raw, calibrated, checked, per_status = self.check_walk_forward(self.SETTINGS)
        self.assertEqual(per_status, 0)
        self.assertGreater(checked, 50)
        for entries in calibrated["vehicles"].values():
            for entry in entries:
                for low, median, high in zip(*entry["q"]):
                    self.assertTrue(0 <= low <= median <= high <= 3)
                    self.assertEqual((low, median, high), (round(low, 2), round(median, 2), round(high, 2)))
        self.assertEqual(calibrated["model_name"], "Fake-1M + calibration")
        self.assertEqual(calibrated["metrics_raw"], raw["metrics"])
        info = calibrated["calibration"]
        self.assertEqual((len(info["first_t"]), len(info["final"]), len(info["history"]["t"])),
                         (self.plan.points, self.plan.points, len(self.plan.origins)))
        self.assertTrue(0 < info["calibrated_share"] < 1)
        json.dumps(calibrated, allow_nan=False)

    def test_a_forecast_never_uses_what_happens_after_it(self):
        before = self.run_forecast(self.plan, self.SETTINGS)
        cut = 20                                            # change every status after slot 20, the next one included
        changed = {vehicle: statuses[:cut + 1] + [5 if s == 1 else 1 for s in statuses[cut + 1:]]
                   for vehicle, statuses in self.series.items()}
        plan = plan_forecast(series_replay(changed), ForecastSettings(horizon=120, origin_every=30, min_history=30))
        self.assertEqual(plan.grid.statuses, self.plan.grid.statuses)      # same scale
        after = self.run_forecast(plan, self.SETTINGS)
        t_cut = float(self.plan.grid.times[cut])
        compared = 0
        for vehicle, entries in before["vehicles"].items():
            for old, new in zip(entries, after["vehicles"][vehicle]):
                if old["t"] <= t_cut:
                    self.assertEqual(old, new)
                    compared += 1
        self.assertGreater(compared, 10)
        self.assertNotEqual(before["vehicles"], after["vehicles"])

    def test_too_few_past_outcomes_keeps_the_raw_output(self):
        raw = self.run_forecast(self.plan, CalibrationSettings(enabled=False))
        for settings in (CalibrationSettings(), CalibrationSettings(enabled=False)):     # 4 buses: below 20
            with self.subTest(settings=settings):
                forecast = self.run_forecast(self.plan, settings)
                self.assertEqual((forecast["vehicles"], forecast["metrics"], forecast["model_name"]),
                                 (raw["vehicles"], raw["metrics"], "Fake-1M"))
                self.assertNotIn("metrics_raw", forecast)
                self.assertNotIn("calibration", forecast)

    def test_a_model_whose_moves_never_happen_is_not_trusted(self):
        # Statuses never change, the model always predicts a rise: w = 0, so the calibrated median is the current level
        plan = plan_forecast(series_replay({"a": [1] * 30, "b": [2] * 30, "c": [3] * 30}),
                             ForecastSettings(horizon=120, origin_every=30, min_history=30))
        forecast = self.run_forecast(plan, self.SETTINGS)
        weights = [w for row in forecast["calibration"]["history"]["w"] for w in row if w is not None]
        self.assertTrue(weights and all(w == 0 for w in weights))
        for vehicle, entries in forecast["vehicles"].items():
            level = {"a": 0, "b": 1, "c": 2}[vehicle]
            for entry, w_row in zip(entries, forecast["calibration"]["history"]["w"]):
                for h, w in enumerate(w_row):
                    if w is not None:
                        self.assertEqual(entry["q"][1][h], level)

    def test_conformal_offset(self):
        self.assertEqual(conformal_offset(np.arange(1.0, 10.0)), 9.0)           # n = 9: the 9th smallest
        self.assertEqual(conformal_offset(np.arange(100.0)), 90.0)              # n = 100: the 91st smallest
        self.assertEqual(conformal_offset(np.arange(1.0, 9.0)), math.inf)       # n = 8: there is no 9th
        self.assertEqual(conformal_offset(np.full(20, -1.0)), 0.0)              # never narrows the range
        self.assertEqual(conformal_offset(np.empty(0)), math.inf)


class ModelTest(TempDirTestCase):

    def test_model_aliases(self):
        for alias, expected in (("28M", "mistralai/Timestral-28M"), ("28m", "mistralai/Timestral-28M"),
                                (" 5M ", "mistralai/Timestral-5M"), ("209m", "mistralai/Timestral-209M"),
                                ("my-org/fine-tuned", "my-org/fine-tuned"), (str(self.tmp), str(self.tmp))):
            with self.subTest(alias=alias):
                self.assertEqual(resolve_model(alias), expected)
        with self.assertRaisesRegex(RecordingError, "Unknown --model 'huge': use 5M, 28M, 123M, 209M"):
            resolve_model("huge")
        self.assertEqual(model_name("mistralai/Timestral-28M"), "Timestral-28M")
        self.assertEqual(model_name(str(self.tmp / "my-model") + "\\"), "my-model")

    def test_missing_timestral_explains_how_to_install_it(self):
        with mock.patch.dict(sys.modules, {"timestral": None}), self.assertRaises(RecordingError) as caught:
            load_forecaster("28M")
        message = str(caught.exception)
        for expected in ("'timestral' is not installed", "uv venv .venv-forecast --python 3.12",
                         "-r software\\recording\\requirements-forecast.in", "python -m venv .venv-forecast"):
            self.assertIn(expected, message)

    def test_a_pytorch_that_does_not_load_explains_what_to_do(self):
        class DllNotLoaded(importlib.abc.MetaPathFinder):       # what `import torch` raises without the VC++ runtime
            def find_spec(self, name, path, target=None):
                if name == "timestral":
                    raise OSError('[WinError 126] Error loading "torch\\lib\\c10.dll" or one of its dependencies.')

        with mock.patch.dict(sys.modules), mock.patch.object(sys, "meta_path", [DllNotLoaded()] + sys.meta_path):
            sys.modules.pop("timestral", None)
            with self.assertRaises(RecordingError) as caught:
                load_forecaster("28M")
        message = str(caught.exception)
        for expected in ("PyTorch could not be loaded: [WinError 126]", "Visual C++ Redistributable",
                         "uv venv .venv-forecast --python 3.12"):
            self.assertIn(expected, message)

    def test_adapter_takes_the_nearest_model_levels(self):
        calls = []
        with mock.patch.dict(sys.modules, {"timestral": stub_timestral(calls)}):
            adapter = load_forecaster("28M", on_loading=lambda: calls.append("loading"))
        self.assertEqual((adapter.model, adapter.model_name), ("mistralai/Timestral-28M", "Timestral-28M"))
        values = adapter.predict(np.zeros((2, 7), dtype=np.float32), 40, QUANTILES)
        self.assertEqual(calls, ["loading", ("from_pretrained", "mistralai/Timestral-28M", "cpu", False), (2, 1, 7)])
        self.assertEqual(values.shape, (2, 3, 40))
        np.testing.assert_array_equal(values[:, :, 0], [[2, 10, 18], [2, 10, 18]])     # levels 0.1, 0.5 and 0.9

    def test_model_load_errors_give_a_hint_for_their_cause(self):
        cache_miss, no_cuda = OSError("not in the cache"), AssertionError("Torch not compiled with CUDA enabled")
        cases = [(cache_miss, "cpu", "With --offline, the model must already be in the Hugging Face cache", "--device"),
                 (cache_miss, "cuda", "With --offline", "--device"),
                 (no_cuda, "cuda", "Check --device cuda: this PyTorch must be built for it", "--offline")]
        for error, device, expected, unexpected in cases:
            with self.subTest(error=error, device=device), self.assertRaises(RecordingError) as caught, \
                    mock.patch.dict(sys.modules, {"timestral": stub_timestral([], fail=error)}):
                load_forecaster("28M", device, offline=True)
            message = str(caught.exception)
            self.assertIn(f"on {device} ({type(error).__name__}: {error}).", message)
            self.assertIn(expected, message)
            self.assertNotIn(unexpected, message)


class ForecastCommandTest(TempDirTestCase):

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "replay.json"
        write_replay(series_replay({"40902": [1, 1, 2, 2, 3, 3, 2, 1] * 5, "41053": [None] * 6 + [2, 3, "FULL"] * 10}),
                     self.path)

    def forecast(self, *args) -> tuple[int, str, str]:
        with mock.patch("recording.forecast.load_forecaster", return_value=FakeForecaster()) as self.load:
            return run_cli("forecast", str(self.path), *args)

    def test_forecast_is_stored_after_the_frames_and_replaced_on_a_new_run(self):
        code, stdout, _ = self.forecast()
        self.assertEqual(code, 0)
        self.load.assert_called_once_with("28M", "cpu", False, on_loading=mock.ANY)
        raw = self.path.read_text(encoding="utf-8")
        replay = json.loads(raw)
        self.assertEqual(list(replay)[-2:], ["frames", "forecast"])
        head = raw[:raw.index('"frames"')]
        self.assertIn('"name":"Test · forecast"', head)
        self.assertIn('"start_time":', head)
        self.assertGreater(raw.index('"forecast":'), raw.index('"frames":'))

        stored = replay["forecast"]
        self.assertEqual({key: stored[key] for key in ("format", "version", "model", "model_name", "target", "levels",
                                                        "step", "point_every", "horizon", "origin_every", "min_history",
                                                        "quantiles")},
                         {"format": "stm-dt-occupancy-forecast", "version": 2, "model": "tests/fake-forecaster",
                          "model_name": "Fake-1M", "target": "vehicle.occupancy_status", "levels": [1, 2, 3, 5],
                          "step": 15.0, "point_every": 30.0, "horizon": 600.0, "origin_every": 60.0,
                          "min_history": 120.0, "quantiles": [0.1, 0.5, 0.9]})
        for key in ("step", "point_every", "horizon", "origin_every", "min_history"):
            self.assertIs(type(stored[key]), float, key)        # seconds, as floats (the CLI defaults are ints)
        self.assertTrue(stored["created_at"].endswith("Z"))
        self.assertEqual(list(stored["vehicles"]), ["40902", "41053"])
        for entries in stored["vehicles"].values():
            self.assertEqual([entry["t"] for entry in entries], sorted(entry["t"] for entry in entries))
            for entry in entries:
                self.assertEqual([len(row) for row in entry["q"]], [20, 20, 20])
                for low, median, high in zip(*entry["q"]):
                    self.assertTrue(0 <= low <= median <= high <= 3)        # ranks of the 4 statuses
        self.assertEqual(stored["metrics"]["mae"], stored["metrics"]["mae_last_value"])
        # Two buses: never enough past outcomes to calibrate (200 from 20 buses), so the raw output is stored
        self.assertEqual(("metrics_raw" in stored, "calibration" in stored), (False, False))
        for expected in ("Saved the forecast in", "calibrated: none", "Fake-1M", "\"no change\"", "right", "error (steps)",
                         "80% range holds", "MANY_SEATS_AVAILABLE, FEW_SEATS_AVAILABLE, STANDING_ROOM_ONLY, FULL",
                         "Next step"):
            self.assertIn(expected, stdout)
        self.assertNotIn("replaced", stdout)

        code, stdout, _ = self.forecast("--horizon-minutes", "5", "--model", "5m", "--offline")
        self.assertEqual(code, 0)
        self.load.assert_called_once_with("5m", "cpu", True, on_loading=mock.ANY)
        self.assertIn("replaced the existing forecast (model Fake-1M)", stdout)
        raw = self.path.read_text(encoding="utf-8")
        self.assertEqual(raw.count('"forecast":'), 1)
        replay = json.loads(raw)
        self.assertEqual((list(replay)[-2:], replay["forecast"]["horizon"]), (["frames", "forecast"], 300.0))

    def test_set_forecast_keeps_the_header_before_the_frames(self):
        replay = {"frames": [], "format": "stm-dt-replay", "name": "x", "forecast": {"old": True}, "start_time": 1}
        self.assertEqual(set_forecast(replay, {"new": True}), {"old": True})
        self.assertEqual(list(replay), ["format", "name", "start_time", "frames", "forecast"])

    def test_errors_are_friendly_and_do_not_load_the_model(self):
        not_a_replay = self.tmp / "other.json"
        not_a_replay.write_text('{"format": "something-else"}', encoding="utf-8")
        no_occupancy = self.tmp / "no_occupancy.json"
        write_replay(series_replay({"40902": [7] * 20}), no_occupancy)
        cases = [([str(self.tmp / "missing.json")], "Replay not found"),
                 ([str(not_a_replay)], "is not a replay"),
                 ([str(no_occupancy)], "reports its occupancy"),
                 ([str(self.path), "--horizon-minutes", "0"], "--horizon-minutes and --every-minutes must be"),
                 ([str(self.path), "--model", "huge"], "Unknown --model 'huge'")]
        for args, expected in cases:
            with self.subTest(args=args), mock.patch("recording.forecast.load_forecaster") as load, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(["forecast", *args]), 2)
                load.assert_not_called()
            self.assertIn(expected, stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_percentages_round_half_up_like_the_viewer(self):
        from recording.cli import _accuracy_text, _percent_text
        self.assertEqual([_percent_text(value) for value in (0.725, 0.745, 0.7296, 0.0, 1.0, None)],
                         ["73%", "75%", "73%", "0%", "100%", "n/a"])
        # Accuracies get one decimal: 0.740 and 0.743 must not both print as 74%
        self.assertEqual([_accuracy_text(value) for value in (0.74, 0.743, 0.7255, None)],
                         ["74.0%", "74.3%", "72.6%", "n/a"])

    def test_a_calibrated_forecast_prints_its_scores_beside_the_raw_ones(self):
        from recording.cli import _percent_text
        # Thresholds low enough for the 2 test buses (run_forecast takes CalibrationSettings from recording.forecast)
        with mock.patch("recording.forecast.CalibrationSettings",
                        functools.partial(CalibrationSettings, min_points=6, min_buses=2)):
            code, stdout, _ = self.forecast()
        self.assertEqual(code, 0)
        stored = json.loads(self.path.read_text(encoding="utf-8"))["forecast"]
        self.assertEqual(stored["model_name"], "Fake-1M + calibration")
        self.assertIn(f"calibrated: {_percent_text(stored['calibration']['calibrated_share'])} of the stored points "
                      f"(a forecast point is calibrated once it has 6 past outcomes from 2 buses)", stdout)
        self.assertRegex(stdout, r"calibrated +raw Fake-1M +\"no change\"")
        self.assertRegex(stdout, r"right( +\d+\.\d%){3}\n")
        self.assertRegex(stdout, r"80% range holds( +\d+%){3}\n")

    def test_cli_defaults_are_those_of_the_module(self):
        args = build_parser().parse_args(["forecast", "x.json"])
        defaults = ForecastSettings()
        self.assertEqual((args.horizon_minutes * 60, args.every_minutes * 60, args.min_history_minutes * 60, args.model),
                         (defaults.horizon, defaults.origin_every, defaults.min_history, forecast.DEFAULT_MODEL))

    def test_forecast_help_does_not_import_numpy_or_the_live_service_modules(self):
        run = subprocess.run([sys.executable, "-X", "importtime", str(SOFTWARE_DIR / "recording"), "forecast", "--help"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("--horizon-minutes", run.stdout)
        imported = {line.rsplit("|", 1)[1].strip() for line in run.stderr.splitlines() if line.startswith("import time:")}
        self.assertIn("recording.cli", imported)
        heavy = {"numpy", "torch", "timestral", "pandas", "requests", "google.protobuf", "pika"}
        self.assertEqual(imported & heavy, set())


class ExportWithForecastTest(TempDirTestCase):

    def setUp(self):
        super().setUp()
        recorded = [(T0 + 15 * i, [entity("41053", "121", T0 + 15 * i - 4, **{"vehicle.occupancy_status": 1 + i % 3}),
                                   entity("31841", "45", T0 + 15 * i - 2)]) for i in range(20)]
        self.recording = make_recording(self.tmp / "day.sqlite", recorded)

    def test_export_then_forecast(self):
        out = self.tmp / "replay.json"
        with mock.patch("recording.forecast.load_forecaster", return_value=FakeForecaster()), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["export", str(self.recording), "--out", str(out), "--forecast"]), 0)
        replay = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(list(replay["forecast"]["vehicles"]), ["31841", "41053"])
        self.assertIn("Exported", stdout.getvalue())
        self.assertIn("Saved the forecast in", stdout.getvalue())

    def test_export_without_timestral_still_writes_the_replay(self):
        out = self.tmp / "replay.json"
        with mock.patch.dict(sys.modules, {"timestral": None}), contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["export", str(self.recording), "--out", str(out), "--forecast"]), 2)
        replay = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((len(replay["frames"]), "forecast" in replay), (20, False))
        self.assertIn("'timestral' is not installed", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn("Loading", stdout.getvalue())          # the missing module is reported before any loading
        self.assertIn("The replay was written without a forecast", stdout.getvalue())
        self.assertRegex(stdout.getvalue(), r'\.venv-forecast[\\/]Scripts[\\/]python\S* .*forecast ".*replay\.json"')

    def test_export_without_occupancy_does_not_suggest_adding_the_forecast_later(self):
        recorded = [(T0 + 15 * i, [entity("41053", "121", T0 + 15 * i - 4, **{"vehicle.occupancy_status": 7})])
                    for i in range(20)]
        recording = make_recording(self.tmp / "no_occupancy.sqlite", recorded)
        out = self.tmp / "replay.json"
        with mock.patch("recording.forecast.load_forecaster") as load, contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["export", str(recording), "--out", str(out), "--forecast"]), 2)
        load.assert_not_called()
        self.assertNotIn("forecast", json.loads(out.read_text(encoding="utf-8")))
        self.assertIn("No bus in this replay reports its occupancy", stderr.getvalue())
        self.assertIn("The replay was written without a forecast", stdout.getvalue())
        self.assertNotIn("Next step", stdout.getvalue())

    def test_a_model_runtime_error_is_friendly_and_keeps_the_replay(self):
        def out_of_memory(context, length, quantiles):
            raise RuntimeError("CUDA out of memory")        # torch.OutOfMemoryError is a RuntimeError
        out = self.tmp / "replay.json"
        with mock.patch("recording.forecast.load_forecaster", return_value=FakeForecaster(fn=out_of_memory)), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["export", str(self.recording), "--out", str(out), "--forecast"]), 2)
            exported = out.read_bytes()
            self.assertEqual(main(["forecast", str(out)]), 2)
        self.assertEqual(out.read_bytes(), exported)            # `forecast` leaves the file as it was
        self.assertNotIn('"forecast":', exported.decode("utf-8"))
        self.assertEqual(stderr.getvalue().count("RuntimeError: CUDA out of memory"), 2)
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertIn("The replay was written without a forecast", stdout.getvalue())
        self.assertIn("add the forecast later", stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
