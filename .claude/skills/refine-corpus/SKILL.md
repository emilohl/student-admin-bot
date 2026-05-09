---
name: refine-corpus
description: Audit recent production failures in the student-bot logs and propose targeted corpus / eval / config changes. Use after a larger update or periodically (weekly/biweekly). Read-only diagnostics by default; any scrape, reindex, or commit needs explicit confirmation.
---

# refine-corpus

A semi-structured procedure for closing the question-answer feedback loop on student-bot. The mechanical bits are scripted; the *judgment* bits are spelled out so you don't re-derive them every iteration.

## When to use

- After a code change that could affect retrieval, the gate, or generation.
- After widening the corpus (manifest entries, new curated `.md`).
- Periodically (weekly is plenty) — student questions and KTH page contents drift.
- Whenever the user says "is the bot still answering X correctly?"

Skip when: the change is purely cosmetic (CSS, README), or you've run the loop in the same session and nothing in the corpus has changed since.

## Quick procedure

1. **Pull failure signal** — `uv run student-bot-replay-failures --since 30d --show-sources` (read-only). Defaults to gate-fail OR low-confidence(<1.0) OR negative-feedback. Print only; don't act yet.
2. **Categorise** the STILL-REFUSE / REGRESSED rows by `gate_reason` and topic:
   - `top1<-0.5` → likely missing corpus coverage. Look at `question` text for a topic.
   - `programme_clarification` → not a retrieval issue; lives in `bot/web_retrieval.py`'s cohort logic. Don't try to fix with a manifest edit.
   - `kth_course_not_found` / `kth_program_not_found` → student typed an invalid code; refusal is correct.
   - `pass` with low top1 + 👎 → retrieval grabbed something adjacent. Inspect top-5 `source_url`s in the replay output.
3. **Diagnose** for each cluster:
   - Find the right kth.se page by `curl`-ing candidate URLs from the `/student/studier` nav (use `curl -s -L --max-time 6 -o /dev/null -w "%{http_code}\n" <url>` to avoid downloading body text).
   - Check whether it's already in `data/url_source_map.json` under another slug.
   - If it's reachable but not body-linked from any current seed, it needs an explicit manifest seed (parent pages aren't enough — see *Load-bearing facts*).
4. **Propose**, then *ask the user* before running:
   - Manifest entries to add to `data/url_manifest_KTH.yaml`. Prefer widening an existing seed with `follow_links: true` + a section-bounded `^/student/studier/<section>/` include_pattern over adding many leaf seeds.
   - Eval cases to append to `eval/eval_set.yml`. Use `expected_any: [...]` with multiple plausible slugs unless the topic has exactly one canonical doc.
   - Code/prompt change if the failure isn't a coverage problem.
5. **Execute** (after confirmation): `student-bot-fetch-url-corpus` → `python -m scripts.reindex` → `python -m eval.run_eval --show-failures` → `student-bot-replay-failures --since 30d --show-sources` → confirm FIXED/IMPROVED count went up and REGRESSED stayed near zero.
6. **Re-tune the gate** only if the eval suggests it AND the production refuse-rate is climbing. The gate is intentionally biased toward false-refuse (better one counselor email than one confident hallucination) — leave thresholds alone unless evidence demands.

## Load-bearing facts (rederiving these wastes a lot of time)

- **Live manifest** is `data/url_manifest_KTH.yaml`. `data/url_manifest.yaml` is empty/dead — `config.yaml:url_ingest.manifest_file` overrides the default.
- **Two web layers, distinct purposes**:
  - `scripts/fetch_url_corpus.py` is the *offline* manifest-driven scraper → `docs/corpus/web_import/`. Runs at index time. Generic; not KTH-specific.
  - `src/student_bot/bot/web_retrieval.py` is the *runtime* dynamic fetch. Hard-allowlisted to `kth.se/student/kurser/kurs/<code>` and `kth.se/student/kurser/program/<code>` only. Handles `__compressedApplicationStore__` JSON, programme cohort terms, course-code aliasing.
  - Don't confuse them. A general `kth.se/student/...` failure goes into the offline manifest, not the dynamic-web allowlist.
- **`<nav>` is stripped at scrape**, so the BFS only follows links present in the page *body*. Section-parent pages whose only links from the parent are in the navigation pane will NOT be reached and need an explicit seed (real example: `/student/studier/val/masterprogram` was missed — only its body-linked sub-pages were scraped).
- **Slug shape**: scraped files end up at `web_import/www.kth.se/<url-stem>-<sha256[:10]>.md`. The `expected_doc_substring` in eval matches against this rel_source path, so use the *URL stem*, lowercase. Not the page title.
- **Frontmatter in scraped `.md`** carries `source_url`, which is preserved into Chroma metadata and used by `build_doc_url` so citations link upstream (https://kth.se/...) rather than the local static mount. Never break this — tells students where the answer actually lives.
- **Ingest strips YAML frontmatter** (`ingest/parse.py:_parse_markdown` via `_FRONTMATTER_RE`), so author/source/date metadata won't leak into retrieval chunks.
- **Curated `.md` under `docs/corpus/markdown/`** is rendered by the `/doc/<rel_source>` web route (gated on `web.md_render_base_url`). Don't route those through the static mount.

## Common pitfalls

- **Label drift in `eval/eval_set.yml`**: after a corpus expansion, an existing `expected_doc_substring` may no longer be the most-relevant chunk because a better page now competes. Verify by reading the new top-5 from `--show-failures`. If the bot now finds an *equally valid* doc (e.g., `Omprövning av betyg.pdf` for a grade-appeal question), broaden the eval to `expected_any` rather than calling it a regression.
- **Auto-fixing eval labels silently** — don't. The user wants to see drift surfaced as failures the first time, then approve a relabel.
- **Adding curated `.md` under `web_import/`** — DO NOT. That tree is for scraped pages; the `/doc/<rel_source>` renderer skips `web_import/`. Curated docs go in `docs/corpus/markdown/`.
- **Setting `follow_links: true` on a seed without an `include_patterns` regex** — pulls in unrelated sub-trees and can saturate `max_pages_per_seed`. Always anchor with `^/student/studier/<section>/` or similar.
- **Forgetting the leading `^`** on `include_patterns`. `re.search` matches anywhere in the path otherwise — `student/studier/val/` will match `studier/value/` too.
- **Re-tuning the gate to chase the suggester output** from `eval/run_eval`. The suggester optimises the eval set, not production. Leave thresholds unless production refuse-rate climbs.
- **Trying to fix `programme_clarification` refusals via the manifest**. They're not retrieval failures; the bot is asking a follow-up question and not merging the answer. Look at `bot/web_retrieval.py:merge_programme_clarification_followup`.

## Tools you'll reach for

- `student-bot-replay-failures` (this skill's main tool) — replay historical qa_log rows against the current corpus.
- `student-bot-stats [--since 7d]` — per-topic counts, latency, 👍/👎 ratios.
- `student-bot-fetch-url-corpus [--limit-seeds N]` — manifest-driven scrape.
- `python -m scripts.reindex` — incremental Chroma rebuild.
- `python -m eval.run_eval --show-failures` — recall@5 + gate accuracy on `eval/eval_set.yml`.
- `student-bot-cli "<question>"` — manual smoke against a specific question (uses LLM, slow).

## Output format expected from this skill

Each invocation should end with:
1. A short summary table: how many FIXED / STILL-REFUSE in the replay, and the dominant remaining `gate_reason`.
2. A proposal block (manifest entries / eval cases / code pointers), each item with a one-line rationale tying back to a qa_id.
3. An explicit "ready to run scrape + reindex + eval?" prompt — never auto-execute.
