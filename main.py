"""
TikTok Downloader Bot — main entry point.

Architecture:
  • One asyncio.Queue feeds N background workers (QUEUE_WORKERS).
  • Every TikTok URL is persisted to SQLite the instant it arrives.
  • Workers survive crash/restart: pending rows are re-queued on startup.
  • All DB I/O is wrapped in asyncio.to_thread so the event loop stays free.
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, time as dtime
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from database import Database
from downloader import download_tiktok
from processor import process_video
from stats import (
    format_failed,
    format_help,
    format_last,
    format_month,
    format_time,
    format_today,
    format_total,
    format_weekly_summary,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(config.DATA_DIR, "bot.log"), encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("tiktok_bot")

# ── globals (set during post_init) ────────────────────────────────────────────
db: Optional[Database] = None
download_queue: asyncio.Queue = asyncio.Queue()
is_paused: bool = False
session_start: Optional[datetime] = None
active_chat_id: Optional[int] = None

# Compile patterns once
_URL_RE = re.compile(
    "|".join(config.TIKTOK_PATTERNS),
    re.IGNORECASE,
)


# ══════════════════════════════════════════════════════════════════════════════
#  URL detection
# ══════════════════════════════════════════════════════════════════════════════

def find_tiktok_urls(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;)")
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  Background download worker
# ══════════════════════════════════════════════════════════════════════════════

async def _do_download(app: Application, download_id: int, url: str, chat_id: int):
    """Download → process → send one video.  Handles retries internally."""
    last_error = ""

    for attempt in range(1, config.MAX_RETRIES + 2):  # +1 for initial try
        try:
            await asyncio.to_thread(db.update_status, download_id, "downloading")
            dl_path = await asyncio.to_thread(download_tiktok, url)

            await asyncio.to_thread(db.update_status, download_id, "processing")
            proc_path, fsize = await asyncio.to_thread(process_video, dl_path)

            with open(proc_path, "rb") as f:
                await app.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )

            await asyncio.to_thread(
                db.update_status,
                download_id,
                "done",
                filename=os.path.basename(proc_path),
                file_size=fsize,
            )

            for p in (dl_path, proc_path):
                try:
                    os.remove(p)
                except OSError:
                    pass

            await _check_personal_best(app, chat_id)
            return  # success

        except Exception as exc:
            last_error = str(exc)
            logger.warning(f"Attempt {attempt} failed for {url}: {last_error[:200]}")

            if attempt <= config.MAX_RETRIES:
                await asyncio.to_thread(db.increment_retry, download_id)
                wait = 2 ** attempt
                logger.info(f"Retrying in {wait}s (attempt {attempt}/{config.MAX_RETRIES})")
                await asyncio.sleep(wait)
            else:
                await asyncio.to_thread(
                    db.update_status,
                    download_id,
                    "failed",
                    error=last_error[:500],
                    retry_count=attempt - 1,
                )
                short_url = url[:60] + ("..." if len(url) > 60 else "")
                short_err = last_error[:150] + ("..." if len(last_error) > 150 else "")
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ *Failed after {config.MAX_RETRIES} retries*\n"
                        f"`{short_url}`\n\n"
                        f"_{short_err}_"
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                )


async def _worker(app: Application):
    """Long-running coroutine — one per QUEUE_WORKERS."""
    while True:
        try:
            if is_paused:
                await asyncio.sleep(0.5)
                continue

            try:
                item = download_queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.25)
                continue

            download_id, url, chat_id = item
            try:
                await _do_download(app, download_id, url, chat_id)
            except Exception as e:
                logger.error(f"Unhandled worker error: {e}", exc_info=True)
                try:
                    await asyncio.to_thread(
                        db.update_status, download_id, "failed", error=str(e)[:500]
                    )
                except Exception:
                    pass
            finally:
                download_queue.task_done()

        except Exception as e:
            logger.critical(f"Worker crashed: {e}", exc_info=True)
            await asyncio.sleep(1)


# ══════════════════════════════════════════════════════════════════════════════
#  Personal best tracker
# ══════════════════════════════════════════════════════════════════════════════

async def _check_personal_best(app: Application, chat_id: int):
    today_stats = await asyncio.to_thread(db.get_today_stats)
    today_count = today_stats.get("done") or 0
    if today_count < 2:
        return

    best = await asyncio.to_thread(db.get_best_day)
    prev_best = best.get("c") or 0

    if today_count > prev_best:
        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🏆 *Personal Best!*\n"
                f"{today_count} downloads today "
                f"(previous record: {prev_best})"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Message handler — URL detection
# ══════════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_chat_id

    if not update.message or not update.message.text:
        return

    text = update.message.text
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    user = update.effective_user

    urls = find_tiktok_urls(text)
    if not urls:
        return

    active_chat_id = chat_id
    await asyncio.to_thread(db.set_state, "active_chat_id", str(chat_id))

    for url in urls:
        # ── SAVE FIRST, always, before anything else ──
        download_id = await asyncio.to_thread(
            db.save_link,
            url,
            chat_id,
            message_id,
            user.id if user else 0,
            user.username or "" if user else "",
        )
        await download_queue.put((download_id, url, chat_id))

    if len(urls) == 1:
        await update.message.reply_text("⬇️ Downloading…")
    else:
        await update.message.reply_text(f"⬇️ Downloading {len(urls)} videos…")


# ══════════════════════════════════════════════════════════════════════════════
#  Command handlers
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_chat_id
    active_chat_id = update.effective_chat.id
    await asyncio.to_thread(db.set_state, "active_chat_id", str(active_chat_id))
    await update.message.reply_text(
        "*TikTok Downloader Bot*\n\n"
        "Send any TikTok link — I'll strip the watermark and send it back.\n\n"
        "Use /help to see all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await asyncio.to_thread(db.get_today_stats)
    streak = await asyncio.to_thread(db.get_streak)
    await update.message.reply_text(
        format_today(stats, streak), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.get_month_stats)
    await update.message.reply_text(
        format_month(rows), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await asyncio.to_thread(db.get_total_stats)
    await update.message.reply_text(
        format_total(stats), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_fail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.get_failed_today)
    await update.message.reply_text(
        format_failed(rows), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_counts = await asyncio.to_thread(db.get_queue_status)
    mem_size = download_queue.qsize()

    lines = ["*Queue Status*", ""]
    lines.append(f"In-memory queue: `{mem_size}`")
    for status, count in db_counts.items():
        lines.append(f"{status.capitalize()}: `{count}`")
    if is_paused:
        lines.append("\n⏸ Bot is *paused* — send /go to resume")

    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.get_last_downloads, 5)
    await update.message.reply_text(
        format_last(rows), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.get_failed_for_retry, True)
    if not rows:
        await update.message.reply_text("No failed downloads from today to retry.")
        return

    for r in rows:
        await asyncio.to_thread(db.update_status, r["id"], "pending")
        await download_queue.put((r["id"], r["url"], r["chat_id"]))

    await update.message.reply_text(f"♻️ Re-queued *{len(rows)}* downloads", parse_mode=ParseMode.MARKDOWN)


async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = await asyncio.to_thread(db.get_active_session)
    await update.message.reply_text(
        format_time(session, session_start), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_paused
    is_paused = True
    await asyncio.to_thread(db.set_state, "is_paused", "1")
    await update.message.reply_text(
        "⏸ *Bot paused.*\nNew links are still saved — send /go to resume.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_paused
    is_paused = False
    await asyncio.to_thread(db.set_state, "is_paused", "0")
    q = download_queue.qsize()
    await update.message.reply_text(
        f"▶️ *Bot resumed.* {q} item(s) in queue.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from downloader import _resolve_cookies
    import os
    path = _resolve_cookies()
    if path:
        size = os.path.getsize(path)
        source = (
            "TIKTOK_COOKIES_FILE env" if os.environ.get("TIKTOK_COOKIES_FILE") == path
            else "TIKTOK_COOKIES env (decoded)" if "tiktok_cookies_" in path
            else "local cookies.txt"
        )
        # Count domains in the cookie file
        try:
            with open(path) as f:
                lines = [l for l in f if not l.startswith("#") and l.strip()]
            domains = {l.split("\t")[0] for l in lines if "\t" in l}
        except Exception:
            lines, domains = [], set()
        await update.message.reply_text(
            f"*Cookie Status: Active*\n\n"
            f"Source: `{source}`\n"
            f"Size: `{size:,}` bytes\n"
            f"Lines: `{len(lines)}`\n"
            f"Domains: `{', '.join(sorted(domains)[:5]) or 'unknown'}`\n\n"
            f"Cookies expire ~30 days after export.\n"
            f"Re-export from browser if downloads start failing.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "*Cookie Status: None*\n\n"
            "Downloads may fail with `status code 0`.\n\n"
            "*To fix:*\n"
            "1. Install *Get cookies.txt LOCALLY* Chrome extension\n"
            "2. Log into tiktok.com, export cookies.txt\n"
            "3. Place file at `~/tiktok_bot/cookies.txt`\n\n"
            "*For Railway:*\n"
            "`base64 < cookies.txt | tr -d '\\n'`\n"
            "→ set as `TIKTOK_COOKIES` env var in dashboard",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(format_help(), parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
#  Scheduled jobs
# ══════════════════════════════════════════════════════════════════════════════

async def _weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    if not active_chat_id:
        return
    rows = await asyncio.to_thread(db.get_week_stats)
    text = format_weekly_summary(rows)
    await context.bot.send_message(
        chat_id=active_chat_id, text=text, parse_mode=ParseMode.MARKDOWN
    )


# ══════════════════════════════════════════════════════════════════════════════
#  App lifecycle
# ══════════════════════════════════════════════════════════════════════════════

async def post_init(app: Application):
    global db, is_paused, session_start, active_chat_id

    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(config.PROCESSED_DIR, exist_ok=True)

    db = Database(config.DB_PATH)

    # Restore pause state
    is_paused = db.get_state("is_paused", "0") == "1"

    # Restore last active chat so weekly summary works after restart
    saved = db.get_state("active_chat_id")
    if saved:
        try:
            active_chat_id = int(saved)
        except ValueError:
            pass

    # Start session
    db.start_session()
    session_start = datetime.now()

    # Restore pending / retrying downloads into queue
    pending = db.get_pending_downloads()
    for row in pending:
        await download_queue.put((row["id"], row["url"], row["chat_id"]))
    if pending:
        logger.info(f"Restored {len(pending)} pending download(s) from DB")

    # Launch background workers
    for _ in range(config.QUEUE_WORKERS):
        asyncio.create_task(_worker(app))

    logger.info(
        f"Bot ready | paused={is_paused} | workers={config.QUEUE_WORKERS} "
        f"| queue={download_queue.qsize()}"
    )


async def post_shutdown(app: Application):
    if db:
        db.end_session()
    logger.info("Session ended, bot shutdown complete.")


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # ── command handlers ──────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("total", cmd_total))
    app.add_handler(CommandHandler("fail", cmd_fail))
    app.add_handler(CommandHandler("q", cmd_queue))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("time", cmd_time))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("go", cmd_go))
    app.add_handler(CommandHandler("cookies", cmd_cookies))
    app.add_handler(CommandHandler("help", cmd_help))

    # ── message handler ───────────────────────────────────────────────────────
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # ── weekly Sunday summary at 20:00 ────────────────────────────────────────
    app.job_queue.run_daily(
        _weekly_summary,
        time=dtime(20, 0, 0),
        days=(6,),          # Sunday = 6 in python-telegram-bot
        name="weekly_summary",
    )

    logger.info("Starting polling…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
