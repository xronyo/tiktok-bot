"""
Format stats dicts from database.py into human-readable Telegram messages.
"""

from datetime import datetime
from typing import Optional


def _fmt_bytes(b) -> str:
    if b is None:
        return "0 B"
    b = int(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _bar(done: int, total: int, width: int = 10) -> str:
    if total == 0:
        return "░" * width
    filled = round(done / total * width)
    return "█" * filled + "░" * (width - filled)


def format_today(stats: dict, streak: int) -> str:
    total = stats.get("total") or 0
    done = stats.get("done") or 0
    failed = stats.get("failed") or 0
    pending = total - done - failed
    hour = stats.get("most_active_hour")
    hour_str = f"{int(hour):02d}:00" if hour is not None else "—"

    lines = [
        "*Today's Report*",
        "",
        f"Downloaded:  `{done}`",
        f"Failed:      `{failed}`",
        f"Pending:     `{pending}`",
        f"Total links: `{total}`",
        "",
        f"Most active: `{hour_str}`",
        f"Streak:      `{streak}` day{'s' if streak != 1 else ''}",
        "",
        f"`{_bar(done, total)}` {done}/{total}",
    ]
    return "\n".join(lines)


def format_month(rows: list) -> str:
    if not rows:
        return "*Last 30 Days* — no data yet."

    lines = ["*Last 30 Days*", ""]
    total_done = 0
    for r in rows:
        day = r.get("day", "?")
        done = r.get("done") or 0
        failed = r.get("failed") or 0
        total = r.get("total") or 0
        total_done += done
        bar = _bar(done, total, 6)
        lines.append(f"`{day}` {bar} {done}✓ {failed}✗")

    lines.append("")
    lines.append(f"Total success: `{total_done}`")
    return "\n".join(lines)


def format_total(stats: dict) -> str:
    total = stats.get("total") or 0
    done = stats.get("done") or 0
    failed = stats.get("failed") or 0
    total_bytes = stats.get("total_bytes")
    first = stats.get("first_download", "—")
    last = stats.get("last_download", "—")

    success_pct = round(done / total * 100) if total else 0

    lines = [
        "*All-Time Stats*",
        "",
        f"Total links:   `{total}`",
        f"Successful:    `{done}` ({success_pct}%)",
        f"Failed:        `{failed}`",
        f"Data saved:    `{_fmt_bytes(total_bytes)}`",
        "",
        f"First link:    `{first[:16] if first else '—'}`",
        f"Last success:  `{last[:16] if last else '—'}`",
    ]
    return "\n".join(lines)


def format_failed(rows: list) -> str:
    if not rows:
        return "*Failed Today* — none! 🎉"

    lines = ["*Failed Today*", ""]
    for r in rows:
        url = r.get("url", "?")
        err = (r.get("error") or "unknown error")[:120]
        retries = r.get("retry_count") or 0
        short = url.split("?")[0][-40:]
        lines.append(f"• `...{short}`")
        lines.append(f"  Retries: {retries} | _{err}_")
        lines.append("")

    return "\n".join(lines).strip()


def format_last(rows: list) -> str:
    if not rows:
        return "*Last Downloads* — none yet."

    lines = ["*Last 5 Downloads*", ""]
    for i, r in enumerate(rows, 1):
        url = r.get("url", "?")
        size = _fmt_bytes(r.get("file_size"))
        ts = r.get("completed_at", "?")[:16]
        short = url.split("?")[0][-35:]
        lines.append(f"{i}. `...{short}`")
        lines.append(f"   {ts} · {size}")

    return "\n".join(lines)


def format_time(session: Optional[dict], fallback_start: Optional[datetime]) -> str:
    if session:
        started = session.get("started_at", "")
        try:
            start_dt = datetime.fromisoformat(started)
        except Exception:
            start_dt = fallback_start
    else:
        start_dt = fallback_start

    now = datetime.now()
    if start_dt:
        diff = now - start_dt
        h, rem = divmod(int(diff.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        duration = f"{h}h {m}m {s}s"
        start_str = start_dt.strftime("%Y-%m-%d %H:%M")
    else:
        duration = "—"
        start_str = "—"

    lines = [
        "*Session Time*",
        "",
        f"Started:  `{start_str}`",
        f"Now:      `{now.strftime('%Y-%m-%d %H:%M')}`",
        f"Duration: `{duration}`",
    ]
    return "\n".join(lines)


def format_help() -> str:
    return (
        "*TikTok Bot Commands*\n"
        "\n"
        "Just send any TikTok link — the bot downloads it automatically.\n"
        "\n"
        "*Stats*\n"
        "/today  — daily report: downloads, success, failed, most active hour\n"
        "/month  — last 30 days breakdown\n"
        "/total  — all-time statistics\n"
        "/fail   — failed downloads with error reasons\n"
        "/q      — current queue status\n"
        "/last   — last 5 downloaded clips\n"
        "/time   — session start, end, duration\n"
        "\n"
        "*Actions*\n"
        "/retry  — retry all failed downloads from today\n"
        "/stop   — pause the bot (links are still saved)\n"
        "/go     — resume the bot\n"
        "\n"
        "*Extras*\n"
        "🏆 Personal best notification when daily record is broken\n"
        "🔥 Streak counter — days in a row used\n"
        "📊 Weekly Sunday summary automatically sent at 20:00\n"
    )


def format_weekly_summary(rows: list) -> str:
    if not rows:
        return "📊 *Weekly Summary* — no downloads this week."

    total = sum(r.get("done", 0) or 0 for r in rows)
    best = max(rows, key=lambda r: r.get("done", 0) or 0)
    worst = min(rows, key=lambda r: r.get("done", 0) or 0)

    lines = [
        "📊 *Weekly Summary*",
        "",
        f"Total downloads: `{total}`",
        "",
    ]

    for r in rows:
        day = r.get("day", "?")
        done = r.get("done") or 0
        t = r.get("total") or 0
        bar = _bar(done, t, 8)
        lines.append(f"`{day}` {bar} {done}")

    lines.append("")
    lines.append(f"🏆 Best:  `{best['day']}` — {best.get('done',0)} downloads")
    if worst["day"] != best["day"]:
        lines.append(f"📉 Worst: `{worst['day']}` — {worst.get('done',0)} downloads")

    return "\n".join(lines)
