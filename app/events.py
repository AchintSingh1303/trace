from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import AsyncIterator

from .config import settings

TERMINAL = {"generation_complete", "generation_error"}
RETENTION_SECONDS = 15 * 60


class EventBus:
    """In-process pub/sub keyed by cycle id, with replayable history.

    A late subscriber (page refresh, second tab) first receives everything that
    already happened, then live events. Single process only: swap for Redis
    pub/sub before running more than one worker.
    """

    def __init__(self) -> None:
        self._history: dict[int, list[dict]] = {}
        self._subs: dict[int, set[asyncio.Queue]] = {}
        self._active: set[int] = set()
        self._finished_at: dict[int, float] = {}

    def is_active(self, cycle_id: int) -> bool:
        return cycle_id in self._active

    def start(self, cycle_id: int) -> None:
        self._history[cycle_id] = []
        self._finished_at.pop(cycle_id, None)
        self._active.add(cycle_id)

    def publish(self, cycle_id: int, event_type: str, data: dict | None = None) -> dict:
        history = self._history.setdefault(cycle_id, [])
        event = {
            "seq": len(history) + 1,
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            "data": data or {},
        }
        history.append(event)
        for queue in list(self._subs.get(cycle_id, ())):
            queue.put_nowait(event)
        if event_type in TERMINAL:
            self._active.discard(cycle_id)
            self._finished_at[cycle_id] = time.time()
        return event

    def snapshot(self, cycle_id: int) -> dict:
        finished = self._finished_at.get(cycle_id)
        if finished and time.time() - finished > RETENTION_SECONDS:
            self._history.pop(cycle_id, None)
            self._finished_at.pop(cycle_id, None)
        return {
            "active": self.is_active(cycle_id),
            "events": list(self._history.get(cycle_id, [])),
        }

    async def subscribe(self, cycle_id: int) -> AsyncIterator[dict | None]:
        """Yield replayed + live events. Yields None on heartbeat ticks."""
        queue: asyncio.Queue = asyncio.Queue()
        for event in self._history.get(cycle_id, []):
            queue.put_nowait(event)
        self._subs.setdefault(cycle_id, set()).add(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=settings.heartbeat_seconds)
                except asyncio.TimeoutError:
                    if not self.is_active(cycle_id) and queue.empty():
                        return
                    yield None
                    continue
                yield event
                if event["type"] in TERMINAL:
                    return
        finally:
            self._subs.get(cycle_id, set()).discard(queue)


def sse_format(event: dict | None) -> str:
    if event is None:
        return ": heartbeat\n\n"
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


bus = EventBus()
