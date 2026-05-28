"""
OpenCode Manager — spawn, health-check, and restart the OpenCode sidecar server.

One OpenCode server instance per user for isolation (port allocation).
Generates opencode.json config from ReTrace's llm_settings DB table.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from app.skills.mcp_builder_oc_prompt import MCP_BUILDER_OC_PROMPT
from app.services import reasoning as reasoning_resolver

logger = structlog.get_logger()

_BASE_PORT = 4096
_MAX_RESTARTS = 3
_RESTART_DELAY_S = 5
_HEALTH_INTERVAL_S = 30
_HEALTH_TIMEOUT_S = 5

_opencode_bin: Optional[str] = None


def _find_opencode_binary() -> Optional[str]:
    """Locate the opencode binary to spawn.

    Lookup order:
      1. `RETRACE_OPENCODE_BIN` env var — point at a fork's locally-built
         binary while developing. Falls through if the path doesn't exist
         so a typo doesn't lock you out of upstream.
      2. Cached value from a previous lookup.
      3. `which opencode` (PATH lookup).
      4. Common install locations (~/.opencode/bin, ~/.local/bin, ...).
      5. The npm-distributed `opencode-ai` install (npm prefix -g).

    Returns the absolute path to the binary, or None if nothing was found.
    """
    override = os.environ.get("RETRACE_OPENCODE_BIN")
    if override:
        p = Path(override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            logger.info("opencode_bin_override", path=str(p))
            return str(p)
        logger.warning(
            "opencode_bin_override_invalid",
            path=str(p),
            reason="not a file or not executable; falling back to default lookup",
        )

    if _opencode_bin:
        return _opencode_bin
    path = shutil.which("opencode")
    if path:
        return path
    common = [
        Path.home() / ".opencode" / "bin" / "opencode",
        Path.home() / ".local" / "bin" / "opencode",
        Path.home() / "go" / "bin" / "opencode",
        Path("/usr/local/bin/opencode"),
    ]
    for p in common:
        if p.is_file():
            return str(p)
    npm_prefix = os.popen("npm prefix -g 2>/dev/null").read().strip()
    if npm_prefix:
        candidate = Path(npm_prefix) / "bin" / "opencode"
        if candidate.is_file():
            return str(candidate)
    return None


def _opencode_config_dir() -> Path:
    d = Path.home() / ".config" / "opencode"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _opencode_projects_dir() -> Path:
    d = Path.home() / ".config" / "opencode" / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


class OpenCodeProcess:
    def __init__(self, user_id: str, port: int, external_url: Optional[str] = None):
        """Represent an OC instance for a user.

        Normal mode: retrace spawns and manages the OC subprocess.
        External mode (`external_url` set): the developer runs OC themselves
        in a terminal (e.g. `bun run dev` against the fork source). Retrace
        skips spawning + lifecycle management and just talks to the running
        OC at the given URL. Trade-off: multi-tenancy collapses to a single
        shared OC, suitable for dev only.
        """
        self.user_id = user_id
        self.port = port
        self.external_url = external_url
        self.process: Optional[asyncio.subprocess.Process] = None
        self.config_dir = _opencode_projects_dir() / user_id
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.restart_count = 0
        self._health_task: Optional[asyncio.Task] = None
        self._log_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        # External mode: don't spawn anything. Retrace will route HTTP calls
        # to `external_url` and we trust the dev has OC running there.
        if self.external_url:
            cmd = (
                f"OPENCODE_CONFIG_DIR={self.config_dir} "
                f"bun run --cwd packages/opencode --conditions=browser "
                f"src/index.ts serve --port {self.port} --hostname 127.0.0.1"
            )
            logger.info(
                "opencode_external_mode",
                user_id=self.user_id,
                url=self.external_url,
                config_dir=str(self.config_dir),
                run_this_in_your_terminal=cmd,
            )
            return

        bin_path = _find_opencode_binary()
        if not bin_path:
            raise RuntimeError("opencode binary not found on PATH")

        config_path = self.config_dir / "opencode.json"
        if not config_path.exists():
            logger.warning("opencode_config_missing", user_id=self.user_id)
            return

        env = os.environ.copy()
        env["OPENCODE_CONFIG_DIR"] = str(self.config_dir)
        # Retrace's forked shell.ts in OC reads these to route bash through
        # the PTY bridge. Without them OC's shell tool returns a config error.
        env["RETRACE_USER_ID"] = self.user_id
        env["RETRACE_BACKEND_URL"] = os.environ.get("RETRACE_BACKEND_URL", "http://127.0.0.1:8000")
        if os.environ.get("RETRACE_INTERNAL_TOKEN"):
            env["RETRACE_INTERNAL_TOKEN"] = os.environ["RETRACE_INTERNAL_TOKEN"]

        self.process = await asyncio.create_subprocess_exec(
            bin_path,
            "serve",
            "--port", str(self.port),
            "--hostname", "127.0.0.1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(self.config_dir),
        )

        self._log_task = asyncio.create_task(self._stream_logs())
        self._health_task = asyncio.create_task(self._health_loop())

        logger.info(
            "opencode_started",
            user_id=self.user_id,
            port=self.port,
            pid=self.process.pid,
        )

    async def stop(self) -> None:
        # External mode: we never spawned anything, so nothing to stop.
        # Don't kill the dev's terminal-managed OC.
        if self.external_url:
            return
        if self._health_task:
            self._health_task.cancel()
            self._health_task = None
        if self._log_task:
            self._log_task.cancel()
            self._log_task = None
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
            logger.info("opencode_stopped", user_id=self.user_id, port=self.port)
        self.process = None

    async def restart(self) -> bool:
        if self.restart_count >= _MAX_RESTARTS:
            logger.error("opencode_max_restarts", user_id=self.user_id)
            return False
        self.restart_count += 1
        await self.stop()
        await asyncio.sleep(_RESTART_DELAY_S)
        try:
            await self.start()
            return True
        except Exception as e:
            logger.error("opencode_restart_failed", user_id=self.user_id, error=str(e))
            return False

    async def is_healthy(self) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_S) as client:
                resp = await client.get(f"http://127.0.0.1:{self.port}/session")
                return resp.status_code in (200, 404)
        except Exception:
            return False

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEALTH_INTERVAL_S)
            if self.process and self.process.returncode is not None:
                logger.warning("opencode_process_died", user_id=self.user_id, returncode=self.process.returncode)
                await self.restart()
                return
            if not await self.is_healthy():
                logger.warning("opencode_unhealthy", user_id=self.user_id)
                await self.restart()
                return

    async def _stream_logs(self) -> None:
        if not self.process or not self.process.stderr:
            return
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace").rstrip()
                if decoded:
                    logger.debug("opencode_log", user_id=self.user_id, line=decoded)
        except asyncio.CancelledError:
            pass


class OpenCodeManager:
    def __init__(self):
        self._processes: Dict[str, OpenCodeProcess] = {}
        self._port_map: Dict[str, int] = {}
        # Per-user lock serializes start/stop so concurrent callers (page mount
        # + handleExecute, or rapid restart on team change) can't race past the
        # "already running" check and both bind the same port.
        self._user_locks: Dict[str, asyncio.Lock] = {}
        # Set of ports currently in use. _alloc_port scans starting from
        # _BASE_PORT so stopped users' ports are reclaimed on the next start.
        self._used_ports: set[int] = set()
        # The team_id / model the last config generation ran with, per user.
        # reload_for_user() replays these so a reload triggered from the MCP
        # Builder / MCP Explorer flow doesn't reset the user's /agent context.
        self._start_params: Dict[str, dict] = {}

    def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        # dict.setdefault is atomic in CPython — safe to call from multiple
        # coroutines without locking-the-locks recursively.
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    def _alloc_port(self, user_id: str) -> int:
        if user_id in self._port_map:
            return self._port_map[user_id]
        port = _BASE_PORT
        while port in self._used_ports:
            port += 1
            if port > 65535:
                raise RuntimeError("OpenCode: no available ports in the ephemeral range")
        self._port_map[user_id] = port
        self._used_ports.add(port)
        return port

    def _release_port(self, user_id: str) -> None:
        port = self._port_map.pop(user_id, None)
        if port is not None:
            self._used_ports.discard(port)

    async def generate_config(
        self,
        user_id: str,
        team_id: Optional[str] = None,
        model_override: Optional[str] = None,
        reasoning_mode: Optional[str] = None,
    ) -> Path:
        # Remember how this config was generated so reload_for_user() can
        # replay the same team/model/reasoning instead of resetting the user's
        # context. `reasoning_mode` falls back to the previously-stored value so
        # callers that don't know it (page mount, session create) preserve it.
        prev_params = self._start_params.get(user_id, {})
        if reasoning_mode is None:
            reasoning_mode = prev_params.get("reasoning_mode", "off")
        reasoning_mode = reasoning_resolver.normalize_mode(reasoning_mode)
        self._start_params[user_id] = {
            "team_id": team_id,
            "model_override": model_override,
            "reasoning_mode": reasoning_mode,
        }
        from datetime import datetime
        from app.db.database import async_session_maker
        from app.api.settings import get_active_llm_settings
        from sqlalchemy import select as sa_select
        from app.models.mcp_extension import McpExtension
        from app.models.user_extension import UserExtension

        async with async_session_maker() as session:
            llm = await get_active_llm_settings(session)

        api_key = llm.get("api_key", "")
        api_url = llm.get("api_url", "")
        model_name = llm.get("model_name", "gpt-4o")
        provider = llm.get("provider", "openai")

        # If the caller supplied a model_override (e.g. from the frontend's
        # model picker), it's expected to be "provider/model" — split it and
        # use those values for the config. The DB defaults still drive
        # everything else (api_key, api_url, custom_models metadata, etc.).
        if model_override:
            if "/" in model_override:
                ov_provider, ov_model = model_override.split("/", 1)
                if ov_provider and ov_model:
                    provider = ov_provider
                    model_name = ov_model
            else:
                model_name = model_override
        small_model_name = llm.get("small_model_name") or None
        plan_model_name = llm.get("plan_model_name") or None
        max_steps = llm.get("max_steps", 50)
        compaction_auto = llm.get("compaction_auto", False)
        compaction_tail_turns = llm.get("compaction_tail_turns", 4)
        snapshot_enabled = llm.get("snapshot_enabled", False)

        provider_id = "openai"
        if provider == "anthropic":
            provider_id = "anthropic"
        elif api_url and "anthropic" in api_url:
            provider_id = "anthropic"
        elif provider in ("xai", "grok") or (api_url and "x.ai" in api_url):
            provider_id = "openai"

        custom_models: Dict[str, Any] = {}
        # Emit custom-model metadata whenever the underlying provider is the
        # OpenAI-compatible HTTP shape. This includes xAI (`apiKey starts with
        # "xai-"`, baseURL contains `x.ai`) even when the user picks the
        # "openai" radio button in our LLM settings UI — we still need OC to
        # see the model's `attachment` capability so file/image parts get
        # forwarded to the provider. Previously gated on `provider in (xai, grok)`
        # which left the `models: {}` empty for xAI-as-openai users.
        is_xai_endpoint = provider in ("xai", "grok") or (api_url and "x.ai" in api_url)
        # A custom OpenAI-compatible gateway (any baseURL other than stock
        # api.openai.com) may serve models that aren't in OC's models.dev
        # catalog — e.g. self-hosted Gemma / Qwen / UI-TARS behind a vLLM
        # gateway. OC raises ProviderModelNotFoundError for any `openai/<model>`
        # it can't resolve from the catalog unless the model is declared here.
        # Leaving `models: {}` empty is exactly what broke agent startup for the
        # lumenatech gateway. Stock OpenAI model ids ARE catalogued, so restrict
        # this to non-OpenAI base URLs to avoid clobbering their real metadata.
        is_custom_openai_endpoint = bool(api_url) and "api.openai.com" not in api_url
        if provider_id == "openai" and (is_xai_endpoint or is_custom_openai_endpoint):
            custom_models[model_name] = {
                "name": model_name,
                # `attachment: true` lets OC forward file/image parts to the
                # provider as vision content blocks (or `file` blocks for PDFs).
                # Trust the provider to reject unsupported attachments — every
                # vision-capable model (Grok 4.x, GPT-4o, Claude 3+) works once
                # this is on, and text/markdown attachments inline for ANY model.
                "attachment": True,
                "reasoning": False,
                # OC reads input modalities from models.dev when the model id is
                # known there. xAI's `grok-4-1-fast-reasoning` (and other
                # newer/non-cataloged ids) aren't in models.dev's xai entry, so
                # OC defaults `capabilities.input.image` to false and strips
                # image bytes before forwarding. Declaring `modalities` here
                # makes OC use ["text","image"] explicitly. Set output text-only.
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "tool_call": True,
                "cost": {"input": 3.0, "output": 15.0},
                "limit": {"context": 131072, "output": 16384},
            }
            if small_model_name and small_model_name != model_name:
                custom_models[small_model_name] = {
                    "name": small_model_name,
                    "attachment": True,
                    "reasoning": False,
                    "cost": {"input": 1.0, "output": 5.0},
                    "limit": {"context": 131072, "output": 8192},
                }
            if plan_model_name and plan_model_name not in custom_models and plan_model_name != model_name:
                custom_models[plan_model_name] = {
                    "name": plan_model_name,
                    "attachment": True,
                    "reasoning": False,
                    "cost": {"input": 3.0, "output": 15.0},
                    "limit": {"context": 131072, "output": 16384},
                }

        # Reasoning ("Thinking") — reload-free design.
        #
        # If the selected model supports native reasoning, make it PERMANENTLY
        # reasoning-capable at the model level (`reasoning: true` +
        # `interleaved: {field: "reasoning_content"}`). This is harmless when a
        # request doesn't ask to think — the gateway just returns no
        # reasoning_content. Per-message on/off is then toggled WITHOUT any
        # OpenCode restart by routing the turn to a "<agent>-think" variant that
        # carries the body flag (e.g. chat_template_kwargs.enable_thinking).
        # `think_options` is that per-agent options patch (empty for
        # non-reasoning models, in which case the -think variants are inert).
        think_options: Dict[str, Any] = {}
        _rplan = reasoning_resolver.resolve(provider, model_name, "think")
        if _rplan.enabled:
            think_options = dict(_rplan.options_patch)
            for _mn in (model_name, plan_model_name):
                if _mn and _mn in custom_models:
                    custom_models[_mn].update(_rplan.model_patch)

        team_description = ""
        if team_id:
            try:
                from app.models.team import Team
                async with async_session_maker() as session:
                    team_result = await session.execute(
                        sa_select(Team).where(Team.team_id == team_id)
                    )
                    team = team_result.scalar_one_or_none()
                    if team and team.description:
                        team_description = team.description
            except Exception:
                pass

        today = datetime.utcnow().strftime("%Y-%m-%d")

        build_prompt = (
            f"You are ReTrace, an enterprise knowledge management assistant. Today is {today}.\n\n"
            "You help users find information, automate tasks, and manage knowledge.\n"
            "You have access to the following capabilities through MCP tools:\n"
            "- **Knowledge Base**: Search and read team knowledge base documents\n"
            "- **Browser**: Navigate web pages, take screenshots, click, type, and read content\n"
            "- **Screen Automation**: Take screenshots, click, type, and interact with desktop applications\n"
            "- **SOPs**: List, read, create, and run Standard Operating Procedures\n"
            "- **Email**: List inboxes, read emails, and send emails\n\n"
            "When using tools:\n"
            "- Always check the knowledge base first when the user asks about information that might be in team docs\n"
            "- Use the browser tool when you need to access web content or web applications\n"
            "- Create SOPs for repeatable processes the user describes\n"
            "- Be thorough but concise in your responses\n"
        )
        if team_description:
            build_prompt += (
                f"\n## Team Context\n"
                f"You are working within a team with the following description:\n"
                f"{team_description}\n"
            )

        plan_prompt = (
            f"You are ReTrace Plan Agent, an analysis and planning assistant. Today is {today}.\n\n"
            "You help users analyze problems, create plans, and review code or documents.\n"
            "You do NOT edit files or execute commands — you only analyze and advise.\n"
            "Use the knowledge base and browser tools to gather information as needed.\n"
        )

        plan_model = plan_model_name or model_name

        # MCP server processes (retrace_mcp/*.py) call into ReTrace's domain
        # logic by direct Python import — they're spawned with the same
        # PYTHONPATH so `from app.* import ...` works. No HTTP loopback, no
        # Bearer auth, no JWT expiry to chase. RETRACE_USER_ID + RETRACE_TEAM_ID
        # are enough context for tenant scoping inside the MCP servers.
        #
        # Exception: retrace-browser CAN'T use direct import — Playwright +
        # Chromium are owned by the FastAPI process so the MCP needs to RPC
        # into it. RETRACE_BACKEND_URL + RETRACE_INTERNAL_TOKEN below let
        # the MCP find the backend and authenticate to the internal
        # /api/v1/internal/browser/.../action endpoint.
        backend_url = os.environ.get("RETRACE_BACKEND_URL", "http://127.0.0.1:8000")
        # Internal token: the backend's lifespan handler writes one to
        # ~/.local/share/retrace/internal-token and also sets os.environ.
        # We prefer the env var (fast path); fall back to the file so MCP
        # subprocesses always see the same value the backend's bridge accepts.
        internal_token = os.environ.get("RETRACE_INTERNAL_TOKEN")
        if not internal_token:
            try:
                token_path = Path.home() / ".local" / "share" / "retrace" / "internal-token"
                if token_path.exists():
                    internal_token = token_path.read_text().strip() or None
                    if internal_token:
                        os.environ["RETRACE_INTERNAL_TOKEN"] = internal_token
            except Exception:
                pass
        if not internal_token:
            import secrets
            internal_token = secrets.token_urlsafe(32)
            os.environ["RETRACE_INTERNAL_TOKEN"] = internal_token

        mcp_env_base = {
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent.parent),
            "RETRACE_USER_ID": user_id,
            "RETRACE_TEAM_ID": team_id or "",
            "RETRACE_BACKEND_URL": backend_url,
            "RETRACE_INTERNAL_TOKEN": internal_token,
        }

        config: Dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "model": f"{provider_id}/{model_name}",
            "provider": {
                provider_id: {
                    "models": custom_models,
                    "options": {
                        "apiKey": api_key,
                    },
                }
            },
            "agent": {
                "build": {
                    "model": f"{provider_id}/{model_name}",
                    "prompt": build_prompt,
                    "maxSteps": max_steps,
                },
                "plan": {
                    "model": f"{provider_id}/{plan_model}",
                    "prompt": plan_prompt,
                    "maxSteps": max_steps,
                    "permission": {
                        "edit": "deny",
                        "bash": "deny",
                    },
                },
                # MCP Builder runs as its own OC agent. The forked shell.ts
                # uses ctx.agent == "mcp-builder" to run bash natively
                # (subprocess) instead of routing through the user's PTY —
                # a build is a batch job, not an interactive shell.
                "mcp-builder": {
                    "model": f"{provider_id}/{model_name}",
                    "prompt": MCP_BUILDER_OC_PROMPT,
                    "maxSteps": 80,
                    "permission": {
                        "edit": "allow",
                        "bash": "allow",
                        "external_directory": "allow",
                        "mcp": "allow",
                    },
                },
            },
            "mcp": {
                "retrace-kb": {
                    "type": "local",
                    "enabled": True,
                    "command": [sys.executable, "-m", "app.retrace_mcp.kb"],
                    "environment": {**mcp_env_base},
                },
                "retrace-browser": {
                    "type": "local",
                    "enabled": True,
                    "command": [sys.executable, "-m", "app.retrace_mcp.browser"],
                    "environment": {**mcp_env_base},
                },
                "retrace-screenops": {
                    "type": "local",
                    "enabled": True,
                    "command": [sys.executable, "-m", "app.retrace_mcp.screenops"],
                    "environment": {
                        **mcp_env_base,
                        "RETRACE_SCREENOPS_API_KEY": llm.get("screenops_api_key") or "",
                        "RETRACE_SCREENOPS_API_URL": llm.get("screenops_api_url") or "",
                        "RETRACE_SCREENOPS_MODEL": llm.get("screenops_model") or "",
                    },
                },
                "retrace-search": {
                    "type": "local",
                    "enabled": True,
                    "command": [sys.executable, "-m", "app.retrace_mcp.search"],
                    "environment": {
                        **mcp_env_base,
                        "RETRACE_SERPER_API_KEY": llm.get("serper_api_key") or "",
                        "RETRACE_LLM_API_KEY": api_key or "",
                        "RETRACE_LLM_BASE_URL": api_url or "",
                        "RETRACE_LLM_MODEL": model_name or "",
                    },
                },
                # retrace-bash MCP removed — OC's forked shell.ts now routes
                # directly through the PTY bridge (see opencode/.../tool/shell.ts).
                # Saves ~50MB per OC instance and removes the parallel-bash-tool
                # weirdness.
            },
            "permission": {
                "edit": "allow",
                # OC's `bash` runs through the forked shell.ts which routes
                # to retrace's PTY bridge (see opencode/.../tool/shell.ts).
                # Native OC subprocess spawning is bypassed.
                "bash": "allow",
                "external_directory": "allow",
                "mcp": "allow",
            },
        }

        # Reload-free "Thinking": define build-think / plan-think agent variants
        # (same model/prompt/permissions as build/plan, plus the reasoning body
        # flag via `options`). agent.py routes a Think turn to "<agent>-think".
        # Always defined so the per-turn agent switch never hits a missing
        # agent; for non-reasoning models `think_options` is empty, making the
        # variants behave identically to build/plan.
        # Thinking mode must NOT consult the knowledge base — it should reason
        # from first principles and answer directly. For each -think variant:
        #   - deny the retrace-kb tools in the agent's permission ruleset (a
        #     non-matching deny rule is harmless, so this can't break startup), and
        #   - add a prompt instruction as a reliable backup.
        _no_kb_note = (
            "\n\n## Thinking mode\n"
            "You are in Thinking mode. Do NOT call the knowledge base tools "
            "(retrace-kb) and do NOT compare against the knowledge base. Reason "
            "from first principles and answer directly."
        )
        for _src, _dst in (("build", "build-think"), ("plan", "plan-think")):
            _base = config["agent"][_src]
            config["agent"][_dst] = {
                **_base,
                "prompt": (_base.get("prompt") or "") + _no_kb_note,
                "options": dict(think_options),
                "permission": {
                    **(_base.get("permission") or config.get("permission") or {}),
                    "retrace-kb*": "deny",
                },
            }

        if small_model_name:
            config["small_model"] = f"{provider_id}/{small_model_name}"

        if compaction_auto:
            config["compaction"] = {
                "auto": True,
                "tailTurns": compaction_tail_turns,
            }

        if snapshot_enabled:
            config["snapshot"] = True

        # User-toggle-driven disables.
        #
        # The toolbar in /agent lets the user toggle individual tools on/off.
        # Each click PATCHes `settings.agent_tools_config = {"disabled": [...]}`
        # (see `app/api/settings.py:558`). Translate that list into OC's two
        # actual disable mechanisms:
        #   1. OC native tools → `permission.<key> = "deny"` (the `tools` map
        #      in `agent.<name>` is deprecated; OC's `agent.ts` normalize()
        #      collapses it into the permission block anyway).
        #   2. ReTrace's built-in MCP servers → `mcp.<name>.enabled = false`.
        agent_tools_cfg = llm.get("agent_tools_config")
        disabled_names: set[str] = set()
        if isinstance(agent_tools_cfg, dict):
            raw = agent_tools_cfg.get("disabled") or []
            if isinstance(raw, list):
                disabled_names = {str(n) for n in raw}
        # Legacy alias from older Settings exports — equivalent to retrace-kb.
        if "knowledge_base" in disabled_names:
            disabled_names.add("retrace-kb")
        # UI tool name → OC permission key. write/multiEdit collapse to `edit`
        # per OC's agent.ts normalize(). web_fetch and todo_write are renamed
        # to match OC's permission schema keys (webfetch / todowrite).
        _NATIVE_PERM_MAP = {
            "bash": "bash", "read": "read", "edit": "edit", "write": "edit",
            "multiEdit": "edit", "glob": "glob", "grep": "grep", "task": "task",
            "web_fetch": "webfetch", "todo_write": "todowrite",
        }
        _RETRACE_MCP_NAMES = {"retrace-kb", "retrace-browser", "retrace-screenops", "retrace-search"}
        for name in disabled_names:
            if name in _NATIVE_PERM_MAP:
                config["permission"][_NATIVE_PERM_MAP[name]] = "deny"
            elif name in _RETRACE_MCP_NAMES and name in config.get("mcp", {}):
                config["mcp"][name]["enabled"] = False
            # web_search / web_research / web_advanced are sub-tools of
            # retrace-search MCP and require per-tool MCP filtering — not
            # currently wired. The toolbar toggle for those three is
            # advisory-only until that lands.

        if api_url:
            config["provider"][provider_id]["options"]["baseURL"] = api_url

        # Custom OpenAI-compatible gateways (vLLM, lumenatech, xAI, etc.) speak
        # the Chat Completions API (`/v1/chat/completions`), but OC's built-in
        # `openai` provider uses `@ai-sdk/openai`, which defaults to the OpenAI
        # Responses API (`/v1/responses`). Those gateways reject the Responses
        # request shape (e.g. "input should be a string" validation errors), so
        # the agent starts but every turn fails. Pin the provider to
        # `@ai-sdk/openai-compatible` (the same package OC's own Zen models use)
        # so OC posts Chat Completions. Stock api.openai.com keeps the default.
        if provider_id == "openai" and is_custom_openai_endpoint:
            config["provider"][provider_id]["npm"] = "@ai-sdk/openai-compatible"

        user_mcp_servers = await self._get_user_mcp_servers(user_id)
        for name, srv in user_mcp_servers.items():
            config["mcp"][name] = srv

        # Local MCP servers import heavy dependencies on startup — retrace-search
        # pulls the langchain tree (`app.tools`), which takes ~35-40s to import
        # cold (no .pyc cache: PYTHONDONTWRITEBYTECODE). That exceeds OC's default
        # MCP startup/connect timeout, so the server is killed as "timed out" and
        # resolveTools stalls the whole turn. Give local servers generous headroom
        # so a slow cold import can't fail tool resolution. (OC reads `timeout`
        # per MCP server: see opencode/src/config/mcp.ts + mcp/index.ts.)
        for _srv in config["mcp"].values():
            if isinstance(_srv, dict) and _srv.get("type") == "local":
                _srv.setdefault("timeout", 90000)

        proc = self._processes.get(user_id)
        config_dir = proc.config_dir if proc else _opencode_projects_dir() / user_id
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "opencode.json"
        config_path.write_text(json.dumps(config, indent=2))

        logger.info("opencode_config_generated", user_id=user_id, path=str(config_path), model=f"{provider_id}/{model_name}", mcp_servers=list(config.get("mcp", {}).keys()))
        return config_path

    async def _get_user_mcp_servers(self, user_id: str) -> Dict[str, Any]:
        from app.db.database import async_session_maker
        from sqlalchemy import select as sa_select
        from app.models.mcp_extension import McpExtension
        from app.models.user_extension import UserExtension

        servers: Dict[str, Any] = {}
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    sa_select(UserExtension, McpExtension)
                    .join(McpExtension, UserExtension.extension_id == McpExtension.extension_id)
                    .where(UserExtension.user_id == user_id)
                    .where(UserExtension.is_enabled == True)
                    .where(McpExtension.is_enabled == True)
                )
                rows = result.all()
                for user_ext, catalog_ext in rows:
                    name = f"user-{catalog_ext.name.lower().replace(' ', '-')}"
                    srv: Dict[str, Any] = {}
                    # `transport` is stored as "stdio" or "http" (see
                    # McpExtension model). The HTTP branch must match "http" —
                    # checking "sse" here meant remote MCP extensions silently
                    # got an empty config and never reached the agent.
                    if catalog_ext.transport == "http" and catalog_ext.url:
                        srv["type"] = "remote"
                        srv["url"] = catalog_ext.url
                    elif catalog_ext.command:
                        srv["type"] = "local"
                        cmd_parts = [catalog_ext.command]
                        if catalog_ext.args_json and isinstance(catalog_ext.args_json, list):
                            cmd_parts.extend(catalog_ext.args_json)
                        srv["command"] = cmd_parts
                    # Skip malformed extensions: an HTTP ext with no url, or a
                    # stdio ext with no command, yields an empty {} — which
                    # would poison the OC config. Drop it and log instead.
                    if "type" not in srv:
                        logger.warning(
                            "opencode_user_mcp_skipped_malformed",
                            user_id=user_id,
                            extension=catalog_ext.name,
                            transport=catalog_ext.transport,
                        )
                        continue
                    merged_env = user_ext.get_merged_env_vars(catalog_ext.get_raw_env_vars())
                    if srv["type"] == "remote":
                        # Remote MCPs use `headers`, not `environment` (per
                        # OC's mcp.ts schema). `env_vars_json` for HTTP
                        # extensions is meant to be HTTP headers (see the
                        # McpExtension model docstring).
                        headers: Dict[str, str] = dict(merged_env) if merged_env else {}
                        # Bridge ReTrace's OAuth flow into OC: if we have an
                        # access token from the user's /oauth/initiate +
                        # /oauth/callback dance (stored on
                        # UserExtension.auth_config_json), hand it to OC as a
                        # standard Authorization header. Without this, OC sees
                        # an unauthenticated request, falls back to its own
                        # MCP-SDK OAuth flow, and in headless `serve` mode that
                        # flow has no live callback listener — so the MCP
                        # silently stays unauthenticated.
                        auth_cfg = user_ext.auth_config_json or {}
                        access_token = auth_cfg.get("oauth_access_token")
                        if access_token:
                            token_type = auth_cfg.get("oauth_token_type") or "Bearer"
                            headers.setdefault("Authorization", f"{token_type} {access_token}")
                            # Tell OC not to auto-trigger its own OAuth — we
                            # already provided the token. Otherwise OC may
                            # try to refresh via its 19876 callback on a 401.
                            srv["oauth"] = False
                        if headers:
                            srv["headers"] = headers
                    else:
                        # Local (stdio) MCPs: env vars get passed to the
                        # subprocess. This is the original behavior.
                        if merged_env:
                            srv["environment"] = merged_env
                    servers[name] = srv
        except Exception as e:
            logger.warning("opencode_user_mcp_failed", user_id=user_id, error=str(e))
        return servers

    async def start_for_user(
        self,
        user_id: str,
        team_id: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> int:
        async with self._get_user_lock(user_id):
            # External-OC mode: dev runs OC themselves in a terminal; retrace
            # only generates the config and routes HTTP. No spawn, no lifecycle
            # management. Multi-tenancy collapses to a single shared OC.
            external_url = os.environ.get("RETRACE_OPENCODE_EXTERNAL_URL", "").strip().rstrip("/")
            if external_url:
                from urllib.parse import urlparse
                parsed = urlparse(external_url)
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                # Always regenerate config so the dev's terminal-launched OC
                # has up-to-date MCP env vars + model settings on next OC start.
                logger.info(
                    "[OC] start_for_user: external mode — generating config only",
                    user_id=user_id, port=port, external_url=external_url,
                )
                await self.generate_config(user_id, team_id, model_override=model_override)
                # Register a stub OpenCodeProcess so get_port works. The
                # external_url flag tells start()/stop() to no-op.
                if user_id not in self._processes:
                    proc = OpenCodeProcess(user_id, port, external_url=external_url)
                    self._processes[user_id] = proc
                    self._port_map[user_id] = port
                    await proc.start()  # just logs the "run this in your terminal" hint
                return port

            # Re-check inside the lock so concurrent callers don't both spawn.
            if user_id in self._processes:
                proc = self._processes[user_id]
                if proc.process and proc.process.returncode is None:
                    logger.debug("[OC] start_for_user: already running", user_id=user_id, port=proc.port)
                    return proc.port

            port = self._alloc_port(user_id)
            logger.info("[OC] start_for_user: generating config and starting", user_id=user_id, port=port)
            await self.generate_config(user_id, team_id, model_override=model_override)
            proc = OpenCodeProcess(user_id, port)
            self._processes[user_id] = proc
            try:
                await proc.start()
            except Exception:
                # If start fails, release the port so the next attempt can reuse it.
                self._processes.pop(user_id, None)
                self._release_port(user_id)
                raise
            logger.info("[OC] start_for_user: complete", user_id=user_id, port=port)
            return port

    async def stop_for_user(self, user_id: str) -> None:
        async with self._get_user_lock(user_id):
            proc = self._processes.pop(user_id, None)
            if proc:
                logger.info("[OC] stop_for_user", user_id=user_id, port=proc.port)
                await proc.stop()
            else:
                logger.debug("[OC] stop_for_user: no process found", user_id=user_id)
            # Always release the port even if no process — defends against
            # stale port_map entries left over from an aborted start.
            self._release_port(user_id)

    async def reload_for_user(self, user_id: str) -> bool:
        """Restart a user's OpenCode so a freshly built/edited MCP extension
        reaches the /agent chat.

        OpenCode reads its `mcp` server roster from `opencode.json` when an
        instance is built; ReTrace writes that file in `generate_config`. A
        running OC therefore holds a stale roster until its config is
        regenerated — hence the restart. Replays the team_id / model the OC
        was last started with (see `_start_params`) so this does not reset the
        user's /agent team or model selection.

        No-op when the user has no running OC — their next cold start reads the
        new config anyway. Returns True if a live reload happened.
        """
        params = self._start_params.get(user_id, {})
        team_id = params.get("team_id")
        model_override = params.get("model_override")

        # External mode: the developer owns the OC process, so ReTrace cannot
        # restart it. Writing the config file is enough — OC's ConfigWatcher
        # (see opencode/src/config/config-watcher.ts) sees the file change,
        # waits for any busy session to go idle, then disposes all instances
        # so the next request rebuilds against the new config.
        external_url = os.environ.get("RETRACE_OPENCODE_EXTERNAL_URL", "").strip().rstrip("/")
        if external_url:
            await self.generate_config(user_id, team_id, model_override=model_override)
            logger.info(
                "[OC] reload_for_user: external mode — config refreshed (watcher will reload)",
                user_id=user_id,
            )
            return True

        proc = self.get_process(user_id)
        running = proc is not None and proc.process is not None and proc.process.returncode is None
        if not running:
            logger.info("[OC] reload_for_user: no running process, skipping", user_id=user_id)
            return False

        logger.info(
            "[OC] reload_for_user: restarting to pick up MCP changes",
            user_id=user_id, team_id=team_id,
        )
        await self.stop_for_user(user_id)
        await self.start_for_user(user_id, team_id=team_id, model_override=model_override)
        return True

    async def set_reasoning(self, user_id: str, mode: Optional[str]) -> bool:
        """Set the per-user "Thinking" reasoning mode (off/think/deep).

        If it differs from the mode the current opencode.json was generated with,
        regenerate the config and reload the user's OpenCode so native reasoning
        (enable_thinking + reasoning_content) takes effect. No-op (returns False)
        when unchanged — safe to call on every message send.
        """
        mode = reasoning_resolver.normalize_mode(mode)
        params = self._start_params.get(user_id)
        current = (params or {}).get("reasoning_mode", "off")
        if mode == current:
            return False
        # Persist so the next config generation (reload or cold start) bakes it in.
        if params is None:
            self._start_params[user_id] = {"team_id": None, "model_override": None, "reasoning_mode": mode}
        else:
            params["reasoning_mode"] = mode
        logger.info("[OC] set_reasoning", user_id=user_id, mode=mode, previous=current)
        # Reload only when an instance exists so OC re-reads the regenerated config.
        proc = self.get_process(user_id)
        external = bool(os.environ.get("RETRACE_OPENCODE_EXTERNAL_URL", "").strip())
        running = external or (proc is not None and proc.process is not None and proc.process.returncode is None)
        if running:
            reloaded = await self.reload_for_user(user_id)
            if reloaded:
                # reload_for_user restarts OC (stop+start) but returns before the
                # new server is accepting connections. The send handler POSTs to
                # OC immediately after this call, so block until OC is listening
                # to avoid a connection-refused race on the first reasoning turn.
                await self._wait_until_healthy(user_id)
            return reloaded
        return False

    async def _wait_until_healthy(self, user_id: str, attempts: int = 40, delay_s: float = 0.5) -> bool:
        """Poll the user's OC server until it answers (or times out ~20s)."""
        proc = self.get_process(user_id)
        if proc is None:
            return False
        for _ in range(attempts):
            if await proc.is_healthy():
                return True
            await asyncio.sleep(delay_s)
        logger.warning("[OC] _wait_until_healthy: timed out waiting for OpenCode", user_id=user_id)
        return False

    async def stop_all(self) -> None:
        for user_id in list(self._processes.keys()):
            await self.stop_for_user(user_id)

    def get_port(self, user_id: str) -> Optional[int]:
        return self._port_map.get(user_id)

    def get_process(self, user_id: str) -> Optional[OpenCodeProcess]:
        return self._processes.get(user_id)


opencode_manager = OpenCodeManager()
