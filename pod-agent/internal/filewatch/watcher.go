package filewatch

import (
	"context"
	"path/filepath"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"
	"go.uber.org/zap"
)

// ChangeHandler handles file change notifications.
type ChangeHandler func(path string, changeType string)

// Watcher watches for file changes.
type Watcher struct {
	debounceSeconds int
	logger          *zap.Logger
	handler         ChangeHandler
	watchPaths      []string
	watcher         *fsnotify.Watcher

	// Debouncing
	pendingChanges map[string]string
	mu             sync.Mutex
}

// NewWatcher creates a new file watcher.
func NewWatcher(debounceSeconds int, logger *zap.Logger) *Watcher {
	return &Watcher{
		debounceSeconds: debounceSeconds,
		logger:          logger,
		pendingChanges:  make(map[string]string),
	}
}

// SetChangeHandler sets the handler for file changes.
func (w *Watcher) SetChangeHandler(handler ChangeHandler) {
	w.handler = handler
}

// AddPath adds a path to watch.
func (w *Watcher) AddPath(path string) error {
	absPath, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	w.watchPaths = append(w.watchPaths, absPath)
	return nil
}

// Run starts the file watcher.
func (w *Watcher) Run(ctx context.Context) {
	var err error
	w.watcher, err = fsnotify.NewWatcher()
	if err != nil {
		w.logger.Error("Failed to create file watcher", zap.Error(err))
		return
	}
	defer w.watcher.Close()

	// Add paths to watch
	for _, path := range w.watchPaths {
		if err := w.watcher.Add(path); err != nil {
			w.logger.Warn("Failed to watch path", zap.String("path", path), zap.Error(err))
		} else {
			w.logger.Info("Watching path", zap.String("path", path))
		}
	}

	// Start debounce processor
	go w.processDebounced(ctx)

	// Process events
	for {
		select {
		case <-ctx.Done():
			return

		case event, ok := <-w.watcher.Events:
			if !ok {
				return
			}
			w.handleEvent(event)

		case err, ok := <-w.watcher.Errors:
			if !ok {
				return
			}
			w.logger.Error("Watcher error", zap.Error(err))
		}
	}
}

// handleEvent handles a file system event.
func (w *Watcher) handleEvent(event fsnotify.Event) {
	var changeType string

	switch {
	case event.Op&fsnotify.Create == fsnotify.Create:
		changeType = "created"
	case event.Op&fsnotify.Write == fsnotify.Write:
		changeType = "modified"
	case event.Op&fsnotify.Remove == fsnotify.Remove:
		changeType = "deleted"
	case event.Op&fsnotify.Rename == fsnotify.Rename:
		changeType = "renamed"
	default:
		return // Ignore other events
	}

	// Add to pending changes (debounce)
	w.mu.Lock()
	w.pendingChanges[event.Name] = changeType
	w.mu.Unlock()
}

// processDebounced processes debounced file changes.
func (w *Watcher) processDebounced(ctx context.Context) {
	ticker := time.NewTicker(time.Duration(w.debounceSeconds) * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return

		case <-ticker.C:
			w.mu.Lock()
			if len(w.pendingChanges) > 0 {
				changes := w.pendingChanges
				w.pendingChanges = make(map[string]string)
				w.mu.Unlock()

				// Process changes
				for path, changeType := range changes {
					if w.handler != nil {
						w.handler(path, changeType)
					}
				}
			} else {
				w.mu.Unlock()
			}
		}
	}
}
