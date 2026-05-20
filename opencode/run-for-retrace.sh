#!/bin/bash
# Run this OC fork's source for retrace to use in EXTERNAL mode.
#
# Workflow:
#   1. Make sure retrace's backend is started with:
#        export RETRACE_OPENCODE_EXTERNAL_URL=http://127.0.0.1:4096
#      (and `unset RETRACE_OPENCODE_BIN` so retrace doesn't try to spawn its own)
#   2. Hit /agent once so retrace generates the per-user opencode.json
#      (you'll see the path in retrace's backend log).
#   3. Run this script in a fresh terminal, passing your user_id:
#        ./run-for-retrace.sh <user-id>
#   4. Edit OC source freely. Restart with Ctrl+C + re-run when you want
#      your edits picked up.
#
# Auto-detects user_id if there's exactly one ~/.config/opencode/projects/<id>/
# directory.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECTS_DIR="$HOME/.config/opencode/projects"
PORT="${RETRACE_OC_PORT:-4096}"

# Pick the config dir
USER_ID="${1:-}"
if [ -z "$USER_ID" ]; then
  if [ -d "$PROJECTS_DIR" ]; then
    USERS=("$PROJECTS_DIR"/*/)
    if [ ${#USERS[@]} -eq 1 ] && [ "${USERS[0]}" != "$PROJECTS_DIR/*/" ]; then
      USER_ID="$(basename "${USERS[0]}")"
      echo "→ Auto-detected user_id: $USER_ID"
    elif [ ${#USERS[@]} -gt 1 ]; then
      echo "Multiple user configs found in $PROJECTS_DIR/:"
      for u in "${USERS[@]}"; do
        echo "    $(basename "$u")"
      done
      echo "Usage: $0 <user-id>"
      exit 1
    else
      echo "No user configs found in $PROJECTS_DIR/."
      echo "Hit /agent in retrace first to generate one, then re-run."
      exit 1
    fi
  else
    echo "No $PROJECTS_DIR directory found. Has retrace ever started OC for this user?"
    exit 1
  fi
fi

CONFIG_DIR="$PROJECTS_DIR/$USER_ID"
if [ ! -f "$CONFIG_DIR/opencode.json" ]; then
  echo "No opencode.json at $CONFIG_DIR. Hit /agent in retrace first to generate it."
  exit 1
fi

echo "→ Starting OC from source"
echo "    config dir: $CONFIG_DIR"
echo "    port:       $PORT"
echo "    source:     $SCRIPT_DIR/packages/opencode"
echo
echo "Tip: ^C to stop. Edits to *.ts files take effect on next restart."
echo

# Retrace's forked shell.ts in OC routes bash through the PTY bridge using
# these env vars. Without them OC's shell tool returns a config error.
export OPENCODE_CONFIG_DIR="$CONFIG_DIR"
export RETRACE_USER_ID="$USER_ID"
export RETRACE_BACKEND_URL="${RETRACE_BACKEND_URL:-http://127.0.0.1:8000}"
if [ -z "$RETRACE_INTERNAL_TOKEN" ] && [ -f "$HOME/.local/share/retrace/internal-token" ]; then
  export RETRACE_INTERNAL_TOKEN="$(cat "$HOME/.local/share/retrace/internal-token")"
fi
if [ -z "$RETRACE_INTERNAL_TOKEN" ]; then
  echo "WARN: RETRACE_INTERNAL_TOKEN is not set; shell tool will fail."
  echo "       Backend should write it to ~/.local/share/retrace/internal-token at startup."
fi

exec bun run --cwd "$SCRIPT_DIR/packages/opencode" --conditions=browser \
  src/index.ts serve --port "$PORT" --hostname 127.0.0.1
