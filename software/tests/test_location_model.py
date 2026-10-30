import copy
import dataclasses
import json
import math
import random
import unittest
from unittest import mock
import numpy as np
from fixtures import T0, TempDirTestCase, entity, replay_dict, run_cli
from recording import location_model
from recording.forecast import QUANTILES, build_grid, plan_forecast
from recording.location_model import (FEATURE_NAMES, LocationModelSettings, StatusLogit, build_trip_grid,
                                      compute_location_forecast, location_features, quantiles)
from recording.replay import write_replay

STOPS = [f"S{i}" for i in range(1, 11)]                 # one line, one direction: place p is stop S{p + 1}
ROUTE_STOPS = {"routes": {"24": {"directions": {"0": {"stops": STOPS}}}},
               "stops": {stop: {"name": "Station Test" if stop == "S6" else f"Street / {stop}"} for stop in STOPS}}
SMALL = LocationModelSettings(min_points=200, min_buses=3, min_points_per_point=10, min_points_per_status=10)


def trip_replay(buses:dict[str, list], step:float=15.0, direction=0) -> dict:
    """buses: vehicle -> per slot (trip, stop sequence, occupancy status) or None (not in that frame). Line 24, the stop
    is S<sequence>; direction None leaves out vehicle.trip.direction_id."""
    slots = max(len(series) for series in buses.values())
    frames = []
    for slot in range(slots):
        data = []
        for vehicle, series in buses.items():
            if slot >= len(series) or series[slot] is None:
                continue
            trip, seq, status = series[slot]
            fields = {"vehicle.trip.trip_id": trip, "vehicle.current_stop_sequence": seq,
                      "vehicle.stop_id": f"S{seq}", "vehicle.occupancy_status": status}
            if direction is not None:
                fields["vehicle.trip.direction_id"] = direction
            data.append(entity(vehicle, "24", T0 + int(slot * step), **fields))
        frames.append({"t": slot * step, "data": data})
    return replay_dict(frames, step, "Test · location model", [24], len(buses))


def line_replay(buses:int=6, slots:int=200, slots_per_stop:int=5) -> dict:
    """Buses going round line 24 (10 stops, one every `slots_per_stop` slots, a new trip at stop 1), staggered: they
    fill up along the trip (many seats at stops 1-3, few at 4-6, standing at 7-10), so where they are tells a lot.
    A trip takes 12.5 min: within the 10 min horizon, a bus never comes back to where it was."""
    series = {}
    for b in range(buses):
        rows = []
        for slot in range(slots):
            trip, place = divmod((slot + 7 * b) // slots_per_stop, len(STOPS))
            seq = place + 1
            rows.append((f"t{b}-{trip}", seq, 1 if seq <= 3 else 2 if seq <= 6 else 3))
        series[f"b{b}"] = rows
    return trip_replay(series)


def scenario(y_change:int=6) -> dict:
    """a: from stop 1 at slot 0, one stop per minute (4 slots), many seats until slot 9, few seats after.
    y: the bus ahead of a, two stops ahead (stop 3 at slot 0), many seats until slot y_change - 1, few seats after.
    z: parked at the last stop (10) all along, standing. w: like a, but its frames have no direction."""
    def walk(trip, first_seq, change_slot, slots=40):
        return [(trip, min(first_seq + slot // 4, 10), 1 if slot < change_slot else 2) for slot in range(slots)]
    return {"a": walk("ta", 1, 10), "y": [(t, min(1 + (s + 8) // 4, 10), st) for s, (t, _, st) in
                                         enumerate(walk("ty", 1, y_change, 28))],
            "z": [("tz", 10, 3)] * 40}


def two_trips() -> dict:
    """a: trip t1 from stop 1 at slot 0 (one stop per 4 slots, at stop 10 from slot 36), then trip t2 from stop 1 at
    slot 41, one stop per 2 slots. z: parked at stop 10. Many seats until slot 19, few seats after."""
    a = [("t1", min(1 + slot // 4, 10), 1 if slot < 20 else 2) for slot in range(41)]
    a += [("t2", 1 + (slot - 41) // 2, 2) for slot in range(41, 60)]
    return {"a": a, "z": [("tz", 10, 3)] * 60}


def with_status(replay:dict, status:int) -> dict:
    """The replay with every bus at this occupancy status."""
    for frame in replay["frames"]:
        for bus in frame["data"]:
            bus["vehicle.occupancy_status"] = status
    return replay


def features_at(plan, features, vehicle:str, slot:int) -> dict:
    """{feature name: (points,) values} of the forecast of `vehicle` at `slot`."""
    index = 0
    for origin, vehicles in plan.origins:
        for v in vehicles:
            if origin == slot and v == vehicle:
                return {name: features.x[index, :, i] for i, name in enumerate(FEATURE_NAMES)}
            index += 1
    raise AssertionError(f"no forecast of {vehicle} at slot {slot}")


class TripGridTest(unittest.TestCase):

    def test_places_groups_and_the_later_frame_on_a_shared_slot(self):
        replay = trip_replay({"a": [("ta", 1, 1), ("ta", 2, 1), ("ta", 3, 1)], "b": [("tb", 5, 2), ("tb", 6, 2)]})
        replay["frames"].insert(2, {"t": 20.0, "data": [entity("b", "24", T0 + 20, **{
            "vehicle.trip.trip_id": "tb", "vehicle.current_stop_sequence": 7, "vehicle.trip.direction_id": 0,
            "vehicle.stop_id": "S7", "vehicle.occupancy_status": 2})]})        # slot 1 too (20 / 15 rounds to 1)
        grid = build_grid(replay)
        tg = build_trip_grid(replay, grid, ROUTE_STOPS)
        self.assertEqual(tg.vehicles, ["a", "b"])
        a, b = 0, 1
        self.assertEqual(tg.seq[a].tolist()[0], 1.0)
        # Slot 1: the frame at t=20 replaced the one at t=15, which had both buses
        self.assertTrue(math.isnan(tg.seq[a, 1]) and tg.group[a, 1] == -1 and tg.stop[a, 1] == -1 and tg.trip[a, 1] == -1)
        self.assertEqual((tg.seq[b, 1], tg.stop[b, 1]), (7.0, 6))
        self.assertEqual(tg.stop[a, 2], 2)
        self.assertEqual(tg.group[a, 0], tg.group[b, 0])
        self.assertTrue(tg.stations[0][5] and tg.stations[0].sum() == 1)

    def test_unknown_lines_directions_and_stops(self):
        replay = trip_replay({"a": [("ta", 1, 1)] * 2}, direction=None)
        frames = replay["frames"]
        frames[1]["data"].append(entity("b", "99", T0 + 15, **{"vehicle.trip.direction_id": 1, "vehicle.stop_id": "S1",
                                                              "vehicle.occupancy_status": 1}))
        frames[1]["data"].append(entity("c", "24", T0 + 15, **{"vehicle.trip.direction_id": "north",
                                                              "vehicle.occupancy_status": 1}))
        frames[1]["data"].append(entity("d", "24", T0 + 15, **{"vehicle.trip.direction_id": 0, "vehicle.stop_id": "X",
                                                              "vehicle.current_stop_sequence": True,
                                                              "vehicle.occupancy_status": 1}))
        grid = build_grid(replay)
        tg = build_trip_grid(replay, grid, ROUTE_STOPS)
        row = {vehicle: i for i, vehicle in enumerate(tg.vehicles)}
        self.assertEqual(tg.group[row["a"], 1], -1)                    # no direction
        self.assertGreaterEqual(tg.group[row["b"], 1], 0)              # a line without a stop list: a group...
        self.assertEqual(tg.stop[row["b"], 1], -1)                     # ... but no place on it
        self.assertEqual(tg.group[row["c"], 1], -1)                    # a direction that is not a number
        self.assertEqual(tg.stop[row["d"], 1], -1)                     # a stop not on the line's list
        self.assertTrue(math.isnan(tg.seq[row["d"], 1]))               # a boolean is not a stop sequence
        self.assertIsNone(build_trip_grid(replay, grid, None).stations[0])


class FeatureTest(unittest.TestCase):
    """Hand-computed values on scenario() (15 s slots, a forecast point every 30 s = 2 slots, 20 points)."""

    @classmethod
    def setUpClass(cls):
        cls.replay = trip_replay(scenario())
        cls.plan = plan_forecast(cls.replay)
        cls.features = location_features(cls.plan, build_trip_grid(cls.replay, cls.plan.grid, ROUTE_STOPS))

    def at(self, vehicle, slot):
        return features_at(self.plan, self.features, vehicle, slot)

    def test_horizon(self):
        x = self.at("a", 8)
        np.testing.assert_allclose(x["log_horizon"], np.log(np.arange(1, 21)))
        np.testing.assert_allclose(x["horizon"], np.arange(1, 21) / 20)

    def test_status_history(self):
        x = self.at("a", 8)            # never changed, seen for 8 slots = 2 min
        self.assertTrue((x["never_changed"] == 1).all() and (x["time_since_change"] == 0).all())
        np.testing.assert_allclose(x["never_changed_x_history"], 0.2)
        x = self.at("a", 12)           # changed at slot 10: 2 slots = 0.5 min ago
        self.assertTrue((x["never_changed"] == 0).all() and (x["never_changed_x_history"] == 0).all())
        np.testing.assert_allclose(x["time_since_change"], 0.05)

    def test_trip_position_from_the_last_stop_seen_before(self):
        # z sits at stop 10 from slot 0, so the line's last stop is known to be 10 from slot 1 on
        x = self.at("a", 8)            # stop 3, one stop per 4 slots: at k points ahead, 3 + 0.5 k
        self.assertTrue((x["trip_start"] == 1).all() and (x["trip_end"] == 0).all())
        np.testing.assert_allclose(x["trip_progress"], 3 / 10 - 0.5)
        np.testing.assert_array_equal(x["trip_boundary"], (3 + 0.5 * np.arange(1, 21) >= 10).astype(float))
        x = self.at("a", 28)           # stop 8: within 3 stops of the end
        self.assertTrue((x["trip_start"] == 0).all() and (x["trip_end"] == 1).all())
        self.assertTrue((x["position_unknown"] == 0).all())

    def test_the_last_stop_is_only_what_was_seen_before_the_forecast(self):
        # q is at stop 5 until slot 11 and first at stop 10 at slot 12: at a's forecast at slot 12 (stop 4), the last
        # stop known is 5, not 10
        replay = trip_replay({"a": scenario()["a"][:20], "q": [("tq", 5 if slot < 12 else 10, 2) for slot in range(20)]})
        plan = plan_forecast(replay)
        features = location_features(plan, build_trip_grid(replay, plan.grid, ROUTE_STOPS))
        x = features_at(plan, features, "a", 12)
        self.assertTrue((x["trip_end"] == 1).all())
        np.testing.assert_allclose(x["trip_progress"], 4 / 5 - 0.5)

    def test_station_ahead(self):
        x = self.at("a", 8)            # place 2, station at place 5: reached once 2 + 0.5 k >= 5
        np.testing.assert_array_equal(x["station_ahead"], (np.floor(2 + 0.5 * np.arange(1, 21)) >= 5).astype(float))
        self.assertTrue((self.at("a", 20)["station_ahead"] == 1).all())     # at the station itself (stop 6)
        self.assertTrue((self.at("a", 28)["station_ahead"] == 0).all())     # already past it

    def test_a_new_trip_seen_for_less_than_a_minute_has_no_progress_yet(self):
        replay = trip_replay(two_trips())
        plan = plan_forecast(replay)
        features = location_features(plan, build_trip_grid(replay, plan.grid, ROUTE_STOPS))
        # Slot 44: trip t2 seen since slot 41 (3 slots, under 4): the projection holds stop 2
        self.assertTrue((features_at(plan, features, "a", 44)["trip_boundary"] == 0).all())
        # Slot 48: stop 4, 3 stops in 7 slots: projected 4 + 3/7 * 2k, at the last stop (10) from k = 7
        x = features_at(plan, features, "a", 48)
        np.testing.assert_array_equal(x["trip_boundary"], (4 + 3 / 7 * 2 * np.arange(1, 21) >= 10).astype(float))
        # Its own trip t1 passed stop 4 earlier: that is not a bus ahead
        self.assertTrue((x["no_bus_ahead"] == 1).all())

    def test_the_bus_ahead_is_cut_at_the_slot_before_the_forecast(self):
        # y now gets few seats at slot 8, the forecast time itself: a's forecast at slot 8 only sees it until slot 7
        replay = trip_replay(scenario(y_change=8))
        plan = plan_forecast(replay)
        features = location_features(plan, build_trip_grid(replay, plan.grid, ROUTE_STOPS))
        x = features_at(plan, features, "a", 8)
        self.assertTrue((x["bus_ahead_change"] == 0).all() and (x["no_bus_ahead"] == 0).all())

    def test_bus_ahead(self):
        # y passed a's stop (place 2) at slot 4; its last status before was many seats (slot 3); it had few seats from
        # slot 6. Its change over k points from slot 3, cut at slot 7 (the slot before a's forecast at 8)
        x = self.at("a", 8)
        np.testing.assert_array_equal(x["bus_ahead_change"], [0.0] + [1.0] * 19)
        np.testing.assert_array_equal(x["bus_ahead_abs_change"], [0.0] + [1.0] * 19)
        self.assertTrue((x["no_bus_ahead"] == 0).all())
        # z never passes a stop (it is first seen at the last one); nobody passes z
        self.assertTrue((self.at("z", 8)["no_bus_ahead"] == 1).all() and (self.at("y", 8)["no_bus_ahead"] == 1).all())

    def test_no_direction_means_no_position(self):
        replay = trip_replay({"w": scenario()["a"], "z": scenario()["z"]}, direction=None)
        plan = plan_forecast(replay)
        features = location_features(plan, build_trip_grid(replay, plan.grid, ROUTE_STOPS))
        x = features_at(plan, features, "w", 8)
        self.assertTrue((x["position_unknown"] == 1).all() and (x["no_bus_ahead"] == 1).all())
        for name in ("trip_start", "trip_end", "trip_boundary", "trip_progress", "station_ahead"):
            self.assertTrue((x[name] == 0).all(), name)
        self.assertFalse(features.position.any() or features.placed.any())

    def test_without_stop_lists_the_trip_position_is_still_used(self):
        features = location_features(self.plan, build_trip_grid(self.replay, self.plan.grid, None))
        x = features_at(self.plan, features, "a", 28)
        self.assertTrue((x["trip_end"] == 1).all() and (x["no_bus_ahead"] == 1).all() and (x["station_ahead"] == 0).all())
        self.assertTrue((x["position_unknown"] == 1).all())
        self.assertTrue(features.position.all() and not features.placed.any())


class StatusLogitTest(unittest.TestCase):

    def setUp(self):
        rng = np.random.default_rng(0)
        self.n, self.classes = 60, 3
        self.rank = rng.integers(0, self.classes, self.n).astype(float)
        self.x = rng.normal(size=(self.n, len(FEATURE_NAMES)))
        self.y = rng.integers(0, self.classes, self.n)

    def model(self, weight=None):
        weight = np.ones(self.n) if weight is None else weight
        return StatusLogit(self.classes, self.rank, self.x, self.y, weight)

    def test_gradient_and_hessian_match_finite_differences(self):
        model = self.model(np.r_[np.ones(40), np.zeros(20)])
        reg = np.where(model.penalized, 0.01, 1e-6)
        theta = np.random.default_rng(1).normal(scale=0.3, size=model.size)
        ranges = [(0, 25), (25, 60)]
        value, grad, hess = model._evaluate(theta, ranges, reg, 40.0, True)
        eps = 1e-6
        for i in range(model.size):
            step = np.zeros(model.size)
            step[i] = eps
            plus, grad_plus, _ = model._evaluate(theta + step, ranges, reg, 40.0, True)
            minus, grad_minus, _ = model._evaluate(theta - step, ranges, reg, 40.0, True)
            self.assertAlmostEqual(grad[i], (plus - minus) / (2 * eps), places=6)
            np.testing.assert_allclose(hess[:, i], (grad_plus - grad_minus) / (2 * eps), atol=1e-5)

    def test_rows_with_zero_weight_are_ignored(self):
        weight = np.r_[np.ones(40), np.zeros(20)]
        theta = self.model(weight).fit([(0, 60)], 0.01)
        only = StatusLogit(self.classes, self.rank[:40], self.x[:40], self.y[:40], np.ones(40)).fit([(0, 40)], 0.01)
        np.testing.assert_allclose(theta, only, atol=1e-7)

    def test_blocks_and_the_design_built_on_the_fly_give_the_same_fit(self):
        theta = self.model().fit([(0, 60)], 0.01)
        with mock.patch.object(location_model, "CHUNK", 7), mock.patch.object(StatusLogit, "DESIGN_BUDGET", 0):
            model = self.model()
            self.assertIsNone(model._design)
            np.testing.assert_allclose(model.fit([(0, 30), (30, 60)], 0.01), theta, atol=1e-8)
            np.testing.assert_allclose(model.proba(np.arange(60), theta), self.model().proba(np.arange(60), theta))

    def test_it_learns_a_clear_effect(self):
        # Buses at rank 1 drop to rank 0 when trip_boundary is 1, else stay
        n = 400
        rank = np.ones(n)
        x = np.zeros((n, len(FEATURE_NAMES)))
        boundary = np.arange(n) % 4 == 0
        x[boundary, FEATURE_NAMES.index("trip_boundary")] = 1
        y = np.where(boundary, 0, 1)
        model = StatusLogit(3, rank, x, y, np.ones(n))
        prob = model.proba(np.array([0, 1]), model.fit([(0, n)], 1e-4))
        self.assertGreater(prob[0, 0], 0.9)
        self.assertGreater(prob[1, 1], 0.9)


class QuantileTest(unittest.TestCase):

    def test_a_certain_status(self):
        np.testing.assert_allclose(quantiles(np.array([[0.0, 1.0, 0.0, 0.0]]), 3), [[0.6, 1.0, 1.4]])
        np.testing.assert_allclose(quantiles(np.array([[1.0, 0.0, 0.0]]), 2), [[0.0, 0.0, 0.4]])   # clipped at 0

    def test_the_median_rounds_half_up_to_the_median_status(self):
        rng = np.random.default_rng(2)
        prob = rng.dirichlet(np.full(4, 0.3), size=5000)
        prob[:100] = np.eye(4)[rng.integers(0, 4, 100)] * 0.5 + 0.125      # many ties at a CDF of exactly 0.5
        values = quantiles(prob, 3)
        median_status = (np.cumsum(prob, axis=1) < 0.5).sum(axis=1)
        np.testing.assert_array_equal(values[:, 1], median_status)
        self.assertTrue((np.diff(values, axis=1) >= 0).all() and (values >= 0).all() and (values <= 3).all())
        np.testing.assert_array_equal(values, np.round(values, 2))


class WalkForwardTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.replay = line_replay()
        cls.result = compute_location_forecast(plan_forecast(cls.replay), cls.replay, ROUTE_STOPS, SMALL)

    def test_it_learns_where_buses_fill_up(self):
        metrics, training = self.result.metrics, self.result.forecast["training"]
        self.assertGreater(metrics["accuracy"], metrics["accuracy_last_value"] + 0.2)
        self.assertGreater(metrics["accuracy_at_horizon"], metrics["accuracy_last_value_at_horizon"] + 0.04)
        self.assertLess(metrics["mae"], metrics["mae_last_value"])
        self.assertGreater(training["model_share"], 0.5)
        self.assertEqual((training["position_known"], training["stop_known"]), (1.0, 1.0))
        final = training["final"]
        self.assertEqual(set(final["coefficients"]["beta"]), set(location_model.CHANGE_FEATURES))
        # It fills up along a trip and empties at a new one: later in the trip, changes go up
        self.assertGreater(final["coefficients"]["delta"]["trip_progress"], 0)

    def test_stored_forecasts_are_valid_and_in_time_order(self):
        forecast = self.result.forecast
        self.assertEqual(forecast["model_name"], "Location model")
        self.assertEqual(forecast["levels"], [1, 2, 3])
        for entries in forecast["vehicles"].values():
            self.assertEqual([e["t"] for e in entries], sorted(e["t"] for e in entries))
            for entry in entries:
                q = np.array(entry["q"])
                self.assertEqual(q.shape, (len(QUANTILES), 20))
                self.assertTrue((np.diff(q, axis=0) >= 0).all() and (q >= 0).all() and (q <= 2).all())
                np.testing.assert_array_equal(q, np.round(q, 2))
        json.dumps(forecast)        # plain JSON types only

    def test_it_learns_from_the_outcomes_known_at_each_forecast_time(self):
        # Counted independently: points of earlier forecasts whose target slot is at or before the forecast time
        plan, grid = plan_forecast(self.replay), build_grid(self.replay)
        forecasts = [(slot, vehicle) for slot, vehicles in plan.origins for vehicle in vehicles]
        expected = []
        for slot, _ in plan.origins:
            expected.append(sum(1 for origin, vehicle in forecasts for k in range(1, 21)
                                if origin + 2 * k <= slot and math.isfinite(grid.levels[vehicle][origin + 2 * k])))
        self.assertEqual(self.result.forecast["training"]["history"]["examples"], expected)
        self.assertEqual(self.result.forecast["training"]["history"]["t"], [float(grid.times[s]) for s, _ in plan.origins])

    def test_metrics_are_those_of_the_stored_forecasts(self):
        forecast, grid = self.result.forecast, build_grid(self.replay)
        rows = []
        for vehicle, entries in forecast["vehicles"].items():
            for entry in entries:
                slot = int(round(entry["t"] / grid.step))
                for k in range(20):
                    target = slot + 2 * (k + 1)
                    if target < grid.slots and math.isfinite(grid.levels[vehicle][target]):
                        rows.append((grid.levels[vehicle][target], entry["q"][1][k], grid.levels[vehicle][slot]))
        observed, median, last = map(np.array, zip(*rows))
        self.assertEqual(forecast["metrics"]["evaluated_points"], len(rows))
        self.assertAlmostEqual(forecast["metrics"]["accuracy"], round(float((np.floor(median + 0.5) == observed).mean()), 3))
        self.assertAlmostEqual(forecast["metrics"]["accuracy_last_value"], round(float((last == observed).mean()), 3))

    def test_a_forecast_never_uses_what_happens_after_it(self):
        cut = 100       # slot
        changed = copy.deepcopy(self.replay)
        rng = random.Random(0)
        for frame in changed["frames"][cut + 1:]:
            for bus in frame["data"]:
                bus["vehicle.occupancy_status"] = rng.choice([1, 2, 3])
                bus["vehicle.current_stop_sequence"] = rng.randint(1, 10)
                bus["vehicle.stop_id"] = f"S{rng.randint(1, 10)}"
                bus["vehicle.trip.trip_id"] = rng.choice(["x", "y"])
            frame["data"] = frame["data"][:rng.randint(2, len(frame["data"]))]
        other = compute_location_forecast(plan_forecast(changed), changed, ROUTE_STOPS, SMALL).forecast
        mine, compared = self.result.forecast, 0
        for vehicle, entries in mine["vehicles"].items():
            for entry in entries:
                if entry["t"] <= cut * 15:
                    self.assertIn(entry, other["vehicles"][vehicle])
                    compared += 1
        self.assertGreater(compared, 50)
        self.assertNotEqual(mine["vehicles"], other["vehicles"])
        self.assertEqual(mine["training"]["first_t"], other["training"]["first_t"])     # it starts before the cut

    def test_too_few_outcomes_means_no_change(self):
        result = compute_location_forecast(plan_forecast(self.replay), self.replay, ROUTE_STOPS, LocationModelSettings())
        training = result.forecast["training"]
        self.assertIsNone(training["final"])
        self.assertEqual(training["first_t"], [None] * 20)
        self.assertEqual(training["model_share"], 0.0)
        self.assertEqual(result.metrics["accuracy"], result.metrics["accuracy_last_value"])
        for entries in result.forecast["vehicles"].values():
            for entry in entries:
                self.assertTrue(all(len(set(column)) == 1 for column in zip(*entry["q"])))     # low = median = high

    def test_a_rare_status_stays_no_change(self):
        replay = line_replay()
        for frame in replay["frames"][150:]:        # bus b0 is full near the end: fewer than 300 outcomes from that status
            for bus in frame["data"]:
                if bus["id"] == "b0":
                    bus["vehicle.occupancy_status"] = 5
        settings = dataclasses.replace(SMALL, min_points_per_status=300)
        forecast = compute_location_forecast(plan_forecast(replay), replay, ROUTE_STOPS, settings).forecast
        full = float(len(forecast["levels"]) - 1)
        late = [entry for entry in forecast["vehicles"]["b0"] if entry["t"] >= 152 * 15]
        self.assertTrue(late)
        for entry in late:
            self.assertEqual(entry["q"], [[full] * 20] * 3)
        # The other buses, at their own statuses, are forecast by the model at the same times
        others = [entry for vehicle, entries in forecast["vehicles"].items() if vehicle != "b0" for entry in entries
                  if entry["t"] >= 152 * 15]
        self.assertTrue(any(any(len(set(column)) > 1 for column in zip(*entry["q"])) for entry in others))

    def test_a_single_status_is_never_modelled(self):
        replay = with_status(line_replay(), 2)
        result = compute_location_forecast(plan_forecast(replay), replay, ROUTE_STOPS, SMALL)
        self.assertIsNone(result.forecast["training"]["final"])
        self.assertEqual(result.metrics["accuracy"], 1.0)


class LocationCommandTest(TempDirTestCase):

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "line.json"
        write_replay(line_replay(), self.path)
        self.stops = self.tmp / "route_stops.json"
        self.stops.write_text(json.dumps(ROUTE_STOPS), encoding="utf-8")

    def test_location_model_without_timestral(self):
        with mock.patch("recording.cli.ROUTE_STOPS_FILE", self.stops), \
                mock.patch("recording.location_model.LocationModelSettings", return_value=SMALL), \
                mock.patch("recording.forecast.load_forecaster", side_effect=AssertionError("Timestral loaded")):
            code, out, err = run_cli("forecast", str(self.path), "--model", "Location")
        self.assertEqual(code, 0, err)
        forecast = json.loads(self.path.read_text(encoding="utf-8"))["forecast"]
        self.assertEqual(forecast["model_name"], "Location model")
        self.assertIn("training", forecast)
        self.assertNotIn("calibration", forecast)
        self.assertRegex(out, r"learned:  the model made \d+% of the stored points")
        self.assertRegex(out, r"position: trip position known for 100% of the forecasts")
        self.assertRegex(out, r"stop on the line for 100%")
        self.assertRegex(out, r"right +\d+\.\d% +\d+\.\d%")

    def test_nothing_learned_and_no_stop_lists(self):
        with mock.patch("recording.cli.ROUTE_STOPS_FILE", self.tmp / "missing.json"):
            code, out, err = run_cli("forecast", str(self.path), "--model", "location")
        self.assertEqual(code, 0, err)
        self.assertIn("no stop lists", out)
        self.assertIn('learned:  nothing - every stored point is "no change"', out)
        self.assertIn("trip position known for 100%", out)          # the trip position needs no stop list
        self.assertIn("stop on the line for 0%", out)
        self.assertNotIn("cannot use where they are", out)

    def test_a_single_status(self):
        write_replay(with_status(line_replay(), 2), self.path)
        with mock.patch("recording.cli.ROUTE_STOPS_FILE", self.stops):
            code, out, err = run_cli("forecast", str(self.path), "--model", "location")
        self.assertEqual(code, 0, err)
        self.assertIn("a single occupancy status: there is no change to learn", out)

    def test_no_calibration_is_for_timestral(self):
        code, out, err = run_cli("forecast", str(self.tmp / "missing.json"), "--model", "location", "--no-calibration")
        self.assertEqual(code, 2)
        self.assertIn("--no-calibration is for Timestral", err)

    def test_a_replay_without_directions_says_so(self):
        replay = line_replay()
        for frame in replay["frames"]:
            for bus in frame["data"]:
                del bus["vehicle.trip.direction_id"]
        write_replay(replay, self.path)
        with mock.patch("recording.cli.ROUTE_STOPS_FILE", self.stops):
            code, out, err = run_cli("forecast", str(self.path), "--model", "location")
        self.assertEqual(code, 0, err)
        self.assertIn("position: trip position known for 0% of the forecasts", out)
        self.assertIn("cannot use where they are on their trip", out)


if __name__ == "__main__":
    unittest.main()
