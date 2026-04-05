#!/usr/bin/env bash
# Stages the affine-mcp-agent sources into ./mcp-agent so Portainer's
# build context contains everything it needs.
#
# Run this from the portainer-stack/ directory BEFORE committing to the
# repo that Portainer pulls from (or before uploading as a tarball).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_DIR="$(cd "$SCRIPT_DIR/../affine-mcp-agent" && pwd)"
TARGET_DIR="$SCRIPT_DIR/mcp-agent"

echo "Staging affine-mcp-agent → $TARGET_DIR"

mkdir -p "$TARGET_DIR"

# Copy only what the container needs — skip node_modules and local env files
rm -rf "$TARGET_DIR/src"
cp -R "$SOURCE_DIR/src" "$TARGET_DIR/src"
cp "$SOURCE_DIR/package.json" "$TARGET_DIR/package.json"
cp "$SOURCE_DIR/package-lock.json" "$TARGET_DIR/package-lock.json"
cp "$SOURCE_DIR/tsconfig.json" "$TARGET_DIR/tsconfig.json"

echo "Done. Files in $TARGET_DIR:"
ls -la "$TARGET_DIR"
