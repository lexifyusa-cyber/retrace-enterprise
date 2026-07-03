package agent

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func newTestRPCHandler(t *testing.T) *RPCHandler {
	t.Helper()
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "docs"), 0o755); err != nil {
		t.Fatalf("create docs dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "docs", "readme.txt"), []byte("hello from sandbox"), 0o644); err != nil {
		t.Fatalf("write test file: %v", err)
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		t.Fatalf("open root: %v", err)
	}
	t.Cleanup(func() { _ = root.Close() })
	return &RPCHandler{agent: &Agent{fsRoot: root}}
}

func TestNormalizePathAllowsLocalPathsAndStripsLeadingSlash(t *testing.T) {
	cases := map[string]string{
		"":                ".",
		".":               ".",
		"/":               ".",
		"/docs/readme.txt": "docs/readme.txt",
		"docs/../docs/readme.txt": "docs/readme.txt",
	}
	for input, want := range cases {
		got, err := normalizePath(input)
		if err != nil {
			t.Fatalf("normalizePath(%q) unexpected error: %v", input, err)
		}
		if got != want {
			t.Fatalf("normalizePath(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestNormalizePathRejectsTraversal(t *testing.T) {
	for _, input := range []string{"..", "../secret.txt", "docs/../../secret.txt"} {
		if _, err := normalizePath(input); err == nil {
			t.Fatalf("normalizePath(%q) expected traversal error", input)
		}
	}
}

func TestReadFileReadsWithinRoot(t *testing.T) {
	h := newTestRPCHandler(t)
	result, err := h.readFile(map[string]interface{}{"path": "/docs/readme.txt"})
	if err != nil {
		t.Fatalf("readFile unexpected error: %v", err)
	}
	payload, ok := result.(map[string]interface{})
	if !ok {
		t.Fatalf("readFile returned %T", result)
	}
	if payload["content"] != "hello from sandbox" {
		t.Fatalf("content = %q", payload["content"])
	}
	if payload["path"] != "docs/readme.txt" {
		t.Fatalf("path = %q", payload["path"])
	}
}

func TestReadFileRejectsDirectoryAndTraversal(t *testing.T) {
	h := newTestRPCHandler(t)
	if _, err := h.readFile(map[string]interface{}{"path": "docs"}); err == nil || !strings.Contains(err.Error(), "directory") {
		t.Fatalf("expected directory error, got %v", err)
	}
	if _, err := h.readFile(map[string]interface{}{"path": "../secret.txt"}); err == nil {
		t.Fatalf("expected traversal error")
	}
}
