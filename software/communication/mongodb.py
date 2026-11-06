from pymongo.errors import BulkWriteError, ConnectionFailure
from pymongo import MongoClient
import logging
import re


class MongoDB:

    def __init__(self, database_name:str, collection_name:str, vhost:str=None, port:int=None, username:str=None, password:str=None, connection_str=None):
        self.logger = logging.getLogger("MongoDb")
        assert database_name, "Database name is a required field."
        assert collection_name, "Collection name is a required field."
        self.client = None
        self.database = None
        self.collection = None
        self.db_name = database_name
        self.col_name = collection_name

        if not connection_str:
            assert vhost, "Vhost is a required field when a connection string is not provided."
            assert port, "Port number is a required field when a connection string is not provided."
            assert username, "Username is a required field when a connection string is not provided."
            assert password, "Password is a required field when a connection string is not provided."
            self.connection_str = f"mongodb://{username}:{password}@{vhost}:{port}"
        else:
            self.connection_str = connection_str
        self.setup()


    def setup(self):
        try:
            self.client = MongoClient(self.connection_str)
            self.client.admin.command("ping")   # Fails here, not at the first query, when MongoDB cannot be reached
            # MongoDB creates the database and the collection at the first write
            self.database = self.client[self.db_name]
            self.collection = self.database[self.col_name]

        except ConnectionFailure as e:
            # Without the user and password of the connection string (pymongo's own message names only the hosts)
            address = re.sub(r"//[^@/]*@", "//", self.connection_str)
            error_msg = f"Unable to connect to {address}/{self.db_name}/{self.col_name}, error: {e}"
            self.logger.error(error_msg)
            raise ConnectionFailure(error_msg) from e


    def save(self, data: list[dict]):
        if not data:
            self.logger.debug("Skipped writing to db, empty list provided.")
            return

        try:
            self.logger.debug("Starting writing to db ... ")

            results = self.collection.insert_many(data)

            self.logger.debug("Ending writing to db ... ")
            return results
        except BulkWriteError as e:
            self.logger.error(f"Error: {str(e)}")


    def find(self, _filter: dict):
        self.logger.debug(f"Fetching from the database: {_filter}")
        return self.collection.find(_filter)    # A cursor: the query runs as it is read