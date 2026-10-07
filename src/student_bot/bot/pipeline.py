"""Full RAG flow: lang-detect → guardrails → retrieve → gate → generate (or refuse).

Programmatic API (used by Mattermost client and web app):
    answer(question, history=[], rate_limit_key=None) -> AnswerResult

CLI:
    student-bot-cli "Hur överklagar jag ett betyg?"
    student-bot-cli --interactive
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

import click
import psutil
from rich.console import Console

from student_bot.bot.citations import (
    _chunk_dedup_key,
    apply_citation_numbering,
    confidence_badge,
    format_sources_block,
    literacy_footer,
)
from student_bot.bot.gate import GateDecision, evaluate as evaluate_gate
from student_bot.bot.llm import stream_chat
from student_bot.bot.memory import ConversationMemory
from student_bot.bot.prompts import (
    compose_messages,
    compose_meta_fallback_messages,
    empty_answer_message,
    llm_unavailable_message,
    question_is_offtopic,
    refusal_message,
)
from student_bot.bot.retrieval import RetrievalResult, RetrievedChunk, get_reranker, retrieve
from student_bot.bot.web_retrieval import (
    _question_is_master_eligibility,
    _term_label_sv,
    corpus_programme_substrings_for_query,
    history_without_programme_clarification_tail,
    maybe_fetch_dynamic_web,
    is_programme_clarification_assistant_message,
    merge_programme_clarification_followup,
    reply_admission_hints,
)
from student_bot.config import Config, get_config
from student_bot.jargon import Jargon, JargonEntry
from student_bot.lang import detect

log = logging.getLogger("student_bot")


_jargon_cache: dict[int, Jargon] = {}

# Programme codes that are also words people actually write, where the
# verbatim uppercase form is required so an ordinary sentence is not read as
# a programme code. "How many times can I retake an exam?" is a perfectly
# normal English question, and TIMES is a real code (maskinteknik och
# ekonomi); MEDIA (medieteknik) is the same story in both languages.
#
# Screening the 318 codes against /usr/share/dict/words also flagged COPEN,
# but that list is web2 (Webster's 1934, 236k entries — it has "zyzzogeton"
# and "thole"), and "copen" is an archaic colour term nobody types. It is
# deliberately NOT guarded: "copen-programmet" should resolve like any other
# code. Judge additions by whether a student would plausibly write the word,
# not by whether some dictionary lists it.
#
# This is a frozen list, not derived at runtime: the wordlist does not exist
# in the Docker image. A new KTH code that collides with a common word would
# need adding here by hand.
_AMBIGUOUS_PROGRAM_CODES = frozenset({"MEDIA", "TIMES"})

_PROGRAM_CODE_RE = re.compile(r"\b[A-Za-z]{5}\b")


def _resolve_program_codes(cfg: Config, text: str, lang: str | None = None) -> dict[str, str]:
    """Map programme codes appearing in `text` to their official long names.

    Matching is case-insensitive: students type "ctfys" about as often as
    "CTFYS", and the uppercase-only form this replaced silently skipped the
    lowercase half. Candidate tokens are filtered against the alias table
    scraped from `kth.se/student/kurser/kurser-inom-program`, so an ordinary
    five-letter word cannot be mistaken for a code — except for the few that
    genuinely are words (see `_AMBIGUOUS_PROGRAM_CODES`).

    Returns {CODE: official long name}, in `lang` when both exist.

    The alias table holds a Swedish and an English name per programme, and
    without `lang` the longest wins — which is a coin flip between them: CTFYS
    comes back Swedish, TTFYM English.

    PASS `lang` FOR THE PROMPT GLOSSARY, NOT FOR THE RETRIEVAL QUERY. The
    glossary is read by the model and should match the conversation. The query
    is matched against a corpus that is 127 Swedish files to 59 English, and
    the programme-director pages are Swedish — so expanding "Who is the
    programme director for CFATE?" with the *English* name moved it away from
    the only document that answers it, and cost a recall point. Measured, not
    assumed: recall@5 went 44/45 -> 43/45 when this was applied to both.
    """
    if not text:
        return {}
    tokens = set(_PROGRAM_CODE_RE.findall(text))
    if not tokens:
        return {}
    try:
        from student_bot.bot.web_retrieval import _get_program_aliases

        aliases = _get_program_aliases(cfg)
    except Exception as e:  # alias table is best-effort; never break a query
        log.warning("failed to load program aliases: %s", e)
        return {}

    wanted = {
        tok.upper()
        for tok in tokens
        if not (tok.upper() in _AMBIGUOUS_PROGRAM_CODES and tok != tok.upper())
    }
    by_code: dict[str, list[str]] = {}
    for alias, code in aliases.items():
        code_upper = str(code).upper()
        if code_upper not in wanted or alias.upper() == code_upper:
            continue
        by_code.setdefault(code_upper, []).append(alias)

    code_to_name: dict[str, str] = {}
    for code_upper, names in by_code.items():
        preferred = [n for n in names if _alias_language(n) == lang] if lang else []
        # Longest within the preferred language, else longest overall — the
        # short entries are nicknames, not the official name.
        code_to_name[code_upper] = max(preferred or names, key=len)
    return code_to_name


# Swedish programme names carry å/ä/ö or one of these stems; the English ones
# read "degree programme in ...", "master's programme, ...". Cheap and specific
# enough for this table, which is scraped from one KTH page in two languages.
_SV_ALIAS_RE = re.compile(
    r"[åäö]|\b(?:civilingenj|masterprogram|kandidatprogram|h\wgskoleingenj|"
    r"arkitektutbildning|utbildning|program i)\b",
    re.IGNORECASE,
)
_EN_ALIAS_RE = re.compile(
    r"\b(?:degree|programme|program|master's|bachelor's|engineering|studies)\b",
    re.IGNORECASE,
)


def _alias_language(alias: str) -> str | None:
    """ "sv", "en", or None when the alias gives no clear signal."""
    sv = bool(_SV_ALIAS_RE.search(alias))
    en = bool(_EN_ALIAS_RE.search(alias))
    if sv and not en:
        return "sv"
    if en and not sv:
        return "en"
    return None


def _expand_program_codes(text: str, code_to_name: dict[str, str]) -> str:
    """Inline each programme code's official name, mirroring the jargon
    convention: "CTFYS" -> "CTFYS (Civilingenjörsutbildning i teknisk fysik)".

    The code itself is preserved because the dynamic-web router keys off it.
    Each code is expanded only once, so a query that repeats a code does not
    accumulate duplicate names.
    """
    emitted: set[str] = set()

    def repl(m: re.Match) -> str:
        code = m.group(0).upper()
        name = code_to_name.get(code)
        if not name or code in emitted:
            return m.group(0)
        emitted.add(code)
        return f"{m.group(0)} ({name.strip().capitalize()})"

    return _PROGRAM_CODE_RE.sub(repl, text)


def _glossary_with_codes(glossary_md: str, lang: str, code_to_name: dict[str, str]) -> str:
    """Append `- CODE = Official name` lines to the prompt glossary.

    Shared by both directions: codes the student typed, and the code the
    dynamic-web router resolved from a programme's name. Returns the glossary
    unchanged when there is nothing to add.
    """
    entries = [
        f"- {code} = {name.strip().capitalize()}"
        for code, name in code_to_name.items()
        if name.strip()
    ]
    if not entries:
        return glossary_md
    label = "Ordlista" if lang == "sv" else "Glossary"
    if not glossary_md:
        return f"{label}:\n" + "\n".join(entries)
    return glossary_md + "\n" + "\n".join(entries)


def _conversation_facts(
    cfg: Config,
    lang: str,
    program_code: str | None,
    admission_term: str | None,
    admission_year_prefix: str | None,
) -> str:
    """One prompt line with what the conversation has established: the programme
    and admission year that `ConversationMemory` keeps per thread.

    The router already reads these to pick a study plan; this lets the model see
    them too once the turn that stated them has left the history. Neutral wording
    on purpose: the programme is the one the conversation is about, which need
    not be the student's own, and a question that says otherwise wins.
    """
    sv = lang == "sv"
    facts: list[str] = []
    if program_code:
        code = program_code.strip().upper()
        name = _resolve_program_codes(cfg, code, lang).get(code, "").strip()
        label = "program" if sv else "programme"
        facts.append(f"{label} {code} ({name.capitalize()})" if name else f"{label} {code}")
    if admission_term:
        label = "antagningsomgång" if sv else "admission year"
        facts.append(f"{label} {_term_label_sv(admission_term)}")
    elif admission_year_prefix:
        label = "antagningsår" if sv else "admission year"
        facts.append(f"{label} {admission_year_prefix}")
    if not facts:
        return ""
    if sv:
        return (
            f"Från samtalet hittills: {', '.join(facts)}. Använd det bara när frågan gäller "
            "det; säger frågan något annat gäller frågan."
        )
    return (
        f"From the conversation so far: {', '.join(facts)}. Use it only when the question "
        "is about it; if the question says otherwise, the question wins."
    )


def build_retrieval_query(
    cfg: Config,
    text: str,
    lang: str | None,
    *,
    jargon: Jargon | None = None,
) -> tuple[str, list[JargonEntry], dict[str, str]]:
    """Build the query string retrieval actually sees.

    Two expansions, in order: jargon ("PA" -> "PA (programansvarig)") and
    programme codes ("CFATE" -> "CFATE (Civilingenjörsutbildning i
    farkostteknik)"). Both keep the original surface form, so downstream
    consumers that key off the raw token — notably the dynamic-web router —
    still see it.

    Shared with `eval/run_eval.py` deliberately. The harness used to apply
    jargon expansion only, so it scored a different string than production
    retrieved on and could not see programme-code expansion at all. Anything
    that changes the retrieval query belongs here, not in either caller.

    Returns (expanded query, jargon hits, {CODE: official name}). Callers use
    the latter two to build the prompt glossary; retrieval needs only the
    first.
    """
    expanded = text
    hits: list[JargonEntry] = []
    if jargon is not None:
        expanded, hits = jargon.expand_query(text, lang=lang)
    code_to_name = _resolve_program_codes(cfg, text)
    if code_to_name:
        expanded = _expand_program_codes(expanded, code_to_name)
    return expanded, hits, code_to_name


_COURSE_CODE_TOKEN_RE = re.compile(r"^(?:[A-Z]{2}[0-9]{4}|[A-Z]{2}[0-9]{3}[A-Z])$")
_PROGRAM_CODE_TOKEN_RE = re.compile(r"^[A-Z]{5}$")


def _jargon(cfg: Config) -> Jargon | None:
    if not cfg.jargon.enabled:
        return None
    j = _jargon_cache.get(id(cfg))
    if j is None:
        j = Jargon.from_config(cfg)
        _jargon_cache[id(cfg)] = j
    return j


def _history_lang(history: list[dict]) -> str | None:
    for turn in reversed(history):
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        # Skip ultra-short/noisy turns to avoid inheriting from fragments.
        if len(content) < 6:
            continue
        return detect(content)
    return None


def _is_lang_ambiguous_input(question: str) -> bool:
    q = (question or "").strip()
    if not q:
        return True

    tokens = re.findall(r"[A-Za-zÅÄÖåäö0-9]+", q)
    if not tokens:
        return True

    code_like = sum(
        1
        for t in tokens
        if _COURSE_CODE_TOKEN_RE.fullmatch(t.upper()) or _PROGRAM_CODE_TOKEN_RE.fullmatch(t.upper())
    )
    alpha_words = re.findall(r"[A-Za-zÅÄÖåäö]+", q)
    lower_words = [w for w in alpha_words if not w.isupper()]
    meaningful_words = [w for w in lower_words if len(w) >= 3]

    if not meaningful_words:
        return True
    if code_like and len(meaningful_words) <= 2:
        return True
    return False


def _select_turn_lang(question: str, history: list[dict]) -> str:
    detected = detect(question)
    if not _is_lang_ambiguous_input(question):
        return detected
    inherited = _history_lang(history)
    return inherited or detected


@dataclass
class AnswerResult:
    question: str  # original user text (pre-expansion)
    lang: str
    answered: bool
    answer: str  # the model's text only (no sources/footer)
    rendered: str  # answer + sources block + footer + jargon note
    gate: GateDecision
    retrieval: RetrievalResult
    latency_ms: int
    rate_limited: bool = False
    too_long: bool = False
    # True when the gate refused but the LLM produced a self-aware fallback
    # (scope reflection / soft refusal). Worth keeping in conversation
    # memory for follow-ups, even though answered=False.
    meta_fallback: bool = False
    expanded_question: str = ""  # post-jargon-expansion query used for retrieval
    jargon_hits: list = field(default_factory=list)
    # Body with inline `[N]` citations and the chunks those numbers point to,
    # in citation order. Populated only on the answered path; empty otherwise.
    # Exposed so non-Markdown consumers (e.g. MM message attachments) can
    # re-render the Sources block without re-parsing `rendered`.
    numbered_body: str = ""
    cited_chunks: list = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    stale_cache_days: int | None = None
    context_tokens_est: int | None = None
    context_tokens_limit: int | None = None
    gen_tokens_est: int | None = None
    ttft_ms: int | None = None
    gen_tps: float | None = None
    # Five-letter KTH code resolved during this turn, when exactly one program
    # was narrowed to. Callers should persist this in conversation memory so
    # follow-up turns can reuse it as a prior (see ConversationMemory.set_program_code).
    program_code: str | None = None
    # Admission year that picked a programme's study plan this turn (the
    # router falls back to the persisted prior when the turn carries no hint).
    # Callers persist the pair via `ConversationMemory.set_admission_hints(
    # replace=True)` so a follow-up that doesn't restate the term still routes
    # to the same cohort.
    admission_term: str | None = None
    admission_year_prefix: str | None = None
    # UX-honesty signals plumbed up from `ConversationMemory`. The web UI
    # shows a small notice for each. `history_truncated` is sticky for the
    # session (ring-buffer evicted at least one turn); `session_expired`
    # fires once per pruned slot (TTL boundary crossed since last turn).
    history_truncated: bool = False
    session_expired: bool = False
    # Set when this turn asked a clarification on behalf of a question: callers
    # store it (`ConversationMemory.set_pending_question`) and pass it back as
    # `pending_question_prior` next turn. None clears it.
    pending_question: str | None = None
    # The full question when this turn folded a clarification reply into the
    # question it answered. `remember_turn` stores it in place of the
    # clarification pair.
    merged_question: str | None = None
    # Per-stage wall-clock breakdown (#19 diagnostics). chroma_ms covers
    # query embed + Chroma lookup (CPU); rerank_ms covers the cross-encoder
    # pass (CPU); llm_ms covers prompt-submit → stream-end (GPU/cloud).
    # Any of these may be None on short-circuit paths.
    chroma_ms: int | None = None
    rerank_ms: int | None = None
    llm_ms: int | None = None
    # Resident set size of the current process at end of turn, in MiB.
    # The series across turns is the OOM-watch feed for #19.
    rss_mb: int | None = None
    # When the caller passed learn_more=True and this turn reached
    # retrieval+gate+LLM, this holds the gzippable diagnostic payload to
    # persist via LogDB.record_qa_debug. None on opt-out or short-circuit.
    debug_payload: dict[str, Any] | None = None


def keep_in_history(result: AnswerResult) -> bool:
    """Whether a turn belongs in conversation memory. One rule for every frontend;
    clarifications are kept so the reply is read in their context."""
    return (
        result.answered or result.meta_fallback or result.gate.reason == "programme_clarification"
    )


def remember_turn(
    memory: ConversationMemory, user_id: str, root_id: str, question: str, result: AnswerResult
) -> None:
    """Persist one turn's outcome. Shared by the web, Mattermost and CLI frontends.

    A turn that folded a clarification reply into its question replaces the
    clarification pair with one pair holding the whole question, so a question
    costs one turn of memory however many clarifications it took. Sets
    `result.history_truncated` from the post-turn state."""
    if keep_in_history(result):
        if result.merged_question:
            memory.replace_last_pair(user_id, root_id, result.merged_question, result.answer)
        else:
            memory.append(user_id, root_id, "user", question)
            memory.append(user_id, root_id, "assistant", result.answer)
        # The pending question lives exactly as long as the clarification it
        # belongs to is the last stored turn. A turn that is not stored (refused
        # input, unknown programme code, kth.se down, LLM error) leaves both in
        # place, so the student's retry is still read as the reply.
        memory.set_pending_question(user_id, root_id, result.pending_question)
    if result.program_code:
        memory.set_program_code(user_id, root_id, result.program_code)
    if result.admission_term or result.admission_year_prefix:
        memory.set_admission_hints(
            user_id,
            root_id,
            exact_term=result.admission_term,
            year_prefix=result.admission_year_prefix,
            replace=True,
        )
    result.history_truncated = memory.history_truncated(user_id, root_id)


def _estimate_tokens(text: str) -> int:
    # Coarse heuristic for UI telemetry; avoids model-specific tokenizers.
    return max(0, int(round(len(text or "") / 4)))


def _estimate_context_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += _estimate_tokens(m.get("content", ""))
        total += 3  # rough message framing overhead
    return total


# Reuse one Process handle; psutil caches the proc lookup, but the explicit
# module-level handle keeps the per-turn call to memory_info() cheap (~50 µs).
_PROC = psutil.Process()


def _rss_mb() -> int | None:
    """Snapshot current-process RSS in mebibytes. None if psutil hiccups."""
    try:
        return int(_PROC.memory_info().rss / (1024 * 1024))
    except Exception:
        return None


# How much of each retrieved chunk's body to keep in the debug payload.
# 400 chars is enough to recognise the section without bloating the per-user
# 1 MiB cap with full chunk texts; the chunk_id round-trips so the UI can
# request the full text on demand if we ever wire that up.
_DEBUG_SNIPPET_MAX = 400


def _debug_chunk(c: RetrievedChunk, include_rerank: bool) -> dict[str, Any]:
    snippet = (c.text or "").strip().replace("\n", " ")
    if len(snippet) > _DEBUG_SNIPPET_MAX:
        snippet = snippet[:_DEBUG_SNIPPET_MAX] + "…"
    out: dict[str, Any] = {
        "id": c.chunk_id,
        "doc_title": c.doc_title,
        "section_path": c.section_path,
        "page": c.page_start,
        "rel_source": c.rel_source,
        "chroma_distance": round(c.chroma_distance, 4),
        "snippet": snippet,
    }
    if include_rerank:
        out["rerank_score"] = round(c.rerank_score, 4)
    return out


def _build_debug_payload(
    *,
    lang: str,
    expanded_q: str,
    jargon_hits: list[JargonEntry],
    retrieval: RetrievalResult,
    gate: GateDecision,
    messages: list[dict],
    model_identifier: str,
    prompt_tokens_est: int,
    chroma_ms: int | None,
    rerank_ms: int | None,
    llm_ms: int | None,
    rss_mb: int | None,
) -> dict[str, Any]:
    """Assemble the JSON payload persisted to qa_debug for opt-in turns."""
    return {
        "routing": {
            "lang": lang,
            "jargon_hits": [{"key": j.key, "term": j.term} for j in jargon_hits],
            "expanded_query": expanded_q,
        },
        "retrieval": {
            "candidates": [_debug_chunk(c, include_rerank=False) for c in retrieval.candidates],
            "reranked": [_debug_chunk(c, include_rerank=True) for c in retrieval.reranked],
        },
        "gate": {
            "top1": round(gate.top1, 4),
            "meanK": round(gate.meanK, 4),
            "distinct_sources": gate.distinct_sources,
            "pass": bool(gate.passed),
            "reason": gate.reason,
        },
        "llm": {
            "messages": messages,
            "model": model_identifier,
            "prompt_tokens_est": prompt_tokens_est,
        },
        "stages": {
            "chroma_ms": chroma_ms,
            "rerank_ms": rerank_ms,
            "llm_ms": llm_ms,
        },
        "host": {"rss_mb": rss_mb},
    }


# How many corpus chunks to merge into the LLM context when a web fetch
# also fired. Hand-curated markdown (FAQ.md, etc.) often directly answers
# the same question the web fetch was triggered for; pulling 2 top corpus
# chunks gives the LLM a grounded baseline alongside the structured live
# pages. The score floor keeps weak matches from displacing the web result.
_CORPUS_MERGE_KEEP = 2
_CORPUS_MERGE_MIN_SCORE = 0.0


# Intent-aware boosts applied to web-chunk rerank scores before sorting.
# Cross-encoders score on text similarity, which for the CTFYS master-mapping
# question routinely surfaces "Villkor för deltagande" above the actual
# master-list chunks — both contain the trigger words. A targeted nudge
# fixes the ordering without retraining anything.
_MASTER_MAPPING_SECTION_BONUS = 3.0
_SPECIALISATIONS_SECTION_PENALTY = -2.0
_MASTER_SECTION_TOKENS = (
    "behörighetsgivande kurser per masterprogram",
    "valbara masterprogram",
    "available master programs",
    "arskursinformationar4",
    "arskursinformationar5",
    "eligibilityrequirementsmasterprograms",
)
_SPECIALISATIONS_SECTION_TOKENS = (
    "inriktningar",
    "specialisations",
    "specializations",
    "spår",
    "tracks",
    # `studyProgramme.specialisations` shows up here when atlas-labelled.
    "fält: specialisations",
)


def _master_intent_score_adjust(chunk: "RetrievedChunk") -> float:
    """Boost authoritative master-mapping chunks; penalise specialisation
    chunks. Used only when the question is master-eligibility shaped so
    these adjustments don't bleed into other intents."""
    label = " ".join(filter(None, (chunk.section_path, chunk.doc_title))).lower()
    if any(token in label for token in _SPECIALISATIONS_SECTION_TOKENS):
        return _SPECIALISATIONS_SECTION_PENALTY
    if any(token in label for token in _MASTER_SECTION_TOKENS):
        return _MASTER_MAPPING_SECTION_BONUS
    return 0.0


def _rerank_web_chunks(
    cfg: Config,
    query: str,
    query_language: str,
    chunks: list[RetrievedChunk],
) -> list[RetrievedChunk]:
    """Score web-fetched chunks with the cross-encoder and return top-K.

    Web fetches (esp. studyplan bundles) can produce 30+ chunks per page with
    a flat synthetic rerank_score; without this step every chunk would be
    stuffed into the prompt, regularly overrunning ``num_ctx``. Reranker
    failure is non-fatal: we fall back to the original order and the
    top-``cfg.reranker.keep`` slice.

    On master-eligibility questions, applies a targeted boost to authoritative
    master-mapping sections (``eligibilityRequirementsMasterPrograms``,
    ``arskursinformationAr4/Ar5``) and penalises ``specialisations``-derived
    chunks — those are inriktningar/tracks, not the master-programme list.
    """
    if not chunks:
        return []
    keep = max(1, cfg.reranker.keep)
    is_master_intent = _question_is_master_eligibility(query)
    try:
        pairs = [(query, c.text) for c in chunks]
        scores = get_reranker(cfg).predict(pairs, batch_size=cfg.reranker.batch_size).tolist()
        for c, s in zip(chunks, scores):
            c.rerank_score = float(s)
        if query_language and cfg.reranker.language_bonus:
            for c in chunks:
                if c.language and c.language == query_language:
                    c.rerank_score += cfg.reranker.language_bonus
        if is_master_intent:
            for c in chunks:
                c.rerank_score += _master_intent_score_adjust(c)

        # Boost chunks belonging to a programme code mentioned in the query
        prog_codes = {w for w in re.findall(r"\b([A-Z]{5})\b", query)}
        if prog_codes:
            try:
                from student_bot.bot.web_retrieval import _get_program_aliases

                aliases = _get_program_aliases(cfg)
                valid_codes = {str(v).upper() for v in aliases.values()}
                prog_codes = prog_codes.intersection(valid_codes)
            except Exception as e:
                log.warning("failed to fetch valid program codes for rerank boost: %s", e)

            if prog_codes:
                for c in chunks:
                    c_upper_src = (c.rel_source or "").upper()
                    c_upper_url = (c.source_url or "").upper()
                    c_upper_id = (c.chunk_id or "").upper()
                    if any(
                        code in c_upper_src or code in c_upper_url or code in c_upper_id
                        for code in prog_codes
                    ):
                        c.rerank_score += 4.0

        chunks.sort(key=lambda c: c.rerank_score, reverse=True)
    except Exception as e:
        log.warning("dynamic-web rerank failed, keeping original order: %s", e)
    # Pre-prompt dedup: collapse chunks with identical text+source so the LLM
    # doesn't see the same paragraph twice (study-plan bundles can emit the
    # same JSON block under different per-year chunk titles). Iterates in
    # sorted order so the highest-scored survivor keeps its slot.
    deduped: list[RetrievedChunk] = []
    seen: set = set()
    for c in chunks:
        key = _chunk_dedup_key(c)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
        if len(deduped) >= keep:
            break
    reranked = deduped
    if len(chunks) > len(reranked):
        top1 = reranked[0].rerank_score if reranked else 0.0
        log.info(
            "dynamic-web: reranked %d -> %d chunks (top1=%.3f, master_intent=%s)",
            len(chunks),
            len(reranked),
            top1,
            is_master_intent,
        )
    return reranked


# --- Per-key rate limiter (simple sliding window over the last 60 s) ---


@dataclass
class _RateLimiter:
    cfg: Config
    _hits: dict[str, deque] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def allow(self, key: str) -> bool:
        if not key:
            return True
        limit = self.cfg.guardrails.rate_limit_per_minute
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and now - dq[0] > 60:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True


_rate_limiters: dict[int, _RateLimiter] = {}


def _rate_limiter(cfg: Config) -> _RateLimiter:
    key = id(cfg)
    rl = _rate_limiters.get(key)
    if rl is None:
        rl = _RateLimiter(cfg)
        _rate_limiters[key] = rl
    return rl


def _too_long_message(cfg: Config, lang: str) -> str:
    cap = cfg.guardrails.input_max_chars
    if lang == "en":
        return (
            f"Your message is too long ({cap} character limit). "
            "Please ask a shorter, more focused question."
        )
    return f"Frågan är för lång (max {cap} tecken). Ställ en kortare, mer fokuserad fråga."


def _rate_limited_message(cfg: Config, lang: str) -> str:
    n = cfg.guardrails.rate_limit_per_minute
    if lang == "en":
        return f"Slow down — you can ask up to {n} questions per minute."
    return f"Lugna ner dig lite – högst {n} frågor per minut."


def _render(
    cfg: Config,
    lang: str,
    body: str,
    chunks: list[RetrievedChunk],
    gate: GateDecision,
    *,
    include_sources: bool,
    jargon_note: str = "",
    channel: str = "mattermost",
) -> str:
    # Order: [jargon] + body + [conf badge] + [sources] + tip. Keeping
    # everything *after* the body makes the streaming tail (= rendered
    # minus already-streamed prefix) a clean suffix. Citation numbering
    # is applied separately by pipeline.answer() in the answered path,
    # because rewriting body here would break the streaming-tail math.
    parts: list[str] = []
    if jargon_note and cfg.jargon.show_transparency_note:
        parts.append(jargon_note + "\n\n")
    parts.append(body)
    if cfg.guardrails.show_confidence_badge and include_sources:
        label = "Tillförlitlighet" if lang == "sv" else "Confidence"
        parts.append(f"\n\n_{label}: {confidence_badge(lang, gate.top1)}_")
    if include_sources:
        sources = format_sources_block(cfg, chunks, lang)
        if sources:
            parts.append(sources)
    parts.append("\n\n" + literacy_footer(lang, channel=channel))
    return "".join(parts).strip()


def _emit_jargon_prefix(jargon_note: str, on_jargon_prefix, on_token) -> None:
    payload = jargon_note + "\n\n"
    if on_jargon_prefix:
        on_jargon_prefix(payload)
    elif on_token:
        on_token(payload)


def answer(
    question: str,
    history: list[dict] | None = None,
    cfg: Config | None = None,
    on_token=None,
    on_thinking=None,
    on_jargon_prefix=None,
    rate_limit_key: str | None = None,
    program_prior: str | None = None,
    admission_term_prior: str | None = None,
    admission_year_prefix_prior: str | None = None,
    channel: str = "mattermost",
    learn_more: bool = False,
    pending_question_prior: str | None = None,
) -> AnswerResult:
    cfg = cfg or get_config()
    history = history or []
    t0 = time.monotonic()

    # --- guardrails: input length cap and per-user rate limit ---
    lang = _select_turn_lang(question, history)
    if cfg.guardrails.input_max_chars and len(question) > cfg.guardrails.input_max_chars:
        msg = _too_long_message(cfg, lang)
        if on_token:
            on_token(msg)
        return AnswerResult(
            question=question,
            lang=lang,
            answered=False,
            answer=msg,
            rendered=msg,
            gate=GateDecision(False, "input_too_long", 0.0, 0.0, 0),
            retrieval=RetrievalResult(query=question),
            latency_ms=int((time.monotonic() - t0) * 1000),
            too_long=True,
        )
    if rate_limit_key and not _rate_limiter(cfg).allow(rate_limit_key):
        msg = _rate_limited_message(cfg, lang)
        if on_token:
            on_token(msg)
        return AnswerResult(
            question=question,
            lang=lang,
            answered=False,
            answer=msg,
            rendered=msg,
            gate=GateDecision(False, "rate_limited", 0.0, 0.0, 0),
            retrieval=RetrievalResult(query=question),
            latency_ms=int((time.monotonic() - t0) * 1000),
            rate_limited=True,
        )

    # Only right after a real clarification (it left a pending question). The
    # text detectors behind the merge also match an ordinary answer that
    # mentions "antagningsomgång": merging there fused a reply like "Förlåt,
    # jag menar HT2024" with the previous question and routed it to the old
    # admission year.
    contextual_q = (
        merge_programme_clarification_followup(
            question, history, cfg, pending_question=pending_question_prior
        )
        if pending_question_prior
        else question
    )
    programme_followup_merged = contextual_q != question
    merged_question = contextual_q if programme_followup_merged else None
    # The admission year the reply states, if any. The router uses it over any year
    # earlier in the joined text, so the newest answer decides.
    reply_admission = reply_admission_hints(question) if programme_followup_merged else None
    if reply_admission is not None and not (
        reply_admission.exact_term or reply_admission.year_prefix
    ):
        reply_admission = None
    history_for_llm = history_without_programme_clarification_tail(
        history, programme_followup_merged
    )

    # --- expand query for retrieval, build glossary for prompt ---
    jargon = _jargon(cfg)
    expanded_q, jargon_hits, code_to_name = build_retrieval_query(
        cfg, contextual_q, lang, jargon=jargon
    )
    glossary_md = ""
    jargon_note = ""
    if jargon is not None and jargon_hits:
        glossary_md = jargon.glossary_block(
            jargon_hits,
            lang,
            max_entries=cfg.jargon.max_glossary_entries,
        )
        jargon_note = jargon.transparency_note(jargon_hits, lang)

    # Codes resolved above also go into the prompt glossary, so the model can
    # name the programme it is answering about. Resolved again with `lang`:
    # the glossary should name the programme in the conversation's language,
    # while the retrieval query deliberately does not — see
    # `_resolve_program_codes`.
    glossary_md = _glossary_with_codes(
        glossary_md, lang, _resolve_program_codes(cfg, contextual_q, lang)
    )

    web_result = maybe_fetch_dynamic_web(
        cfg,
        expanded_q,
        lang,
        program_prior=program_prior,
        admission_term_prior=admission_term_prior,
        admission_year_prefix_prior=admission_year_prefix_prior,
        admission_hints_override=reply_admission,
    )
    resolved_program_code = web_result.resolved_program_code if web_result else None

    # A question can name a programme without ever typing its code — "vad har
    # masterprogrammet i teknisk fysik för programkod?". The router resolves
    # that to TTFYM in order to pick a URL, but until now the code went no
    # further than the URL: the model received the fetched page and the links,
    # never the code itself, so it answered that the information was not in the
    # context. It was not — it was in the router. See issue #85.
    #
    # `code_to_name` above only covers codes present in the question text, so
    # this is the reverse direction and has to run after the fetch.
    if resolved_program_code and resolved_program_code.upper() not in code_to_name:
        glossary_md = _glossary_with_codes(
            glossary_md, lang, _resolve_program_codes(cfg, resolved_program_code.upper(), lang)
        )
    # Design decision: students ask about themselves. An admission year counts only when
    # it picked a programme's study plan this turn, and then it is taken as the
    # student's own and replaces the stored one (`remember_turn` stores it with
    # `replace=True`). So "Jag började HT2023, vilka kurser har jag i årskurs
    # 2?" moves the student to HT2023 — as does "vad gällde för de som började
    # HT2023 på CTFYS?", the accepted cost, and a semester in a message the
    # router sends to the stored programme's plan ("valfria kurser i årskurs 3
    # VT2025?"). A course-only fetch reports the parsed hints too and does not
    # count, and neither does a message that routes to no study plan: a bare
    # "Förlåt, jag menar HT2024" changes nothing until the admission year is used for a
    # programme again.
    routed_programme = bool(web_result and resolved_program_code)
    applied_admission_term = web_result.applied_admission_term if routed_programme else None
    applied_admission_year_prefix = (
        web_result.applied_admission_year_prefix if routed_programme else None
    )
    # Only what earlier turns established: the current message speaks for
    # itself, and a single-turn question gets the same prompt as before.
    facts = _conversation_facts(
        cfg, lang, program_prior, admission_term_prior, admission_year_prefix_prior
    )
    source_urls: list[str] = []
    stale_cache_days: int | None = None
    if web_result and web_result.clarification:
        msg = web_result.clarification[0] if lang == "sv" else web_result.clarification[1]
        if on_token:
            on_token(msg)
        # An admission year the student just gave that the programme doesn't have is not
        # kept: the pending question and the stored pair stay as they were, so
        # the rejected year never reaches memory, the next joined text or the
        # prompt (left in, the model answered with the wrong courses). Only
        # for a reply to the admission year question: a programme pick that also names
        # a missing year ("CTFYS, jag började 2019") keeps the pick, and the
        # next reply's admission year still decides the routing.
        if (
            reply_admission
            and web_result.admission_not_found
            and is_programme_clarification_assistant_message(history[-1].get("content", ""))
        ):
            contextual_q = merged_question = pending_question_prior
        return AnswerResult(
            question=question,
            lang=lang,
            answered=False,
            answer=msg,
            rendered=msg,
            gate=GateDecision(False, "programme_clarification", 0.0, 0.0, 0),
            retrieval=RetrievalResult(query=expanded_q),
            latency_ms=int((time.monotonic() - t0) * 1000),
            expanded_question=expanded_q,
            jargon_hits=jargon_hits,
            program_code=resolved_program_code,
            pending_question=contextual_q,
            merged_question=merged_question,
        )
    if web_result and web_result.missing_kth_course:
        msg = web_result.missing_kth_course[0] if lang == "sv" else web_result.missing_kth_course[1]
        if on_token:
            on_token(msg)
        return AnswerResult(
            question=question,
            lang=lang,
            answered=False,
            answer=msg,
            rendered=msg,
            gate=GateDecision(False, "kth_course_not_found", 0.0, 0.0, 0),
            retrieval=RetrievalResult(query=expanded_q),
            latency_ms=int((time.monotonic() - t0) * 1000),
            expanded_question=expanded_q,
            jargon_hits=jargon_hits,
        )
    if web_result and web_result.missing_kth_program:
        msg = (
            web_result.missing_kth_program[0] if lang == "sv" else web_result.missing_kth_program[1]
        )
        if on_token:
            on_token(msg)
        return AnswerResult(
            question=question,
            lang=lang,
            answered=False,
            answer=msg,
            rendered=msg,
            gate=GateDecision(False, "kth_program_not_found", 0.0, 0.0, 0),
            retrieval=RetrievalResult(query=expanded_q),
            latency_ms=int((time.monotonic() - t0) * 1000),
            expanded_question=expanded_q,
            jargon_hits=jargon_hits,
        )
    if web_result and web_result.chunks:
        web_candidates = list(web_result.chunks)
        web_rerank_t0 = time.monotonic()
        reranked_web = _rerank_web_chunks(cfg, expanded_q, lang, web_candidates)
        web_rerank_ms = int((time.monotonic() - web_rerank_t0) * 1000)
        # Merge in the top corpus (Chroma) chunks instead of replacing them.
        # The FAQ.md and other hand-curated markdown chunks often answer the
        # very question that triggered the web fetch — silently dropping them
        # was a bug. Take up to `_CORPUS_MERGE_KEEP` chunks whose rerank score
        # clears `_CORPUS_MERGE_MIN_SCORE`, dedupe against the web set, and
        # cap the merged list at `keep + corpus_merge_keep` so we don't
        # blow the prompt budget.
        merged = list(reranked_web)
        try:
            corpus_terms = corpus_programme_substrings_for_query(expanded_q)
            corpus_result = retrieve(
                cfg,
                expanded_q,
                corpus_programme_substrings=corpus_terms,
                query_language=lang,
            )
        except Exception as e:
            log.warning("dynamic-web: corpus-side retrieve failed during merge: %s", e)
            corpus_result = None
        if corpus_result and corpus_result.reranked:
            seen = {_chunk_dedup_key(c) for c in merged}
            # When the question is master-eligibility shaped, only merge
            # corpus chunks that are also on-topic. Otherwise the merge can
            # inject off-topic candidates (e.g. an unrelated FAQ section
            # that scored high for the question's surface tokens) and
            # crowd the prompt with noise the LLM may then ground in.
            require_master_topic = _question_is_master_eligibility(expanded_q)
            added = 0
            for c in corpus_result.reranked:
                if added >= _CORPUS_MERGE_KEEP:
                    break
                if c.rerank_score < _CORPUS_MERGE_MIN_SCORE:
                    continue
                if require_master_topic and _master_intent_score_adjust(c) <= 0:
                    continue
                key = _chunk_dedup_key(c)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(c)
                added += 1
            if added:
                log.info("dynamic-web: merged %d corpus chunks into web result", added)
        # Web-fetched candidates don't pass through Chroma; propagate the
        # corpus-side chroma_ms (if a corpus merge ran) plus the combined
        # rerank time (web + corpus rerank passes are both CPU cross-encoder).
        web_corpus_chroma_ms = corpus_result.chroma_ms if corpus_result else None
        web_corpus_rerank_ms = corpus_result.rerank_ms if corpus_result else None
        retrieval = RetrievalResult(
            query=expanded_q,
            candidates=web_candidates,
            reranked=merged,
            chroma_ms=web_corpus_chroma_ms,
            rerank_ms=web_rerank_ms + (web_corpus_rerank_ms or 0),
        )
        # Preserve the synthetic web gate (web-fetched content always passes);
        # the 3.5/2.5 values feed the confidence badge and are intentionally
        # independent of the per-chunk rerank logits.
        gate = GateDecision(
            True,
            "web_cache" if web_result.used_stale_cache else "web_live",
            3.5 if not web_result.used_stale_cache else 2.5,
            3.5 if not web_result.used_stale_cache else 2.5,
            len({c.rel_source for c in reranked_web}),
        )
        source_urls = list(web_result.source_urls)
        if web_result.used_stale_cache:
            stale_cache_days = web_result.stale_age_days
    elif web_result and web_result.failure_url:
        msg = (
            "KTH-sidan kunde inte nås just nu och ingen färsk cache finns. "
            f"Prova gärna länken direkt: {web_result.failure_url}"
            if lang == "sv"
            else "The KTH page could not be reached and no recent cache exists. "
            f"Try opening the URL directly: {web_result.failure_url}"
        )
        if on_token:
            on_token(msg)
        return AnswerResult(
            question=question,
            lang=lang,
            answered=False,
            answer=msg,
            rendered=msg,
            gate=GateDecision(False, "web_unreachable_no_cache", 0.0, 0.0, 0),
            retrieval=RetrievalResult(query=expanded_q),
            latency_ms=int((time.monotonic() - t0) * 1000),
            expanded_question=expanded_q,
            jargon_hits=jargon_hits,
        )
    else:
        corpus_terms = corpus_programme_substrings_for_query(expanded_q)
        retrieval = retrieve(
            cfg,
            expanded_q,
            corpus_programme_substrings=corpus_terms,
            query_language=lang,
        )
        gate = evaluate_gate(cfg, retrieval)

    if not gate.passed:
        # Run a single LLM call with a self-aware system prompt and no
        # retrieved context, so the bot can either reflect on its scope
        # (when the user asks about it) or politely decline (when the
        # question is genuinely off-topic). If the LLM itself is
        # unreachable we surface a service-unavailable error rather than
        # a refusal — refusing would mis-attribute an outage to scope.
        # The same off-topic test that decides the canned refusal's wording,
        # applied to the prompt that actually produces the answer here (#84).
        offer_counselor = not question_is_offtopic(cfg, gate.top1)
        meta_messages = compose_meta_fallback_messages(
            cfg, lang, history_for_llm, expanded_q, offer_counselor=offer_counselor, facts=facts
        )
        if jargon_note and cfg.jargon.show_transparency_note:
            _emit_jargon_prefix(jargon_note, on_jargon_prefix, on_token)
        body = ""
        meta_fallback = False
        llm_error = False
        ttft_ms: int | None = None
        gen_tps: float | None = None
        llm_ms: int | None = None
        gen_tokens_est = 0
        context_tokens_est = _estimate_context_tokens(meta_messages)
        try:
            parts: list[str] = []
            stream_t0 = time.monotonic()
            first_tok_at: float | None = None
            for delta in _stream_answer(cfg, meta_messages, on_thinking=on_thinking):
                parts.append(delta)
                if first_tok_at is None and delta:
                    first_tok_at = time.monotonic()
                if on_token:
                    on_token(delta)
            llm_ms = int((time.monotonic() - stream_t0) * 1000)
            body = "".join(parts).strip()
            meta_fallback = bool(body)
            gen_tokens_est = _estimate_tokens(body)
            if first_tok_at is not None:
                ttft_ms = int((first_tok_at - stream_t0) * 1000)
                gen_secs = max(0.001, time.monotonic() - first_tok_at)
                gen_tps = gen_tokens_est / gen_secs if gen_tokens_est else 0.0
        except Exception as e:
            log.warning("meta-fallback LLM call failed: %s", e)
            llm_error = True
        if not body:
            # Only offer the counselor when the question is plausibly in
            # scope. Far below the gate it is not, and the referral would send
            # someone to a person who cannot help — see gate.offtopic_top1_max.
            body = (
                llm_unavailable_message(lang)
                if llm_error
                else refusal_message(cfg, lang, offer_counselor=offer_counselor)
            )
            if on_token:
                on_token(body)
        rendered = _render(
            cfg,
            lang,
            body,
            [],
            gate,
            include_sources=False,
            jargon_note=jargon_note,
            channel=channel,
        )
        if on_token:
            already = (
                jargon_note + "\n\n" if jargon_note and cfg.jargon.show_transparency_note else ""
            ) + body
            tail = rendered[len(already) :]
            if tail:
                on_token(tail)
        rss_mb = _rss_mb()
        debug_payload = None
        if learn_more:
            debug_payload = _build_debug_payload(
                lang=lang,
                expanded_q=expanded_q,
                jargon_hits=jargon_hits,
                retrieval=retrieval,
                gate=gate,
                messages=meta_messages,
                model_identifier=cfg.active_model().identifier,
                prompt_tokens_est=context_tokens_est,
                chroma_ms=retrieval.chroma_ms,
                rerank_ms=retrieval.rerank_ms,
                llm_ms=llm_ms,
                rss_mb=rss_mb,
            )
        return AnswerResult(
            question=question,
            lang=lang,
            answered=False,
            answer=body,
            rendered=rendered,
            gate=gate,
            retrieval=retrieval,
            latency_ms=int((time.monotonic() - t0) * 1000),
            meta_fallback=meta_fallback,
            merged_question=merged_question,
            expanded_question=expanded_q,
            jargon_hits=jargon_hits,
            context_tokens_est=context_tokens_est,
            context_tokens_limit=cfg.active_model().num_ctx,
            gen_tokens_est=gen_tokens_est or None,
            ttft_ms=ttft_ms,
            gen_tps=gen_tps,
            chroma_ms=retrieval.chroma_ms,
            rerank_ms=retrieval.rerank_ms,
            llm_ms=llm_ms,
            rss_mb=rss_mb,
            debug_payload=debug_payload,
        )

    messages = compose_messages(
        cfg,
        lang,
        history_for_llm,
        retrieval.reranked,
        expanded_q,
        glossary_md=glossary_md,
        facts=facts,
    )

    # Emit the jargon note up-front so the user sees it before tokens stream.
    if jargon_note and cfg.jargon.show_transparency_note:
        _emit_jargon_prefix(jargon_note, on_jargon_prefix, on_token)

    parts: list[str] = []
    ttft_ms: int | None = None
    gen_tps: float | None = None
    llm_ms: int | None = None
    stream_t0 = time.monotonic()
    first_tok_at: float | None = None
    for delta in _stream_answer(cfg, messages, on_thinking=on_thinking):
        parts.append(delta)
        if first_tok_at is None and delta:
            first_tok_at = time.monotonic()
        if on_token:
            on_token(delta)
    llm_ms = int((time.monotonic() - stream_t0) * 1000)
    body = "".join(parts).strip()
    gen_tokens_est = _estimate_tokens(body)
    if first_tok_at is not None:
        ttft_ms = int((first_tok_at - stream_t0) * 1000)
        gen_secs = max(0.001, time.monotonic() - first_tok_at)
        gen_tps = gen_tokens_est / gen_secs if gen_tokens_est else 0.0
    if stale_cache_days is not None:
        note = (
            f"Not: KTH-sidan kunde inte nås live. Svar baseras på cache från {stale_cache_days} dagar sedan."
            if lang == "sv"
            else "Note: The KTH page could not be reached live. This answer uses a cached copy "
            f"from {stale_cache_days} days ago."
        )
        body = f"{note}\n\n{body}" if body else note

    # Rare hiccup: gate passed and the LLM streamed cleanly but emitted no
    # text (e.g., sampler stopped immediately, context full). Surface a
    # short message instead of an empty bubble, log so operators can see
    # how often this happens, and don't mark the turn as `answered` so it
    # isn't saved into conversation memory.
    answered = True
    if not body:
        log.warning(
            "LLM produced empty body for question (lang=%s, gate=%s, top1=%.3f): %r",
            lang,
            gate.reason,
            gate.top1,
            question[:120],
        )
        body = empty_answer_message(lang)
        answered = False
        if on_token:
            on_token(body)

    # Replace inline [Title · Section] citations with [N] numbering and
    # build the Sources block from cited rows only (no silent dump of the
    # full reranked list). Done server-side
    # so Mattermost / CLI / web all get the same compact reference list.
    numbered_body = body
    sources_chunks: list = []
    if retrieval.reranked:
        numbered_body, cited = apply_citation_numbering(body, retrieval.reranked)
        sources_chunks = cited

    # Build everything that comes after the body: confidence badge,
    # sources block, literacy tip. Same content for the streaming tail
    # and for the final rendered string consumed by non-streaming
    # channels (Mattermost, CLI --no-stream).
    tail_parts: list[str] = []
    if cfg.guardrails.show_confidence_badge:
        label = "Tillförlitlighet" if lang == "sv" else "Confidence"
        tail_parts.append(f"\n\n_{label}: {confidence_badge(lang, gate.top1)}_")
    sources_md = format_sources_block(cfg, sources_chunks, lang)
    if sources_md:
        tail_parts.append(sources_md)
    tail_parts.append("\n\n" + literacy_footer(lang, channel=channel))
    tail = "".join(tail_parts)

    if on_token and tail:
        on_token(tail)

    # `result.rendered` uses the numbered body so non-streaming consumers
    # render with [N] inline. The streaming consumers (web) saw the raw
    # body during the stream and re-render it client-side using the same
    # numbering algorithm — the outputs match.
    jargon_prefix = (
        jargon_note + "\n\n" if jargon_note and cfg.jargon.show_transparency_note else ""
    )
    rendered = (jargon_prefix + numbered_body + tail).strip()

    context_tokens_est = _estimate_context_tokens(messages)
    rss_mb = _rss_mb()
    debug_payload = None
    if learn_more:
        debug_payload = _build_debug_payload(
            lang=lang,
            expanded_q=expanded_q,
            jargon_hits=jargon_hits,
            retrieval=retrieval,
            gate=gate,
            messages=messages,
            model_identifier=cfg.active_model().identifier,
            prompt_tokens_est=context_tokens_est,
            chroma_ms=retrieval.chroma_ms,
            rerank_ms=retrieval.rerank_ms,
            llm_ms=llm_ms,
            rss_mb=rss_mb,
        )
    return AnswerResult(
        question=question,
        lang=lang,
        answered=answered,
        answer=body,
        rendered=rendered,
        gate=gate,
        retrieval=retrieval,
        latency_ms=int((time.monotonic() - t0) * 1000),
        expanded_question=expanded_q,
        jargon_hits=jargon_hits,
        numbered_body=numbered_body,
        cited_chunks=list(sources_chunks),
        source_urls=source_urls,
        stale_cache_days=stale_cache_days,
        context_tokens_est=context_tokens_est,
        context_tokens_limit=cfg.active_model().num_ctx,
        gen_tokens_est=gen_tokens_est or None,
        ttft_ms=ttft_ms,
        gen_tps=gen_tps,
        program_code=resolved_program_code,
        admission_term=applied_admission_term,
        admission_year_prefix=applied_admission_year_prefix,
        merged_question=merged_question,
        chroma_ms=retrieval.chroma_ms,
        rerank_ms=retrieval.rerank_ms,
        llm_ms=llm_ms,
        rss_mb=rss_mb,
        debug_payload=debug_payload,
    )


def _stream_answer(cfg: Config, messages: list[dict], on_thinking=None) -> Iterator[str]:
    yield from stream_chat(cfg, messages, on_thinking=on_thinking)


# --- CLI ---


@click.command()
@click.argument("question", nargs=-1, required=False)
@click.option("--show-context", is_flag=True, help="Print retrieved chunks before the answer.")
@click.option("--no-stream", is_flag=True, help="Wait for full response instead of streaming.")
@click.option(
    "-i",
    "--interactive",
    is_flag=True,
    help="REPL mode: keeps short conversation memory between turns.",
)
def main(question: tuple[str, ...], show_context: bool, no_stream: bool, interactive: bool):
    cfg = get_config()
    console = Console()

    if interactive:
        _repl(cfg, console, show_context=show_context)
        return

    if not question:
        console.print("[red]Provide a question, or use --interactive.[/red]")
        sys.exit(2)

    q = " ".join(question)
    _run_once(cfg, console, q, show_context=show_context, no_stream=no_stream)


def _run_once(cfg: Config, console: Console, q: str, *, show_context: bool, no_stream: bool):
    if show_context:
        from student_bot.bot.retrieval import retrieve as _rt

        r = _rt(cfg, q)
        _print_context(console, r.reranked)

    if no_stream:
        result = answer(q, cfg=cfg, channel="cli")
        console.print(result.rendered)
    else:
        printed_any = False

        def on_tok(delta: str):
            nonlocal printed_any
            sys.stdout.write(delta)
            sys.stdout.flush()
            printed_any = True

        result = answer(q, cfg=cfg, on_token=on_tok, channel="cli")
        if printed_any:
            sys.stdout.write("\n")

    console.print()
    console.print(
        f"[dim]lang={result.lang}  answered={result.answered}  "
        f"gate={result.gate.reason}  top1={result.gate.top1:.3f}  "
        f"meanK={result.gate.meanK:.3f}  sources={result.gate.distinct_sources}  "
        f"latency={result.latency_ms}ms[/dim]"
    )


def _repl(cfg: Config, console: Console, *, show_context: bool):
    """REPL mode – same conversation memory model the bot uses for threads."""
    memory = ConversationMemory(cfg)
    console.print("[bold]student-bot[/bold] interactive mode. Empty line or :q to exit.")
    user_id = "cli"
    thread_id = "default"
    while True:
        try:
            q = console.input("[bold cyan]› [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not q or q in (":q", ":quit", "/quit", "exit"):
            break
        if q in (":reset", ":clear"):
            memory.clear(user_id, thread_id)
            console.print("[dim]conversation memory cleared[/dim]")
            continue

        history = memory.get(user_id, thread_id)
        if show_context:
            from student_bot.bot.retrieval import retrieve as _rt

            r = _rt(cfg, q)
            _print_context(console, r.reranked)

        printed_any = False

        def on_tok(delta: str):
            nonlocal printed_any
            sys.stdout.write(delta)
            sys.stdout.flush()
            printed_any = True

        program_prior = memory.get_program_code(user_id, thread_id)
        adm_term_prior, adm_year_prior = memory.get_admission_hints(user_id, thread_id)
        pending_prior = memory.get_pending_question(user_id, thread_id)
        session_expired = memory.take_expired_flag(user_id, thread_id)
        result = answer(
            q,
            history=history,
            cfg=cfg,
            on_token=on_tok,
            program_prior=program_prior,
            admission_term_prior=adm_term_prior,
            admission_year_prefix_prior=adm_year_prior,
            channel="cli",
            pending_question_prior=pending_prior,
        )
        result.session_expired = session_expired
        if printed_any:
            sys.stdout.write("\n")

        # Sets `history_truncated` after the append, so the REPL flag
        # reflects post-turn memory (sticky once the buffer evicts).
        remember_turn(memory, user_id, thread_id, q, result)

        console.print(
            f"[dim]lang={result.lang}  gate={result.gate.reason}  "
            f"top1={result.gate.top1:.3f}  latency={result.latency_ms}ms[/dim]\n"
        )


def _print_context(console: Console, chunks: list[RetrievedChunk]):
    console.print("[bold cyan]Retrieved context:[/bold cyan]")
    for i, c in enumerate(chunks, 1):
        section = c.section_path or "–"
        page = f" p.{c.page_start}" if c.page_start else ""
        preview = c.text.strip().replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:200] + "…"
        console.print(
            f"  [bold]{i}.[/bold] [{c.doc_title} · {section}{page}] "
            f"[dim](score={c.rerank_score:.3f}, dist={c.chroma_distance:.3f})[/dim]\n"
            f"     {preview}"
        )
    console.print()


if __name__ == "__main__":
    main()


__all__ = ["AnswerResult", "answer"]
