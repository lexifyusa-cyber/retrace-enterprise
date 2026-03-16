#!/bin/bash
# Build POD Agent for multiple platforms

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$PROJECT_ROOT/dist"

VERSION=${1:-"0.1.0"}

echo "🔨 Building ReTrace v${VERSION}"
echo "=========================================="

cd "$PROJECT_ROOT"

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Build for each platform
platforms=(
    "linux/amd64"
    "linux/arm64"
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
        -ldflags="-s -w -X main.Version=${VERSION}" \
        -o "$OUTPUT_DIR/$output_name" \
        ./cmd/agent
    
    echo "  ✓ $output_name"
done

echo ""
echo "=========================================="
echo "✅ Build complete! Binaries in: $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"
