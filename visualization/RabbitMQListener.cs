using Godot;
using RabbitMQ.Client;
using RabbitMQ.Client.Events;
using System;
using System.Collections.Concurrent;
using System.Text;

public partial class RabbitMQListener : Node
{
	private const string ExchangeName = "log6953fe_AMQP";
	private const string RoutingKey = "stm.bus.routes.updates";

	// The local broker of software/config/installation/rabbitmq (its development account)
	private readonly ConnectionFactory factory = new ConnectionFactory {
		HostName = "localhost",
		Port = 5672,
		UserName = "log6953fe",
		Password = "log6953fe",
		// Fail fast instead of freezing the startup when the broker is down
		RequestedConnectionTimeout = TimeSpan.FromSeconds(3),
		SocketReadTimeout = TimeSpan.FromSeconds(3),
		SocketWriteTimeout = TimeSpan.FromSeconds(3),
	};
	private IConnection connection;
	private IModel channel;
	private string localQueue;
	// Filled by the RabbitMQ consumer thread, drained on the main thread in _Process
	private readonly ConcurrentQueue<string> messages = new();

	[Signal]
	public delegate void OnMessageEventHandler(string message);

	public bool IsListening => connection != null && connection.IsOpen;

	// Connects to the broker and starts consuming. Returns false (with a warning) when the broker is unreachable.
	public bool StartListening()
	{
		if (IsListening) {
			return true;
		}

		try {
			connection = factory.CreateConnection();
			channel = connection.CreateModel();

			localQueue = channel.QueueDeclare(autoDelete: true, exclusive: true);
			channel.QueueBind(queue: localQueue, exchange: ExchangeName, routingKey: RoutingKey);
			StartConsuming();
		}
		catch (Exception e) {
			GD.PushWarning("Could not connect to RabbitMQ at " + factory.HostName + ":" + factory.Port + " (" + e.GetType().Name + ": " + e.Message + ")");
			CloseConnection();
			return false;
		}

		if (!connection.IsOpen) {
			GD.Print("Error! Could not connect!");
			CloseConnection();
			return false;
		}
		GD.Print("Connected to RabbitMQ at " + factory.HostName + ":" + factory.Port);
		return true;
	}

	public override void _ExitTree()
	{
		CloseConnection();
	}

	public override void _Process(double delta) {
		while (messages.TryDequeue(out string message)) {
			EmitSignal(SignalName.OnMessage, message);
		}
	}

	private void StartConsuming() {
		var consumer = new EventingBasicConsumer(channel);

		consumer.Received += (model, ea) => {
			var body = ea.Body.ToArray();
			var message = Encoding.ASCII.GetString(body);
			messages.Enqueue(message);
		};

		channel.BasicConsume(queue: localQueue, autoAck: true, consumer: consumer);
	}

	private void CloseConnection() {
		try {
			if (channel != null && channel.IsOpen) {
				channel.Close();
			}
		}
		catch (Exception) { }
		try {
			if (connection != null && connection.IsOpen) {
				connection.Close();
			}
		}
		catch (Exception) { }
		try {
			channel?.Dispose();
			connection?.Dispose();
		}
		catch (Exception) { }
		channel = null;
		connection = null;
	}
}
