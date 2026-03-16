#!/bin/bash
# Pre-build POD agents for all platforms

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "🔨 Pre-building POD agents for all platforms..."

cd "$PROJECT_ROOT/pod-agent"

# Ensure dependencies are ready
echo "📦 Downloading Go dependencies..."
go mod tidy

# Create dist directory
mkdir -p dist

# Build for each platform
platforms=(
    "linux/amd64"
    "darwin/amd64"
    "darwin/arm64"
    "windows/amd64"
)

for platform in "${platforms[@]}"; do
    IFS='/' read -r GOOS GOARCH <<< "$platform"
    
    output_name="retrace-agent-${GOOS}-${GOARCH}"
    if [ "$GOOS" = "windows" ]; then
        output_name="${output_name}.exe"
    fi
    
    echo "Building for $GOOS/$GOARCH..."
    
    GOOS=$GOOS GOARCH=$GOARCH go build \
        -ldflags="-s -w" \
        -o "dist/$output_name" \
        ./cmd/agent
    
    chmod +x "dist/$output_name" 2>/dev/null || true
    
    echo "  ✓ $output_name"
done

echo ""
echo "✅ All POD agents built successfully!"
ls -lh dist/
