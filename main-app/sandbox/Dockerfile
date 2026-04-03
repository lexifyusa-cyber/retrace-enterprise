FROM python:3.11-slim

# System tools for terminal, grep, glob
RUN apt-get update && apt-get install -y \
    curl git ripgrep build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python packages for web tools
RUN pip install --no-cache-dir \
    httpx trafilatura beautifulsoup4 \
    duckduckgo-search requests

# For Chromium (auto_browser)
# Install to /opt/playwright so it's baked into the image (not in /workspace volume)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN pip install --no-cache-dir playwright \
    && playwright install --with-deps chromium

# For MCP servers (Node.js — npx comes bundled with npm)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs

# Set HOME to /workspace so ~/file.txt goes to the persistent volume
ENV HOME=/workspace
WORKDIR /workspace
