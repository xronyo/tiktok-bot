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
    Download a TikTok video (watermark-free, highest quality) via tikwm.com API.
    Prefers hdplay (1080p) over play (576p). Logs which quality is selected.
    Returns the absolute path to the downloaded .mp4 file.
    """
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    # ── Step 1: resolve video URL (POST with hd=1 for highest quality) ────────
    body = urllib.parse.urlencode({"url": url, "hd": "1"}).encode()
    req = urllib.request.Request(
        "https://www.tikwm.com/api/",
        data=body,
        headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"tikwm error {data.get('code')}: {data.get('msg', 'unknown')}")

    vdata = data["data"]

    # Pick highest quality watermark-free URL available
    hdplay = vdata.get("hdplay", "").strip()
    play   = vdata.get("play",   "").strip()

    if hdplay:
        video_url = hdplay
        quality   = f"HD {vdata.get('hd_size', 0) // 1024} KB"
    elif play:
        video_url = play
        quality   = f"SD {vdata.get('size', 0) // 1024} KB"
    else:
        raise RuntimeError("tikwm returned no playable URL")

    logger.info(f"Quality selected: {quality}")

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

    logger.info(f"Downloaded {size / 1024 / 1024:.1f} MB ({quality}) → {fname}")
    return output
