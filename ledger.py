#!/usr/bin/env python3
"""ledger.py — an open-loops tracker used by the Radar.

Instead of "summarize the workstreams", this asks: who owes what to whom,
and is it still open? The LLM only classifies and cites message numbers;
code owns dates, ages, sorting, dedupe and layout.
"""

import datetime
import re

import mstate

UTC = datetime.timezone.utc

# No hardcoded people or chats: @LID mentions resolve through the engine's
# contact/LID lookup per chat; anything unresolvable becomes "@someone".
MENTION_RE = re.compile(r"@(\d{10,})")
# Pure filler only. Never drop closure words ("done", "paused", "live", "yes").
BANTER = {"lol", "haha", "hey", "o", "k", "hmm", "👋👋", "?", "??", "???", "."}

# Bot digests and slash commands never belong in the input transcript.
BOT_NOISE = (
    "🎯",
    "⏭ *Next:*",
    "⚠️ *Nembu* 🍋",
    "⚠️ *Mosaic*",
    "📋 *Last 24 Hrs*",
    "📊 *Team Stats",
    "📌 *Pending",
)


def is_bot_noise(content: str) -> bool:
    text = (content or "").strip()
    if text.startswith("/"):
        return True
    return any(marker in text for marker in BOT_NOISE)


STATUSES = ["no_reply", "open", "blocked", "disputed", "done"]
STATUS_RANK = {"disputed": 0, "blocked": 1, "no_reply": 2, "open": 3}


def own_display_names() -> set:
    """Names that are 'me' — resolved from the pairing store, quietly."""
    names = set()
    try:
        import engine
        import mechat

        phone, lid, _src = mechat.own_identity()
        for ident in filter(None, [phone, lid]):
            try:
                name = engine._resolve_sender_name(ident)
            except Exception:
                name = ""
            if name and name != ident:
                names.add(name)
    except Exception:
        pass
    return names


# ── time helpers (code owns every date the user sees) ─────────────────────────

def now_ist() -> datetime.datetime:
    return datetime.datetime.now(mstate.local_tz())


def _ist(ts: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(ts)
    if dt.tzinfo is None:          # fixture stores naive UTC
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(mstate.local_tz())


def _to_ist(dt) -> datetime.datetime:
    if dt is None:
        return None
    if dt.tzinfo is None:          # engine returns naive UTC
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(mstate.local_tz())


def _now(ctx: dict) -> datetime.datetime:
    """Dump time for reproducible sandbox runs; wall clock in production."""
    raw = (ctx.get("meta") or {}).get("dumped_at")
    if raw:
        dt = datetime.datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=mstate.local_tz())
    return now_ist()


def day_label(dt: datetime.datetime, now: datetime.datetime) -> str:
    days = (now.date() - dt.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return dt.strftime("%a")
    return f"{dt.day} {dt:%b}"


def since_label(dt, now) -> str:
    lbl = day_label(dt, now)
    return lbl if lbl in ("today", "yesterday") else f"since {lbl}"


# ── preprocessing ─────────────────────────────────────────────────────────────

def real_mention_map(ids) -> dict:
    """@LID -> real display name via the engine's contact/LID lookup.

    Falls back to the owner's own name when the LID is the pairing store's
    owner LID (it sometimes has no lid_map row). Returns {} on failure.
    """
    ids = {i for i in ids if i}
    if not ids:
        return {}
    try:
        import engine
    except Exception:
        return {}
    own_lid, own_name = "", ""
    try:
        import mechat

        _phone, own_lid, _src = mechat.own_identity()
        names = own_display_names()
        own_name = next(iter(names), "")
    except Exception:
        pass
    out = {}
    for mid in ids:
        try:
            name = engine._resolve_sender_name(mid)
        except Exception:
            name = ""
        if (not name or name == mid) and own_lid and mid == own_lid and own_name:
            name = own_name
        if name and name != mid:
            out[mid] = name
    return out


def resolve_mentions(text: str, mention_map=None) -> str:
    mapping = mention_map or {}
    return MENTION_RE.sub(lambda m: "@" + mapping.get(m.group(1), "someone"), text)


def _own_idents() -> tuple[set, str]:
    """(digits that mean 'me', my display name) from the pairing store."""
    idents, name = set(), ""
    try:
        import mechat

        phone, lid, _src = mechat.own_identity()
        idents = {i for i in (phone, lid) if i}
        names = own_display_names()
        name = next(iter(names), "")
    except Exception:
        pass
    return idents, name


def _sender_label(sender: str, own_idents: set, own_name: str, chat_jid: str) -> str:
    """Raw-digit senders (own phone, group-system sends, unresolvable LIDs) are
    normalized so the model never sees numbers as 'people'."""
    s = (sender or "").strip()
    digits = s.lstrip("+")
    if not digits.isdigit():
        return s or "?"
    if digits in own_idents:
        return own_name or s
    group_user = (chat_jid or "").split("@")[0]
    if group_user and digits == group_user:
        return "Unknown"
    try:
        import engine

        name = engine._resolve_sender_name(digits)
    except Exception:
        name = ""
    if name and name != digits:
        return name
    return "Unknown"


def _rows_to_transcript(rows, now, max_msgs: int = 400, chat_jid: str = ""):
    """Numbered, cleaned, IST-labelled transcript + index for validation."""
    own_idents, own_name = _own_idents()
    raw_rows = []
    for dt, sender, content in rows:
        content = (content or "").strip()
        if not content or dt is None:
            continue
        if is_bot_noise(content) or content.lower() in BANTER:  # always clean
            continue
        sender = _sender_label(sender, own_idents, own_name, chat_jid)
        raw_rows.append((dt, sender, content))
    raw_rows = raw_rows[-max_msgs:]

    mentioned_ids = set()
    for _dt, _sender, content in raw_rows:
        mentioned_ids.update(MENTION_RE.findall(content))
    mention_map = real_mention_map(mentioned_ids)

    index, lines, people = {}, [], set(mention_map.values())
    for n, (dt, sender, content) in enumerate(raw_rows, 1):
        text = resolve_mentions(content, mention_map).replace("\n", " / ")
        if len(text) > 400:
            text = text[:400] + "…"
        index[n] = (dt, sender, text)
        people.add(sender)
        lines.append(f"[{n}] {day_label(dt, now)} {dt:%H:%M} · {sender}: {text}")
    people.discard("Unknown")
    return index, "\n".join(lines), sorted(people)


def build_transcript(ctx: dict, window: str = "7d", max_msgs: int = 400):
    """Sandbox variant: transcript from a data-dump context."""
    rows = []
    for m in ctx["windows"][window]["messages"]:
        ts = m.get("ts")
        rows.append((_ist(ts) if ts else None, m.get("sender") or "?", m.get("content")))
    chat_jid = (ctx.get("meta") or {}).get("chat_jid") or ""
    return _rows_to_transcript(rows, _now(ctx), max_msgs, chat_jid)


# ── prompt + schema ───────────────────────────────────────────────────────────

ASK_PAT = re.compile(
    r"(please|pls|need|can you|can u|could you|kindly|check|update|share|push|"
    r"confirm|validate|figure|send|take live|go live|follow up|next step|chase|"
    r"unblock|fix|revert)", re.I)


def candidate_asks(index: dict, limit: int = 80) -> list[tuple[int, str, str]]:
    """Deterministic recall guard: messages that ask a named person (or the
    group) to do something. [(msg_no, person, preview)] — recent ones win."""
    question_start = re.compile(r"^(what|whats|what's|when|who|why|how)\b", re.I)
    out = []
    for n in sorted(index):
        _dt, _sender, text = index[n]
        core = re.sub(r"\s+", " ", text).strip()
        mentioned = re.findall(r"@([A-Za-z][\w.'-]*(?: [A-Z][\w.'-]*)?)", text)
        first = core.split(" / ", 1)[0]
        is_ask = (first.rstrip().endswith("?") or core.endswith("?")
                  or ASK_PAT.search(core)
                  or re.search(r"\b(what|how|why|when|who)\b", core, re.I))
        if mentioned and is_ask:
            out.append((n, mentioned[0].strip(), core[:70]))
        elif not mentioned and question_start.match(core) and len(core) < 120:
            out.append((n, "group", core[:70]))
    return out[-limit:]


LEDGER_PROMPT = """You maintain the open-loops ledger for a user. Read the numbered WhatsApp messages below (times are IST; today is {today}) and list every OPEN loop: something a specific person was asked to do, or committed to do, that the chat does not show as finished.

People: {people}

Messages:
{transcript}

Candidate asks detected in the transcript — each UNRESOLVED one must appear as a loop (merge duplicates; skip only when the chat shows it finished):
{candidates}

How to judge:
- owner = the person who must act next, NOT the person asking. "@Name" inside an ask means that person is being asked. An ask to "@all" or the whole group → owner "Unassigned".
- A loop is done ONLY if a later message shows it finished ("done", "live", "paused", "shared", "sorted", a link delivered). Acks like "yes yes", "will check", "calling you", "in 20 mins" do NOT close it.
- status: "no_reply" if the owner never answered; "disputed" if the owner pushed back or people disagree; "blocked" if waiting on someone outside the group or another loop; else "open".
- ONE loop per owner per brand/campaign: fold follow-ups ("update on X", "own X", "fix X") into the original ask. Merge repeat asks: opened = first ask's [number], last_touch = latest related [number], chases = how many times it was asked in total.
- task: imperative, at most 12 words, specific (brand + asset + action), e.g. "Push IDFC webview live at ₹40k/day".
- due: copy the deadline wording from the chat ("by 12 noon", "Monday", "tom H1"), else "".
- Skip ideas with no ask, opinions, banter, and anything finished.
- Asks marked (OWNER) are directed at the user himself: always include them.
- Never write dates or times yourself; cite message numbers only.
- No duplicates: once an ask appears, merge every repeat of it into that loop.
- Prefer the most consequential loops; never exceed 20.

headline: one line, max 15 words — the single thing to unblock first, naming who. No dates or time words (today, noon, Monday).

Return ONLY JSON: {{"headline": "...", "loops": [{{"task", "owner", "asked_by", "opened", "last_touch", "chases", "status", "due"}}]}}
"""


def ledger_schema(people: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "loops": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "owner": {"type": "string", "enum": people + ["Unassigned"]},
                        "asked_by": {"type": "string", "enum": people + ["Unknown"]},
                        "opened": {"type": "integer"},
                        "last_touch": {"type": "integer"},
                        "chases": {"type": "integer"},
                        "status": {"type": "string", "enum": STATUSES},
                        "due": {"type": "string"},
                    },
                    "required": ["task", "owner", "asked_by", "opened",
                                 "last_touch", "chases", "status", "due"],
                },
            },
        },
        "required": ["headline", "loops"],
    }


def _ledger_prompt(index: dict, transcript: str, people: list[str],
                   now: datetime.datetime):
    own = own_display_names()
    candidates = candidate_asks(index)
    cand_block = "\n".join(
        f"- [{n}] asks @{person}{' (OWNER)' if person in own else ''}: {preview}"
        for n, person, preview in candidates
    ) or "(none detected)"
    prompt = LEDGER_PROMPT.format(
        today=f"{now:%a %d %b %Y}", people=", ".join(people),
        transcript=transcript, candidates=cand_block)
    return prompt, ledger_schema(people), {"headline": "", "loops": []}, own


def _merge_samples(d1, d2) -> dict:
    return {
        "headline": ((d1 or {}).get("headline") or (d2 or {}).get("headline") or ""),
        "loops": (((d1 or {}).get("loops") or []) + ((d2 or {}).get("loops") or [])),
    }


# ── validation ────────────────────────────────────────────────────────────────

# Capitalised words that name a channel/asset/time, not a brand — they must not
# be used to decide two loops are "the same thing".
GENERIC_CAPS = {
    "webview", "spotlight", "masthead", "top", "nav", "gtm", "home", "win",
    "sl", "wv", "pps", "mf", "cc", "pm", "am", "jira", "slack", "ops", "kam",
    "kams", "qc", "ro", "cpm", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "today", "tomorrow",
}


def _entities(task: str) -> set:
    """Brand-ish tokens (Capitalised / ALLCAPS, not the leading verb)."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9&.-]+", task)[1:]
    return {w.lower() for w in words
            if w[0].isupper() and len(w) >= 3 and w.lower() not in GENERIC_CAPS}


def clean_loops(data, index) -> list[dict]:
    """Drop done/uncited loops; merge loops one owner has on the same brand."""
    kept: list[dict] = []
    for lp in (data or {}).get("loops") or []:
        if lp.get("status") == "done":
            continue
        opened = lp.get("opened")
        if opened not in index:            # uncited / hallucinated → drop
            continue
        task = re.sub(r"[*_~`]", "", (lp.get("task") or "")).strip()
        if not task:
            continue
        last = lp.get("last_touch") if lp.get("last_touch") in index else opened
        cur = {**lp, "task": task, "ents": _entities(task),
               "opened_dt": index[opened][0], "last_dt": index[last][0]}

        twin = next((k for k in kept if k.get("owner") == cur.get("owner") and (
            k["opened"] == opened or (k["ents"] & cur["ents"]))), None)
        if twin is None:
            kept.append(cur)
            continue
        # merge into the earlier-ranked twin: oldest open, newest touch, max chases
        twin["chases"] = max(twin.get("chases") or 0, cur.get("chases") or 0)
        twin["opened_dt"] = min(twin["opened_dt"], cur["opened_dt"])
        twin["last_dt"] = max(twin["last_dt"], cur["last_dt"])
        if STATUS_RANK.get(cur.get("status"), 4) < STATUS_RANK.get(twin.get("status"), 4):
            twin["status"] = cur["status"]
        if not (twin.get("due") or "").strip():
            twin["due"] = cur.get("due") or ""
    return kept


# ── due dates (code decides overdue, never the model) ─────────────────────────

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DATE_DUE = re.compile(
    r"(?:\b(?:by|before|due|on|till|until)\s+)?\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")[a-z]*\b",
    re.I,
)
RELATIVE_DUE = re.compile(
    r"\b(today|tonight|tom|tmrw|tomorrow|eod|noon|h1|h2|first half|second half|"
    r"\d{1,2}(:\d{2})?\s*(am|pm))\b", re.I)
NOT_A_DEADLINE = re.compile(r"^\s*(asap|urgent|priority|prio|now|immediately)\s*$", re.I)


def due_label(due: str, opened_dt, now):
    """('Mon', False) / ('overdue', True) / (None, False). Weekdays resolve to
    the first such day on/after the ask; relative words expire after the ask day."""
    due = (due or "").strip()
    if not due:
        return None, False
    if NOT_A_DEADLINE.match(due):
        return None, False
    low = due.lower()
    for i, wd in enumerate(WEEKDAYS):
        if wd in low or re.search(rf"\b{wd[:3]}\b", low):
            target = opened_dt.date() + datetime.timedelta(days=(i - opened_dt.weekday()) % 7)
            if target < now.date():
                return "overdue", True
            if target == now.date():
                return "today", False
            return target.strftime("%a"), False
    dm = DATE_DUE.search(low)
    if dm:
        day_n = int(dm.group(1))
        month_n = MONTHS.index(dm.group(2).lower()) + 1
        year = opened_dt.year
        try:
            target_date = datetime.date(year, month_n, day_n)
        except ValueError:
            return due, False
        if target_date < now.date():
            return "overdue", True
        if target_date == now.date():
            return "today", False
        return target_date.strftime("%d %b"), False
    if RELATIVE_DUE.search(low):
        if opened_dt.date() < now.date() and "tom" not in low:
            return "overdue", True
        if ("tom" in low or "tomorrow" in low) and \
                opened_dt.date() + datetime.timedelta(days=1) < now.date():
            return "overdue", True
    return due, False


# ── render (WhatsApp-native) ──────────────────────────────────────────────────

STATUS_TEXT = {"no_reply": "no reply", "disputed": "disputed", "blocked": "blocked"}


def _short(name: str) -> str:
    return (name or "").split()[0] if name else ""


def render_ledger(data, index, now, chat_name: str = "", own_names=None) -> str:
    """WhatsApp formatting only: *bold*, _italic_, '- ' bullet lists, '> ' quote."""
    loops = clean_loops(data, index)
    title = f"📌 *Pending — {chat_name.strip()[:48]}*" if chat_name else "📌 *Pending*"
    if not loops:
        return f"{title}\n\n✅ Nothing open right now."
    if own_names is None:
        own_names = own_display_names()

    # decorate: due / overdue computed here
    for lp in loops:
        lp["due_txt"], lp["overdue"] = due_label(lp.get("due"), lp["opened_dt"], now)

    by_owner: dict[str, list] = {}
    for lp in loops:
        by_owner.setdefault(lp.get("owner") or "Unassigned", []).append(lp)
    for items in by_owner.values():
        items.sort(key=lambda lp: (not lp["overdue"],
                                   STATUS_RANK.get(lp.get("status"), 4),
                                   -(lp.get("chases") or 0), lp["opened_dt"]))
    owners = sorted(by_owner, key=lambda o: (o not in own_names, o == "Unassigned",
                                             -len(by_owner[o])))

    total = len(loops)
    overdue = sum(lp["overdue"] for lp in loops)
    stamp = f"{now:%a %-d %b}, {now:%-I:%M %p}"
    sub = [stamp, f"{total} open"]
    if overdue:
        sub.append(f"{overdue} overdue")
    sub.append("last 7 days")

    out = [title, f"_{' · '.join(sub)}_"]
    headline = ((data or {}).get("headline") or "").strip()
    # the model must not date things; a headline that does is dropped, not shown
    if headline and not (RELATIVE_DUE.search(headline) or
                         any(wd in headline.lower() for wd in WEEKDAYS)):
        out += ["", f"> {re.sub(r'[*_~`]', '', headline)}"]

    for owner in owners:
        items = by_owner[owner]
        label = "You" if owner in own_names else (
            "Team (@all)" if owner == "Unassigned" else owner)
        out += ["", f"*{label}* · {len(items)}"]
        for lp in items:
            meta = []
            asker = lp.get("asked_by")
            if asker and asker not in (owner, "Unknown"):
                meta.append(_short(asker))
            meta.append(since_label(lp["opened_dt"], now))
            if (lp.get("chases") or 0) >= 2:
                meta.append(f"chased {lp['chases']}×")
            if lp.get("status") in STATUS_TEXT:
                meta.append(STATUS_TEXT[lp["status"]])
            task = lp["task"]
            if lp["overdue"]:
                task = f"⚠️ {task}"
            elif lp["due_txt"]:
                meta.append(f"due {lp['due_txt']}")
            out.append(f"- {task} — _{' · '.join(meta)}_")
    return "\n".join(out).strip()
