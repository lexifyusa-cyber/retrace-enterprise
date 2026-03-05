package agent

import (
	"context"
	"fmt"
	"runtime"
	"sync"
	"time"

	"github.com/lumenatech/retrace-agent/internal/communication"
	"github.com/lumenatech/retrace-agent/internal/filewatch"
	"github.com/lumenatech/retrace-agent/internal/storage"
	"go.uber.org/zap"
)

// Config holds the agent configuration.
type Config struct {
	Pod struct {
		ID   string `mapstructure:"id"`
		Name string `mapstructure:"name"`
	} `mapstructure:"pod"`

	MainApp struct {
		URL                      string `mapstructure:"url"`
		ReconnectIntervalSeconds int    `mapstructure:"reconnect_interval_seconds"`
	} `mapstructure:"main_app"`

	Storage struct {
		DataDir  string `mapstructure:"data_dir"`
		VectorDB struct {
			Type string `mapstructure:"type"`
			Path string `mapstructure:"path"`
		} `mapstructure:"vector_db"`
		MetadataDB struct {
			Type string `mapstructure:"type"`
			Path string `mapstructure:"path"`
		} `mapstructure:"metadata_db"`
	} `mapstructure:"storage"`

	Resources struct {
		MaxMemoryMB   int `mapstructure:"max_memory_mb"`
		MaxCPUPercent int `mapstructure:"max_cpu_percent"`
	} `mapstructure:"resources"`

	FileWatcher struct {
		Enabled         bool `mapstructure:"enabled"`
		DebounceSeconds int  `mapstructure:"debounce_seconds"`
	} `mapstructure:"file_watcher"`
}

// Agent represents the POD agent.
type Agent struct {
	config      *Config
	logger      *zap.Logger
	wsClient    *communication.WebSocketClient
	storage     *storage.Manager
	fileWatcher *filewatch.Watcher
	rpcHandler  *RPCHandler

	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

// New creates a new Agent instance.
func New(config *Config, logger *zap.Logger) (*Agent, error) {
	ctx, cancel := context.WithCancel(context.Background())

	// Initialize storage
	storageManager, err := storage.NewManager(config.Storage.DataDir, logger)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("failed to initialize storage: %w", err)
	}

	// Create agent
	agent := &Agent{
		config:  config,
		logger:  logger,
		storage: storageManager,
		ctx:     ctx,
		cancel:  cancel,
	}

	// Initialize WebSocket client
	agent.wsClient = communication.NewWebSocketClient(
		config.MainApp.URL,
		config.Pod.ID,
		config.Pod.Name,
		config.MainApp.ReconnectIntervalSeconds,
		logger,
	)

	// Initialize RPC handler
	agent.rpcHandler = NewRPCHandler(agent)

	// Initialize file watcher if enabled
	if config.FileWatcher.Enabled {
		agent.fileWatcher = filewatch.NewWatcher(
			config.FileWatcher.DebounceSeconds,
			logger,
		)
	}

	return agent, nil
}

// Start starts the agent.
func (a *Agent) Start() error {
	a.logger.Info("Starting agent components")

	// Set up message handler
	a.wsClient.SetMessageHandler(a.handleMessage)

	// Connect to main app
	a.wg.Add(1)
	go func() {
		defer a.wg.Done()
		a.wsClient.Run(a.ctx)
	}()

	// Start heartbeat
	a.wg.Add(1)
	go func() {
		defer a.wg.Done()
		a.runHeartbeat()
	}()

	// Start file watcher if enabled
	if a.fileWatcher != nil {
		a.fileWatcher.SetChangeHandler(a.handleFileChange)
		a.wg.Add(1)
		go func() {
			defer a.wg.Done()
			a.fileWatcher.Run(a.ctx)
		}()
	}

	return nil
}

// Stop stops the agent gracefully.
func (a *Agent) Stop() error {
	a.logger.Info("Stopping agent")
	a.cancel()
	a.wg.Wait()

	if err := a.storage.Close(); err != nil {
		a.logger.Error("Error closing storage", zap.Error(err))
	}

	return nil
}

// handleMessage handles incoming WebSocket messages.
func (a *Agent) handleMessage(msg *communication.Message) {
	switch msg.Event {
	case "registered":
		a.logger.Info("Successfully registered with main app")

	case "rpc_request":
		a.handleRPCRequest(msg)

	default:
		a.logger.Warn("Unknown message event", zap.String("event", msg.Event))
	}
}

// handleRPCRequest handles RPC requests from main app.
func (a *Agent) handleRPCRequest(msg *communication.Message) {
	data, ok := msg.Data.(map[string]interface{})
	if !ok {
		a.logger.Error("Invalid RPC request data")
		return
	}

	callID, _ := data["call_id"].(string)
	method, _ := data["method"].(string)
	params, _ := data["params"].(map[string]interface{})

	a.logger.Debug("RPC request received",
		zap.String("call_id", callID),
		zap.String("method", method),
	)

	// Process RPC request
	result, err := a.rpcHandler.Handle(method, params)

	// Send response
	response := map[string]interface{}{
		"call_id": callID,
	}

	if err != nil {
		response["error"] = err.Error()
	} else {
		response["result"] = result
	}

	a.wsClient.Send(&communication.Message{
		Event: "rpc_response",
		Data:  response,
	})
}

// handleFileChange handles file change notifications.
func (a *Agent) handleFileChange(path string, changeType string) {
	a.logger.Debug("File change detected",
		zap.String("path", path),
		zap.String("change_type", changeType),
	)

	// Notify main app
	a.wsClient.Send(&communication.Message{
		Event: "file_changed",
		Data: map[string]interface{}{
			"pod_id":      a.config.Pod.ID,
			"file_path":   path,
			"change_type": changeType,
		},
	})
}

// runHeartbeat sends periodic heartbeats to main app.
func (a *Agent) runHeartbeat() {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-a.ctx.Done():
			return
		case <-ticker.C:
			var memStats runtime.MemStats
			runtime.ReadMemStats(&memStats)

			a.wsClient.Send(&communication.Message{
				Event: "heartbeat",
				Data: map[string]interface{}{
					"pod_id":    a.config.Pod.ID,
					"timestamp": time.Now().UTC().Format(time.RFC3339),
					"status":    "healthy",
					"metrics": map[string]interface{}{
						"memory_usage_mb": memStats.Alloc / 1024 / 1024,
						"goroutines":      runtime.NumGoroutine(),
					},
				},
			})
		}
	}
}
