#!/usr/bin/env bash
# One-command start for Mac/Linux: ./run.sh
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "First run: setting up Python environment..."
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ] && [ -z "$ANTHROPIC_API_KEY" ]; then
    echo
    echo "AI photo scanning needs a Claude API key (platform.claude.com)."
    read -r -p "Paste your API key (or press Enter to skip for now): " KEY
    if [ -n "$KEY" ]; then
        echo "ANTHROPIC_API_KEY=$KEY" > .env
        echo "Saved to .env — it will be remembered next time."
    fi
fi

echo
echo "Starting Bookshelf Exchange at http://localhost:5000  (Ctrl+C to stop)"
exec .venv/bin/python app.py
