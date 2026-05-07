-- Phase 1: initial ingest service schema.
-- All statements use IF NOT EXISTS so the runner is idempotent across
-- redeploys (the migration container runs on every stack start).

-- Provided by pgvector/pgvector:pg16 image, but enable per-database.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS captures (
    id              TEXT PRIMARY KEY,                  -- ULID
    url             TEXT,
    url_hash        TEXT UNIQUE,                       -- sha256(normalized_url)
    source_app      TEXT,
    shared_title    TEXT,
    shared_text     TEXT,
    platform        TEXT NOT NULL,
    status          TEXT NOT NULL,                     -- queued|extracting|classifying|filing|done|failed|deleted
    doc_id          TEXT,
    web_url         TEXT,
    topic_path      TEXT,
    classifier_topic     TEXT,
    classifier_conf      REAL,
    classifier_reasoning TEXT,
    needs_classification BOOLEAN DEFAULT FALSE,
    error           TEXT,
    retry_count     INT DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

-- Worker pulls 'queued' items by oldest first.
CREATE INDEX IF NOT EXISTS captures_queued
    ON captures(created_at)
    WHERE status = 'queued';

-- Failed items wait until next_attempt_at <= NOW().
CREATE INDEX IF NOT EXISTS captures_failed_due
    ON captures(next_attempt_at)
    WHERE status = 'failed' AND next_attempt_at IS NOT NULL;

-- History view in iOS app sorts by newest.
CREATE INDEX IF NOT EXISTS captures_created_at_desc
    ON captures(created_at DESC);

CREATE TABLE IF NOT EXISTS folder_embeddings (
    folder_id   TEXT PRIMARY KEY,
    folder_name TEXT NOT NULL,
    parent_path TEXT NOT NULL,
    embedding   VECTOR(1536),                          -- text-embedding-3-small dim
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS folder_embeddings_parent
    ON folder_embeddings(parent_path);

CREATE TABLE IF NOT EXISTS topic_aliases (
    parent_path TEXT NOT NULL,                         -- e.g. Sources/Socials/Instagram
    alias       TEXT NOT NULL,                         -- "Cooking"
    canonical   TEXT NOT NULL,                         -- "Recipes"
    PRIMARY KEY (parent_path, alias)
);
