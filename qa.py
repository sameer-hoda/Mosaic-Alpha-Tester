#!/usr/bin/env python3
"""qa.py — Me-chat Q&A pipeline (catalog first).

question -> certain signals -> understand call -> code checks -> expert over
retrieved messages -> code-formatted answer (header, coverage).

Profiles are never evidence. The general fallback never resolves a group.
Two model calls per question: understand, then one expert.
"""
import datetime
import difflib
import json
import os
import _envload
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import agent_config
import budget
import conversation
import engine
import ledger
import mechat
import mstate
import qa_profiles
import qa_search
import watch_rules

IST = mstate.local_tz()
SELECTION_TTL_MIN = 30
CHOICE_TTL_MIN = 10
RESOLVE_CONF = 0.80
RESOLVE_LEAD = 0.20
QA_MODEL = os.getenv("GEMINI_MODEL_QA", "gemini-3.5-flash")
QA_CONTEXT_CHARS = int(os.getenv("QA_CONTEXT_CHARS", "200000"))
SHORTLIST_OVER = 40
SHORTLIST_SIZE = 15
RETRIEVAL_CAP_CHARS = 24000
_MENTION_RE = re.compile(r"@(\d{9,20})")

EXPERTS = ("catch_up", "lookup", "decision", "open_items", "reply", "general")

WHATSAPP_FORMAT = """WhatsApp formatting (strict):
- *bold* for section labels; _italics_ only for asides; no **, no #, no tables, no code fences.
- Bullets are lines starting with "- ". One bullet per line, no blank lines between bullets.
- Numbered lists are lines starting with "1. ", "2. ".
- Keep it crisp: at most 14 lines; no filler, no repeated header, no markdown."""

EXPERT_RULES = """Rules for every answer:
- Use only the messages provided. If the answer isn't there, say so and mention the nearest thing that is.
- Attribute facts ("Priya, Fri 7:40 pm"). Quote only the phrase that decides the answer.
- Restate money amounts and dates exactly as written.
- Reply in the user's language, Hinglish included.
- Never invent facts, dates, numbers or people.

""" + WHATSAPP_FORMAT

# ── selection ────────────────────────────────────────────────────────────────
def get_selection() -> dict:
    c = mstate.connect()
    try:
        row = c.execute("SELECT * FROM selected_group WHERE id=1").fetchone()
    finally:
        c.close()
    return dict(row) if row else {}


def set_selection(jid, name, question, answer):
    c = mstate.connect()
    try:
        now = mstate.now_local().isoformat()
        c.execute(
            "INSERT OR REPLACE INTO selected_group "
            "(id, chat_jid, name, selected_at, last_asked_at, last_question, last_answer) "
            "VALUES (1,?,?,?,?,?,?)",
            (jid, name, now, now, (question or "")[:400], (answer or "")[:1200]),
        )
        c.commit()
    finally:
        c.close()


def clear_selection():
    c = mstate.connect()
    try:
        c.execute("DELETE FROM selected_group WHERE id=1")
        c.commit()
    finally:
        c.close()


def selection_active(sel=None) -> bool:
    sel = sel if sel is not None else get_selection()
    if not sel or not sel.get("last_asked_at"):
        return False
    try:
        last = datetime.datetime.fromisoformat(sel["last_asked_at"])
    except (TypeError, ValueError):
        return False
    return (mstate.now_local() - last).total_seconds() <= SELECTION_TTL_MIN * 60


# ── pending choice ───────────────────────────────────────────────────────────
def _drop_choice():
    c = mstate.connect()
    try:
        c.execute("DELETE FROM pending_choice")
        c.commit()
    finally:
        c.close()


def _save_choice(question, cands, mention):
    c = mstate.connect()
    try:
        expires = (mstate.now_local() + datetime.timedelta(minutes=CHOICE_TTL_MIN)).isoformat()
        c.execute(
            "DELETE FROM pending_choice",
        )
        c.execute(
            "INSERT INTO pending_choice (id, question, candidates, created_at, expires_at, mention) "
            "VALUES (1,?,?,?,?,?)",
            (question, json.dumps(cands, ensure_ascii=False),
             mstate.now_local().isoformat(), expires, mention or ""),
        )
        c.commit()
    finally:
        c.close()


def get_choice() -> dict:
    c = mstate.connect()
    try:
        row = c.execute("SELECT * FROM pending_choice WHERE id=1").fetchone()
    finally:
        c.close()
    if not row:
        return {}
    d = dict(row)
    try:
        expires = datetime.datetime.fromisoformat(d.get("expires_at") or "")
        if mstate.now_local() > expires:
            _drop_choice()
            return {}
    except (TypeError, ValueError):
        pass
    try:
        d["candidates"] = json.loads(d.get("candidates") or "[]")
    except Exception:
        d["candidates"] = []
    return d


# ── certain signals ──────────────────────────────────────────────────────────
def _shortlist(text, context_jids):
    profs = qa_profiles.profiles(eligible_only=True)
    if len(profs) <= SHORTLIST_OVER:
        return profs
    norm = qa_profiles.normalize(text)
    scored = {}
    for jid, p in profs.items():
        score = 0
        for alias in p.get("aliases") or []:
            a = qa_profiles.normalize(alias)
            if a and a in norm:
                score += 3 + len(a)
        for topic in p.get("topics") or []:
            if topic and topic in norm:
                score += 2
        if jid in context_jids:
            score += 5
        if score:
            scored[jid] = score
    keep = [j for j, _ in sorted(scored.items(), key=lambda kv: kv[1], reverse=True)[:SHORTLIST_SIZE]]
    for jid in context_jids:
        if jid in profs and jid not in keep:
            keep.append(jid)
    return {j: profs[j] for j in keep}


def _profile_lines(profs):
    out = []
    for jid, p in profs.items():
        aliases = ", ".join((p.get("aliases") or [])[:6])
        topics = ", ".join((p.get("topics") or [])[:6])
        out.append(
            f'- id={jid} name={p.get("name")!r} aliases=[{aliases}] '
            f'description={p.get("description")!r} topics=[{topics}]'
        )
    return "\n".join(out) or "(none)"


def _media_label(media, filename=""):
    m = (media or "").lower()
    if not m:
        return ""
    if "image" in m or "photo" in m:
        return "[photo]"
    if "video" in m:
        return "[video]"
    if "audio" in m or "voice" in m or "ptt" in m:
        return "[voice note]"
    if "document" in m or "file" in m:
        name = (filename or "").strip()
        return f"[document: {name[:60]}]" if name else "[document]"
    if "sticker" in m:
        return "[sticker]"
    return f"[{m.split('/')[0][:20]}]"


def _load(jid, days=None):
    days = days or mstate.RETENTION_DAYS
    try:
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
    except Exception:
        return []
    cutoff = (mstate.now_local() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = con.execute(
            "SELECT id, sender, content, media_type, timestamp, is_from_me, "
            "COALESCE(quoted_id,''), COALESCE(filename,'') FROM messages "
            "WHERE chat_jid=? AND timestamp >= ? ORDER BY timestamp",
            (jid, cutoff),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        con.close()
    transcripts = {}
    if rows:
        c = mstate.connect()
        try:
            for r in c.execute(
                "SELECT msg_id, text FROM transcripts WHERE chat_jid=?", (jid,)
            ).fetchall():
                transcripts[r["msg_id"]] = r["text"]
        finally:
            c.close()

    def clean(text: str) -> str:
        if not text:
            return ""
        text = _MENTION_RE.sub(lambda m: engine._resolve_sender_name(m.group(1)) or "someone", text)
        return " ".join(text.split())

    by_id = {}
    out = []
    for mid, sender, content, media, ts, me, qid, fname in rows:
        if _is_forwarded_mosaic(content):
            continue
        name = "You" if me else _sender_label(sender, jid)
        if not me and str(name).replace("-", "").isdigit():
            name = "Someone"
        item = {"id": str(mid), "sender": name, "raw_sender": sender or "",
                "text": clean(content or ""), "media": media or "",
                "filename": fname or "", "ts": ts, "me": bool(me),
                "quoted": qid, "quoted_who": "", "quoted_text": "",
                "transcript": clean(transcripts.get(str(mid), ""))}
        out.append(item)
        by_id[item["id"]] = item
    for item in out:
        q = item.get("quoted") or ""
        if not q:
            continue
        qm = by_id.get(str(q))
        if qm:
            item["quoted_who"] = qm["sender"]
            item["quoted_text"] = (qm["text"] or qm["transcript"] or "")[:160]
    return out


def _split_head(full, limit):
    """Split an answer for the Me chat, preferring a newline near the limit.

    Answers that fit are returned whole. Only genuinely long text is chunked
    and gets the "Say more" footer.
    """
    text = full or ""
    if limit <= 0 or len(text) <= limit:
        return text, ""
    head = text[:limit]
    cut = head.rfind("\n")
    if cut > limit // 2:
        head = head[:cut]
    return head, text[len(head):]


def _chunk_and_store(full, group_jid):
    limit = max(1, mstate.MAX_BODY_CHARS - 200)
    first, tail = _split_head(full, limit)
    if tail:
        first = first.rstrip("\n") + "\n\nSay *more* for the rest."
    c = mstate.connect()
    try:
        cur = c.execute(
            "INSERT INTO answers (turn_key, full_text, head_lines) VALUES (?,?,?)",
            (f"qa:{group_jid}:{__import__('uuid').uuid4().hex[:10]}", full,
             first.count("\n") + 1),
        )
        c.commit()
        return first, str(cur.lastrowid)
    finally:
        c.close()


def _header_range(start, end) -> str:
    if start.date() == end.date():
        return f"{start.strftime('%a %d %b %H:%M')}–{end.strftime('%H:%M')}"
    return f"{start.strftime('%a %d %b %H:%M')}–{end.strftime('%a %d %b %H:%M')}"


def _chats_read(context) -> int:
    return len(re.findall(r"^### ", context or "", re.M))


def _coverage_footer(total, chats_read, in_scope, media=0, voice=0,
                     cut=False, timed_out=False) -> str:
    """Honest counts: what was read, how many chats that was, how many in scope."""
    bits = [f"read {int(total or 0)} msgs from {int(chats_read or 0)} chats",
            f"{int(in_scope or 0)} in scope"]
    if voice:
        bits.append(f"{int(voice)} voice notes not transcribed")
    if media:
        bits.append(f"{int(media)} images/documents not searchable")
    if cut:
        bits.append("some messages were left out")
    if timed_out:
        bits.append("short answer — the AI writer timed out")
    return "\n_" + " · ".join(bits) + "_"


def format_body(expert: str, body: str) -> str:
    """Code-side formatting pass so every answer is WhatsApp-native.

    The model proposes words; this enforces structure: bold section labels,
    "- " bullets for lists, no markdown, no raw IDs.
    """
    body = (body or "").replace("\\n", " ").replace("\\t", " ").replace("**", "*").replace("__", "_")
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"^#{1,6}\s*", "", body, flags=re.M)
    body = re.sub(r"```[a-zA-Z]*\n?", "", body)
    lines = [ln.rstrip() for ln in body.split("\n")]

    section = re.compile(r"^\s*(on you|others|open items|on me)\s*:?\s*$", re.I)
    out = []
    bulletize = expert in ("catch_up", "open_items", "general", "done")
    for ln in lines:
        if not ln.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        m = section.match(ln)
        if m:
            out.append(f"*{m.group(1).title()}*")
            bulletize = expert in ("catch_up", "open_items", "general", "done")
            continue
        stripped = ln.strip()
        if stripped.startswith(("- ", "* ", "• ", "1. ", "2. ", "3.")) or stripped.startswith("*") and stripped.endswith("*"):
            out.append(stripped)
            continue
        if bulletize:
            out.append("- " + stripped)
        else:
            out.append(stripped)
    # collapse runs of blank lines, trim ends
    tidy = []
    for ln in out:
        if ln == "" and (not tidy or tidy[-1] == ""):
            continue
        tidy.append(ln)
    while tidy and tidy[-1] == "":
        tidy.pop()
    return "\n".join(tidy)


CALL1_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "enum": ["new", "follow_up", "note", "send",
                                            "alert_action", "command"]},
        "groups": {"type": "array", "items": {"type": "string"}},
        "task_outline": {"type": "string"},
        "follow_up_refs": {"type": "array", "items": {"type": "string"}},
        "window": {"type": "object", "properties": {"from": {"type": "string"},
                                                    "to": {"type": "string"}}},
        "answer_shape": {"type": "string",
                         "enum": ["direct_answer", "list", "on_you_others", "done"]},
    },
    "required": ["task", "groups", "task_outline", "follow_up_refs"],
}

CALL2_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "items": {"type": "object", "properties": {
            "group": {"type": "string"}, "who": {"type": "string"},
            "when": {"type": "string"}, "what": {"type": "string"},
            "quote": {"type": "string"}}, "required": ["group", "who", "when", "what", "quote"]}},
        "on_you": {"type": "array", "items": {"type": "string"}},
        "others": {"type": "array", "items": {"type": "string"}},
        "open_items": {"type": "array", "items": {"type": "object", "properties": {
            "chat": {"type": "string"}, "person": {"type": "string"},
            # owner_quote must be copied from the source message and prove the
            # actor. A missing/unproven owner falls back to person, then Others.
            "owner": {"type": "string"}, "owner_quote": {"type": "string"},
            "what": {"type": "string"}, "deadline": {"type": "string"},
            "since": {"type": "string"}, "next_action": {"type": "string"},
            "priority": {"type": "string", "enum": ["high", "medium", "low"]},
            "on_you": {"type": "boolean"}}, "required": ["what"]}},
        "done": {"type": "array", "items": {"type": "object", "properties": {
            "group": {"type": "string"}, "who": {"type": "string"},
            "when": {"type": "string"}, "what": {"type": "string"},
            "quote": {"type": "string"}}, "required": ["group", "who", "when", "what", "quote"]}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "coverage": {"type": "object", "properties": {
            "messages": {"type": "integer"},
            "media_unsearchable": {"type": "integer"},
            "cut": {"type": "boolean"}}},
    },
    "required": ["facts", "on_you", "others", "done", "conflicts", "coverage"],
}

CALL3_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
                "required": ["answer"]}

MERGED_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "citations"],
}


def _merged_prompt(text, outline, names, valid, context, mode):
    groups = ", ".join(names.get(j, j) for j in valid)
    shape = "direct_answer" if mode in ("lookup", "decision") else "list"
    reply_rule = ("- Each bullet is one item that needs a reply: chat, person, when, what, "
                  "ordered by urgency (overdue or deadline-today first, then direct asks to "
                  "the user, then others). Do not use On you/Others sections.\n"
                  if mode == "reply" else "")
    decision_rule = ("- This is a decision/why question: answer the decision and the reason in a short "
                     "paragraph, then one source line. Treat kill/stop/pause/go/no-go statements as "
                     "facts and cite the deciding message. If only a recommendation exists, say "
                     "\"recommendation, not a confirmed decision\" instead of \"no record\". If the "
                     "evidence is older than the window, say \"in the last N days\".\n"
                     if mode == "decision" else "")
    now = mstate.now_local()
    tomorrow = now + datetime.timedelta(days=1)
    date_anchor = (f"Today is {now.strftime('%A %d %b %Y')} IST. "
                   f"\"Tomorrow\" is {tomorrow.strftime('%A %d %b %Y')}.\n")
    return f"""{agent_config.persona_block()}{_identity_block()}Answer the user's WhatsApp question using ONLY the messages below.

{date_anchor}Exact question: {text}
Task outline: {outline}
Chats: {groups}
Answer shape: {shape}

Rules:
- Use only these messages. If the answer is not there, say so and mention the nearest thing.
- Attribute facts ("Raghav, Mon 10:07"). Quote only the deciding phrase.
- Keep amounts, dates and names exact.
- A go-live, launch, visit, delivery or deadline counts as a plan even if the word "plan" is absent.
- Reply in the user's language, Hinglish included.
- If answer_shape is direct_answer: one short paragraph followed by one source line; no bullets.
- Otherwise one bullet per line, each starting with "- ".
- If more than one chat, prefix every bullet with "- *<Chat name>*: ".
- At most 14 bullets. No title, no preamble, no filler.
{reply_rule}{decision_rule}- Never invent facts, dates, numbers or people.
- citations: short source labels such as "Chat · Person · Day HH:MM".

Messages:
{context[:QA_CONTEXT_CHARS]}

Return ONLY JSON."""


def _fresh_card(card, now):
    if not card or not card.get("updated_at"):
        return False
    try:
        return (now - datetime.datetime.fromisoformat(card["updated_at"])).total_seconds() < 24 * 3600
    except (TypeError, ValueError):
        return False


def _fallback_card(jid, rows):
    p = qa_profiles.get_profile(jid)
    desc = (p.get("description") or "").strip() if p else ""
    topics = ", ".join((p.get("topics") or [])) if p else ""
    latest = (rows[-1]["text"] or "")[:140] if rows else "unclear"
    return {"objective": desc if desc and desc != "unclear" else "unclear",
            "themes": topics or "unclear", "latest": latest or "unclear"}


def _build_card(jid, name, rows):
    transcript = []
    for m in rows[-80:]:
        try:
            stamp = datetime.datetime.fromisoformat(m["ts"]).strftime("%a %H:%M")
        except (TypeError, ValueError):
            stamp = ""
        transcript.append(f"[{stamp}] {m['sender']}: {(m['text'] or '')[:200]}")
    fallback = _fallback_card(jid, rows)
    if not transcript:
        return fallback
    prompt = f"""Write a factual card for this WhatsApp group. Only use the messages below; if unclear, say "unclear". No goals, no judgements.

Group: {name!r}

Messages (oldest first):
{chr(10).join(transcript)}

Return ONLY JSON: {{"objective": "one line: what the group is used for", "themes": "one line: 3-6 recurring subjects", "latest": "one line: the latest material development"}}"""
    data = budget.gemini_json(prompt, fallback,
                              {"type": "object", "properties": {
                                  "objective": {"type": "string"},
                                  "themes": {"type": "string"},
                                  "latest": {"type": "string"}},
                               "required": ["objective", "themes", "latest"]},
                              purpose="qa-group-card")
    return {"objective": data.get("objective") or fallback["objective"],
            "themes": data.get("themes") or fallback["themes"],
            "latest": data.get("latest") or fallback["latest"]}


def _store_card(jid, card, now):
    c = mstate.connect()
    try:
        c.execute("INSERT OR REPLACE INTO group_cards "
                  "(chat_jid, objective, themes, latest, updated_at) VALUES (?,?,?,?,?)",
                  (jid, card["objective"][:180], card["themes"][:180],
                   card["latest"][:180], now.isoformat()))
        c.commit()
    finally:
        c.close()


def group_cards(limit=None):
    """Cards for eligible groups. Never calls the model inline: cached cards
    first, else a code fallback. Background builds fill the cache."""
    elig = qa_profiles.eligibility()
    now = mstate.now_local()
    c = mstate.connect()
    try:
        cached = {r["chat_jid"]: dict(r) for r in c.execute("SELECT * FROM group_cards").fetchall()}
    finally:
        c.close()
    out = []
    for jid, (name, ok, reason) in elig.items():
        if not ok:
            continue
        card = cached.get(jid)
        if not _fresh_card(card, now):
            card = _fallback_card(jid, _load(jid))
        out.append({"jid": jid, "name": name,
                    **{k: (card.get(k) or "unclear")[:180] for k in ("objective", "themes", "latest")}})
        if limit and len(out) >= limit:
            break
    return out


def ensure_cards():
    """Build/refresh cards for eligible groups (background job)."""
    elig = qa_profiles.eligibility()
    now = mstate.now_local()
    c = mstate.connect()
    try:
        cached = {r["chat_jid"]: dict(r) for r in c.execute("SELECT * FROM group_cards").fetchall()}
    finally:
        c.close()
    built = 0
    for jid, (name, ok, reason) in elig.items():
        if built >= CARD_BATCH:
            break
        if not ok or not jid.endswith("@g.us") or _fresh_card(cached.get(jid), now):
            continue
        rows = _load(jid)
        card = _build_card(jid, name, rows)
        _store_card(jid, card, now)
        built += 1
    return built


def _group_list_text(cards):
    return "\n\n".join(
        f"id: {c['jid']}\nkind: {'group' if c['jid'].endswith('@g.us') else 'DM'}\n"
        f"name: {c['name']}\nobjective: {c['objective']}\n"
        f"themes: {c['themes']}\nlatest: {c['latest']}" for c in cards)


def _window_from(d1, raw_text):
    w = d1.get("window") or {}
    txt = ((d1.get("task_outline") or "") + " " + (raw_text or "")).lower()
    if any(k in txt for k in ("today", "tofay", "yesterday", "morning",
                              "last night", "week", "month")):
        return _global_window(txt)
    now = mstate.now_local()
    if "tomorrow" in txt:
        # A question about tomorrow is answered from what was said before it;
        # the date belongs to the question, not to the message window.
        return now - datetime.timedelta(days=7), now
    if DECISION_RE.search(txt):
        # Decisions/why questions need the long view; 7 days misses the pivot.
        return now - datetime.timedelta(days=min(30, mstate.RETENTION_DAYS)), now
    start = end = None
    try:
        if w.get("from") and w.get("to"):
            start = datetime.datetime.fromisoformat(w["from"]).replace(tzinfo=mstate.local_tz())
            end = datetime.datetime.fromisoformat(w["to"]).replace(tzinfo=mstate.local_tz())
    except (TypeError, ValueError):
        start = end = None
    if end and end > now:
        end = now
    if start and start > now:
        start = now - datetime.timedelta(days=7)
    if "morning" in txt:
        start = now.replace(hour=8, minute=0, second=0, microsecond=0)
        end = now
    elif "today" in txt and (start is None or start.hour == 0):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    if start and end:
        return start, end
    return now - datetime.timedelta(days=7), now


def _flow_messages(gids, start, end=None, limit=220, names=None,
                   person=None, stats=None):
    """Recent messages from the scope, with replies and media made visible.

    ``person`` is {"name": ..., "idents": set(...)} to focus a thread query.
    Returns (context, message_count, media_count).
    """
    end = end or mstate.now_local()
    names = names or {}
    if stats is None:
        stats = {}
    blocks, total, media_count = [], 0, 0
    for jid in gids:
        rows = _load(jid)
        picked = []
        for m in rows:
            try:
                ts = datetime.datetime.fromisoformat(m["ts"])
            except (TypeError, ValueError):
                continue
            if start <= ts <= end:
                picked.append(m)
        if person:
            ident = person.get("idents") or set()
            nm = (person.get("name") or "").lower()
            # A person query is only their DM/thread and direct mentions; the
            # user's unrelated messages in other chats must not leak in.
            is_dm_thread = (jid in ident or
                            (not jid.endswith("@g.us") and bool(nm) and
                             nm in (_chat_name(jid) or "").lower()))
            keep = set()
            for i, m in enumerate(picked):
                hit = (is_dm_thread or m["raw_sender"] in ident or
                       (nm and nm in (m["text"] or "").lower()) or
                       (nm and nm in (m.get("quoted_text") or "").lower()))
                if hit:
                    keep.add(i)
                    for j in range(i, min(i + 3, len(picked))):
                        keep.add(j)
            picked = [picked[i] for i in sorted(keep)]
        lines = []
        for m in picked[-limit:]:
            body = m["transcript"] or m["text"]
            if not body and m["media"]:
                body = _media_label(m["media"], m.get("filename", ""))
                media_count += 1
                if body.startswith("[voice"):
                    stats["voice"] = stats.get("voice", 0) + 1
                else:
                    stats["media"] = stats.get("media", 0) + 1
            if not body:
                continue
            try:
                stamp = datetime.datetime.fromisoformat(m["ts"]).strftime("%a %H:%M")
            except (TypeError, ValueError):
                stamp = ""
            quoted = ""
            if m.get("quoted_text"):
                quoted = f' (↪ {m.get("quoted_who") or "someone"}: "{m["quoted_text"][:80]}")'
            lines.append(f"[{stamp}] {m['sender']}{quoted}: {body[:320]}")
        if lines:
            name = names.get(jid) or _chat_name(jid)
            blocks.append(f"### {name} [{jid}]\n" + "\n".join(lines))
            total += len(lines)
    return "\n\n".join(blocks), total, media_count


def _search_context(text, start, end, names, allowed=None, limit=20,
                    stats=None):
    """Keyword hits plus five messages on either side, across chats."""
    if stats is None:
        stats = {}
    try:
        hits = qa_search.search(
            engine.MESSAGES_DB_PATH, mstate.ENGINE_DB_PATH, text, start, end,
            limit=limit, exclude=set(mechat.mechat_jids()))
    except Exception:
        return "", set(), 0, 0
    if allowed is not None:
        hits = [h for h in hits if h.get("chat_jid") in allowed]
    by_chat = {}
    for h in hits:
        by_chat.setdefault(h["chat_jid"], []).append(h)
    blocks, total, media = [], 0, 0
    for jid, hs in by_chat.items():
        if _excluded_chat(jid):
            continue
        rows = _load(jid)
        idx = {m["id"]: i for i, m in enumerate(rows)}
        picked = set()
        for h in hs:
            i = idx.get(h.get("msg_id"))
            if i is None:
                continue
            for j in range(max(0, i - 5), min(len(rows), i + 6)):
                picked.add(j)
        lines = []
        for i in sorted(picked):
            m = rows[i]
            body = m["transcript"] or m["text"]
            if not body and m["media"]:
                body = _media_label(m["media"], m.get("filename", ""))
                media += 1
                if body.startswith("[voice"):
                    stats["voice"] = stats.get("voice", 0) + 1
                else:
                    stats["media"] = stats.get("media", 0) + 1
            if not body:
                continue
            try:
                stamp = datetime.datetime.fromisoformat(m["ts"]).strftime("%a %d %b %H:%M")
            except (TypeError, ValueError):
                stamp = ""
            quoted = ""
            if m.get("quoted_text"):
                quoted = f' (↪ {m.get("quoted_who") or "someone"}: "{m["quoted_text"][:80]}")'
            lines.append(f"[{stamp}] {m['sender']}{quoted}: {body[:320]}")
        if lines:
            name = names.get(jid) or _chat_name(jid)
            blocks.append(f"### {name} [{jid}] (keyword matches)\n" + "\n".join(lines))
            total += len(lines)
    return "\n\n".join(blocks), set(by_chat), total, media


DONE_CAP_CHATS = int(os.getenv("QA_DONE_CHATS", "200"))
DONE_MAX_CHARS = 120_000
DONE_MAX_ITEMS = 60
DONE_BATCH = int(os.getenv("QA_DONE_BATCH", "250"))
DONE_BATCH_CHARS = int(os.getenv("QA_DONE_BATCH_CHARS", "120000"))
CARD_BATCH = _envload.getenv_int("NEMBU_CARD_BATCH", "MOSAIC_CARD_BATCH", 10)
DONE_MIN_CHARS = 8
DONE_TAIL_REPLIES = 2
DONE_HIGHLIGHTS = 7

DONE_SCHEMA = {
    "type": "object",
    "properties": {
        "done": {"type": "array", "items": {"type": "object", "properties": {
            "n": {"type": "integer"}, "what": {"type": "string"},
            "quote": {"type": "string"},
            "also": {"type": "array", "items": {"type": "integer"}}},
            "required": ["n", "what", "quote"]}},
    },
    "required": ["done"],
}

_DONE_NOISE_RE = re.compile(
    r"(ok|okay|k|kk|thanks?|thank you|thx|done|yes|yeah|no|nope|sure|cool|great|"
    r"nice|hmm+|👍+|🙏+|lol|haha|\?+|\.+|good|fine|noted|received|ack)[\s.!]*",
    re.I,
)
_URL_ONLY_RE = re.compile(r"https?://\S+", re.I)
_NEMBU_FORWARD_RE = re.compile(r"^\s*(?:\*Mosaic\*|\*Nembu\*|◆|🍋)", re.I)
_MOSAIC_FORWARD_RE = _NEMBU_FORWARD_RE  # legacy alias
_QUESTION_START_RE = re.compile(
    r"^\s*(?:what|whats|what'?s|when|where|who|whom|whose|why|how|which|"
    r"can|could|would|should|shall|do|does|did|is|are|was|were|will|"
    r"any|anyone|anybody|kya|kab|kaun|kahan|kaise)\b",
    re.I,
)


def _is_forwarded_mosaic(content):
    """True for a forwarded *Nembu* 🍋/◆ summary, which is not primary evidence."""
    return bool(_MOSAIC_FORWARD_RE.match((content or "").strip()))


def _is_question_only(content):
    """True when the user's message is only a question, so it cannot be done."""
    text = (content or "").strip()
    if not text:
        return False
    if text.endswith("?"):
        return True
    return bool(_QUESTION_START_RE.match(text))


def _evidence_quote(candidate_text, quote):
    """Return a quote that is verbatim inside the user's own candidate text."""
    text = (candidate_text or "").strip()
    if not text:
        return ""
    q = (quote or "").strip()
    if q and q.lower() in text.lower():
        return q
    return text[:160]



def _fmt_ts(raw):
    try:
        return datetime.datetime.fromisoformat(str(raw)).strftime("%a %d %b %H:%M")
    except (TypeError, ValueError):
        return str(raw or "")


def _is_done_message(content, min_chars=DONE_MIN_CHARS):
    text = (content or "").strip()
    if len(text) < min_chars:
        return False
    return not (_DONE_NOISE_RE.fullmatch(text) or _URL_ONLY_RE.fullmatch(text))


_FUTURE_Q_RE = re.compile(
    r"\b(tomorrow|day after|next week|next month|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))\b",
    re.I,
)


def _future_plan_context(text, start, end, names=None, allowed=None,
                         db_path=None):
    """Matching future-plan messages, ready to merge into search context."""
    if not _FUTURE_Q_RE.search(text or ""):
        return "", [], []
    db_path = db_path or engine.MESSAGES_DB_PATH
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT id, chat_jid, sender, content, timestamp FROM messages "
                "WHERE timestamp >= ? AND timestamp <= ? AND ("
                "content LIKE '%tomorrow%' OR content LIKE '%go live%' OR "
                "content LIKE '%go-live%' OR content LIKE '%launch%' OR "
                "content LIKE '%visit%' OR content LIKE '%next week%') "
                "AND chat_jid NOT LIKE '%@newsletter' "
                "AND chat_jid <> 'status@broadcast' ORDER BY timestamp",
                (start.strftime("%Y-%m-%d %H:%M:%S"),
                 end.strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return "", [], []
    allow = set(allowed) if allowed is not None else None
    names = names or {}
    rows_out, ids, jids = [], [], []
    for mid, jid, sender, content, ts in rows:
        if not jid or _excluded_chat(jid) or (allow is not None and jid not in allow):
            continue
        rows_out.append((jid, f"[{_fmt_ts(ts)}] {_sender_label(sender, jid)}: "
                              f"{(content or '')[:320]} (id {mid})"))
        ids.append(str(mid))
        if jid not in jids:
            jids.append(jid)
    return _group_context_lines(rows_out, names), ids, jids


def _future_plan_chats(text, start, end, allowed=None, db_path=None):
    """Chats whose recent messages state a concrete future plan.

    A go-live, launch, visit or deadline is a plan even when the word "plan"
    never appears, so a "what's tomorrow" question must scan for these.
    """
    _ctx, _ids, jids = _future_plan_context(
        text, start, end, allowed=allowed, db_path=db_path)
    return jids


def _scope_excluded_jids(include_archived=None):
    """Chats excluded from done scope: archived (unless opted in), muted, bot,
    and explicit operator exclusions."""
    if include_archived is None:
        try:
            include_archived = agent_config.include_archived()
        except Exception:
            include_archived = os.getenv("QA_INCLUDE_ARCHIVED", "0") == "1"
    reasons = {"muted", "bot", "excluded"} if include_archived else {"archived", "muted", "bot", "excluded"}
    try:
        elig = qa_profiles.eligibility()
    except Exception:
        return set()
    return {jid for jid, (_name, ok, reason) in elig.items()
            if not ok and reason in reasons}


def _done_scope(db_path, start, end, cap=DONE_CAP_CHATS, exclude=None):
    """Chats the user actually posted in, ranked by their own message count.

    Archived, muted and bot chats are excluded unless QA_INCLUDE_ARCHIVED=1.
    Returns ([(jid, user_msg_count)], omitted_chat_count).
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT chat_jid, COUNT(*) FROM messages "
                "WHERE timestamp >= ? AND timestamp <= ? AND is_from_me=1 "
                "AND chat_jid NOT LIKE '%@newsletter' "
                "AND chat_jid <> 'status@broadcast' "
                "GROUP BY chat_jid ORDER BY COUNT(*) DESC, chat_jid",
                (start.strftime("%Y-%m-%d %H:%M:%S"),
                 end.strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return [], 0
    excluded = _scope_excluded_jids() if exclude is None else set(exclude)
    rows = [r for r in rows
            if r[0] and not _excluded_chat(r[0]) and r[0] not in excluded]
    selected = [(r[0], int(r[1])) for r in rows[:cap]]
    return selected, max(0, len(rows) - len(selected))


def _done_candidates(chats, db_path, start, end, names=None,
                     min_chars=DONE_MIN_CHARS, tail=DONE_TAIL_REPLIES):
    """Code-owned recall guard: every substantive message the user sent, with
    the next replies that show the outcome. The model classifies these by
    number; it never invents group or time.
    """
    names = names or {}
    if not chats:
        return [], {"candidates": 0, "user_messages": 0, "window_messages": 0,
                    "chats": 0, "cut": False, "media_unsearchable": 0}
    jids = [j for j, _n in chats]
    ph = ",".join("?" * len(jids))
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT id, chat_jid, sender, content, media_type, timestamp, "
                "is_from_me FROM messages WHERE chat_jid IN (" + ph + ") "
                "AND timestamp >= ? AND timestamp <= ? "
                "ORDER BY chat_jid, timestamp",
                (*jids, start.strftime("%Y-%m-%d %H:%M:%S"),
                 end.strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        rows = []
    by_chat = {}
    for r in rows:
        by_chat.setdefault(r[1], []).append(r)
    seen = set()
    cands = []
    media = 0
    voice = 0
    sent = 0
    for jid in jids:
        seq = by_chat.get(jid) or []
        for i, r in enumerate(seq):
            if not r[6]:
                continue
            sent += 1
            content = (r[3] or "").strip()
            if not content:
                if r[4]:
                    media += 1
                    if _media_label(r[4]).startswith("[voice"):
                        voice += 1
                continue
            if len(content) < min_chars:
                continue
            if not _is_done_message(content, min_chars):
                continue
            if _is_forwarded_mosaic(content) or _is_question_only(content):
                continue
            key = (jid, " ".join(content.lower().split()).rstrip(".! "))
            if key in seen:
                continue
            seen.add(key)
            nxt = []
            for j in range(i + 1, len(seq)):
                body = (seq[j][3] or "").strip()
                if not body:
                    continue
                if seq[j][6]:
                    if not _is_done_message(body, min_chars):
                        continue
                    dup = (jid, " ".join(body.lower().split()).rstrip(".! "))
                    if dup in seen:
                        continue
                    break
                who = seq[j][2] or "Someone"
                try:
                    who = engine._resolve_sender_name(seq[j][2]) or who
                except Exception:
                    pass
                nxt.append(f"{who}: {body[:160]}")
                if len(nxt) >= tail:
                    break
            name = names.get(jid) or _chat_name(jid)
            cands.append({"jid": jid,
                          "group": _display_group_name(name, jid) or jid,
                          "when": _fmt_ts(r[5]), "ts": str(r[5]),
                          "text": content[:320], "next": " | ".join(nxt),
                          "confirmed": bool(nxt)})
    cands.sort(key=lambda c: c["ts"])
    for i, c in enumerate(cands, 1):
        c["n"] = i
    meta = {"candidates": len(cands), "user_messages": len(cands),
            "sent_messages": sent,
            "window_messages": len(rows),
            "chats": len(set(c["jid"] for c in cands)),
            "chats_read": len(by_chat),
            "cut": False, "media_unsearchable": media,
            "voice_unsearchable": voice,
            "media_other": max(0, media - voice)}
    return cands, meta


def _done_candidate_text(cands):
    lines = []
    for c in cands:
        line = f"[{c['n']}] {c['group']} · {c['when']}\n    wrote: {c['text']}"
        if c["next"]:
            line += f"\n    then: {c['next']}"
        lines.append(line)
    return "\n\n".join(lines)


def _display_group_name(name, jid=""):
    """Short, human label for a chat; never a full sentence as a group name."""
    raw = " ".join((name or "").split())
    jid = jid or ""
    if not re.search(r"[A-Za-z0-9]", raw):
        raw = ""
    if not raw:
        if jid and not jid.endswith("@g.us"):
            return _dm_fallback(jid)
        bare = re.sub(r"\D", "", jid)
        return f"Group {bare[-6:]}" if len(bare) >= 6 else ""
    sentence_like = (len(raw) > 40 or raw.count(" ") >= 6 or
                     bool(re.search(r"[.!?]\s", raw)))
    if not sentence_like:
        return raw
    if jid and not jid.endswith("@g.us"):
        return _dm_fallback(jid)
    prof = qa_profiles.get_profile(jid) if jid else {}
    alias = ((prof.get("aliases") or [""])[0]).strip() if prof else ""
    if alias:
        return alias[:40]
    bare = re.sub(r"\D", "", jid)
    return f"Group {bare[-6:]}" if len(bare) >= 6 else raw[:40]


def _done_body(done):
    """Render extracted accomplishments in code, so no item is dropped."""
    lines = []
    seen = set()
    for d in done or []:
        if not isinstance(d, dict):
            continue
        what = (d.get("what") or "").strip()
        if not what:
            continue
        key = " ".join(what.lower().split())
        if key in seen:
            continue
        seen.add(key)
        group = _display_group_name(d.get("group") or "", d.get("jid") or "")
        when = (d.get("when") or "").strip()
        quote = (d.get("quote") or "").strip()
        line = (f"- *{group}*: " if group else "- ") + what
        if when:
            line += f" ({when})"
        if quote and quote.lower() not in what.lower():
            line += f' - "{quote[:160]}"'
        lines.append(line)
    return "\n".join(lines)


def _done_category(item):
    name = (item.get("group") or "").lower()
    jid = item.get("jid") or ""
    if "instinct" in name or "assistant" in name:
        return "Assistant"
    if jid.endswith("@g.us"):
        return "Work"
    if jid:
        return "Personal"
    return "Work"


def _done_split(done):
    groups = {"Work": [], "Personal": [], "Assistant": []}
    for d in done or []:
        groups[_done_category(d)].append(d)
    return groups


def _done_highlights(done, n=DONE_HIGHLIGHTS):
    """Top items: confirmed by an outcome reply first, then most recent."""
    ranked = sorted(
        done or [],
        key=lambda d: (bool(d.get("confirmed")), str(d.get("ts") or "")),
        reverse=True,
    )
    return ranked[:n]


def _done_summary_body(done, groups=None, highlights=None):
    groups = groups if groups is not None else _done_split(done)
    highlights = (highlights if highlights is not None
                  else _done_highlights(done))
    lines = [f"*Top {len(highlights)}*"]
    for i, d in enumerate(highlights, 1):
        group = _display_group_name(d.get("group") or "", d.get("jid") or "")
        what = (d.get("what") or "").strip()
        when = (d.get("when") or "").strip()
        line = (f"{i}. *{group}* — {what}" if group else f"{i}. {what}")
        if when:
            line += f" ({when})"
        lines.append(line)
    split = " · ".join(f"*{k}* {len(v)}" for k, v in groups.items() if v)
    if split:
        lines.append("")
        lines.append(split)
    return "\n".join(lines)


def _done_full_lines(groups):
    """Every extracted item as a complete line; never truncate a clause."""
    lines = []
    for cat in ("Work", "Personal", "Assistant"):
        items = groups.get(cat) or []
        if not items:
            continue
        lines.append(f"*{cat}*")
        for d in items:
            group = _display_group_name(d.get("group") or "", d.get("jid") or "")
            what = (d.get("what") or "").strip()
            when = (d.get("when") or "").strip()
            line = (f"- *{group}*: " if group else "- ") + what
            if when:
                line += f" ({when})"
            lines.append(line)
    return lines


def _done_full_list(groups):
    """The full list, complete. One 'more' delivers it (see chunk_more)."""
    return "\n".join(_done_full_lines(groups))


def _store_summary_first(head, rest, group_jid):
    """Store a short first message and the full list behind one 'more'.

    ``head_lines`` is stored negative as a sentinel: ``chunk_more`` then sends
    the whole remaining list in one message instead of truncating it.
    """
    full = head + (("\n\n" + rest) if rest else "")
    head_lines = -(head.count("\n") + 1)
    c = mstate.connect()
    try:
        cur = c.execute(
            "INSERT INTO answers (turn_key, full_text, head_lines) VALUES (?,?,?)",
            (f"qa:{group_jid}:{__import__('uuid').uuid4().hex[:10]}",
             full, head_lines),
        )
        c.commit()
        return head + "\n\nSay *more* for the full list.", str(cur.lastrowid)
    finally:
        c.close()


def _done_footer(meta, omitted):
    bits = []
    if meta.get("window_messages") is not None:
        bits.append(f"read {meta.get('window_messages', 0)} msgs from "
                    f"{meta.get('chats_read', meta.get('chats', 0))} chats")
    if meta.get("sent_messages") is not None:
        bits.append(f"{meta['sent_messages']} msgs sent")
    if meta.get("candidates") is not None:
        bits.append(f"{meta['candidates']} substantive candidates")
    elif meta.get("user_messages"):
        bits.append(f"{meta['user_messages']} substantive candidates")
    if meta.get("chats_in_scope") is not None:
        bits.append(f"{meta['chats_in_scope']} chats in scope")
    if meta.get("chats") is not None:
        bits.append(f"{meta['chats']} chats with your messages")
    if meta.get("cut"):
        bits.append("some messages were left out")
    if omitted:
        bits.append(f"{omitted} chats not scanned")
    voice = meta.get("voice_unsearchable", 0) or 0
    other = meta.get("media_other")
    if other is None:
        other = meta.get("media_unsearchable", 0) or 0
    if voice:
        bits.append(f"{voice} voice notes not transcribed")
    if other:
        bits.append(f"{other} images/docs/video not searchable")
    return ("\n_" + " · ".join(bits) + "_") if bits else ""


def _done_extract(cands, text, start, end, names, chats, trace_id=""):
    """Extract accomplishments in batches so no candidate is silently cut."""
    by_n = {c["n"]: c for c in cands}
    out = []
    seen = set()
    failed = 0
    batches, cur, size = [], [], 0
    for c in cands:
        line_len = len(c["text"]) + len(c["next"]) + 80
        if cur and (len(cur) >= DONE_BATCH or size + line_len > DONE_BATCH_CHARS):
            batches.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += line_len
    if cur:
        batches.append(cur)
    persona = agent_config.persona_block()

    def run_batch(bi, batch):
        prompt = f"""{persona}{_identity_block()}Extract every accomplishment the USER completed, shipped, closed, delivered, decided, reviewed, directed, or followed up on in this window.

Question: {text}
Batch {bi} of {len(batches)}. Below are {len(batch)} messages the user sent, numbered [n], each with the next replies that show the outcome. Window: {start.strftime('%a %d %b %H:%M')} → {end.strftime('%a %d %b %H:%M')}.
Chats: {', '.join(names.get(j, j) for j, _n in chats)}

Rules:
- Process EVERY numbered candidate in this batch. Do not stop at 10; return up to {DONE_MAX_ITEMS} genuine items.
- n: the candidate number. what: one concrete line (what changed, for whom). quote: the deciding phrase from the user's own message.
- Only the user's own completed action. A question, plan or promise is not done unless a later reply shows it finished.
- A direction the user issued ("update this sheet today", "set the MBR for 17 Nov") counts as done. Phrase it as "Directed ...".
- quote MUST be a phrase copied verbatim from the user's "wrote:" text, never from a reply by someone else. If no phrase fits, copy the user's text.
- Skip pure acknowledgements, link-only messages, chatter and other people's work.
- If several candidates are the same accomplishment, return the earliest n and list the others in "also".
- Empty if none.

Candidates:
{_done_candidate_text(batch)[:DONE_BATCH_CHARS]}

Return ONLY JSON."""
        data = _call_json_retry(prompt, {"done": []}, DONE_SCHEMA,
                                "qa-done-extract", trace_id, model=QA_MODEL,
                                attempts=2)
        return bi, data

    workers = min(4, max(1, len(batches)))
    results = []
    if len(batches) <= 1:
        results = [run_batch(1, batches[0])] if batches else []
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_batch, bi, batch)
                       for bi, batch in enumerate(batches, 1)]
            for fut in futures:
                try:
                    results.append(fut.result())
                except Exception as exc:  # pragma: no cover - defensive
                    failed += 1
                    mstate.event(trace_id, "qa.done", "warn",
                                 f"batch error: {type(exc).__name__}")
    for bi, data in sorted(results, key=lambda r: r[0]):
        if data.get("_gemini_failed"):
            failed += 1
            mstate.event(trace_id, "qa.done", "warn",
                         f"batch {bi}/{len(batches)} failed: "
                         f"{data.get('_reason') or 'bad_output'}")
            continue
        for d in data.get("done") or []:
            if not isinstance(d, dict):
                continue
            try:
                n = int(d.get("n") or 0)
            except (TypeError, ValueError):
                continue
            c = by_n.get(n)
            if not c or n in seen:
                continue
            for extra in d.get("also") or []:
                try:
                    seen.add(int(extra or 0))
                except (TypeError, ValueError):
                    pass
            seen.add(n)
            what = (d.get("what") or "").strip()
            if not what:
                continue
            out.append({"group": c["group"], "when": c["when"], "ts": c["ts"],
                        "jid": c["jid"], "confirmed": bool(c.get("confirmed")),
                        "what": what,
                        "quote": _evidence_quote(c["text"], d.get("quote"))})
    out.sort(key=lambda x: x.get("ts", ""))
    return out, failed


def _flow_done(text, start, end, forced_group="", names=None, trace_id=""):
    """Exhaustive 'what did I get done' path.

    Scope and retrieval are code-owned: every group the user posted in, their
    own messages, and the next replies that show outcomes. The model only
    extracts and writes.
    """
    names = dict(names or {})
    if forced_group:
        chats, omitted = [(forced_group, 0)], 0
    else:
        chats, omitted = _done_scope(engine.MESSAGES_DB_PATH, start, end)
    for jid, _n in chats:
        if not names.get(jid):
            names[jid] = _chat_name(jid)
        names[jid] = names.get(jid) or jid
    if not chats:
        return {"kind": "question", "decision": "resolved", "jid": forced_group,
                "name": names.get(forced_group, forced_group), "expert": "done",
                "question": text, "turn_key": "",
                "reply": f"*Nembu* 🍋 · {_header_range(start, end)}\n- Nothing you sent found in that window.",
                "coverage": {"messages": 0, "chats": 0}}
    cands, meta = _done_candidates(chats, engine.MESSAGES_DB_PATH, start, end,
                                   names=names)
    if not cands:
        return {"kind": "question", "decision": "resolved", "jid": forced_group,
                "name": names.get(forced_group, forced_group), "expert": "done",
                "question": text, "turn_key": "",
                "reply": f"*Nembu* 🍋 · {_header_range(start, end)}\n- Nothing you sent found in that window.",
                "coverage": meta}
    done, failed = _done_extract(cands, text, start, end, names, chats,
                                 trace_id=trace_id)
    meta["cut"] = failed > 0
    meta["chats_in_scope"] = len(chats)
    mstate.event(trace_id, "qa.done", "ok",
                 f"{len(done)} done · {len(cands)} candidates · {meta['chats']} chats")
    if len(chats) == 1:
        header = (f"*Nembu* 🍋 · *{_display_group_name(names[chats[0][0]], chats[0][0])}*"
                  f" · {end.strftime('%a %d %b')}")
    else:
        header = f"*Nembu* 🍋 · {len(chats)} chats · {_header_range(start, end)}"
    footer = _done_footer(meta, omitted)
    if len(done) > DONE_HIGHLIGHTS:
        groups = _done_split(done)
        head = header + "\n" + _done_summary_body(done, groups) + footer
        rest = _done_full_list(groups)
        reply, turn_key = _store_summary_first(
            head, rest,
            forced_group or (chats[0][0] if len(chats) == 1 else "__multi__"))
        body = head
    else:
        body = _done_body(done) or "- No completed items found in this window."
        full = header + "\n" + body + footer
        reply, turn_key = _chunk_and_store(
            full, forced_group or (chats[0][0] if len(chats) == 1 else "__multi__"))
    _drop_choice()
    if len(chats) == 1:
        set_selection(chats[0][0], names[chats[0][0]], text, body)
    return {"kind": "question", "decision": "resolved", "jid": chats[0][0],
            "name": names[chats[0][0]], "expert": "done", "question": text,
            "reply": reply, "turn_key": turn_key, "coverage": meta,
            "global": len(chats) > 1}


def _strip_single_group_prefix(body, name):
    out = []
    for line in (body or "").split("\n"):
        s = line.strip()
        pref = f"- *{name}*:"
        if s.lower().startswith(pref.lower()):
            s = "- " + s[len(pref):].strip()
        out.append(s)
    return "\n".join(out)


def _alias_hits(text):
    norm = qa_profiles.normalize(text)
    if not norm:
        return {}
    profs = qa_profiles.profiles(eligible_only=True)
    hits = {}
    for jid, p in profs.items():
        best = 0
        for alias in p.get("aliases") or []:
            a = qa_profiles.normalize(alias)
            if a and (a in norm or (len(a) >= 4 and qa_profiles.phonetic_key(a) == qa_profiles.phonetic_key(norm))):
                best = max(best, len(a))
        if best:
            hits[jid] = best
    return hits


_BROAD_RE = re.compile(
    r"\b(miss|missed|summary|summar\w*|recap|important|urgent|pending|respond|"
    r"followed up|follow up|got done|completed|across|everything|catch me up|"
    r"on my plate|to-?do|overview|this week|this month)\b", re.I)


def is_broad_question(text) -> bool:
    return bool(_BROAD_RE.search(text or ""))


DONE_RE = re.compile(
    r"\b(got done|get done|completed|finished|closed|shipped|delivered|"
    r"accomplish\w*|achieved|followed up|what did i do)\b", re.I)
RESPOND_RE = re.compile(
    r"\b(respond|reply to|need to reply|should i reply|who should i reply)\b", re.I)
DECISION_RE = re.compile(
    r"\b(decision|decide|decided|why|reason|rationale|pivot|chose|chosen|"
    r"trade-?off)\b", re.I)
SUMMARY_RE = re.compile(
    r"\b(miss|missed|happened|summary|summar\w*|recap|catch me up|"
    r"what'?s new|today|yesterday)\b", re.I)


def _ask_mode(ask_low: str) -> str:
    if DONE_RE.search(ask_low) and not RESPOND_RE.search(ask_low):
        return "done"
    # A raw "respond"/"reply to" request is reply mode even when the model's
    # outline also mentions pending work.
    if RESPOND_RE.search(ask_low):
        return "reply"
    if re.search(r"\b(pending|open items?|waiting on|anything on me|on me|"
                 r"overdue|to-?do|remind|reminder|follow up on|chase|who owes|nudge)\b",
                 ask_low):
        return "open_items"
    if DECISION_RE.search(ask_low):
        return "decision"
    if re.search(r"\b(who|when|where|how much|what time|what'?s the plan|"
                 r"explain|link|address|amount|number)\b", ask_low):
        return "lookup"
    return "catch_up"


def _resolve_mode(mode, shape):
    """Model answer_shape may refine the mode, but never overrides reply/done."""
    shape = (shape or "").strip()
    if shape == "done":
        return "done"
    if shape == "on_you_others" and mode not in ("done", "reply", "catch_up"):
        return "open_items"
    return mode


def looks_global(text):
    low = (text or "").lower()
    if not any(k in low for k in ("yesterday", "today", "tofay", "morning",
                                  "last night", "week", "month")):
        return is_broad_question(text)
    if any(k in low for k in ("miss", "what", "important", "ask", "update",
                              "summary", "summar", "across", "all chats",
                              "all groups", "everything", "respond",
                              "followed up", "got done", "completed")):
        return True
    for tok in re.findall(r"[a-z]+", low):
        if difflib.SequenceMatcher(None, tok, "happened").ratio() >= 0.8 or \
           difflib.SequenceMatcher(None, tok, "happening").ratio() >= 0.8:
            return True
    return False


def _person_thread_name(text):
    """Return a person name for 'all threads with X' / 'pending with X'.

    Case-sensitive on the captured name so ordinary words ("with respect")
    are not mistaken for people.
    """
    m = re.search(
        r"\b(?:pending|open|threads?|summary|summar\w*|all|remind|follow up)\b"
        r".{0,60}?\bwith\s+([A-Z][A-Za-z]+)",
        text or "",
    )
    return m.group(1) if m else ""


_IDENTITY_CACHE = None


def _identity_block():
    """Tell the model who the user is, so @mentions are attributed to them."""
    global _IDENTITY_CACHE
    if _IDENTITY_CACHE is not None:
        return _IDENTITY_CACHE
    names = set()
    try:
        names |= set(ledger.own_display_names() or set())
    except Exception:
        pass
    try:
        phone, lid, _src = mechat.own_identity()
        names |= {x for x in (phone, lid) if x and str(x).isdigit()}
    except Exception:
        pass
    names = sorted({str(n).strip() for n in names
                    if str(n).strip() and str(n).strip().lower() not in ("you", "me")})
    if not names:
        _IDENTITY_CACHE = ""
    else:
        _IDENTITY_CACHE = ("The user is " + ", ".join(names[:5]) +
                           ". @mentions of these names refer to the user.\n\n")
    return _IDENTITY_CACHE


_OWN_NAMES_CACHE = None


def _own_names() -> set:
    """Lowercased names/JIDs that mean the user, for on-you inference."""
    global _OWN_NAMES_CACHE
    if _OWN_NAMES_CACHE is not None:
        return _OWN_NAMES_CACHE
    names = set()
    try:
        names |= set(ledger.own_display_names() or set())
    except Exception:
        pass
    try:
        phone, lid, _src = mechat.own_identity()
        names |= {x for x in (phone, lid) if x}
    except Exception:
        pass
    try:
        names |= set(mechat.mechat_jids())
    except Exception:
        pass
    _OWN_NAMES_CACHE = {
        str(n).strip().lower() for n in names
        if str(n).strip() and str(n).strip().lower() not in ("you", "me")
    }
    return _OWN_NAMES_CACHE


def _decision_context(text, start, end, names=None, allowed=None, db_path=None,
                      facts_out=None):
    """Deciding kill/stop/pause statements, even when keyword search misses."""
    low = (text or "").lower()
    terms = [t for t in ("kill", "killed", "stop", "stopped", "pause",
                         "paused", "shut down", "no-go", "go/no-go",
                         "recommendation") if t in low]
    if not terms:
        return "", [], []
    db_path = db_path or engine.MESSAGES_DB_PATH
    clauses = " OR ".join("content LIKE ?" for _ in terms)
    params = [start.strftime("%Y-%m-%d %H:%M:%S"),
              end.strftime("%Y-%m-%d %H:%M:%S")] + [f"%{t}%" for t in terms]
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT id, chat_jid, sender, content, timestamp FROM messages "
                "WHERE timestamp >= ? AND timestamp <= ? AND (" + clauses + ") "
                "AND LENGTH(content) <= 1500 "
                "AND chat_jid NOT LIKE '%@newsletter' "
                "AND chat_jid <> 'status@broadcast' ORDER BY timestamp",
                params,
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return "", [], []
    allow = set(allowed) if allowed is not None else None
    names = names or {}
    strong = re.compile(r"recommend|decid|no-?go|go/no-?go", re.I)
    explicit = re.compile(r"\b(decided|decision|approved|we will|we are going to|"
                          r"final(ly)?|agreed)\b", re.I)
    rows = sorted(rows, key=lambda r: (
        0 if strong.search(r[3] or "") else 1, str(r[4])))
    rows_out, ids, jids = [], [], []
    fact_candidates = []
    qterms = [t for t in terms if t != "recommendation"]
    for mid, jid, sender, content, ts in rows[:60]:
        if not jid or _excluded_chat(jid) or (allow is not None and jid not in allow):
            continue
        label = _sender_label(sender, jid)
        rows_out.append((jid, f"[{_fmt_ts(ts)}] {label}: "
                              f"{(content or '')[:320]} (id {mid})"))
        ids.append(str(mid))
        if jid not in jids:
            jids.append(jid)
        low = (content or "").lower()
        hit = any(t in low for t in qterms)
        rec = bool(strong.search(content or ""))
        if hit or rec:
            what = (content or "").strip()[:220]
            if not explicit.search(content or ""):
                what = "Recommendation, not a confirmed decision: " + what
            if "kill today" in low or ("kill" in low and "not invest" in low):
                score = 0
            elif hit and rec:
                score = 1
            elif hit:
                score = 2
            else:
                score = 3
            if len(low) > 800:
                score += 3
            fact_candidates.append((score, {
                "group": names.get(jid) or _chat_name(jid),
                "who": label,
                "when": _fmt_ts(ts),
                "what": what,
                "quote": (content or "").strip()[:220],
                "id": str(mid),
            }))
    if facts_out is not None:
        fact_candidates.sort(key=lambda fc: fc[0])
        facts_out.extend([fc[1] for fc in fact_candidates[:8]])
    return _group_context_lines(rows_out, names), ids, jids


def _person_scope(name, start, end, cap=20):
    """The person's DM/thread plus group chats with direct mentions (≤ cap)."""
    ident = set()
    named = set()
    mention = set()
    try:
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        try:
            for jid, cname in con.execute(
                "SELECT jid, name FROM chats WHERE name IS NOT NULL "
                "AND name <> '' AND lower(name) LIKE ?",
                (f"%{name.lower()}%",),
            ).fetchall():
                if jid and jid not in _excluded_chat(jid):
                    named.add(jid)
        finally:
            con.close()
    except Exception:
        pass
    try:
        con = sqlite3.connect(f"file:{engine.WHATSAPP_DB_PATH}?mode=ro", uri=True)
        try:
            for (ident_jid,) in con.execute(
                "SELECT their_jid FROM whatsmeow_contacts "
                "WHERE lower(COALESCE(full_name,'')) LIKE ? "
                "OR lower(COALESCE(push_name,'')) LIKE ?",
                (f"%{name.lower()}%", f"%{name.lower()}%"),
            ).fetchall():
                if ident_jid:
                    ident.add(ident_jid)
        finally:
            con.close()
    except Exception:
        pass
    try:
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        try:
            params = [start.strftime("%Y-%m-%d %H:%M:%S"),
                      end.strftime("%Y-%m-%d %H:%M:%S")]
            if ident:
                marks = ",".join("?" * len(ident))
                sql = ("SELECT DISTINCT chat_jid FROM messages "
                       "WHERE timestamp >= ? AND timestamp <= ? AND ("
                       f"sender IN ({marks}) OR content LIKE ?) "
                       "AND chat_jid NOT LIKE '%@newsletter' "
                       "AND chat_jid <> 'status@broadcast'")
                params += list(ident) + [f"%{name}%"]
            else:
                sql = ("SELECT DISTINCT chat_jid FROM messages "
                       "WHERE timestamp >= ? AND timestamp <= ? AND content LIKE ? "
                       "AND chat_jid NOT LIKE '%@newsletter' "
                       "AND chat_jid <> 'status@broadcast'")
                params += [f"%{name}%"]
            for (jid,) in con.execute(sql, params).fetchall():
                if jid and not _excluded_chat(jid):
                    mention.add(jid)
        finally:
            con.close()
    except Exception:
        pass
    # The person's own DM/thread: a resolved contact JID or a DM named for them.
    dm = {jid for jid in (named | ident)
          if jid and not _excluded_chat(jid) and
          (jid in ident or not jid.endswith("@g.us"))}
    # Direct mentions only in groups; other people's DMs are never included.
    groups = {jid for jid in mention
              if jid.endswith("@g.us") and jid not in dm}
    ordered = sorted(dm) + sorted(groups - dm)
    return ordered[:cap], ident


def _excluded_chat(jid):
    if not jid:
        return True
    if jid.endswith("@newsletter") or jid == "status@broadcast":
        return True
    try:
        if jid in set(mechat.mechat_jids()):
            return True
    except Exception:
        pass
    try:
        if agent_config.is_excluded_chat(jid, _chat_name(jid)):
            return True
    except Exception:
        return False
    return False


_NAME_CACHE = {}
_CONTACT_CACHE = None


def _contact_names():
    global _CONTACT_CACHE
    if _CONTACT_CACHE is None:
        try:
            _CONTACT_CACHE = watch_rules.contact_info()
        except Exception:
            _CONTACT_CACHE = {}
    return _CONTACT_CACHE


def _dm_fallback(jid):
    """A readable label for a DM whose contact name is unknown."""
    digits = re.sub(r"\D", "", str(jid or "").split("@")[0])
    return f"DM {digits[-4:]}" if len(digits) >= 4 else "DM"


def _chat_name(jid):
    if jid in _NAME_CACHE:
        return _NAME_CACHE[jid]
    name = jid
    try:
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        try:
            r = con.execute("SELECT name FROM chats WHERE jid=?", (jid,)).fetchone()
            if r and r[0]:
                name = r[0]
        finally:
            con.close()
    except Exception:
        pass
    bare = str(name).split("@")[0]
    weak = (str(name) == jid or bare.isdigit() or
            not re.search(r"[A-Za-z0-9]", str(name)))
    if jid and not jid.endswith("@g.us") and weak:
        saved, biz = _contact_names().get(jid, ("", ""))
        if not re.search(r"[A-Za-z0-9]", saved or ""):
            saved = ""
        if not re.search(r"[A-Za-z0-9]", biz or ""):
            biz = ""
        name = saved or biz or _dm_fallback(jid)
    _NAME_CACHE[jid] = name
    return name


def _sender_label(sender, jid=""):
    """Resolve a sender LID/phone to a saved name; never emit a bare digit."""
    raw = str(sender or "").strip()
    if not raw:
        return "Someone"
    chat_jid = str(jid or "")
    is_group = chat_jid.endswith("@g.us")
    raw_user = raw.split("@")[0]
    chat_user = chat_jid.split("@")[0]
    # Group messages often store the group JID itself as the sender.
    if is_group and raw_user and raw_user == chat_user:
        name = _chat_name(chat_jid)
        if (name and name != chat_jid and
                not str(name).split("@")[0].isdigit() and
                re.search(r"[A-Za-z0-9]", str(name))):
            return name
        return "Someone"
    try:
        name = engine._resolve_sender_name(raw)
    except Exception:
        name = ""
    if (name and str(name) not in ("Someone", "Unknown") and
            not re.fullmatch(r"[\d@.\-]+", str(name))):
        return name
    contacts = _contact_names()
    candidates = []
    if "@" in raw:
        candidates.append(raw)
    else:
        candidates += [raw + "@lid", raw + "@s.whatsapp.net"]
    if chat_jid:
        candidates.append(chat_jid)
    for cand in candidates:
        saved, biz = contacts.get(cand, ("", ""))
        for val in (saved, biz):
            if val and re.search(r"[A-Za-z0-9]", val):
                return val
    if raw.endswith("@g.us") or is_group:
        return "Someone"
    if re.fullmatch(r"[\d\-]+", raw):
        return _dm_fallback(raw + "@lid")
    return raw


def _group_context_lines(rows, names):
    """Group (jid, line) pairs under ### headers so coverage can count chats."""
    by_chat, order = {}, []
    for jid, line in rows:
        if jid not in by_chat:
            by_chat[jid] = []
            order.append(jid)
        by_chat[jid].append(line)
    blocks = []
    for jid in order:
        name = (names or {}).get(jid) or _chat_name(jid)
        blocks.append(f"### {name} [{jid}]\n" + "\n".join(by_chat[jid]))
    return "\n\n".join(blocks)


def _call_json(prompt, fallback, schema, purpose, trace_id, model=""):
    fault = _envload.getenv("NEMBU_FAULT", "MOSAIC_FAULT", "").strip()
    if fault:
        target, _, code = fault.partition(":")
        if target in (purpose, purpose.replace("qa-", "")):
            reason = "rate_limited" if code.strip() in ("429", "") else "network"
            return {**fallback, "_gemini_failed": True, "_reason": reason}
    try:
        data = budget.gemini_json(prompt, fallback, schema, model=model,
                                  purpose=purpose, trace_id=trace_id)
    except budget.BudgetPause:
        return {**fallback, "_gemini_failed": True, "_reason": "budget"}
    if model and data.get("_gemini_failed") and data.get("_reason") == "bad_model":
        try:
            data = budget.gemini_json(prompt, fallback, schema, purpose=purpose,
                                      trace_id=trace_id)
        except budget.BudgetPause:
            return {**fallback, "_gemini_failed": True, "_reason": "budget"}
    return data


def _call_json_retry(prompt, fallback, schema, purpose, trace_id, model="",
                     attempts=3):
    """Retry transient model failures (network/429) with a short backoff."""
    delay = float(os.getenv("QA_RETRY_DELAY", "1.0"))
    last = None
    for attempt in range(max(1, attempts)):
        last = _call_json(prompt, fallback, schema, purpose, trace_id, model)
        if not last.get("_gemini_failed"):
            return last
        if last.get("_reason") not in ("network", "rate_limited"):
            return last
        if attempt < attempts - 1 and delay > 0:
            time.sleep(delay * (attempt + 1))
    return last or {**fallback, "_gemini_failed": True, "_reason": "network"}


def _fallback_scope(text, sel, elig_ids, exclude_jid):
    """Code-only group choice when call 1 fails."""
    ah = _alias_hits(text)
    if len(ah) == 1:
        jid = next(iter(ah))
        if jid in elig_ids and jid != exclude_jid:
            return {"task": "new", "groups": [jid], "task_outline": text,
                    "follow_up_refs": [], "window": {}}, "alias"
    if sel and selection_active(sel):
        jid = sel.get("chat_jid") or ""
        if jid in elig_ids and jid != exclude_jid:
            return {"task": "new", "groups": [jid], "task_outline": text,
                    "follow_up_refs": [], "window": {}}, "selection"
    if looks_global(text) or is_global_question(text) or is_broad_question(text):
        start, end = _global_window(text)
        ranked = []
        for jid in elig_ids:
            if jid == exclude_jid:
                continue
            rows = _load(jid)
            n = 0
            for m in rows:
                try:
                    ts = datetime.datetime.fromisoformat(m["ts"])
                except (TypeError, ValueError):
                    continue
                if start <= ts <= end:
                    n += 1
            if n:
                ranked.append((n, jid))
        ranked.sort(reverse=True)
        gids = [j for _n, j in ranked[:20]]
        if not gids:
            gids = [j for j in elig_ids if j != exclude_jid][:20]
        if gids:
            return {"task": "new", "groups": gids, "task_outline": text,
                    "follow_up_refs": [],
                    "window": {"from": start.isoformat(), "to": end.isoformat()}}, "global"
    return None, ""


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
    "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _parse_deadline(text, now=None):
    """Best-effort deadline date from a short phrase; None when unparseable."""
    now = now or mstate.now_local()
    low = (text or "").strip().lower()
    if not low:
        return None
    today = now.date()
    if "today" in low or "tonight" in low or "eod" in low:
        return today
    if "tomorrow" in low:
        return today + datetime.timedelta(days=1)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", low)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,4})", low)
    if m and m.group(2)[:3] in _MONTHS:
        try:
            year = today.year if _MONTHS[m.group(2)[:3]] >= today.month else today.year + 1
            return datetime.date(year, _MONTHS[m.group(2)[:3]], int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r"\b([a-z]{3,4})\s+(\d{1,2})(?:st|nd|rd|th)?", low)
    if m and m.group(1)[:3] in _MONTHS:
        try:
            month = _MONTHS[m.group(1)[:3]]
            year = today.year if month >= today.month else today.year + 1
            return datetime.date(year, month, int(m.group(2)))
        except ValueError:
            pass
    for name, idx in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", low):
            ahead = (idx - today.weekday()) % 7
            return today + datetime.timedelta(days=ahead or 7)
    return None


def _norm_identity(text):
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _is_own_label(label):
    """True when a structured person/chat label is the user's identity."""
    value = _norm_identity(label)
    if not value:
        return False
    if value in ("you", "me", "myself"):
        return True
    first = value.split()[0]
    for own in _own_names():
        norm = _norm_identity(own)
        if not norm:
            continue
        if value == norm:
            return True
        if first == norm.split()[0] and len(first) >= 4:
            return True
    return False


def _is_own_item(owner, person):
    """Ownership: only the structured owner can put an item on the user.

    ``person`` is the requester, never the actor, so an owner-unclear item
    (owner empty) stays out of On you even when person is the user.
    """
    return bool(owner) and _is_own_label(owner)


_FIRST_PERSON_RE = re.compile(
    r"\b(i|i'll|i will|i need|i have|i am|i'm|me|my|myself)\b", re.I)
_REQUEST_RE = re.compile(
    r"\b(please|pls|kindly|asked|requested|requesting|needs?|wants?|"
    r"can you|could you|would you|you to)\b", re.I)
_USER_WORD_RE = re.compile(r"\b(you|your|yourself)\b", re.I)
_ACTOR_RE = re.compile(
    r"\b([A-Z][a-zA-Z]{1,30})(?:'s)?\s+"
    r"(?:will|to|should|must|needs? to|has to|is going to)\b|"
    r"\b([A-Z][a-zA-Z]{1,30})'s list\b")
_RECIPIENT_RE = re.compile(
    r"\b(?:asked|requested|told|pinged|messaged|to|for)\s+"
    r"([A-Z][a-z][a-zA-Z]{0,29})\b")
_SELF_TASK_RE = re.compile(
    r"\b(i need|i have|i must|i should|i will|i'll|my|me)\b", re.I)


def _name_in_quote(name, quote):
    """True when the name (or an AK-style abbreviation) is in the quote."""
    if not name or not quote:
        return False
    norm = _norm_identity(name)
    text = _norm_identity(quote)
    if not norm or not text:
        return False
    if norm in text or norm.split()[0] in text:
        return True
    for tok in re.findall(r"\b[A-Z]{2,6}\b", quote):
        if norm.startswith(tok.lower()):
            return True
    return False


def _own_display_name():
    for name in _own_names():
        if "@" in name or re.fullmatch(r"[\d\-]+", name):
            continue
        if re.search(r"[a-z]", name):
            return name.title()
    return "you"


def _other_actor_in_text(text, own):
    for match in _ACTOR_RE.finditer(text or ""):
        name = match.group(1) or match.group(2) or ""
        if name and not _is_own_label(name):
            return name
    return ""


def _recipient_actor(next_action, quote, own):
    """The named recipient of a request the user made, if any."""
    na = (next_action or "").strip()
    if na:
        match = re.match(r"\s*([A-Z][a-z][a-zA-Z]{0,29})\b", na)
        if match and not _is_own_label(match.group(1)):
            return match.group(1)
    for match in _RECIPIENT_RE.finditer(quote or ""):
        name = match.group(1)
        if not _is_own_label(name):
            return name
    return ""


def _resolve_owner(owner, person, owner_quote, what, next_action=""):
    """Trust owner only when the evidence proves the actor; never default."""
    owner = (owner or "").strip()
    person = (person or "").strip()
    quote = (owner_quote or "").strip()
    if not quote:
        return ""
    names_owner = bool(owner) and _name_in_quote(owner, quote)
    names_person = bool(person) and _name_in_quote(person, quote)
    person_is_user = bool(person) and _is_own_label(person)
    owner_is_user = bool(owner) and _is_own_label(owner)
    request = bool(_REQUEST_RE.search(quote))
    first_person = bool(_FIRST_PERSON_RE.search(quote))
    user_word = bool(_USER_WORD_RE.search(quote))
    other = _other_actor_in_text(f"{what} {quote}", _own_names())
    quote_names_user = any(
        _name_in_quote(name, quote)
        for name in _own_names()
        if "@" not in name and not re.fullmatch(r"[\d\-]+", name)
    )
    # A named third party is the actor; the user never wins over it.
    if other:
        if names_owner:
            return owner
        if names_person and not person_is_user:
            return person
        return other
    # The user as requester: the actor is the recipient, not the user.
    if person_is_user:
        if names_owner:
            return owner
        recipient = _recipient_actor(next_action, quote, _own_names())
        if recipient:
            return recipient
        if owner and not owner_is_user:
            return owner
        if _SELF_TASK_RE.search(quote) and not user_word:
            return _own_display_name()
        return ""
    # Direct requests are addressed to the recipient (the user).
    if request and not names_owner:
        return person if person_is_user else _own_display_name()
    if names_owner:
        return owner
    if quote_names_user:
        return _own_display_name()
    if user_word or first_person:
        return owner if owner_is_user else _own_display_name()
    if names_person and not person_is_user:
        return person
    if person and not person_is_user:
        return person
    return ""


def _norm_open_item(raw, now):
    d = dict(raw) if isinstance(raw, dict) else {"what": str(raw)}
    what = (d.get("what") or d.get("next_action") or "").strip()
    deadline_text = (d.get("deadline") or "").strip()
    deadline = _parse_deadline(deadline_text, now)
    overdue = ((now.date() - deadline).days
               if deadline and deadline < now.date() else 0)
    age = d.get("age_days")
    if age is None:
        since = _parse_deadline(d.get("since") or "", now)
        age = (now.date() - since).days if since else 0
    try:
        age = max(0, int(age))
    except (TypeError, ValueError):
        age = 0
    chat = (d.get("chat") or "").strip()
    person = (d.get("person") or "").strip()
    owner_quote = (d.get("owner_quote") or "").strip()
    # The quote is the primary proof; fall back to the item text only when
    # the extractor omitted it (both are copied from the source message).
    owner = _resolve_owner(d.get("owner") or "", person,
                           owner_quote or what, what,
                           (d.get("next_action") or "").strip())
    # Ownership is code-owned: only a proven owner puts an item On you.
    # person is the requester, never the actor.
    on_you = _is_own_item(owner, person)
    return {
        "what": what,
        "chat": chat,
        "person": person,
        "owner": owner,
        "owner_quote": owner_quote,
        "owner_unclear": not bool(owner),
        "deadline": deadline_text,
        "deadline_date": deadline,
        "overdue_days": overdue,
        "age_days": age,
        "next_action": (d.get("next_action") or "").strip(),
        "priority": (d.get("priority") or "medium").strip().lower(),
        "on_you": on_you,
    }


_OPEN_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "about",
    "onto", "over", "your", "their", "them", "they", "will", "would",
    "should", "could", "have", "has", "had", "been", "being", "was", "were",
    "are", "is", "a", "an", "to", "of", "in", "on", "at", "by", "or", "as",
    "it", "its", "we", "you", "me", "my", "our", "us", "he", "she", "his",
    "her", "not", "no", "do", "does", "did", "done", "please", "need",
    "needs", "provide", "review", "share", "move", "execute", "follow",
    "send", "make", "get", "update", "check", "close", "confirm", "discuss",
    "route", "re-route",
}


_GENERIC_OPEN_TOKENS = {
    "feedback", "review", "check", "verify", "update", "follow", "followup",
    "request", "draft", "document", "doc", "docs", "merchant", "mbr",
    "approval", "approvals", "budget", "meeting", "call", "email", "message",
    "task", "item", "plan", "report", "today", "tomorrow", "week", "month",
    "deadline", "status", "details", "info",
}

_VERB_SYNONYMS = {
    "check": "review", "review": "review", "verify": "review",
    "confirm": "review", "look": "review", "follow": "review",
    "move": "move", "re-route": "move", "reroute": "move",
    "redirect": "move", "transfer": "move", "route": "move",
    "provide": "share", "share": "share", "send": "share", "give": "share",
    "execute": "execute", "run": "execute",
    "update": "update", "close": "close", "approve": "approve",
    "draft": "draft", "plan": "plan", "book": "book", "ship": "ship",
}


def _open_verb(text):
    for tok in re.findall(r"[a-z]+", (text or "").lower()):
        if tok in _VERB_SYNONYMS:
            return _VERB_SYNONYMS[tok]
    return ""


def _open_tokens(text):
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(t) >= 3 and t not in _OPEN_STOP}


def _same_name(a, b):
    return bool(a and b and (a == b or a.startswith(b) or b.startswith(a)))


def _same_actor(a, b):
    """Dedupe identity: owner first (the actor), then person as a fallback."""
    oa = (a.get("owner") or "").strip().lower()
    ob = (b.get("owner") or "").strip().lower()
    pa = (a.get("person") or "").strip().lower()
    pb = (b.get("person") or "").strip().lower()
    if oa and ob:
        return _same_name(oa, ob)
    if oa and pb:
        return _same_name(oa, pb)
    if ob and pa:
        return _same_name(ob, pa)
    if pa and pb:
        return _same_name(pa, pb)
    return True


def _same_open_item(a, b):
    """True when two open items are the same task, even paraphrased."""
    if not _same_actor(a, b):
        return False
    ta, tb = _open_tokens(a.get("what")), _open_tokens(b.get("what"))
    if not ta or not tb:
        return False
    shared = ta & tb
    if len(shared) >= 2:
        return True
    # One distinctive shared noun is enough, unless both items name a
    # different action verb (e.g. "check Zephyr" vs "move Zephyr budget").
    if not (shared - _GENERIC_OPEN_TOKENS):
        return False
    va = _open_verb(a.get("what"))
    vb = _open_verb(b.get("what"))
    return not (va and vb and va != vb)


def _open_items(data2, now=None):
    now = now or mstate.now_local()
    items = []

    def add(raw):
        it = _norm_open_item(raw, now)
        if not it.get("what"):
            return
        structured = isinstance(raw, dict)
        for existing in items:
            if _same_open_item(existing, it):
                # Prefer structured fields over a plain-string duplicate.
                if structured and not existing.get("_structured"):
                    existing.update(it)
                    existing["_structured"] = True
                return
        it["_structured"] = structured
        items.append(it)

    for r in data2.get("open_items") or []:
        add(r if isinstance(r, dict) else {"what": str(r)})
    for x in data2.get("on_you") or []:
        add(x if isinstance(x, dict) else {"what": str(x), "on_you": True})
    for x in data2.get("others") or []:
        add(x if isinstance(x, dict) else {"what": str(x), "on_you": False})
    return items


def _open_item_sort_key(it):
    return (
        0 if it["overdue_days"] else 1,
        _PRIORITY_ORDER.get(it["priority"], 1),
        it["deadline_date"] or datetime.date.max,
        -it["age_days"],
    )


def _open_item_line(it):
    label = (it.get("owner") or it.get("person") or it.get("chat")
             or "owner unclear")
    what = it.get("what") or it.get("next_action") or ""
    line = f"- *{label}* — {what}" if label else f"- {what}"
    if it["overdue_days"]:
        line += f" *OVERDUE {it['overdue_days']}d*"
    elif it.get("deadline"):
        line += f" ({it['deadline']})"
    if it.get("next_action") and it["next_action"] != what:
        line += f" → {it['next_action']}"
    if it.get("owner_unclear") and it.get("person"):
        line += " (owner unclear)"
    return line


def _open_item_pointer(it):
    label = (it.get("owner") or it.get("person") or it.get("chat")
             or "owner unclear")
    suffix = " (owner unclear)" if it.get("owner_unclear") and it.get("person") else ""
    return (f"- *{label}* — see Do first{suffix}" if label
            else f"- see Do first{suffix}")


_DIRECT_REQUEST_RE = re.compile(
    r"\b(please|pls|kindly|can you|could you|would you)\b", re.I)
_ASKED_RE = re.compile(
    r"\b(asked|requested|requesting|told|pinged|messaged)\b", re.I)
_SECOND_PERSON_ASK_RE = re.compile(
    r"\b(you|your)\b[^.!?]{0,60}\b(need|must|should|can|could|would|send|"
    r"share|check|confirm|submit|review|update|schedule|call|close|resolve|"
    r"follow|provide|approve|sign|pay|due)\b", re.I | re.S)


def _recall_direct_asks(gids, start, end, names=None):
    """Promote source messages that are direct asks to the user.

    Code-owned recall net for the flagship pending answer: the model
    sometimes omits genuine on-you asks. Only messages from others that
    contain an explicit request pattern are promoted; nothing is invented.
    """
    jids = [j for j in (gids or []) if j]
    if not jids:
        return []
    names = names or {}
    ph = ",".join("?" * len(jids))
    try:
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT id, chat_jid, sender, content, timestamp FROM messages "
                "WHERE chat_jid IN (" + ph + ") "
                "AND timestamp >= ? AND timestamp <= ? AND is_from_me=0 "
                "AND content IS NOT NULL AND TRIM(content) != '' "
                "ORDER BY timestamp",
                (*jids, start.strftime("%Y-%m-%d %H:%M:%S"),
                 end.strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    own_names = [n for n in _own_names()
                 if "@" not in n and not re.fullmatch(r"[\d\-]+", n)]
    items = []
    for _mid, jid, sender, content, _ts in rows:
        text = " ".join((content or "").split())
        if len(text) < 8:
            continue
        low = text.lower()
        request_cue = bool(_DIRECT_REQUEST_RE.search(low))
        asked_cue = bool(_ASKED_RE.search(low))
        first_person = bool(_FIRST_PERSON_RE.search(low))
        user_mention = any(
            re.search(rf"\b{re.escape(n)}\b", low) for n in own_names
        ) or bool(re.search(r"\byou\b", low))
        user_to = any(
            re.search(rf"\b{re.escape(n)}\b\s+to\b", low) for n in own_names
        )
        second_person = bool(_SECOND_PERSON_ASK_RE.search(low))
        direct = (request_cue or (asked_cue and not first_person)
                  or user_to or second_person or (asked_cue and user_mention))
        if not direct:
            continue
        items.append({
            "chat": names.get(jid) or _chat_name(jid),
            "person": _sender_label(sender, jid),
            "owner": _own_display_name(),
            "owner_quote": text[:240],
            "what": text[:240],
            "on_you": True,
            "priority": "medium",
        })
    return items


def _open_items_body(data2, now=None):
    items = _open_items(data2, now)
    do_first = sorted(items, key=_open_item_sort_key)[:3]
    first_ids = {id(i) for i in do_first}
    on = [i for i in items if i["on_you"]]
    oth = [i for i in items if not i["on_you"]]

    def render(items_in_section):
        return [_open_item_pointer(i) if id(i) in first_ids
                else _open_item_line(i) for i in items_in_section]

    lines = []
    if do_first:
        lines.append("*Do first*")
        lines += [_open_item_line(i) for i in do_first]
        lines.append("")
    lines.append("*On you*")
    lines += render(on) if on else ["Nothing waiting on you."]
    lines.append("")
    lines.append("*Others*")
    lines += render(oth) if oth else ["- Nothing else open."]
    return "\n".join(lines)


def _apply_decision_facts(body, facts):
    """Guarantee a retrieved kill/recommendation is quoted and cited."""
    if not facts:
        return body
    top = facts[0]
    quote = " ".join((top.get("quote") or "").split())
    probe = "kill today" if "kill today" in quote.lower() else quote[:50]
    if probe and probe.lower() not in (body or "").lower():
        body = (body or "").rstrip() + (
            f'\n- *Recommendation, not a confirmed decision:* "{quote[:180]}"'
            f' ({top.get("group") or "chat"} · {top.get("when") or ""})')
    ids = [f.get("id") for f in facts if f.get("id")]
    missing = [i for i in ids if i not in (body or "")]
    if missing:
        body = (body or "").rstrip() + "\n_Source: " + " · ".join(missing[:3]) + "_"
    return body


def _fallback_answer_body(fmt_mode, data2):
    """Render facts in code when the writer call fails."""
    if fmt_mode == "open_items":
        return _open_items_body(data2)
    if fmt_mode == "done":
        lines = []
        for d in data2.get("done") or []:
            if isinstance(d, dict):
                group = d.get("group") or ""
                when = d.get("when") or ""
                what = d.get("what") or ""
                meta = " · ".join(str(x) for x in (group, when) if x)
                if what:
                    lines.append(f"- {meta}: {what}" if meta else f"- {what}")
            elif d:
                lines.append(f"- {d}")
        return "\n".join(lines) or "- No completed items found in this window."
    lines = []
    for f in data2.get("facts") or []:
        if isinstance(f, dict):
            who = f.get("who") or f.get("sender") or f.get("from") or ""
            when = f.get("when") or f.get("time") or ""
            what = f.get("what") or f.get("text") or f.get("fact") or ""
            if not what:
                what = ", ".join(str(v) for v in f.values() if v)
            meta = ", ".join(str(x) for x in (who, when) if x)
            lines.append(f"- {meta}: {what}" if meta else f"- {what}")
        else:
            lines.append(f"- {f}")
    return "\n".join(lines)


def _basic_cards():
    """Lightweight chat list so Q&A works before profiles/cards finish
    building after a fresh sync. Includes direct messages."""
    out = []
    try:
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT jid, name FROM chats "
                "WHERE jid NOT LIKE '%@newsletter' AND jid <> 'status@broadcast'"
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    for jid, name in rows:
        if jid and not _excluded_chat(jid):
            out.append({"jid": jid, "name": (name or jid)[:80],
                        "objective": "", "themes": "", "latest": ""})
    return out


def _name_hits(text, cards) -> set:
    norm = qa_profiles.normalize(text)
    hits = set()
    for c in cards:
        n = qa_profiles.normalize(c.get("name") or "")
        if n and len(n) >= 4 and n in norm:
            hits.add(c["jid"])
            continue
        toks = [t for t in n.split() if len(t) >= 4]
        if len(toks) >= 2 and all(t in norm for t in toks):
            hits.add(c["jid"])
    return hits


def _active_jids(start, limit: int = 60) -> list:
    cutoff = start.strftime("%Y-%m-%d %H:%M:%S")
    try:
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT chat_jid, COUNT(*) FROM messages "
                "WHERE timestamp >= ? "
                "GROUP BY chat_jid ORDER BY COUNT(*) DESC LIMIT ?",
                (cutoff, limit * 2),
            ).fetchall()
        finally:
            con.close()
        return [r[0] for r in rows if r[0] and not _excluded_chat(r[0])][:limit]
    except Exception:
        return []


def _shortlist_cards(text, cards, sel):
    """Show the model only the groups that can plausibly matter, instead of
    all 300+ cards. This keeps call 1 accurate and bounded."""
    by_jid = {c["jid"]: c for c in cards}
    keep = set()
    keep |= _name_hits(text, cards)
    keep |= set(_alias_hits(text).keys())
    if sel and selection_active(sel):
        keep.add(sel.get("chat_jid") or "")
    start, _end = _global_window(text)
    keep |= set(_active_jids(start, 60))
    try:
        hits = qa_search.search(engine.MESSAGES_DB_PATH, mstate.ENGINE_DB_PATH,
                                text, start, _end, limit=15)
        keep |= {h["chat_jid"] for h in hits if h.get("chat_jid")}
    except Exception:
        pass
    keep.discard("")
    if not keep:
        keep = set(list(by_jid.keys())[:60])
    out = [c for jid, c in by_jid.items() if jid in keep]
    return out[:60] or cards[:60]


def _flow_answer(text, forced_group="", exclude_jid="", trace_id=""):
    """The 3-call Me-chat flow. Returns a router action dict."""
    cards = group_cards()
    if not cards:
        cards = _basic_cards()
        mstate.event(trace_id, "qa.cards", "info", f"using {len(cards)} basic cards")
    if not cards:
        return {"kind": "failed", "reason": "no_chats"}
    names = {c["jid"]: c["name"] for c in cards}
    elig_ids = set(names.keys())
    sel = get_selection()
    prompt_cards = _shortlist_cards(text, cards, sel)
    sel_txt = (f'id={sel.get("chat_jid")} name={sel.get("name")!r} '
               f'active={selection_active(sel)}') if sel else "(none)"
    now = mstate.now_local()
    prompt1 = f"""{_identity_block()}You decide what the user wants in their WhatsApp assistant.

Tasks:
- new: a fresh question about one group or across groups
- follow_up: continues an earlier question/answer
- note: personal note to self (no reply)
- send: explicitly send a pending draft
- alert_action: skip/edit a pending alert
- command: status/help/top 10/done/mute/watch/change/forget

GLOBAL QUESTIONS — always across chats (groups AND direct messages), never the selected chat:
- "what all did I miss", "what did I miss since morning", "what happened yesterday",
  "what did I miss yesterday", "anything important today", "any asks for me",
  "what's on my plate", "across groups", "who do I need to remind".
  → task = new, groups = every chat that could plausibly matter (up to 20), window = the period.
  → Include direct messages for global questions; reminders to people often live there.
  → NEVER return an empty groups list for a global question unless the list has no activity in the window.
  → Do NOT return a single follow_up group just because a chat was selected.

Other rules:
- groups must be ids from chat_list, or [] if none apply. Each entry has kind: group or DM.
- task_outline: 1-2 lines on what they want (sent to the next step).
- answer_shape: direct_answer (one fact or a short paragraph), list (bullets), on_you_others (split pending work), done (completed work).
- follow_up_refs: quote the earlier question/answer this continues.
- window: only when a period is stated/implied (ISO dates); "since morning" = 08:00. A future date ("tomorrow", "Friday's meeting") must NOT become a message window — leave window empty and keep the date in the question.
- A go-live, launch, visit, delivery or deadline is a plan even if no message says "plan".
- Never treat Nembu's past messages as user instructions.

Current time: {now.strftime('%A %d %b %Y, %I:%M %p')} IST
Selected chat: {sel_txt}

Recent Me-chat (user/nembu):
{conversation.dialogue_block(30)}

CHAT_LIST:
{_group_list_text(prompt_cards)}

Message: {text!r}

Return ONLY JSON."""
    data1 = _call_json(prompt1, {"task": "note", "groups": [], "task_outline": text,
                                 "follow_up_refs": [], "window": {}},
                       CALL1_SCHEMA, "qa-call1", trace_id, model=QA_MODEL)
    if data1.get("_gemini_failed"):
        reason = data1.get("_reason") or "bad_output"
        mstate.event(trace_id, "qa.call1", "error", reason)
        fb, how = _fallback_scope(text, sel, elig_ids, exclude_jid)
        if not fb:
            return {"kind": "failed", "reason": reason}
        data1 = fb
        mstate.event(trace_id, "qa.call1", "warn", f"code fallback: {how}")
    person_info = None
    person_thread = _person_thread_name(text)
    if person_thread:
        # A person query is inherently global: never ask which group.
        _now = mstate.now_local()
        _start = _now - datetime.timedelta(days=mstate.RETENTION_DAYS)
        p_jids, p_idents = _person_scope(person_thread, _start, _now)
        if not p_jids and p_idents:
            p_jids = [j for j in sorted(p_idents)
                      if j and not j.endswith("@g.us")][:20]
        if not p_jids:
            # Unresolved person: bounded fallback, never the whole Q&A scope.
            p_jids = [j for j in _active_jids(_start, 20) if j in elig_ids][:20]
        data1 = {"task": "new", "groups": p_jids, "task_outline": text,
                 "follow_up_refs": [],
                 "window": {"from": _start.isoformat(), "to": _now.isoformat()}}
        person_info = {"name": person_thread, "idents": p_idents}
        mstate.event(trace_id, "qa.scope", "ok",
                     f"person thread: {person_thread} · {len(p_jids)} chats")
    mstate.event(trace_id, "qa.call1", "ok",
                 f"{data1.get('task')} · {len(data1.get('groups') or [])} groups")
    task = data1.get("task")
    if task == "note":
        return {"kind": "action", "action": "note_only"}
    if task in ("send", "alert_action", "command"):
        return {"kind": "action", "action": "legacy_delegate", "task": task, "text": text}
    valid = [g for g in (data1.get("groups") or []) if g in elig_ids and g != exclude_jid]
    if not valid:
        fb, how = _fallback_scope(text, sel, elig_ids, exclude_jid)
        if fb:
            data1 = fb
            valid = [g for g in (data1.get("groups") or []) if g in elig_ids and g != exclude_jid]
            mstate.event(trace_id, "qa.scope", "info", f"code fallback: {how}")
    if not valid:
        mention = (data1.get("task_outline") or text)[:80]
        return {"kind": "question", "decision": "unresolved",
                "reply": (f"◆ I couldn't place {mention!r} in your groups. Name a group, "
                          f"or swipe-reply to a message from it.")}
    outline_low = (data1.get("task_outline") or "").lower()
    time_global = looks_global(text)
    if time_global and len(valid) < 2 and not person_info:
        w_start, w_end = _window_from(data1, text)
        ranked = []
        for jid in elig_ids:
            rows = _load(jid)
            n = 0
            for m in rows:
                try:
                    ts = datetime.datetime.fromisoformat(m["ts"])
                except (TypeError, ValueError):
                    continue
                if w_start <= ts <= w_end:
                    n += 1
            if n:
                ranked.append((n, jid))
        ranked.sort(reverse=True)
        expanded = [j for _n, j in ranked[:16]]
        if expanded:
            valid = expanded
            mstate.event(trace_id, "qa.scope", "info",
                         f"global fallback: expanded to {len(valid)} groups")
    global_like = (bool(person_thread) or time_global or is_global_question(text) or
                   (len(valid) > 1 and bool(data1.get("window"))) or
                   (len(valid) > 1 and any(k in outline_low for k in
                    ("across groups", "all groups", "missed", "yesterday", "today"))))
    mstate.event(trace_id, "qa.scope", "ok",
                 f"{'global' if global_like else 'single'} · {len(valid)} groups")
    if not global_like and len(valid) == 1:
        ah = _alias_hits(text)
        if ah:
            longest = max(ah.values())
            tied = [j for j, n in ah.items() if n == longest and j in elig_ids]
            if len(tied) > 1:
                rows = [{"jid": j, "name": names[j]} for j in tied[:4]]
                why = " · ".join(f"{i}. *{r['name'][:36]}*" for i, r in enumerate(rows, 1))
                _save_choice(text, rows, "")
                return {"kind": "question", "decision": "ambiguous",
                        "reply": "◆ More than one group matches — reply with the number:\n" + why}
    if len(valid) > 1 and not global_like:
        global_like = True
        mstate.event(trace_id, "qa.scope", "info",
                     f"answering across {len(valid)} chats")

    start, end = _window_from(data1, text)
    future_ctx, future_ids, future = "", [], []
    if not person_info:
        future_ctx, future_ids, future = _future_plan_context(
            text, start, end, names=names, allowed=elig_ids)
        if future:
            for _j in future:
                if _j not in valid and _j != exclude_jid:
                    valid.append(_j)
                    names.setdefault(_j, _chat_name(_j))
            mstate.event(trace_id, "qa.scope", "info",
                         f"future plans: {len(future)} chats · {len(future_ids)} msgs")
    if global_like and not person_info:
        # Direct messages are often where reminders live; always make the
        # active ones part of a global answer instead of hoping call 1 picks them.
        _added = 0
        for _j in _active_jids(start, 20):
            if _added >= 8:
                break
            if (not _j.endswith("@g.us") and _j not in valid
                    and _j in elig_ids):
                valid.append(_j)
                names.setdefault(_j, _chat_name(_j))
                _added += 1
    ask_low = ((data1.get("task_outline") or "") + " " + text).lower()
    mode = _ask_mode(ask_low)
    if _ask_mode(text.lower()) == "catch_up" and SUMMARY_RE.search(text or ""):
        # A "what did I miss / summary" question stays a catch-up summary even
        # if the model's task outline mentions pending work.
        mode = "catch_up"
    shape = (data1.get("answer_shape") or "").strip()
    mode = _resolve_mode(mode, shape)
    if mode == "done":
        if len(valid) == 1 and not global_like:
            return _flow_done(text, start, end, forced_group=valid[0],
                              names=names, trace_id=trace_id)
        return _flow_done(text, start, end, names=names, trace_id=trace_id)
    search_ctx = ""
    search_total = search_media = 0
    stats = {"voice": 0, "media": 0}
    decision_ctx, decision_ids, decision_jids = "", [], []
    decision_facts = []
    if mode == "decision":
        decision_ctx, decision_ids, decision_jids = _decision_context(
            text, start, end, names=names, allowed=elig_ids,
            facts_out=decision_facts)
        for _j in decision_jids:
            if _j not in valid and _j != exclude_jid:
                valid.append(_j)
                names.setdefault(_j, _chat_name(_j))
    if mode in ("lookup", "decision"):
        allowed = set(valid) if person_info else elig_ids
        search_ctx, hit_jids, search_total, search_media = _search_context(
            text, start, end, names, allowed=allowed, stats=stats)
        if hit_jids:
            valid = list(dict.fromkeys(list(valid) + sorted(hit_jids)))
            for _j in hit_jids:
                names.setdefault(_j, _chat_name(_j))
            mstate.event(trace_id, "qa.search", "ok",
                         f"{len(hit_jids)} chats · {search_total} msgs")
    if decision_ctx:
        search_ctx = (decision_ctx + ("\n\n" + search_ctx if search_ctx else ""))
        search_total += len([ln for ln in decision_ctx.splitlines() if ln.strip()])
        mstate.event(trace_id, "qa.search", "info",
                     f"decision context: {len(decision_ids)} msgs")
    if future_ctx and mode == "lookup":
        # The future-plan scan must survive the search-context swap.
        search_ctx = (future_ctx + ("\n\n" + search_ctx if search_ctx else ""))
        search_total += len([ln for ln in future_ctx.splitlines() if ln.strip()])
        if future_ids:
            mstate.event(trace_id, "qa.search", "info",
                         f"future context: {len(future_ids)} msgs")
    per_group = 120 if len(valid) > 1 else 300
    if search_ctx:
        context, total, media_count = search_ctx, search_total, search_media
    else:
        context, total, media_count = _flow_messages(
            valid, start, end=end, limit=per_group, names=names,
            person=person_info, stats=stats)
    truncated = len(context) >= QA_CONTEXT_CHARS
    coverage_chats = _chats_read(context)
    if mode == "decision" and decision_jids:
        coverage_chats = len(set(decision_jids) | set(hit_jids))
    if not context:
        return {"kind": "question", "decision": "resolved",
                "jid": valid[0], "name": names.get(valid[0], valid[0]),
                "question": data1.get("task_outline") or text,
                "expert": "catch_up",
                "reply": f"*Nembu* 🍋 · *{names.get(valid[0], valid[0])}* · {end.strftime('%a %d %b')}\n- Nothing stored in that window.",
                "turn_key": ""}
    fmt_mode = ("open_items" if mode == "open_items"
                else "lookup" if mode == "lookup"
                else "decision" if mode == "decision"
                else "done" if mode == "done" else "catch_up")
    if mode in ("catch_up", "lookup", "reply") and \
            _envload.getenv("NEMBU_QA_MERGE", "MOSAIC_QA_MERGE", "1") != "0":
        merged = _call_json(
            _merged_prompt(text, data1.get("task_outline") or text, names,
                           valid, context, mode),
            {"answer": "", "citations": []}, MERGED_SCHEMA, "qa-merged",
            trace_id, model=QA_MODEL)
        if not merged.get("_gemini_failed") and (merged.get("answer") or "").strip():
            raw = (merged.get("answer") or "").strip()
            raw = re.sub(r"\s+-\s+(?=\*)", "\n- ", raw)
            if len(valid) == 1:
                raw = _strip_single_group_prefix(raw, names[valid[0]])
            if mode == "reply":
                raw = "\n".join(
                    ln for ln in raw.splitlines()
                    if not re.search(
                        r"no (items?|messages?) (require|directed|need)|"
                        r"nothing (waiting|else|directed|requires|needs)",
                        ln, re.I)
                ).strip() or "Nothing needs a reply from you right now."
            body = format_body(fmt_mode, raw)
            if body:
                cov = {"messages": total, "media_unsearchable": media_count,
                       "cut": truncated}
                header = (f"*Nembu* 🍋 · *{names[valid[0]]}* · {end.strftime('%a %d %b')}"
                          if len(valid) == 1 else
                          f"*Nembu* 🍋 · {len(valid)} chats · {_header_range(start, end)}")
                footer = _coverage_footer(
                    total, _chats_read(context), len(valid),
                    media=stats.get("media", 0), voice=stats.get("voice", 0),
                    cut=truncated)
                full = header + "\n" + body + footer
                reply, turn_key = _chunk_and_store(
                    full, valid[0] if len(valid) == 1 else "__multi__")
                _drop_choice()
                if len(valid) == 1:
                    set_selection(valid[0], names[valid[0]], text, body)
                mstate.event(trace_id, "qa.merged", "ok", f"{len(body)} chars")
                return {"kind": "question", "decision": "resolved",
                        "jid": valid[0], "name": names[valid[0]],
                        "expert": fmt_mode, "question": text, "reply": reply,
                        "turn_key": turn_key, "coverage": cov,
                        "global": len(valid) > 1}
        mstate.event(trace_id, "qa.merged", "warn",
                     merged.get("_reason") or "fallback")
    prompt2 = f"""{_identity_block()}Extract the material facts from these WhatsApp messages for the user.

Task: {data1.get('task_outline') or text}
Question (exact): {text}
Window: {start.strftime('%a %d %b %H:%M')} → {end.strftime('%H:%M')}
Follow-up threads to carry: {json.dumps(data1.get('follow_up_refs') or [], ensure_ascii=False)}
Groups: {', '.join(names[g] for g in valid)}

Rules:
- facts: only what the messages say; group, who, when, what, exact deciding quote.
- on_you: asks/mentions addressed to the user (by name, @mention, reply, "you", their team, or a role they own) still unanswered; approvals/decisions waiting on the user; deadlines the user owns. Include indirect asks where a named owner is clearly the user or their side.
- others: open asks between others, things the user asked that nobody answered, undecided decisions, upcoming deadlines.
- open_items: for pending/overdue questions, one entry per open item with chat, person (who raised/asked), owner (who must act), owner_quote (the exact short phrase from the message that proves the actor), what, deadline (as written), since (when it was raised), next_action, priority (high/medium/low), on_you (true only when owner is the user or the assistant DM).
- Return one open_items entry for EVERY unresolved ask, deadline, approval or decision; do not collapse them. person is who asked/raised; owner is the actor; owner_quote must be copied from the message, never inferred. Examples: "Raghav: Will share an update on campaigns and C2" -> person=Raghav, owner=Raghav, owner_quote="Will share an update"; "AK to follow up and close by 6th" -> person=Nisha, owner=Nisha, owner_quote="AK to follow up"; "Maya asked Sam to submit feedback" -> person=Maya, owner=Sam, owner_quote="asked Sam to submit feedback"; "Vihaan: please check Zephyr approvals" -> person=Vihaan, owner=Sam, owner_quote="please check Zephyr approvals"; "John's list ... inspect the T Nagar door" -> person=John, owner=John, owner_quote="John's list". If owner/owner_quote are unknown, leave them empty; code falls back to person, then Others.
- decision/why: include the decision, the reason, and when it was made; keep the deciding quote. Kill/stop/pause/go/no-go statements are decisions; if only a recommendation exists, label it "recommendation, not a confirmed decision".
- done: things the USER themselves completed, shipped, closed, delivered or followed up on in the window (look for messages they sent and their outcomes). One entry per accomplishment: group, who, when, what, quote. Empty if none.
- conflicts: conflicting values, with both.
- coverage: messages read, media not searchable, anything cut.
- Most recent messages matter most.

Messages:
{context[:QA_CONTEXT_CHARS]}

Return ONLY JSON."""
    data2 = _call_json_retry(prompt2, {"facts": [], "on_you": [], "others": [], "done": [],
                                       "conflicts": [],
                                       "coverage": {"messages": total,
                                                    "media_unsearchable": media_count,
                                                    "cut": len(context) >= QA_CONTEXT_CHARS}},
                             CALL2_SCHEMA, "qa-call2", trace_id, model=QA_MODEL)
    if data2.get("_gemini_failed"):
        reason = data2.get("_reason") or "bad_output"
        if mode == "decision" and decision_facts:
            mstate.event(trace_id, "qa.call2", "warn",
                         f"{reason}; using {len(decision_facts)} retrieved decision facts")
            data2 = {"facts": list(decision_facts), "on_you": [], "others": [],
                     "done": [], "conflicts": [],
                     "coverage": {"messages": total,
                                  "media_unsearchable": media_count,
                                  "cut": len(context) >= QA_CONTEXT_CHARS}}
        else:
            mstate.event(trace_id, "qa.call2", "error", reason)
            return {"kind": "failed", "reason": reason}
    if mode == "decision" and decision_facts:
        existing = [f for f in (data2.get("facts") or []) if isinstance(f, dict)]
        seen = {str(f.get("quote") or "").strip().lower() for f in existing}
        for fact in decision_facts:
            key = str(fact.get("quote") or "").strip().lower()
            if key and key not in seen:
                existing.append(fact)
                seen.add(key)
        data2["facts"] = existing
    mstate.event(trace_id, "qa.call2", "ok",
                 f"{len(data2.get('facts') or [])} facts · {total} msgs")
    fmt_mode = ("open_items" if mode == "open_items"
                else "lookup" if mode == "lookup"
                else "decision" if mode == "decision"
                else "done" if mode == "done"
                else "catch_up")
    if fmt_mode == "open_items":
        recall = _recall_direct_asks(valid, start, end, names)
        if recall:
            data2["open_items"] = list(data2.get("open_items") or []) + recall
            mstate.event(trace_id, "qa.recall", "info",
                         f"{len(recall)} direct asks")
        body = _open_items_body(data2)
        header = (f"*Nembu* 🍋 · *{names[valid[0]]}* · {end.strftime('%a %d %b')}"
                  if len(valid) == 1 else
                  f"*Nembu* 🍋 · {len(valid)} chats · {_header_range(start, end)}")
        cov = dict(data2.get("coverage") or {})
        cov["messages"] = total
        cov["media_unsearchable"] = media_count
        cov["chats_read"] = _chats_read(context)
        cov["chats_in_scope"] = len(valid)
        cov["cut"] = truncated or bool(cov.get("cut"))
        footer = _coverage_footer(
            total, _chats_read(context), len(valid),
            media=stats.get("media", 0), voice=stats.get("voice", 0),
            cut=cov["cut"])
        full = header + "\n" + body + footer
        reply, turn_key = _chunk_and_store(full, valid[0] if len(valid) == 1 else "__multi__")
        _drop_choice()
        if len(valid) == 1:
            set_selection(valid[0], names[valid[0]], data1.get("task_outline") or text, body)
        return {"kind": "question", "decision": "resolved", "jid": valid[0],
                "name": names[valid[0]], "expert": "open_items",
                "question": data1.get("task_outline") or text, "reply": reply,
                "turn_key": turn_key, "coverage": cov, "global": len(valid) > 1}

    persona = agent_config.persona_block()
    reply_hint = (
        '- If the ask is "what should I respond to", output one bullet per item '
        'needing a reply, ordered by urgency: group, person, when, what. Do not '
        'use On you/Others sections.\n' if mode == "reply" else "")
    summary_hint = (
        '- If the ask is what the user missed, give a short catch-up summary '
        'grouped by chat; do not use On you/Others sections.\n'
        if mode == "catch_up" else "")
    prompt3 = f"""{persona}Write the WhatsApp answer for the user.

Exact message: {text}
What they asked: {data1.get('task_outline') or text}
Groups: {', '.join(names[g] for g in valid)}

Facts:
{json.dumps({'facts': data2.get('facts'), 'done': data2.get('done'),
             'on_you': data2.get('on_you'), 'others': data2.get('others'),
             'conflicts': data2.get('conflicts')}, ensure_ascii=False)[:20000]}

Rules:
- Use only these facts. If the answer isn't there, say so and mention the nearest thing.
- Attribute facts ("Raghav, Mon 10:07"). Quote only the deciding phrase.
- Keep amounts and dates exactly.
- A go-live, launch, visit, delivery or deadline counts as a plan even if the word "plan" is absent.
- Do NOT add a title or header line. Start directly with the bullets.
- One bullet per line, each starting with "- ".
- If more than one group, prefix every bullet with "- *<Group name>*: ".
- WhatsApp formatting only: *bold*, "- " bullets, no **, no #, no tables.
- At most 14 bullets.
- If answer_shape is direct_answer, write one short paragraph (no bullets) and then one source line.
- If the ask is about pending/open items, use *On you* and *Others* sections. If nothing is on the user, write exactly "Nothing waiting on you." then a one-line count of the rest.
- If the ask is a lookup, give one direct answer plus a source line instead of a list.
- If the ask is a decision/why question, answer the decision and the reason in a short paragraph plus one source line; treat kill/stop/pause/go/no-go as facts, and label a lone recommendation as "recommendation, not a confirmed decision".
- If the ask is what the user got done, list only the completed items from `done`: group, what, when. No unrelated chatter, no filler. If `done` is empty, say "No completed items found in this window."
{reply_hint}{summary_hint}
Return ONLY JSON: {{"answer": "..."}}"""
    data3 = _call_json(prompt3, {"answer": ""}, CALL3_SCHEMA, "qa-call3", trace_id, model=QA_MODEL)
    timed_out = False
    if data3.get("_gemini_failed"):
        reason = data3.get("_reason") or "bad_output"
        mstate.event(trace_id, "qa.call3", "error", reason)
        body = _fallback_answer_body(fmt_mode, data2)
        timed_out = True
    else:
        mstate.event(trace_id, "qa.call3", "ok", f"{len(data3.get('answer') or '')} chars")
        raw = (data3.get("answer") or "").strip()
        raw = re.sub(r"\s+-\s+(?=\*)", "\n- ", raw)
        if len(valid) == 1:
            raw = _strip_single_group_prefix(raw, names[valid[0]])
        reply_mode = (mode == "reply")
        if reply_mode:
            raw = "\n".join(
                ln for ln in raw.splitlines()
                if not re.search(r"no (items?|messages?) (require|directed|need)|nothing (waiting|else|directed|requires|needs)",
                                 ln, re.I)
            ).strip()
            if not raw:
                raw = "Nothing needs a reply from you right now."
        body = format_body(fmt_mode, raw)
    if mode == "decision":
        body = _apply_decision_facts(body, decision_facts)
    if not body:
        return {"kind": "failed", "reason": data3.get("_reason") or "bad_output"}
    if len(valid) == 1:
        header = f"*Nembu* 🍋 · *{names[valid[0]]}* · {end.strftime('%a %d %b')}"
    else:
        header = f"*Nembu* 🍋 · {len(valid)} chats · {_header_range(start, end)}"
    cov = dict(data2.get("coverage") or {})
    cov["messages"] = total
    cov["media_unsearchable"] = media_count
    cov["chats_read"] = coverage_chats
    cov["chats_in_scope"] = len(valid)
    cov["cut"] = truncated or bool(cov.get("cut"))
    footer = _coverage_footer(
        total, coverage_chats, len(valid),
        media=stats.get("media", 0), voice=stats.get("voice", 0),
        cut=cov["cut"], timed_out=timed_out)
    full = header + "\n" + body + footer
    reply, turn_key = _chunk_and_store(full, valid[0] if len(valid) == 1 else "__multi__")
    _drop_choice()
    if len(valid) == 1:
        set_selection(valid[0], names[valid[0]], data1.get("task_outline") or text, body)
    return {"kind": "question", "decision": "resolved", "jid": valid[0],
            "name": names[valid[0]], "expert": "catch_up", "question": data1.get("task_outline") or text,
            "reply": reply, "turn_key": turn_key, "coverage": cov,
            "global": len(valid) > 1}


def answer(text: str, forced_group: str = "", exclude_jid: str = "", trace_id: str = ""):
    """3-call Me-chat flow (call1 tool/group, call2 facts, call3 words)."""
    text = (text or "").strip()
    if not text:
        return {"kind": "not_question"}
    mstate.event(trace_id, "qa.start", "info", text[:80])
    if not forced_group and _ask_mode(text.lower()) == "done":
        # Done is code-scoped; call 1 would only add latency.
        start, end = _window_from({"task_outline": text}, text)
        return _flow_done(text, start, end, trace_id=trace_id)
    if forced_group and _ask_mode(text.lower()) == "done":
        start, end = _window_from({"task_outline": text}, text)
        name = (qa_profiles.get_profile(forced_group) or {}).get("name") or forced_group
        return _flow_done(text, start, end, forced_group=forced_group,
                          names={forced_group: name}, trace_id=trace_id)
    if forced_group:
        globals_map = {"forced": forced_group}
        forced_name = (qa_profiles.get_profile(forced_group) or {}).get("name") or forced_group
        # fixed group: skip call1's ambiguity, still extract facts and write
        cards = group_cards() or _basic_cards()
        if forced_group not in {c["jid"] for c in cards}:
            name = (qa_profiles.get_profile(forced_group) or {}).get("name") or forced_group
            cards = list(cards) + [{"jid": forced_group, "name": name,
                                    "objective": "", "themes": "", "latest": ""}]
        names = {c["jid"]: c["name"] for c in cards}
        data1 = {"task": "new", "groups": [forced_group],
                 "task_outline": text, "follow_up_refs": [], "window": {}}
    else:
        out = _flow_answer(text, exclude_jid=exclude_jid, trace_id=trace_id)
        if out is not None:
            return out
        return None
    # forced-group path reuses the same executor
    valid = [forced_group]
    cards = group_cards() or _basic_cards()
    names = {c["jid"]: c["name"] for c in cards}
    start, end = _window_from({"task_outline": text}, text)
    context, total, media_count = _flow_messages(
        valid, start, end=end, names=names)
    if not context:
        return {"kind": "question", "decision": "resolved", "jid": forced_group,
                "name": names[forced_group],
                "reply": f"*Nembu* 🍋 · *{names[forced_group]}* · {end.strftime('%a %d %b')}\n- Nothing stored in that window.",
                "turn_key": ""}
    prompt2 = f"""{_identity_block()}Extract the material facts from these WhatsApp messages for the user.

Task: {text}
Question (exact): {text}
Window: {start.strftime('%a %d %b %H:%M')} → {end.strftime('%H:%M')}
Groups: {names[forced_group]}

Rules:
- facts: only what the messages say; group, who, when, what, exact deciding quote.
- on_you: asks/mentions addressed to the user (by name, @mention, reply, "you", their team, or a role they own) still unanswered; approvals/decisions waiting on the user; deadlines the user owns. Include indirect asks where a named owner is clearly the user or their side.
- others: open asks between others, things the user asked that nobody answered, undecided decisions, upcoming deadlines.
- open_items: for pending/overdue questions, one entry per open item with chat, person (who raised/asked), owner (who must act), owner_quote (the exact short phrase from the message that proves the actor), what, deadline (as written), since (when it was raised), next_action, priority (high/medium/low), on_you (true only when owner is the user or the assistant DM).
- Return one open_items entry for EVERY unresolved ask, deadline, approval or decision; do not collapse them. person is who asked/raised; owner is the actor; owner_quote must be copied from the message, never inferred. Examples: "Raghav: Will share an update on campaigns and C2" -> person=Raghav, owner=Raghav, owner_quote="Will share an update"; "AK to follow up and close by 6th" -> person=Nisha, owner=Nisha, owner_quote="AK to follow up"; "Maya asked Sam to submit feedback" -> person=Maya, owner=Sam, owner_quote="asked Sam to submit feedback"; "Vihaan: please check Zephyr approvals" -> person=Vihaan, owner=Sam, owner_quote="please check Zephyr approvals"; "John's list ... inspect the T Nagar door" -> person=John, owner=John, owner_quote="John's list". If owner/owner_quote are unknown, leave them empty; code falls back to person, then Others.
- decision/why: include the decision, the reason, and when it was made; keep the deciding quote. Kill/stop/pause/go/no-go statements are decisions; if only a recommendation exists, label it "recommendation, not a confirmed decision".
- done: things the USER themselves completed, shipped, closed, delivered or followed up on in the window. One entry per accomplishment: group, who, when, what, quote. Empty if none.
- conflicts: conflicting values, with both.
- coverage: messages read, media not searchable, anything cut.

Messages:
{context[:QA_CONTEXT_CHARS]}

Return ONLY JSON."""
    data2 = _call_json_retry(prompt2, {"facts": [], "on_you": [], "others": [], "done": [],
                                       "conflicts": [],
                                       "coverage": {"messages": total,
                                                    "media_unsearchable": media_count,
                                                    "cut": len(context) >= QA_CONTEXT_CHARS}},
                             CALL2_SCHEMA, "qa-call2", trace_id, model=QA_MODEL)
    if data2.get("_gemini_failed"):
        return {"kind": "failed", "reason": data2.get("_reason") or "bad_output"}
    mode = _ask_mode(text.lower())
    fmt_mode = ("open_items" if mode == "open_items"
                else "lookup" if mode == "lookup"
                else "decision" if mode == "decision"
                else "done" if mode == "done"
                else "catch_up")
    persona = agent_config.persona_block()
    prompt3 = f"""{persona}Write the WhatsApp answer for the user.

What they asked: {text}
Group: {names[forced_group]}

Facts:
{json.dumps({'facts': data2.get('facts'), 'done': data2.get('done')}, ensure_ascii=False)[:20000]}

Rules:
- Use only these facts. If the answer isn't there, say so.
- Attribute facts ("Raghav, Mon 10:07"). Keep amounts/dates exact.
- No title line; one bullet per line starting "- ".
- WhatsApp formatting only.
- If the ask is what the user got done, list only the completed items from `done`: what, when. No unrelated chatter.

Return ONLY JSON: {{"answer": "..."}}"""
    data3 = _call_json(prompt3, {"answer": ""}, CALL3_SCHEMA, "qa-call3", trace_id, model=QA_MODEL)
    timed_out = False
    if data3.get("_gemini_failed"):
        body = _fallback_answer_body(fmt_mode, data2)
        timed_out = True
    else:
        body = format_body(fmt_mode, _strip_single_group_prefix(
            (data3.get("answer") or "").strip(), names[forced_group]))
    if not body:
        return {"kind": "failed", "reason": data3.get("_reason") or "bad_output"}
    header = f"*Nembu* 🍋 · *{names[forced_group]}* · {end.strftime('%a %d %b')}"
    footer = "\n_(short answer — the AI writer timed out)_" if timed_out else ""
    reply, turn_key = _chunk_and_store(header + "\n" + body + footer, forced_group)
    set_selection(forced_group, names[forced_group], text, body)
    return {"kind": "question", "decision": "resolved", "jid": forced_group,
            "name": names[forced_group], "expert": "catch_up", "question": text,
            "reply": reply, "turn_key": turn_key}


_Q_RE = re.compile(
    r"\b(what|whats|what'?s|who|whom|whose|when|where|why|how|which|did|does|doing|"
    r"is|are|was|were|can|could|should|would|any|anything|someone|somebody|"
    r"summar\w*|summari[sz]e|tell|show|list|give|status|pending|open items?|"
    r"happening|happened|happon\w*|plan|decided|decision|waiting|newest|latest|"
    r"action item|draft|reply|kya|kaun|kab|kahan)\b", re.I)


_GROUP_MENTION = re.compile(r"\b(group|grp|wale|waale|mein\s+)\b|\bin\s+the\s+[a-z]", re.I)


_GLOBAL_RE = re.compile(
    r"(what (all )?did i miss|what have i missed|catch me up|anything important|"
    r"anything urgent|any asks? for me|anything on my plate|all groups|across groups|"
    r"everything i missed|what happened (today|yesterday|last night|since morning)|what'?s important today)",
    re.I)


def is_global_question(text: str) -> bool:
    return bool(_GLOBAL_RE.search(text or ""))


def _global_window(text: str):
    now = mstate.now_local()
    low = (text or "").lower()
    if "yesterday" in low:
        start = (now - datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(hour=23, minute=59, second=59)
        return start, end
    if "morning" in low:
        start = now.replace(hour=8, minute=0, second=0, microsecond=0)
    elif "today" in low or "tofay" in low:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif "week" in low:
        start = now - datetime.timedelta(days=7)
    elif "month" in low:
        start = now - datetime.timedelta(days=30)
    else:
        start = now - datetime.timedelta(hours=24)
    return start, now


ASK_RE = re.compile(r"\?|\b(please|pls|kindly|can you|could you|confirm|approve|share|send|"
                    r"need|needed|blocked|deadline|due|by (today|tomorrow|eod|tonight))\b", re.I)
MONEY_RE2 = re.compile(r"₹|\brs\.?|\binr\b|invoice|payment|reimburse|budget|amount", re.I)


