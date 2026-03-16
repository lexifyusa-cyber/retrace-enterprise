package agent

import (
	"fmt"
	"io/ioutil"
	"os"
	"path/filepath"
	"strings"

	"github.com/lumenatech/retrace-agent/internal/communication"
	"go.uber.org/zap"
)

// RPCHandler handles RPC method calls from the main application.
type RPCHandler struct {
	agent *Agent
}

// NewRPCHandler creates a new RPC handler.
func NewRPCHandler(agent *Agent) *RPCHandler {
	return &RPCHandler{agent: agent}
}

// Handle processes an RPC method call.
func (h *RPCHandler) Handle(method string, params map[string]interface{}) (interface{}, error) {
	switch method {
	case "list_directory":
		return h.listDirectory(params)
	case "read_file":
		return h.readFile(params)
	case "start_training":
		return h.startTraining(params)
	case "vector_search":
		return h.vectorSearch(params)
	case "graph_query":
		return h.graphQuery(params)
	default:
		return nil, fmt.Errorf("unknown method: %s", method)
	}
}

// listDirectory lists contents of a directory.
func (h *RPCHandler) listDirectory(params map[string]interface{}) (interface{}, error) {
	path, _ := params["path"].(string)
	recursive, _ := params["recursive"].(bool)
	filters, _ := params["filters"].(map[string]interface{})

	if path == "" {
		path = "/"
	}

	// Security: Ensure path is absolute and exists
	absPath, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("invalid path: %w", err)
	}

	info, err := os.Stat(absPath)
	if err != nil {
		return nil, fmt.Errorf("path not found: %w", err)
	}

	if !info.IsDir() {
		return nil, fmt.Errorf("path is not a directory")
	}

	// Get include/exclude patterns
	includePatterns := []string{}
	excludePatterns := []string{}

	if filters != nil {
		if inc, ok := filters["include"].([]interface{}); ok {
			for _, p := range inc {
				if s, ok := p.(string); ok {
					includePatterns = append(includePatterns, s)
				}
			}
		}
		if exc, ok := filters["exclude"].([]interface{}); ok {
			for _, p := range exc {
				if s, ok := p.(string); ok {
					excludePatterns = append(excludePatterns, s)
				}
			}
		}
	}

	// List directory
	files := []map[string]interface{}{}
	totalSize := int64(0)

	walkFunc := func(filePath string, info os.FileInfo, err error) error {
		if err != nil {
			return nil // Skip files with errors
		}

		// Skip hidden files
		if strings.HasPrefix(info.Name(), ".") {
			if info.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}

		// Apply exclude patterns
		for _, pattern := range excludePatterns {
			matched, _ := filepath.Match(pattern, info.Name())
			if matched {
				if info.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
		}

		// Apply include patterns (only for files)
		if !info.IsDir() && len(includePatterns) > 0 {
			matched := false
			for _, pattern := range includePatterns {
				if m, _ := filepath.Match(pattern, info.Name()); m {
					matched = true
					break
				}
			}
			if !matched {
				return nil
			}
		}

		relPath, _ := filepath.Rel(absPath, filePath)
		if relPath == "." {
			return nil
		}

		fileType := "file"
		if info.IsDir() {
			fileType = "directory"
		}

		files = append(files, map[string]interface{}{
			"name":     info.Name(),
			"path":     filePath,
			"rel_path": relPath,
			"type":     fileType,
			"size":     info.Size(),
			"modified": info.ModTime().Format("2006-01-02T15:04:05Z"),
		})

		if !info.IsDir() {
			totalSize += info.Size()
		}

		if !recursive && info.IsDir() && filePath != absPath {
			return filepath.SkipDir
		}

		return nil
	}

	if err := filepath.Walk(absPath, walkFunc); err != nil {
		return nil, fmt.Errorf("error walking directory: %w", err)
	}

	return map[string]interface{}{
		"files":       files,
		"total_count": len(files),
		"total_size":  totalSize,
	}, nil
}

// readFile reads the contents of a file.
func (h *RPCHandler) readFile(params map[string]interface{}) (interface{}, error) {
	path, _ := params["path"].(string)

	if path == "" {
		return nil, fmt.Errorf("path is required")
	}

	// Security: Ensure path is absolute
	absPath, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("invalid path: %w", err)
	}

	info, err := os.Stat(absPath)
	if err != nil {
		return nil, fmt.Errorf("file not found: %w", err)
	}

	if info.IsDir() {
		return nil, fmt.Errorf("path is a directory, not a file")
	}

	// Read file
	content, err := ioutil.ReadFile(absPath)
	if err != nil {
		return nil, fmt.Errorf("error reading file: %w", err)
	}

	return map[string]interface{}{
		"content": string(content),
		"size":    info.Size(),
		"path":    absPath,
	}, nil
}

// startTraining starts a training job on this POD.
func (h *RPCHandler) startTraining(params map[string]interface{}) (interface{}, error) {
	jobID, _ := params["job_id"].(string)
	groupID, _ := params["group_id"].(string)
	namespace, _ := params["namespace"].(string)
	groupType, _ := params["group_type"].(string)
	folderPaths, _ := params["folder_paths"].([]interface{})

	h.agent.logger.Info("Starting training job",
		zap.String("job_id", jobID),
		zap.String("group_id", groupID),
		zap.String("namespace", namespace),
	)

	// Convert folder paths
	paths := []string{}
	for _, p := range folderPaths {
		if s, ok := p.(string); ok {
			paths = append(paths, s)
		}
	}

	// Start training in background
	go h.runTraining(jobID, groupID, namespace, groupType, paths)

	return map[string]interface{}{
		"status": "started",
		"job_id": jobID,
	}, nil
}

// runTraining runs the actual training process.
func (h *RPCHandler) runTraining(jobID, groupID, namespace, groupType string, paths []string) {
	// Count total files
	totalFiles := 0
	for _, path := range paths {
		filepath.Walk(path, func(_ string, info os.FileInfo, _ error) error {
			if info != nil && !info.IsDir() {
				totalFiles++
			}
			return nil
		})
	}

	processed := 0
	chunksCreated := 0
	entitiesExtracted := 0

	// Process each path
	for _, path := range paths {
		filepath.Walk(path, func(filePath string, info os.FileInfo, err error) error {
			if err != nil || info.IsDir() {
				return nil
			}

			// Skip hidden files
			if strings.HasPrefix(info.Name(), ".") {
				return nil
			}

			// Process file based on type
			ext := strings.ToLower(filepath.Ext(info.Name()))

			switch groupType {
			case "code":
				if isCodeFile(ext) {
					chunks, entities := h.processCodeFile(filePath, namespace)
					chunksCreated += chunks
					entitiesExtracted += entities
				}
			case "documentation":
				if isDocFile(ext) {
					chunks := h.processDocFile(filePath, namespace)
					chunksCreated += chunks
				}
			default:
				// Generic processing
				chunks := h.processGenericFile(filePath, namespace)
				chunksCreated += chunks
			}

			processed++

			// Send progress update
			h.agent.wsClient.Send(&communication.Message{
				Event: "training_progress",
				Data: map[string]interface{}{
					"job_id":       jobID,
					"phase":        "processing",
					"progress":     processed,
					"total":        totalFiles,
					"current_file": filePath,
				},
			})

			return nil
		})
	}

	h.agent.logger.Info("Training completed",
		zap.String("job_id", jobID),
		zap.Int("files_processed", processed),
		zap.Int("chunks_created", chunksCreated),
	)

	// Send completion
	h.agent.wsClient.Send(&communication.Message{
		Event: "training_progress",
		Data: map[string]interface{}{
			"job_id":   jobID,
			"phase":    "completed",
			"progress": processed,
			"total":    totalFiles,
			"statistics": map[string]interface{}{
				"files_processed":    processed,
				"chunks_created":     chunksCreated,
				"entities_extracted": entitiesExtracted,
			},
		},
	})
}

// processCodeFile processes a code file.
func (h *RPCHandler) processCodeFile(path, namespace string) (chunks int, entities int) {
	content, err := ioutil.ReadFile(path)
	if err != nil {
		return 0, 0
	}

	// Simple chunking by lines (in production, use AST parsing)
	text := string(content)
	lines := strings.Split(text, "\n")

	// Create chunks of ~50 lines
	chunkSize := 50
	for i := 0; i < len(lines); i += chunkSize {
		end := i + chunkSize
		if end > len(lines) {
			end = len(lines)
		}

		chunkText := strings.Join(lines[i:end], "\n")

		// Store in vector DB
		h.agent.storage.StoreChunk(namespace, path, i, chunkText, map[string]interface{}{
			"type":       "code",
			"file_path":  path,
			"line_start": i,
			"line_end":   end,
		})

		chunks++
	}

	// Extract simple entities (functions/classes)
	// In production, use proper AST parsing
	for _, line := range lines {
		if strings.HasPrefix(strings.TrimSpace(line), "def ") ||
			strings.HasPrefix(strings.TrimSpace(line), "class ") ||
			strings.HasPrefix(strings.TrimSpace(line), "func ") ||
			strings.HasPrefix(strings.TrimSpace(line), "function ") {
			entities++
		}
	}

	return chunks, entities
}

// processDocFile processes a documentation file.
func (h *RPCHandler) processDocFile(path, namespace string) int {
	content, err := ioutil.ReadFile(path)
	if err != nil {
		return 0
	}

	text := string(content)

	// Simple chunking by paragraphs
	paragraphs := strings.Split(text, "\n\n")
	chunks := 0

	for i, para := range paragraphs {
		para = strings.TrimSpace(para)
		if len(para) < 50 { // Skip very short paragraphs
			continue
		}

		h.agent.storage.StoreChunk(namespace, path, i, para, map[string]interface{}{
			"type":      "documentation",
			"file_path": path,
			"section":   i,
		})

		chunks++
	}

	return chunks
}

// processGenericFile processes a generic file.
func (h *RPCHandler) processGenericFile(path, namespace string) int {
	content, err := ioutil.ReadFile(path)
	if err != nil {
		return 0
	}

	text := string(content)

	// Store as single chunk if small
	if len(text) < 5000 {
		h.agent.storage.StoreChunk(namespace, path, 0, text, map[string]interface{}{
			"type":      "other",
			"file_path": path,
		})
		return 1
	}

	// Otherwise, chunk by size
	chunkSize := 2000
	chunks := 0

	for i := 0; i < len(text); i += chunkSize {
		end := i + chunkSize
		if end > len(text) {
			end = len(text)
		}

		h.agent.storage.StoreChunk(namespace, path, i/chunkSize, text[i:end], map[string]interface{}{
			"type":      "other",
			"file_path": path,
			"offset":    i,
		})

		chunks++
	}

	return chunks
}

// vectorSearch performs a vector similarity search.
func (h *RPCHandler) vectorSearch(params map[string]interface{}) (interface{}, error) {
	embedding, _ := params["embedding"].([]interface{})
	namespace, _ := params["namespace_filter"].(string)
	topK := 10
	if k, ok := params["top_k"].(float64); ok {
		topK = int(k)
	}

	// Convert embedding to float slice
	vec := make([]float32, len(embedding))
	for i, v := range embedding {
		if f, ok := v.(float64); ok {
			vec[i] = float32(f)
		}
	}

	// Search storage
	results := h.agent.storage.VectorSearch(namespace, vec, topK)

	return map[string]interface{}{
		"results": results,
	}, nil
}

// graphQuery performs a graph query.
func (h *RPCHandler) graphQuery(params map[string]interface{}) (interface{}, error) {
	entities, _ := params["entities"].([]interface{})
	namespace, _ := params["namespace_filter"].(string)

	// Convert entities
	entityNames := []string{}
	for _, e := range entities {
		if s, ok := e.(string); ok {
			entityNames = append(entityNames, s)
		}
	}

	// Query graph
	results := h.agent.storage.GraphQuery(namespace, entityNames)

	return map[string]interface{}{
		"entities": results,
	}, nil
}

// Helper functions

func isCodeFile(ext string) bool {
	codeExts := map[string]bool{
		".py": true, ".js": true, ".ts": true, ".jsx": true, ".tsx": true,
		".go": true, ".rs": true, ".java": true, ".c": true, ".cpp": true,
		".h": true, ".hpp": true, ".rb": true, ".php": true, ".swift": true,
		".kt": true, ".scala": true, ".cs": true,
	}
	return codeExts[ext]
}

func isDocFile(ext string) bool {
	docExts := map[string]bool{
		".md": true, ".txt": true, ".rst": true, ".adoc": true,
		".html": true, ".htm": true,
	}
	return docExts[ext]
}
