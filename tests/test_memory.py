"""Unit tests for #47 — admission-term persistence, ring-buffer truncation
signal, and TTL-tombstone expiry signal."""

from __future__ import annotations

import pytest

import student_bot.bot.memory as memory_mod
from student_bot.bot.memory import ConversationMemory
from student_bot.config import get_config


@pytest.fixture
def mem():
    return ConversationMemory(get_config())


class _Clock:
    """Tiny monotonic clock stub for tests that need to advance time. Patches
    `time.time` inside the memory module."""

    def __init__(self, monkeypatch, t0: float = 1_000_000.0):
        self._now = t0
        monkeypatch.setattr(memory_mod.time, "time", self._tick)

    def _tick(self) -> float:
        return self._now

    def advance(self, seconds: float):
        self._now += seconds


# ---- admission-hint persistence -----------------------------------------


class TestAdmissionHintsPersistence:
    def test_get_on_empty_slot_returns_pair_of_none(self, mem):
        assert mem.get_admission_hints("u", "default") == (None, None)

    def test_round_trips_exact_term(self, mem):
        mem.set_admission_hints("u", "default", exact_term="20242")
        assert mem.get_admission_hints("u", "default") == ("20242", None)

    def test_round_trips_year_prefix(self, mem):
        mem.set_admission_hints("u", "default", year_prefix="2024")
        assert mem.get_admission_hints("u", "default") == (None, "2024")

    def test_round_trips_both(self, mem):
        mem.set_admission_hints("u", "default", exact_term="20242", year_prefix="2024")
        assert mem.get_admission_hints("u", "default") == ("20242", "2024")

    def test_setting_none_does_not_clear_existing(self, mem):
        """Symmetric with the pipeline behaviour: a turn that *doesn't*
        resolve a term should not forget the previously-resolved one."""
        mem.set_admission_hints("u", "default", exact_term="20242", year_prefix="2024")
        mem.set_admission_hints("u", "default", exact_term=None, year_prefix=None)
        assert mem.get_admission_hints("u", "default") == ("20242", "2024")

    def test_setting_new_value_overwrites(self, mem):
        mem.set_admission_hints("u", "default", exact_term="20232")
        mem.set_admission_hints("u", "default", exact_term="20242")
        assert mem.get_admission_hints("u", "default") == ("20242", None)

    def test_clear_drops_admission_hints(self, mem):
        mem.set_admission_hints("u", "default", exact_term="20242")
        mem.clear("u", "default")
        assert mem.get_admission_hints("u", "default") == (None, None)


# ---- ring-buffer truncation flag (`history_truncated`) -----------------


class TestHistoryTruncated:
    def test_false_on_empty_slot(self, mem):
        assert mem.history_truncated("u", "default") is False

    def test_false_until_buffer_exceeds_max_turns(self, mem):
        # max_turns * 2 entries fit; anything beyond pops + flips the flag.
        cap = mem.max_turns * 2
        for i in range(cap):
            role = "user" if i % 2 == 0 else "assistant"
            mem.append("u", "default", role, f"msg{i}")
        assert mem.history_truncated("u", "default") is False

    def test_true_after_eviction(self, mem):
        cap = mem.max_turns * 2
        for i in range(cap + 2):  # 2 extra → 1 pop happens
            role = "user" if i % 2 == 0 else "assistant"
            mem.append("u", "default", role, f"msg{i}")
        assert mem.history_truncated("u", "default") is True

    def test_sticky_once_truncated(self, mem):
        """The flag should not reset on subsequent appends — once the LLM
        has lost the start of the conversation, that's permanent for the
        session."""
        cap = mem.max_turns * 2
        for i in range(cap + 2):
            mem.append("u", "default", "user", f"x{i}")
        assert mem.history_truncated("u", "default") is True
        # Append one more; the flag stays True.
        mem.append("u", "default", "user", "later")
        assert mem.history_truncated("u", "default") is True

    def test_clear_resets_the_flag(self, mem):
        cap = mem.max_turns * 2
        for i in range(cap + 2):
            mem.append("u", "default", "user", f"x{i}")
        assert mem.history_truncated("u", "default") is True
        mem.clear("u", "default")
        assert mem.history_truncated("u", "default") is False


# ---- TTL tombstone (`take_expired_flag`) -------------------------------


class TestTtlExpiredFlag:
    def test_false_for_unknown_slot(self, mem):
        assert mem.take_expired_flag("u", "default") is False

    def test_false_when_slot_active(self, mem, monkeypatch):
        _Clock(monkeypatch)
        mem.append("u", "default", "user", "hi")
        assert mem.take_expired_flag("u", "default") is False

    def test_set_then_advance_past_ttl_pops_tombstone_once(self, mem, monkeypatch):
        clock = _Clock(monkeypatch)
        mem.append("u", "default", "user", "hi")
        # Advance past TTL — next access will move the slot to a tombstone.
        clock.advance(mem.ttl + 1)
        assert mem.take_expired_flag("u", "default") is True
        # Subsequent call: no tombstone left.
        assert mem.take_expired_flag("u", "default") is False

    def test_clear_does_not_leave_a_tombstone(self, mem, monkeypatch):
        """Explicit user reset is not a 'session expired' event — the user
        knows they pressed the button."""
        _Clock(monkeypatch)
        mem.append("u", "default", "user", "hi")
        mem.clear("u", "default")
        assert mem.take_expired_flag("u", "default") is False

    def test_tombstone_itself_expires_after_window(self, mem, monkeypatch):
        clock = _Clock(monkeypatch)
        mem.append("u", "default", "user", "hi")
        clock.advance(mem.ttl + 1)
        # Trigger _prune via an unrelated access so the slot moves to the
        # tombstone dict *now*. Without this, the tombstone is created on
        # the take_expired_flag call below and is therefore brand-new.
        mem.get("other", "default")
        clock.advance(mem._TOMBSTONE_WINDOW_SECONDS + 1)
        # Trigger _prune again so the old tombstone is reaped.
        mem.get("other", "default")
        assert mem.take_expired_flag("u", "default") is False

    def test_active_slot_then_inactive_then_active_again(self, mem, monkeypatch):
        """Full round-trip: a slot expires, the user comes back, gets the
        flag once, and the new slot is fresh."""
        clock = _Clock(monkeypatch)
        mem.append("u", "default", "user", "q1")
        mem.append("u", "default", "assistant", "a1")
        clock.advance(mem.ttl + 1)
        # First request after the slot has aged out:
        assert mem.get("u", "default") == []  # history is gone
        assert mem.take_expired_flag("u", "default") is True
        # User keeps talking — new slot, no carry-over.
        mem.append("u", "default", "user", "q2")
        assert mem.get("u", "default") == [{"role": "user", "content": "q2"}]
        assert mem.take_expired_flag("u", "default") is False


# ---- integration: signals coexist with the existing API ----------------


class TestSignalsCoexistWithExistingApi:
    def test_program_code_still_round_trips(self, mem):
        mem.set_program_code("u", "default", "CTMAT")
        assert mem.get_program_code("u", "default") == "CTMAT"

    def test_admission_and_program_code_independent(self, mem):
        mem.set_program_code("u", "default", "CTMAT")
        mem.set_admission_hints("u", "default", exact_term="20242")
        assert mem.get_program_code("u", "default") == "CTMAT"
        assert mem.get_admission_hints("u", "default") == ("20242", None)
