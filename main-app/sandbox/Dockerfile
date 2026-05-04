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

# Browser automation: long-lived daemon + thin client wrapper.
# browser_daemon.py keeps Playwright connected across requests.
# browser_control.py is the per-action entry point called by the host.
COPY browser_daemon.py /opt/browser_daemon.py
COPY browser_control.py /opt/browser_control.py
COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

# Set HOME to /workspace so ~/file.txt goes to the persistent volume
ENV HOME=/workspace
WORKDIR /workspace

# Start the browser daemon eagerly on boot, then keep the container alive.
CMD ["/opt/entrypoint.sh"]
