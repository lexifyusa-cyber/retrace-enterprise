#!/bin/bash
# Wrapper that lets retrace spawn this OC fork's source via bun.
#
# Retrace's opencode_manager invokes the opencode binary like:
#     <binary> serve --port <N> --hostname 127.0.0.1
#
# This script proxies that to:
#     bun run --cwd packages/opencode --conditions=browser src/index.ts <args>
#
# So you can edit OC source files and the next retrace turn picks them up
# (no build step). Point retrace at it with:
#     export RETRACE_OPENCODE_BIN=/path/to/this/run-from-source.sh
#
# To switch to the published binary instead, unset RETRACE_OPENCODE_BIN.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bun run --cwd "$SCRIPT_DIR/packages/opencode" --conditions=browser src/index.ts "$@"
