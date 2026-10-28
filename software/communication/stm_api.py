import logging
import requests
import pandas as pd
from google.transit.gtfs_realtime_pb2 import FeedMessage
from protobuf_to_dict import protobuf_to_dict

# Shared by the live DataIngestionService and the recorder (software/recording) so that recorded
# entities have exactly the same keys and types as the live RabbitMQ messages.
# Does not import communication.protocol: that module needs STM_API_KEY at import time.

logger = logging.getLogger("StmApi")


def fetch_feed(url:str, headers:dict, timeout:float=None, check_status:bool=False) -> FeedMessage:
    feed = FeedMessage()
    response = requests.get(url, headers=headers, timeout=timeout)
    if check_status:
        response.raise_for_status()
    feed.ParseFromString(response.content)
    return feed


def extract_timestamp(entity:dict) -> int:
    try:
        return int(entity['vehicle']['timestamp'])
    except KeyError as e:
        logger.error(f"Error at extracting timestamp: {str(e)}")
        return 0


def filter_vehicle_entities(feed:FeedMessage, route_ids:list[int]=None, sort:bool=True) -> list[dict]:
    """Vehicle entities of the feed on the given routes (all routes if empty), newest first."""
    data = []
    # Convert to dictionary from the original protobuf feed (an empty feed has no 'entity' key)
    feed_dict = protobuf_to_dict(feed)

    for entity in feed_dict.get('entity', []):
        if 'vehicle' in entity and 'trip' in entity['vehicle'] and 'route_id' in entity['vehicle']['trip']:
            logger.debug(f"Retrieved entity: {entity}")

            route_id = int(entity['vehicle']['trip']['route_id'])
            if (not route_ids) or (route_id in route_ids):
                data.append(entity)
    if sort:
        data.sort(key=extract_timestamp, reverse=True)
    return data


def flatten_entities(data:list[dict]) -> list[dict]:
    """Nested entities -> flat dicts with dotted keys, e.g. 'vehicle.position.latitude'."""
    flatten_data = []
    for entity in data:
        flatten_data.append(pd.json_normalize(entity).T.to_dict()[0])
    return flatten_data
