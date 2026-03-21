#!/bin/bash
# ReTrace Development Kill Script — Lumena Technologies
# Stops all processes started by start-dev.sh

echo "Stopping ReTrace Development Environment..."

# Backend (uvicorn on port 8000)
if lsof -ti :8000 &> /dev/null; then
    echo "  Killing backend (port 8000)..."
    lsof -ti :8000 | xargs kill -9 2>/dev/null || true
fi

# WebSocket server (port 8001)
if lsof -ti :8001 &> /dev/null; then
    echo "  Killing WebSocket server (port 8001)..."
    lsof -ti :8001 | xargs kill -9 2>/dev/null || true
fi

# Frontend (vite on port 5173)
if lsof -ti :5173 &> /dev/null; then
    echo "  Killing frontend (port 5173)..."
    lsof -ti :5173 | xargs kill -9 2>/dev/null || true
fi

# Catch any stragglers
pkill -9 -f "uvicorn app.main:app" 2>/dev/null || true
pkill -9 -f "npm run dev" 2>/dev/null || true

sleep 1

# Verify
REMAINING=$(lsof -ti :8000,:5173,:8001 2>/dev/null | wc -l | tr -d ' ')
if [ "$REMAINING" = "0" ]; then
    echo "All ReTrace processes stopped."
else
    echo "Warning: $REMAINING process(es) still running on ports 8000/5173/8001"
fi
