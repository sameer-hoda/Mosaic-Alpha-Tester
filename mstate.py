#!/usr/bin/env python3
"""state.py — durable engine state for the Nembu alpha (legacy Mosaic).

Fallback to ~/.mosaic when ~/.nembu missing.

One SQLite db ($STORE_DIR/engine.db) the alpha's new modules own:
outbox, ingest watermarks, triage results, alerts, Me-chat turns,
spend, watch status, overrides, Top-10 items, job watermarks.
bot_sends lives in messages.db and is written by the Go bridge.

Steering constants (env-overridable, never settings screens):
Radar scans every 2 hours 08:00-20:00 IST, 4 proactive pings/day,
confidence gates 0.75 info / 0.6 action, alert TTL 24h, watch window
14 capped by the 30-day retention, soft cap 30 groups and 30 DMs.
"""

import os
import pathlib
import sqlite3
from datetime import datetime, timedelta, timezone

import _envload


def _getenv(new_key: str, old_key: str = "", default: str = "") -> str:
    v = os.getenv(new_key)
    if v is not None and v != "":
        return v
    if old_key:
        ov = os.getenv(old_key)
        if ov is not None and ov != "":
            return ov
    return default


def _getenv_int(new_key: str, old_key: str = "", default: int = 0) -> int:
    raw = _getenv(new_key, old_key, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _getenv_float(new_key: str, old_key: str = "", default: float = 0.0) -> float:
    raw = _getenv(new_key, old_key, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def _resolve_home() -> str:
    # Prefer NEMBU_HOME, fallback to MOSAIC_HOME, then default ~/.nembu with legacy fallback
    new_home = os.getenv("NEMBU_HOME")
    old_home = os.getenv("MOSAIC_HOME")
    if new_home:
        return new_home
    if old_home:
        return old_home
    home_base = os.path.expanduser("~")
    nembu_default = os.path.join(home_base, ".nembu")
    mosaic_default = os.path.join(home_base, ".mosaic")
    if os.path.exists(nembu_default):
        return nembu_default
    if os.path.exists(mosaic_default):
        return mosaic_default
    return nembu_default


_dir = os.path.dirname(os.path.abspath(__file__))
_home_for_env = _resolve_home()
_envload.load(os.path.join(_home_for_env, ".env"))
# Also try legacy location if new missing
if not os.path.exists(os.path.join(_home_for_env, ".env")):
    _legacy = os.getenv("MOSAIC_HOME")
    if _legacy:
        _envload.load(os.path.join(_legacy, ".env"))
    else:
        _envload.load(os.path.join(os.path.expanduser("~/.mosaic"), ".env"))
STORE_DIR = os.getenv("STORE_DIR") or os.path.join(_dir, "store")

ENGINE_DB_PATH = os.getenv("ENGINE_DB_PATH", os.path.join(STORE_DIR, "engine.db"))

# One retention window for message history and everything derived from it.
RETENTION_DAYS = _getenv_int("NEMBU_HISTORY_DAYS", "MOSAIC_HISTORY_DAYS", 30)
# Raw prompts/responses are chat text: only record them for explicit debugging.
DEBUG_PROMPTS = _getenv("NEMBU_DEBUG_PROMPTS", "MOSAIC_DEBUG_PROMPTS", "") == "1"

QUIET_START = int(os.getenv("QUIET_START_HOUR", "20"))
QUIET_END = int(os.getenv("QUIET_END_HOUR", "8"))

# Radar: one scan every RADAR_EVERY_HOURS, 08:00-20:00 IST.
RADAR_START_HOUR = int(os.getenv("RADAR_START_HOUR", "8"))
RADAR_END_HOUR = int(os.getenv("RADAR_END_HOUR", "20"))
RADAR_EVERY_HOURS = int(os.getenv("RADAR_EVERY_HOURS", "2"))

MAX_PINGS_PER_DAY = int(os.getenv("MAX_PINGS_PER_DAY", "4"))
CONF_INFO = float(os.getenv("CONF_INFO_THRESHOLD", "0.75"))
CONF_ACTION = float(os.getenv("CONF_ACTION_THRESHOLD", "0.6"))
ALERT_TTL_HOURS = int(os.getenv("ALERT_TTL_HOURS", "24"))
WATCH_WINDOW_DAYS = min(int(os.getenv("WATCH_WINDOW_DAYS", "14")), RETENTION_DAYS)

MAX_BODY_CHARS = _getenv_int("NEMBU_BODY_CHARS", "MOSAIC_BODY_CHARS", 4000)
YES_WORDS = {"yes", "ok", "okay", "y", "okk", "sure", "done", "hmm"}
IST_FALLBACK = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT,
  dedupe_key TEXT UNIQUE,
  target TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  deliver_at TEXT,
  created_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  ref_chat TEXT,
  meta TEXT
);
CREATE TABLE IF NOT EXISTS watermarks (
  job TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (job, key)
);
CREATE TABLE IF NOT EXISTS triage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_jid TEXT NOT NULL,
  created_at TEXT NOT NULL,
  disposition TEXT NOT NULL,
  result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_jid TEXT NOT NULL,
  dedupe_key TEXT UNIQUE,
  kind TEXT NOT NULL,
  headline TEXT NOT NULL,
  why_you TEXT DEFAULT '',
  evidence_ids TEXT DEFAULT '[]',
  draft TEXT DEFAULT '',
  confidence REAL DEFAULT 0.0,
  topic_key TEXT DEFAULT '',
  state TEXT NOT NULL DEFAULT 'open',
  message_id TEXT,
  superseded_by INTEGER,
  created_at TEXT NOT NULL,
  expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_state ON alerts(state, chat_jid);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  msg_id TEXT UNIQUE,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  text TEXT DEFAULT '',
  transcribed INT NOT NULL DEFAULT 0,
  quoted_alert_id INTEGER,
  raw_json TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS group_cards (
  chat_jid TEXT PRIMARY KEY,
  objective TEXT DEFAULT '',
  themes TEXT DEFAULT '',
  latest TEXT DEFAULT '',
  updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS group_profile (
  chat_jid TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  aliases TEXT DEFAULT '[]',
  description TEXT DEFAULT '',
  topics TEXT DEFAULT '[]',
  eligible INTEGER NOT NULL DEFAULT 1,
  reason TEXT DEFAULT '',
  refreshed_at TEXT DEFAULT '',
  msgs_from TEXT DEFAULT '',
  msgs_to TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS selected_group (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  chat_jid TEXT DEFAULT '',
  name TEXT DEFAULT '',
  selected_at TEXT DEFAULT '',
  last_asked_at TEXT DEFAULT '',
  last_question TEXT DEFAULT '',
  last_answer TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sent_links (
  wa_message_id TEXT PRIMARY KEY,
  group_jid TEXT DEFAULT '',
  kind TEXT NOT NULL,        -- alert | answer | clarify | send | digest | reply
  ref_id TEXT DEFAULT '',
  sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_choice (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  question TEXT NOT NULL,
  candidates TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_pending (
  msg_id TEXT PRIMARY KEY,
  chat_jid TEXT NOT NULL,
  ts TEXT DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 1,
  next_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcripts (
  msg_id TEXT PRIMARY KEY,
  chat_jid TEXT,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_key TEXT UNIQUE,
  full_text TEXT NOT NULL,
  head_lines INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS retry_questions (
  msg_id TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  forced_group TEXT DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_at TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_pings (
  day TEXT PRIMARY KEY,
  delivered INTEGER NOT NULL DEFAULT 0,
  created INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS spend (
  day TEXT PRIMARY KEY,
  in_chars INTEGER NOT NULL DEFAULT 0,
  out_chars INTEGER NOT NULL DEFAULT 0,
  usd REAL NOT NULL DEFAULT 0,
  calls INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS watch_status (
  chat_jid TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  reason TEXT DEFAULT '',
  evidence TEXT DEFAULT '[]',
  weight REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS overrides (
  chat_jid TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  until TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS top10 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,
  thread_key TEXT DEFAULT '',
  ref_alert_id INTEGER,
  ref_loop_key TEXT,
  payload TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_top10_day ON top10(day, state);
CREATE TABLE IF NOT EXISTS jobs (
  job TEXT PRIMARY KEY,
  last_ran_at TEXT NOT NULL
);
"""


def _tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(os.getenv("TZ") or os.getenv("ENGINE_TZ") or "Asia/Kolkata")
    except Exception:
        return IST_FALLBACK


def local_tz():
    return _tz()


def now_local() -> datetime:
    fixed = _getenv("NEMBU_NOW", "MOSAIC_NOW", "").strip()
    if fixed:
        try:
            dt = datetime.fromisoformat(fixed)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz())
            return dt
        except (TypeError, ValueError):
            pass
    return datetime.now(_tz())


def is_utc_naive(dt: datetime) -> bool:
    return dt.tzinfo is None


def local_date_key(dt: datetime = None) -> str:
    dt = dt or now_local()
    if is_utc_naive(dt):
        dt = dt.replace(tzinfo=IST_FALLBACK)
    return dt.strftime("%Y-%m-%d")


def is_quiet(dt: datetime = None) -> bool:
    dt = dt or now_local()
    if is_utc_naive(dt):
        dt = dt.replace(tzinfo=IST_FALLBACK)
    h = dt.hour
    return h >= QUIET_START or h < QUIET_END


def event(msg_id: str, step: str, status: str = "info", detail: str = "",
          dur_ms=None, source: str = "engine", prompt: str = "", response: str = "") -> None:
    """Append one pipeline event (JSONL) for the TUI trace. Never raises."""
    import json as _json

    try:
        rec = {
            "ts": now_local().isoformat(),
            "msg_id": (msg_id or "")[:40],
            "source": source,
            "step": step,
            "status": status,
            "detail": (detail or "")[:300],
            "dur_ms": dur_ms,
        }
        if DEBUG_PROMPTS and (prompt or response):
            rec["prompt"] = (prompt or "")[:2000]
            rec["response"] = (response or "")[:2000]
        path = os.path.join(STORE_DIR, "pipeline.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def markerize(body: str) -> str:
    """◆ Nembu is a display marker only — never a content filter."""
    return "◆ " + body if body and not body.startswith("◆ ") else (body or "")


def _migrate(conn) -> None:
    """Additive migrations (SQLite has no IF NOT EXISTS for columns)."""
    def cols(table):
        try:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    adds = {
        "watch_status": {"weight": "REAL NOT NULL DEFAULT 0"},
        "pending_choice": {
            "expires_at": "TEXT DEFAULT ''",
            "mention": "TEXT DEFAULT ''",
        },
    }
    for table, columns in adds.items():
        existing = cols(table)
        if not existing:
            continue
        for name, ddl in columns.items():
            if name not in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                except sqlite3.Error:
                    pass
    conn.commit()


def connect(rw: bool = True):
    if not os.path.exists(STORE_DIR):
        os.makedirs(STORE_DIR, exist_ok=True)
    conn = sqlite3.connect(ENGINE_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    if rw:
        conn.executescript(SCHEMA)
        _migrate(conn)
    return conn


def set_watermark(job: str, key: str, value: str, conn=None) -> None:
    own = conn is None
    c = conn or connect()
    c.execute(
        "INSERT INTO watermarks (job, key, value) VALUES (?,?,?) "
        "ON CONFLICT(job, key) DO UPDATE SET value=excluded.value",
        (job, key, value),
    )
    c.commit()
    if own:
        c.close()


def get_watermark(job: str, key: str, conn=None):
    own = conn is None
    c = conn or connect()
    try:
        row = c.execute(
            "SELECT value FROM watermarks WHERE job=? AND key=? AND value!=''", (job, key)
        ).fetchone()
        return row["value"] if row else None
    finally:
        if own:
            c.close()


def job_last_ran(job: str, conn=None):
    own = conn is None
    c = conn or connect()
    try:
        row = c.execute("SELECT last_ran_at FROM jobs WHERE job=?", (job,)).fetchone()
        return row["last_ran_at"] if row else None
    finally:
        if own:
            c.close()


def job_mark_ran(job: str, when: str = None, conn=None) -> None:
    own = conn is None
    c = conn or connect()
    c.execute(
        "INSERT INTO jobs (job, last_ran_at) VALUES (?,?) "
        "ON CONFLICT(job) DO UPDATE SET last_ran_at=excluded.last_ran_at",
        (job, when or now_local().isoformat()),
    )
    c.commit()
    if own:
        c.close()


def pings_delivered(day: str = None, conn=None) -> int:
    own = conn is None
    c = conn or connect()
    try:
        row = c.execute(
            "SELECT delivered FROM daily_pings WHERE day=?", (day or local_date_key(),)
        ).fetchone()
        return row["delivered"] if row else 0
    finally:
        if own:
            c.close()


def ping_bump(kind: str, day: str = None, n: int = 1, conn=None) -> None:
    own = conn is None
    c = conn or connect()
    d = day or local_date_key()
    col = "delivered" if kind == "delivered" else "created"
    c.execute(
        f"INSERT INTO daily_pings (day, {col}) VALUES (?,?) "
        f"ON CONFLICT(day) DO UPDATE SET {col}={col}+?",
        (d, n, n),
    )
    c.commit()
    if own:
        c.close()


def note_pings_status(conn=None) -> str:
    used = pings_delivered(conn=conn)
    return f"{used}/{MAX_PINGS_PER_DAY} pings today"
