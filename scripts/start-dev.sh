#!/bin/bash
# ReTrace Development Startup Script — Lumena Technologies

set -e

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
check_command node
install_go

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

# Cleanup function
cleanup() {
    echo ""
    echo "🛑 Shutting down..."
    kill $BACKEND_PID 2>/dev/null
    kill $FRONTEND_PID 2>/dev/null
    exit 0
}

trap cleanup SIGINT SIGTERM

echo ""
echo "================================================"
echo "✅ ReTrace Development Environment Running!"
echo ""
echo "  Backend:  http://localhost:8000"
echo "  API Docs: http://localhost:8000/docs"
echo "  Frontend: http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop all services"
echo "================================================"

# Wait for processes
wait
