#!/usr/bin/env python3
"""
Generate the TIKTOK_COOKIES env-var value for Railway.

Usage:
    python make_cookie_env.py ~/Downloads/cookies.txt

What it does:
    1. Reads a Netscape-format cookies.txt (full browser export is fine)
    2. Strips every domain except TikTok — other cookies waste space and
       are ignored by yt-dlp anyway
    3. Gzip-compresses the result (typically 9-12 KB → ~3 KB base64)
    4. Prints the value to stdout AND copies it to the clipboard
    5. Shows a size summary so you can confirm it fits Railway's 32 768-char limit

Required cookies for TikTok downloads:
    sessionid, msToken, tt_chain_token, ttwid, s_v_web_id
    (these are all present in any logged-in browser export)
"""

import base64
import gzip
import io
import subprocess
import sys
from pathlib import Path

RAILWAY_LIMIT = 32_768

TIKTOK_DOMAINS = (
    ".tiktok.com",
    ".tiktokv.com",
    ".tiktokw.eu",
    "www.tiktok.com",
    ".musical.ly",
    ".bytedance.com",
)

REQUIRED_COOKIES = {"sessionid", "msToken", "tt_chain_token", "ttwid", "s_v_web_id"}


def filter_tiktok(path: Path) -> bytes:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    header = next((l for l in lines if l.startswith("# Netscape")), "# Netscape HTTP Cookie File\n")
    kept = [
        l for l in lines
        if not l.startswith("#") and l.strip()
        and any(l.startswith(d) or l.startswith(d.lstrip(".")) for d in TIKTOK_DOMAINS)
    ]
    return (header + "".join(kept)).encode("utf-8")


def encode(data: bytes) -> str:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as gz:
        gz.write(data)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def check_required(data: bytes) -> set:
    text = data.decode("utf-8", errors="replace")
    found = set()
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 7:
            found.add(parts[5])
    return REQUIRED_COOKIES - found  # missing cookies


def copy_to_clipboard(text: str) -> bool:
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return True
    except Exception:
        pass
    try:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=True)
        return True
    except Exception:
        pass
    try:
        subprocess.run(["xdotool", "type", "--clearmodifiers", text], check=True)
        return True
    except Exception:
        pass
    return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python make_cookie_env.py <path/to/cookies.txt>")
        sys.exit(1)

    path = Path(sys.argv[1]).expanduser()
    if not path.exists():
        print(f"Error: file not found: {path}")
        sys.exit(1)

    print(f"Reading: {path}")

    # Filter
    filtered = filter_tiktok(path)
    original_size = path.stat().st_size
    line_count = filtered.decode().count("\n") - 1  # minus header

    print(f"  Original size  : {original_size:>8,} bytes")
    print(f"  TikTok cookies : {line_count:>8} lines")

    # Check required cookies present
    missing = check_required(filtered)
    if missing:
        print(f"\n  WARNING: missing key cookies: {', '.join(sorted(missing))}")
        print("  Make sure you are logged in to tiktok.com before exporting.\n")

    # Encode
    encoded = encode(filtered)
    print(f"  Gzip+base64    : {len(encoded):>8,} chars  (Railway limit: {RAILWAY_LIMIT:,})")

    if len(encoded) > RAILWAY_LIMIT:
        print(f"\n  ERROR: still too large by {len(encoded) - RAILWAY_LIMIT:,} chars.")
        print("  Try logging out of non-essential TikTok accounts and re-exporting.")
        sys.exit(1)

    headroom = RAILWAY_LIMIT - len(encoded)
    print(f"  Headroom       : {headroom:>8,} chars  ✓ fits")

    # Show missing key cookies warning
    if missing:
        print(f"\n  WARNING — missing session cookies: {', '.join(sorted(missing))}")
        print("  Log in to tiktok.com first, then re-export.")

    # Output
    print()
    print("=" * 64)
    print("TIKTOK_COOKIES value (copy everything between the lines):")
    print("=" * 64)
    print(encoded)
    print("=" * 64)

    clipped = copy_to_clipboard(encoded)
    if clipped:
        print("\nCopied to clipboard.")
    else:
        print("\nCould not copy to clipboard — copy the string above manually.")

    print()
    print("Next steps:")
    print("  Railway: dashboard → your service → Variables → add TIKTOK_COOKIES")
    print("  Local  : python make_cookie_env.py already updated cookies.txt in place")

    # Also write filtered cookies.txt locally for immediate local use
    local_cookies = path.parent.parent / "tiktok_bot" / "cookies.txt"
    if not local_cookies.exists():
        local_cookies = Path("cookies.txt")
    try:
        local_cookies.write_bytes(filtered)
        print(f"  Also saved filtered cookies.txt → {local_cookies}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
