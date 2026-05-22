#!/bin/bash
# ReTrace Development Startup Script — Lumena Technologies

set -e

OS="$(uname -s)"
ARCH="$(uname -m)"

# Ensure Homebrew is in PATH (macOS)
if [ "$OS" = "Darwin" ] && [ -f "/opt/homebrew/bin/brew" ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
fi

# Prefer a previously-installed local Go (avoids re-downloading on every run)
if [ -d "$HOME/.local/go/bin" ]; then
    export PATH="$HOME/.local/go/bin:$PATH"
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

# pod-agent uses os.Root.ReadFile (added in Go 1.25), so 1.25+ is required.
REQUIRED_GO_MAJOR=1
REQUIRED_GO_MINOR=25
GO_INSTALL_VERSION="1.25.0"

go_version_ok() {
    command -v go &> /dev/null || return 1
    local ver major minor
    ver=$(go version 2>/dev/null | grep -oE 'go[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1 | sed 's/go//')
    [ -z "$ver" ] && return 1
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if [ "$major" -gt "$REQUIRED_GO_MAJOR" ]; then return 0; fi
    if [ "$major" -eq "$REQUIRED_GO_MAJOR" ] && [ "$minor" -ge "$REQUIRED_GO_MINOR" ]; then return 0; fi
    return 1
}

install_go() {
    if go_version_ok; then
        echo "✅ Go $(go version | awk '{print $3}') OK"
        return
    fi
    if command -v go &> /dev/null; then
        echo "ℹ️  Detected Go $(go version | awk '{print $3}'); need ${REQUIRED_GO_MAJOR}.${REQUIRED_GO_MINOR}+. Installing newer version..."
    else
        echo "📦 Go is not installed; installing ${GO_INSTALL_VERSION}..."
    fi

    if [ "$OS" = "Darwin" ]; then
        if ! command -v brew &> /dev/null; then
            echo "❌ Homebrew is required to install Go on macOS. Installing Homebrew first..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            [ -f "/opt/homebrew/bin/brew" ] && eval "$(/opt/homebrew/bin/brew shellenv)"
        fi
        brew install go
        echo "✅ Go installed successfully"
        return
    fi

    # Linux: download official Go tarball into ~/.local/go (no sudo, no apt — apt is too old)
    local arch_label="amd64"
    case "$ARCH" in
        aarch64|arm64) arch_label="arm64" ;;
    esac
    local tarball="go${GO_INSTALL_VERSION}.linux-${arch_label}.tar.gz"
    local url="https://go.dev/dl/${tarball}"
    local install_dir="$HOME/.local/go"
    echo "  Downloading $url ..."
    mkdir -p "$HOME/.local"
    rm -rf "$install_dir"
    curl -fsSL "$url" -o "/tmp/$tarball"
    tar -C "$HOME/.local" -xzf "/tmp/$tarball"
    rm -f "/tmp/$tarball"
    export PATH="$install_dir/bin:$PATH"
    if go_version_ok; then
        echo "✅ Go $(go version | awk '{print $3}') installed at $install_dir"
    else
        echo "❌ Go installation failed"; exit 1
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

# OpenCode's tree-sitter and node-pty native modules compile C/C++ at install time.
# macOS has clang via Xcode CLT; Debian/Ubuntu needs build-essential explicitly.
install_build_tools_linux() {
    [ "$OS" = "Darwin" ] && return 0
    if command -v g++ &> /dev/null && command -v make &> /dev/null; then return 0; fi
    echo "📦 Installing build-essential (g++/make required by OpenCode native modules)..."
    if command -v apt-get &> /dev/null; then
        sudo apt-get update -y && sudo apt-get install -y build-essential
    elif command -v dnf &> /dev/null; then
        sudo dnf groupinstall -y "Development Tools"
    elif command -v pacman &> /dev/null; then
        sudo pacman -Sy --noconfirm base-devel
    else
        echo "⚠️  No supported package manager found; install a C/C++ toolchain manually."
        return 1
    fi
    echo "✅ build-essential installed"
}

install_build_tools_linux

# pyautogui (via mouseinfo) imports tkinter at module load. On Debian/Ubuntu
# Python is built without _tkinter unless python3-tk is installed, and the
# import calls sys.exit() — which leaks past the screenops try/except guard
# and crashes vision_click. Install the system package so the full screenops
# path works; the in-code guard now also catches SystemExit as a safety net.
install_python_tk_linux() {
    [ "$OS" = "Darwin" ] && return 0
    [ -z "$PYTHON_CMD" ] && return 0
    if $PYTHON_CMD -c "import tkinter" &> /dev/null; then return 0; fi
    echo "📦 Installing python3-tk (required by pyautogui/mouseinfo for screenops)..."
    if command -v apt-get &> /dev/null; then
        sudo apt-get update -y && sudo apt-get install -y python3-tk python3-dev
    elif command -v dnf &> /dev/null; then
        sudo dnf install -y python3-tkinter python3-devel
    elif command -v pacman &> /dev/null; then
        sudo pacman -Sy --noconfirm tk
    else
        echo "⚠️  No supported package manager found; install python3-tk manually if you need pyautogui screenops."
        return 0
    fi
    if $PYTHON_CMD -c "import tkinter" &> /dev/null; then
        echo "✅ python3-tk installed"
    else
        echo "⚠️  python3-tk install attempted but 'import tkinter' still fails. pyautogui screenops will be disabled (vision_click via Playwright still works)."
    fi
}

install_python_tk_linux

# Install uv (required for MCP Builder dependency management)
if ! command -v uv &> /dev/null && [ ! -f "$HOME/.local/bin/uv" ]; then
    echo "📦 Installing uv (Python package manager for MCP Builder)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "✅ uv installed successfully"
fi

install_go

setup_podman_macos() {
    if ! command -v podman &> /dev/null; then
        echo "📦 Installing Podman..."
        if ! command -v brew &> /dev/null; then
            echo "❌ Homebrew is required. Installing Homebrew first..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            [ -f "/opt/homebrew/bin/brew" ] && eval "$(/opt/homebrew/bin/brew shellenv)"
        fi
        brew install podman
        echo "✅ Podman installed"
    else
        echo "✅ Podman already installed ($(podman --version))"
    fi

    local vm_provider="applehv"
    if [ "$ARCH" = "arm64" ]; then
        vm_provider="libkrun"
        if ! command -v krunkit &> /dev/null; then
            echo "📦 Installing krunkit (required for libkrun)..."
            brew tap slp/krunkit && brew install krunkit
        fi
        export CONTAINERS_MACHINE_PROVIDER=libkrun
    fi

    if ! podman machine inspect podman-machine-default &> /dev/null; then
        echo "📦 Initializing Podman machine (provider: $vm_provider)..."
        podman machine init
    fi

    local machine_state
    machine_state=$(podman machine info --format "{{.Host.MachineState}}" 2>/dev/null || echo "Unknown")
    if [ "$machine_state" = "Running" ]; then
        if podman info --format "{{.Host.RemoteSocket.Exists}}" &> /dev/null; then
            echo "✅ Podman machine running and responsive"
        else
            echo "⚠️  Podman machine connection stale. Restarting..."
            podman machine stop 2>/dev/null || true
            podman machine start
        fi
    else
        echo "🔄 Starting Podman machine..."
        podman machine start
    fi

    export CONTAINER_SSHKEY=~/.local/share/containers/podman/machine/machine

    if ! grep -q "ServerAliveInterval" ~/.ssh/config 2>/dev/null; then
        echo "🔧 Configuring SSH keep-alive for Podman stability..."
        mkdir -p ~/.ssh
        {
            echo ""
            echo "# Podman machine keep-alive (prevents SSH drops)"
            echo "Host *"
            echo "    ServerAliveInterval 30"
            echo "    ServerAliveCountMax 5"
        } >> ~/.ssh/config
        chmod 600 ~/.ssh/config
    fi
}

setup_podman_linux() {
    if ! command -v podman &> /dev/null; then
        echo "📦 Installing Podman via apt..."
        sudo apt-get update -y && sudo apt-get install -y podman
    else
        echo "✅ Podman already installed ($(podman --version))"
    fi

    # The backend shells out to `docker` for sandbox lifecycle. podman-docker
    # provides a /usr/bin/docker shim that forwards to podman. On macOS the
    # user typically has Docker Desktop or `podman` aliased, so this is
    # Linux-specific.
    if ! command -v docker &> /dev/null; then
        echo "📦 Installing podman-docker (provides 'docker' shim → podman)..."
        if command -v apt-get &> /dev/null; then
            sudo apt-get install -y podman-docker
        elif command -v dnf &> /dev/null; then
            sudo dnf install -y podman-docker
        fi
        if command -v docker &> /dev/null; then
            echo "✅ 'docker' shim installed (forwards to podman)"
        else
            echo "⚠️  Could not install podman-docker; backend sandbox features may fail"
        fi
    fi

    # Linux podman is rootless and runs natively — no VM, no SSH, no krunkit.
    if podman info &> /dev/null; then
        echo "✅ Podman responsive (native, no VM needed)"
    else
        echo "⚠️  'podman info' failed. Sandbox containers may not work."
        echo "    Try: systemctl --user enable --now podman.socket"
    fi
}

# Install and setup Podman (for sandbox container isolation)
setup_podman() {
    echo ""
    echo "🐳 Setting up Podman (sandbox container runtime)..."

    if [ "$OS" = "Darwin" ]; then
        setup_podman_macos
    else
        setup_podman_linux
    fi

    # Build sandbox image if not exists (same on both OSes)
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

# Install Playwright browser binaries. The backend uses chrome-headless-shell;
# the previous check only inspected the expected path string and skipped install
# even when the binary was absent. Verify on disk before skipping.
echo "Checking Playwright browser binaries..."
PLAYWRIGHT_OK=$(python - <<'PY' 2>/dev/null
import os
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    paths = [p.chromium.executable_path]
    # chrome-headless-shell lives in a sibling cache dir; the most reliable
    # check is whether *every* expected binary actually exists on disk.
    print("OK" if all(os.path.isfile(x) for x in paths) else "MISSING")
PY
)
if [ "$PLAYWRIGHT_OK" != "OK" ] || ! ls "$HOME/.cache/ms-playwright"/chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell 2>/dev/null | head -1 | grep -q .; then
    echo "📦 Installing Playwright browsers (chromium + chrome-headless-shell, first time, ~280 MB)..."
    python -m playwright install chromium chromium-headless-shell
    echo "✅ Playwright browsers installed"
else
    echo "✅ Playwright browsers already installed"
fi

# Install local ServiceNow MCP server package
echo "Installing ServiceNow MCP server..."
pip install -q -e "$PROJECT_ROOT/servicenow-mcp-server/"

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "⚠️  Created .env file. Please configure your API keys!"
fi

# Kill any existing dev processes on the ports we own. Without this, a stale
# OC on :4096 (or Vite on :5173) from a previous run survives and the newly
# spawned process fails to bind silently in the background — the frontend
# ends up talking to a half-dead old OC and the UI looks hung.
echo "Checking for existing dev processes on ports 8000 / 5173 / 4096..."
killed_any=0
for port in 8000 5173 4096; do
    if lsof -ti :$port &> /dev/null; then
        echo "⚠️  Port $port is in use. Killing existing processes..."
        lsof -ti :$port | xargs kill -9 2>/dev/null || true
        killed_any=1
    fi
done
[ "$killed_any" = "1" ] && sleep 1

echo "Starting FastAPI backend..."
# By default we run in **external mode**: we launch the OC source from
# `opencode/packages/opencode` on :4096 via `bun run` (below) and point the
# backend at it. This gets you hot-reload of OC TypeScript edits, which is
# what daily development needs. Trade-off: every logged-in user shares the
# one OC process, so multi-user testing is broken in this mode.
#
# Set RETRACE_USE_MANAGED_OC=1 to flip into managed mode: the backend spawns
# its own `opencode` binary per logged-in user (using the patched ReTrace
# fork at opencode/packages/opencode/dist/...). Required for multi-user
# testing and matches production behavior. Loses hot-reload of OC source.
if [ "${RETRACE_USE_MANAGED_OC:-}" = "1" ]; then
    unset RETRACE_OPENCODE_EXTERNAL_URL
    # Point at the patched ReTrace fork binary for the current platform,
    # falling back to whatever `opencode` is on PATH if the fork hasn't
    # been built yet. The fork's build.ts produces per-platform output dirs.
    case "$(uname -s)-$(uname -m)" in
        Darwin-arm64) PATCHED_OC_BIN="$PROJECT_ROOT/opencode/packages/opencode/dist/opencode-darwin-arm64/bin/opencode" ;;
        Darwin-x86_64) PATCHED_OC_BIN="$PROJECT_ROOT/opencode/packages/opencode/dist/opencode-darwin-x64/bin/opencode" ;;
        Linux-aarch64|Linux-arm64) PATCHED_OC_BIN="$PROJECT_ROOT/opencode/packages/opencode/dist/opencode-linux-arm64/bin/opencode" ;;
        Linux-x86_64) PATCHED_OC_BIN="$PROJECT_ROOT/opencode/packages/opencode/dist/opencode-linux-x64/bin/opencode" ;;
        *) PATCHED_OC_BIN="" ;;
    esac
    if [ -n "$PATCHED_OC_BIN" ] && [ -x "$PATCHED_OC_BIN" ]; then
        export RETRACE_OPENCODE_BIN="$PATCHED_OC_BIN"
        echo "→ Managed OC mode (using patched fork: $PATCHED_OC_BIN)"
    else
        echo "→ Managed OC mode (patched binary not built; will fall back to \`opencode\` on PATH)"
        echo "   Build with: cd opencode/packages/opencode && bun run build"
    fi
else
    export RETRACE_OPENCODE_EXTERNAL_URL="${RETRACE_OPENCODE_EXTERNAL_URL:-http://127.0.0.1:4096}"
    echo "→ External OC mode (default — hot-reload of OC source, single-user only)"
fi
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

# OpenCode startup — only spawn it manually in external mode (default).
# Managed mode handles OC lifecycle in the backend, so we skip this block.
OC_PID=""
if [ "${RETRACE_USE_MANAGED_OC:-}" != "1" ]; then
    echo ""
    echo "🤖 Starting OpenCode agent runtime (external mode)..."

    # Install OpenCode dependencies if needed
    OC_PKG_DIR="$PROJECT_ROOT/opencode/packages/opencode"
    if [ -d "$OC_PKG_DIR" ] && [ ! -d "$OC_PKG_DIR/node_modules" ]; then
        echo "📦 Installing OpenCode dependencies (first time)..."
        export PATH="$HOME/.bun/bin:$PATH"
        bun install --cwd "$OC_PKG_DIR"
    fi

    # Determine which user's OC config to launch. Precedence:
    #   1. $RETRACE_OC_USER_ID         — explicit override.
    #   2. Single user dir             — auto-pick.
    #   3. Multiple dirs               — pick the most-recently-modified
    #                                    (the user who last hit /agent).
    # Without this, multi-account dev boxes hit run-for-retrace.sh's
    # "Usage: ... <user-id>" exit because it refuses to guess.
    OC_PROJECTS_DIR="$HOME/.config/opencode/projects"
    OC_USER_ID="${RETRACE_OC_USER_ID:-}"
    OC_USER_COUNT=0
    if [ -d "$OC_PROJECTS_DIR" ]; then
        OC_USER_COUNT=$(find "$OC_PROJECTS_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
    fi
    if [ -z "$OC_USER_ID" ] && [ "$OC_USER_COUNT" -gt 0 ]; then
        OC_USER_ID=$(ls -1td "$OC_PROJECTS_DIR"/*/ 2>/dev/null | head -1 | xargs -n1 basename)
    fi

    if [ -n "$OC_USER_ID" ]; then
        export PATH="$HOME/.bun/bin:$PATH"
        if [ "$OC_USER_COUNT" -gt 1 ] && [ -z "${RETRACE_OC_USER_ID:-}" ]; then
            echo "ℹ️  Multiple OpenCode user configs found; using most-recent: $OC_USER_ID"
            echo "    Override with: RETRACE_OC_USER_ID=<user-id> scripts/start-dev.sh"
        fi
        "$PROJECT_ROOT/opencode/run-for-retrace.sh" "$OC_USER_ID" &
        OC_PID=$!
        echo "OpenCode spawning (PID: $OC_PID, user: $OC_USER_ID, port: 4096) — waiting for HTTP listener..."

        # Cold `bun run src/index.ts` can sit silent for 10+ s before OC binds
        # :4096. The frontend's first /agent call during that window gets
        # ECONNREFUSED and the UI looks hung. Poll until OC speaks HTTP AND
        # has at least one MCP server connected — /doc proves the listener,
        # /mcp proves the retrace-* subprocesses (browser/search/kb/screenops)
        # have actually spawned and registered. Without the second check, the
        # Agent page paints with every MCP-backed tool chip dim until the user
        # hard-refreshes. Cap at ~30 s; if MCP is still warming up at the cap,
        # warn but continue — the frontend's refetchInterval picks up the slack.
        oc_ready=0
        oc_mcp_ready=0
        for i in $(seq 1 60); do
            if ! kill -0 "$OC_PID" 2>/dev/null; then
                echo "❌ OpenCode process exited during startup (PID $OC_PID gone). Check output above."
                break
            fi
            if [ "$oc_ready" = "0" ] && curl -sS -o /dev/null --max-time 1 http://127.0.0.1:4096/doc 2>/dev/null; then
                oc_ready=1
            fi
            if [ "$oc_ready" = "1" ]; then
                if curl -sS --max-time 1 http://127.0.0.1:4096/mcp 2>/dev/null | grep -q '"status":"connected"'; then
                    oc_mcp_ready=1
                    break
                fi
            fi
            sleep 0.5
        done
        if [ "$oc_mcp_ready" = "1" ]; then
            echo "✅ OpenCode HTTP listener + MCP servers ready on :4096"
        elif [ "$oc_ready" = "1" ]; then
            echo "⚠️  OpenCode HTTP up but MCP servers not yet connected within 30s. Tool chips will light up as they finish connecting."
        else
            echo "⚠️  OpenCode did not respond on :4096 within 30s. Continuing anyway — the UI may show 'agent unavailable' until it catches up."
        fi
    else
        echo "ℹ️  No OpenCode user config exists yet (first-time setup)."
        echo "    1. Backend + frontend are up — open http://localhost:5173 and sign in."
        echo "    2. Click 'Agent' in the sidebar. That generates"
        echo "       ~/.config/opencode/projects/<your-user-id>/opencode.json."
        echo "    3. Re-run scripts/start-dev.sh and OpenCode will come online."
        echo "    Alternatively, set RETRACE_USE_MANAGED_OC=1 to have the backend"
        echo "    spawn OC per-user on demand (no script re-run needed)."
    fi
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
