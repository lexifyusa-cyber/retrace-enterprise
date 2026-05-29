# Generic `monitor` Primitive — Implementation Plan (v2)

Status: COMPLETE. Phases 1-5 done.
- 1: predicate engine (monitor_predicate.py) + 53 tests
- 2: unified tool_monitor service + table, url/command native + fire_when
- 3: builtin/browser via read_page; legacy browser_monitor deleted
- 4: mcp targets via mcp_client_manager.call_tool (DB cold-start + name
     resolution) + monitor_mcp_tool MCP tool
- 5: deleted legacy url_monitor/command_monitor/browser_monitor services,
     models, and their /internal/* endpoints. Orphaned url_monitors /
     command_monitors / browser_monitors tables left in the dev DB (harmless;
     drop on a fresh deploy).
Agent-facing surface: monitor_url, monitor_command, monitor_browser,
monitor_mcp_tool, list_monitors, cancel_monitor — all on one backend with
one _check pipeline + fire_when.

Phase 6 (streaming, process-streams only) ALSO done — see
~/.claude/plans/binary-greeting-popcorn.md. Adds monitor_stream (live
tail -f-style line-by-line watching) via a sibling StreamMonitorService
(persistent asyncio task per monitor, shared tool_monitors table with
target_kind="stream"), reusing should_fire + _inject_prompt. Gated by
RETRACE_ENABLE_STREAM_MONITOR.

Phase 7 (websocket monitor) ALSO done. Extends StreamMonitorService with a
second source kind target_kind="websocket": persistent wss:// connection
(websockets 16.0, additional_headers), optional subscribe frame, reconnect
with exponential backoff, per-message should_fire + coalesce/debounce. New
MCP tool monitor_websocket. Closes the live-network-feed gap (crypto/stock
ticks) that polling can't see — verified live against Coinbase BTC-USD
ticker (175 msgs, 5 coalesced fires, clean expiry). Agent surface now 17 tools.

## Goal & end state

One monitor primitive that watches the output of **any tool the agent can
reach** (native fast paths + any MCP tool) and fires only when a configurable
predicate matches. As users install MCPs (Slack, Linear, Stripe…), each becomes
monitorable for free with zero new code. Browser monitoring stops being a
special case — it's just "monitor the `read_page` tool."

This closes two gaps observed in real testing:
- The CPU threshold test fired 17 times when only 6 were true crossings —
  diff-only monitors are noisy for threshold/pattern asks. `fire_when` fixes it.
- Each new MCP would otherwise need a hand-built monitor. The generic path
  removes that per-MCP work.

## Architecture — two invocation paths

`_invoke_target(target) -> str` dispatches on `target.kind`:

| Path | kind | invocation each tick | why |
|---|---|---|---|
| Native fast path | `url` | `httpx.get` + HTML-strip | no cheaper tool equivalent |
| | `command` | `create_subprocess_shell` | subprocess simpler than any tool round-trip |
| Generic tool path | `mcp` | `mcp_client_manager.call_tool(extension_id, user_id, tool, args)` | user-installed MCPs |
| | `builtin` | call the backend service the retrace MCP shim wraps (e.g. `read_page`) | **browser monitoring lives here** |

Browser is NOT a distinct kind. A browser monitor is
`target = {kind:"builtin", tool:"read_page", args:{selector}}`. The existing
`read_page` tool (browser.py:228) already extracts live post-JS DOM text. url
and command stay native because httpx/subprocess are genuinely simpler than
routing through a tool.

## `fire_when` predicate language

Evaluated backend-side BEFORE waking the agent. No `eval()` — explicit prefix
dispatch. Malformed spec → log once, fall back to `changed`.

| spec | fires when | firing policy |
|---|---|---|
| `changed` (default) | output hash differs from last tick | every change |
| `value > N` / `< / >= / <= / == / !=` | first number parsed from output satisfies compare | edge (false→true) |
| `contains:X` | substring present | changed AND present |
| `not_contains:X` | substring absent | edge (false→true) |
| `matches:REGEX` | regex search hits | changed AND match |
| `not_seen_for:Ns` | output unchanged for ≥ N seconds | edge (false→true) |

Edge-triggered predicates use `last_predicate_state` on the row so a threshold
that stays satisfied fires once on crossing, not every tick. `contains`/`matches`
use "changed AND condition" so a new matching line fires but a static match
sitting in a tail window doesn't spam. Known v1 limitation: `contains` can
false-positive if unrelated content changes while the match still sits in the
window (no line-level new-content detection yet).

## Data model — `tool_monitors`

```
monitor_id, user_id, conversation_id, is_active
target_kind:  String(20)   # url | command | mcp | builtin
target_json:  JSON
every_seconds, expires_at
fire_when:    String(200)  default "changed"
prompt_on_change, description
last_content_hash, last_predicate_state, last_check_at, last_changed_at, last_error, last_url
check_count, change_count, created_at, updated_at
```

## Files

New:
- `app/models/tool_monitor.py` (~70)
- `app/services/monitor_predicate.py` (~90) + `tests/test_monitor_predicate.py`
- `app/services/tool_monitor.py` (~340)

Changed:
- `app/api/agent.py` — `/internal/tool-monitors/{add,cancel,list}` (~70)
- `app/retrace_mcp/scheduler.py` — new `monitor(target,...)`; `monitor_url`/`command`/`browser`
  become sugar; `list_monitors`/`cancel_monitor` include tool monitors
- `app/services/opencode_manager.py` — build_prompt: `monitor` + `fire_when` + kinds + threshold example
- `app/main.py` — register model + start/stop service

Deleted in Phase 5 (superseded): `browser_monitor.py`, `command_monitor.py`,
`url_monitor.py` services + their tables + their `/internal/*` endpoints.

## Tool surface (LLM-facing)

```
monitor(target, every, duration, fire_when="changed", prompt_on_change="", conversation_id="")
#   {kind:"url", url, selector?}
#   {kind:"command", command}
#   {kind:"builtin", tool:"read_page", args:{selector}}   ← browser
#   {kind:"mcp", extension_id, tool, args}                ← any installed MCP
```
Sugar kept for ergonomics: `monitor_url`, `monitor_command`, `monitor_browser`.

## Edge cases / risks

- `builtin`/`mcp` invocation has overhead + may reconnect → enforce `every>=10s`
  (builtin), `>=30s` (mcp); skip-tick-if-prev-still-running guard.
- `read_page` truncates at 8000 chars — set the monitor output cap to match so
  hashes are stable.
- predicate on non-numeric output → false, log once, never crash.
- unknown/disabled extension_id → fail the tick with a clear `last_error`.

## Phasing

1. Predicate engine + tests (~140 LOC, 1h)  ← **doing first**
2. Unified service + model, native (url/command) + fire_when; port url/command sugar
   (~430 LOC, 3-4h). **Fixes the CPU 17-fire bug.**
3. Generic path — `builtin` (browser via read_page); port browser sugar; delete old
   browser_monitor (~120 + deletions, 2h)
4. Generic path — `mcp` (user MCPs) via call_tool (~120, 1.5h)
5. Cleanup: delete old url/command services, tables, endpoints (~-500, 1h)

Total ~750 net LOC, ~8h. Each phase independently shippable.
