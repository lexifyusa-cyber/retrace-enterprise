package agent

import (
	"fmt"
	"io"
	"io/fs"
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

// root returns the sandboxed filesystem root.
func (h *RPCHandler) root() *os.Root {
	return h.agent.fsRoot
}

func (h *RPCHandler) readRootFile(path string) ([]byte, error) {
	f, err := h.root().Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return io.ReadAll(f)
}

// normalizePath converts an incoming path to a relative path safe for os.Root.
// Absolute paths are rejected; empty or "." maps to the root itself.
func normalizePath(path string) (string, error) {
	if path == "" || path == "." || path == "/" {
		return ".", nil
	}
	// Strip leading "/" so paths sent by the server become relative.
	path = strings.TrimPrefix(path, "/")
	if path == "" {
		return ".", nil
	}
	cleaned := filepath.Clean(path)
	if filepath.IsAbs(cleaned) {
		return "", fmt.Errorf("absolute paths are not allowed: %s", path)
	}
	if !filepath.IsLocal(cleaned) {
		return "", fmt.Errorf("path escapes root directory: %s", path)
	}
	return cleaned, nil
}

// listDirectory lists contents of a directory within the sandbox.
func (h *RPCHandler) listDirectory(params map[string]interface{}) (interface{}, error) {
	rawPath, _ := params["path"].(string)
	recursive, _ := params["recursive"].(bool)
	filters, _ := params["filters"].(map[string]interface{})

	relPath, err := normalizePath(rawPath)
	if err != nil {
		return nil, err
	}

	info, err := h.root().Stat(relPath)
	if err != nil {
		return nil, fmt.Errorf("path not found: %w", err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("path is not a directory")
	}

	includePatterns := extractPatterns(filters, "include")
	excludePatterns := extractPatterns(filters, "exclude")

	files := []map[string]interface{}{}
	totalSize := int64(0)

	if recursive {
		err = fs.WalkDir(h.root().FS(), relPath, func(entryPath string, d fs.DirEntry, walkErr error) error {
			if walkErr != nil {
				return nil
			}
			if entryPath == relPath {
				return nil
			}

			name := d.Name()
			if strings.HasPrefix(name, ".") {
				if d.IsDir() {
					return fs.SkipDir
				}
				return nil
			}
			if matchesAny(name, excludePatterns) {
				if d.IsDir() {
					return fs.SkipDir
				}
				return nil
			}
			if !d.IsDir() && len(includePatterns) > 0 && !matchesAny(name, includePatterns) {
				return nil
			}

			entryRelPath, _ := filepath.Rel(relPath, entryPath)
			entryInfo, infoErr := d.Info()
			if infoErr != nil {
				return nil
			}

			fileType := "file"
			if d.IsDir() {
				fileType = "directory"
			}

			files = append(files, map[string]interface{}{
				"name":     name,
				"path":     entryPath,
				"rel_path": entryRelPath,
				"type":     fileType,
				"size":     entryInfo.Size(),
				"modified": entryInfo.ModTime().Format("2006-01-02T15:04:05Z"),
			})

			if !d.IsDir() {
				totalSize += entryInfo.Size()
			}
			return nil
		})
	} else {
		f, openErr := h.root().Open(relPath)
		if openErr != nil {
			return nil, fmt.Errorf("failed to open directory: %w", openErr)
		}
		defer f.Close()

		entries, readErr := f.ReadDir(-1)
		if readErr != nil {
			return nil, fmt.Errorf("failed to read directory: %w", readErr)
		}

		for _, d := range entries {
			name := d.Name()
			if strings.HasPrefix(name, ".") {
				continue
			}
			if matchesAny(name, excludePatterns) {
				continue
			}
			if !d.IsDir() && len(includePatterns) > 0 && !matchesAny(name, includePatterns) {
				continue
			}

			entryInfo, infoErr := d.Info()
			if infoErr != nil {
				continue
			}

			entryPath := relPath
			if entryPath == "." {
				entryPath = name
			} else {
				entryPath = entryPath + "/" + name
			}

			fileType := "file"
			if d.IsDir() {
				fileType = "directory"
			}

			files = append(files, map[string]interface{}{
				"name":     name,
				"path":     entryPath,
				"rel_path": name,
				"type":     fileType,
				"size":     entryInfo.Size(),
				"modified": entryInfo.ModTime().Format("2006-01-02T15:04:05Z"),
			})

			if !d.IsDir() {
				totalSize += entryInfo.Size()
			}
		}
	}

	if err != nil {
		return nil, fmt.Errorf("error walking directory: %w", err)
	}

	return map[string]interface{}{
		"files":       files,
		"total_count": len(files),
		"total_size":  totalSize,
	}, nil
}

// readFile reads the contents of a file within the sandbox.
func (h *RPCHandler) readFile(params map[string]interface{}) (interface{}, error) {
	rawPath, _ := params["path"].(string)

	if rawPath == "" {
		return nil, fmt.Errorf("path is required")
	}

	relPath, err := normalizePath(rawPath)
	if err != nil {
		return nil, err
	}

	info, err := h.root().Stat(relPath)
	if err != nil {
		return nil, fmt.Errorf("file not found: %w", err)
	}
	if info.IsDir() {
		return nil, fmt.Errorf("path is a directory, not a file")
	}

	content, err := h.readRootFile(relPath)
	if err != nil {
		return nil, fmt.Errorf("error reading file: %w", err)
	}

	return map[string]interface{}{
		"content": string(content),
		"size":    info.Size(),
		"path":    relPath,
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

	paths := []string{}
	for _, p := range folderPaths {
		if s, ok := p.(string); ok {
			rel, err := normalizePath(s)
			if err != nil {
				h.agent.logger.Warn("Skipping invalid training path",
					zap.String("path", s), zap.Error(err))
				continue
			}
			paths = append(paths, rel)
		}
	}

	go h.runTraining(jobID, groupID, namespace, groupType, paths)

	return map[string]interface{}{
		"status": "started",
		"job_id": jobID,
	}, nil
}

// runTraining runs the actual training process using the sandboxed FS.
func (h *RPCHandler) runTraining(jobID, groupID, namespace, groupType string, paths []string) {
	rootFS := h.root().FS()

	totalFiles := 0
	for _, path := range paths {
		fs.WalkDir(rootFS, path, func(_ string, d fs.DirEntry, _ error) error {
			if d != nil && !d.IsDir() {
				totalFiles++
			}
			return nil
		})
	}

	processed := 0
	chunksCreated := 0
	entitiesExtracted := 0

	for _, path := range paths {
		fs.WalkDir(rootFS, path, func(filePath string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return nil
			}

			if strings.HasPrefix(d.Name(), ".") {
				return nil
			}

			ext := strings.ToLower(filepath.Ext(d.Name()))

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
				chunks := h.processGenericFile(filePath, namespace)
				chunksCreated += chunks
			}

			processed++

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

// processCodeFile reads and chunks a code file via the sandbox.
func (h *RPCHandler) processCodeFile(path, namespace string) (chunks int, entities int) {
	content, err := h.readRootFile(path)
	if err != nil {
		return 0, 0
	}

	text := string(content)
	lines := strings.Split(text, "\n")

	chunkSize := 50
	for i := 0; i < len(lines); i += chunkSize {
		end := i + chunkSize
		if end > len(lines) {
			end = len(lines)
		}

		chunkText := strings.Join(lines[i:end], "\n")

		h.agent.storage.StoreChunk(namespace, path, i, chunkText, map[string]interface{}{
			"type":       "code",
			"file_path":  path,
			"line_start": i,
			"line_end":   end,
		})

		chunks++
	}

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

// processDocFile reads and chunks a documentation file via the sandbox.
func (h *RPCHandler) processDocFile(path, namespace string) int {
	content, err := h.readRootFile(path)
	if err != nil {
		return 0
	}

	text := string(content)
	paragraphs := strings.Split(text, "\n\n")
	chunks := 0

	for i, para := range paragraphs {
		para = strings.TrimSpace(para)
		if len(para) < 50 {
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

// processGenericFile reads and chunks a generic file via the sandbox.
func (h *RPCHandler) processGenericFile(path, namespace string) int {
	content, err := h.readRootFile(path)
	if err != nil {
		return 0
	}

	text := string(content)

	if len(text) < 5000 {
		h.agent.storage.StoreChunk(namespace, path, 0, text, map[string]interface{}{
			"type":      "other",
			"file_path": path,
		})
		return 1
	}

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

	vec := make([]float32, len(embedding))
	for i, v := range embedding {
		if f, ok := v.(float64); ok {
			vec[i] = float32(f)
		}
	}

	results := h.agent.storage.VectorSearch(namespace, vec, topK)

	return map[string]interface{}{
		"results": results,
	}, nil
}

// graphQuery performs a graph query.
func (h *RPCHandler) graphQuery(params map[string]interface{}) (interface{}, error) {
	entities, _ := params["entities"].([]interface{})
	namespace, _ := params["namespace_filter"].(string)

	entityNames := []string{}
	for _, e := range entities {
		if s, ok := e.(string); ok {
			entityNames = append(entityNames, s)
		}
	}

	results := h.agent.storage.GraphQuery(namespace, entityNames)

	return map[string]interface{}{
		"entities": results,
	}, nil
}

// --- helpers ---

func extractPatterns(filters map[string]interface{}, key string) []string {
	if filters == nil {
		return nil
	}
	raw, ok := filters[key].([]interface{})
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, v := range raw {
		if s, ok := v.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func matchesAny(name string, patterns []string) bool {
	for _, p := range patterns {
		if matched, _ := filepath.Match(p, name); matched {
			return true
		}
	}
	return false
}

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
