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

from pathlib import Path

from student_bot.bot import course_resolver, web_retrieval
from student_bot.bot.course_resolver import (
    _candidate_phrase,
    _fuzzy_score,
    question_has_course_intent,
    question_has_explicit_course_code,
    resolve_course_intent,
)
from student_bot.bot.memory import ConversationMemory
from student_bot.bot.study_plan_atlas import get_atlas
from student_bot.bot.web_retrieval import (
    _alias_score,
    _extract_program_candidates,
    _level_prior_from_question,
    _program_level,
    _programme_term_bundle_urls,
    _resolve_multi_program_candidates,
    _studyplan_chunks_from_html,
    is_multi_program_clarification_assistant_message,
    merge_programme_clarification_followup,
)
from student_bot.config import get_config

_FIXTURES = Path(__file__).parent / "fixtures"

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
    # `q_strong_tokens` mirrors what `_extract_program_candidates` builds:
    # tokens ≥ 4 chars that aren't in the generic blocklist. For this query
    # that's {årskurs, teknisk, fysik, funderar}.
    q_strong = {"årskurs", "teknisk", "fysik", "funderar"}

    # Alias's strong tokens (after blocklist) = {teknisk, fysik};
    # both are in the query, so alias_coverage = 2/2 = 1.0.
    # Query-side coverage = 2/4 = 0.5. Score = 1.0 * 0.5 = 0.5 (no verbatim).
    s, _ = _alias_score("civilingenjörsutbildning i teknisk fysik", qn, qt, q_strong)
    _check("CTFYS alias 2/2 strong * 2/4 query coverage", abs(s - 0.5) < 0.02, f"score={s:.3f}")

    # All strong tokens present + verbatim phrase not contained → 1.0
    qn2 = "vad är masterprogrammet i fusionsenergi och teknisk fysik"
    qt2 = set(qn2.split())
    # Strong tokens here are length≥4 + non-generic = {fusionsenergi, teknisk, fysik}.
    q_strong2 = {"fusionsenergi", "teknisk", "fysik"}
    s, v = _alias_score("masterprogram, fusionsenergi och teknisk fysik", qn2, qt2, q_strong2)
    # alias_strong = {fusionsenergi, teknisk, fysik}; all present → coverage 1.0.
    # Verbatim phrase doesn't appear (comma vs " i ", "masterprogram" vs "masterprogrammet"),
    # so no bonus.
    _check("TFEPM alias all 3 strong tokens", s >= 0.99 and not v, f"score={s:.3f} verbatim={v}")

    # Generic-only alias (all tokens in blocklist) falls through the "no strong
    # tokens" branch and only scores when the alias appears verbatim as the
    # whole query — which it doesn't here.
    s, _ = _alias_score("masterprogram", qn, qt, q_strong)
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

    # Ambiguous, no level signal: keep the nickname-mapped candidates so the
    # multi-candidate path can disambiguate by intake-year recency.
    # `data/program_nicknames.json` currently maps "teknisk fysik" to
    # {CTFYS, TTFYM} — TFEPM was pruned from that nickname because it's
    # more specifically "fusionsenergi och teknisk fysik" and is only
    # reached when the user types "fusion*".
    q = "Vad ingår i utbildningsplanen för Teknisk fysik?"
    cands, _ = _extract_program_candidates(q, cfg)
    codes = sorted(c.code for c in cands)
    _check(
        "ambiguous no-level: nickname-mapped candidates",
        codes == ["CTFYS", "TTFYM"],
        f"got={codes}",
    )

    # Verbatim TFEPM suppresses alias-only matches.
    q = "Vad är TFEPM?"
    cands, verbatim = _extract_program_candidates(q, cfg)
    codes = [c.code for c in cands]
    _check(
        "verbatim TFEPM only",
        codes == ["TFEPM"] and verbatim == ["TFEPM"],
        f"got={codes} verbatim={verbatim}",
    )

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
        cands, _ = _extract_program_candidates("Vilken utbildningsplan har teknisk fysik?", cfg)
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
    with patched(
        web_retrieval, "_cached_terms_for_code", lambda _cfg, code: fake_terms.get(code, [])
    ):
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
        _check("multi-cand: TFEPM annotated as discontinued", "avvecklat" in msg2, msg2)

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
    _check(
        "program-pick fuses with prior question",
        "Teknisk fysik" in fused and "CTFYS" in fused,
        fused,
    )

    hist_round = [
        {"role": "user", "content": "Vad ingår i utbildningsplanen för CTFYS?"},
        {"role": "assistant", "content": round_msg},
    ]
    fused = merge_programme_clarification_followup("HT2024", hist_round)
    _check(
        "admission-round fuses with prior question", "CTFYS" in fused and "HT2024" in fused, fused
    )

    # Negative: an unrelated reply should NOT fuse.
    fused = merge_programme_clarification_followup("Hej hur mår du?", hist_pick)
    _check("non-pick reply doesn't fuse", fused == "Hej hur mår du?", fused)

    _check(
        "multi-program clarification recogniser",
        is_multi_program_clarification_assistant_message(pick_msg),
    )


# -----------------------------------------------------------------------------
# Section 6: course resolver — phrase extraction, fuzzy match, mocked HTTP
# -----------------------------------------------------------------------------


def section_course_resolver() -> None:
    print("\n[course resolver]")

    _check(
        "course intent detected", question_has_course_intent("Vad är tentamen i linjär algebra?")
    )
    _check("explicit code detected", question_has_explicit_course_code("Vad är tentamen i SK1110?"))
    _check("no false-positive on program-only", not question_has_course_intent("Vad är CTFYS?"))

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
    _check(
        "fuzzy partial", _fuzzy_score("linjär algebra", "Linjär algebra, fortsättningskurs") > 0.4
    )

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
        patched(course_resolver, "_fetch_program_courses", lambda _c, _code, _term: fake_courses),
    ):
        # Multi-hit → clarification.
        res = resolve_course_intent(cfg, "Vad är tentamen i Linjär algebra?", program_prior="CTFYS")
        _check(
            "course resolver: multi-hit → clarification",
            res is not None and bool(res.clarification_sv),
            repr(res),
        )
        _check(
            "course resolver: lists both Linjär algebra hits",
            "SF1672" in res.clarification_sv and "SF1681" in res.clarification_sv,
            res.clarification_sv if res else "",
        )

        # Single hit → auto-resolve to course URL.
        res = resolve_course_intent(
            cfg, "Vilka föreläsningar har vi om vektoranalys?", program_prior="CTFYS"
        )
        _check(
            "course resolver: single-hit auto-resolve",
            res is not None and res.course_urls and "SI1146" in res.course_urls[0],
            repr(res),
        )

        # No prior, no resolution.
        res = resolve_course_intent(cfg, "Vad är tentamen i Linjär algebra?", program_prior=None)
        _check("course resolver: no prior → None", res is None, repr(res))

        # Has explicit code → not handled by resolver.
        res = resolve_course_intent(cfg, "Vad är tentamen i SK1110?", program_prior="CTFYS")
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


# -----------------------------------------------------------------------------
# Section 8: study-plan atlas + structured chunker (per-section coverage)
# -----------------------------------------------------------------------------


def section_studyplan_chunks() -> None:
    print("\n[study-plan atlas + chunker]")
    atlas = get_atlas()
    _check("atlas loaded with topics", len(atlas.topics) > 0, str(len(atlas.topics)))
    _check(
        "atlas: arskursinformationAr4 -> Valbara masterprogram",
        atlas.label_for_field("arskursinformationAr4", "sv") == "Valbara masterprogram",
    )
    _check(
        "atlas: utbildningensupplagg -> Utbildningens upplägg (primary)",
        atlas.label_for_field("utbildningensupplagg", "sv") == "Utbildningens upplägg",
    )
    _check(
        "atlas: utlandsstudier -> Utbytesstudier",
        atlas.label_for_field("utlandsstudier", "sv") == "Utbytesstudier",
    )

    # Bundle reorder: /omfattning must come before any /arskursN.
    bundle = _programme_term_bundle_urls(
        "https://www.kth.se/student/kurser/program/CTFYS/20242/arskurs2"
    )
    omf_idx = next(i for i, u in enumerate(bundle) if u.endswith("/omfattning"))
    first_year_idx = next(i for i, u in enumerate(bundle) if "/arskurs" in u)
    _check(
        "bundle order: /omfattning before /arskursN",
        omf_idx < first_year_idx,
        f"omf_idx={omf_idx} first_year_idx={first_year_idx}",
    )

    # CTFYS /omfattning fixture: master-program list comes from arskursinformationAr4.
    fix = _FIXTURES / "ctfys_omfattning.html"
    if not fix.exists():
        _check("CTFYS fixture present", False, f"missing {fix}")
        return
    html = fix.read_text(encoding="utf-8")
    chunks = _studyplan_chunks_from_html(
        html,
        final_url="https://www.kth.se/student/kurser/program/CTFYS/20242/omfattning",
        fetched_at=0,
    )
    _check("CTFYS /omfattning produces chunks", len(chunks) > 0, str(len(chunks)))

    master_chunks = [c for c in chunks if "Valbara masterprogram" in c.section_path]
    _check(
        "CTFYS: at least one 'Valbara masterprogram' chunk",
        len(master_chunks) >= 1,
        f"got {len(master_chunks)}",
    )
    arskursinfo4 = [c for c in master_chunks if "arskursinformationAr4" in c.rel_source]
    _check(
        "CTFYS: arskursinformationAr4 chunk lists actual master names",
        bool(arskursinfo4)
        and any("matematik" in c.text.lower() and "fysik" in c.text.lower() for c in arskursinfo4),
        f"hits={[c.text[:80] for c in arskursinfo4]}",
    )

    # CINEK: master-program info lives in different fields. Atlas should
    # still surface findable chunks even though arskursinformationAr4 is
    # absent on this program.
    fix2 = _FIXTURES / "cinek_omfattning.html"
    if fix2.exists():
        html2 = fix2.read_text(encoding="utf-8")
        chunks2 = _studyplan_chunks_from_html(
            html2,
            final_url="https://www.kth.se/student/kurser/program/CINEK/20242/omfattning",
            fetched_at=0,
        )
        _check("CINEK /omfattning produces chunks", len(chunks2) > 0, str(len(chunks2)))
        cinek_topics = {c.section_path.split(" (")[0] for c in chunks2}
        # Issue #55 clarification: `studyProgramme.specialisations` is
        # inriktningar/spår (paths through the programme), NOT the list of
        # master programmes a civilingenjör student can apply to. The atlas
        # now labels that field as "Inriktningar (spår)" — confirm it
        # surfaces under that name, not the legacy "Valbara masterprogram".
        _check(
            "CINEK: 'Inriktningar' surfaces (via specialisations)",
            "Inriktningar" in cinek_topics,
            f"topics={sorted(cinek_topics)[:8]}",
        )
        _check(
            "CINEK: legacy 'Valbara masterprogram' no longer mislabels specialisations",
            "Valbara masterprogram" not in cinek_topics,
            f"topics={sorted(cinek_topics)[:8]}",
        )
        _check(
            "CINEK: 'Utbildningens upplägg' surfaces",
            "Utbildningens upplägg" in cinek_topics,
            f"topics={sorted(cinek_topics)[:8]}",
        )

    # Year-page chunker: each non-empty Valvillkor bucket = one chunk.
    fix3 = _FIXTURES / "ctfys_arskurs4.html"
    if fix3.exists():
        html3 = fix3.read_text(encoding="utf-8")
        chunks3 = _studyplan_chunks_from_html(
            html3,
            final_url="https://www.kth.se/student/kurser/program/CTFYS/20242/arskurs4",
            fetched_at=0,
        )
        # CTFYS year 4 is empty (master phase), but the chunker shouldn't crash.
        _check("CTFYS arskurs4 chunker runs without error", isinstance(chunks3, list))

    # CINEK year 4 is empty on the civilingenjör track (master phase). Year 1
    # ships as a single HTML string (not a structured list), so the chunker
    # falls back to a single year-level chunk. Years 2+ on CINEK use the
    # structured list shape and emit per-Valvillkor bucket chunks.
    fix4 = _FIXTURES / "cinek_arskurs1.html"
    if fix4.exists():
        html4 = fix4.read_text(encoding="utf-8")
        chunks4 = _studyplan_chunks_from_html(
            html4,
            final_url="https://www.kth.se/student/kurser/program/CINEK/20242/arskurs1",
            fetched_at=0,
        )
        year_chunks = [c for c in chunks4 if c.section_path.startswith("Årskurs 1")]
        _check(
            "CINEK arskurs1 (HTML-string shape): emits >=1 year-level chunk",
            len(year_chunks) >= 1,
            f"got {len(year_chunks)}; section_paths={[c.section_path for c in chunks4][:5]}",
        )
        _check(
            "CINEK arskurs1: chunk text mentions Obligatoriska kurser",
            any(
                "obligatoriska" in c.text.lower() or "kurskod" in c.text.lower()
                for c in year_chunks
            ),
            f"texts={[c.text[:60] for c in year_chunks]}",
        )


def main() -> int:
    print("Program / course disambiguation tests (offline)")
    section_alias_score()
    section_level_prior()
    section_extract_candidates()
    section_multi_candidate()
    section_clarification_followup()
    section_course_resolver()
    section_memory()
    section_studyplan_chunks()
    print(f"\n{('FAILED ' + str(_FAIL)) if _FAIL else 'OK'} — failures={_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
