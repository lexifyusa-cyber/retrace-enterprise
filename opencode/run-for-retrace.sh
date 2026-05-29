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

# Pick the config dir. When no user-id is passed and no config exists yet,
# wait for the backend to write one (first sign-in + /agent click) instead of
# bailing. start-dev.sh launches us in the background before any user has
# signed in, so the wait is what lets the whole dev environment come up on a
# single script run.
USER_ID="${1:-}"

config_dir_for() { echo "$PROJECTS_DIR/$1"; }

pick_user_id() {
  [ -d "$PROJECTS_DIR" ] || return 1
  local users=("$PROJECTS_DIR"/*/)
  [ "${users[0]}" = "$PROJECTS_DIR/*/" ] && return 1
  if [ ${#users[@]} -eq 1 ]; then
    basename "${users[0]}"
    return 0
  fi
  # Multiple configs: pick most-recently-modified (matches start-dev.sh's prior
  # behavior — the user who last hit /agent).
  basename "$(ls -1td "$PROJECTS_DIR"/*/ 2>/dev/null | head -1)"
}

if [ -z "$USER_ID" ]; then
  USER_ID="$(pick_user_id || true)"
  if [ -z "$USER_ID" ]; then
    echo "⏳ Waiting for OpenCode user config at $PROJECTS_DIR/<user-id>/"
    echo "   (sign in at http://localhost:5173 and click 'Agent' to create one)"
    while [ -z "$USER_ID" ]; do
      sleep 1
      USER_ID="$(pick_user_id || true)"
    done
    echo "→ Detected user_id: $USER_ID"
  else
    echo "→ Auto-detected user_id: $USER_ID"
  fi
fi

CONFIG_DIR="$(config_dir_for "$USER_ID")"
# The user-id directory exists but opencode.json may not be written yet
# (generate_config writes the dir + file together, but be defensive).
while [ ! -f "$CONFIG_DIR/opencode.json" ]; do
  sleep 1
done

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
# Enable OC's experimental background-subagents support so the agent can:
#   - launch a subagent with `task(background=true)` and get back a task_id
#     immediately (instead of blocking until completion);
#   - poll its status via `task_status(task_id, wait=false)`.
# Without this flag, monitoring/long-running tool calls have to be held
# open synchronously inside the agent — bad UX and burns LLM tokens
# waiting. See opencode/.../tool/task.ts:113 and tool/task_status.ts:114.
export OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=1
if [ -z "$RETRACE_INTERNAL_TOKEN" ] && [ -f "$HOME/.local/share/retrace/internal-token" ]; then
  export RETRACE_INTERNAL_TOKEN="$(cat "$HOME/.local/share/retrace/internal-token")"
fi
if [ -z "$RETRACE_INTERNAL_TOKEN" ]; then
  echo "WARN: RETRACE_INTERNAL_TOKEN is not set; shell tool will fail."
  echo "       Backend should write it to ~/.local/share/retrace/internal-token at startup."
fi

exec bun run --cwd "$SCRIPT_DIR/packages/opencode" --conditions=browser \
  src/index.ts serve --port "$PORT" --hostname 127.0.0.1
