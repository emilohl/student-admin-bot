"""Offline + live tests for the multi-model LLM registry (#20).

The offline sections verify:
- `cfg.active_model()` resolver: bad-active errors, provider/model lookup.
- `_stream_chat_openai` error paths: missing base_url / api_key raise clean
  RuntimeErrors (not surprising AttributeErrors / hangs).
- The Mattermost GDPR notice appends the cloud warning only when the
  active provider's `provider_kind != "ollama"`.
- `/api/health` reports `cloud_provider_name` derived from the active
  model's provider display name.
- API key SecretStr redaction: the token never appears in `repr(cfg)`,
  `cfg.model_dump()`, or `cfg.model_dump_json()`.

The live section round-trips a real chat completion against the configured
provider. Runs only when `LLM_ACTIVE` selects an `openai_compatible`
provider AND the corresponding `*_API_KEY` env var is set. Otherwise
skips with a hint.

Run:
    uv run python -m eval.test_cloud_llm
"""

from __future__ import annotations

import os
import sys

from pydantic import SecretStr

from student_bot.bot import llm
from student_bot.bot.llm import _filter_gemma_channel_blocks
from student_bot.bot.mattermost_client import gdpr_notice_en, gdpr_notice_sv
from student_bot.config import (
    LLMConfig,
    ModelConfig,
    ProviderConfig,
    get_config,
)

_FAIL = 0


def _check(label: str, cond: bool, detail: str = "") -> None:
    global _FAIL
    mark = "ok" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        _FAIL += 1


def _fresh_cfg():
    """Independent Config instance for tests that mutate fields."""
    get_config.cache_clear()
    return get_config()


def _registry_with_cloud() -> LLMConfig:
    """A two-provider registry covering both code paths."""
    return LLMConfig(
        active="berget/test-model",
        providers={
            "ollama": ProviderConfig(kind="ollama", base_url="http://127.0.0.1:11434"),
            "berget": ProviderConfig(
                kind="openai_compatible",
                base_url="https://api.berget.ai/v1",
                display_name="Berget AI",
                api_key_env="BERGET_API_KEY",
            ),
        },
        models={
            "ollama/gemma-4-E4B": ModelConfig(num_ctx=16384, thinking=True, thinking_style="gemma"),
            "berget/test-model": ModelConfig(num_ctx=128000, thinking_style="none"),
        },
    )


def section_resolver() -> None:
    print("\n[1] cfg.active_model() resolver")
    cfg = _fresh_cfg()
    resolved = cfg.active_model()
    _check("default active resolves", resolved.identifier == cfg.llm.active)
    _check("default provider_kind == ollama", resolved.provider_kind == "ollama")
    _check(
        "model_id stripped of provider prefix",
        not resolved.model_id.startswith("ollama/"),
        f"model_id={resolved.model_id!r}",
    )

    # Bad active strings raise clearly.
    cfg.llm.active = "no-slash-here"
    raised = False
    try:
        cfg.active_model()
    except RuntimeError as e:
        raised = "provider/model_id" in str(e) or "must be" in str(e)
    _check("missing slash raises RuntimeError", raised)

    cfg.llm.active = "doesnotexist/some-model"
    raised = False
    try:
        cfg.active_model()
    except RuntimeError as e:
        raised = "unknown provider" in str(e)
    _check("unknown provider raises RuntimeError", raised)

    # Re-set to a registered provider but unknown model.
    cfg.llm.active = "ollama/not-a-registered-model"
    raised = False
    try:
        cfg.active_model()
    except RuntimeError as e:
        raised = "unknown model" in str(e)
    _check("unknown model raises RuntimeError", raised)


def section_openai_error_paths() -> None:
    print("\n[2] _stream_chat_openai error paths")
    cfg = _fresh_cfg()
    cfg.llm = _registry_with_cloud()
    # No API key set → should raise on the first iteration.
    cfg.llm_api_keys.pop("berget", None)
    raised = False
    try:
        next(llm.stream_chat(cfg, [{"role": "user", "content": "ping"}]))
    except RuntimeError as e:
        raised = "API key" in str(e) and "berget" in str(e)
    _check("missing api_key raises RuntimeError with provider name", raised)

    # Missing base_url: blank out the provider.
    cfg.llm.providers["berget"].base_url = ""
    cfg.llm_api_keys["berget"] = SecretStr("sk-test-fake-1234567890")
    raised = False
    try:
        next(llm.stream_chat(cfg, [{"role": "user", "content": "ping"}]))
    except RuntimeError as e:
        raised = "base_url" in str(e)
    _check("missing base_url raises RuntimeError with helpful message", raised)


def section_gdpr_notice() -> None:
    print("\n[3] Mattermost GDPR notice cloud append")
    cfg = _fresh_cfg()
    # Default registry uses ollama → no cloud append.
    _check("local active: no cloud warning (sv)", "Obs" not in gdpr_notice_sv(cfg))
    _check("local active: no cloud warning (en)", "Note:" not in gdpr_notice_en(cfg))

    cfg.llm = _registry_with_cloud()
    sv = gdpr_notice_sv(cfg)
    en = gdpr_notice_en(cfg)
    _check(
        "cloud (sv): warning appended with display_name", "Berget AI" in sv and "personnummer" in sv
    )
    _check(
        "cloud (en): warning appended with display_name",
        "Berget AI" in en and "personal numbers" in en,
    )


def section_health_endpoint() -> None:
    print("\n[4] /api/health reports cloud_provider_name from active model")
    from fastapi.testclient import TestClient

    from student_bot.web.app import create_app

    cfg = _fresh_cfg()
    cfg.llm = _registry_with_cloud()
    app = create_app(cfg)
    client = TestClient(app)
    body = client.get("/api/health").json()
    _check(
        "cloud_provider_name surfaced as display_name",
        body.get("cloud_provider_name") == "Berget AI",
        f"body={body}",
    )

    # Active model on local provider → hidden, even with cloud entries
    # registered.
    cfg.llm.active = "ollama/gemma-4-E4B"
    app = create_app(cfg)
    client = TestClient(app)
    body = client.get("/api/health").json()
    _check(
        "cloud_provider_name hidden when active is ollama",
        body.get("cloud_provider_name") == "",
        f"body={body}",
    )


def section_secret_redaction() -> None:
    """The api key must NOT appear in repr(cfg), model_dump(), or
    model_dump_json(). Pydantic's `SecretStr` handles this — pinned here
    so future regressions are caught."""
    print("\n[5] API key redaction (defense-in-depth)")
    cfg = _fresh_cfg()
    secret = "sk-deadbeef-pin-this-string-1234567890"
    cfg.llm_api_keys["berget"] = SecretStr(secret)
    _check("secret NOT in repr(cfg)", secret not in repr(cfg))
    _check("secret NOT in str(cfg.model_dump())", secret not in str(cfg.model_dump()))
    _check("secret NOT in cfg.model_dump_json()", secret not in cfg.model_dump_json())
    _check(
        "secret accessible via get_secret_value()",
        cfg.llm_api_keys["berget"].get_secret_value() == secret,
    )


def section_gemma_channel_filter() -> None:
    """The Gemma `<|channel>thought\\n...<channel|>` filter must strip the
    block whether it arrives whole, split across deltas, or wrapped around
    visible content. Same helper used on both ollama and openai paths."""
    print("\n[6] _filter_gemma_channel_blocks (shared across providers)")

    OPEN = "<|channel>thought\n"
    CLOSE = "<channel|>"

    def run(deltas: list[str]) -> tuple[str, list[bool]]:
        events: list[bool] = []
        out = "".join(_filter_gemma_channel_blocks(iter(deltas), on_thinking=events.append))
        return out, events

    # 1. Block contained in a single delta.
    out, events = run([f"Hello {OPEN}internal reasoning{CLOSE} world."])
    _check("single-delta block stripped", out == "Hello  world.", f"got {out!r}")
    _check("single-delta thinking events", events == [True, False], f"got {events}")

    # 2. Block split across multiple deltas (the real streaming case).
    out, events = run(
        [
            "He",
            "llo ",
            "<|channel",
            ">thought\n",
            "secret ",
            "reasoning",
            "<chan",
            "nel|>",
            " world.",
        ]
    )
    _check(
        "split-block stripped",
        out == "Hello  world.",
        f"got {out!r}",
    )
    _check("split-block thinking events", events == [True, False], f"got {events}")

    # 3. No block — pass-through.
    out, events = run(["just ", "plain ", "text."])
    _check("no-block pass-through", out == "just plain text.", f"got {out!r}")
    _check("no-block no thinking events", events == [], f"got {events}")

    # 4. Block at very start.
    out, _ = run([f"{OPEN}thinking{CLOSE}answer"])
    _check("leading-block stripped", out == "answer", f"got {out!r}")

    # 5. Unclosed block (truncation case) — nothing yielded after the open.
    out, events = run([f"prefix {OPEN}reasoning never closes"])
    _check("unclosed block: prefix yielded", "prefix " in out, f"got {out!r}")
    _check("unclosed block: reasoning suppressed", "reasoning" not in out, f"got {out!r}")
    _check("unclosed block: thinking start signaled", events and events[0] is True, f"got {events}")


def section_live_roundtrip() -> None:
    """Live test — runs only when `LLM_ACTIVE` points at an openai_compatible
    provider AND the matching API key env var is set.
    """
    print("\n[7] Live round-trip against the configured cloud provider")
    cfg = _fresh_cfg()
    try:
        resolved = cfg.active_model()
    except RuntimeError as e:
        print(f"  [skip] cfg.active_model() failed: {e}")
        return
    if resolved.provider_kind != "openai_compatible":
        print(
            f"  [skip] active={resolved.identifier!r} is provider_kind={resolved.provider_kind!r}; "
            "set LLM_ACTIVE to a cloud model and the matching *_API_KEY env to exercise this."
        )
        return
    provider = cfg.llm.providers[resolved.provider_key]
    if not os.environ.get(provider.api_key_env):
        print(f"  [skip] {provider.api_key_env} not set for provider {resolved.provider_key!r}")
        return

    messages = [
        {"role": "system", "content": "Reply with the single word OK and nothing else."},
        {"role": "user", "content": "Say OK."},
    ]
    deltas: list[str] = []
    try:
        for d in llm.stream_chat(cfg, messages):
            deltas.append(d)
            if sum(len(x) for x in deltas) > 200:
                break
    except Exception as e:
        _check("live round-trip succeeded", False, f"{type(e).__name__}: {e}")
        return
    full = "".join(deltas).strip()
    _check(
        f"live round-trip produced non-empty output (provider={resolved.display_name or resolved.provider_key}, model={resolved.model_id})",
        bool(full),
        f"deltas={len(deltas)} text={full[:120]!r}",
    )


def main() -> int:
    print("Cloud LLM provider tests (#20)")
    section_resolver()
    section_openai_error_paths()
    section_gdpr_notice()
    section_health_endpoint()
    section_secret_redaction()
    section_gemma_channel_filter()
    section_live_roundtrip()
    print(f"\n{('FAILED ' + str(_FAIL)) if _FAIL else 'OK'} — failures={_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
