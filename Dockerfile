# syntax=docker/dockerfile:1
# ReTrace — Fly.io deployment image.
# Builds the React/Vite frontend, then bakes it into the FastAPI backend
# image which serves both the API and the SPA on a single origin (port 8000).

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
# Stage 2 — Python backend runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS backend

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System packages:
#  - tesseract-ocr / poppler-utils : optional OCR + PDF rasterisation
#  - xvfb + X libs + scrot          : let pyautogui import/run headless
#  - libmagic1                      : file type sniffing
#  - build-essential, curl, git     : building wheels / debugging
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      curl \
      git \
      libmagic1 \
      tesseract-ocr \
      poppler-utils \
      xvfb \
      scrot \
      python3-tk \
      libx11-6 libxext6 libxrandr2 libxtst6 libxi6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (layer caching).
COPY main-app/backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# Local ServiceNow MCP package (referenced at startup as `-m servicenow_mcp`).
# Non-fatal if its layout isn't pip-installable — the seeding step is guarded.
COPY servicenow-mcp-server/ /opt/servicenow-mcp-server/
RUN pip install /opt/servicenow-mcp-server/ || echo "servicenow-mcp-server install skipped"

# Backend source.
COPY main-app/backend/ /app/

# Built SPA -> /app/static (app.main serves it when this dir exists).
COPY --from=frontend /fe/dist/ /app/static/

EXPOSE 8000

# xvfb-run provides a virtual DISPLAY so GUI-automation imports don't crash on
# a headless VM. PORT defaults to 8000 (overridable via env / fly.toml).
CMD ["sh", "-c", "xvfb-run -a uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
