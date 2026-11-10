#!/usr/bin/env python3
"""alerts.py — the alert lifecycle and the central noise gate.

States: open → draft_pending → sent → closed; also done / skipped /
expired. One ongoing thread per topic: a MATERIAL change (new date,
cancellation, correction) on an open topic_key supersedes the waiting
alert; minor chatter waits; one open alert per topic_key.

The gate:
  confidence >= 0.75 info / >= 0.6 action; hard cap 4 delivered
  proactive pings/day (info + action + follow_up combined — the
  morning brief is separate). Past the cap, events are stored as
  cap_deferred and surface in the morning brief instead. After 20:00
  proactive alerts expire rather than queueing overnight.
"""

import datetime
import json

import mstate
import outbox

ALERT_KINDS = ("info", "action", "follow_up")
OPEN_STATES = ("open", "draft_pending", "sent")
OPEN_PH = ",".join("?" * len(OPEN_STATES))
NOTIFY_KINDS = {"info": "alert_info", "action": "alert_action", "follow_up": "alert_followup"}
KIND_TAGS = {"info": "For your info", "action": "Action", "follow_up": "Follow up"}


def _chat_name(chat_jid: str) -> str:
    try:
        import engine

        name = engine._chats_db_name(chat_jid)
    except Exception:
        name = ""
    return (name or chat_jid.split("@")[0] or "group")[:48]


def notice_text(chat_name: str, kind: str, result: dict) -> str:
    """WhatsApp-native alert body. Group bold on line 1; marker is display
    only (outbox.markerize adds ◆ Nembu)."""
    tag = KIND_TAGS[kind if kind in ALERT_KINDS else "info"]
    lines = [f"*{chat_name} · {tag}*: {result.get('headline', '')}"]
    if result.get("why_you"):
        lines.append(result["why_you"])
    if kind in ("action", "follow_up") and result.get("draft"):
        lines.append(
            'Draft: "' + result["draft"] + '" — reply *send*, ask me to edit it, or skip.'
        )
    return "\n".join(lines)


def store_triage(chat_jid: str, disposition: str, result: dict, now, conn) -> None:
    conn.execute(
        "INSERT INTO triage (chat_jid, created_at, disposition, result) VALUES (?,?,?,?)",
        (chat_jid, now.isoformat(), disposition, json.dumps(result, ensure_ascii=False)),
    )


def gated_create(chat_jid: str, kind: str, result: dict, now=None) -> tuple:
    """Apply the gate for one triage result, store it, and enqueue the
    Me-chat notification when it passes.

    Returns (alert_id, disposition): alert_queued | expired_quiet |
    no_alert | superseded | cap_deferred | bad_result | no_target.
    """
    now = now or mstate.now_local()
    try:
        kind = kind if kind in ALERT_KINDS else "info"
        confidence = float(result.get("confidence") or 0.0)
        topic_key = (result.get("topic_key") or "").strip()
        if not result.get("headline"):
            c = mstate.connect()
            store_triage(chat_jid, "bad_result", result, now, c)
            c.commit()
            c.close()
            return 0, "bad_result"
    except Exception:
        c = mstate.connect()
        store_triage(chat_jid, "bad_result", {}, now, c)
        c.commit()
        c.close()
        return 0, "bad_result"

    c = mstate.connect()
    try:
        supersede = False
        if topic_key:
            since = (now - datetime.timedelta(hours=24)).isoformat()
            recent = c.execute(
                "SELECT result FROM triage WHERE chat_jid=? AND created_at >= ? "
                "AND disposition IN ('alert_queued','expired_quiet','cap_deferred') "
                "ORDER BY id DESC LIMIT 40",
                (chat_jid, since),
            ).fetchall()
            for (raw,) in recent:
                try:
                    prev = json.loads(raw)
                except Exception:
                    continue
                if (prev.get("topic_key") or "") == topic_key:
                    if not result.get("material_change"):
                        store_triage(chat_jid, "no_alert", result, now, c)
                        c.commit()
                        return 0, "no_alert"
                    break
            open_rows = c.execute(
                f"SELECT id FROM alerts WHERE topic_key=? AND chat_jid=? "
                f"AND state IN ({OPEN_PH}) ORDER BY id DESC LIMIT 1",
                (topic_key, chat_jid, *OPEN_STATES),
            ).fetchall()
            if open_rows:
                supersede = bool(result.get("material_change"))
                if not supersede:
                    store_triage(chat_jid, "no_alert", result, now, c)
                    c.commit()
                    return 0, "no_alert"
        threshold = mstate.CONF_ACTION if kind in ("action", "follow_up") else mstate.CONF_INFO
        alert_worthy = confidence >= threshold
        mechat_target = None
        try:
            import mechat

            mechat_target = mechat.get_mechat_jid()
        except Exception:
            mechat_target = ""

        if alert_worthy and not mechat_target:
            store_triage(chat_jid, "no_target", result, now, c)
            c.commit()
            return 0, "no_target"

        if alert_worthy and supersede:
            c.execute(
                f"UPDATE alerts SET state='closed' WHERE topic_key=? AND chat_jid=? "
                f"AND state IN ({OPEN_PH})",
                (topic_key, chat_jid, *OPEN_STATES),
            )

        day = mstate.local_date_key(now)
        delivered = mstate.pings_delivered(day, conn=c)
        pending = c.execute(
            "SELECT COUNT(*) FROM outbox WHERE status IN ('queued','held_unknown') "
            "AND substr(deliver_at,1,10)=? "
            "AND kind IN ('alert_info','alert_action','alert_followup')",
            (day,),
        ).fetchone()[0]
        cap_reached = (delivered + pending) >= mstate.MAX_PINGS_PER_DAY

        if not alert_worthy:
            disposition = "no_alert"
        elif cap_reached:
            disposition = "cap_deferred"
        elif mstate.is_quiet(now):
            disposition = "expired_quiet"
        else:
            disposition = "alert_queued"
        store_triage(chat_jid, disposition, result, now, c)

        alert_id = 0
        if disposition == "alert_queued":
            alert_id = _insert_alert(c, chat_jid, kind, result, now, topic_key)
            chat_name = _chat_name(chat_jid)
            outbox.enqueue(
                NOTIFY_KINDS[kind],
                dedupe_key=f"{kind}:{topic_key}:{(result.get('headline') or '')[:48]}",
                target=mechat_target,
                body=notice_text(chat_name, kind, result),
                ref_chat=chat_jid,
                meta={"alert_id": alert_id, "supersedes": supersede},
                conn=c,
            )
        mstate.ping_bump("created", day=day,
                         n=0 if disposition in ("no_alert", "expired_quiet") else 1, conn=c)
        c.commit()
        return alert_id, disposition
    finally:
        c.close()


def _insert_alert(conn, chat_jid: str, kind: str, result: dict, now, topic_key: str) -> int:
    cur = conn.execute(
        "INSERT OR REPLACE INTO alerts (chat_jid, dedupe_key, kind, headline, why_you, "
        "evidence_ids, draft, confidence, topic_key, state, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            chat_jid,
            f"{kind}:{topic_key or (result.get('headline') or '')[:60]}",
            kind,
            (result.get("headline") or "")[:160],
            (result.get("why_you") or "")[:240],
            json.dumps(result.get("evidence_ids") or []),
            result.get("draft") or "",
            result.get("confidence") or 0.0,
            topic_key,
            "open",
            now.isoformat(),
            (now + datetime.timedelta(hours=mstate.ALERT_TTL_HOURS)).isoformat(),
        ),
    )
    return cur.lastrowid


def open_alerts(chat_jid: str = None):
    c = mstate.connect()
    try:
        if chat_jid:
            rows = c.execute(
                f"SELECT * FROM alerts WHERE chat_jid=? AND state IN ({OPEN_PH}) ORDER BY id DESC",
                (chat_jid, *OPEN_STATES),
            ).fetchall()
        else:
            rows = c.execute(
                f"SELECT * FROM alerts WHERE state IN ({OPEN_PH}) ORDER BY id DESC",
                OPEN_STATES,
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def find_open_by_number(n: int):
    """The Nth open-draft alert, most recent first — 'send 2' targeting."""
    rows = open_alerts()
    return rows[n - 1] if rows and n <= len(rows) else None


def latest_open():
    rows = open_alerts()
    return rows[0] if rows else None


def _nudge_weight(chat_jid: str, delta: float) -> None:
    try:
        import watch_rules

        watch_rules.adjust_weight(chat_jid, delta)
    except Exception:
        pass


def set_state(alert_id: int, state: str) -> None:
    chat_jid = ""
    c = mstate.connect()
    try:
        row = c.execute("SELECT chat_jid FROM alerts WHERE id=?", (alert_id,)).fetchone()
        chat_jid = row["chat_jid"] if row else ""
        c.execute("UPDATE alerts SET state=? WHERE id=?", (state, alert_id))
        c.commit()
    finally:
        c.close()
    if state in ("skipped", "done", "expired") and chat_jid:
        adjust = -2 if state == "skipped" else -1
        _nudge_weight(chat_jid, adjust)


def replace_draft(alert_id: int, new_draft: str) -> None:
    c = mstate.connect()
    try:
        c.execute(
            "UPDATE alerts SET draft=?, state='draft_pending' WHERE id=?",
            (new_draft, alert_id),
        )
        c.commit()
    finally:
        c.close()


def expire_stale() -> int:
    rows = []
    c = mstate.connect()
    try:
        now = mstate.now_local().isoformat()
        rows = c.execute(
            f"SELECT id, chat_jid FROM alerts WHERE state IN ({OPEN_PH}) "
            "AND expires_at IS NOT NULL AND expires_at <= ?",
            (*OPEN_STATES, now),
        ).fetchall()
        c.execute(
            f"UPDATE alerts SET state='expired' WHERE state IN ({OPEN_PH}) "
            "AND expires_at IS NOT NULL AND expires_at <= ?",
            (*OPEN_STATES, now),
        )
        c.commit()
    finally:
        c.close()
    for row in rows:
        _nudge_weight(row["chat_jid"], -1)
    return len(rows)


def confirm_after_send(chat_jid: str) -> str:
    return f"◆ Sent to *{_chat_name(chat_jid)}* ✓"
