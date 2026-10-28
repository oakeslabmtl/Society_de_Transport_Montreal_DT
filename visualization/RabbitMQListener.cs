using Godot;
using RabbitMQ.Client;
using RabbitMQ.Client.Events;
using System;
using System.Collections.Concurrent;
using System.Text;

public partial class RabbitMQListener : Node
{
	private ConnectionFactory factory = new ConnectionFactory();
	private IConnection connection;
	private IModel channel;

	private string exchangeName = "log6953fe_AMQP";
	private string ROUTING_KEY_STATE = "stm.bus.routes.updates";
	private string localQueue;
	// Filled by the RabbitMQ consumer thread, drained on the main thread in _Process
	private ConcurrentQueue<string> messages = new();

	private string userName = "log6953fe";
	private string hostName = "localhost";
	private string password = "log6953fe";
	private string port = "5672";

	[Signal]
	public delegate void OnMessageEventHandler(string message);

	public bool IsListening => connection != null && connection.IsOpen;

	// Connects to the broker and starts consuming. Returns false (with a warning) when the broker is unreachable.
	public bool StartListening()
	{
		if (IsListening) {
			return true;
		}

		if (userName != "") {
			factory.UserName = userName;
			GD.Print("User name set to: " + userName);
		}

		if (hostName != "") {
			factory.HostName = hostName;
			GD.Print("Host name set to: " + hostName);
		}

		if (password != "") {
			factory.Password = password;
			GD.Print("Password set");
		}

		if (port != "") {
			factory.Port = port.ToInt();
			GD.Print("Port set to: " + port);
		}
		else {
			factory.Port = 5672;
			GD.Print("Port not set, using default: 5672");
		}

		// Fail fast instead of freezing the startup when the broker is down
		factory.RequestedConnectionTimeout = TimeSpan.FromSeconds(3);
		factory.SocketReadTimeout = TimeSpan.FromSeconds(3);
		factory.SocketWriteTimeout = TimeSpan.FromSeconds(3);

		try {
			connection = factory.CreateConnection();
			channel = connection.CreateModel();

			localQueue = channel.QueueDeclare(autoDelete: true, exclusive: true);
			channel.QueueBind(queue: localQueue, exchange: exchangeName, routingKey: ROUTING_KEY_STATE);
			ReceiveMessage();
		}
		catch (Exception e) {
			GD.PushWarning("Could not connect to RabbitMQ at " + hostName + ":" + factory.Port + " (" + e.GetType().Name + ": " + e.Message + ")");
			CloseConnection();
			return false;
		}

		if (!connection.IsOpen) {
			GD.Print("Error! Could not connect!");
			CloseConnection();
			return false;
		}
		GD.Print("Connection established");
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

	private void ReceiveMessage() {
		GD.Print("Waiting for messages");
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
