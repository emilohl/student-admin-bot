"""Offline assertions for program / course disambiguation.

Exercises the bits added for issue #26 — alias scoring, multi-candidate
clarification, level/historical filters, conversation-prior carryover, and
the kurslista-backed course resolver. HTTP is mocked so this script can run
in CI without network.

Run:
    uv run python -m eval.test_program_resolution

Exits 0 on success, 1 on the first assertion failure. Add new cases to the
``CASES`` lists rather than asserting in-line — the report at the bottom
prints which case index failed.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any

from student_bot.bot import course_resolver, web_retrieval
from student_bot.bot.course_resolver import (
    _candidate_phrase,
    _fuzzy_score,
    question_has_course_intent,
    question_has_explicit_course_code,
    resolve_course_intent,
)
from student_bot.bot.memory import ConversationMemory
from student_bot.bot.web_retrieval import (
    _alias_score,
    _extract_program_candidates,
    _level_prior_from_question,
    _program_level,
    _resolve_multi_program_candidates,
    is_multi_program_clarification_assistant_message,
    merge_programme_clarification_followup,
)
from student_bot.config import get_config

_FAIL = 0


def _check(label: str, cond: bool, detail: str = "") -> None:
    global _FAIL
    if cond:
        print(f"  PASS  {label}")
    else:
        _FAIL += 1
        print(f"  FAIL  {label}: {detail}")


@contextmanager
def patched(target: Any, attr: str, value: Any):
    sentinel = object()
    orig = getattr(target, attr, sentinel)
    setattr(target, attr, value)
    try:
        yield
    finally:
        if orig is sentinel:
            delattr(target, attr)
        else:
            setattr(target, attr, orig)


# -----------------------------------------------------------------------------
# Section 1: alias_score primitive
# -----------------------------------------------------------------------------

def section_alias_score() -> None:
    print("\n[alias_score]")
    qn = "jag går årskurs 2 på teknisk fysik och funderar på masterprogram"
    qt = set(qn.split()) | {"teknisk", "fysik", "masterprogram"}

    # Coverage: 2 of {civilingenjörsutbildning, teknisk, fysik} present → 0.67
    s, _ = _alias_score("civilingenjörsutbildning i teknisk fysik", qn, qt)
    _check("CTFYS alias 2/3 strong tokens", abs(s - 2 / 3) < 0.02, f"score={s:.3f}")

    # All strong tokens present + verbatim phrase contained → 1.0 + 0.5
    qn2 = "vad är masterprogrammet i fusionsenergi och teknisk fysik"
    qt2 = set(qn2.split())
    s, v = _alias_score(
        "masterprogram, fusionsenergi och teknisk fysik", qn2, qt2
    )
    # Strong tokens are {fusionsenergi, teknisk, fysik}; all present → 1.0.
    # Verbatim phrase doesn't appear (comma vs " i "), so no bonus.
    _check("TFEPM alias all 3 strong tokens", s >= 0.99 and not v, f"score={s:.3f} verbatim={v}")

    # Empty / generic-only alias → 0
    s, _ = _alias_score("masterprogram", qn, qt)
    _check("generic-only alias scores 0", s == 0.0)


# -----------------------------------------------------------------------------
# Section 2: program_level + level_prior_from_question
# -----------------------------------------------------------------------------

def section_level_prior() -> None:
    print("\n[level / level_prior]")
    _check("CTFYS → civilingenjor", _program_level("CTFYS") == "civilingenjor")
    _check("TTFYM → master", _program_level("TTFYM") == "master")
    _check("TFEPM → master", _program_level("TFEPM") == "master")
    _check("TIELF → hogskoleingenjor", _program_level("TIELF") == "hogskoleingenjor")

    cases = [
        ("Jag går årskurs 2 på teknisk fysik och funderar på masterprogram", {"civilingenjor"}),
        ("Vilket masterprogram passar mig?", {"master"}),
        ("Jag pluggar civilingenjör i teknisk fysik", {"civilingenjor"}),
        ("What is the curriculum for Engineering Physics?", None),
        ("I'm in year 5 of my masters", {"civilingenjor", "master"}),
    ]
    for q, expected in cases:
        got = _level_prior_from_question(q)
        _check(f"level_prior {q[:40]!r:42} → {expected}", got == expected, f"got={got}")


# -----------------------------------------------------------------------------
# Section 3: _extract_program_candidates against the live alias snapshot
# -----------------------------------------------------------------------------

def section_extract_candidates() -> None:
    print("\n[extract_program_candidates]")
    cfg = get_config()

    # Issue #26 bug case: year 2 + masterprogram + Teknisk fysik → CTFYS only.
    q = "Jag heter X och går i årskurs 2 på Teknisk fysik och funderar på masterprogram."
    cands, verbatim = _extract_program_candidates(q, cfg)
    codes = [c.code for c in cands]
    _check("issue #26: candidates = [CTFYS]", codes == ["CTFYS"], f"got={codes}")

    # Ambiguous, no level signal: keep all three so the multi-candidate path
    # can drop TFEPM by intake-year recency.
    q = "Vad ingår i utbildningsplanen för Teknisk fysik?"
    cands, _ = _extract_program_candidates(q, cfg)
    codes = sorted(c.code for c in cands)
    _check("ambiguous no-level: 3 candidates", codes == ["CTFYS", "TFEPM", "TTFYM"], f"got={codes}")

    # Verbatim TFEPM suppresses alias-only matches.
    q = "Vad är TFEPM?"
    cands, verbatim = _extract_program_candidates(q, cfg)
    codes = [c.code for c in cands]
    _check("verbatim TFEPM only", codes == ["TFEPM"] and verbatim == ["TFEPM"], f"got={codes} verbatim={verbatim}")

    # Conversation prior carries the program when no other signal exists.
    q = "Vilka kurser ingår i programmet?"
    cands, _ = _extract_program_candidates(q, cfg, program_prior="CTFYS")
    codes = [c.code for c in cands]
    _check("prior carryover", codes == ["CTFYS"], f"got={codes}")

    # Nickname registry backstop fires when alias scoring drops to 0.
    # "Teknisk fysik" without level signal still scores >0 today (alias score
    # 1.0 for TTFYM). Test the curated registry directly via a query that
    # _wouldn't_ match aliases: short, ambiguous abbreviation. We fake the
    # alias snapshot to be empty for this assertion.
    real = web_retrieval._get_program_aliases
    web_retrieval._get_program_aliases = lambda c: {}  # type: ignore[assignment]
    try:
        cands, _ = _extract_program_candidates(
            "Vilken utbildningsplan har teknisk fysik?", cfg
        )
        codes = sorted(c.code for c in cands)
        _check(
            "nickname backstop yields curated candidates",
            "CTFYS" in codes and "TTFYM" in codes,
            f"got={codes}",
        )
    finally:
        web_retrieval._get_program_aliases = real  # type: ignore[assignment]


# -----------------------------------------------------------------------------
# Section 4: multi-candidate resolver — historical filter, discriminator bypass
# -----------------------------------------------------------------------------

def section_multi_candidate() -> None:
    print("\n[resolve_multi_program_candidates]")
    cfg = get_config()

    # Mock _cached_terms_for_code so the resolver doesn't hit the network.
    # CTFYS/TTFYM are current (intake 2026); TFEPM's last intake is 2010.
    fake_terms = {
        "CTFYS": ["20262", "20252", "20242"],
        "TTFYM": ["20262", "20252", "20242"],
        "TFEPM": ["20102", "20092", "20082", "20072"],
    }
    with patched(web_retrieval, "_cached_terms_for_code", lambda _cfg, code: fake_terms.get(code, [])):
        # Question without discriminator: TFEPM should be hidden in the list.
        q = "Vad ingår i utbildningsplanen för Teknisk fysik?"
        prog_roots = [
            "https://www.kth.se/student/kurser/program/CTFYS",
            "https://www.kth.se/student/kurser/program/TTFYM",
            "https://www.kth.se/student/kurser/program/TFEPM",
        ]
        res = _resolve_multi_program_candidates(cfg, q, prog_roots)
        msg = res.clarification_sv
        _check("multi-cand: clarification produced", bool(msg), repr(res))
        _check("multi-cand: TFEPM hidden when no discriminator", "TFEPM" not in msg, msg)
        _check("multi-cand: CTFYS + TTFYM listed", "CTFYS" in msg and "TTFYM" in msg, msg)

        # With discriminator "fusionsenergi", TFEPM survives + is annotated.
        q2 = "Vad är masterprogrammet i fusionsenergi och teknisk fysik?"
        res2 = _resolve_multi_program_candidates(cfg, q2, prog_roots)
        msg2 = res2.clarification_sv
        _check("multi-cand: fusionsenergi keeps TFEPM", "TFEPM" in msg2, msg2)
        _check("multi-cand: TFEPM annotated as discontinued",
               "avvecklat" in msg2, msg2)

        # Verbatim TFEPM also survives.
        q3 = "Vilken utbildningsplan har TFEPM och TTFYM?"
        res3 = _resolve_multi_program_candidates(cfg, q3, prog_roots)
        msg3 = res3.clarification_sv
        _check("multi-cand: verbatim TFEPM kept", "TFEPM" in msg3, msg3)


# -----------------------------------------------------------------------------
# Section 5: clarification-followup merging
# -----------------------------------------------------------------------------

def section_clarification_followup() -> None:
    print("\n[clarification followup merging]")
    pick_msg = "Ditt program är inte entydigt. Vilket av följande menar du?"
    round_msg = "För att visa rätt utbildningsplan för CTFYS behöver jag veta vilken antagningsomgång som gäller."
    hist_pick = [
        {"role": "user", "content": "Vad ingår i utbildningsplanen för Teknisk fysik?"},
        {"role": "assistant", "content": pick_msg},
    ]
    fused = merge_programme_clarification_followup("CTFYS", hist_pick)
    _check("program-pick fuses with prior question",
           "Teknisk fysik" in fused and "CTFYS" in fused, fused)

    hist_round = [
        {"role": "user", "content": "Vad ingår i utbildningsplanen för CTFYS?"},
        {"role": "assistant", "content": round_msg},
    ]
    fused = merge_programme_clarification_followup("HT2024", hist_round)
    _check("admission-round fuses with prior question",
           "CTFYS" in fused and "HT2024" in fused, fused)

    # Negative: an unrelated reply should NOT fuse.
    fused = merge_programme_clarification_followup("Hej hur mår du?", hist_pick)
    _check("non-pick reply doesn't fuse", fused == "Hej hur mår du?", fused)

    _check("multi-program clarification recogniser",
           is_multi_program_clarification_assistant_message(pick_msg))


# -----------------------------------------------------------------------------
# Section 6: course resolver — phrase extraction, fuzzy match, mocked HTTP
# -----------------------------------------------------------------------------

def section_course_resolver() -> None:
    print("\n[course resolver]")

    _check("course intent detected", question_has_course_intent("Vad är tentamen i linjär algebra?"))
    _check("explicit code detected", question_has_explicit_course_code("Vad är tentamen i SK1110?"))
    _check("no false-positive on program-only",
           not question_has_course_intent("Vad är CTFYS?"))

    cases = [
        ("Vilken kursbok används i elektromagnetisk fältteori?", "elektromagnetisk fältteori"),
        ("Vad är tentamen i Linjär algebra?", "linjär algebra"),
        ("What textbook is used in linear algebra?", "linear algebra"),
    ]
    for q, expected in cases:
        got = _candidate_phrase(q)
        _check(f"phrase: {q[:40]!r:42} → {expected!r}", got == expected, f"got={got!r}")

    # Fuzzy: same string scores 1.0; partial overlap > 0.4
    _check("fuzzy identical", _fuzzy_score("linjär algebra", "Linjär algebra") == 1.0)
    _check("fuzzy partial",
           _fuzzy_score("linjär algebra", "Linjär algebra, fortsättningskurs") > 0.4)

    # End-to-end with mocked HTTP fetches.
    cfg = get_config()
    fake_courses = [
        ("SF1672", "Linjär algebra"),
        ("SF1681", "Linjär algebra, fortsättningskurs"),
        ("SI1146", "Vektoranalys"),
        ("SI2360", "Analytisk mekanik och klassisk fältteori"),
    ]
    fake_terms = ["20262", "20252"]
    with (
        patched(web_retrieval, "_cached_terms_for_code", lambda _c, _code: fake_terms),
        patched(course_resolver, "_fetch_program_courses",
                lambda _c, _code, _term: fake_courses),
    ):
        # Multi-hit → clarification.
        res = resolve_course_intent(cfg, "Vad är tentamen i Linjär algebra?",
                                    program_prior="CTFYS")
        _check("course resolver: multi-hit → clarification",
               res is not None and bool(res.clarification_sv), repr(res))
        _check("course resolver: lists both Linjär algebra hits",
               "SF1672" in res.clarification_sv and "SF1681" in res.clarification_sv,
               res.clarification_sv if res else "")

        # Single hit → auto-resolve to course URL.
        res = resolve_course_intent(cfg, "Vilka föreläsningar har vi om vektoranalys?",
                                    program_prior="CTFYS")
        _check("course resolver: single-hit auto-resolve",
               res is not None and res.course_urls
               and "SI1146" in res.course_urls[0], repr(res))

        # No prior, no resolution.
        res = resolve_course_intent(cfg, "Vad är tentamen i Linjär algebra?",
                                    program_prior=None)
        _check("course resolver: no prior → None", res is None, repr(res))

        # Has explicit code → not handled by resolver.
        res = resolve_course_intent(cfg, "Vad är tentamen i SK1110?",
                                    program_prior="CTFYS")
        _check("course resolver: explicit code skipped", res is None, repr(res))


# -----------------------------------------------------------------------------
# Section 7: ConversationMemory program-code state
# -----------------------------------------------------------------------------

def section_memory() -> None:
    print("\n[memory.set/get_program_code]")
    cfg = get_config()
    mem = ConversationMemory(cfg)
    _check("initial program code is None", mem.get_program_code("u", "t") is None)
    mem.set_program_code("u", "t", "CTFYS")
    _check("set then get returns CTFYS", mem.get_program_code("u", "t") == "CTFYS")
    mem.set_program_code("u", "t", "TTFYM")
    _check("overwrite to TTFYM", mem.get_program_code("u", "t") == "TTFYM")
    mem.clear("u", "t")
    _check("clear drops the slot", mem.get_program_code("u", "t") is None)


def main() -> int:
    print("Program / course disambiguation tests (offline)")
    section_alias_score()
    section_level_prior()
    section_extract_candidates()
    section_multi_candidate()
    section_clarification_followup()
    section_course_resolver()
    section_memory()
    print(f"\n{('FAILED ' + str(_FAIL)) if _FAIL else 'OK'} — failures={_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
