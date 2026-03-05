package communication

import (
	"context"
	"encoding/json"
	"os"
	"runtime"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"go.uber.org/zap"
)

// Message represents a WebSocket message.
type Message struct {
	Event string      `json:"event"`
	Data  interface{} `json:"data"`
}

// MessageHandler handles incoming messages.
type MessageHandler func(*Message)

// WebSocketClient manages the WebSocket connection to the main app.
type WebSocketClient struct {
	url                string
	podID              string
	podName            string
	reconnectInterval  time.Duration
	logger             *zap.Logger
	conn               *websocket.Conn
	handler            MessageHandler
	sendChan           chan *Message
	mu                 sync.Mutex
	connected          bool
}

// NewWebSocketClient creates a new WebSocket client.
func NewWebSocketClient(
	url string,
	podID string,
	podName string,
	reconnectIntervalSeconds int,
	logger *zap.Logger,
) *WebSocketClient {
	return &WebSocketClient{
		url:               url,
		podID:             podID,
		podName:           podName,
		reconnectInterval: time.Duration(reconnectIntervalSeconds) * time.Second,
		logger:            logger,
		sendChan:          make(chan *Message, 100),
	}
}

// SetMessageHandler sets the handler for incoming messages.
func (c *WebSocketClient) SetMessageHandler(handler MessageHandler) {
	c.handler = handler
}

// Run starts the WebSocket client and handles reconnection.
func (c *WebSocketClient) Run(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			c.close()
			return
		default:
			if err := c.connect(ctx); err != nil {
				c.logger.Error("Connection failed", zap.Error(err))
			}

			// Wait before reconnecting
			select {
			case <-ctx.Done():
				return
			case <-time.After(c.reconnectInterval):
				c.logger.Info("Attempting to reconnect...")
			}
		}
	}
}

// Send sends a message to the main app.
func (c *WebSocketClient) Send(msg *Message) {
	select {
	case c.sendChan <- msg:
	default:
		c.logger.Warn("Send channel full, dropping message")
	}
}

// IsConnected returns whether the client is connected.
func (c *WebSocketClient) IsConnected() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.connected
}

// connect establishes a WebSocket connection.
func (c *WebSocketClient) connect(ctx context.Context) error {
	c.logger.Info("Connecting to main app", zap.String("url", c.url))

	// Dial WebSocket
	conn, _, err := websocket.DefaultDialer.DialContext(ctx, c.url, nil)
	if err != nil {
		return err
	}

	c.mu.Lock()
	c.conn = conn
	c.connected = true
	c.mu.Unlock()

	c.logger.Info("Connected to main app")

	// Send registration message
	if err := c.sendMessage(&Message{
		Event: "register",
		Data: map[string]interface{}{
			"pod_id":   c.podID,
			"pod_name": c.podName,
			"hostname": getHostname(),
			"os":       getOS(),
		},
	}); err != nil {
		c.close()
		return err
	}

	// Start goroutines
	errChan := make(chan error, 2)

	go c.readLoop(ctx, errChan)
	go c.writeLoop(ctx, errChan)

	// Wait for error or context cancellation
	select {
	case <-ctx.Done():
		c.close()
		return ctx.Err()
	case err := <-errChan:
		c.close()
		return err
	}
}

// readLoop reads messages from the WebSocket.
func (c *WebSocketClient) readLoop(ctx context.Context, errChan chan<- error) {
	for {
		select {
		case <-ctx.Done():
			return
		default:
			_, data, err := c.conn.ReadMessage()
			if err != nil {
				errChan <- err
				return
			}

			var msg Message
			if err := json.Unmarshal(data, &msg); err != nil {
				c.logger.Warn("Failed to parse message", zap.Error(err))
				continue
			}

			if c.handler != nil {
				c.handler(&msg)
			}
		}
	}
}

// writeLoop writes messages to the WebSocket.
func (c *WebSocketClient) writeLoop(ctx context.Context, errChan chan<- error) {
	for {
		select {
		case <-ctx.Done():
			return
		case msg := <-c.sendChan:
			if err := c.sendMessage(msg); err != nil {
				errChan <- err
				return
			}
		}
	}
}

// sendMessage sends a message immediately.
func (c *WebSocketClient) sendMessage(msg *Message) error {
	data, err := json.Marshal(msg)
	if err != nil {
		return err
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	if c.conn == nil {
		return nil
	}

	return c.conn.WriteMessage(websocket.TextMessage, data)
}

// close closes the WebSocket connection.
func (c *WebSocketClient) close() {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.conn != nil {
		c.conn.Close()
		c.conn = nil
	}
	c.connected = false
}

// Helper functions

func getHostname() string {
	hostname, _ := os.Hostname()
	return hostname
}

func getOS() string {
	return runtime.GOOS
}
