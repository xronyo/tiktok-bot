import logging
import os
import re
import yt_dlp

import config

logger = logging.getLogger(__name__)

YDL_OPTS = {
    "outtmpl": os.path.join(config.DOWNLOAD_DIR, "%(id)s.%(ext)s"),
    "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "merge_output_format": "mp4",
    "quiet": True,
    "no_warnings": False,
    "noplaylist": True,
    "nocheckcertificate": True,
    # TikTok watermark-free: prefer the no-watermark stream
    "extractor_args": {
        "tiktok": {
            "webpage_download": ["false"],
            "api_hostname": ["api22-normal-c-useast2a.tiktokv.com"],
        }
    },
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://www.tiktok.com/",
    },
    "socket_timeout": 30,
    "retries": 2,
    "fragment_retries": 3,
}


def download_tiktok(url: str) -> str:
    """
    Download TikTok video without watermark.
    Returns absolute path to the downloaded file.
    Raises on failure.
    """
    downloaded_path = None

    class PathCapture:
        def debug(self, msg):
            pass
        def warning(self, msg):
            logger.warning(f"yt-dlp: {msg}")
        def error(self, msg):
            logger.error(f"yt-dlp: {msg}")

    opts = dict(YDL_OPTS)
    opts["logger"] = PathCapture()

    def progress_hook(d):
        nonlocal downloaded_path
        if d["status"] == "finished":
            downloaded_path = d.get("filename") or d.get("info_dict", {}).get("_filename")

    opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if downloaded_path is None:
            # Fallback: reconstruct path from info dict
            video_id = info.get("id", "unknown")
            ext = info.get("ext", "mp4")
            downloaded_path = os.path.join(config.DOWNLOAD_DIR, f"{video_id}.{ext}")

    # yt-dlp sometimes renames after merge; find the actual mp4
    if not os.path.exists(downloaded_path):
        video_id = info.get("id", "")
        for f in os.listdir(config.DOWNLOAD_DIR):
            if video_id and video_id in f and f.endswith(".mp4"):
                downloaded_path = os.path.join(config.DOWNLOAD_DIR, f)
                break
        else:
            raise FileNotFoundError(
                f"Downloaded file not found. Last known path: {downloaded_path}"
            )

    if not os.path.exists(downloaded_path):
        raise FileNotFoundError(f"Downloaded file missing: {downloaded_path}")

    size_mb = os.path.getsize(downloaded_path) / (1024 * 1024)
    logger.info(f"Downloaded {size_mb:.1f} MB → {downloaded_path}")
    return downloaded_path
