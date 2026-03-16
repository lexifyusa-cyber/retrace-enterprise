package storage

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"go.uber.org/zap"
)

// Chunk represents a stored text chunk with metadata.
type Chunk struct {
	ID        string                 `json:"id"`
	Namespace string                 `json:"namespace"`
	FilePath  string                 `json:"file_path"`
	ChunkIdx  int                    `json:"chunk_idx"`
	Text      string                 `json:"text"`
	Metadata  map[string]interface{} `json:"metadata"`
	Embedding []float32              `json:"embedding,omitempty"`
}

// Entity represents an extracted entity.
type Entity struct {
	ID        string                 `json:"id"`
	Namespace string                 `json:"namespace"`
	Name      string                 `json:"name"`
	Type      string                 `json:"type"`
	FilePath  string                 `json:"file_path"`
	Metadata  map[string]interface{} `json:"metadata"`
}

// Manager manages the storage backends.
type Manager struct {
	dataDir string
	logger  *zap.Logger

	// In-memory stores (for MVP - replace with proper DBs in production)
	chunks   map[string][]*Chunk // namespace -> chunks
	entities map[string][]*Entity
	mu       sync.RWMutex
}

// NewManager creates a new storage manager.
func NewManager(dataDir string, logger *zap.Logger) (*Manager, error) {
	// Create data directory
	if err := os.MkdirAll(dataDir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create data directory: %w", err)
	}

	m := &Manager{
		dataDir:  dataDir,
		logger:   logger,
		chunks:   make(map[string][]*Chunk),
		entities: make(map[string][]*Entity),
	}

	// Load existing data
	if err := m.load(); err != nil {
		logger.Warn("Failed to load existing data", zap.Error(err))
	}

	return m, nil
}

// StoreChunk stores a text chunk.
func (m *Manager) StoreChunk(namespace, filePath string, chunkIdx int, text string, metadata map[string]interface{}) {
	m.mu.Lock()
	defer m.mu.Unlock()

	// Generate ID
	idInput := fmt.Sprintf("%s:%s:%d", namespace, filePath, chunkIdx)
	hash := sha256.Sum256([]byte(idInput))
	id := hex.EncodeToString(hash[:16])

	chunk := &Chunk{
		ID:        id,
		Namespace: namespace,
		FilePath:  filePath,
		ChunkIdx:  chunkIdx,
		Text:      text,
		Metadata:  metadata,
	}

	// Add to namespace
	m.chunks[namespace] = append(m.chunks[namespace], chunk)
}

// StoreEntity stores an extracted entity.
func (m *Manager) StoreEntity(namespace, name, entityType, filePath string, metadata map[string]interface{}) {
	m.mu.Lock()
	defer m.mu.Unlock()

	idInput := fmt.Sprintf("%s:%s:%s", namespace, entityType, name)
	hash := sha256.Sum256([]byte(idInput))
	id := hex.EncodeToString(hash[:16])

	entity := &Entity{
		ID:        id,
		Namespace: namespace,
		Name:      name,
		Type:      entityType,
		FilePath:  filePath,
		Metadata:  metadata,
	}

	m.entities[namespace] = append(m.entities[namespace], entity)
}

// VectorSearch performs a vector similarity search.
// For MVP, this is a simple text-based search.
// In production, use proper vector DB with embeddings.
func (m *Manager) VectorSearch(namespace string, embedding []float32, topK int) []map[string]interface{} {
	m.mu.RLock()
	defer m.mu.RUnlock()

	results := []map[string]interface{}{}

	// Get chunks for namespace (or all if empty)
	var searchChunks []*Chunk
	if namespace != "" {
		searchChunks = m.chunks[namespace]
	} else {
		for _, chunks := range m.chunks {
			searchChunks = append(searchChunks, chunks...)
		}
	}

	// For MVP without real embeddings, return all chunks (sorted by length as proxy for relevance)
	// In production, compute cosine similarity with embeddings
	for i, chunk := range searchChunks {
		if i >= topK {
			break
		}

		results = append(results, map[string]interface{}{
			"chunk_id": chunk.ID,
			"score":    0.9 - float64(i)*0.05, // Decreasing score
			"text":     chunk.Text,
			"metadata": chunk.Metadata,
		})
	}

	return results
}

// GraphQuery queries entities by name.
func (m *Manager) GraphQuery(namespace string, entityNames []string) []map[string]interface{} {
	m.mu.RLock()
	defer m.mu.RUnlock()

	results := []map[string]interface{}{}

	// Get entities for namespace (or all if empty)
	var searchEntities []*Entity
	if namespace != "" {
		searchEntities = m.entities[namespace]
	} else {
		for _, entities := range m.entities {
			searchEntities = append(searchEntities, entities...)
		}
	}

	// Search for matching entities
	nameSet := make(map[string]bool)
	for _, name := range entityNames {
		nameSet[name] = true
	}

	for _, entity := range searchEntities {
		// Check if entity name contains any of the search terms
		for name := range nameSet {
			if contains(entity.Name, name) {
				results = append(results, map[string]interface{}{
					"id":        entity.ID,
					"name":      entity.Name,
					"type":      entity.Type,
					"file_path": entity.FilePath,
					"metadata":  entity.Metadata,
				})
				break
			}
		}
	}

	return results
}

// Close closes the storage manager and persists data.
func (m *Manager) Close() error {
	return m.save()
}

// save persists data to disk.
func (m *Manager) save() error {
	m.mu.RLock()
	defer m.mu.RUnlock()

	// Save chunks
	chunksPath := filepath.Join(m.dataDir, "chunks.json")
	chunksData, err := json.Marshal(m.chunks)
	if err != nil {
		return err
	}
	if err := os.WriteFile(chunksPath, chunksData, 0644); err != nil {
		return err
	}

	// Save entities
	entitiesPath := filepath.Join(m.dataDir, "entities.json")
	entitiesData, err := json.Marshal(m.entities)
	if err != nil {
		return err
	}
	if err := os.WriteFile(entitiesPath, entitiesData, 0644); err != nil {
		return err
	}

	m.logger.Info("Storage data persisted",
		zap.Int("chunks", countChunks(m.chunks)),
		zap.Int("entities", countEntities(m.entities)),
	)

	return nil
}

// load loads data from disk.
func (m *Manager) load() error {
	m.mu.Lock()
	defer m.mu.Unlock()

	// Load chunks
	chunksPath := filepath.Join(m.dataDir, "chunks.json")
	if data, err := os.ReadFile(chunksPath); err == nil {
		if err := json.Unmarshal(data, &m.chunks); err != nil {
			return err
		}
	}

	// Load entities
	entitiesPath := filepath.Join(m.dataDir, "entities.json")
	if data, err := os.ReadFile(entitiesPath); err == nil {
		if err := json.Unmarshal(data, &m.entities); err != nil {
			return err
		}
	}

	m.logger.Info("Storage data loaded",
		zap.Int("chunks", countChunks(m.chunks)),
		zap.Int("entities", countEntities(m.entities)),
	)

	return nil
}

// Helper functions

func contains(s, substr string) bool {
	return strings.Contains(strings.ToLower(s), strings.ToLower(substr))
}

func countChunks(chunks map[string][]*Chunk) int {
	total := 0
	for _, c := range chunks {
		total += len(c)
	}
	return total
}

func countEntities(entities map[string][]*Entity) int {
	total := 0
	for _, e := range entities {
		total += len(e)
	}
	return total
}
