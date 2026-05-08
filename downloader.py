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

# Music CDN hostnames — tikwm sometimes returns audio tracks instead of videos
_MUSIC_CDN_HOSTS = ("v16-ies-music.", "v19-ies-music.", "v26-ies-music.", "ies-music.")


def _resolve_short_url(url: str) -> str:
    """Follow redirects to get the canonical long-form TikTok URL."""
    req = urllib.request.Request(url, headers={"User-Agent": _HEADERS["User-Agent"]},
                                  method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.url
    except Exception:
        # urlopen may raise on redirect chains; grab the final URL from the exception
        return url


def _query_tikwm(url: str) -> dict:
    body = urllib.parse.urlencode({"url": url, "hd": "1"}).encode()
    req = urllib.request.Request(
        "https://www.tikwm.com/api/",
        data=body,
        headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def download_tiktok(url: str) -> str:
    """
    Download a TikTok video (watermark-free, highest quality) via tikwm.com API.
    Prefers hdplay (1080p) over play (576p). Logs which quality is selected.
    Returns the absolute path to the downloaded .mp4 file.
    """
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    # ── Step 1: resolve short URLs before querying tikwm ─────────────────────
    # vm.tiktok.com / vt.tiktok.com short links confuse tikwm and cause it to
    # return the audio track (size=0) instead of the video.
    canonical = url
    if any(h in url for h in ("vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com")):
        canonical = _resolve_short_url(url)
        if canonical != url:
            logger.info(f"Resolved short URL → {canonical}")

    # ── Step 2: resolve video URL via tikwm API ───────────────────────────────
    data = _query_tikwm(canonical)

    # Retry once with the original URL if canonical failed
    if data.get("code") != 0 and canonical != url:
        logger.warning(f"tikwm failed for canonical URL, retrying with original")
        data = _query_tikwm(url)

    if data.get("code") != 0:
        raise RuntimeError(f"tikwm error {data.get('code')}: {data.get('msg', 'unknown')}")

    vdata = data["data"]

    # size=0 / hd_size=0 means tikwm couldn't retrieve the video
    hd_size = vdata.get("hd_size", 0) or 0
    sd_size = vdata.get("size", 0) or 0
    if hd_size == 0 and sd_size == 0:
        raise RuntimeError("tikwm returned size=0 — video is private, deleted, or unavailable")

    hdplay = vdata.get("hdplay", "").strip()
    play   = vdata.get("play",   "").strip()

    # Reject URLs that point to the music CDN (audio-only, not a video)
    def _is_music_url(u: str) -> bool:
        return any(frag in u for frag in _MUSIC_CDN_HOSTS)

    if hdplay and not _is_music_url(hdplay):
        video_url = hdplay
        quality   = f"HD {hd_size // 1024} KB"
    elif play and not _is_music_url(play):
        video_url = play
        quality   = f"SD {sd_size // 1024} KB"
    elif hdplay:
        # Both URLs are music CDN — tikwm resolved to audio; bail out
        raise RuntimeError("tikwm returned a music/audio URL instead of a video — "
                           "video may be a slideshow or unavailable in this region")
    else:
        raise RuntimeError("tikwm returned no playable video URL")

    logger.info(f"Quality selected: {quality}")

    # ── Step 3: stream download ───────────────────────────────────────────────
    fname  = f"tikwm_{int(time.time() * 1000)}.mp4"
    output = os.path.join(config.DOWNLOAD_DIR, fname)

    dl_req = urllib.request.Request(video_url, headers=_HEADERS)
    content_type = ""
    with urllib.request.urlopen(dl_req, timeout=60) as resp, open(output, "wb") as f:
        content_type = resp.headers.get("Content-Type", "")
        while chunk := resp.read(512 * 1024):
            f.write(chunk)

    # Reject audio-only containers (audio/mp4, audio/x-m4a, etc.)
    if content_type.startswith("audio/"):
        os.remove(output)
        raise RuntimeError(f"Downloaded file is audio-only ({content_type}) — "
                           "video is a slideshow or not available via tikwm")

    size = os.path.getsize(output)
    if size < 50_000:
        os.remove(output)
        raise RuntimeError(f"Download too small ({size} bytes) — video may be private or deleted")

    logger.info(f"Downloaded {size / 1024 / 1024:.1f} MB ({quality}) → {fname}")
    return output
