#!/usr/bin/env bash
set -euo pipefail

#############################################################################
# build-dmg.sh — Build KnowledgePod macOS .dmg
#
# Usage:  ./scripts/build-dmg.sh
# Output: main-app/frontend/dist-electron/KnowledgePod-*.dmg
#############################################################################

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/main-app/frontend"
BACKEND_DIR="$ROOT_DIR/main-app/backend"
BUNDLE_DIR="$FRONTEND_DIR/backend-bundle"

echo "======================================"
echo "  KnowledgePod DMG Builder"
echo "======================================"
echo ""

# ── 0. Prerequisites ───────────────────────────────────────────────────────

echo "[1/6] Checking prerequisites..."

if ! command -v node &>/dev/null; then
  echo "ERROR: Node.js is required. Install via: brew install node"
  exit 1
fi

if ! command -v npm &>/dev/null; then
  echo "ERROR: npm is required."
  exit 1
fi

# Find Python 3.11 or 3.12
PYTHON=""
for candidate in python3.12 python3.11 python3; do
  if command -v "$candidate" &>/dev/null; then
    ver=$("$candidate" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if [[ "$major" == "3" ]] && [[ "$minor" == "11" || "$minor" == "12" ]]; then
      PYTHON="$candidate"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  echo "ERROR: Python 3.11 or 3.12 is required."
  echo "  Install via: brew install python@3.12"
  exit 1
fi

echo "  Node: $(node --version)"
echo "  npm:  $(npm --version)"
echo "  Python: $($PYTHON --version)"
echo ""

# ── 1. Build React frontend ───────────────────────────────────────────────

echo "[2/6] Building React frontend..."
cd "$FRONTEND_DIR"
npm install --no-audit --no-fund 2>&1 | tail -1
npx vite build
echo "  Frontend built -> $FRONTEND_DIR/dist/"
echo ""

# ── 2. Generate macOS app icon (.icns) ─────────────────────────────────────

echo "[3/6] Generating app icon..."
ICON_SVG="$FRONTEND_DIR/public/icon.svg"
ICON_ICNS="$FRONTEND_DIR/public/icon.icns"
ICONSET_DIR="$FRONTEND_DIR/public/icon.iconset"

if [[ -f "$ICON_SVG" ]]; then
  mkdir -p "$ICONSET_DIR"

  # Check for rsvg-convert (better SVG rendering) or fall back to sips
  if command -v rsvg-convert &>/dev/null; then
    rsvg-convert -w 1024 -h 1024 "$ICON_SVG" -o "$ICONSET_DIR/icon_1024.png"
  elif command -v cairosvg &>/dev/null; then
    cairosvg "$ICON_SVG" -o "$ICONSET_DIR/icon_1024.png" -W 1024 -H 1024
  else
    # Create a simple PNG from the SVG using Python + the SVG itself
    # sips can't read SVG, so we create a basic PNG with Python
    $PYTHON -c "
import struct, zlib, base64, os

# Render a simple 1024x1024 gradient circle as PNG (matching the SVG colors)
W = H = 1024
pixels = bytearray()
for y in range(H):
    pixels.append(0)  # filter byte
    for x in range(W):
        cx, cy = W // 2, H // 2
        dx, dy = x - cx, y - cy
        dist = (dx*dx + dy*dy) ** 0.5
        r_outer = W * 0.45
        if dist <= r_outer:
            t = (x + y) / (W + H)
            r = int(0x88 + (0x5e - 0x88) * t)
            g = int(0xc0 + (0x81 - 0xc0) * t)
            b = int(0xd0 + (0xac - 0xd0) * t)
            a = 255
        else:
            r, g, b, a = 0, 0, 0, 0
        pixels.extend([r, g, b, a])

def write_png(path, width, height, data):
    def chunk(ctype, d):
        c = ctype + d
        return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    sig = b'\\x89PNG\\r\\n\\x1a\\n'
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    raw = zlib.compress(bytes(data), 9)
    idat = chunk(b'IDAT', raw)
    iend = chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(sig + ihdr + idat + iend)

write_png('$ICONSET_DIR/icon_1024.png', W, H, pixels)
"
  fi

  # Generate all required sizes from the 1024px source
  BASE_PNG="$ICONSET_DIR/icon_1024.png"
  if [[ -f "$BASE_PNG" ]]; then
    for size in 16 32 128 256 512; do
      sips -z $size $size "$BASE_PNG" --out "$ICONSET_DIR/icon_${size}x${size}.png" &>/dev/null
      double=$((size * 2))
      sips -z $double $double "$BASE_PNG" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" &>/dev/null
    done
    cp "$BASE_PNG" "$ICONSET_DIR/icon_512x512@2x.png"
    rm -f "$BASE_PNG"

    iconutil -c icns "$ICONSET_DIR" -o "$ICON_ICNS" 2>/dev/null || true
    rm -rf "$ICONSET_DIR"
  fi

  if [[ -f "$ICON_ICNS" ]]; then
    echo "  Icon generated -> $ICON_ICNS"
  else
    echo "  WARNING: Could not generate .icns icon (DMG will use default icon)"
  fi
else
  echo "  WARNING: No icon.svg found in public/"
fi
echo ""

# ── 3. Bundle Python backend + venv ────────────────────────────────────────

echo "[4/6] Bundling Python backend..."

rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR"

# Copy backend source code (exclude dev files, db, caches)
rsync -a \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='knowledgepod.db*' \
  --exclude='venv' \
  --exclude='screenops_screenshots' \
  --exclude='screenops_debug.log' \
  --exclude='directory_tool.py' \
  --exclude='*.egg-info' \
  "$BACKEND_DIR/" "$BUNDLE_DIR/"

# Create a fresh venv inside the bundle
echo "  Creating Python venv..."
$PYTHON -m venv "$BUNDLE_DIR/venv"
source "$BUNDLE_DIR/venv/bin/activate"

echo "  Installing dependencies (this may take a few minutes)..."
pip install --upgrade pip setuptools wheel -q
pip install -r "$BUNDLE_DIR/requirements.txt" -q

# pywebview not needed for Electron approach, but ensure uvicorn is there
pip install uvicorn[standard] -q 2>/dev/null || true

deactivate

# Make venv paths relative-friendly by fixing the shebang in activate scripts
# (Not strictly necessary since we use the full venv/bin/python path)

echo "  Backend bundled -> $BUNDLE_DIR/"
BUNDLE_SIZE=$(du -sh "$BUNDLE_DIR" | cut -f1)
echo "  Bundle size: $BUNDLE_SIZE"
echo ""

# ── 4. Copy built frontend into backend bundle for static serving ──────────

echo "[5/6] Copying frontend dist into backend bundle..."
mkdir -p "$BUNDLE_DIR/frontend-dist"
cp -R "$FRONTEND_DIR/dist/"* "$BUNDLE_DIR/frontend-dist/"
echo "  Done"
echo ""

# ── 5. Build Electron app + DMG ────────────────────────────────────────────

echo "[6/6] Building Electron app and DMG..."
cd "$FRONTEND_DIR"

# electron-builder needs the frontend dist/ and electron/ dirs plus backend-bundle as extraResources
npx electron-builder --mac --publish=never

echo ""
echo "======================================"
echo "  BUILD COMPLETE"
echo "======================================"
echo ""
echo "DMG location:"
ls -la "$FRONTEND_DIR/dist-electron/"*.dmg 2>/dev/null || echo "  (check dist-electron/ for output)"
echo ""

# ── Cleanup (optional) ────────────────────────────────────────────────────

echo "To clean up the backend bundle: rm -rf $BUNDLE_DIR"
echo ""
