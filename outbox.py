#!/usr/bin/env python3
"""outbox.py — the durable delivery queue between an approved alert/draft
and its Me-chat (or group) send.

Every proactive send is written here BEFORE any attempt. Statuses:
queued → delivered | failed after backoff | held_unknown | expired_quiet.

Unknown outcome (timeout after the request may have been accepted):
on recovery the sender reconciles against recent outgoing messages in
messages.db for the target chat (exact content match); an inconclusive
check leaves the row pending (held_unknown) and it is surfaced, never
resent on a guess. Same rule for approved group sends via ref_chat.
Duplicates are made rare by dedupe_key + reconcile-first; no
exactly-once guarantee is claimed.

Overnight queueing is gone: proactive notices created after 20:00 are
expired_quiet, never combined into a morning message. Replies to the user
are always delivered immediately.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta

import mstate

# 15s, 1m, 5m, 15m, then 1h repeats: about six hours before giving up.
BACKOFF_SECONDS = [15, 60, 300, 900, 3600]
MAX_ATTEMPTS = 8

MESSAGES_DB_PATH = os.getenv("MESSAGES_DB_PATH", os.path.join(mstate.STORE_DIR, "messages.db"))


def _bridge_connected() -> bool:
    """Pause the whole drain while the bridge is down or reconnecting."""
    import json as _json
    import urllib.request

    from mechat import bridge_token

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8090/api/status",
            headers={"X-Bridge-Token": bridge_token()},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = _json.loads(resp.read().decode())
        return bool(data.get("connected"))
    except Exception:
        return False


def _send_via_bridge(jid: str, text: str) -> tuple:
    """Post to the loopback API. Returns (outcome, error, wa_message_id).

    outcome: ok | not_sent | paused (bridge not connected) | permanent | unknown
    """
    import json as _json
    import socket
    import urllib.error
    import urllib.request

    from mechat import bridge_token

    url = os.getenv("WA_API_URL", "http://127.0.0.1:8090/api/send")
    req = urllib.request.Request(
        url,
        data=_json.dumps({"recipient": jid, "message": text}).encode(),
        headers={"Content-Type": "application/json", "X-Bridge-Token": bridge_token()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            wa_id = ""
            try:
                wa_id = str((_json.loads(body.decode()) or {}).get("message_id") or "")
            except Exception:
                wa_id = ""
            return "ok", "", wa_id
    except urllib.error.HTTPError as exc:
        body = exc.read()
        detail = f"HTTP {exc.code}: {body[:120]!r}"
        if exc.code == 503:
            return "paused", detail, ""
        if exc.code == 400:
            return "permanent", detail, ""
        if exc.code >= 500:
            return "not_sent", detail, ""
        return "permanent", detail, ""
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return "paused", f"connection refused: {exc.reason}", ""
        if "timed out" in str(exc).lower():
            return "unknown", f"{type(exc).__name__}: {exc}", ""
        return "not_sent", f"{type(exc).__name__}: {exc}", ""
    except (socket.timeout, TimeoutError):
        return "unknown", "read timeout (send status unknown)", ""
    except Exception as exc:
        return "unknown", f"{type(exc).__name__}: {exc}", ""


def _notify_failure(row, meta: dict) -> None:
    """One honest note when a send is permanently lost."""
    import alerts as _alerts
    import mechat

    me = mechat.get_mechat_jid()
    if not me:
        return
    if row["kind"] == "alert_send":
        alid = meta.get("alert_id")
        if alid:
            _alerts.set_state(alid, "draft_pending")
        chat = _alerts._chat_name(row["ref_chat"] or "")
        enqueue(
            "reply", f"failed-send:{row['id']}", me,
            f"◆ Your message to {chat} didn't go through. Reply *send* to try again.",
            ref_chat=row["ref_chat"],
        )
        return
    if row["kind"] == "reply" and meta.get("link_kind") == "answer":
        question = (meta.get("question") or "").strip()
        snippet = f"'{question[:60]}'" if question else "an earlier answer"
        enqueue(
            "reply", f"failed-answer:{mstate.local_date_key()}", me,
            f"◆ I couldn't deliver an earlier answer to {snippet}. Ask again?",
            ref_chat=row["ref_chat"],
        )


def _record_sent_link(row, wa_id: str) -> None:
    """Map a WhatsApp message id back to what Nembu meant by it.

    A swipe-reply to that message resolves to the group it was about:
    quoted_id -> sent_links.group_jid. Written for alerts, answers, clarify
    questions, user-approved sends and digests.
    """
    if not wa_id:
        return
    try:
        import json as _json

        meta = _json.loads(row["meta"] or "{}")
    except Exception:
        meta = {}
    kind = row["kind"] or ""
    link_kind, group, ref_id = "reply", "", ""
    if kind in ("alert_info", "alert_action", "alert_followup"):
        link_kind, group = "alert", row["ref_chat"] or ""
        ref_id = str(meta.get("alert_id") or "")
    elif kind == "alert_send":
        link_kind, group = "send", row["ref_chat"] or ""
        ref_id = str(meta.get("alert_id") or "")
    elif kind == "daily_digest":
        link_kind = "digest"
    else:
        link_kind = str(meta.get("link_kind") or "reply")
        group = str(meta.get("group_jid") or row["ref_chat"] or "")
        ref_id = str(meta.get("link_ref") or "")
    c = mstate.connect()
    try:
        c.execute(
            "INSERT OR REPLACE INTO sent_links (wa_message_id, group_jid, kind, ref_id, sent_at) "
            "VALUES (?,?,?,?,?)",
            (wa_id, group, link_kind, ref_id, mstate.now_local().isoformat()),
        )
        c.commit()
    finally:
        c.close()


def reconcile_already_sent(target: str, body: str):
    """'yes' (mirror copy present), 'no' (absent while newer outgoing messages
    exist — safe to resend), 'ambiguous' (no outgoing history at all)."""
    if not os.path.exists(MESSAGES_DB_PATH):
        return "ambiguous"
    try:
        con = sqlite3.connect(f"file:{MESSAGES_DB_PATH}?mode=ro", uri=True)
    except Exception:
        return "ambiguous"
    try:
        rows = con.execute(
            "SELECT content FROM messages "
            "WHERE chat_jid=? AND is_from_me=1 AND content IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 400",
            (target,),
        ).fetchall()
    except Exception:
        return "ambiguous"
    finally:
        con.close()
    if not rows:
        return "ambiguous"
    body_norm = (body or "").strip()
    for (content,) in rows:
        if (content or "").strip() == body_norm:
            return "yes"
    return "no"


def enqueue(kind: str, dedupe_key: str, target: str, body: str,
            ref_chat: str = None, meta: dict = None, deliver_at=None,
            conn=None) -> int:
    own = conn is None
    c = conn or mstate.connect()
    try:
        now = mstate.now_local()
        due = deliver_at if deliver_at is not None else now
        cur = c.execute(
            "INSERT OR IGNORE INTO outbox (kind, dedupe_key, target, body, status, "
            "deliver_at, created_at, ref_chat, meta) "
            "VALUES (?,?,?,?, 'queued', ?, ?, ?, ?)",
            (kind, dedupe_key, target, body if kind == "alert_send" else mstate.markerize(body),
             due.isoformat(), now.isoformat(), ref_chat, json.dumps(meta or {})),
        )
        c.commit()
        return cur.rowcount
    finally:
        if own:
            c.close()


def _apply_outcome(row, outcome: str, err: str) -> str:
    c = mstate.connect()
    try:
        if outcome == "unknown":
            c.execute(
                "UPDATE outbox SET status='held_unknown', last_error=? WHERE id=?",
                (err, row["id"]),
            )
            c.commit()
            return f"held_unknown ({err})"
        if outcome == "paused":
            c.execute(
                "UPDATE outbox SET status='queued', deliver_at=?, last_error=? WHERE id=?",
                ((mstate.now_local() + timedelta(seconds=30)).isoformat(), err, row["id"]),
            )
            c.commit()
            return f"paused ({err})"
        if outcome == "ok":
            c.execute(
                "UPDATE outbox SET status='delivered', last_error=NULL WHERE id=?",
                (row["id"],),
            )
            if row["kind"] in ("alert_info", "alert_action", "alert_followup"):
                mstate.ping_bump("delivered", conn=c)
            c.commit()
            if row["kind"] == "alert_send":
                _on_alert_send_delivered(row)
            return "delivered"
        attempts = (row["attempts"] or 0) + 1
        if outcome == "permanent" or attempts >= MAX_ATTEMPTS:
            c.execute(
                "UPDATE outbox SET status='failed', attempts=?, last_error=?, "
                "dedupe_key = dedupe_key || ':failed:' || CAST(id AS TEXT) WHERE id=?",
                (attempts, err, row["id"]),
            )
            c.commit()
            try:
                meta = json.loads(row["meta"] or "{}")
            except Exception:
                meta = {}
            _notify_failure(row, meta)
            return f"failed ({err})"
        delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
        nxt = (mstate.now_local() + timedelta(seconds=delay)).isoformat()
        c.execute(
            "UPDATE outbox SET status='queued', attempts=?, deliver_at=?, last_error=? WHERE id=?",
            (attempts, nxt, err, row["id"]),
        )
        c.commit()
        return f"retry in {delay}s"
    finally:
        c.close()


def _on_alert_send_delivered(row):
    """The user's drafted message actually left the phone: close the alert
    and tell them once, in the Me chat (WhatsApp's delivery is what we claim)."""
    try:
        import alerts as _alerts
        import mechat

        meta = {}
        try:
            meta = json.loads(row["meta"] or "{}")
        except Exception:
            meta = {}
        alid = meta.get("alert_id")
        if alid:
            _alerts.set_state(alid, "closed")
        ref_chat = row["ref_chat"] or ""
        jid = mechat.get_mechat_jid()
        if jid and ref_chat:
            confirm = _alerts.confirm_after_send(ref_chat)
            enqueue("reply", f"confirm:{row['id']}", jid, confirm, ref_chat=ref_chat)
    except Exception:
        pass


def _claim(row) -> bool:
    """Atomically take ownership so two workers cannot send the same row."""
    c = mstate.connect()
    try:
        cur = c.execute(
            "UPDATE outbox SET status='sending' WHERE id=? AND status IN ('queued','held_unknown')",
            (row["id"],),
        )
        c.commit()
        return cur.rowcount == 1
    finally:
        c.close()


def _send_row(row) -> str:
    if row["status"] == "sending":
        row = dict(row)
        row["status"] = "held_unknown" if row.get("_was_held") else "queued"
    if row["status"] == "held_unknown":
        verdict = reconcile_already_sent(row["target"], row["body"])
        if verdict == "yes":
            return _apply_outcome(row, "ok", "")
        if verdict == "ambiguous":
            c = mstate.connect()
            try:
                c.execute("UPDATE outbox SET status='held_unknown' WHERE id=?", (row["id"],))
                c.commit()
            finally:
                c.close()
            return "held (reconcile inconclusive — surfaced, not resent)"
    outcome, err, wa_id = _send_via_bridge(row["target"], row["body"])
    if outcome == "ok":
        _record_sent_link(row, wa_id)
    try:
        meta = json.loads(row["meta"] or "{}")
    except Exception:
        meta = {}
    trace = str(meta.get("trace_id") or "")
    mstate.event(trace, "send", "ok" if outcome == "ok" else "error",
                 (("wa=" + wa_id) if wa_id else (err or outcome)))
    return _apply_outcome(row, outcome, err)


def drain(max_n: int = 30):
    """Deliver everything due. Proactive rows left at quiet hours expire."""
    if not _bridge_connected():
        return [("paused", "bridge not connected")]
    c = mstate.connect()
    try:
        if mstate.is_quiet():
            c.execute(
                "UPDATE outbox SET status='expired_quiet' "
                "WHERE status='queued' AND kind IN "
                "('alert_info','alert_action','alert_followup','daily_digest')"
            )
            c.commit()
        now = mstate.now_local().isoformat()
        rows = c.execute(
            "SELECT * FROM outbox WHERE status IN ('queued','held_unknown') "
            "AND (deliver_at IS NULL OR deliver_at <= ?) ORDER BY id LIMIT ?",
            (now, max_n),
        ).fetchall()
    finally:
        c.close()
    results = []
    for r in rows:
        r = dict(r)
        if r.get("status") == "held_unknown":
            r["_was_held"] = True
        if _claim(r):
            outcome = _send_row(_normalize(r))
            results.append((r["dedupe_key"], outcome))
            if outcome.startswith("paused"):
                break
    return results


def _normalize(row: dict) -> dict:
    row.setdefault("attempts", 0)
    row.setdefault("last_error", "")
    return row


def pending() -> list:
    c = mstate.connect()
    try:
        rows = c.execute(
            "SELECT id, dedupe_key, target, status, attempts, last_error "
            "FROM outbox WHERE status NOT IN ('delivered', 'failed')"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()
