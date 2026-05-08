#!/usr/bin/env bash
# Phase 11 backfill: re-run the pipeline on existing captures so they get
# AI-generated titles + summaries from the new pipeline (set_doc_title +
# summarizer + structured blocks).
#
# Usage:
#   INGEST_BASE=http://localhost:3200 \
#   INGEST_API_TOKEN=$INGEST_API_TOKEN \
#   ./backfill_url_titles.sh                      # retry all done captures
#   ./backfill_url_titles.sh <id> <id> ...        # retry specific capture IDs
#
# Each retry resets the row to status=queued; the worker picks it up,
# extractor + summarizer run again, and set_doc_title rewrites the doc
# title in AFFiNE.

set -euo pipefail

: "${INGEST_BASE:=http://localhost:3200}"
: "${INGEST_API_TOKEN:?INGEST_API_TOKEN is required}"

if [ "$#" -gt 0 ]; then
    ids=("$@")
else
    echo "→ Listing all done captures from $INGEST_BASE/captures..."
    raw=$(curl -fsS -H "Authorization: Bearer $INGEST_API_TOKEN" \
        "$INGEST_BASE/captures?status=done&limit=200")
    mapfile -t ids < <(echo "$raw" | jq -r '.items[].capture_id')
fi

count=0
failed=0
for id in "${ids[@]}"; do
    if [ -z "$id" ]; then continue; fi
    printf "  retry %s ... " "$id"
    if curl -fsS -X POST -H "Authorization: Bearer $INGEST_API_TOKEN" \
            "$INGEST_BASE/captures/$id/retry" >/dev/null 2>&1; then
        echo "queued"
        count=$((count + 1))
    else
        echo "FAILED"
        failed=$((failed + 1))
    fi
done

echo ""
echo "Re-queued: $count    Failed: $failed"
echo "Worker will reprocess in the background. Watch: docker logs -f affine_ingest"
