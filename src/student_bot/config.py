"""Configuration loader: merges config.yaml with environment / .env secrets."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


def _discover_project_root() -> Path:
    """Repo root for config paths. Docker sets STUDENT_BOT_ROOT=/app; local dev uses pyproject walk."""
    if env := os.environ.get("STUDENT_BOT_ROOT"):
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "pyproject.toml").exists():
            return p
    # Last resort: src/student_bot/config.py -> two parents above package dir
    return here.parents[2]


PROJECT_ROOT = _discover_project_root()


class Paths(BaseModel):
    docs_dir: Path
    chroma_dir: Path
    logs_db: Path


class ChunkConfig(BaseModel):
    target_tokens: int = 600
    overlap_tokens: int = 80


class IngestConfig(BaseModel):
    docling_files: list[str] = Field(default_factory=list)
    chunk: ChunkConfig = Field(default_factory=ChunkConfig)
    dedup_cosine_threshold: float = 0.97


class EmbeddingConfig(BaseModel):
    model: str
    device: str = "cpu"
    query_prefix: str = "query: "
    passage_prefix: str = "passage: "


class RerankerConfig(BaseModel):
    model: str
    device: str = "cpu"
    candidates: int = 20
    keep: int = 5


class GateConfig(BaseModel):
    rerank_top1_min: float
    rerank_meanK_min: float
    meanK: int = 3
    max_distinct_sources_in_topk: int = 3


class LLMConfig(BaseModel):
    model: str
    ollama_url: str
    num_ctx: int = 16384
    temperature: float = 0.1
    max_tokens: int = 1024
    # Gemma 4 reasoning mode: prepends <|think|> to the system prompt and
    # filters <think>...</think> blocks from the streamed output. Default
    # on for better multi-step answers; set false to A/B against thinking-off.
    thinking: bool = True


class MemoryConfig(BaseModel):
    max_turns: int = 4
    ttl_minutes: int = 30


class MattermostConfig(BaseModel):
    trigger_mention: str = "@studybot"
    reply_in_thread: bool = True
    reconnect_max_seconds: int = 60
    # When true, render the answer using a Slack-style "message attachment"
    # with a colored sidebar (confidence -> good/warning/danger) and the
    # Sources block as fields. Falls back to plain Markdown for refusals,
    # rate-limit, and other paths without sources. Default off until the
    # rendering has been eyeballed in the target MM instance.
    use_attachments: bool = False


class LoggingConfig(BaseModel):
    retain_days: int = 90


class FallbackConfig(BaseModel):
    counselor_label_sv: str = "studievägledaren"
    counselor_label_en: str = "the study counselor"
    counselor_link: str = ""


class GuardrailsConfig(BaseModel):
    input_max_chars: int = 1000
    rate_limit_per_minute: int = 5
    show_confidence_badge: bool = True


class WebConfig(BaseModel):
    bind_host: str = "127.0.0.1"
    port: int = 8000
    # URL prefix where the web app exposes the corpus as static files.
    # Citations link to "<doc_base_url>/<rel_source>#page=N". Leave empty to
    # render plain-text citations without links.
    doc_base_url: str = "/docs"
    require_name: bool = True
    auth_enabled: bool = False
    # Path (relative to project root) to a passwd-style file:
    #   user:scrypt:<saltb64>:<hashb64>
    users_file: str = "data/web_users"
    session_idle_minutes: int = 60


class TopicsConfig(BaseModel):
    enabled: bool = True
    file: str = "topics.yaml"
    classifier_temperature: float = 0.0


class JargonConfig(BaseModel):
    enabled: bool = True
    file: str = "dictionary.json"
    proposals_file: str = "dictionary_proposals.json"
    show_transparency_note: bool = True
    max_glossary_entries: int = 6


class MattermostSecrets(BaseModel):
    url: str
    port: int = 443
    scheme: str = "https"
    token: str
    team: str | None = None


class Config(BaseModel):
    paths: Paths
    ingest: IngestConfig
    embedding: EmbeddingConfig
    reranker: RerankerConfig
    gate: GateConfig
    llm: LLMConfig
    memory: MemoryConfig
    mattermost: MattermostConfig
    logging: LoggingConfig
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    topics: TopicsConfig = Field(default_factory=TopicsConfig)
    jargon: JargonConfig = Field(default_factory=JargonConfig)

    # Secrets injected from env (only required when actually used).
    user_id_hash_salt: str | None = None
    mattermost_secrets: MattermostSecrets | None = None

    def absolute(self, p: Path) -> Path:
        """Resolve a relative path against PROJECT_ROOT."""
        return p if p.is_absolute() else (PROJECT_ROOT / p)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def get_config() -> Config:
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    config_path = Path(os.environ.get("CONFIG_FILE") or (PROJECT_ROOT / "config.yaml"))
    raw = _load_yaml(config_path)

    cfg = Config(**raw)

    salt = os.environ.get("USER_ID_HASH_SALT")
    if salt:
        cfg.user_id_hash_salt = salt

    mm_url = os.environ.get("MATTERMOST_URL")
    mm_token = os.environ.get("MATTERMOST_TOKEN")
    if mm_url and mm_token:
        cfg.mattermost_secrets = MattermostSecrets(
            url=mm_url.replace("https://", "").replace("http://", "").rstrip("/"),
            port=int(os.environ.get("MATTERMOST_PORT", "443")),
            scheme=os.environ.get("MATTERMOST_SCHEME", "https"),
            token=mm_token,
            team=os.environ.get("MATTERMOST_TEAM") or None,
        )

    if ollama_url := os.environ.get("OLLAMA_URL"):
        cfg.llm.ollama_url = ollama_url

    # Web bind/auth overrides via env so docker-compose / launchd can flip
    # them without editing config.yaml.
    if h := os.environ.get("WEB_BIND_HOST"):
        cfg.web.bind_host = h
    if p := os.environ.get("WEB_PORT"):
        cfg.web.port = int(p)
    if a := os.environ.get("WEB_AUTH_ENABLED"):
        cfg.web.auth_enabled = a.lower() in ("1", "true", "yes", "on")

    return cfg


__all__ = ["Config", "get_config", "PROJECT_ROOT"]
