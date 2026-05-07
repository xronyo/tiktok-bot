import os
import shutil

# ── bot token ─────────────────────────────────────────────────────────────────
# Set BOT_TOKEN env var in Railway dashboard; fallback keeps local dev working.
BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    "8514558502:AAGZPKhVRDZVwMNTV-R5EFxqm42Kz3Lec0Q",
)
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

# ── paths ─────────────────────────────────────────────────────────────────────
# In Railway: DATA_DIR is a mounted persistent volume (/data).
# downloads/ and processed/ are ephemeral (/tmp) — they're just work dirs.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR      = os.environ.get("DATA_DIR",      os.path.join(BASE_DIR, "data"))
DOWNLOAD_DIR  = os.environ.get("DOWNLOAD_DIR",  os.path.join(BASE_DIR, "downloads"))
PROCESSED_DIR = os.environ.get("PROCESSED_DIR", os.path.join(BASE_DIR, "processed"))
DB_PATH       = os.path.join(DATA_DIR, "tiktok_bot.db")

# ── tunables ──────────────────────────────────────────────────────────────────
MAX_RETRIES      = int(os.environ.get("MAX_RETRIES", 3))
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", 49))
QUEUE_WORKERS    = int(os.environ.get("QUEUE_WORKERS", 3))

# ── TikTok URL patterns ───────────────────────────────────────────────────────
TIKTOK_PATTERNS = [
    r'https?://(?:www\.)?tiktok\.com/@[^/\s]+/video/\d+(?:\?[^\s]*)?',
    r'https?://(?:vm|vt)\.tiktok\.com/[A-Za-z0-9]+/?',
    r'https?://(?:www\.)?tiktok\.com/t/[A-Za-z0-9]+/?',
    r'https?://m\.tiktok\.com/v/\d+(?:\.html)?(?:\?[^\s]*)?',
    r'https?://(?:www\.)?tiktok\.com/@[^/\s]+/video/\d+',
]

PITCH_SHIFT_FACTOR = 1.002  # 0.2% — completely imperceptible

# ── FFmpeg binary resolution ──────────────────────────────────────────────────
# Priority: 1) $FFMPEG_BIN env var  2) ./bin/ffmpeg  3) system PATH  4) imageio-ffmpeg
def _find_ffmpeg() -> str:
    env_path = os.environ.get("FFMPEG_BIN")
    if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
        return env_path
    local = os.path.join(BASE_DIR, "bin", "ffmpeg")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    raise RuntimeError(
        "ffmpeg not found. In Docker it comes from apt; locally run: brew install ffmpeg"
    )

FFMPEG_BIN: str = _find_ffmpeg()
