FROM python:3.12-slim

# Match pyproject.toml requires-python (<3.13). Python 3.14 + unconstrained chromadb
# resolves have produced Chroma persist-dir metadata errors against volumes built
# with other client versions.

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

COPY pyproject.toml uv.lock ./
# Prefer CPU-only torch (PyPI Linux default is CUDA + nvidia-*); Ollama/GPU stay on the host.
ENV UV_TORCH_BACKEND=cpu
# 1) Install third-party deps first (cacheable across code-only edits).
RUN uv sync --frozen --no-dev --no-install-project

# 2) Copy project code and install just the local package.
COPY src ./src
COPY scripts ./scripts
COPY eval ./eval
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY config.yaml ./
COPY topics.yaml ./
COPY data/dictionary.json ./data/dictionary.json

# Surface the git version into the runtime env so the About page can show a
# commit hash / release tag even though `.git/` is not copied into the image.
# Pass at build time, e.g.
#     docker compose build --build-arg STUDENT_BOT_VERSION=$(git rev-parse --short HEAD)
ARG STUDENT_BOT_VERSION=""
ENV STUDENT_BOT_VERSION=${STUDENT_BOT_VERSION}

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
