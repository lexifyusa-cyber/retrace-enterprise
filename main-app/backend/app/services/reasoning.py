"""Reasoning ("Thinking") resolver — Phase 2.

Maps the frontend's 3-state reasoning mode (off / think / deep) to the
OpenCode `opencode.json` model-entry patches needed to enable native
reasoning for the SELECTED model, in a provider-agnostic way.

The request format is always OpenAI Chat Completions; only *which* fields we
add varies per model (see `resolve`). The patches are applied by
`opencode_manager.generate_config()`:

  * `model_patch`   -> merged into the `custom_models[<model>]` entry. OpenCode
                       reads `reasoning` + `interleaved.field` to parse the
                       provider's reasoning channel into a `thinking` part.
  * `options_patch` -> merged into the model entry's `options`, which OpenCode
                       forwards into the `/v1/chat/completions` request body
                       (verified: provider.ts builds `model.options` from the
                       config entry, llm.ts merges it into `providerOptions`).

For Gemma 4 / Qwen3 / QwQ served via vLLM the gateway only emits
`reasoning_content` when `enable_thinking: true` is in the request body, so we
inject it. There is no native per-call token budget on those models, so
`think` and `deep` both simply enable thinking (the budget distinction only
applies to providers that expose an effort/budget knob — handled below).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Optional


VALID_MODES = ("off", "think", "deep")


@dataclass(frozen=True)
class ReasoningPlan:
    enabled: bool
    # Merged into the opencode.json custom_models[<model>] entry.
    model_patch: dict = field(default_factory=dict)
    # Merged into that entry's "options" (reaches the request body).
    options_patch: dict = field(default_factory=dict)
    # Coarse effort label (low/high) for providers that use an enum.
    effort: Optional[str] = None


_OFF = ReasoningPlan(enabled=False)


def normalize_mode(mode: Optional[str]) -> str:
    """Coerce arbitrary input to a valid mode; unknown -> 'off'."""
    m = (mode or "off").strip().lower()
    return m if m in VALID_MODES else "off"


def resolve(provider: str, model: str, mode: Optional[str]) -> ReasoningPlan:
    """Return the opencode.json patches for the given model + reasoning mode."""
    m = normalize_mode(mode)
    if m == "off":
        return _OFF

    name = (model or "").lower()
    effort = "high" if m == "deep" else "low"

    # OpenAI o-series / gpt-5 reasoning: effort enum, no body flag.
    if re.search(r"(^|/)o[1-9]\b", name) or "gpt-5" in name:
        return ReasoningPlan(
            enabled=True,
            model_patch={"reasoning": True},
            options_patch={"reasoningEffort": effort},
            effort=effort,
        )

    # DeepSeek-R1 / reasoner: reasons by default; just declare the channel.
    if re.search(r"deepseek-(r1|reasoner)", name):
        return ReasoningPlan(
            enabled=True,
            model_patch={"reasoning": True, "interleaved": {"field": "reasoning_content"}},
        )

    # Generic OpenAI-compatible reasoning model on vLLM (Gemma 4, Qwen3, QwQ,
    # and any custom gateway model the operator marks reasoning-capable).
    # vLLM only emits reasoning_content when enable_thinking is passed to the
    # chat template — it MUST be nested under `chat_template_kwargs`. A top-level
    # `enable_thinking` is accepted (HTTP 200) but silently ignored, so reasoning
    # would never engage. Verified against the lumenatech gateway.
    return ReasoningPlan(
        enabled=True,
        model_patch={"reasoning": True, "interleaved": {"field": "reasoning_content"}},
        options_patch={"chat_template_kwargs": {"enable_thinking": True}},
        effort=effort,
    )


def apply_to_model_entry(entry: dict, plan: ReasoningPlan) -> None:
    """Mutate a custom_models[<model>] dict in place to (de)activate reasoning.

    Always writes an explicit `reasoning` flag so toggling OFF restores the
    non-reasoning baseline (entries default to reasoning:false elsewhere).
    """
    if not plan.enabled:
        entry["reasoning"] = False
        entry.pop("interleaved", None)
        opts = entry.get("options")
        if isinstance(opts, dict):
            opts.pop("enable_thinking", None)
            opts.pop("chat_template_kwargs", None)
            opts.pop("reasoningEffort", None)
        return
    entry.update(plan.model_patch)
    if plan.options_patch:
        opts = entry.setdefault("options", {})
        if isinstance(opts, dict):
            opts.update(plan.options_patch)
