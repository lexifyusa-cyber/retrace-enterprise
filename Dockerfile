# syntax=docker/dockerfile:1
# ReTrace — Fly.io deployment image.
# Builds the React/Vite frontend, then bakes it into the FastAPI backend
# image which serves both the API and the SPA on a single origin (port 8000).
# Also bundles the vendored OpenCode fork (Bun) so the agent/chat feature can
# spawn an OpenCode server per user.

# ---------------------------------------------------------------------------
# Stage 1 — build the frontend (Vite -> /fe/dist)
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /fe

# Install deps first for better layer caching.
COPY main-app/frontend/package.json main-app/frontend/package-lock.json* ./
RUN npm ci

# Build. We invoke `vite build` directly (not `npm run build`, which also runs
# `tsc`) so a stray TypeScript error can't block a deploy — type-checking is a
# CI concern, not an image concern.
COPY main-app/frontend/ ./
RUN npx vite build

# ---------------------------------------------------------------------------
# Stage 2 — Python backend runtime (+ Bun-powered OpenCode)
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS backend

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BUN_INSTALL=/usr/local \
    PATH=/usr/local/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# System packages:
#  - tesseract-ocr / poppler-utils : optional OCR + PDF rasterisation
#  - xvfb + xauth + X libs + scrot  : let pyautogui import/run headless
#    (xvfb-run needs the xauth binary, or it exits before uvicorn starts)
#  - nodejs + build-essential       : compile OpenCode native modules
#    (tree-sitter, node-pty) during `bun install`
#  - unzip / ca-certificates        : required by the Bun installer
#  - libmagic1                      : file type sniffing
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      curl \
      git \
      unzip \
      libmagic1 \
      nodejs \
      tesseract-ocr \
      poppler-utils \
      xvfb \
      xauth \
      scrot \
      python3-tk \
      libx11-6 libxext6 libxrandr2 libxtst6 libxi6 \
    && rm -rf /var/lib/apt/lists/*

# Bun (installs to $BUN_INSTALL/bin -> /usr/local/bin/bun, already on PATH).
RUN curl -fsSL https://bun.sh/install | bash

WORKDIR /app

# ---- OpenCode fork (Bun) ---------------------------------------------------
# Copied + installed before the Python deps so backend-code edits don't bust
# this expensive layer. run-from-source.sh proxies `opencode serve ...` to
# `bun run src/index.ts ...`; the backend spawns it per user (managed mode)
# via RETRACE_OPENCODE_BIN (set in fly.toml).
COPY opencode/ /opt/opencode/
RUN cd /opt/opencode && bun install --frozen-lockfile \
    && chmod +x /opt/opencode/run-from-source.sh

# Python deps.
COPY main-app/backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# Playwright browser + OS libraries for the agent's headless-Chromium browser
# tool (app/services/browser_manager.py launches `pw.chromium`). `pip install
# playwright` ships only the Python package — the browser binary and its system
# libraries must be installed separately, or chromium.launch() fails at runtime
# (first "Executable doesn't exist", then missing libnss/libatk/libxkbcommon/...
# shared objects). `--with-deps` apt-installs those libs; the browser downloads
# into PLAYWRIGHT_BROWSERS_PATH (set above) so build + runtime agree on the path
# regardless of $HOME. apt-get update is needed because the lists were cleared
# in the earlier system-package layer.
RUN apt-get update \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Local ServiceNow MCP package (referenced at startup as `-m servicenow_mcp`).
# Non-fatal if its layout isn't pip-installable — the seeding step is guarded.
COPY servicenow-mcp-server/ /opt/servicenow-mcp-server/
RUN pip install /opt/servicenow-mcp-server/ || echo "servicenow-mcp-server install skipped"

# Backend source.
COPY main-app/backend/ /app/

# Built SPA -> /app/static (app.main serves it when this dir exists).
COPY --from=frontend /fe/dist/ /app/static/

# cloudflared — Cloudflare Tunnel client. Started by CMD only when CF_TUNNEL_TOKEN
# is set, so the image stays usable via the public IP / fly proxy without it.
RUN curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
      -o /usr/local/bin/cloudflared \
 && chmod +x /usr/local/bin/cloudflared

EXPOSE 8000

# xvfb-run provides a virtual DISPLAY so GUI-automation imports don't crash on
# a headless VM. cloudflared starts first (only if CF_TUNNEL_TOKEN is set) so
# the Cloudflare tunnel comes up alongside the app. PORT defaults to 8000.
CMD ["sh", "-c", "if [ -n \"$CF_TUNNEL_TOKEN\" ]; then cloudflared --no-autoupdate tunnel run --token \"$CF_TUNNEL_TOKEN\" & fi; xvfb-run -a uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
