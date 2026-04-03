FROM python:3.11-slim

# System tools for terminal, grep, glob
RUN apt-get update && apt-get install -y \
    curl git ripgrep build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python packages for web tools
RUN pip install --no-cache-dir \
    httpx trafilatura beautifulsoup4 \
    ddgs requests

# Playwright + Chromium for auto_browser (inside container)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN pip install --no-cache-dir playwright \
    && playwright install --with-deps chromium

# Node.js for MCP servers (npx comes bundled with npm)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs

# Browser agent script (manages Chromium via Playwright)
COPY browser_agent.py /opt/browser_agent.py

# Set HOME to /workspace so ~/file.txt goes to the persistent volume
ENV HOME=/workspace
WORKDIR /workspace
