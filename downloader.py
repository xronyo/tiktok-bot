"""
TikTok downloader — three-phase strategy with smart fallback.

Why plain yt-dlp fails on Railway / datacenters:
  TikTok blocks datacenter IP ranges at the TCP level — connections are
  accepted but never answered (20 s timeout).  Changing headers or TLS
  fingerprints doesn't help; the block is at layer 3/4, not 7.

Download phases (tried in order, fast-fail on network blocks):
  Phase 1 — yt-dlp direct (works on residential IPs and home servers)
             Uses curl_cffi for Chrome-accurate TLS fingerprinting.
             Timeout: 12 s.  First network-block error skips remaining
             yt-dlp attempts so we don't burn 60 s on timeouts.

  Phase 2 — tikwm.com API  (works from any IP, including Railway)
             Third-party service; their servers fetch from TikTok.
             Returns watermark-free HD URLs.  No auth required.

  Phase 3 — yt-dlp + PROXY_URL  (if env var is set)
             Residential proxy bypasses the IP block.  Supports
             HTTP, HTTPS, SOCKS4, SOCKS5.  Tried after tikwm so a
             working tikwm response is always preferred (faster).

Cookie setup (still needed for yt-dlp Phase 1 / Phase 3):
  Local:   save cookies.txt in ~/tiktok_bot/
  Railway: run  python make_cookie_env.py ~/Downloads/cookies.txt
           and set the printed value as TIKTOK_COOKIES env var.
"""

import json
import logging
import os
import tempfile
import time
import urllib.parse
import urllib.request
from typing import Optional

import yt_dlp

import config

logger = logging.getLogger(__name__)

# ── cookie resolution ─────────────────────────────────────────────────────────

_decoded_cookie_path: Optional[str] = None


def _decode_cookie_env(value: str) -> str:
    import base64, gzip
    raw = base64.b64decode(value.strip())
    return gzip.decompress(raw).decode("utf-8") if raw[:2] == b"\x1f\x8b" else raw.decode("utf-8")


def _resolve_cookies() -> Optional[str]:
    global _decoded_cookie_path

    env_file = os.environ.get("TIKTOK_COOKIES_FILE")
    if env_file and os.path.isfile(env_file) and os.path.getsize(env_file) > 10:
        logger.info(f"Cookies: {env_file}")
        return env_file

    local = os.path.join(config.BASE_DIR, "cookies.txt")
    if os.path.isfile(local) and os.path.getsize(local) > 10:
        logger.info("Cookies: local cookies.txt")
        return local

    b64 = os.environ.get("TIKTOK_COOKIES")
    if b64:
        if _decoded_cookie_path and os.path.isfile(_decoded_cookie_path):
            return _decoded_cookie_path
        try:
            content = _decode_cookie_env(b64)
            fd, path = tempfile.mkstemp(prefix="tiktok_cookies_", suffix=".txt")
            with os.fdopen(fd, "w") as f:
                f.write(content)
            _decoded_cookie_path = path
            logger.info(f"Cookies: decoded from env var ({len(b64):,} chars)")
            return path
        except Exception as exc:
            logger.warning(f"Cookies: env var decode failed — {exc}")

    logger.info("Cookies: none (Phase 1 may fail on datacenter IPs)")
    return None


# ── error classification ──────────────────────────────────────────────────────

def _is_network_block(exc: Exception) -> bool:
    """True when TikTok is blocking at the network layer (not worth retrying with same IP)."""
    msg = str(exc).lower()
    return any(k in msg for k in (
        "timed out", "timeout", "connection reset", "connection refused",
        "curl: (28)", "curl: (35)", "curl: (56)", "transporterror",
        "status code 0", "unable to download webpage",
    ))


def _is_unrecoverable(exc: Exception) -> bool:
    """True when no strategy will help (video gone, private, geo-blocked content)."""
    msg = str(exc).lower()
    return any(k in msg for k in (
        "does not exist", "this video is private", "account has been banned",
        "http error 404", "video unavailable",
    ))


# ── yt-dlp strategies ─────────────────────────────────────────────────────────

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    _CHROME = ImpersonateTarget("chrome", "110", "windows", "10")
    _SAFARI = ImpersonateTarget("safari", "16", "macos", "13")
    _HAS_IMPERSONATE = True
except ImportError:
    _HAS_IMPERSONATE = False
    _CHROME = _SAFARI = None

_STRATEGIES = [
    {
        "name": "chrome-impersonate",
        "impersonate": _CHROME,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer":         "https://www.tiktok.com/",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {"tiktok": {"app_name": ["tiktok_web"]}},
    },
    {
        "name": "safari-impersonate",
        "impersonate": _SAFARI,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/16.5 Safari/605.1.15"
            ),
            "Referer":         "https://www.tiktok.com/",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {"tiktok": {"app_name": ["tiktok_web"]}},
    },
    {
        "name": "ios-app",
        "impersonate": None,
        "http_headers": {
            "User-Agent":      "TikTok 26.2.0 rv:262018 (iPhone; iOS 17.0.3; en_US) Cronet",
            "Referer":         "https://www.tiktok.com/",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {"tiktok": {"app_name": ["trill"], "app_version": ["26.2.0"]}},
    },
]


def _build_ydl_opts(strategy: dict, cookies: Optional[str],
                     proxy: Optional[str], timeout: int) -> dict:
    opts: dict = {
        "outtmpl":             os.path.join(config.DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "format":              (
            "bestvideo[ext=mp4][vcodec^=h264]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio/bestvideo+bestaudio/best"
        ),
        "merge_output_format": "mp4",
        "quiet":               True,
        "no_warnings":         False,
        "noplaylist":          True,
        "nocheckcertificate":  True,
        "socket_timeout":      timeout,
        "retries":             1,
        "fragment_retries":    2,
        "http_headers":        strategy["http_headers"],
    }
    if strategy.get("extractor_args"):
        opts["extractor_args"] = strategy["extractor_args"]
    if strategy.get("impersonate") and _HAS_IMPERSONATE:
        opts["impersonate"] = strategy["impersonate"]
    if cookies:
        opts["cookiefile"] = cookies
    if proxy:
        opts["proxy"] = proxy
    return opts


class _YTDLLogger:
    def debug(self, msg):
        if not msg.startswith("[debug]"):
            logger.debug(f"yt-dlp: {msg}")
    def warning(self, msg):
        logger.warning(f"yt-dlp: {msg}")
    def error(self, msg):
        logger.error(f"yt-dlp: {msg}")


def _ydl_attempt(url: str, strategy: dict, cookies: Optional[str],
                  proxy: Optional[str], timeout: int) -> str:
    downloaded: Optional[str] = None
    info_ref: dict = {}

    def hook(d):
        nonlocal downloaded
        if d["status"] == "finished":
            downloaded = d.get("filename") or d.get("info_dict", {}).get("_filename")

    opts = _build_ydl_opts(strategy, cookies, proxy, timeout)
    opts["logger"] = _YTDLLogger()
    opts["progress_hooks"] = [hook]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        info_ref.update(info or {})

    if downloaded is None:
        vid_id = info_ref.get("id", "unknown")
        downloaded = os.path.join(config.DOWNLOAD_DIR, f"{vid_id}.mp4")

    if not os.path.exists(downloaded):
        vid_id = info_ref.get("id", "")
        for f in os.listdir(config.DOWNLOAD_DIR):
            if vid_id and vid_id in f and f.endswith(".mp4"):
                downloaded = os.path.join(config.DOWNLOAD_DIR, f)
                break
        else:
            raise FileNotFoundError(f"Merged file not found: {downloaded}")

    size_mb = os.path.getsize(downloaded) / 1024 / 1024
    logger.info(f"[{strategy['name']}] {size_mb:.1f} MB → {os.path.basename(downloaded)}")
    return downloaded


# ── Phase 2: tikwm.com API ────────────────────────────────────────────────────

_TIKWM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.tikwm.com/",
    "Accept":  "application/json, */*",
}


def _tikwm_download(url: str) -> str:
    """
    Fetch video URL via tikwm.com public API then download it directly.
    Works from any IP — their servers handle TikTok authentication.
    Returns the path to the downloaded .mp4 file.
    """
    # Step 1: resolve video URL via tikwm API
    params  = urllib.parse.urlencode({"url": url, "hd": "1"})
    api_req = urllib.request.Request(
        f"https://www.tikwm.com/api/?{params}",
        headers=_TIKWM_HEADERS,
    )
    with urllib.request.urlopen(api_req, timeout=15) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"tikwm API error {data.get('code')}: {data.get('msg', '?')}")

    vdata     = data["data"]
    video_url = vdata.get("hdplay") or vdata.get("play")
    if not video_url:
        raise RuntimeError("tikwm returned no playable URL")

    # Step 2: stream-download the video
    fname  = f"tikwm_{int(time.time() * 1000)}.mp4"
    output = os.path.join(config.DOWNLOAD_DIR, fname)

    dl_req = urllib.request.Request(video_url, headers=_TIKWM_HEADERS)
    with urllib.request.urlopen(dl_req, timeout=60) as resp, open(output, "wb") as f:
        while True:
            chunk = resp.read(512 * 1024)  # 512 KB
            if not chunk:
                break
            f.write(chunk)

    size = os.path.getsize(output)
    if size < 50_000:
        os.remove(output)
        raise RuntimeError(f"tikwm: downloaded file too small ({size} bytes) — likely error page")

    logger.info(f"[tikwm] {size / 1024 / 1024:.1f} MB → {fname}")
    return output


# ── public entry point ────────────────────────────────────────────────────────

def download_tiktok(url: str) -> str:
    """
    Download a TikTok video without watermark.
    Returns the absolute path to a .mp4 file.
    Raises RuntimeError if all phases fail.
    """
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    cookies = _resolve_cookies()
    proxy   = os.environ.get("PROXY_URL")
    errors: list[str] = []

    # ── Phase 1: yt-dlp direct (fast timeout, skip on network block) ──────────
    network_blocked = False
    for strategy in _STRATEGIES:
        tag = strategy["name"]
        try:
            logger.info(f"Phase 1 [{tag}] proxy={'yes' if proxy else 'no'} cookies={'yes' if cookies else 'no'}")
            return _ydl_attempt(url, strategy, cookies, proxy=None, timeout=12)
        except Exception as exc:
            msg = str(exc)
            errors.append(f"[yt-dlp/{tag}] {msg[:180]}")
            if _is_unrecoverable(exc):
                logger.warning(f"Unrecoverable error — stopping: {msg[:100]}")
                break
            if _is_network_block(exc):
                network_blocked = True
                logger.warning(f"Network block detected on [{tag}] — skipping remaining yt-dlp strategies")
                break  # no point trying other yt-dlp strategies on same IP

    # ── Phase 2: tikwm.com API (any IP, third-party) ──────────────────────────
    logger.info("Phase 2 [tikwm] — third-party API")
    try:
        return _tikwm_download(url)
    except Exception as exc:
        errors.append(f"[tikwm] {str(exc)[:180]}")
        logger.warning(f"tikwm failed: {exc}")

    # ── Phase 3: yt-dlp through residential proxy (if PROXY_URL set) ──────────
    if proxy:
        logger.info(f"Phase 3 [proxy] {proxy[:40]}…")
        for strategy in _STRATEGIES[:2]:  # only try top 2 with proxy
            tag = strategy["name"]
            try:
                return _ydl_attempt(url, strategy, cookies, proxy=proxy, timeout=30)
            except Exception as exc:
                errors.append(f"[proxy/{tag}] {str(exc)[:180]}")
                if _is_unrecoverable(exc):
                    break

    # ── all phases failed ──────────────────────────────────────────────────────
    hint = ""
    if network_blocked and not proxy:
        hint = (
            "\n\nHint: TikTok is blocking this IP at the network level."
            "\n  • tikwm.com also failed — check if tikwm.com is reachable from your server"
            "\n  • Add a residential proxy: set PROXY_URL=socks5://user:pass@host:port"
        )
    raise RuntimeError(
        f"All download phases failed for {url}\n"
        + "\n".join(errors)
        + hint
    )
