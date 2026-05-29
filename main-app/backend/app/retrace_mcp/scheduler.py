"""
ReTrace Scheduler MCP Server — agent-callable follow-up scheduling.

Lets the LLM schedule a future agent turn for itself. At fire time, retrace's
backend POSTs the stored prompt as a user-role message into the user's
OpenCode session — same channel a typed message would take. The user's
frontend sees the new turn via the regular SSE stream the moment it lands.

This is the retrace analog of Claude Code's ScheduleWakeup / CronCreate
tools (the harness owns time; the LLM owns instructions). See
app/services/followup_scheduler.py for the fire-time mechanics.

Tools:
  - schedule_followup(prompt, when, ...)   one-shot
  - schedule_interval(prompt, every, unit, ...)   recurring at fixed interval
  - schedule_cron(prompt, expression, ...)        recurring at cron times
  - monitor_tool(...) / create_remote_trigger(...)  watch / webhook
  - agent_status()                                what's active (all kinds)
  - cancel(id)                                    cancel any item by id

Required env (injected by opencode_manager.generate_config):
  RETRACE_USER_ID, RETRACE_BACKEND_URL, RETRACE_INTERNAL_TOKEN
"""

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("retrace-scheduler")

_USER_ID = os.environ.get("RETRACE_USER_ID", "")
_BACKEND_URL = (os.environ.get("RETRACE_BACKEND_URL") or "").rstrip("/")
# Public hostname surfaced to users for webhook URLs. The MCP itself
# always uses RETRACE_BACKEND_URL for backend RPCs (localhost is the
# right address inside the host). But the URL we hand to the user for
# external systems to POST to needs to be publicly reachable —
# typically the Fly app domain in prod, or an ngrok tunnel in dev.
# Falls back to RETRACE_BACKEND_URL when unset (current dev behaviour:
# the user sees a 127.0.0.1 URL and is warned to substitute).
_PUBLIC_BASE_URL = (os.environ.get("RETRACE_PUBLIC_BASE_URL") or _BACKEND_URL).rstrip("/")
_INTERNAL_TOKEN = os.environ.get("RETRACE_INTERNAL_TOKEN") or ""
_HTTP_TIMEOUT = 10.0  # scheduling ops aren't latency-sensitive; be patient


def _conversation_id_from_env() -> Optional[str]:
    """The OC process is per-user, not per-conversation. To fire a follow-up
    into "the same conversation the user was in when they scheduled it," the
    agent has to pass the conversation_id explicitly. The retrace-browser MCP
    has a similar problem and solves it via `_active_browser_conv[user_id]`
    on the backend. For follow-ups we ask the backend for the user's most
    recently-active conversation via the same internal mapping.
    """
    if not (_BACKEND_URL and _INTERNAL_TOKEN and _USER_ID):
        return None
    # Try the active-browser-conv mapping (set on every send_message turn).
    # Falls back to None if there's no active conversation.
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.post(
                f"{_BACKEND_URL}/api/v1/agent/internal/active-conversation",
                json={"user_id": _USER_ID},
                headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
        if resp.status_code == 200:
            return resp.json().get("conversation_id")
    except Exception:
        pass
    return None


def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous HTTP POST to one of the /api/v1/agent/internal/followups/*
    endpoints. Sync (not async) because FastMCP tool callbacks run on a
    threadpool — async here would require extra plumbing for no benefit.
    """
    if not (_BACKEND_URL and _INTERNAL_TOKEN):
        return {"error": "scheduler MCP missing backend URL or internal token"}
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(
                f"{_BACKEND_URL}/api/v1/agent/internal/{path}",
                json=body,
                headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        return resp.json()
    except Exception as exc:
        return {"error": f"transport error: {exc}"}


# ─── "when" parsing for one-shot follow-ups ────────────────────────────────
#
# We accept either an absolute ISO-8601 timestamp ("2026-05-28T22:30:00Z")
# or a relative offset ("in 5 minutes", "in 30 seconds", "in 1 hour", "in 2 days").
# Keeping the relative form is important — LLMs reason about "schedule
# something in 10 minutes" more reliably than computing an absolute timestamp.
_REL_PATTERN = re.compile(r"^\s*in\s+(\d+)\s*(second|minute|hour|day)s?\s*$", re.IGNORECASE)


def _resolve_run_at(when: str) -> Optional[str]:
    """Return an ISO-8601 UTC timestamp string, or None if `when` is invalid."""
    m = _REL_PATTERN.match(when or "")
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = {"second": timedelta(seconds=n), "minute": timedelta(minutes=n),
                 "hour": timedelta(hours=n), "day": timedelta(days=n)}[unit]
        return (datetime.utcnow() + delta).isoformat() + "Z"
    # Absolute ISO. Strip a trailing Z so fromisoformat accepts it.
    try:
        normalized = (when or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.isoformat() + "Z"
    except Exception:
        return None


@mcp.tool()
def schedule_followup(
    prompt: str,
    when: str,
    description: str = "",
    conversation_id: str = "",
) -> str:
    """Schedule the agent to receive a one-shot prompt at a future time.

    When the schedule fires, retrace injects `prompt` as a user-role message
    into the same conversation, and the agent runs a normal turn responding
    to it. The agent at fire time sees `prompt` verbatim with no memory of
    when/why it was scheduled — write the prompt to carry full context
    ("Re-check greatandhra.com vs the snapshot in /tmp/ga.txt, summarize any
    changes" rather than "do the thing").

    Args:
      prompt: The exact text the agent will see when the schedule fires.
      when: When to fire. Either a relative offset ("in 5 minutes", "in 2 hours",
            "in 1 day") OR an ISO-8601 UTC timestamp ("2026-05-28T22:30:00Z").
      description: Optional short human label for the schedule (shows up in
                   agent_status). Defaults to a truncation of `prompt`.
      conversation_id: Optional — defaults to the conversation this agent is
                   currently running in. Specify only if scheduling into a
                   different conversation.

    Returns:
      A description of the scheduled follow-up including its `followup_id`
      (used to cancel it via cancel(id)).
    """
    run_at = _resolve_run_at(when)
    if not run_at:
        return (
            f"Error: couldn't parse `when`={when!r}. Use a relative offset like "
            f"'in 5 minutes' / 'in 2 hours' / 'in 1 day', or an ISO-8601 UTC "
            f"timestamp like '2026-05-28T22:30:00Z'."
        )
    conv = conversation_id or _conversation_id_from_env() or ""
    if not conv:
        return (
            "Error: no conversation_id supplied and the backend doesn't have an "
            "active conversation for this user. Pass conversation_id explicitly."
        )
    desc = (description or prompt[:80]).strip()
    result = _post("followups/schedule", {
        "user_id": _USER_ID,
        "conversation_id": conv,
        "prompt": prompt,
        "trigger_type": "once",
        "trigger_config": {"run_at": run_at},
        "description": desc,
    })
    if "error" in result:
        return f"Error scheduling follow-up: {result['error']}"
    return (
        f"Scheduled one-shot follow-up at {run_at} (UTC).\n"
        f"followup_id: {result.get('followup_id')}\n"
        f"description: {result.get('description', desc)}\n"
        f"Cancel with: cancel(id={result.get('followup_id')!r})"
    )


@mcp.tool()
def schedule_interval(
    prompt: str,
    every: int,
    unit: str = "minutes",
    description: str = "",
    conversation_id: str = "",
    stop_after: str = "",
    stop_after_fires: int = 0,
) -> str:
    """Schedule the agent to receive a prompt repeatedly at a fixed interval,
    with an optional auto-stop.

    Use for recurring checks at regular cadence (every 10 minutes, every hour,
    every 6 hours, etc.). For more complex schedules ("weekdays at 9am",
    "every 30 minutes during business hours") use schedule_cron instead.

    Args:
      prompt: The prompt the agent will see on each fire (same text every
              time; the agent re-derives intent from it).
      every: Numeric interval (e.g. 10).
      unit: One of 'seconds', 'minutes', 'hours', 'days'. Default 'minutes'.
      description: Optional short label (shown in agent_status).
      conversation_id: Defaults to the current conversation.
      stop_after: Optional auto-cancel deadline. Either a relative offset
                  ("in 15 minutes", "in 1 hour") OR an ISO-8601 UTC
                  timestamp. After this time the interval stops firing.
                  Use this when the user asks for bounded monitoring
                  ("every 30s for the next 2 minutes").
      stop_after_fires: Optional auto-cancel by count. Stops after N fires.
                  Use when the user counts iterations ("check 5 times").
                  Set EITHER stop_after OR stop_after_fires — they don't
                  combine usefully.

    Returns:
      A description including the followup_id.

    Notes:
      - First fire happens after `every <unit>` (not immediately).
      - Cancel any time with cancel(id).
      - Avoid intervals shorter than 1 minute unless the work is fast — each
        fire costs LLM tokens.
      - "Every 30 seconds for 2 minutes" → every=30, unit='seconds',
        stop_after='in 2 minutes'.
    """
    if unit not in ("seconds", "minutes", "hours", "days"):
        return f"Error: unit must be one of seconds/minutes/hours/days, got {unit!r}"
    if every <= 0:
        return f"Error: `every` must be positive, got {every}"
    conv = conversation_id or _conversation_id_from_env() or ""
    if not conv:
        return "Error: no conversation_id; pass it explicitly."
    trigger_config: Dict[str, Any] = {"every": every, "unit": unit}
    stop_msg = ""
    if stop_after:
        stop_iso = _resolve_run_at(stop_after)
        if not stop_iso:
            return (
                f"Error: couldn't parse stop_after={stop_after!r}. Use 'in 15 minutes' "
                f"or an ISO timestamp."
            )
        trigger_config["stop_after"] = stop_iso
        stop_msg = f", auto-stops at {stop_iso}"
    if stop_after_fires:
        if stop_after_fires <= 0:
            return f"Error: stop_after_fires must be positive, got {stop_after_fires}"
        trigger_config["stop_after_fires"] = stop_after_fires
        stop_msg = f", auto-stops after {stop_after_fires} fire(s)"
    desc = (description or prompt[:80]).strip()
    result = _post("followups/schedule", {
        "user_id": _USER_ID,
        "conversation_id": conv,
        "prompt": prompt,
        "trigger_type": "interval",
        "trigger_config": trigger_config,
        "description": desc,
    })
    if "error" in result:
        return f"Error scheduling interval follow-up: {result['error']}"
    return (
        f"Scheduled recurring follow-up every {every} {unit}{stop_msg}.\n"
        f"followup_id: {result.get('followup_id')}\n"
        f"description: {result.get('description', desc)}\n"
        f"Cancel with: cancel(id={result.get('followup_id')!r})"
    )


@mcp.tool()
def schedule_cron(
    prompt: str,
    expression: str,
    description: str = "",
    conversation_id: str = "",
) -> str:
    """Schedule the agent via standard 5-field cron expression.

    Cron format: 'minute hour day month day_of_week'. Examples:
      '0 9 * * 1-5'   weekdays at 9:00 UTC
      '*/30 * * * *'  every 30 minutes
      '0 */6 * * *'   every 6 hours on the hour
      '0 0 * * 0'     Sundays at midnight UTC

    Times are evaluated in UTC. For one-off or fixed-interval schedules
    prefer schedule_followup / schedule_interval — cron is heavier.

    Args:
      prompt: The prompt the agent will see on each fire.
      expression: 5-field cron expression.
      description: Optional short label (shown in agent_status).
      conversation_id: Defaults to the current conversation.
    """
    parts = (expression or "").split()
    if len(parts) != 5:
        return (
            f"Error: cron expression must have exactly 5 fields "
            f"(minute hour day month day_of_week), got {len(parts)}: {expression!r}"
        )
    conv = conversation_id or _conversation_id_from_env() or ""
    if not conv:
        return "Error: no conversation_id; pass it explicitly."
    desc = (description or prompt[:80]).strip()
    result = _post("followups/schedule", {
        "user_id": _USER_ID,
        "conversation_id": conv,
        "prompt": prompt,
        "trigger_type": "cron",
        "trigger_config": {"expression": expression},
        "description": desc,
    })
    if "error" in result:
        return f"Error scheduling cron follow-up: {result['error']}"
    return (
        f"Scheduled cron follow-up '{expression}' (UTC).\n"
        f"followup_id: {result.get('followup_id')}\n"
        f"description: {result.get('description', desc)}\n"
        f"Cancel with: cancel(id={result.get('followup_id')!r})"
    )


# ─── Monitors (server-side change watches) ─────────────────────────────────
# Same MCP server as the follow-up tools — all scheduling primitives. The
# watch loop runs server-side with no LLM cost per check; the agent is only
# woken when the fire_when condition is met (or the window ends). One generic
# `monitor_tool` covers every target kind; agent_status lists them and
# cancel(id) cancels them.


_DURATION_PATTERN = re.compile(r"^\s*(\d+)\s*(second|minute|hour|day)s?\s*$", re.IGNORECASE)


def _parse_duration_seconds(value: str) -> Optional[int]:
    """Accept '5 minutes' / '1 hour' / '90 seconds' / '2 days'. Returns total
    seconds, or None if unparseable."""
    m = _DURATION_PATTERN.match(value or "")
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n * {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[unit]


def _monitor_display_kind(r: dict) -> str:
    """Friendly kind label. builtin/read_page is surfaced as 'browser' since
    that's how the user thinks of it."""
    tk = r.get("target_kind")
    tj = r.get("target_json") or {}
    if tk == "builtin" and tj.get("tool") == "read_page":
        return "browser"
    return tk or "?"


def _render_tool_monitor(r: dict) -> str:
    """One-row formatter for a unified tool-monitor dict."""
    tk = r.get("target_kind")
    tj = r.get("target_json") or {}
    disp = _monitor_display_kind(r)
    if tk == "url":
        what = repr(tj.get("url", ""))
    elif tk == "command":
        what = f"`{(tj.get('command') or '')[:120]}`"
    elif tk == "stream":
        what = f"`{(tj.get('command') or '')[:120]}` (debounce {tj.get('debounce_seconds', 5)}s)"
    elif tk == "websocket":
        what = f"{tj.get('url', '')} (debounce {tj.get('debounce_seconds', 5)}s)"
    elif disp == "browser":
        sel = (tj.get("args") or {}).get("selector")
        what = f"live tab (selector: {sel or '<whole page>'}); last_url: {r.get('last_url') or 'none yet'}"
    elif tk == "builtin":
        what = f"tool {tj.get('tool')}"
    elif tk == "mcp":
        ext = tj.get("extension_name") or tj.get("extension_id")
        what = f"{ext}:{tj.get('tool')}"
    else:
        what = str(tj)
    fw = r.get("fire_when", "changed")
    line = (
        f"  - {r.get('monitor_id')} [{disp}]: {what}\n"
        f"    every {r.get('every_seconds')}s, expires {r.get('expires_at')}\n"
        f"    fire_when: {fw}\n"
        f"    checks={r.get('check_count', 0)}, changes={r.get('change_count', 0)}, "
        f"fires={r.get('fire_count', 0)}\n"
        f"    desc: {r.get('description', '')!r}"
    )
    err = r.get("last_error")
    if err:
        line += f"\n    LAST ERROR: {err}"
    return line


# ─── Generic monitor (one tool, any kind) ──────────────────────────────────
# Single entry point that routes any target kind through the same unified
# backend the per-kind tools use. Prefer the specific monitor_* tools above
# (clearer params + guidance route better); this exists for completeness and
# for programmatic callers that want one polymorphic interface.


@mcp.tool()
def monitor_tool(
    target: dict,
    duration: str,
    every: str = "30 seconds",
    debounce: str = "5 seconds",
    fire_when: str = "changed",
    prompt_on_change: str = "",
    description: str = "",
    conversation_id: str = "",
) -> str:
    """Watch something server-side and wake the agent only when it changes
    (or matches `fire_when`). Zero LLM cost while quiet. ONE tool for every
    kind of target — pick the right `kind`:

    POLL kinds (re-checked every `every`; default 30s):
      {"kind":"url", "url":"https://..."}
          Server-rendered HTML or JSON/data APIs ONLY. Does NOT work on
          JS-rendered SPA pages (TradingView, Twitter, most dashboards) —
          they return identical HTML every fetch. For those use kind=browser.
      {"kind":"command", "command":"<shell>"}
          Runs the command ONCE per tick and diffs stdout. Use for snapshots
          (`tail -n 200 log | grep ERROR`, `df -h`, `pgrep ... | wc -l`).
          The command MUST exit each run — a `tail -f` would hang; use
          kind=stream for follow-style commands. THRESHOLDS: pipe a metric to
          a bare number and set fire_when='value > N' (fires on crossings only).
      {"kind":"browser", "selector":"<css>"?}
          Reads the live post-JS DOM text of the conversation's browser tab.
          THIS is the right kind for SPAs / live dashboards / animated tickers
          (the user must have the page open, or navigate there first).
      {"kind":"mcp", "extension":"<name>", "tool":"<tool>", "args":{}}
          Output of any installed MCP extension's tool (e.g. ServiceNow,
          Linear). `extension` is its name; min cadence 30s.

    LIVE kinds (persistent connection; fire at most every `debounce`, default 5s):
      {"kind":"stream", "command":"tail -f ..."}
          A LONG-RUNNING command streamed line-by-line (tail -f, journalctl -f,
          kubectl logs -f). Default fire_when is 'contains:ERROR' — use a
          filter, not 'changed' (a busy log fires constantly).
      {"kind":"websocket", "url":"wss://...", "subscribe":"{...}"?, "headers":{}?}
          A live ws(s):// push feed (crypto/stock ticks, scores). Most feeds
          need a `subscribe` frame (the raw JSON string the server expects).
          THIS is the right kind for "watch the live BTC price" — polling can't
          subscribe to a feed.

    `fire_when` (server-side, before any LLM cost): 'changed' (default),
    'value > N' (also < <= >= == !=, edge-triggered on the FIRST number in the
    output), 'contains:TEXT', 'not_contains:TEXT', 'matches:REGEX',
    'not_seen_for:Ns'. `every` = poll cadence; `debounce` = live-kind coalesce
    window; `duration` = total watch window. Returns monitor_id (cancel via
    cancel(id)). conversation_id defaults to the current conversation.
    """
    if not isinstance(target, dict) or not target.get("kind"):
        return "Error: target must be a dict with a 'kind' field."
    kind = str(target.get("kind")).strip().lower()
    conv = conversation_id or _conversation_id_from_env() or ""
    if not conv:
        return "Error: no conversation_id; pass it explicitly."

    duration_seconds = _parse_duration_seconds(duration)
    if duration_seconds is None:
        return f"Error: `duration` must be like '5 minutes' / '1 hour', got {duration!r}"
    every_seconds = _parse_duration_seconds(every) or 30
    debounce_seconds = _parse_duration_seconds(debounce) or 5

    # Build the backend payload per kind, mirroring the specific tools.
    desc = (description or f"monitor {kind}").strip()
    on_change = (prompt_on_change or
        "The monitored output above changed. Summarise what's new in 1-2 sentences."
    ).strip()
    body = {
        "user_id": _USER_ID,
        "conversation_id": conv,
        "duration_seconds": duration_seconds,
        "prompt_on_change": on_change,
        "fire_when": fire_when or "changed",
        "description": desc,
    }

    if kind == "url":
        if not str(target.get("url", "")).startswith(("http://", "https://")):
            return "Error: url target needs target['url'] starting http(s)://"
        body["target_kind"] = "url"
        body["target_json"] = {"url": target["url"], "selector": target.get("selector")}
        body["every_seconds"] = every_seconds
    elif kind == "command":
        if not str(target.get("command", "")).strip():
            return "Error: command target needs target['command']"
        body["target_kind"] = "command"
        body["target_json"] = {"command": target["command"]}
        body["every_seconds"] = every_seconds
    elif kind == "browser":
        body["target_kind"] = "builtin"
        body["target_json"] = {"tool": "read_page", "args": {"selector": target.get("selector")}}
        body["every_seconds"] = every_seconds
    elif kind == "mcp":
        if not (target.get("extension") and target.get("tool")):
            return "Error: mcp target needs target['extension'] and target['tool']"
        body["target_kind"] = "mcp"
        body["target_json"] = {"extension": target["extension"], "tool": target["tool"],
                               "args": target.get("args") or {}}
        body["every_seconds"] = every_seconds
    elif kind == "stream":
        if not str(target.get("command", "")).strip():
            return "Error: stream target needs target['command'] (e.g. 'tail -f log')"
        body["target_kind"] = "stream"
        body["target_json"] = {"command": target["command"], "debounce_seconds": debounce_seconds}
        body["every_seconds"] = 0
    elif kind == "websocket":
        if not str(target.get("url", "")).startswith(("ws://", "wss://")):
            return "Error: websocket target needs target['url'] starting ws(s)://"
        body["target_kind"] = "websocket"
        body["target_json"] = {"url": target["url"], "subscribe": target.get("subscribe") or None,
                               "headers": target.get("headers") or None,
                               "debounce_seconds": debounce_seconds}
        body["every_seconds"] = 0
    else:
        return (f"Error: unknown target kind {kind!r}. Use one of: "
                f"url, command, browser, mcp, stream, websocket.")

    result = _post("tool-monitors/add", body)
    if "error" in result:
        return f"Error starting {kind} monitor: {result['error']}"
    return (
        f"Started {kind} monitor.\n"
        f"monitor_id: {result.get('monitor_id')}\n"
        f"expires at {result.get('expires_at')} (UTC).\n"
        f"fire_when: {result.get('fire_when', fire_when)}\n"
        f"description: {result.get('description', desc)}\n"
        f"Cancel with: cancel(id={result.get('monitor_id')!r})"
    )


# ─── Remote triggers / webhooks (Tier 4) ───────────────────────────────────
# External systems POST to a per-user URL → retrace fires a turn in the
# conversation. No polling, no schedule — pure event-driven wake-up.


@mcp.tool()
def create_remote_trigger(
    prompt_template: str,
    description: str = "",
    conversation_id: str = "",
) -> str:
    """Create a webhook URL that wakes the agent when something external
    POSTs to it.

    Use for plugging the agent into existing event sources:
      - GitHub Actions: 'curl <url>' in a workflow step → agent gets a
        turn when CI finishes
      - Slack/Linear/Discord webhooks (forwarded) → agent reacts to
        channel messages or ticket updates
      - Your own scripts on other machines → fire alerts here when
        something interesting happens
      - cron from a different box → schedule-like triggers without
        relying on retrace's own scheduler

    Args:
      prompt_template: The instructions the agent will see when the
                       trigger fires. The incoming POST body is
                       AUTOMATICALLY appended after this template as a
                       pretty-printed JSON code block — you do NOT need
                       to write `{{payload}}` or `{payload}` (those
                       literals are stripped on creation and will look
                       like cruft).
                       Write the template as plain instructions that
                       refer to "the payload below" or "the data below".
                       GOOD:
                         'A new GitHub PR review just arrived. Summarise the payload below.'
                         'Linear ticket updated. Tell me what changed (see data below).'
                         'CI run finished. If status != success in the data below, alert me loudly.'
                       BAD (we strip these literals; don't bother):
                         'CI finished. Payload: {{payload}}'
                         'New event: {payload}'
      description: Optional short label (shown in agent_status).
      conversation_id: Defaults to the current conversation.

    Returns:
      The webhook URL (long random token in the path — the URL IS the
      secret), the trigger_id (for cancel), and copy-paste curl examples.
    """
    if not prompt_template or len(prompt_template.strip()) < 10:
        return "Error: prompt_template must be at least 10 chars (give the agent context to act on)."
    conv = conversation_id or _conversation_id_from_env() or ""
    if not conv:
        return "Error: no conversation_id; pass it explicitly."
    desc = (description or prompt_template[:80]).strip()
    result = _post("triggers/create", {
        "user_id": _USER_ID,
        "conversation_id": conv,
        "prompt_template": prompt_template,
        "description": desc,
    })
    if "error" in result:
        return f"Error creating remote trigger: {result['error']}"
    token = result.get("token")
    trigger_id = result.get("trigger_id")
    # Use the publicly-reachable base URL when configured (prod), or fall
    # back to the in-cluster URL (dev). When the fallback hits a localhost
    # URL, the user needs to swap in their public hostname themselves to
    # share it externally — we surface a clear note about that.
    url = f"{_PUBLIC_BASE_URL}/api/v1/triggers/{token}"
    needs_public_host = (
        "127.0.0.1" in _PUBLIC_BASE_URL
        or "localhost" in _PUBLIC_BASE_URL
    )
    note = (
        f"\nThis webhook URL uses {_PUBLIC_BASE_URL!r} which is a local-only address.\n"
        f"To share with an external service (GitHub Actions, Slack, etc.) replace the\n"
        f"{_PUBLIC_BASE_URL} prefix with your public hostname (e.g. https://retrace-trial.fly.dev).\n"
        if needs_public_host else ""
    )
    return (
        f"Created remote trigger '{desc}'.\n"
        f"trigger_id: {trigger_id}\n"
        f"webhook URL (token in the path IS the secret — keep it private):\n"
        f"  {url}\n"
        f"\n"
        f"Test it now with:\n"
        f"  curl -X POST -H 'Content-Type: application/json' -d '{{\"hello\":\"world\"}}' '{url}'\n"
        f"{note}"
        f"Cancel anytime with: cancel(id={trigger_id!r})"
    )


# ─── Unified status (Tier 5) ───────────────────────────────────────────────


@mcp.tool()
def cancel(id: str) -> str:
    """Cancel ANY async item you set up — a monitor, a scheduled follow-up,
    or a remote trigger — by its id. (Replaces the per-type cancel tools.)

    The id is whatever was returned when the item was created, or shown in
    agent_status. ids are globally unique, so this routes to the right one
    automatically. Returns what was cancelled, or a not-found note.
    """
    if not id:
        return "Error: id is required."
    # ids are globally-unique UUIDs, so at most one family owns a given id.
    # Try each in turn; the tool-monitors endpoint itself routes poll- vs
    # stream/websocket-monitors by target_kind.
    r = _post("tool-monitors/cancel", {"user_id": _USER_ID, "monitor_id": id})
    if "error" not in r and r.get("cancelled"):
        return f"Cancelled monitor {id}."
    r = _post("followups/cancel", {"user_id": _USER_ID, "followup_id": id})
    if "error" not in r and r.get("cancelled"):
        return f"Cancelled scheduled follow-up {id}."
    r = _post("triggers/cancel", {"user_id": _USER_ID, "trigger_id": id})
    if "error" not in r and r.get("cancelled"):
        return f"Cancelled remote trigger {id} (its webhook URL will now 404)."
    return (
        f"No active monitor, follow-up, or remote trigger found for id {id} "
        "(already cancelled, expired, or never existed). "
        "Use agent_status to see what's currently active."
    )


@mcp.tool()
def agent_status() -> str:
    """One-shot summary of EVERYTHING currently active for the user:
    scheduled follow-ups, URL monitors, and remote triggers.

    Use when the user asks "what's running?" / "what's pending?" /
    "what have you set up?" — so the agent can answer with a single
    coherent view rather than three separate tool calls.

    Background subagents launched via `task(background=true)` are NOT
    listed here — OC doesn't expose an enumeration endpoint for them.
    If you need a specific subagent's state, call task_status(task_id).
    """
    followups = _post("followups/list", {"user_id": _USER_ID}).get("followups", []) or []
    tool_monitors = _post("tool-monitors/list", {"user_id": _USER_ID}).get("monitors", []) or []
    triggers = _post("triggers/list", {"user_id": _USER_ID}).get("triggers", []) or []

    total = len(followups) + len(tool_monitors) + len(triggers)
    if total == 0:
        return (
            "Nothing currently scheduled or watching.\n\n"
            "(Note: this list does NOT include background subagents from "
            "`task(background=true)` — check those with `task_status(task_id)`.)"
        )

    sections: list[str] = [f"Agent status — {total} item(s) active:\n"]

    if followups:
        sections.append(f"\n## Scheduled follow-ups ({len(followups)})")
        for r in followups:
            cfg = r.get("trigger_config", {}) or {}
            tt = r.get("trigger_type")
            if tt == "once":
                schedule = f"once at {cfg.get('run_at')}"
            elif tt == "interval":
                unit = cfg.get("unit", "minutes")
                schedule = f"every {cfg.get('every')} {unit}"
                if cfg.get("stop_after"):
                    schedule += f" (stops at {cfg['stop_after']})"
                if cfg.get("stop_after_fires"):
                    schedule += f" (stops after {cfg['stop_after_fires']} fires)"
            elif tt == "cron":
                schedule = f"cron: {cfg.get('expression')}"
            else:
                schedule = tt or "unknown"
            sections.append(
                f"  - {r.get('followup_id')}: {schedule}\n"
                f"    desc: {r.get('description', '')!r}\n"
                f"    fires={r.get('fire_count', 0)}, last={r.get('last_fired_at') or 'never'}"
            )

    if tool_monitors:
        sections.append(f"\n## Monitors ({len(tool_monitors)})")
        for r in tool_monitors:
            sections.append(_render_tool_monitor(r))

    if triggers:
        sections.append(f"\n## Remote triggers / webhooks ({len(triggers)})")
        for r in triggers:
            sections.append(
                f"  - {r.get('trigger_id')}: {r.get('description', '')!r}\n"
                f"    fires={r.get('fire_count', 0)}, last={r.get('last_fired_at') or 'never'}"
            )

    sections.append(
        "\n(Background subagents from `task(background=true)` aren't listed; "
        "use `task_status(task_id)` for those.)"
    )
    return "\n".join(sections)


if __name__ == "__main__":
    mcp.run()
