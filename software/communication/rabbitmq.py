
from pika import PlainCredentials, ConnectionParameters, SSLOptions, BlockingConnection
from communication.protocol import encode_json, decode_json
import logging
import ssl as ssl_

class RabbitMQ:

    def __init__(self, ip, port, username, password, vhost, exchange_name, exchange_type, ssl = None):
        self._logger = logging.getLogger("RabbitMQClass")
        self.ip = ip
        self.port = port
        self.vhost = vhost
        self.exchange_name = exchange_name
        self.exchange_type = exchange_type
        self.credentials = PlainCredentials(username, password)

        if ssl is None:
            self.parameters = ConnectionParameters(self.ip, self.port, self.vhost, self.credentials)
        else:
            ssl_context = ssl_.SSLContext(getattr(ssl_, ssl["protocol"]))
            ssl_context.set_ciphers(ssl["ciphers"])
            self.parameters = ConnectionParameters(self.ip, self.port, self.vhost, self.credentials, ssl_options=SSLOptions(context=ssl_context))
        self.connection = None
        self.channel = None
        self.queue_name = []


    def __del__(self):
        self._logger.debug("Deleting queues, close channel and connection")
        if self.channel is not None:
            if not self.channel.is_closed and not self.connection.is_closed:
                self.close()
        self._logger.debug("Connection closed.")


    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        self._logger.debug("Connection closed.")


    def __enter__(self):
        self.connect_to_server()
        return self


    def connect_to_server(self):
        self.connection = BlockingConnection(self.parameters)
        self._logger.debug("Connected.")
        self.channel = self.connection.channel()
        self.channel.exchange_declare(exchange=self.exchange_name, exchange_type=self.exchange_type)


    def send_message(self, routing_key, message, properties=None):
        self.channel.basic_publish(
            exchange=self.exchange_name,
            routing_key=routing_key,
            body=encode_json(message),
            properties=properties
        )
        self._logger.debug(f"Message sent to {routing_key}.")
        self._logger.debug(message)


    def get_message(self, queue_name):
        (method, properties, body) = self.channel.basic_get(queue=queue_name, auto_ack=True)

        self._logger.debug(f"Received message is {body} {method} {properties}")
        if body is not None:
            return decode_json(body)
        else:
            return None

    def declare_local_queue(self, routing_key):
        # Creates a local queue.
        # Rabbitmq server will clean it if the connection drops.
        result = self.channel.queue_declare(queue="", exclusive=True, auto_delete=True)
        created_queue_name = result.method.queue
        self.channel.queue_bind(
            exchange=self.exchange_name,
            queue=created_queue_name,
            routing_key=routing_key
        )
        self.queue_name.append(created_queue_name)
        self._logger.info(f"Bound {routing_key} --> {created_queue_name}")
        return created_queue_name


    def close(self):
        # The queues declare_local_queue makes are exclusive: the broker deletes them, and their bindings, with the
        # connection. Closing a channel or a connection that is already closed raises, and would hide the error that
        # dropped it: nothing to do then
        connection = getattr(self, "connection", None)
        if connection is not None and connection.is_open:
            self._logger.debug("Closing the RabbitMQ connection")
            connection.close()


    def subscribe(self, routing_key, on_message_callback):
        created_queue_name = self.declare_local_queue(routing_key=routing_key)

        # Register an intermediate function to decode the msg.
        def decode_msg(ch, method, properties, body):
            on_message_callback(ch, method, properties, decode_json(body))

        self.channel.basic_consume(
            queue=created_queue_name,
            on_message_callback=decode_msg,
            auto_ack=True
        )
        return created_queue_name


    def start_consuming(self):
        self.channel.start_consuming()