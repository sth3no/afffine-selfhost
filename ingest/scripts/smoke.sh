#!/usr/bin/env bash
# End-to-end smoke against a running ingest service.
#
# Usage:
#   INGEST_BASE=http://localhost:3200 \
#   INGEST_API_TOKEN=... \
#   bash ingest/scripts/smoke.sh
#
# Submits 3 representative URLs (arxiv, reddit, YouTube w/ captions),
# polls /captures/{id} until each reaches status='done' or 60s elapses.

set -euo pipefail

: "${INGEST_BASE:=http://localhost:3200}"
: "${INGEST_API_TOKEN:?INGEST_API_TOKEN must be set}"
: "${SMOKE_TIMEOUT_SEC:=60}"

URLS=(
  "https://arxiv.org/abs/2401.00001"
  "https://www.reddit.com/r/python/"
  "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
)

pass=0
fail=0

for url in "${URLS[@]}"; do
  echo
  echo "=== Submitting: $url ==="

  body=$(jq -n --arg url "$url" '{url: $url, source_app: "smoke"}')
  resp=$(curl -sS -m 10 -X POST "$INGEST_BASE/capture" \
              -H "Authorization: Bearer $INGEST_API_TOKEN" \
              -H "Content-Type: application/json" \
              --data "$body")

  capture_id=$(echo "$resp" | jq -r '.capture_id // empty')
  if [[ -z "$capture_id" ]]; then
    echo "FAIL: no capture_id in response: $resp"
    fail=$((fail + 1))
    continue
  fi

  echo "  capture_id=$capture_id"
  echo "  initial_path=$(echo "$resp" | jq -r '.initial_path')"
  echo "  doc_id=$(echo "$resp" | jq -r '.doc_id')"

  # Poll until done or timeout.
  start=$(date +%s)
  status="queued"
  while :; do
    elapsed=$(($(date +%s) - start))
    if (( elapsed > SMOKE_TIMEOUT_SEC )); then
      echo "  FAIL: timeout after ${SMOKE_TIMEOUT_SEC}s (last status: $status)"
      fail=$((fail + 1))
      break
    fi
    sleep 2

    detail=$(curl -sS -m 10 \
                  -H "Authorization: Bearer $INGEST_API_TOKEN" \
                  "$INGEST_BASE/captures/$capture_id")
    status=$(echo "$detail" | jq -r '.status')
    echo "  t=${elapsed}s status=$status"

    if [[ "$status" == "done" ]]; then
      topic=$(echo "$detail" | jq -r '.topic_path')
      echo "  PASS: filed under '$topic'"
      pass=$((pass + 1))
      break
    fi
    if [[ "$status" == "failed" ]]; then
      err=$(echo "$detail" | jq -r '.error // empty')
      echo "  FAIL: pipeline failed: $err"
      fail=$((fail + 1))
      break
    fi
  done
done

echo
echo "=== Smoke result: $pass passed, $fail failed ==="
test "$fail" -eq 0
