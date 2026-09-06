FROM python:3.11-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG DOCLING_TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RESEARCH_CONNECT_DATA_DIR=/data \
    DAILY_PAPER_DIR=/app/modules/daily-paper-reader \
    XHS_AGENT_DIR=/app/modules/xhs-agent \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HF_HOME=/data/cache/huggingface \
    XDG_CACHE_HOME=/data/cache

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl \
       libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
       libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 \
       libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
       libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 xvfb \
       libfontconfig1 libfreetype6 fonts-liberation fonts-noto-color-emoji \
       fonts-unifont fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# Keep the expensive Python/Chromium layer independent from ordinary source
# changes. It is invalidated only when dependency metadata or core changes.
COPY pyproject.toml constraints.txt /app/
COPY packages/research-connect-core/pyproject.toml /app/packages/research-connect-core/pyproject.toml
COPY packages/research-connect-core/src /app/packages/research-connect-core/src

# Install the shared environment once. Docling is intentionally included in
# the default image; it runs on CPU and can use an available accelerator.
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -c constraints.txt ./packages/research-connect-core \
    && python -m pip install \
       --index-url "${DOCLING_TORCH_INDEX_URL}" \
       --extra-index-url https://pypi.org/simple \
       torch torchvision \
    && python -m pip install -c constraints.txt ".[docling]" \
    && python -m playwright install chromium \
    && python -m pip check

COPY . /app

RUN python -m pip install --no-deps \
       ./packages/research-connect-core \
       ./apps/connect-hub \
       ./modules/citationclaw \
       ./modules/xhs-agent \
    && python -m pip check

# Generated Daily Paper files are not part of the image. Start every fresh
# Docker volume from the upstream clean site shell and public default config.
RUN cp -a modules/daily-paper-reader/docs_init modules/daily-paper-reader/docs \
    && cp modules/daily-paper-reader/docs_init/config.yaml modules/daily-paper-reader/config.yaml \
    && mkdir -p \
       /config \
       /data/cache \
       modules/daily-paper-reader/.local-runs \
       modules/daily-paper-reader/archive \
    && chmod -R a+rX /app /ms-playwright \
    && chmod -R a+rwX \
       /config \
       /data \
       modules/daily-paper-reader/docs \
       modules/daily-paper-reader/.local-runs \
       modules/daily-paper-reader/archive \
    && if ! getent group 1000 >/dev/null; then groupadd --gid 1000 research; fi \
    && if ! getent passwd 1000 >/dev/null; then useradd --uid 1000 --gid 1000 --create-home research; fi

USER 1000:1000

ENTRYPOINT ["connect-hub", "--env-file", "/config/connect-hub.env"]
CMD ["serve"]
