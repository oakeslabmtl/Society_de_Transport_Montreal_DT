import contextlib
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock
import fixtures  # noqa: F401  (puts software/ on sys.path)
import pandas as pd
import requests
from google.transit.gtfs_realtime_pb2 import FeedMessage
from protobuf_to_dict import protobuf_to_dict
from communication import stm_api


# --- Reference: DataIngestionService.process_response / flatten_response as they were at commit 80048df ---------
# (copied from `git show HEAD:software/startup/data_ingestion_service.py`, `self` and logging removed)

def reference_extract_timestamp(_data:dict) -> int:
    try:
        return int(_data['vehicle']['timestamp'])
    except KeyError as e:
        return 0


def reference_flatten_response(data:list[dict]) -> list[dict]:
    flatten_data = []
    for entity in data:
        flatten_data.append(pd.json_normalize(entity).T.to_dict()[0])
    return flatten_data


def reference_process_response(feed, route_ids:list[int]=None, sort:bool=True, flatten:bool=True) -> list[dict]:
    data = []
    # Convert to dictionary from the original protobuf feed
    feed_dict = protobuf_to_dict(feed)

    if not route_ids:
        route_ids = []

    for entity in feed_dict['entity']:
        if 'vehicle' in entity and 'trip' in entity['vehicle'] and 'route_id' in entity['vehicle']['trip']:
            route_id = int(entity['vehicle']['trip']['route_id'])
            if (not route_ids) or (route_id in route_ids) :
                data.append(entity)
    if sort:
        data.sort(key=reference_extract_timestamp, reverse=True)

    if flatten:
        data = reference_flatten_response(data)
    return data

# ------------------------------------------------------------------------------------------------------------------


def synthetic_feed() -> FeedMessage:
    feed = FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1740859380

    def vehicle(entity_id, route_id, timestamp=None, bearing=None):
        entity = feed.entity.add()
        entity.id = entity_id
        v = entity.vehicle
        if route_id is not None:
            v.trip.trip_id = f"28156{entity_id}"
            v.trip.start_time = "14:30:00"
            v.trip.start_date = "20250301"
            v.trip.route_id = route_id
        v.position.latitude = 45.568321228027344
        v.position.longitude = -73.65330505371094
        v.position.speed = 0
        if bearing is not None:
            v.position.bearing = bearing
        v.current_stop_sequence = 32
        v.current_status = 1
        if timestamp is not None:
            v.timestamp = timestamp
        v.vehicle.id = entity_id
        v.occupancy_status = 2
        return entity

    vehicle("41053", "121", 1740859300, bearing=39)
    vehicle("31841", "45", 1740859370).vehicle.congestion_level = 3        # newest, no bearing
    third = vehicle("42027", "121", 1740859340, bearing=222.5)
    third.vehicle.trip.direction_id = 1                                     # keys outside the usual set survive
    third.vehicle.vehicle.label = "42027"
    third.vehicle.stop_id = "52345"
    vehicle("39001", "140", 1740859360, bearing=10)                         # route not subscribed below
    vehicle("39002", None, 1740859365)                                      # no trip -> ignored
    vehicle("39003", "45")                                                  # no timestamp -> sorted last
    alert = feed.entity.add()
    alert.id = "alert-1"
    alert.alert.header_text.translation.add().text = "Detour"               # not a vehicle -> ignored
    return feed


def as_json(value) -> str:
    """Exact comparison: key order, values and types (1 vs 1.0) all matter."""
    return json.dumps(value)


class StmApiGoldenTest(unittest.TestCase):

    CASES = [
        dict(route_ids=None), dict(route_ids=[]), dict(route_ids=[121, 45]), dict(route_ids=[140]),
        dict(route_ids=[121, 45], sort=False), dict(route_ids=None, flatten=False),
        dict(route_ids=[45], sort=False, flatten=False),
    ]

    def setUp(self):
        self.feed = synthetic_feed()
        logging.getLogger("StmApi").setLevel(logging.CRITICAL)  # the entity without timestamp logs an error
        self.enterContext(warnings.catch_warnings())
        warnings.filterwarnings("ignore", message=r"label\(\) is deprecated")  # inside protobuf_to_dict

    def tearDown(self):
        logging.getLogger("StmApi").setLevel(logging.NOTSET)

    def test_stm_api_matches_original_process_response(self):
        for case in self.CASES:
            with self.subTest(**case):
                sort, flatten = case.get("sort", True), case.get("flatten", True)
                expected = reference_process_response(self.feed, case["route_ids"], sort, flatten)
                actual = stm_api.filter_vehicle_entities(self.feed, case["route_ids"], sort)
                if flatten:
                    actual = stm_api.flatten_entities(actual)
                self.assertEqual(as_json(actual), as_json(expected))

    def test_synthetic_feed_exercises_the_interesting_cases(self):
        flat = reference_process_response(self.feed, [121, 45])
        self.assertEqual([e["vehicle.vehicle.id"] for e in flat], ["31841", "42027", "41053", "39003"])
        self.assertNotIn("vehicle.position.bearing", flat[0])
        self.assertEqual(flat[1]["vehicle.trip.direction_id"], 1)
        self.assertIsInstance(flat[0]["vehicle.position.latitude"], float)
        self.assertIsInstance(flat[0]["vehicle.timestamp"], int)

    def test_data_ingestion_service_delegates_with_identical_output(self):
        os.environ.setdefault("STM_API_KEY", "dummy-key-for-tests")
        try:
            with mock.patch("config.config.config_logger"):   # no log file / stdout handler from logging.conf
                from startup.data_ingestion_service import DataIngestionService, flatten_response, to_queue_format
        except ImportError as error:
            self.skipTest(f"service dependencies not installed: {error}")

        service = object.__new__(DataIngestionService)       # no RabbitMQ / MongoDB connection
        service.logger = service._logger = logging.getLogger("DataIngestionServiceTest")
        service.logger.setLevel(logging.CRITICAL)
        service.channel = None                                # what RabbitMQ.__del__ looks at
        for case in self.CASES:
            with self.subTest(**case):
                sort, flatten = case.get("sort", True), case.get("flatten", True)
                expected = reference_process_response(self.feed, case["route_ids"], sort, flatten)
                actual = service.process_response(self.feed, case["route_ids"], sort=sort, flatten=flatten)
                self.assertEqual(as_json(actual), as_json(expected))
        nested = reference_process_response(self.feed, None, flatten=False)
        self.assertEqual(as_json(flatten_response(nested)), as_json(reference_flatten_response(nested)))
        self.assertEqual(to_queue_format([])["source"], "stm_api_bus_routes_update")

    def test_data_ingestion_service_logs_errors_without_the_api_key(self):
        os.environ.setdefault("STM_API_KEY", "dummy-key-for-tests")
        try:
            with mock.patch("config.config.config_logger"):
                from startup.data_ingestion_service import DataIngestionService
        except ImportError as error:
            self.skipTest(f"service dependencies not installed: {error}")

        raw = "dummy-key\r\nfor-tests"          # a line break inside: the strip in protocol.py cannot fix it; never the real key
        with self.assertRaises(requests.exceptions.InvalidHeader) as caught:
            requests.Request("GET", "https://example.test/feed", headers={"apiKey": raw}).prepare()
        service = object.__new__(DataIngestionService)       # no RabbitMQ / MongoDB connection
        service.logger = service._logger = logging.getLogger("DataIngestionServiceLogTest")
        service.channel = None                                # what RabbitMQ.__del__ looks at
        with mock.patch.dict(os.environ, {"STM_API_KEY": raw}), \
                mock.patch.object(service, "fetch_and_update_route", side_effect=caught.exception), \
                self.assertLogs("DataIngestionServiceLogTest", "ERROR") as logs, \
                contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            service.ingest()
        self.assertIn("header value: '<STM_API_KEY>'", logs.output[0])
        for text in (logs.output[0], stderr.getvalue()):     # the log line, and the traceback printed after it
            self.assertNotIn("dummy-key", text)
            self.assertNotIn("for-tests", text)

    def test_live_service_keeps_the_stm_api_error_logs(self):
        # logging.conf (fileConfig) disables the loggers that exist when it is loaded, like "StmApi" (created on import)
        missing = [name for name in ("pika", "pymongo", "pyhocon") if importlib.util.find_spec(name) is None]
        if missing:
            self.skipTest(f"service dependencies not installed: {', '.join(missing)}")
        script = ("import logging, startup.data_ingestion_service\n"
                  "from communication import stm_api\n"
                  "assert not logging.getLogger('StmApi').disabled\n"
                  "stm_api.extract_timestamp({'vehicle': {}})\n")
        # PYTHONPATH as the services are run: config_logger looks for config/logging.conf there
        env = dict(os.environ, PYTHONPATH=str(fixtures.SOFTWARE_DIR), STM_API_KEY="dummy-key-for-tests",
                   PYTHONDONTWRITEBYTECODE="1")
        with tempfile.TemporaryDirectory() as cwd:          # DigitalTwin.log is written in the current folder
            run = subprocess.run([sys.executable, "-c", script], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            log = (Path(cwd) / "DigitalTwin.log").read_text(encoding="utf-8")
        self.assertIn("ERROR StmApi : Error at extracting timestamp: 'timestamp'", log)
        self.assertIn("ERROR StmApi : Error at extracting timestamp", run.stdout)

    def test_empty_feed_gives_no_vehicles(self):
        # The original code raised KeyError('entity') here (protobuf_to_dict omits empty lists)
        self.assertEqual(stm_api.filter_vehicle_entities(FeedMessage(), [121]), [])

    def test_fetch_feed_parses_the_response_and_can_check_the_status(self):
        response = mock.Mock(content=self.feed.SerializeToString())
        with mock.patch("communication.stm_api.requests.get", return_value=response) as get:
            feed = stm_api.fetch_feed("https://example.test/feed", {"apiKey": "x"})
        get.assert_called_once_with("https://example.test/feed", headers={"apiKey": "x"}, timeout=None)
        response.raise_for_status.assert_not_called()
        self.assertEqual(feed, self.feed)

        with mock.patch("communication.stm_api.requests.get", return_value=response):
            stm_api.fetch_feed("https://example.test/feed", {}, timeout=5, check_status=True)
        response.raise_for_status.assert_called_once()


if __name__ == '__main__':
    unittest.main()
