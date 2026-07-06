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
    refusal_message,
)
from student_bot.bot.retrieval import RetrievalResult, RetrievedChunk, get_reranker, retrieve
from student_bot.bot.web_retrieval import (
    _question_is_master_eligibility,
    corpus_programme_substrings_for_query,
    history_without_programme_clarification_tail,
    maybe_fetch_dynamic_web,
    merge_programme_clarification_followup,
)
from student_bot.config import Config, get_config
from student_bot.jargon import Jargon, JargonEntry
from student_bot.lang import detect

log = logging.getLogger("student_bot")


_jargon_cache: dict[int, Jargon] = {}
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
    # Admission round actually used this turn (after falling back to a
    # persisted prior when the current turn carries no hint). Callers
    # persist via `ConversationMemory.set_admission_hints` so a follow-up
    # that doesn't restate the term still routes to the same cohort.
    admission_term: str | None = None
    admission_year_prefix: str | None = None
    # UX-honesty signals plumbed up from `ConversationMemory`. The web UI
    # shows a small notice for each. `history_truncated` is sticky for the
    # session (ring-buffer evicted at least one turn); `session_expired`
    # fires once per pruned slot (TTL boundary crossed since last turn).
    history_truncated: bool = False
    session_expired: bool = False
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
        scores = get_reranker(cfg).predict(pairs).tolist()
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
                    if any(code in c_upper_src or code in c_upper_url or code in c_upper_id for code in prog_codes):
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

    contextual_q = merge_programme_clarification_followup(question, history)
    programme_followup_merged = contextual_q != question
    history_for_llm = history_without_programme_clarification_tail(
        history, programme_followup_merged
    )

    # --- jargon: expand query for retrieval, build glossary for prompt ---
    jargon = _jargon(cfg)
    expanded_q = contextual_q
    jargon_hits: list[JargonEntry] = []
    glossary_md = ""
    jargon_note = ""
    if jargon is not None:
        expanded_q, jargon_hits = jargon.expand_query(contextual_q, lang=lang)
        if jargon_hits:
            glossary_md = jargon.glossary_block(
                jargon_hits,
                lang,
                max_entries=cfg.jargon.max_glossary_entries,
            )
            jargon_note = jargon.transparency_note(jargon_hits, lang)

    # Look up program codes in query to add them to the glossary dynamically
    prog_codes_in_q = {w for w in re.findall(r"\b([A-Z]{5})\b", contextual_q)}
    if prog_codes_in_q:
        try:
            from student_bot.bot.web_retrieval import _get_program_aliases
            aliases = _get_program_aliases(cfg)
            # Find the official long name mapped to each code
            code_to_name = {}
            for alias, code in aliases.items():
                code_upper = str(code).upper()
                if code_upper in prog_codes_in_q:
                    if alias.upper() == code_upper:
                        continue
                    current_best = code_to_name.get(code_upper, "")
                    if len(alias) > len(current_best):
                        code_to_name[code_upper] = alias
            
            # Build dynamic glossary entries
            if code_to_name:
                dynamic_entries = []
                for code_upper, name in code_to_name.items():
                    display_name = name.strip()
                    if display_name:
                        display_name = display_name[0].upper() + display_name[1:]
                    dynamic_entries.append(f"- {code_upper} = {display_name}")
                
                # Append to the prompt's glossary block
                if dynamic_entries:
                    label = "Ordlista" if lang == "sv" else "Glossary"
                    if not glossary_md:
                        glossary_md = f"{label}:\n" + "\n".join(dynamic_entries)
                    else:
                        glossary_md += "\n" + "\n".join(dynamic_entries)
        except Exception as e:
            log.warning("failed to inject program aliases into glossary: %s", e)

    web_result = maybe_fetch_dynamic_web(
        cfg,
        expanded_q,
        lang,
        program_prior=program_prior,
        admission_term_prior=admission_term_prior,
        admission_year_prefix_prior=admission_year_prefix_prior,
    )
    resolved_program_code = web_result.resolved_program_code if web_result else None
    applied_admission_term = web_result.applied_admission_term if web_result else None
    applied_admission_year_prefix = web_result.applied_admission_year_prefix if web_result else None
    source_urls: list[str] = []
    stale_cache_days: int | None = None
    if web_result and web_result.clarification:
        msg = web_result.clarification[0] if lang == "sv" else web_result.clarification[1]
        if on_token:
            on_token(msg)
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
        meta_messages = compose_meta_fallback_messages(cfg, lang, history_for_llm, expanded_q)
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
            body = llm_unavailable_message(lang) if llm_error else refusal_message(cfg, lang)
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
        )
        result.session_expired = session_expired
        if printed_any:
            sys.stdout.write("\n")

        if result.answered or result.meta_fallback:
            memory.append(user_id, thread_id, "user", q)
            memory.append(user_id, thread_id, "assistant", result.answer)
        if result.program_code:
            memory.set_program_code(user_id, thread_id, result.program_code)
        if result.admission_term or result.admission_year_prefix:
            memory.set_admission_hints(
                user_id,
                thread_id,
                exact_term=result.admission_term,
                year_prefix=result.admission_year_prefix,
            )
        # Surface eviction state after the append above so the REPL flag
        # reflects post-turn memory (sticky once the buffer evicts).
        result.history_truncated = memory.history_truncated(user_id, thread_id)

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
