"""Per-conversation short-term memory.

Key = (user_id, root_id). DM root posts use root_id="dm" so a single user
gets one rolling thread per DM channel. In channels we key on the post's
thread root so different threads don't bleed into each other.

Ring buffer of last N user/assistant pairs; entries older than TTL are dropped
on the next access.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

from student_bot.config import Config


@dataclass
class _Slot:
    last_used: float
    turns: deque = field(default_factory=deque)


class ConversationMemory:
    def __init__(self, cfg: Config):
        self.max_turns = cfg.memory.max_turns
        self.ttl = cfg.memory.ttl_minutes * 60
        self._store: dict[tuple[str, str], _Slot] = {}
        self._lock = Lock()

    def _prune(self, now: float):
        stale = [k for k, slot in self._store.items() if now - slot.last_used > self.ttl]
        for k in stale:
            del self._store[k]

    def get(self, user_id: str, root_id: str) -> list[dict]:
        now = time.time()
        with self._lock:
            self._prune(now)
            slot = self._store.get((user_id, root_id))
            if not slot:
                return []
            slot.last_used = now
            return list(slot.turns)

    def append(self, user_id: str, root_id: str, role: str, content: str) -> None:
        now = time.time()
        with self._lock:
            self._prune(now)
            slot = self._store.get((user_id, root_id))
            if not slot:
                slot = _Slot(last_used=now)
                self._store[(user_id, root_id)] = slot
            slot.turns.append({"role": role, "content": content})
            # Cap to 2*max_turns entries (each turn = user + assistant).
            while len(slot.turns) > 2 * self.max_turns:
                slot.turns.popleft()
            slot.last_used = now

    def clear(self, user_id: str, root_id: str) -> None:
        with self._lock:
            self._store.pop((user_id, root_id), None)


__all__ = ["ConversationMemory"]
