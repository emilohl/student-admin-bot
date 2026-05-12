"""Per-conversation short-term memory.

Key = (user_id, root_id). DM root posts use root_id="dm" so a single user
gets one rolling thread per DM channel. In channels we key on the post's
thread root so different threads don't bleed into each other.

Ring buffer of last N user/assistant pairs; entries older than TTL are dropped
on the next access. Two UX-honesty signals are surfaced for the web UI:

- `truncated` flips True the first time the ring buffer evicts an old turn,
  so the client can show "earlier turns are no longer sent to the model".
- TTL pruning leaves a short-lived tombstone keyed on the same
  (user_id, root_id); `take_expired_flag` consumes it on the first request
  after pruning so the client can show "previous context expired".
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
    program_code: str | None = None
    # Resolved admission round for this thread, parallel to program_code.
    # Persisted across turns so a follow-up that doesn't repeat the term
    # ("Are there extra eligibility courses?") still routes to the same
    # cohort's study plan.
    admission_term: str | None = None  # five-digit KTH period, e.g. "20242"
    admission_year_prefix: str | None = None  # four-digit year, e.g. "2024"
    # True once the ring buffer has dropped at least one user/assistant
    # pair to honour max_turns. Sticky for the life of the slot — once
    # context has been silently truncated, the user should keep seeing the
    # signal so they understand that the LLM no longer remembers turn 1.
    truncated: bool = False


@dataclass
class _Tombstone:
    """Marker that a slot was pruned by TTL recently. Consumed on the next
    access for (user_id, root_id) so the client can show a 'session
    expired' notice once. Tombstones themselves expire after
    `_tombstone_window_seconds` so we don't grow the dict unboundedly."""

    expired_at: float


class ConversationMemory:
    # Keep TTL tombstones around for one hour after the slot was pruned.
    # Long enough that the user who comes back from lunch gets a signal;
    # short enough that we don't accumulate dead keys for users who never
    # return.
    _TOMBSTONE_WINDOW_SECONDS = 60 * 60

    def __init__(self, cfg: Config):
        self.max_turns = cfg.memory.max_turns
        self.ttl = cfg.memory.ttl_minutes * 60
        self._store: dict[tuple[str, str], _Slot] = {}
        self._tombstones: dict[tuple[str, str], _Tombstone] = {}
        self._lock = Lock()

    def _prune(self, now: float):
        stale = [k for k, slot in self._store.items() if now - slot.last_used > self.ttl]
        for k in stale:
            # Tombstone replaces the slot. If the user comes back inside
            # _TOMBSTONE_WINDOW_SECONDS they get a "context expired" notice
            # on their first request.
            self._tombstones[k] = _Tombstone(expired_at=now)
            del self._store[k]
        # Drop very old tombstones too.
        dead_tombs = [
            k
            for k, t in self._tombstones.items()
            if now - t.expired_at > self._TOMBSTONE_WINDOW_SECONDS
        ]
        for k in dead_tombs:
            del self._tombstones[k]

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
            # First eviction flips `truncated` so the UI can surface that
            # the LLM no longer sees turn 1.
            while len(slot.turns) > 2 * self.max_turns:
                slot.turns.popleft()
                slot.truncated = True
            slot.last_used = now

    def clear(self, user_id: str, root_id: str) -> None:
        with self._lock:
            self._store.pop((user_id, root_id), None)
            # Explicit user reset is not a "session expired" event.
            self._tombstones.pop((user_id, root_id), None)

    def get_program_code(self, user_id: str, root_id: str) -> str | None:
        now = time.time()
        with self._lock:
            self._prune(now)
            slot = self._store.get((user_id, root_id))
            if not slot:
                return None
            slot.last_used = now
            return slot.program_code

    def set_program_code(self, user_id: str, root_id: str, code: str | None) -> None:
        now = time.time()
        with self._lock:
            self._prune(now)
            slot = self._store.get((user_id, root_id))
            if not slot:
                slot = _Slot(last_used=now)
                self._store[(user_id, root_id)] = slot
            slot.program_code = code
            slot.last_used = now

    def get_admission_hints(self, user_id: str, root_id: str) -> tuple[str | None, str | None]:
        """Return (exact_term, year_prefix) from the slot. Both may be None."""
        now = time.time()
        with self._lock:
            self._prune(now)
            slot = self._store.get((user_id, root_id))
            if not slot:
                return (None, None)
            slot.last_used = now
            return (slot.admission_term, slot.admission_year_prefix)

    def set_admission_hints(
        self,
        user_id: str,
        root_id: str,
        *,
        exact_term: str | None = None,
        year_prefix: str | None = None,
    ) -> None:
        """Persist a resolved admission round. Either field may be None; a
        non-None value overwrites the prior, a None leaves the prior in
        place (we don't want to forget the term just because this turn
        didn't restate it)."""
        now = time.time()
        with self._lock:
            self._prune(now)
            slot = self._store.get((user_id, root_id))
            if not slot:
                slot = _Slot(last_used=now)
                self._store[(user_id, root_id)] = slot
            if exact_term is not None:
                slot.admission_term = exact_term
            if year_prefix is not None:
                slot.admission_year_prefix = year_prefix
            slot.last_used = now

    def history_truncated(self, user_id: str, root_id: str) -> bool:
        """True if the ring buffer has ever evicted a turn from this slot.
        Sticky for the lifetime of the slot."""
        with self._lock:
            slot = self._store.get((user_id, root_id))
            return bool(slot and slot.truncated)

    def take_expired_flag(self, user_id: str, root_id: str) -> bool:
        """Consume the TTL-tombstone for this key (if any). Returns True on
        the first request after the slot was pruned by TTL, then False
        thereafter. Calling `_prune` first is required so a session that
        crosses TTL inside this call gets the tombstone before we read."""
        now = time.time()
        with self._lock:
            self._prune(now)
            return self._tombstones.pop((user_id, root_id), None) is not None


__all__ = ["ConversationMemory"]
