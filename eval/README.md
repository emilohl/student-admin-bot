# Evaluation harness

`eval_set.yml` holds hand-curated queries — both in-domain (the bot should
answer) and out-of-domain (the bot should refuse). `run_eval.py` runs the
retrieval + gate stack against every entry and reports:

- **recall@5** over in-domain queries (does the expected source appear in the
  reranked top-K?)
- **gate accuracy**: in-domain pass-rate and OOD refuse-rate under the current
  thresholds in `config.yaml`
- **suggested thresholds** computed from the score separation between
  in-domain and OOD reranker scores

## Run it

```bash
uv run python -m eval.run_eval                # summary
uv run python -m eval.run_eval --show-failures  # show recall misses
```

The script does not call the LLM, only retrieval + reranker + gate.
That keeps it fast and reproducible across runs.

## Adding entries

Each entry has:

```yaml
- question: "Hur överklagar jag ett betyg?"
  lang: sv                                 # or "en"
  kind: in_domain                          # or "out_of_domain"
  expected_doc_substring: "betygssystem"   # case-insensitive substring of rel_source
  # or use expected_any for multiple acceptable hits:
  # expected_any: ["hantering-plagiering", "Uppforandekod-for-studenter"]
```

Bias for *real questions students ask*. The eval set is meant to grow over
time as you see traffic; treat any drop in recall vs the previous run as a
regression signal.

## Tuning the gate

After running `run_eval.py`, copy the suggested `rerank_top1_min` and
`rerank_meanK_min` into `config.yaml` under `gate:`. If the suggestion looks
permissive, prefer the in-domain min over the OOD max — biasing toward false
refusal is preferable to hallucinating policy.

## Disambiguation logic tests

`test_program_resolution.py` covers the program-alias scoring, multi-candidate
clarification, level/historical filters, conversation-prior carryover, and the
kurslista-backed course resolver introduced for issue #26. It mocks HTTP so it
runs offline.

```bash
uv run python -m eval.test_program_resolution
```

Exits 0 on success, 1 on the first failure. The retrieval-level eval suite
(`run_eval.py`) does not exercise these paths — clarifications short-circuit
before retrieval — so this script is the regression check for them.
