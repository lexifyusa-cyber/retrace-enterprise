"""
monitor_predicate — the `fire_when` predicate engine for tool monitors.

Pure, stateless, dependency-free logic. The monitor service calls
`should_fire(...)` once per tick and uses the returned `(fire, new_state)`
tuple to decide whether to wake the agent and what to persist as
`last_predicate_state`.

Why this exists:
  Diff-only monitors are noisy for threshold/pattern requests. The CPU
  test ("alert when user CPU > 12.63%") fired 17 times when only 6 were
  true crossings — every value change fired, and the agent burned a turn
  each time re-deciding "is this above the threshold?". `fire_when` moves
  that decision server-side, BEFORE the agent is woken, so the agent only
  sees genuine events.

Predicate forms (the `spec` string):
  "changed"            fire on every output change (default; legacy behavior)
  "value > N"          numeric compare on the first number parsed from output
    also: < <= >= == !=    (e.g. "value >= 80", "value != 0")
  "contains:TEXT"      output contains the substring
  "not_contains:TEXT"  output does NOT contain the substring
  "matches:REGEX"      re.search(REGEX, output) hits
  "not_seen_for:Ns"    output unchanged for >= N seconds (stall/heartbeat)

Firing policy is per-kind (this is the part that kills the spam):
  changed       fire = changed
  threshold     fire = cond and not last_state      (edge: false->true crossing)
  contains      fire = cond and changed             (new matching content)
  matches       fire = cond and changed
  not_contains  fire = cond and not last_state      (edge: went missing)
  not_seen_for  fire = cond and not last_state       (edge: crossed staleness)
  invalid       treated as "changed" (and logged by the caller via classify)

A malformed spec never kills the monitor — it degrades to "changed".
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

import structlog

logger = structlog.get_logger()


# Predicate kinds (returned by classify()).
KIND_CHANGED = "changed"
KIND_THRESHOLD = "threshold"
KIND_CONTAINS = "contains"
KIND_NOT_CONTAINS = "not_contains"
KIND_MATCHES = "matches"
KIND_NOT_SEEN_FOR = "not_seen_for"
KIND_INVALID = "invalid"

# "value OP N" — OP is one of the six comparison operators. Longest operators
# first so ">=" isn't mis-split as ">".
_VALUE_RE = re.compile(
    r"^\s*value\s*(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$", re.IGNORECASE
)
# "not_seen_for:30s" / "not_seen_for:5m" / "not_seen_for:120" (bare = seconds)
_NOT_SEEN_RE = re.compile(
    r"^\s*not_seen_for\s*:\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", re.IGNORECASE
)
# First number anywhere in the output, for threshold comparisons. Handles a
# leading sign and decimals; ignores surrounding text ("CPU usage: 12.84%").
_FIRST_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}


def classify(spec: Optional[str]) -> str:
    """Return the predicate KIND_* for a spec string. KIND_INVALID for
    anything unparseable (caller falls back to changed)."""
    s = (spec or "").strip()
    if s == "" or s.lower() == "changed":
        return KIND_CHANGED
    if _VALUE_RE.match(s):
        return KIND_THRESHOLD
    low = s.lower()
    if low.startswith("contains:"):
        return KIND_CONTAINS if len(s) > len("contains:") else KIND_INVALID
    if low.startswith("not_contains:"):
        return KIND_NOT_CONTAINS if len(s) > len("not_contains:") else KIND_INVALID
    if low.startswith("matches:"):
        return KIND_MATCHES if len(s) > len("matches:") else KIND_INVALID
    if _NOT_SEEN_RE.match(s):
        return KIND_NOT_SEEN_FOR
    return KIND_INVALID


def _parse_first_number(text: str) -> Optional[float]:
    m = _FIRST_NUMBER_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _eval_threshold(spec: str, output: str) -> bool:
    m = _VALUE_RE.match(spec)
    if not m:
        return False
    op, rhs_s = m.group(1), m.group(2)
    lhs = _parse_first_number(output)
    if lhs is None:
        # No number in the output — condition simply isn't satisfied. Logged
        # by the caller's debug path; never raises.
        return False
    rhs = float(rhs_s)
    if op == ">":
        return lhs > rhs
    if op == "<":
        return lhs < rhs
    if op == ">=":
        return lhs >= rhs
    if op == "<=":
        return lhs <= rhs
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    return False


def _not_seen_threshold_seconds(spec: str) -> Optional[float]:
    m = _NOT_SEEN_RE.match(spec)
    if not m:
        return None
    return float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]


def _condition_met(
    spec: str,
    kind: str,
    output: str,
    *,
    changed: bool,
    seconds_since_change: Optional[float],
) -> bool:
    """Raw 'is the condition true for this tick?' — no firing policy applied."""
    if kind == KIND_CHANGED:
        return changed
    if kind == KIND_THRESHOLD:
        return _eval_threshold(spec, output)
    if kind == KIND_CONTAINS:
        needle = spec.split(":", 1)[1]
        return needle in (output or "")
    if kind == KIND_NOT_CONTAINS:
        needle = spec.split(":", 1)[1]
        return needle not in (output or "")
    if kind == KIND_MATCHES:
        pattern = spec.split(":", 1)[1]
        try:
            return re.search(pattern, output or "") is not None
        except re.error:
            # Bad regex — log once at caller; treat as not-matched so we
            # don't crash the tick.
            return False
    if kind == KIND_NOT_SEEN_FOR:
        thresh = _not_seen_threshold_seconds(spec)
        if thresh is None or seconds_since_change is None:
            return False
        return seconds_since_change >= thresh
    # KIND_INVALID handled before we get here (degrades to changed).
    return changed


def should_fire(
    spec: Optional[str],
    output: str,
    *,
    changed: bool,
    seconds_since_change: Optional[float] = None,
    last_state: bool = False,
) -> Tuple[bool, bool]:
    """Decide whether this tick should wake the agent.

    Args:
      spec: the `fire_when` string (None/"" == "changed").
      output: this tick's tool output (already normalised/capped by caller).
      changed: did the output hash differ from the previous tick? (caller
               computes this from its own hash bookkeeping).
      seconds_since_change: wall-clock seconds since the output last changed,
               or None if never changed yet. Only used by not_seen_for.
      last_state: the persisted `last_predicate_state` from the prior tick,
               used for edge-triggered predicates.

    Returns:
      (fire, new_state) — `fire` is whether to inject a turn now; `new_state`
      is what the caller must persist as `last_predicate_state` for next tick.
    """
    kind = classify(spec)
    if kind == KIND_INVALID:
        logger.warning("monitor_predicate_invalid_spec", spec=spec)
        kind = KIND_CHANGED  # safe degrade

    cond = _condition_met(
        spec or "changed",
        kind,
        output,
        changed=changed,
        seconds_since_change=seconds_since_change,
    )

    if kind == KIND_CHANGED:
        # Momentary by nature; new_state mirrors cond but is unused for this kind.
        return cond, cond
    if kind in (KIND_CONTAINS, KIND_MATCHES):
        # Fire on NEW matching content: condition true AND the output moved.
        return (cond and changed), cond
    # Edge-triggered kinds: threshold, not_contains, not_seen_for.
    fire = cond and not last_state
    return fire, cond
