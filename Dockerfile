FROM python:3.14-slim

# System deps for pymupdf4llm, lxml, sentence-transformers
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

WORKDIR /app

# Editable install must see package roots ( hatch `packages = src/student_bot, scripts, eval` ).
COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
COPY eval ./eval
# Prefer CPU-only torch (PyPI Linux default is CUDA + nvidia-*); Ollama/GPU stay on the host.
ENV UV_TORCH_BACKEND=cpu
RUN uv pip install --system --no-cache-dir -e .

COPY config.yaml ./
COPY topics.yaml ./
COPY data/dictionary.json ./data/dictionary.json

# Pre-cache the embedding + reranker models so the container is offline-ready.
# Download happens at build time using the same library that loads them at runtime.
ENV TRANSFORMERS_OFFLINE=0 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/app/.hf_cache
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('intfloat/multilingual-e5-base', device='cpu'); \
CrossEncoder('cross-encoder/mmarco-mMiniLMv2-L12-H384-v1', device='cpu')"

# Default: run the Mattermost bot. Override with `docker compose run --rm bot python -m scripts.reindex` etc.
CMD ["python", "-m", "student_bot.bot.mattermost_client"]
