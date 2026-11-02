# GenBot on Hugging Face Spaces: the whole stack in one container, run by supervisord.
# Assembled into dist/hf-space by deploy/build_space.py; this file becomes the Space's Dockerfile.
FROM python:3.12-slim

ARG PROMETHEUS_VERSION=2.55.1
ARG ALERTMANAGER_VERSION=0.27.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Prometheus and Alertmanager from their official releases, verified against the published SHA-256 sums.
RUN set -eux; cd /tmp; \
    for spec in "prometheus ${PROMETHEUS_VERSION}" "alertmanager ${ALERTMANAGER_VERSION}"; do \
        set -- $spec; name=$1; ver=$2; file="$name-$ver.linux-amd64.tar.gz"; \
        base="https://github.com/prometheus/$name/releases/download/v$ver"; \
        curl -fsSLO "$base/$file"; \
        curl -fsSL "$base/sha256sums.txt" | grep " $file\$" | sha256sum -c -; \
        tar xzf "$file"; mv "$name-$ver.linux-amd64/$name" /usr/local/bin/; \
        rm -rf "$file" "$name-$ver.linux-amd64"; \
    done

# Spaces run containers as user 1000.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements.txt ./requirements.txt
COPY --chown=user shopapi/requirements.txt ./shopapi/requirements.txt
RUN pip install --no-cache-dir --user -r requirements.txt
# ShopAPI gets its own environment, as it would as a separate service: its FastAPI pins a Starlette
# version that conflicts with Streamlit's.
RUN python -m venv /home/user/shopapi-venv \
    && /home/user/shopapi-venv/bin/pip install --no-cache-dir -r shopapi/requirements.txt

# Download the embedding model at build time so the first search isn't slow.
RUN python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as E; E()(['warm up'])"

COPY --chown=user . .
RUN mkdir -p /home/user/data/logs

ENV COPILOT_DATA_DIR=/home/user/data \
    COPILOT_LOG_FILE=/home/user/data/logs/shopapi.log \
    LOG_DIR=/home/user/data/logs \
    CHROMA_URL=http://127.0.0.1:8001 \
    PROMETHEUS_URL=http://127.0.0.1:9090 \
    LLM_PROVIDER=groq \
    DEMO_MAX_QUESTIONS=5

EXPOSE 7860
CMD ["supervisord", "-c", "/home/user/app/deploy/supervisord.conf"]
