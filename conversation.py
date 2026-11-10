#!/usr/bin/env python3
"""conversation.py — Me-chat turn memory and follow-up resolution.

Everything the user sends in the Me chat is stored as a turn and
classified as question | alert_reply | command | note. Notes get no
reply. Memory lets "and what's pending there?" resolve "there" from
the ongoing thread, not just the last message.

Bot messages are excluded at the source: the bridge writes every
message it sends to bot_sends (messages.db); a turn whose msg_id is
there is never processed. The ◆ marker line is display only and never
a filter.
"""

import json
import os
import sqlite3

import mstate

MESSAGES_DB_PATH = os.getenv("MESSAGES_DB_PATH", os.path.join(mstate.STORE_DIR, "messages.db"))
BOT_SEND_TYPES = ("question", "alert_reply", "command", "note")

SCHEMA_EXECUTED = False


def _messages_ro():
    if not os.path.exists(MESSAGES_DB_PATH):
        return None
    try:
        con = sqlite3.connect(f"file:{MESSAGES_DB_PATH}?mode=ro", uri=True)
        return con
    except Exception:
        return None


def bot_sends_since():
    """IDs the bridge recorded as its own sends (bot_sends lives in
    messages.db; written by the Go bridge on every successful send)."""
    con = _messages_ro()
    if con is None:
        return set()
    try:
        try:
            rows = con.execute("SELECT id FROM bot_sends").fetchall()
        except Exception:
            return set()
        return {str(row[0]) for row in rows if row[0]}
    finally:
        con.close()


def fetch_new_mechat_turns():
    """Recent Me-chat messages not yet stored as turns and not bot sends.
    Returns list of dicts (msg_id, ts, body, is_voice, recency order)."""
    import mechat

    jids = mechat.mechat_jids()
    if not jids:
        return []
    con = _messages_ro()
    if con is None:
        return []
    marks = ",".join("?" * len(jids))
    try:
        rows = con.execute(
            f"SELECT id, timestamp, content, media_type, "
            f"COALESCE(quoted_id,''), COALESCE(quoted_sender,'') FROM messages "
            f"WHERE chat_jid IN ({marks}) AND (content IS NOT NULL AND TRIM(content)!='' "
            f"OR media_type='audio') ORDER BY timestamp DESC LIMIT 200",
            tuple(jids),
        ).fetchall()
    except Exception:
        try:
            rows = con.execute(
                f"SELECT id, timestamp, content, media_type, '', '' FROM messages "
                f"WHERE chat_jid IN ({marks}) AND (content IS NOT NULL AND TRIM(content)!='' "
                f"OR media_type='audio') ORDER BY timestamp DESC LIMIT 200",
                tuple(jids),
            ).fetchall()
        except Exception:
            return []
    finally:
        con.close()
    known = stored_ids()
    bots = bot_sends_since()
    out = []
    for mid, ts, content, media_type, quoted_id, quoted_sender in rows:
        if not mid or mid in known or str(mid) in bots:
            continue
        out.append({
            "msg_id": str(mid),
            "ts": ts,
            "quoted_id": str(quoted_id or ""),
            "quoted_sender": str(quoted_sender or ""),
            "body": (content or "").strip(),
            "is_voice": media_type == "audio",
        })
    out.reverse()
    return out


def stored_ids() -> set:
    c = mstate.connect()
    try:
        rows = c.execute("SELECT msg_id FROM turns WHERE msg_id IS NOT NULL").fetchall()
        return {r["msg_id"] for r in rows}
    finally:
        c.close()


def store_turn(msg_id: str, kind: str, text: str, quoted_alert_id=None, raw=None,
               transcribed=False) -> int:
    """Record one user turn. Dedupes by msg_id (existing ledger.py-style
    guard). Returns turn id (0 on dedupe)."""
    if not text and not transcribed:
        return 0
    c = mstate.connect()
    try:
        cur = c.execute(
            "INSERT OR IGNORE INTO turns (msg_id, created_at, kind, text, transcribed, "
            "quoted_alert_id, raw_json) VALUES (?,?,?,?,?,?,?)",
            (
                msg_id or None,
                mstate.now_local().isoformat(),
                kind if kind in BOT_SEND_TYPES else "note",
                (text or "")[:4000],
                1 if transcribed else 0,
                quoted_alert_id,
                json.dumps(raw or {}, ensure_ascii=False),
            ),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def recent_turns(limit: int = 12) -> list:
    """Most recent turns oldest-first, for context windows."""
    c = mstate.connect()
    try:
        rows = c.execute(
            "SELECT * FROM turns ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = [dict(r) for r in reversed(rows)]
        return out
    finally:
        c.close()


def resolve_quoted_chat(quoted_id: str):
    """Quoted message id -> (chat_jid, name) for a group or a direct chat."""
    if not quoted_id:
        return "", ""
    import engine

    c = mstate.connect()
    try:
        row = c.execute(
            "SELECT group_jid FROM sent_links WHERE wa_message_id=?", (quoted_id,)
        ).fetchone()
        chat = (row["group_jid"] if row else "") or ""
    finally:
        c.close()
    if not chat:
        try:
            con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
            try:
                r = con.execute(
                    "SELECT chat_jid FROM messages WHERE id=? LIMIT 1", (quoted_id,)
                ).fetchone()
                chat = (r[0] if r else "") or ""
            finally:
                con.close()
        except Exception:
            chat = ""
    if not chat or chat.endswith("@newsletter") or chat == "status@broadcast":
        return "", ""
    try:
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        try:
            r = con.execute("SELECT name FROM chats WHERE jid=?", (chat,)).fetchone()
            return chat, ((r[0] if r else "") or chat)
        finally:
            con.close()
    except Exception:
        return chat, chat


# Older callers used the group-specific name.
resolve_quoted_group = resolve_quoted_chat


def recent_dialogue(limit: int = 12) -> list:
    """Last Me-chat exchange with roles, from both sides of the conversation.

    User turns come from `turns`; Nembu's messages come from the outbox
    (only what was actually delivered). Roles matter: the model must know
    what the user said vs what Nembu answered. Legacy rows with role "mosaic"
    still render via dual-read.
    """
    import mechat

    items = []
    c = mstate.connect()
    try:
        for r in c.execute(
            "SELECT text, created_at FROM turns WHERE text IS NOT NULL AND TRIM(text)!='' "
            "ORDER BY id DESC LIMIT ?",
            (limit * 2,),
        ).fetchall():
            items.append({"role": "user", "text": (r["text"] or "").strip(), "at": r["created_at"]})
        jids = mechat.mechat_jids()
        if jids:
            marks = ",".join("?" * len(jids))
            rows = c.execute(
                f"SELECT body, created_at, kind FROM outbox WHERE target IN ({marks}) "
                f"AND status IN ('delivered','queued') AND body IS NOT NULL "
                f"ORDER BY id DESC LIMIT ?",
                tuple(jids) + (limit * 2,),
            ).fetchall()
            for r in rows:
                body = (r["body"] or "").lstrip("◆ ").strip()
                body = body.replace("_Mosaic_\n", "").replace("_Nembu_\n", "")
                if body:
                    items.append({"role": "nembu", "text": body, "at": r["created_at"]})
    finally:
        c.close()
    items.sort(key=lambda x: x.get("at") or "")
    # dedupe identical consecutive lines
    out = []
    for it in items:
        if out and out[-1]["role"] == it["role"] and out[-1]["text"] == it["text"]:
            continue
        out.append(it)
    return out[-limit:]


def dialogue_block(limit: int = 12) -> str:
    lines = []
    for it in recent_dialogue(limit):
        text = " ".join((it.get("text") or "").split())
        lines.append(f"{it['role']}: {text[:220]}")
    return "\n".join(lines) or "(none)"


def last_mentioned_group() -> str:
    """The last explicitly named group in conversation context — bot alert
    topics or user questions — for 'there' / 'that group' resolution.
    Falls back to scanning turn text for a known chat name."""
    c = mstate.connect()
    try:
        rows = c.execute(
            "SELECT raw_json, text FROM turns ORDER BY id DESC LIMIT 25"
        ).fetchall()
    finally:
        c.close()
    for r in reversed([dict(x) for x in rows]):
        raw = {}
        try:
            raw = json.loads(r["raw_json"] or "{}")
        except Exception:
            raw = {}
        jid = raw.get("group_jid") or raw.get("group")
        if jid:
            return jid

    try:
        import engine
        import re as _re
        import sqlite3
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        known = [(r[0], (r[1] or "").strip()) for r in con.execute(
            "SELECT jid, name FROM chats WHERE name IS NOT NULL AND LENGTH(name) >= 3"
        ).fetchall()]
        con.close()
    except Exception:
        return ""
    for r in reversed([dict(x) for x in rows]):
        text = " " + (r.get("text") or "").lower() + " "
        best = None
        for jid, nm in known:
            low = nm.lower()
            if not low or low.isdigit():
                continue
            if low in text:
                if best is None or len(low) > best[1]:
                    best = (jid, len(low))
        if best:
            return best[0]
    return ""


def store_answer(turn_key: str, full_text: str) -> None:
    c = mstate.connect()
    try:
        c.execute(
            "INSERT OR REPLACE INTO answers (turn_key, full_text, head_lines) "
            "VALUES (?,?,0)",
            (turn_key, full_text or ""),
        )
        c.commit()
    finally:
        c.close()


def chunk_more(max_chars: int = None) -> tuple:
    """Next chunk of the most recent long answer: (has_more, text)."""
    max_chars = max_chars or (mstate.MAX_BODY_CHARS - 200)
    c = mstate.connect()
    try:
        row = c.execute(
            "SELECT full_text, head_lines FROM answers ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        c.close()
    if not row:
        return "", ""
    full = row["full_text"] or ""
    if not full:
        return "", ""
    already = int(row["head_lines"] or 0)
    send_all = already < 0
    already = abs(already)
    lines = full.split("\n")
    while already < len(lines) and not lines[already].strip():
        already += 1
    remaining = lines[already:]
    if not remaining:
        return "", ""
    chunk = []
    used = 0
    for line in remaining:
        if not send_all and used + len(line) + 1 > max_chars and chunk:
            break
        chunk.append(line)
        used += len(line) + 1
    c = mstate.connect()
    try:
        c.execute(
            "UPDATE answers SET head_lines=? WHERE rowid="
            "(SELECT rowid FROM answers ORDER BY rowid DESC LIMIT 1)",
            (already + len(chunk),),
        )
        c.commit()
    finally:
        c.close()
    return "more", "\n".join(chunk)


