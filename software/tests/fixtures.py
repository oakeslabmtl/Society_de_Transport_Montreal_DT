import json
import sys
import tempfile
import unittest
from pathlib import Path

# Tests import the project the way the services do: absolute imports from software/
SOFTWARE_DIR = Path(__file__).resolve().parents[1]
if str(SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(SOFTWARE_DIR))

from recording.store import RecordingStore

T0 = 1740859200  # Sat 2025-03-01 15:00:00 America/Montreal (UTC-5)


def entity(vehicle_id:str, route_id:str, timestamp:int, latitude:float=45.5, longitude:float=-73.6, **fields) -> dict:
    """A flattened entity with the keys and types of a live message."""
    data = {
        "id": vehicle_id,
        "vehicle.trip.trip_id": "281567850",
        "vehicle.trip.start_time": "14:30:00",
        "vehicle.trip.start_date": "20250301",
        "vehicle.trip.route_id": route_id,
        "vehicle.position.latitude": latitude,
        "vehicle.position.longitude": longitude,
        "vehicle.position.bearing": 39.0,
        "vehicle.position.speed": 7.500060081481934,
        "vehicle.current_stop_sequence": 12,
        "vehicle.current_status": 2,
        "vehicle.timestamp": timestamp,
        "vehicle.vehicle.id": vehicle_id,
        "vehicle.occupancy_status": 1,
    }
    data.update(fields)
    return data


def typed(value) -> str:
    """JSON with sorted keys: equal strings mean equal keys, values AND types (1 != 1.0)."""
    return json.dumps(value, sort_keys=True)


def make_recording(path:Path, polls:list, interval:float=15.0, route_ids=("45", "121")) -> Path:
    """polls: (poll_time, [entities]) for ok polls, (poll_time, 'error message') for failed ones."""
    store = RecordingStore.create(path, {"source": "csv-import", "source_detail": "tests", "route_ids": list(route_ids),
                                         "poll_interval_s": interval, "name": Path(path).stem})
    with store:
        for poll_time, data in polls:
            if isinstance(data, str):
                store.add_error_poll(poll_time, data)
            else:
                store.add_poll(poll_time, data)
    return Path(path)


class TempDirTestCase(unittest.TestCase):

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()
