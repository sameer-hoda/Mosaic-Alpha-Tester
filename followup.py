#!/usr/bin/env python3
"""followup.py — the follow-up sweep: triggered by silence, not messages.

Runs 11:00 and 17:00 local over watched groups via ledger.py's open
loops. Ages/deadlines are computed in code from loop timestamps:
  - the user's ask to someone, unanswered for 48h -> follow_up,
  - an ask TO the user, unanswered by the user for 24h -> follow_up,
  - a deadline in the text that has passed with no resolution -> follow_up.

One follow-up alert per topic (ledger loop task + owner), draft written
in the user's voice (10 recent user messages in that group as style
examples), restating money amounts. Same gates as the watcher: cap,
quiet hours, confidence (>= 0.6), topic dedupe + supersede.
"""

import datetime
import json
import os
import _envload
import re
import sqlite3
import time

import alerts
import budget
import engine
import ledger
import mstate
import watch_rules

SWEEP_HOURS = [11, 17]
MIN_LOOP_AGE_HOURS = 4.0
MAX_SWEEP_CHATS = _envload.getenv_int("NEMBU_SWEEP_CHATS", "MOSAIC_SWEEP_CHATS", 40)
SWEEP_DELAY_S = _envload.getenv_float("NEMBU_SWEEP_DELAY_S", "MOSAIC_SWEEP_DELAY_S", 1.5)

FOLLOW_SCHEMA = {
    "type": "object",
    "properties": {
        "loops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "still_open": {"type": "boolean"},
                    "headline": {"type": "string"},
                    "why_you": {"type": "string"},
                    "draft": {"type": "string"},
                    "confidence": {"type": "number"},
                    "topic_key": {"type": "string"},
                    "material_change": {"type": "boolean"},
                    "resolved_evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["n", "still_open", "headline", "confidence", "topic_key"],
            },
        }
    },
    "required": ["loops"],
}


def own_idents():
    return ledger._own_idents()


def extract_groups(jids=None):
    jids = jids or watch_rules.watched_chats()
    out = []
    for jid in jids:
        msgs = engine.get_messages(jid, days=ledger_WINDOW_DAYS(), limit=400)
        if not msgs:
            continue
        out.append((jid, msgs))
    return out


def ledger_WINDOW_DAYS():
    return mstate.RETENTION_DAYS


def ledger_loops(jid: str, msgs) -> list:
    rows = [(_to_ist(dt), sender, content) for sender, content, dt in msgs]
    now = ledger.now_ist()
    index, transcript, people = ledger._rows_to_transcript(rows, now, chat_jid=jid)
    if not index:
        return []
    prompt, schema, fallback, own_names = ledger._ledger_prompt(index, transcript, people, now)
    data = budget.gemini_json(prompt, fallback, schema, purpose=f"followup-ledger:{jid[:12]}")
    if data.get("_gemini_failed"):
        mstate.event(jid, "followup.ledger", "error",
                     data.get("_reason") or "bad_output")
        return []
    loops = ledger.clean_loops(data, index)
    for lp in loops:
        lp["chat_jid"] = jid
        lp["index"] = index
    return loops


def classify_followup(lp, chat_jid: str) -> dict:
    """None when this loop is not follow-up-worthy; else the alert result."""
    own_names = set(ledger.own_display_names() or set())
    # The engine renders is_from_me senders as "You"; treat that label as
    # self even when the session db (push name) is unavailable.
    own_names |= {"you", "me", "(you)"}
    own_phone, own_lid, _src = _own_identity()
    own_idents_set = {i for i in (own_phone, own_lid) if i}
    owner = lp.get("owner") or ""
    asked_by = lp.get("asked_by") or ""
    me_asked = (asked_by or "").strip().lower() in own_names
    me_waited_on = (owner or "").strip().lower() in own_names
    opened_dt = lp.get("opened_dt")
    now = ledger.now_ist()
    age_h = ((now - opened_dt).total_seconds() / 3600) if opened_dt else 0.0
    due_txt, overdue = ledger.due_label(lp.get("due"), opened_dt, now)
    kind = None
    hours_needed = 0
    if me_waited_on and age_h >= 24:
        kind = f"an ask to you is unaddressed {int(age_h)}h"
        hours_needed = 24
    elif me_asked and not me_waited_on and (lp.get("status") not in ("disputed", "blocked", "done", "closed")) and age_h >= 48:
        kind = f"your ask is unanswered {int(age_h)}h"
        hours_needed = 48
    elif overdue:
        kind = "deadline passed with no resolution"
        hours_needed = 0
    if not kind:
        return None
    return {
        "alert": True,
        "type": "follow_up",
        "headline": f"{kind}{_names_fragment(lp, own_idents_set)}",
        "why_you": "",
        "draft": "",
        "confidence": max(0.6, min(0.75 + (age_h - hours_needed) / 240.0, 0.85)),
        "topic_key": f"led:{chat_jid[:8]}:{_slug(lp.get('task',''))[:40]}",
        "material_change": False,
        "_meta": {"owner": owner, "asked_by": asked_by, "task": lp.get("task", ""),
                  "age_h": round(age_h, 1), "due": due_txt or (lp.get("due") or ""),
                  "chases": lp.get("chases") or 0, "opened": str(opened_dt)[:16]},
    }


AMOUNT_RE = re.compile(r"₹\s?[\d,]+(?:\.\d+)?")


def ensure_amounts(draft: str, source_texts) -> str:
    """A draft about money must restate every amount from the thread, even
    when the model forgets. Deterministic, not a suggestion."""
    draft = (draft or "").strip()
    src = " ".join(source_texts or [])
    amounts = []
    for a in AMOUNT_RE.findall(src):
        a = a.replace(" ", "")
        if a not in amounts:
            amounts.append(a)
    missing = [a for a in amounts if a not in draft]
    if not missing:
        return draft
    extra = " Amount" + ("s" if len(missing) > 1 else "") + ": " + ", ".join(missing) + "."
    if len(draft) + len(extra) <= 240:
        return draft.rstrip() + extra
    return draft


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48]


def _own_identity():
    try:
        import mechat

        return mechat.own_identity()
    except Exception:
        return "", "", ""


def _names_fragment(lp, own_idents_set):
    task = (lp.get("task") or "").strip()
    if not task:
        return ""
    return f": {task[:70]}"


def voice_examples(chat_jid: str, limit=10) -> list:
    try:
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT content FROM messages WHERE chat_jid=? AND is_from_me=1 "
            "AND content IS NOT NULL ORDER BY timestamp DESC LIMIT ?",
            (chat_jid, limit),
        ).fetchall()
        con.close()
        # deterministic scrub of marker/ids
        return [r[0].strip()[:120].replace("◆ ", "") for r in rows]
    except Exception:
        return []


def draft_prompt(lp, meta: dict, chat_name: str, examples: list) -> str:
    return f"""Write a short WhatsApp follow-up message the user themself can send to *{chat_name}*.

Loop: {meta.get('task')} — {meta.get('age_h')}h since {meta.get('opened')}, chases so far: {meta.get('chases')}{' (deadline passed)' if meta.get('due') else ''}
Recipients: mostly {meta.get('owner') if meta.get('owner') not in ('Unassigned',) else 'the group'}.

The user's actual recent messages in this chat (style reference, imitate tone and length; if none, plain direct):
{json.dumps(examples, ensure_ascii=False)}

Rules: one message, <= 240 chars, no greeting change, no invented facts or dates (quote the original ask by topic, the age in hours/days is computed for you: {meta.get('age_h')}h). If money or a deadline is part of the loop, restate the exact amount / the deadline inside the message.
Return ONLY JSON: {{"draft": "...", "headline": "<= 90 chars", "confidence": 0.6-0.9, "topic_key": "freeform"<40chars>}}"""


def run_sweep(now=None) -> dict:
    now = now or mstate.now_local()
    sweep_hh = now.hour
    groups = extract_groups()[:MAX_SWEEP_CHATS]
    draft_schema = {
        "type": "object",
        "properties": {"draft": {"type": "string"}, "headline": {"type": "string"},
                       "confidence": {"type": "number"}, "topic_key": {"type": "string"}},
        "required": ["draft", "headline", "confidence", "topic_key"],
    }
    out = {"sweeps": 0, "queued": 0, "dispositions": []}
    for jid, msgs in groups:
        if SWEEP_DELAY_S > 0:
            time.sleep(SWEEP_DELAY_S)
        loops = ledger_loops(jid, msgs)
        cands = []
        for lp in loops:
            r = classify_followup(lp, jid)
            meta = (r or {}).pop("_meta", None)
            if not r or not meta:
                continue
            if r.get("confidence", 0) < mstate.CONF_ACTION:
                continue
            cands.append((lp, meta, r))
        if not cands:
            continue
        chat_name = alerts._chat_name(jid)
        examples = voice_examples(jid)
        batch = "\n".join(
            f"[{i}] {meta.get('task')} — waiting_on: {meta.get('owner')}, age {meta.get('age_h')}h, chases {meta.get('chases')}"
            for i, (_lp, meta, _r) in enumerate(cands, 1)
        )
        prompt = (
            f"For the user's WhatsApp chat *{chat_name}*, judge each open loop below: is it STILL worth a follow-up nudge right now, "
            "or has something in the recent history already resolved it? Reject resolved ones (still_open=false).\n\n" + batch +
            "\n\nRecent messages:\n" + engine.format_messages_ist(msgs)[-6000:]
        )
        try:
            data = budget.gemini_json(prompt, {"loops": []}, FOLLOW_SCHEMA, purpose="followup-verdict")
        except budget.BudgetPause as pause:
            return {"paused": True, "note": str(pause)}
        verdicts = {}
        for item in (data.get("loops") or []):
            try:
                verdicts[int(item.get("n") or 0)] = item
            except (TypeError, ValueError):
                continue
        for pos, (lp, meta, r) in enumerate(cands, 1):
            item = verdicts.get(pos, {})
            vetoed = item.get("still_open") is False and bool(item.get("resolved_evidence"))
            if vetoed:
                continue
            draft_call = budget.gemini_json(
                draft_prompt(lp, meta, chat_name, examples[:10]),
                {"draft": "", "headline": item.get("headline") or r["headline"],
                 "confidence": item.get("confidence") or r["confidence"], "topic_key": item.get("topic_key") or r["topic_key"]},
                schema=draft_schema, purpose="followup-draft",
            )
            result = {
                "alert": True,
                "type": "follow_up",
                "headline": (draft_call.get("headline") or item.get("headline") or "")[:160],
                "why_you": "",
                "evidence_ids": [],
                "draft": draft_call.get("draft") or "",
                "confidence": draft_call.get("confidence") or item.get("confidence") or 0.6,
                "topic_key": draft_call.get("topic_key") or item.get("topic_key") or r["topic_key"],
                "material_change": bool(item.get("material_change")),
            }
            idx = lp.get("index") or {}
            a = int(lp.get("opened") or 0)
            b = int(lp.get("last_touch") or a)

            def _txt(k):
                row = idx.get(k)
                if row is None:
                    row = idx.get(str(k))
                return row[2] if row else ""

            loop_texts = [_txt(k) for k in range(a, b + 1)]
            loop_texts = [t for t in loop_texts if t]
            if not loop_texts and a:
                loop_texts = [_txt(a)]
                loop_texts = [t for t in loop_texts if t]
            result["draft"] = ensure_amounts(result.get("draft", ""), loop_texts)
            if result["confidence"] < mstate.CONF_ACTION or not result["draft"]:
                continue
            alert_id, disp = alerts.gated_create(jid, "follow_up", result)
            out["dispositions"].append({"jid": jid, "disposition": disp, "headline": result["headline"]})
            if disp in ("alert_queued", "expired_quiet", "cap_deferred"):
                out["queued"] += 1
            out["sweeps"] += 1
    alerts.expire_stale()
    return out


def _to_ist(dt):
    return ledger._to_ist(dt)
