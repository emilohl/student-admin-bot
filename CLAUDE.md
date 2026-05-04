# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` is unusually thorough — read it for architecture diagrams, design rationale, threshold tuning, and Docker setup. This file is the short orientation: commands, load-bearing facts, and gotchas that aren't obvious from the code.

## Common commands

Entry-point names come from `pyproject.toml` `[project.scripts]`; the `student-bot-*` prefix is non-obvious.

- `uv sync` — install. `uv sync --extra docling` adds the table-heavy PDF fallback parser; `--extra dev` adds ruff/pytest.
- `uv run student-bot-cli "<question>"` / `uv run student-bot-cli --interactive` — REPL test the pipeline.
- `uv run student-bot-web` — FastAPI web UI (binds 127.0.0.1 by default).
- `uv run student-bot` — Mattermost websocket bot.
- `uv run python -m scripts.reindex` — rebuild Chroma index from `docs/corpus/`. Incremental by content hash.
- `uv run python -m eval.run_eval` (`--show-failures`) — recall@5 + gate accuracy. Does **not** call the LLM.
- `uv run student-bot-stats [--since 7d]` — per-topic counts, latency, 👍/👎 ratios.
- `uv run student-bot-mkuser <name>` — create a web auth user (scrypt).
- `uv run student-bot-jargon list|proposals|accept|reject|add|remove` — manage `dictionary.json`.
- `uv run ruff check .` / `uv run ruff format .` — line-length 100, target py311.
- Docker: `docker compose build`; `docker compose run --rm beta-web python -m scripts.reindex`; `docker compose up -d beta-web bot`.

There is **no test suite** (no `tests/`, no `test_*.py`); `pytest` is declared in `[dev]` but unused. The `eval/` harness is the de-facto regression check for the retrieval+gate stack — run it after any change to embeddings, reranker, gate logic, or the corpus.

## Architecture

Thin custom RAG layer (~3000 lines, deliberately no LangChain/LlamaIndex). Three frontends share one pipeline.

- **Frontends** → `src/student_bot/bot/mattermost_client.py`, `src/student_bot/web/app.py`, `src/student_bot/bot/pipeline.py:main` (CLI).
- **Pipeline (the convergence point)** → `src/student_bot/bot/pipeline.py`: lang-detect → guardrails → jargon-expand → retrieve → gate → generate (or refuse) → log → post-hoc topic-classify.
- **Retrieval** → `bot/retrieval.py` (Chroma top-N + cross-encoder rerank → top-K), `bot/gate.py` (off-topic / low-confidence gate).
- **Generation** → `bot/llm.py` (streaming Ollama), `bot/prompts.py` (bilingual sv/en system prompts), `bot/citations.py` (sources, badge, literacy footers), `bot/memory.py` (per-thread short-term buffer).
- **Ingest (offline)** → `ingest/parse.py` (pymupdf4llm w/ page chunks; docling fallback), `ingest/chunk.py`, `ingest/embed.py` (bge-m3 → Chroma).
- **Storage** → Chroma at `data/chroma/`, SQLite log at `data/logs.sqlite` (schema in `logging_db.py`).
- **Config** → `config.yaml` (non-secrets) + `.env` (secrets), merged via Pydantic in `src/student_bot/config.py`. `PROJECT_ROOT` is discovered by walking up to `pyproject.toml`; Docker overrides this with `STUDENT_BOT_ROOT=/app`.

The README's ASCII diagram and per-file role table are the fastest way in for component-level questions.

## Non-obvious constraints (easy to break)

- **Embeddings + reranker run on CPU on purpose.** Metal stays exclusively with Ollama. Don't switch them to MPS/CUDA without thinking about RAM.
- **bge-m3 does NOT use e5 `query:` / `passage:` prefixes.** `config.yaml` keeps them empty; do not "fix" this.
- **Re-tune `gate.rerank_top1_min` / `rerank_meanK_min` via `eval/run_eval.py` after any change to embedding model, reranker, or corpus.** Cross-encoder logits are unbounded and model-specific.
- **The gate is intentionally biased toward false-refuse.** When tuning, prefer the in-domain min over the OOD max. Cost of refusing a real question = one counselor email; cost of a confident hallucination = wrong policy.
- **The LLM has no agentic tools — that's the prompt-injection security boundary.** No shell, no MCP, no web fetch, no file write. Don't add any.
- **Logging is opt-out, not opt-in.** User IDs are salted SHA-256 (salt from `USER_ID_HASH_SALT`). Opted-out traffic still bumps `anon_counter` (lang + gate-pass only). One-shot disclosure tracked in `disclosed`. Surfaces: Mattermost `!privacy off/on/status`, web onboarding checkbox.
- **Citations link via `web.doc_base_url`** (default `/docs`); PDFs use `#page=N`. Changes to `paths.docs_dir` must keep the web static mount in sync.
- **Reindex is manual.** `scripts/reindex.py` is incremental (by `<rel_source>#<idx>` content hash) but never auto-runs.
- **`dictionary.json` hot-reloads on mtime change** — no restart needed after edits or `student-bot-jargon accept`. `dictionary_proposals.json` is gitignored (in-flight student suggestions stay private).
- **`topics.yaml` edits affect only new classifications.** Old `qa_log` rows keep their previous label until reclassified.
- **Docker corpus mount:** if host `docs/corpus` is a symlink pointing outside `docs/`, set `CORPUS_HOST_PATH` to the symlink's absolute target — symlinks usually don't resolve inside the container.
- **Web auth is two layers** when `WEB_AUTH_ENABLED=true`: `?access=<WEB_ACCESS_TOKEN>` first-visit token sets a session cookie (Starlette `SessionMiddleware`), then HTTP Basic from `data/web_users` (scrypt). Keep `WEB_SESSION_SECRET` stable across restarts or sessions invalidate.

## Pedagogical surface (don't strip without thinking)

The bot deliberately teaches LLM literacy through five repeating surfaces: confidence badge, rotating literacy footer, refusal-with-reason, first-touch GDPR notice, and the Sources block. The README's "How the bot teaches LLM literacy" table lists the file each lives in. UI cleanups can accidentally remove on-purpose features — read that section first.

## Where to start by task type

| Task | Start at |
|---|---|
| Retrieval / rerank logic | `src/student_bot/bot/retrieval.py` → re-run `eval/run_eval.py` |
| Gate / refusal behavior | `bot/gate.py`, `bot/prompts.py` |
| UI strings, footers, badges | `bot/citations.py`, `bot/prompts.py`, `web/app.py` (`_about_page`), `bot/mattermost_client.py` (GDPR / `!privacy`) |
| New config knob | `src/student_bot/config.py` (Pydantic models) + `config.yaml` |
| Ingest behavior | `src/student_bot/ingest/{parse,chunk,embed}.py` → `scripts/reindex.py` |
| Privacy / logging | `src/student_bot/logging_db.py` |
| Web auth | `src/student_bot/web/auth.py`, `scripts/mkuser.py` |
