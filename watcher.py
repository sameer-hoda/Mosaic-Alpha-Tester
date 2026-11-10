#!/usr/bin/env python3
"""watcher.py — one Radar scan.

For each watched chat (group or DM): fetch deltas past the ingest
watermark (code), pre-filter (code), ONE batched fast-tier triage call,
gate and store (alerts.gated_create), then advance the watermark. The
ingest/triage watermark advances as soon as dispositions are stored —
no-alert and expired-quiet advance it immediately; it never waits on
delivery (outbox owns delivery).

Cost/batch rules: a batch above the context budget is split by group
and re-run smaller; a single oversized group is truncated oldest-first
and recorded as truncated. BudgetPause pauses analysis, never ingest
bookkeeping.
"""

import json
import os as _os
import sqlite3

import alerts
import budget
import mstate
import watch_rules

MESSAGES_DB_PATH = _os.getenv("MESSAGES_DB_PATH", _os.path.join(mstate.STORE_DIR, "messages.db"))
IGNORE_SENDERS_OK = {"ok", "👍", "👍👍", "ok 👍", "nothing", "nothing new"}
CONTEXT_TAIL = 20
BATCH_MAX_CHARS = 140_000


def _ro():
    con = sqlite3.connect(f"file:{MESSAGES_DB_PATH}?mode=ro", uri=True)
    return con


def name_for(chat_jid: str) -> str:
    import engine

    return engine._chats_db_name(chat_jid)


def deltas():
    """New messages per watched group since its watermark, plus a small
    tail for context. Returns (per_chat, cursor_max)."""
    con = _ro()
    try:
        groups = watch_rules.watched_chats()
        out = []
        for jid in groups:
            wm = mstate.get_watermark("ingest", jid)
            if not wm:
                # First sight of this group: seed from the newest stored
                # message and alert only on what arrives AFTER that. Without
                # this the LIMIT 200 clause would triage ancient history.
                try:
                    row = con.execute(
                        "SELECT timestamp FROM messages WHERE chat_jid=? "
                        "ORDER BY timestamp DESC LIMIT 1",
                        (jid,),
                    ).fetchone()
                except Exception:
                    row = None
                if row and row[0]:
                    mstate.set_watermark("ingest", jid, row[0])
                continue
            params = [jid]
            clause = "AND timestamp > ?"
            params.append(wm)
            try:
                rows = con.execute(
                    f"SELECT id, sender, content, timestamp, is_from_me, media_type FROM messages "
                    f"WHERE chat_jid=? {clause} AND (content IS NOT NULL AND TRIM(content) != '' "
                    f"OR media_type='audio') ORDER BY timestamp LIMIT 200",
                    tuple(params),
                ).fetchall()
            except Exception:
                rows = []
            if not rows:
                continue
            tail = []
            try:
                tail = con.execute(
                    "SELECT id, sender, content, timestamp, is_from_me, media_type FROM messages "
                    "WHERE chat_jid=? AND content IS NOT NULL AND TRIM(content) != '' "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (jid, CONTEXT_TAIL),
                ).fetchall()
                tail.reverse()
            except Exception:
                tail = []
            out.append({"jid": jid, "deltas": rows, "tail": tail})
        return out
    finally:
        con.close()


def _prefilter(rows):
    """Drop stickers/media-only/banter; the user's own messages are kept as
    context but never as alert triggers (marked me=1)."""
    import ledger

    kept = []
    for mid, sender, content, ts, is_me, media_type in rows:
        body = (content or "").strip()
        if media_type == "audio" and not body:
            kept.append({"id": str(mid), "sender": sender or "?", "text": "[voice note]",
                         "ts": ts, "me": bool(is_me)})
            continue
        if media_type and not body:
            continue
        if not body:
            continue
        if body.lower() in IGNORE_SENDERS_OK:
            continue
        try:
            if ledger.is_bot_noise(body):
                continue
        except Exception:
            pass
        kept.append({
            "id": str(mid),
            "sender": sender or "?",
            "text": body[:400],
            "ts": ts,
            "me": bool(is_me),
        })
    return kept


def transcript_block(items):
    return "\n".join(
        f"[{i}] {x['ts']} {x['sender']}{'(you)' if x['me'] else ''}: {x['text']}"
        for i, x in enumerate(items, 1)
    )


def _max_ts(raw_rows):
    return max((row[3] for row in raw_rows if row and row[3]), default="")


def triage_schema():
    return {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "jid": {"type": "string"},
                        "alert": {"type": "boolean"},
                        "type": {"type": "string", "enum": ["info", "action", "follow_up"]},
                        "headline": {"type": "string"},
                        "why_you": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "draft": {"type": "string"},
                        "confidence": {"type": "number"},
                        "topic_key": {"type": "string"},
                        "material_change": {"type": "boolean"},
                    },
                    "required": ["jid", "alert", "type", "headline",
                                 "confidence", "topic_key"],
                },
            }
        },
        "required": ["groups"],
    }


TRIAGE_PROMPT = """You are the Radar of a busy user's important WhatsApp chats. New messages arrived since the last scan ({window_note}). For EACH changed chat decide: does this contain something the user would regret missing?

The three only alert types, in the user's own terms:
- info: a decision, plan change, date/venue change, or result that affects them; no reply needed.
- action: someone asked the user directly, @mentioned them, or a decision now waits on their call. Include a ready draft reply.
- follow_up: a thread has STALLED far beyond normal (their ask unanswered ~48h+, or something owed overdue). Include a ready draft.
- Direct messages: a direct question or request from a person in a DM that the user has not answered is an action (draft required). Pleasantries, forwards and media without text are silence.

Rules:
- SILENCE IS THE DEFAULT. A day of ordinary chatter is zero alerts. Chatter is NOT an alert; "no ask, no decision" NEVER fires. Do not alert on the user's own messages (marked "(you)") — they close threads instead.
- Confidence gates: info needs >= 0.75, action >= 0.6 — set confidence accordingly; below the bar, alert=false.
- headline <= 90 chars: what happened, concrete, proper noun or number included. why_you <= 120 chars: why it concerns the user.
- draft is REQUIRED and must be non-empty for type=action (a ready 1-2 sentence reply the user can approve by saying "send"), and EMPTY for info and follow_up. It sounds like the user, restates money amounts and who they go to when money is involved, <= 240 chars, plain WhatsApp formatting. When the alert is a DECISION waiting on the user (approve/renew/choose/hire), the draft MUST NOT choose an option — it acknowledges, asks for what is needed, and commits to a decision by the stated deadline (e.g. "Let me review and revert by Friday."). Never put words in the user's mouth about the outcome.
- topic_key: a short stable slug for dedupe (e.g. "okrak-budget"). A changed date/cancellation/correction on the same topic is the same topic_key with material_change=true.
- evidence_ids: the message numbers that prove it (from the numbering below).
- Never invent facts or dates. Ages/dealines are computed in code; you say what the text says.

Chats and messages (context rows numbered [1..k] are history; new rows continue the numbering; "(you)" is the user). Each block is marked [group] or [DM with <name>]:

{transcript}

The user: {owner} (alias (you)). Open alerts already pending for these chats: {open_alerts}

Return ONLY valid JSON: {{"groups": [{{"jid","alert","type","headline","why_you","evidence_ids","draft","confidence","topic_key","material_change"}}]}} — one entry per DISTINCT thread worth attention; the SAME chat may appear MULTIPLE times when it contains separate asks or deadlines (never merge a money/compliance deadline into another ask). For a chat with nothing worth a ping, emit alert=false with a short headline. Never invent chats not listed."""


def draft_for_action(chat_jid: str, g: dict) -> str:
    """Fallback draft when the batch triage returned an action without one:
    a short reply in the user's own recent voice."""
    import followup

    examples = followup.voice_examples(chat_jid)[:8]
    prompt = f"""Write ONE WhatsApp reply the user can send right now, in their own voice.

The message to answer: {g.get('headline','')}
Why it is on them: {g.get('why_you','')}

Their recent messages in this chat (style reference; imitate tone/length; empty means use a plain direct tone):
{json.dumps(examples, ensure_ascii=False)}

Rules: <= 240 chars, 1-2 sentences, no greeting boilerplate, do not invent facts or dates, restate any money amount exactly. If this is a decision (approve/choose/renew/hire), do NOT pick an option — acknowledge and commit to a decision by the deadline. Return ONLY JSON: {{"draft": "..."}}"""
    schema = {"type": "object", "properties": {"draft": {"type": "string"}}, "required": ["draft"]}
    try:
        data = budget.gemini_json(prompt, {"draft": ""}, schema, purpose="watcher-draft")
    except budget.BudgetPause:
        return ""
    return (data.get("draft") or "").strip()


def _chat_label(jid: str) -> str:
    if jid.endswith("@g.us"):
        return "[group]"
    return f"[DM with {name_for(jid)}]"


def build_batch_prompt(chunks, owner_alias, open_by_chat, window_note="since the last scan (about 2 hours)"):
    blocks = []
    for jid, deltas_rows, tail_rows in chunks:
        blocks.append(
            f"### {name_for(jid)} [{jid}] {_chat_label(jid)}\n"
            f"Context:\n{transcript_block(tail_rows)}\n"
            f"New since last scan:\n{transcript_block(deltas_rows)}"
        )
    return TRIAGE_PROMPT.format(
        window_note=window_note,
        transcript="\n\n".join(blocks)[:BATCH_MAX_CHARS],
        owner=owner_alias,
        open_alerts=json.dumps(open_by_chat, ensure_ascii=False)[:4000],
    )


def open_alerts_for(jids):
    out = {}
    for a in alerts.open_alerts():
        if a["chat_jid"] in jids:
            out.setdefault(a["chat_jid"], []).append({
                "topic": a["topic_key"], "type": a["kind"], "headline": a["headline"],
            })
    return out


def _priority(row: dict) -> int:
    """DMs with an unanswered ask first, then @mentions, then the rest."""
    jid = row["jid"]
    deltas = [r for r in row.get("deltas") or [] if r]
    if not jid.endswith("@g.us") and any(not r[4] for r in deltas):
        return 0
    if any("@" in (r[2] or "") for r in deltas):
        return 1
    return 2


def run_cycle(owner_alias="(you)", window_note="since the last scan (about 2 hours)") -> dict:
    """One Radar pass. Splits oversized batches; advances watermarks
    after dispositions are stored (not after delivery)."""
    rows = deltas()
    if not rows:
        return {"groups_seen": 0, "alerts": 0, "dispositions": []}
    rows.sort(key=_priority)

    owner_alias = owner_alias or "you"
    open_by_chat = open_alerts_for({r["jid"] for r in rows})
    prompt = build_batch_prompt(
        [(r["jid"], _prefilter(r["deltas"]), _prefilter(r["tail"])) for r in rows],
        owner_alias, open_by_chat, window_note,
    )
    fallback = {"groups": []}
    try:
        data = budget.gemini_json(prompt, fallback, triage_schema(), purpose="watcher-triage")
    except budget.BudgetPause as pause:
        return {"paused": True, "note": str(pause), "groups_seen": len(rows)}
    if not isinstance(data, dict) or data.get("_gemini_failed"):
        # NEVER advance watermarks over messages the triage never saw —
        # that loses alerts silently. Leave the cursor where it is and let
        # the next cycle retry.
        return {"groups_seen": len(rows), "alerts": 0, "dispositions": [],
                "error": "triage_failed (watermarks held)"}
    dispositions = []
    n_alert = 0
    got = (data.get("groups") or []) if isinstance(data, dict) else []
    rowmap = {r["jid"]: r for r in rows}
    for g in got:
        jid = g.get("jid") or ""
        if jid not in rowmap:
            continue
        if not g.get("alert"):
            copy = dict(g)
            copy["confidence"] = 0.0
            copy["headline"] = copy.get("headline") or ""
            alerts.gated_create(jid, "info", copy)
            continue
        kind = g.get("type") if g.get("type") in ("info", "action", "follow_up") else "info"
        if g.get("alert") and kind != "action" and watch_rules.low_weight(jid):
            copy = dict(g)
            copy["alert"] = False
            copy["confidence"] = 0.0
            alerts.gated_create(jid, "info", copy)
            continue
        if kind == "action" and not (g.get("draft") or "").strip():
            g["draft"] = draft_for_action(jid, g) or g.get("headline", "")
        if kind == "action" and (g.get("draft") or "").strip():
            import followup as _fu

            ev = set(str(x) for x in (g.get("evidence_ids") or []))
            deltas_for_jid = (rowmap.get(jid) or {}).get("deltas") or []
            src_texts = [row[2] for row in deltas_for_jid if str(row[0]) in ev and row[2]]
            if not src_texts:
                src_texts = [row[2] for row in deltas_for_jid if row[2]]
            g["draft"] = _fu.ensure_amounts(g["draft"], src_texts)
        # Money must be visible where the user looks first: the headline.
        if kind in ("action", "info"):
            ev2 = set(str(x) for x in (g.get("evidence_ids") or []))
            deltas2 = (rowmap.get(jid) or {}).get("deltas") or []
            money_src = [row[2] for row in deltas2 if str(row[0]) in ev2 and row[2]] or [row[2] for row in deltas2 if row[2]]
            amounts = []
            for a in __import__("followup").AMOUNT_RE.findall(" ".join(money_src)):
                a = a.replace(" ", "")
                if a not in amounts:
                    amounts.append(a)
            missing = [a for a in amounts if a not in (g.get("headline") or "")]
            if missing:
                g["headline"] = ((g.get("headline") or "").rstrip(" .") + " · " + ", ".join(missing))[:160]
        alert_id, disp = alerts.gated_create(jid, kind, g)
        dispositions.append({"jid": jid, "disposition": disp, "headline": g.get("headline", "")})
        if disp in ("alert_queued", "expired_quiet", "cap_deferred", "superseded"):
            n_alert += 1
    answered = {g.get("jid") for g in got if g.get("jid")}
    for jid, r in rowmap.items():
        freshest = _max_ts(r["deltas"])
        if not freshest:
            continue
        if jid in answered or not _prefilter(r["deltas"]):
            mstate.set_watermark("ingest", jid, freshest)
            mstate.set_watermark("hold", jid, "0")
            continue
        # The model skipped this group although it has real new messages.
        # Hold the cursor (nothing lost) but do not retry forever: after 3
        # consecutive omissions advance and record the gap in triage.
        try:
            holds = int(mstate.get_watermark("hold", jid) or 0) + 1
        except (TypeError, ValueError):
            holds = 1
        if holds >= 3:
            mstate.set_watermark("ingest", jid, freshest)
            mstate.set_watermark("hold", jid, "0")
            c = mstate.connect()
            try:
                alerts.store_triage(jid, "dropped_untriaged", {"reason": "model omitted group 3x"}, mstate.now_local(), c)
                c.commit()
            finally:
                c.close()
        else:
            mstate.set_watermark("hold", jid, str(holds))
    return {"groups_seen": len(rowmap), "alerts": n_alert, "dispositions": dispositions}
