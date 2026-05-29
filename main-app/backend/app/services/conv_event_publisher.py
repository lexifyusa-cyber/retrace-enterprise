"""
Conversation Event Publisher — per-conversation pub-sub fan-out.

When retrace fires a synthetic turn (scheduled followup, URL monitor diff,
or remote-trigger webhook), the resulting OpenCode events go through the
fire handler's internal SSE drain — but the user's open browser tab has
no way to see them, because its own SSE relay closed when the previous
typed turn finished.

This publisher solves that. Each fire handler calls `publish(conv_id, raw_line)`
for every OC event line it consumes; the new `/conversation-events/<id>`
SSE endpoint subscribes to the corresponding conv_id and streams those
events to the frontend. The browser tab then renders the fired turn
exactly like a typed turn — synthetic user bubble, agent text streaming
character-by-character.

We do NOT publish from typed-turn handlers (opencode_send_message has its
own SSE response stream — duplicate publishing would cause every event to
arrive twice on the frontend). The broadcaster is fire-path-only.

Bounded queue per subscriber: when a subscriber lags (e.g. a backgrounded
tab that's not consuming), the oldest events get dropped instead of
backing up forever. A lagging tab loses fidelity but doesn't OOM the
backend.
"""

import asyncio
import collections
from typing import AsyncIterator, Dict, Set

import structlog

logger = structlog.get_logger()


# Per-subscriber queue capacity. With typical OC chunked-streaming, an
# active turn emits 50-200 events. 200 lets a tab miss ~one turn of
# events before backpressure kicks in.
_QUEUE_MAX = 200


class _Subscriber:
    """One subscriber's bounded event queue."""

    __slots__ = ("queue",)

    def __init__(self) -> None:
        # deque + asyncio.Event would be lighter, but Queue is idiomatic
        # and bounded queues handle the put/get plumbing for us. We
        # implement drop-oldest behaviour explicitly via the wrapper below.
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAX)

    def put_nowait_drop_oldest(self, line: str) -> None:
        """Enqueue or, if full, drop the oldest and enqueue the new line.
        Never blocks the publisher.
        """
        while True:
            try:
                self.queue.put_nowait(line)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    # Race: another consumer drained between checks. Retry.
                    pass


class ConvEventPublisher:
    """Fan-out events to all subscribers keyed by conversation_id."""

    def __init__(self) -> None:
        self._subs: Dict[str, Set[_Subscriber]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, conv_id: str, line: str) -> None:
        """Push an SSE line ('data: …') to every subscriber for this conv.
        Drops oldest events when a queue is full so a stuck client can't
        block the publisher.
        """
        async with self._lock:
            subs = list(self._subs.get(conv_id, ()))
        for sub in subs:
            sub.put_nowait_drop_oldest(line)

    async def subscribe(self, conv_id: str) -> AsyncIterator[str]:
        """Async iterator yielding event lines for this conv. The caller
        owns the lifecycle — when they break out of the loop or the
        consuming response closes, the subscriber is removed automatically.
        """
        sub = _Subscriber()
        async with self._lock:
            self._subs.setdefault(conv_id, set()).add(sub)
        logger.info("conv_event_subscriber_added", conv_id=conv_id, total=len(self._subs.get(conv_id, ())))
        try:
            while True:
                line = await sub.queue.get()
                yield line
        finally:
            async with self._lock:
                bucket = self._subs.get(conv_id)
                if bucket:
                    bucket.discard(sub)
                    if not bucket:
                        self._subs.pop(conv_id, None)
            logger.info("conv_event_subscriber_removed", conv_id=conv_id)

    def subscriber_count(self, conv_id: str) -> int:
        """Best-effort count for diagnostics; no lock so may briefly be
        off-by-one during subscribe/unsubscribe — fine for logging."""
        return len(self._subs.get(conv_id, ()))


conv_event_publisher = ConvEventPublisher()
