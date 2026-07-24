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
# The zip never contains your data files (.env, bookshelf.db, uploads,
# .venv are not in the repository), so copying over is safe.
unzip -q "$TMP/update.zip" -d "$TMP"
cp -R "$TMP"/*/bookshelf-exchange/. .
echo "Update complete! Start the site with ./run.sh as usual."
