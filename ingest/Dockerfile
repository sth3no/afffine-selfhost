FROM python:3.12-slim

# System deps: ffmpeg (yt-dlp merge step + Phase 13 frame extract), Node 20
# for the bgutil-ytdlp-pot-provider script-mode token generator, git for
# cloning that provider's repo. asyncpg ships wheels so no compilers needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates git gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# bgutil-ytdlp-pot-provider script mode: clone + build the BgUtils-based
# JS token generator at a known path. The yt-dlp plugin (pip-installed
# below) shells out here to mint poToken on demand. Pure-Node — no
# headless Chromium needed, unlike the previous yt_session_server sidecar
# this replaces.
ARG BGUTIL_VERSION=master
RUN git clone --depth 1 --branch ${BGUTIL_VERSION} \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider /opt/bgutil-pot \
    && cd /opt/bgutil-pot/server \
    && npm ci --omit=dev \
    && npx tsc \
    && rm -rf /root/.npm

WORKDIR /app

# Install Python deps first for layer caching.
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy application code last.
COPY topics.yaml ./
COPY src/ ./src/
COPY migrations/ ./migrations/

ENV PYTHONUNBUFFERED=1
EXPOSE 3200

# Default command starts the HTTP API. The migration container overrides
# this via `command: ["python", "-m", "src.migrate"]` in compose.yaml.
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "3200"]
