#!/bin/sh
# Container entrypoint — starts the browser daemon eagerly so the very
# first browser action from the host doesn't pay a 2-3s Playwright +
# Chromium cold-start cost. Then keeps the container alive.

# Start the browser daemon in the background. It eagerly boots Chromium
# and connects Playwright before returning from its own startup, so the
# daemon is usually ready within a few seconds of container boot.
python3 /opt/browser_daemon.py > /tmp/browser_daemon.log 2>&1 &
echo $! > /tmp/browser_daemon.pid

# Keep the container alive forever (the previous behaviour).
exec tail -f /dev/null
