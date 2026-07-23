#!/usr/bin/env bash
# Fetch the latest version of the site. Your data (.env, bookshelf.db,
# uploads) is untouched. Afterwards start the site with ./run.sh as usual.
set -e
cd "$(dirname "$0")"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading the latest version..."
curl -fsSL -o "$TMP/update.zip" \
  "https://codeload.github.com/taiannangela/vllm/zip/refs/heads/claude/book-sharing-exchange-site-tkbgn3"
unzip -q "$TMP/update.zip" -d "$TMP"
rsync -a \
  --exclude .env --exclude bookshelf.db --exclude secret_key \
  --exclude uploads --exclude .venv \
  "$TMP"/*/bookshelf-exchange/ .
echo "Update complete! Start the site with ./run.sh as usual."
