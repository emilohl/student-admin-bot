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
    keep: int = 8
    # Soft preference for chunks whose language matches the query language.
    # Added to the cross-encoder logit AFTER reranking, before sorting and
    # before the gate. Small enough that a clearly better cross-language
    # chunk still wins; large enough to break ties between near-equal
    # parallel SV/EN sources. Set to 0 to disable. Tune via `eval/run_eval.py`
    # if you change embedding/reranker or the corpus language mix.
    language_bonus: float = 0.5


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
    # Optional URL prefix when serving behind a reverse proxy path, e.g.
    # "/betabot". Empty means app is served from site root.
    base_path: str = ""
    # URL prefix where the web app exposes the corpus as static files.
    # Citations link to "<doc_base_url>/<rel_source>#page=N". Leave empty to
    # render plain-text citations without links.
    doc_base_url: str = "/docs"
    # URL prefix for the curated-markdown renderer. Citations whose rel_source
    # ends in `.md` AND is not under `web_import/` are rewritten to point here
    # so they open as styled HTML (with optional YAML-frontmatter attribution)
    # instead of raw text via the static mount. Empty string disables.
    md_render_base_url: str = "/doc"
    require_name: bool = True
    auth_enabled: bool = False
    # Path (relative to project root) to a passwd-style file:
    #   user:scrypt:<saltb64>:<hashb64>
    users_file: str = "data/web_users"
    session_idle_minutes: int = 60
    performance_panel_enabled: bool = False


class TopicsConfig(BaseModel):
    enabled: bool = True
    file: str = "topics.yaml"
    classifier_temperature: float = 0.0


class JargonConfig(BaseModel):
    enabled: bool = True
    file: str = "data/dictionary.json"
    proposals_file: str = "data/dictionary_proposals.json"
    show_transparency_note: bool = True
    max_glossary_entries: int = 6


class DynamicWebConfig(BaseModel):
    enabled: bool = False
    timeout_seconds: float = 6.0
    max_pages_per_query: int = 12
    cache_ttl_days: int = 7
    # Regexes matched against URL path only (no scheme/host/query).
    allowed_patterns: list[str] = Field(
        default_factory=lambda: [
            r"^/student/kurser/kurs/[A-Z0-9]+/?$",
            r"^/student/kurser/program/[A-Z0-9]+(?:/[0-9]{5}(?:/(?:arskurs[0-9]+|mal|omfattning|behorighet|genomforande|kurslista|inriktningar))?)?/?$",
        ]
    )
    user_agent: str = "student-bot/0.1 (+https://github.com/cohm/student-admin-bot)"
    max_bytes: int = 1_500_000
    max_links_followed: int = 24
    cache_db: str = "data/web_cache.sqlite"
    program_aliases_file: str = "data/program_aliases.json"
    program_aliases_ttl_hours: int = 24
    # Optional manual overrides/additions: alias -> five-letter program code.
    program_aliases: dict[str, str] = Field(default_factory=dict)
    # Curated colloquial-name registry, separate from the user-facing jargon
    # dictionary because nickname resolution needs a candidate list (e.g.
    # "teknisk fysik" -> [CTFYS, TTFYM]) rather than an inline expansion.
    program_nicknames_file: str = "data/program_nicknames.json"
    # Programs whose most recent intake year is older than (current_year - N)
    # are treated as historical: hidden from disambiguation lists when at
    # least one current program also matches. They still resolve when the
    # user types the code or a discriminative alias token verbatim.
    historical_program_years: int = 8
    # Minimum alias score (coverage of discriminative tokens) required to
    # treat a program alias as a candidate match.
    alias_min_score: float = 0.6
    # In `_resolve_multi_program_candidates`, a historical (older than
    # historical_program_years) candidate is rescued only if the user's
    # query contains all of an alias's strong tokens AND at least one of
    # those tokens is *rare* — appearing in this many aliases or fewer
    # across the whole alias set. Keeps common subject terms like
    # `matematik` from trivially rescuing extinct masters; lets unique
    # discriminators like `fusionsenergi` still rescue TFEPM.
    discriminator_rare_token_max_aliases: int = 3


class UrlIngestConfig(BaseModel):
    enabled: bool = False
    # Domains that may be fetched/crawled into corpus files.
    domains_ingest_allowlist: list[str] = Field(default_factory=lambda: ["www.kth.se", "kth.se"])
    # Back-compat alias (legacy name). Migrated into domains_ingest_allowlist at load time.
    domains_allowlist: list[str] = Field(default_factory=list)
    timeout_seconds: float = 8.0
    max_bytes: int = 2_000_000
    max_pages_per_seed: int = 12
    default_max_depth: int = 1
    manifest_file: str = "data/url_manifest.yaml"
    output_dir: str = "docs/corpus/web_import"
    source_map_file: str = "data/url_source_map.json"
    include_vetted_links_in_markdown: bool = False
    max_links_per_doc: int = 20
    # Extra domains allowed in "Related links" output without ingesting them.
    domains_related_links_allowlist: list[str] = Field(default_factory=list)
    # Back-compat alias (legacy name). Migrated into domains_related_links_allowlist at load time.
    related_links_allowlist: list[str] = Field(default_factory=list)
    # Domains blocked globally (for seed/fetch and related links).
    domain_global_link_blocklist: list[str] = Field(default_factory=lambda: ["canvas.kth.se"])
    # Back-compat alias (legacy name). Migrated into domain_global_link_blocklist at load time.
    global_link_blocklist_hosts: list[str] = Field(default_factory=list)
    global_link_blocklist_url_patterns: list[str] = Field(default_factory=list)
    filtered_links_report_file: str = "data/url_filtered_links_report.json"


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
    dynamic_web: DynamicWebConfig = Field(default_factory=DynamicWebConfig)
    url_ingest: UrlIngestConfig = Field(default_factory=UrlIngestConfig)

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
    url_ingest = raw.get("url_ingest")
    if isinstance(url_ingest, dict):
        if "domains_ingest_allowlist" not in url_ingest and isinstance(
            url_ingest.get("domains_allowlist"), list
        ):
            url_ingest["domains_ingest_allowlist"] = url_ingest["domains_allowlist"]
        if "domains_related_links_allowlist" not in url_ingest and isinstance(
            url_ingest.get("related_links_allowlist"), list
        ):
            url_ingest["domains_related_links_allowlist"] = url_ingest["related_links_allowlist"]
        if "domain_global_link_blocklist" not in url_ingest and isinstance(
            url_ingest.get("global_link_blocklist_hosts"), list
        ):
            url_ingest["domain_global_link_blocklist"] = url_ingest["global_link_blocklist_hosts"]

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
    if bp := os.environ.get("WEB_BASE_PATH"):
        cfg.web.base_path = bp
    if a := os.environ.get("WEB_AUTH_ENABLED"):
        cfg.web.auth_enabled = a.lower() in ("1", "true", "yes", "on")

    return cfg


__all__ = ["Config", "get_config", "PROJECT_ROOT"]
