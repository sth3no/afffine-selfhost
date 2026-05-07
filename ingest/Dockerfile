FROM python:3.12-slim

# System deps: ffmpeg (used by yt-dlp/markitdown in later phases),
# build essentials kept minimal — no compilers, asyncpg ships wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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
