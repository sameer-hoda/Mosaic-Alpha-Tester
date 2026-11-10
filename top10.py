#!/usr/bin/env python3
"""top10.py — one digest at 08:30 local: up to 10 things the user must not
drop, from watched groups + Me-chat notes.

Built per the hourlyB (wa-pull) ranking reference with the user's labelled
corrections: daily cadence (not hourly), up to 10 GENUINE items (fewer is
a correct result, no filler quota), groups-only scope, explicit-send
rules (never a bare yes).

Scores are ADDITIVE and computed in code with visible arithmetic:
  +40 blocked on the user personally      +30 money/compliance/outage/escalation
  +25 deadline within 24h or past         +20 unanswered direct question > 4h
  +15 commitment now overdue (+5/day, max +15)
  +15 deadlock only the user can break    +10 movement since the previous digest
  +10 time-bound personal                 -20 no movement in 48h
  -25 already shown and unchanged         -30 chatter, no deliverable
The model classifies and writes words; it never adds scores.
"""

import datetime
import json
import re
import sqlite3
from datetime import timedelta

import alerts
import budget
import engine
import ledger as _l
import mstate
import watch_rules
import ledger as _l

MESSAGES_DB_PATH = engine.MESSAGES_DB_PATH

TITLE_RULES = "imperative verb title <= 80 chars naming the person blocking or what is stuck, always with a proper noun or number; never 'Follow up on X' bare."

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "title": {"type": "string"},
                    "why_you": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                    "state": {"type": "string", "enum": ["waiting_on_me", "waiting_on_them", "needs_decision", "at_risk"]},
                    "topic_key": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "keep": {"type": "boolean"},
                },
                "required": ["n", "title", "why_you", "state", "topic_key", "labels", "keep"],
            },
        },
    },
    "required": ["headline", "items"],
}

LABEL_RULES = (
    "blocked_on_me | money_or_compliance | outage_escalation | deadline_now | "
    "direct_question | commitment_overdue | deadlock_me | time_bound_personal | "
    "no_movement | already_shown | chatter_not_actionable | escalation"
)

MONEY_RE = re.compile(r"(₹|rs\.?|inr|\bpaid?|invoice|settle(ment)?|refund|budget|cost|spend|payment|compliance|gst|tax|penalty|filing|notice|rbi|legal|outage|down|escalat)", re.I)
ASK_RE = re.compile(r"\?\s*$|\b(please|pls|kindly|can you|need(ed)?|share|confirm|update|status|schedule|send)\b", re.I)
COMMIT_RE = re.compile(r"\b(i'?ll|will share|will send|by (eod|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday)|on it)\b", re.I)
PERSONAL_RE = re.compile(r"\b(flight|visa|doctor|appointment|birthday|school|home|mum|dad|family|passport)\b", re.I)
DEADLINE_RE = re.compile(r"\b(by |before |due |deadline|eod|tonight|tomorrow|today|monday|tuesday|wednesday|thursday|friday|\d{1,2}(st|nd|rd|th))\b", re.I)
RESULT_RE = re.compile(r"\b(signed|approved|closed|won|launched|kickoff|kick-off|confirmed|finalised|finalized|locked)\b", re.I)


def _me_names():
    try:
        import mechat, ledger

        names = ledger.own_display_names()
        phone, lid, _src = mechat.own_identity()
        names |= {str(x) for x in (phone, lid) if x}
        return names
    except Exception:
        return set()


def parse_dt(raw):
    try:
        dt = datetime.datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def age_hours(ts) -> float:
    dt = parse_dt(ts)
    if not dt:
        return 0.0
    now = parse_dt(mstate.now_local().isoformat())
    return max(0.0, (now - dt).total_seconds() / 3600)


def prior_digest_items(prior_days: int = 2) -> list:
    c = mstate.connect()
    try:
        since = (mstate.now_local() - timedelta(days=prior_days)).strftime("%Y-%m-%d")
        rows = c.execute(
            "SELECT t.day, t.payload, t.state FROM top10 t WHERE t.day >= ? ORDER BY t.id DESC",
            (since,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                p = json.loads(r["payload"])
            except Exception:
                continue
            out.append({"day": r["day"], "state": r["state"], "topic": p.get("topic", ""),
                        "title": p.get("title", ""), "chat_jid": p.get("chat_jid", "")})
        return out
    finally:
        c.close()


def gather_candidates(now=None) -> list:
    now = now or mstate.now_local()
    cands = []
    seen_threads = {p["topic"] for p in prior_digest_items(2) if p["topic"]}
    shown_titles = {p["title"].lower() for p in prior_digest_items(2)}

    for a in alerts.open_alerts():
        topic = a["topic_key"] or a["headline"][:32]
        why = a["why_you"] or ""
        cands.append(_cand(
            chat_jid=a["chat_jid"], topic=topic, kind=a["kind"],
            text=a["headline"] + (" " + a.get("draft", "") if a.get("draft") else ""),
            ts=a["created_at"], extra={"from_alert": a["id"], "already": topic in seen_threads},
            shown=topic in seen_threads or a["headline"].lower() in shown_titles, source="alert",
        ))

    for row in cap_deferred_items(24):
        cands.append(_cand(
            chat_jid=row["chat_jid"], topic="dg:" + str(row["topic_key"] or row["headline"][:24]),
            kind=row["kind"], text=(row["headline"] + " " + row.get("why_you", "")).strip(),
            ts=row["created_at"], extra={"from_triage": True}, shown=False, source="cap_deferred",
        ))

    for t in conversation_notes(24):
        cands.append(_cand(
            chat_jid="__me__", topic=str(t["kind"]) + ":" + t["text"][:24], kind="note",
            text=t["text"], ts=t["created_at"], extra={}, shown=False, source="note",
        ))

    for jid, rows, msgs in watched_activity():
        try:
            ts_rows = [(engine.parse_ts(r[1]) or mstate.now_local(), r[0], r[2]) for r in rows if r[2]]
            _index, _transcript, _people = _l._rows_to_transcript(ts_rows, _l.now_ist(), chat_jid=jid)
            asks = _l.candidate_asks(_index, limit=12)
        except Exception:
            asks = []
        for (_n, who, preview) in asks[-4:]:
            topic = "ask:" + re.sub(r"\W+", "", (preview or ""))[:24].lower()
            cands.append(_cand(
                chat_jid=jid, topic=topic, kind="chip", text=preview or "",
                ts=rows[-1][1] if rows else "", extra={},
                shown=topic in seen_threads, source="ask",
            ))
    for cand in cands:
        if cand["topic_key"] in seen_threads:
            cand["shown"] = True
    return cands


def cap_deferred_items(hours: int) -> list:
    """Alerts the daily ping cap deferred: they must still reach the Top 10."""
    import json as _json

    since = (mstate.now_local() - timedelta(hours=hours)).isoformat()
    c = mstate.connect()
    try:
        rows = c.execute(
            "SELECT chat_jid, created_at, result FROM triage "
            "WHERE disposition='cap_deferred' AND created_at >= ? ORDER BY id DESC LIMIT 20",
            (since,),
        ).fetchall()
    finally:
        c.close()
    out = []
    for r in rows:
        try:
            res = _json.loads(r["result"])
        except Exception:
            continue
        if not res.get("headline"):
            continue
        out.append({
            "chat_jid": r["chat_jid"], "created_at": r["created_at"],
            "headline": res.get("headline", ""), "why_you": res.get("why_you", ""),
            "kind": res.get("type") if res.get("type") in ("action", "info", "follow_up") else "info",
            "topic_key": res.get("topic_key", ""),
        })
    return out


def conversation_notes(hours: int) -> list:
    since = (mstate.now_local() - timedelta(hours=hours)).isoformat()
    c = mstate.connect()
    try:
        rows = c.execute(
            "SELECT kind, text, created_at FROM turns WHERE kind='note' AND created_at >= ? "
            "ORDER BY id DESC LIMIT 20",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def watched_activity(limit_per_chat: int = 40):
    con = sqlite3.connect(f"file:{MESSAGES_DB_PATH}?mode=ro", uri=True)
    out = []
    try:
        for jid in watch_rules.watched_chats():
            try:
                rows = con.execute(
                    "SELECT sender, timestamp, content FROM messages "
                    "WHERE chat_jid=? AND content IS NOT NULL AND TRIM(content)!='' "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (jid, limit_per_chat),
                ).fetchall()
                out.append((jid, list(reversed(rows)), rows))
            except Exception:
                continue
    finally:
        con.close()
    return out


def _cand(chat_jid, topic, kind, text, ts, extra, shown, source="") -> dict:
    return {
        "chat_jid": chat_jid, "topic_key": topic, "kind": kind, "text": (text or "")[:400],
        "ts": ts, "age_h": round(age_hours(ts), 1), "extra": extra or {}, "shown": bool(shown),
        "movement_h": _movement_hours(chat_jid), "score": 0, "breakdown": [], "source": source,
    }


def _movement_hours(chat_jid) -> float:
    try:
        con = sqlite3.connect(f"file:{MESSAGES_DB_PATH}?mode=ro", uri=True)
        row = con.execute(
            "SELECT timestamp FROM messages WHERE chat_jid=? ORDER BY timestamp DESC LIMIT 1",
            (chat_jid,),
        ).fetchone()
        return round(age_hours(row[0]) if row else 999.0, 1)
    except Exception:
        return 999.0
    finally:
        con.close()


def score_candidate(c: dict, me_names: set) -> dict:
    """Additive, visible arithmetic. Returns the candidate with score."""
    text = c["text"] or ""
    b = []

    def add(pts, why):
        c["score"] += pts
        b.append(f"{pts:+d} {why}")

    if c["kind"] == "action" or c["kind"] == "chip":
        add(40, "blocked on me personally")
    money_like = bool(MONEY_RE.search(text))
    if c["kind"] != "note" and money_like:
        add(30, "money/compliance/outage/escalation")
    now = mstate.now_local()
    if c["kind"] == "follow_up":
        add(25, "deadline within 24h or already passed")
    elif DEADLINE_RE.search(text):
        add(25, "explicit deadline in the text")
    if c["kind"] == "chip" and c["age_h"] > 4:
        add(20, "unanswered direct question aged over 4h")
    if c["kind"] == "chip":
        add(15, "stated commitment now overdue")
    movement = c.get("movement_h", 999.0)
    if movement <= 24 and not c["shown"]:
        add(10, "movement since the previous digest")
    if PERSONAL_RE.search(text):
        add(20, "time-bound personal")
    if RESULT_RE.search(text):
        add(20, "outcome/result the user should know")
    if movement > 48:
        add(-20, "no movement in 48h")
    if c["shown"]:
        add(-25, "already shown and unchanged")
    c["breakdown"] = b
    return c


def build_prompt(cands, chat_names) -> str:
    lines = []
    for i, c0 in enumerate(cands, 1):
        n = chat_names.get(c0["chat_jid"], c0["chat_jid"])
        lines.append(
            f"[{i}] chat: {n} | kind: {c0['kind']} | via: {c0.get('source','')} | age: {c0['age_h']}h | movement: {c0['movement_h']}h | code_score: {c0['score']}\n"
            f"    text: {c0['text'][:240]}"
        )
    return f"""You are the user's chief of staff building the ONE daily Top-10. Candidate threads (numbered) with their CODE-COMPUTED additive scores below; code owns ALL numbers — you never add or subtract scores, you only:

1) verify labels for each candidate (pick ALL that apply from {LABEL_RULES}); chatter_not_actionable means no ask, decision or deliverable — those are drop worthy even if score says otherwise (code only drops/keeps via your keep flag),
2) write for kept candidates a crisp imperative title ({TITLE_RULES}), one-line why_you (<=120 chars, consequence not quote), a <= 20-word evidence quote from the text, state (waiting_on_me/them/needs_decision/at_risk), and a stable topic_key slug,
3) keep=true ONLY for genuine items that are still worth the user's attention (up to 10; fewer is better; never filler).

No item twice from the same thread. One item per main object across chats.

Candidates:
{chr(10).join(lines)}

Return ONLY valid JSON: {{"headline": "...", "items": [{{"n","title","why_you","evidence_quote","state","topic_key","labels","keep"}}]}}"""


def run_daily(now=None) -> dict:
    now = now or mstate.now_local()
    try:
        budget.assert_room()
    except budget.BudgetPause as pause:
        return {"paused": True, "note": str(pause)}
    cands = gather_candidates(now)
    if not cands:
        return {"skipped": "nothing-candidates"}
    me_names = _me_names()
    for cands_item in cands:
        score_candidate(cands_item, me_names)
    chat_names = {}
    c = engine._get_conn()
    if c is not None:
        try:
            for r in c.execute("SELECT jid, name FROM chats").fetchall():
                chat_names[r[0]] = (r[1] or r[0])[:40]
        except Exception:
            pass
        finally:
            c.close()
    chat_names["__me__"] = "Your note"
    prompt = build_prompt(cands, chat_names)
    data = budget.gemini_json(prompt, {"headline": "", "items": []}, DIGEST_SCHEMA, purpose="top10")
    if data.get("_gemini_failed") or not data.get("items"):
        return {"skipped": "model-failed" if data.get("_gemini_failed") else "no-items"}
    kept = []
    model_keep = {}
    for it in data["items"]:
        try:
            n = int(it.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if not (1 <= n <= len(cands)):
            continue
        model_keep[n] = it
        if not it.get("keep"):
            continue
        cand = cands[n - 1]
        cand["payload"] = {
            "title": (it.get("title") or "")[:110],
            "why_you": (it.get("why_you") or "")[:150],
            "evidence_quote": (it.get("evidence_quote") or "")[:140],
            "state": it.get("state") or "waiting_on_them",
            "labels": it.get("labels") or [],
            "topic": it.get("topic_key") or cand["topic_key"],
            "chat_jid": cand["chat_jid"],
            "who": _who_line(cand, it, me_names),
            "age_h": cand["age_h"],
            "score": cand["score"],
            "breakdown": cand["breakdown"],
            "source": "top10",
        }
        cand["_label_n"] = n
        kept.append(cand)
    # Code scores are authoritative: a high-scoring candidate is not dropped
    # by model whim unless it explicitly called it non-actionable chatter.
    kept_ns = {c["_label_n"] for c in kept}
    forced = []
    for idx, cand in enumerate(cands, 1):
        if idx in kept_ns or cand["score"] < 45:
            continue
        it = model_keep.get(idx) or {}
        labels = it.get("labels") or []
        if "chatter_not_actionable" in labels:
            continue
        cand["payload"] = {
            "title": (it.get("title") or cand["text"][:70] or cand["topic_key"])[:110],
            "why_you": (it.get("why_you") or "")[:150],
            "evidence_quote": (it.get("evidence_quote") or cand["text"][:120])[:140],
            "state": it.get("state") or "waiting_on_you",
            "labels": labels,
            "topic": it.get("topic_key") or cand["topic_key"],
            "chat_jid": cand["chat_jid"],
            "who": "waiting on you" if it.get("state") in (None, "waiting_on_me", "needs_decision") else "waiting on them",
            "age_h": cand["age_h"],
            "score": cand["score"],
            "breakdown": cand["breakdown"],
            "source": "top10-forced",
        }
        forced.append(cand)
    kept.extend(forced)
    kept.sort(key=lambda x: x["score"], reverse=True)
    kept = _dedupe_threads(kept)[:10]
    day = mstate.local_date_key(now)
    with digest_writer() as conn:
        # A manual re-run replaces today's digest rather than appending it.
        conn.execute("DELETE FROM top10 WHERE day=?", (day,))
        for cp in kept:
            conn.execute(
                "INSERT INTO top10 (day, thread_key, payload, state, created_at) VALUES (?, ?, ?, 'open', ?)",
                (day, cp["payload"]["topic"], json.dumps(cp["payload"], ensure_ascii=False), now.isoformat()),
            )
    msg = render_digest(data.get("headline") or "", kept)
    try:
        import mechat

        target = mechat.get_mechat_jid()
    except Exception:
        target = ""
    if target:
        import outbox

        outbox.enqueue("daily_digest", f"top10::{day}", target, msg)
    return {"day": day, "items": len(kept)}


def _thread_tokens(text: str) -> set:
    import re as _re

    return {
        w for w in _re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) >= 4 and w not in
        {"with", "from", "that", "this", "your", "need", "needs", "into", "about", "their",
         "there", "have", "will", "been", "over", "under", "deadline", "approval", "requested",
         "request", "today", "tomorrow", "update", "check", "clear", "could", "should"}
    }


def _dedupe_threads(items: list) -> list:
    """One item per real thread: same chat + strongly overlapping words is the
    same object (alert + ask + cap-deferred can each surface it)."""
    out = []
    for cand in items:
        p = cand["payload"]
        toks = _thread_tokens(p.get("title", "") + " " + p.get("evidence_quote", ""))
        dup = False
        for prev in out:
            q = prev["payload"]
            if q.get("chat_jid") != p.get("chat_jid"):
                continue
            ptoks = _thread_tokens(q.get("title", "") + " " + q.get("evidence_quote", ""))
            if not toks or not ptoks:
                continue
            j = len(toks & ptoks) / len(toks | ptoks)
            if j >= 0.5:
                dup = True
                break
        if not dup:
            out.append(cand)
    return out


def _who_line(cand, item, me_names):
    state = item.get("state")
    if state == "waiting_on_me":
        return "waiting on you"
    if state == "needs_decision":
        return "needs your call"
    if state == "at_risk":
        return "at risk"
    return "waiting on them"


def render_digest(headline: str, kept: list) -> str:
    lines = ["◆ *TOP 10 — today*"]
    if headline:
        h = headline.strip()
        if len(h) > 80:
            h = h[:80].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"
        lines[0] = f"◆ *TOP 10 — today* — _{h}_"
    if not kept:
        lines.append("_Nothing that can't wait today._")
    else:
        for i, cand in enumerate(kept, 1):
            p = cand["payload"]
            title = p["title"] or p["topic"]
            bit = f"{i}. *{title}*"
            details = [f"_{p['who']}_"] if p.get("who") else []
            if p.get("age_h") and p["age_h"] >= 4:
                details.append(f"{int(p['age_h'])}h old")
            if details:
                bit += "\n    " + " · ".join(details)
            if p.get("why_you"):
                bit += f"\n    {p['why_you']}"
            elif p.get("evidence_quote"):
                bit += f"\n    \"{p['evidence_quote']}\""
            lines.append(bit)
    lines.append("_reply done N to clear · context N for background · more for anything beyond 10_")
    return "\n\n".join(lines)


def digest_writer():
    return mstate.connect()


def apply_dismissal(arg: str) -> str:
    """'done 3' / 'done all' / 'context 2' — updates today's top10 rows."""
    day = mstate.local_date_key()
    arg = arg.strip().lower()
    numbers = [int(x) for x in re.findall(r"\d+", arg)] if re.search(r"\d", arg) else None
    state = "done" if arg.startswith("done") else "context"
    if "all" in arg:
        numbers = list(range(1, 11))
    if not numbers:
        return "◆ Tell me which numbers, e.g. done 3."
    open_rows = [dict(r) for r in _today_open(day)]
    out = []
    for n in numbers:
        if n <= len(open_rows):
            row = open_rows[n - 1]
            c = mstate.connect()
            c.execute("UPDATE top10 SET state=? WHERE id=?", (state, row["id"]))
            c.commit()
            c.close()
            if state == "done":
                try:
                    import watch_rules

                    chat = (json.loads(row["payload"] or "{}") or {}).get("chat_jid")
                    if chat:
                        watch_rules.adjust_weight(chat, -1)
                except Exception:
                    pass
            out.append(str(n))
    return f"◆ Cleared {', '.join(out)}." if out else "◆ No open items at those numbers."


def _today_open(day):
    c = mstate.connect()
    try:
        rows = c.execute(
            "SELECT id, payload FROM top10 WHERE day=? AND state='open' ORDER BY id", (day,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


