package main

import (
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/lumenatech/retrace-agent/internal/agent"
	"github.com/spf13/cobra"
	"github.com/spf13/viper"
	"go.uber.org/zap"
)

var (
	cfgFile string
	logger  *zap.Logger
)

func main() {
	rootCmd := &cobra.Command{
		Use:   "retrace-agent",
		Short: "ReTrace POD Agent by Lumena",
		Long: `ReTrace POD Agent — A lightweight knowledge processing agent
by Lumena Technologies that connects to the ReTrace main application
and processes local files for knowledge extraction and querying.`,
		Run: runAgent,
	}

	rootCmd.PersistentFlags().StringVar(&cfgFile, "config", "", "config file (default is ./config.yaml)")

	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func runAgent(cmd *cobra.Command, args []string) {
	// Initialize logger
	var err error
	logger, err = zap.NewDevelopment()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to initialize logger: %v\n", err)
		os.Exit(1)
	}
	defer logger.Sync()

	// Load configuration
	config, err := loadConfig()
	if err != nil {
		logger.Fatal("Failed to load configuration", zap.Error(err))
	}

	// Validate pod name is set
	if config.Pod.Name == "" || config.Pod.Name == "CHANGE_ME" {
		logger.Fatal("Pod name is not configured. The agent name should be set during generation from the ReTrace UI.")
	}

	logger.Info("Starting ReTrace",
		zap.String("pod_id", config.Pod.ID),
		zap.String("pod_name", config.Pod.Name),
	)

	// Create and start agent
	podAgent, err := agent.New(config, logger)
	if err != nil {
		logger.Fatal("Failed to create agent", zap.Error(err))
	}

	// Start agent
	if err := podAgent.Start(); err != nil {
		logger.Fatal("Failed to start agent", zap.Error(err))
	}

	// Wait for shutdown signal
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	<-sigChan
	logger.Info("Shutdown signal received")

	// Graceful shutdown
	if err := podAgent.Stop(); err != nil {
		logger.Error("Error during shutdown", zap.Error(err))
	}

	logger.Info("Agent stopped")
}

func loadConfig() (*agent.Config, error) {
	if cfgFile != "" {
		viper.SetConfigFile(cfgFile)
	} else {
		viper.SetConfigName("config")
		viper.SetConfigType("yaml")
		viper.AddConfigPath(".")
		viper.AddConfigPath("./configs")
		viper.AddConfigPath("/etc/retrace")
	}

	// Set defaults
	viper.SetDefault("pod.id", "")
	viper.SetDefault("pod.name", "")
	viper.SetDefault("main_app.url", "ws://localhost:8001")
	viper.SetDefault("main_app.reconnect_interval_seconds", 30)
	viper.SetDefault("storage.data_dir", "./pod-data")
	viper.SetDefault("resources.max_memory_mb", 2048)
	viper.SetDefault("resources.max_cpu_percent", 50)
	viper.SetDefault("file_watcher.enabled", true)
	viper.SetDefault("file_watcher.debounce_seconds", 5)

	// Read config file
	if err := viper.ReadInConfig(); err != nil {
		if _, ok := err.(viper.ConfigFileNotFoundError); !ok {
			return nil, fmt.Errorf("error reading config file: %w", err)
		}
		// Config file not found; use defaults
	}

	// Unmarshal config
	var config agent.Config
	if err := viper.Unmarshal(&config); err != nil {
		return nil, fmt.Errorf("error unmarshaling config: %w", err)
	}

	// Generate POD ID if not set
	if config.Pod.ID == "" {
		config.Pod.ID = generatePodID()
		logger.Info("Generated new POD ID", zap.String("pod_id", config.Pod.ID))
	}

	return &config, nil
}

func generatePodID() string {
	// Simple UUID-like generation
	// In production, use github.com/google/uuid
	return fmt.Sprintf("pod-%d", os.Getpid())
}
