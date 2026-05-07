import sqlite3
import os
import threading
from contextlib import contextmanager
from datetime import date, timedelta


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS downloads (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    url         TEXT    NOT NULL,
                    chat_id     INTEGER NOT NULL,
                    message_id  INTEGER,
                    user_id     INTEGER,
                    username    TEXT,
                    status      TEXT DEFAULT 'pending',
                    created_at  TEXT DEFAULT (datetime('now')),
                    started_at  TEXT,
                    completed_at TEXT,
                    error       TEXT,
                    filename    TEXT,
                    file_size   INTEGER,
                    retry_count INTEGER DEFAULT 0,
                    video_id    TEXT
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT DEFAULT (datetime('now')),
                    ended_at   TEXT,
                    is_active  INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS bot_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_dl_status  ON downloads(status);
                CREATE INDEX IF NOT EXISTS idx_dl_created ON downloads(created_at);
                CREATE INDEX IF NOT EXISTS idx_dl_chat    ON downloads(chat_id);
            """)

    # ──────────────── writes ────────────────

    def save_link(self, url: str, chat_id: int, message_id: int,
                  user_id: int, username: str) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO downloads (url,chat_id,message_id,user_id,username,status)"
                " VALUES (?,?,?,?,?,'pending')",
                (url, chat_id, message_id, user_id, username),
            )
            return cur.lastrowid

    def update_status(self, download_id: int, status: str, **kwargs):
        fields = {"status": status}
        if status == "downloading":
            fields["started_at"] = "datetime('now')"
        if status in ("done", "failed"):
            fields["completed_at"] = "datetime('now')"
        # normal scalar kwargs
        for k, v in kwargs.items():
            fields[k] = v

        # separate literal SQL expressions from plain values
        set_parts = []
        values = []
        for k, v in fields.items():
            if isinstance(v, str) and v.startswith("datetime("):
                set_parts.append(f"{k} = {v}")
            else:
                set_parts.append(f"{k} = ?")
                values.append(v)
        values.append(download_id)

        with self._lock, self._conn() as c:
            c.execute(
                f"UPDATE downloads SET {', '.join(set_parts)} WHERE id = ?",
                values,
            )

    def increment_retry(self, download_id: int):
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE downloads SET retry_count = retry_count + 1,"
                " status = 'retrying' WHERE id = ?",
                (download_id,),
            )

    def start_session(self) -> int:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE sessions SET is_active=0, ended_at=datetime('now')"
                " WHERE is_active=1"
            )
            cur = c.execute("INSERT INTO sessions DEFAULT VALUES")
            return cur.lastrowid

    def end_session(self):
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE sessions SET is_active=0, ended_at=datetime('now')"
                " WHERE is_active=1"
            )

    def set_state(self, key: str, value: str):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO bot_state (key,value) VALUES (?,?)",
                (key, value),
            )

    # ──────────────── reads ────────────────

    def get_state(self, key: str, default=None):
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM bot_state WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def get_active_session(self):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE is_active=1"
                " ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def get_pending_downloads(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM downloads WHERE status IN ('pending','retrying')"
                " ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    # ──────────── stats ────────────────────

    def get_today_stats(self):
        with self._conn() as c:
            today = date.today().isoformat()
            row = c.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='done'   THEN 1 ELSE 0 END) as done,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
                FROM downloads WHERE date(created_at)=?
            """, (today,)).fetchone()
            hour = c.execute("""
                SELECT strftime('%H', created_at) as h, COUNT(*) as c
                FROM downloads WHERE date(created_at)=?
                GROUP BY h ORDER BY c DESC LIMIT 1
            """, (today,)).fetchone()
            d = dict(row)
            d["most_active_hour"] = hour["h"] if hour else None
            return d

    def get_month_stats(self):
        with self._conn() as c:
            rows = c.execute("""
                SELECT
                    date(created_at) as day,
                    COUNT(*) as total,
                    SUM(CASE WHEN status='done'   THEN 1 ELSE 0 END) as done,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
                FROM downloads
                WHERE created_at >= datetime('now','-30 days')
                GROUP BY day ORDER BY day DESC
            """).fetchall()
            return [dict(r) for r in rows]

    def get_total_stats(self):
        with self._conn() as c:
            row = c.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='done'   THEN 1 ELSE 0 END) as done,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                    SUM(file_size) as total_bytes,
                    MIN(created_at) as first_download,
                    MAX(CASE WHEN status='done' THEN completed_at END) as last_download
                FROM downloads
            """).fetchone()
            return dict(row) if row else {}

    def get_failed_today(self):
        with self._conn() as c:
            today = date.today().isoformat()
            rows = c.execute("""
                SELECT url, error, retry_count, created_at
                FROM downloads
                WHERE date(created_at)=? AND status='failed'
                ORDER BY created_at DESC
            """, (today,)).fetchall()
            return [dict(r) for r in rows]

    def get_queue_status(self):
        with self._conn() as c:
            rows = c.execute("""
                SELECT status, COUNT(*) as c FROM downloads
                WHERE status IN ('pending','downloading','processing','retrying')
                GROUP BY status
            """).fetchall()
            return {r["status"]: r["c"] for r in rows}

    def get_last_downloads(self, n: int = 5):
        with self._conn() as c:
            rows = c.execute("""
                SELECT url, status, completed_at, file_size, created_at
                FROM downloads WHERE status='done'
                ORDER BY completed_at DESC LIMIT ?
            """, (n,)).fetchall()
            return [dict(r) for r in rows]

    def get_failed_for_retry(self, today_only: bool = True):
        with self._conn() as c:
            if today_only:
                today = date.today().isoformat()
                rows = c.execute("""
                    SELECT id, url, chat_id, message_id FROM downloads
                    WHERE status='failed' AND date(created_at)=?
                """, (today,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT id,url,chat_id,message_id FROM downloads WHERE status='failed'"
                ).fetchall()
            return [dict(r) for r in rows]

    def get_best_day(self):
        with self._conn() as c:
            row = c.execute("""
                SELECT date(created_at) as day, COUNT(*) as c
                FROM downloads WHERE status='done'
                GROUP BY day ORDER BY c DESC LIMIT 1
            """).fetchone()
            return dict(row) if row else {"day": None, "c": 0}

    def get_streak(self) -> int:
        with self._conn() as c:
            rows = c.execute("""
                SELECT date(created_at) as day
                FROM downloads WHERE status='done'
                GROUP BY day ORDER BY day DESC
            """).fetchall()
        if not rows:
            return 0

        days = [date.fromisoformat(r["day"]) for r in rows]
        today = date.today()
        streak = 0
        expected = today

        for d in days:
            if d == expected:
                streak += 1
                expected -= timedelta(days=1)
            elif d == today - timedelta(days=1) and streak == 0:
                streak = 1
                expected = d - timedelta(days=1)
            else:
                break
        return streak

    def get_week_stats(self):
        with self._conn() as c:
            rows = c.execute("""
                SELECT
                    date(created_at) as day,
                    COUNT(*) as total,
                    SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
                FROM downloads
                WHERE created_at >= datetime('now','-7 days')
                GROUP BY day ORDER BY day ASC
            """).fetchall()
            return [dict(r) for r in rows]

    def get_daily_counts_for_best(self):
        with self._conn() as c:
            rows = c.execute("""
                SELECT date(created_at) as day, COUNT(*) as c
                FROM downloads WHERE status='done'
                GROUP BY day ORDER BY day DESC
            """).fetchall()
            return [(r["day"], r["c"]) for r in rows]
