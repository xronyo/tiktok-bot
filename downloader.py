import json
import logging
import os
import time
import urllib.parse
import urllib.request

import config

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.tikwm.com/",
    "Accept":  "application/json, */*",
}


def download_tiktok(url: str) -> str:
    """
    Download a TikTok video (watermark-free) via tikwm.com API.
    Returns the absolute path to the downloaded .mp4 file.
    """
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    # ── Step 1: resolve video URL ─────────────────────────────────────────────
    params = urllib.parse.urlencode({"url": url, "hd": "1"})
    req = urllib.request.Request(
        f"https://www.tikwm.com/api/?{params}",
        headers=_HEADERS,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"tikwm error {data.get('code')}: {data.get('msg', 'unknown')}")

    vdata = data["data"]
    video_url = vdata.get("hdplay") or vdata.get("play")
    if not video_url:
        raise RuntimeError("tikwm returned no playable URL")

    # ── Step 2: stream download ───────────────────────────────────────────────
    fname  = f"tikwm_{int(time.time() * 1000)}.mp4"
    output = os.path.join(config.DOWNLOAD_DIR, fname)

    dl_req = urllib.request.Request(video_url, headers=_HEADERS)
    with urllib.request.urlopen(dl_req, timeout=60) as resp, open(output, "wb") as f:
        while chunk := resp.read(512 * 1024):
            f.write(chunk)

    size = os.path.getsize(output)
    if size < 50_000:
        os.remove(output)
        raise RuntimeError(f"Download too small ({size} bytes) — video may be private or deleted")

    logger.info(f"Downloaded {size / 1024 / 1024:.1f} MB → {fname}")
    return output
