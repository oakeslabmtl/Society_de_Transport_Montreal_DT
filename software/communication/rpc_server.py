import inspect
import pika
import logging
from communication.rabbitmq import RabbitMQ
from communication.protocol import decode_json, encode_json

METHOD_ATTRIBUTE = "method"
ARGS_ATTRIBUTE = "args"

class RPCServer(RabbitMQ):

    def __init__(self, ip, port, username, password, vhost, exchange_name, exchange_type, ssl = None):
        super().__init__(ip, port, username, password, vhost, exchange_name, exchange_type, ssl)
        self._logger = logging.getLogger("RPCServer")


    def setup(self, routing_key, queue_name, on_message_callback = None):
        self.connect_to_server()
        self.channel.basic_qos(prefetch_count=1)
        self.channel.queue_declare(queue=queue_name)
        self.channel.queue_bind(exchange=self.exchange_name, queue=queue_name, routing_key=routing_key)

        if on_message_callback is None:
            on_message_callback = self.serve

        self.channel.basic_consume(queue=queue_name, on_message_callback=on_message_callback)

        self._logger.debug(f"Ready to listen for messages in queue {queue_name} bound to topic {routing_key}")


    def start_serving(self):
        self.start_consuming()


    def serve(self, ch, method, props, body):
        body_json = decode_json(body)
        self._logger.debug(f"Message received: \n{body_json}")

        # Check if reply_to is given
        if props.reply_to is None:
            self._logger.warning(f"Message received does not have reply_to. Will be ignored. Message:\n{body_json}.")
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        routing_key_reply = props.reply_to
        self._logger.debug(f"routing_key_reply = {routing_key_reply}")

        request_id = props.correlation_id
        self._logger.debug(f"request_id = {request_id}")

        # Create short function to reply
        def reply(msg):
            self._logger.debug(f"Sending reply message:\n{msg}")
            ch.basic_publish(
                exchange='',
                routing_key=routing_key_reply,
                properties=pika.BasicProperties(correlation_id=request_id),
                body=encode_json(msg)
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)

        # Check if method is provided
        if METHOD_ATTRIBUTE not in body_json:
            self._logger.warning(f"Message received does not have attribute {METHOD_ATTRIBUTE}. Message:\n{body_json}")
            reply({"error": f"Attribute {METHOD_ATTRIBUTE} must be specified."})
            return

        server_method = body_json[METHOD_ATTRIBUTE]

        # Check if method exists in subclasses
        method_op = getattr(self, server_method, None)
        if method_op is None:
            self._logger.warning(f"Method specified does not exist: {server_method}. Message:\n{body_json}")
            reply({"error": f"Method specified does not exist: {server_method}."})
            return

        # Check if args are provided
        if ARGS_ATTRIBUTE not in body_json:
            self._logger.warning(f"Message received does not have arguments in attribute {ARGS_ATTRIBUTE}. Message:\n{body_json}")
            reply({"error": f"Message received does not have arguments in attribute {ARGS_ATTRIBUTE}."})
            return
        args = body_json[ARGS_ATTRIBUTE]

        # Get method signature and compare it with args provided
        # This ensures that methods are called with the arguments in their signature.
        signature = inspect.signature(method_op)
        if "callback_func" not in signature.parameters:
            error_msg = f"Method {method_op} must declare a parameter 'callback_func' that must be invoked to reply to an invocation."
            self._logger.warning(error_msg)
            reply({"error": error_msg})
            return

        for arg_name in signature.parameters:
            if arg_name != "callback_func" and arg_name not in args:
                self._logger.warning(f"Message received does not specify argument {arg_name} in attribute {ARGS_ATTRIBUTE}. Message:\n{body_json}")
                reply({"error": f"Message received does not specify argument {arg_name} in attribute {ARGS_ATTRIBUTE}."})
                return

        # Call method with named arguments provided.
        method_op(**args, callback_func=reply)


    def echo(self, msg, callback_func):
        callback_func(msg)