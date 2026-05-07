#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create venv if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "[setup] Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "[setup] Installing dependencies..."
pip install -q -r requirements.txt

# Check ffmpeg
if ! command -v ffmpeg &>/dev/null; then
    echo "[error] ffmpeg not found. Install it:"
    echo "  macOS:  brew install ffmpeg"
    echo "  Ubuntu: sudo apt install ffmpeg"
    exit 1
fi

if ! command -v ffprobe &>/dev/null; then
    echo "[error] ffprobe not found (should come with ffmpeg)."
    exit 1
fi

echo "[info] ffmpeg: $(ffmpeg -version 2>&1 | head -1)"
echo "[info] yt-dlp: $(yt-dlp --version 2>/dev/null || echo 'not found')"
echo ""
echo "Starting TikTok Bot..."
echo "Logs → data/bot.log"
echo "Press Ctrl+C to stop."
echo ""

exec python main.py
