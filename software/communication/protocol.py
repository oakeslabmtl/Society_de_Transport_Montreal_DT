import json
import os

STM_API_URL = "https://api.stm.info/pub/od/gtfs-rt/ic/v2/vehiclePositions"
STM_API_HEADER = {'apiKey': os.environ["STM_API_KEY"].strip(), 'Accept': 'application/x-protobuf'}
ENCODING = "ascii"

ROUTING_KEY_STM_BUS_ROUTE_UPDATES = "stm.bus.routes.updates"
ROUTING_KEY_STM_TELEMETRY_VALIDATION = "stm.bus.routes.telemetry.validation.service"
ROUTING_KEY_STM_TELEMETRY_VALIDATION_VIEW = "stm.bus.routes.telemetry.validation.service.view"
ROUTING_KEY_STM_NOTIFICATION = "stm.notification.service"

EARTH_RADIUS = 6371 # Radius of the Earth in Kilometers
MAX_ALLOW_DISTANCE = 0.5 # Maximum allowed distance in Kilometers
EXECUTION_INTERVAL = 15.0


def encode_json(object_):
    return json.dumps(object_).encode(ENCODING)


def decode_json(bytes_):
    return json.loads(bytes_.decode(ENCODING))
