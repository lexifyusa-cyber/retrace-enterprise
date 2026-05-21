#!/bin/bash
# ReTrace Development Startup Script — Lumena Technologies

set -e

# Ensure Homebrew is in PATH (macOS)
if [ -f "/opt/homebrew/bin/brew" ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "🚀 Starting ReTrace Development Environment"
echo "================================================"

# Check prerequisites
check_command() {
    if ! command -v $1 &> /dev/null; then
        echo "❌ $1 is required but not installed."
        exit 1
    fi
}

install_go() {
    if ! command -v go &> /dev/null; then
        echo "📦 Go is not installed. Installing via Homebrew..."
        
        if ! command -v brew &> /dev/null; then
            echo "❌ Homebrew is required to install Go. Installing Homebrew first..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            
            # Add Homebrew to PATH for Apple Silicon Macs
            if [ -f "/opt/homebrew/bin/brew" ]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            fi
        fi
        
        brew install go
        echo "✅ Go installed successfully"
    fi
}

# Check/install Python 3.11 or 3.12 (required for package compatibility)
check_python() {
    # Prefer python3.11 or python3.12 specifically
    if command -v python3.11 &> /dev/null; then
        export PYTHON_CMD="python3.11"
        return 0
    elif command -v python3.12 &> /dev/null; then
        export PYTHON_CMD="python3.12"
        return 0
    fi
    
    # Check if python3 exists and is 3.11 or 3.12 (not 3.13+)
    if command -v python3 &> /dev/null; then
        PYTHON_VER=$(python3 --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
        MAJOR=$(echo $PYTHON_VER | cut -d. -f1)
        MINOR=$(echo $PYTHON_VER | cut -d. -f2)
        
        if [ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 11 ] && [ "$MINOR" -le 12 ]; then
            export PYTHON_CMD="python3"
            return 0
        elif [ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 13 ]; then
            echo "⚠️  Python 3.13+ detected. Packages require Python 3.11 or 3.12."
        fi
    fi
    
    echo "📦 Python 3.11 or 3.12 not found. Installing Python 3.11 via Homebrew..."
    
    if ! command -v brew &> /dev/null; then
        echo "❌ Homebrew is required. Installing Homebrew first..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        if [ -f "/opt/homebrew/bin/brew" ]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        fi
    fi
    
    brew install python@3.11
    export PYTHON_CMD="python3.11"
    echo "✅ Python 3.11 installed"
}

check_python
install_node() {
    if ! command -v node &> /dev/null; then
        echo "📦 Node.js is not installed. Installing via Homebrew..."
        if ! command -v brew &> /dev/null; then
            echo "❌ Homebrew is required to install Node.js. Installing Homebrew first..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            if [ -f "/opt/homebrew/bin/brew" ]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            fi
        fi
        brew install node
        echo "✅ Node.js installed successfully"
    fi
}

install_node

install_bun() {
    if ! command -v bun &> /dev/null; then
        echo "📦 bun is not installed. Installing..."
        curl -fsSL https://bun.sh/install | bash
        # Pick up bun in the current shell — bun installs to ~/.bun/bin by default.
        export PATH="$HOME/.bun/bin:$PATH"
        if ! command -v bun &> /dev/null; then
            echo "❌ bun install completed but bun is not on PATH. Add ~/.bun/bin to PATH and retry."
            exit 1
        fi
        echo "✅ bun installed successfully"
    fi
}

install_bun

# Install uv (required for MCP Builder dependency management)
if ! command -v uv &> /dev/null && [ ! -f "$HOME/.local/bin/uv" ]; then
    echo "📦 Installing uv (Python package manager for MCP Builder)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "✅ uv installed successfully"
fi

install_go

# Install and setup Podman (for sandbox container isolation)
setup_podman() {
    echo ""
    echo "🐳 Setting up Podman (sandbox container runtime)..."

    # Install Podman if not present
    if ! command -v podman &> /dev/null; then
        echo "📦 Installing Podman..."
        if ! command -v brew &> /dev/null; then
            echo "❌ Homebrew is required. Installing Homebrew first..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            if [ -f "/opt/homebrew/bin/brew" ]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            fi
        fi
        brew install podman
        echo "✅ Podman installed"
    else
        echo "✅ Podman already installed ($(podman --version))"
    fi

    # Detect chip type for optimal VM provider
    ARCH=$(uname -m)
    if [ "$ARCH" = "arm64" ]; then
        VM_PROVIDER="libkrun"   # Apple Silicon — no SSH, stable
        # Install krunkit if needed (required for libkrun)
        if ! command -v krunkit &> /dev/null; then
            echo "📦 Installing krunkit (required for libkrun)..."
            brew tap slp/krunkit
            brew install krunkit
            echo "✅ krunkit installed"
        fi
        export CONTAINERS_MACHINE_PROVIDER=libkrun
    else
        VM_PROVIDER="applehv"   # Intel — SSH-based, needs keep-alive
    fi

    # Initialize Podman machine if not exists
    if ! podman machine inspect podman-machine-default &> /dev/null; then
        echo "📦 Initializing Podman machine (Linux VM, provider: $VM_PROVIDER)..."
        podman machine init
        echo "✅ Podman machine initialized"
    fi

    # Ensure machine is running and responsive
    echo "🔄 Ensuring Podman machine is ready..."
    MACHINE_STATE=$(podman machine info --format "{{.Host.MachineState}}" 2>/dev/null || echo "Unknown")
    if [ "$MACHINE_STATE" = "Running" ]; then
        # Verify connection actually works
        if podman info --format "{{.Host.RemoteSocket.Exists}}" &> /dev/null; then
            echo "✅ Podman machine running and responsive"
        else
            echo "⚠️  Podman machine connection stale. Restarting..."
            podman machine stop 2>/dev/null || true
            podman machine start
            echo "✅ Podman machine restarted"
        fi
    else
        echo "🔄 Starting Podman machine..."
        podman machine start
        echo "✅ Podman machine started"
    fi

    # Set correct SSH key for Podman machine (prevents auth failures)
    export CONTAINER_SSHKEY=~/.local/share/containers/podman/machine/machine

    # Configure SSH keep-alive to prevent Podman connection drops (all Macs)
    if ! grep -q "ServerAliveInterval" ~/.ssh/config 2>/dev/null; then
        echo "🔧 Configuring SSH keep-alive for Podman stability..."
        mkdir -p ~/.ssh
        echo "" >> ~/.ssh/config
        echo "# Podman machine keep-alive (prevents SSH drops)" >> ~/.ssh/config
        echo "Host *" >> ~/.ssh/config
        echo "    ServerAliveInterval 30" >> ~/.ssh/config
        echo "    ServerAliveCountMax 5" >> ~/.ssh/config
        chmod 600 ~/.ssh/config
        echo "✅ SSH keep-alive configured"
    fi

    # Build sandbox image if not exists
    SANDBOX_DIR="$PROJECT_ROOT/main-app/sandbox"
    if [ -f "$SANDBOX_DIR/Dockerfile" ]; then
        if ! podman image exists retrace-sandbox:latest 2>/dev/null; then
            echo "🔨 Building sandbox container image (first time, may take 2-3 minutes)..."
            podman build -t retrace-sandbox:latest "$SANDBOX_DIR"
            echo "✅ Sandbox image built"
        else
            echo "✅ Sandbox image already exists"
        fi
    fi

    echo "✅ Podman sandbox ready"
}

setup_podman

# Build POD agents (pre-build for faster POD generation)
if command -v go &> /dev/null; then
    echo ""
    echo "🔨 Pre-building POD agents..."
    "$SCRIPT_DIR/build-pod-agents.sh" || echo "⚠️  POD build failed, will build on-demand"
fi

# Start Backend
echo ""
echo "📦 Setting up Backend..."
cd "$PROJECT_ROOT/main-app/backend"

# Use the Python version determined by check_python
if [ -z "$PYTHON_CMD" ]; then
    # Fallback if check_python didn't set it
    if command -v python3.11 &> /dev/null; then
        PYTHON_CMD="python3.11"
    elif command -v python3.12 &> /dev/null; then
        PYTHON_CMD="python3.12"
    else
        PYTHON_CMD="python3"
    fi
fi

PYTHON_VERSION=$($PYTHON_CMD --version 2>&1 | awk '{print $2}')
echo "Using $PYTHON_CMD (version $PYTHON_VERSION)"

# Verify Python version is 3.11 or 3.12
PYTHON_VER=$($PYTHON_CMD --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
MAJOR=$(echo $PYTHON_VER | cut -d. -f1)
MINOR=$(echo $PYTHON_VER | cut -d. -f2)

if [ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 13 ]; then
    echo "❌ ERROR: Python 3.13+ is not compatible with required packages."
    echo "Please install Python 3.11 or 3.12: brew install python@3.11"
    exit 1
fi

# Check if venv exists and was created with correct Python version
if [ -d "venv" ]; then
    VENV_PYTHON=$(venv/bin/python --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
    VENV_MAJOR=$(echo $VENV_PYTHON | cut -d. -f1)
    VENV_MINOR=$(echo $VENV_PYTHON | cut -d. -f2)
    
    # If venv was created with Python 3.13+, recreate it
    if [ "$VENV_MAJOR" -eq 3 ] && [ "$VENV_MINOR" -ge 13 ]; then
        echo "⚠️  Existing venv uses Python 3.13+. Removing and recreating with Python 3.11/3.12..."
        rm -rf venv
    fi
fi

if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    $PYTHON_CMD -m venv venv
fi

source venv/bin/activate

# Verify venv Python version
VENV_PYTHON_VER=$(python --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
echo "Virtual environment Python: $VENV_PYTHON_VER"

# Upgrade pip first
pip install --upgrade pip setuptools wheel

# Install requirements
echo "Installing Python dependencies..."
pip install -q -r requirements.txt

# Install Playwright browser binaries (Chromium) if not present
echo "Installing Playwright browser binaries..."
if ! python -m playwright install --dry-run 2>&1 | grep -q "already installed"; then
    python -m playwright install chromium
else
    echo "✅ Playwright Chromium already installed"
fi

# Install local ServiceNow MCP server package
echo "Installing ServiceNow MCP server..."
pip install -q -e "$PROJECT_ROOT/servicenow-mcp-server/"

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "⚠️  Created .env file. Please configure your API keys!"
fi

# Kill any existing backend on port 8000
echo "Checking for existing backend processes..."
if lsof -ti :8000 &> /dev/null; then
    echo "⚠️  Port 8000 is in use. Killing existing processes..."
    lsof -ti :8000 | xargs kill -9 2>/dev/null || true
    sleep 1
fi

echo "Starting FastAPI backend..."
# External-OC mode: tell the backend to route HTTP to the OC source we
# launch below on :4096 instead of trying to spawn an `opencode` binary.
export RETRACE_OPENCODE_EXTERNAL_URL="${RETRACE_OPENCODE_EXTERNAL_URL:-http://127.0.0.1:4096}"
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!
sleep 2  # Give backend time to start
echo "Backend started (PID: $BACKEND_PID)"

# Start Frontend
echo ""
echo "🎨 Setting up Frontend..."
cd "$PROJECT_ROOT/main-app/frontend"

if [ ! -d "node_modules" ]; then
    echo "Installing npm dependencies..."
    npm install
fi

echo "Starting Vite dev server..."
npm run dev &
FRONTEND_PID=$!
echo "Frontend started (PID: $FRONTEND_PID)"

# Start OpenCode (the agent runtime, port 4096). The forked source lives at
# $PROJECT_ROOT/opencode and is run via bun directly — no build step. Edits
# to opencode/.../src/*.ts get picked up on the next start.
#
# `run-for-retrace.sh` auto-detects user_id from ~/.config/opencode/projects/
# and sets OPENCODE_CONFIG_DIR + RETRACE_USER_ID/BACKEND_URL/INTERNAL_TOKEN.
# Skips OC startup gracefully if no user config exists yet (first-run case
# where the user hasn't visited /agent yet to trigger generate_config).
OC_PID=""
echo ""
echo "🤖 Starting OpenCode agent runtime..."

# Install OpenCode dependencies if needed
OC_PKG_DIR="$PROJECT_ROOT/opencode/packages/opencode"
if [ -d "$OC_PKG_DIR" ] && [ ! -d "$OC_PKG_DIR/node_modules" ]; then
    echo "📦 Installing OpenCode dependencies (first time)..."
    export PATH="$HOME/.bun/bin:$PATH"
    bun install --cwd "$OC_PKG_DIR"
fi

if [ -d "$HOME/.config/opencode/projects" ] && [ -n "$(ls -A "$HOME/.config/opencode/projects" 2>/dev/null)" ]; then
    export PATH="$HOME/.bun/bin:$PATH"
    "$PROJECT_ROOT/opencode/run-for-retrace.sh" &
    OC_PID=$!
    echo "OpenCode started (PID: $OC_PID, port: 4096)"
else
    echo "⚠️  No OpenCode user config found yet. Hit /agent once in the UI to"
    echo "   generate one, then run: $PROJECT_ROOT/opencode/run-for-retrace.sh"
fi

# Cleanup function
cleanup() {
    echo ""
    echo "🛑 Shutting down..."
    kill $BACKEND_PID 2>/dev/null
    kill $FRONTEND_PID 2>/dev/null
    [ -n "$OC_PID" ] && kill $OC_PID 2>/dev/null
    exit 0
}

trap cleanup SIGINT SIGTERM

echo ""
echo "================================================"
echo "✅ ReTrace Development Environment Running!"
echo ""
echo "  Backend:   http://localhost:8000"
echo "  API Docs:  http://localhost:8000/docs"
echo "  Frontend:  http://localhost:5173"
if [ -n "$OC_PID" ]; then
echo "  OpenCode:  http://127.0.0.1:4096"
fi
echo ""
echo "Press Ctrl+C to stop all services"
echo "================================================"

# Wait for processes
wait
