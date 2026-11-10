#!/usr/bin/env python3
"""qa_search.py — full-text search over messages and voice transcripts.

The index lives in its own SQLite file (search.db), owned by Python. It must
NOT live in messages.db: the Go bridge uses a SQLite build without FTS5, and
triggers referencing an FTS table make every message insert fail.

Indexing is incremental and watermark-driven:
  messages.db.messages      -> search.db.messages_fts
  engine.db.transcripts     -> search.db.transcripts_fts

If FTS5 is unavailable, search() returns [] and callers keep the old path.
"""

import os
import re
import sqlite3

import _envload

_STOP = {
    "the", "and", "for", "with", "that", "this", "what", "when", "where",
    "who", "why", "how", "did", "does", "was", "were", "are", "is", "any",
    "all", "my", "me", "you", "your", "our", "from", "about", "into", "on",
    "in", "to", "of", "a", "an", "it", "be", "been", "has", "have", "had",
    "please", "tell", "give", "show", "list", "get", "got",
    "see", "saw", "say", "said", "mention", "mentioned", "talk", "talked",
    "discuss", "discussed", "went", "come", "came", "look", "looked",
    "find", "found", "think", "thought", "know", "knew",
}

_ENSURED = {}
_FTS_OK = None
_CHUNK = 4000


def _has_fts():
    global _FTS_OK
    if _FTS_OK is None:
        try:
            c = sqlite3.connect(":memory:")
            c.execute("CREATE VIRTUAL TABLE __probe USING fts5(a)")
            c.close()
            _FTS_OK = True
        except Exception:
            _FTS_OK = False
    return _FTS_OK


def search_db_path(messages_db=None):
    if _envload.getenv("NEMBU_SEARCH_DB", "MOSAIC_SEARCH_DB", ""):
        return _envload.getenv("NEMBU_SEARCH_DB", "MOSAIC_SEARCH_DB", "")
    base = os.path.dirname(messages_db) if messages_db else (
        os.getenv("STORE_DIR") or os.path.join(_envload.resolve_home(), "store"))
    return os.path.join(base, "search.db")


def _open(path, read_only=False):
    if read_only:
        return sqlite3.connect(f"file:{path}?mode=ro&_busy_timeout=3000", uri=True)
    return sqlite3.connect(path, timeout=10)


def _meta_get(con, key, default=""):
    row = con.execute("SELECT value FROM search_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(con, key, value):
    con.execute(
        "INSERT INTO search_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def ensure_index(messages_db, engine_db=None, search_db=None):
    """Create the Python-owned index and add new rows since the watermark."""
    if not _has_fts() or not messages_db or not os.path.exists(messages_db):
        return False
    search_db = search_db or search_db_path(messages_db)
    key = (messages_db, engine_db, search_db)
    if key in _ENSURED:
        return True
    try:
        con = _open(search_db)
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS search_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            con.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                "content, chat_jid UNINDEXED, msg_id UNINDEXED, "
                "timestamp UNINDEXED, tokenize='unicode61 remove_diacritics 2')"
            )
            con.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5("
                "text, chat_jid UNINDEXED, msg_id UNINDEXED, "
                "timestamp UNINDEXED, tokenize='unicode61 remove_diacritics 2')"
            )
            con.commit()
            _index_messages(con, messages_db)
            if engine_db and os.path.exists(engine_db):
                _index_transcripts(con, engine_db)
            _prune(con)
            con.commit()
        finally:
            con.close()
        _ENSURED[key] = True
        return True
    except Exception:
        return False


def _index_messages(con, messages_db):
    src = _open(messages_db, read_only=True)
    try:
        last = int(_meta_get(con, "messages_rowid", "0") or 0)
        max_row = src.execute("SELECT COALESCE(MAX(rowid),0) FROM messages").fetchone()[0]
        if max_row < last:
            # messages.db was rebuilt; start over.
            con.execute("DELETE FROM messages_fts")
            last = 0
        while True:
            rows = src.execute(
                "SELECT rowid, id, chat_jid, COALESCE(content,''), "
                "COALESCE(timestamp,'') FROM messages "
                "WHERE rowid > ? AND COALESCE(content,'') <> '' "
                "ORDER BY rowid LIMIT ?",
                (last, _CHUNK),
            ).fetchall()
            if not rows:
                break
            con.executemany(
                "INSERT INTO messages_fts(rowid, content, chat_jid, msg_id, timestamp) "
                "VALUES (?,?,?,?,?)",
                [(r[0], r[3], r[2], r[1], r[4]) for r in rows],
            )
            last = rows[-1][0]
            _meta_set(con, "messages_rowid", last)
            con.commit()
    finally:
        src.close()


def _index_transcripts(con, engine_db):
    src = _open(engine_db, read_only=True)
    try:
        try:
            last = int(_meta_get(con, "transcripts_rowid", "0") or 0)
            max_row = src.execute("SELECT COALESCE(MAX(rowid),0) FROM transcripts").fetchone()[0]
        except Exception:
            return
        if max_row < last:
            con.execute("DELETE FROM transcripts_fts")
            last = 0
        while True:
            rows = src.execute(
                "SELECT rowid, msg_id, chat_jid, text, COALESCE(created_at,'') "
                "FROM transcripts WHERE rowid > ? AND text <> '' "
                "ORDER BY rowid LIMIT ?",
                (last, _CHUNK),
            ).fetchall()
            if not rows:
                break
            con.executemany(
                "INSERT INTO transcripts_fts(rowid, text, chat_jid, msg_id, timestamp) "
                "VALUES (?,?,?,?,?)",
                [(r[0], r[3], r[2], r[1], r[4]) for r in rows],
            )
            last = rows[-1][0]
            _meta_set(con, "transcripts_rowid", last)
            con.commit()
    finally:
        src.close()


def _prune(con):
    """Drop index rows outside the retention window; cheap and idempotent."""
    import datetime
    days = _envload.getenv_int("NEMBU_HISTORY_DAYS", "MOSAIC_HISTORY_DAYS", 30)
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S")
    try:
        con.execute("DELETE FROM messages_fts WHERE timestamp < ?", (cutoff,))
        con.execute("DELETE FROM transcripts_fts WHERE timestamp < ?", (cutoff,))
        con.commit()
    except Exception:
        pass


def _tokens(query):
    toks = re.findall(r"[A-Za-z0-9\u0900-\u097F]+", (query or "").lower())
    out = []
    for t in toks:
        if len(t) < 3 or t in _STOP:
            continue
        if t not in out:
            out.append(t)
    return out[:10]


def search(messages_db, engine_db, query, start, end, limit=20, exclude=None,
           search_db=None):
    """Ranked hits from the Python-owned index."""
    if not ensure_index(messages_db, engine_db, search_db):
        return []
    toks = _tokens(query)
    if not toks:
        return []
    search_db = search_db or search_db_path(messages_db)
    joiner = " AND " if len(toks) >= 2 else " OR "
    match = joiner.join('"' + t.replace('"', '') + '"' for t in toks)
    start_s = start.strftime("%Y-%m-%d %H:%M:%S")
    end_s = end.strftime("%Y-%m-%d %H:%M:%S")
    exclude = exclude or set()
    hits = []
    try:
        con = _open(search_db, read_only=True)
        try:
            for table, col in (("messages_fts", "content"),
                               ("transcripts_fts", "text")):
                try:
                    rows = con.execute(
                        f"SELECT chat_jid, msg_id, timestamp, "
                        f"snippet({table}, 0, '', '', '…', 12) "
                        f"FROM {table} WHERE {table} MATCH ? "
                        f"AND timestamp >= ? AND timestamp <= ? "
                        f"ORDER BY bm25({table}) LIMIT ?",
                        (match, start_s, end_s, limit),
                    ).fetchall()
                except Exception:
                    rows = []
                for jid, mid, ts, snip in rows:
                    if jid and jid not in exclude:
                        hits.append({
                            "chat_jid": jid, "msg_id": str(mid or ""),
                            "timestamp": str(ts or ""), "snippet": snip or "",
                            "source": "message" if table == "messages_fts" else "transcript",
                        })
        finally:
            con.close()
    except Exception:
        return []
    return hits
