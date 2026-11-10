#!/usr/bin/env python3
"""qa_profiles.py — group profiles for Me-chat Q&A.

Profiles are a lookup aid only: name, aliases, a 2-sentence factual description
and 3-8 topics that pass a code check. They are never shown to an expert and
never quoted in answers. Built after sync, refreshed at most daily and only
for groups that changed; skipped when the AI budget is low.

Q&A scope is every non-archived group with activity in the retention window
(minus mutes and bot-flagged groups). It does not require the user to have
posted, so groups they read but rarely write in are still searchable.
"""
import json
import os
import _envload
import re
import unicodedata

import agent_config
import budget
import engine
import mechat
import mstate
import watch_rules

PROFILE_TOKEN_CHARS = 16000   # ~4k tokens of recent messages
PROFILE_BATCH = _envload.getenv_int("NEMBU_PROFILE_BATCH", "MOSAIC_PROFILE_BATCH", 20)
QA_SCOPE_CHATS = int(os.getenv("QA_SCOPE_CHATS",
                               os.getenv("QA_SCOPE_GROUPS", "200")))
QA_INCLUDE_BOT_DMS = os.getenv("QA_INCLUDE_BOT_DMS", "1") == "1"
REFRESH_NEW_MSGS = 20
TOPIC_MIN_MSGS = 3
TOPIC_MIN_DAYS = 2

_EMOJI = re.compile(
    "[" "\U0001F000-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "]+"
)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = _EMOJI.sub(" ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def phonetic_key(text: str) -> str:
    """Cheap sound-alike key (consonant skeleton with common substitutions)."""
    out = []
    for tok in normalize(text).split():
        s = tok
        for a, b in (("ph", "f"), ("ck", "k"), ("qu", "k"), ("x", "ks"),
                     ("z", "s"), ("v", "w"), ("dh", "d"), ("th", "t"),
                     ("ee", "i"), ("oo", "u")):
            s = s.replace(a, b)
        s = re.sub(r"[aeiou]", "", s)
        s = re.sub(r"(.)\1+", r"\1", s)
        if s:
            out.append(s)
    return "-".join(out)


def derive_aliases(name: str) -> list:
    norm = normalize(name)
    if not norm:
        return []
    aliases = {norm}
    toks = norm.split()
    if len(toks) > 1 and len(toks[0]) >= 4:
        aliases.add(toks[0])
    trimmed = re.sub(r"\b(group|grp|chat|team)\b", "", norm).strip()
    if trimmed and trimmed != norm:
        aliases.add(trimmed)
    key = phonetic_key(norm)
    if key:
        aliases.add(key)
    return sorted(a for a in aliases if a)


# ── eligibility ───────────────────────────────────────────────────────────────
def _dm_label(jid: str) -> str:
    """Readable fallback for a DM with no saved contact name."""
    digits = re.sub(r"\D", "", str(jid or "").split("@")[0])
    return f"DM {digits[-4:]}" if len(digits) >= 4 else "DM"


def _resolve_dm_names(names: dict) -> dict:
    """Replace raw LID/phone DM labels with saved contact names."""
    try:
        contacts = watch_rules.contact_info()
    except Exception:
        contacts = {}
    out = dict(names)
    for jid, name in names.items():
        if jid.endswith("@g.us"):
            continue
        bare = str(name).split("@")[0]
        if (str(name) == jid or bare.isdigit() or
                not re.search(r"[A-Za-z0-9]", str(name))):
            saved, biz = contacts.get(jid, ("", ""))
            if not re.search(r"[A-Za-z0-9]", saved or ""):
                saved = ""
            if not re.search(r"[A-Za-z0-9]", biz or ""):
                biz = ""
            out[jid] = saved or biz or _dm_label(jid)
    return out


def eligibility() -> dict:
    """{jid: (name, eligible, reason)} for every searchable chat.

    Groups and direct messages with activity in the retention window.
    Archived, muted and bot groups stay out (archived/bot are configurable).
    """
    out = {}
    con = engine._get_conn()
    names = {}
    if con is not None:
        try:
            for jid, name in con.execute(
                "SELECT jid, name FROM chats "
                "WHERE jid NOT LIKE '%@newsletter' "
                "AND jid <> 'status@broadcast'"
            ).fetchall():
                names[jid] = name or jid
        finally:
            con.close()
    try:
        me_jids = set(mechat.mechat_jids())
    except Exception:
        me_jids = set()
    for jid in list(names):
        if jid in me_jids:
            names.pop(jid, None)
    names = _resolve_dm_names(names)
    archived = watch_rules.archived_jids()
    muted = watch_rules.muted_jids()
    activity = watch_rules.chat_activity(days=mstate.RETENTION_DAYS)
    status = {}
    c = mstate.connect()
    try:
        for row in c.execute("SELECT chat_jid, state, reason FROM watch_status").fetchall():
            status[row["chat_jid"]] = (row["state"], row["reason"])
    finally:
        c.close()
    eligible = []
    for jid, name in names.items():
        is_group = jid.endswith("@g.us")
        state, reason = status.get(jid, ("", ""))
        if agent_config.is_excluded_chat(jid, name):
            out[jid] = (name, False, "excluded")
        elif jid in archived and not agent_config.include_archived():
            out[jid] = (name, False, "archived")
        elif jid in muted:
            out[jid] = (name, False, "muted")
        elif is_group and state == "excluded" and reason == "bot":
            out[jid] = (name, False, "bot")
        elif (not is_group and not QA_INCLUDE_BOT_DMS and
              state == "excluded" and reason == "bot"):
            out[jid] = (name, False, "bot")
        elif not activity.get(jid):
            out[jid] = (name, False,
                        f"no activity in the last {mstate.RETENTION_DAYS} days")
        else:
            out[jid] = (name, True, "")
            eligible.append(jid)
    if len(eligible) > QA_SCOPE_CHATS:
        eligible.sort(
            key=lambda j: ((activity.get(j) or {}).get("n_all", 0),
                           (activity.get(j) or {}).get("last_all") or ""),
            reverse=True,
        )
        for jid in eligible[QA_SCOPE_CHATS:]:
            out[jid] = (names.get(jid, jid), False, "over Q&A scope cap")
    return out


# ── profile build ─────────────────────────────────────────────────────────────
def _recent_messages(jid: str, limit: int = 200):
    if engine._get_conn() is None:
        return []
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT id, sender, content, media_type, timestamp, is_from_me FROM messages "
                "WHERE chat_jid=? AND content IS NOT NULL AND TRIM(content)!='' "
                "ORDER BY timestamp DESC LIMIT ?",
                (jid, limit),
            ).fetchall()
        finally:
            con.close()
        return list(reversed(rows))
    except Exception:
        return []


def _profile_prompt(name: str, rows) -> str:
    lines, used = [], 0
    for _mid, sender, content, _media, _ts, _me in rows:
        line = f"{sender or '?'}: {content}"
        if used + len(line) > PROFILE_TOKEN_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    return f"""You write a small factual profile of a WhatsApp group so an assistant can find it later.

Group name: {name!r}

Recent messages:
{chr(10).join(lines)}

Rules:
- description: at most 2 sentences describing what the group is used for, only from what the messages show. If unclear, use exactly "unclear".
- topics: 3-8 short lowercase phrases (1-3 words) for recurring subjects. No people's names, no goals/OKRs, no sentiment, no judgements about members.
- Return ONLY JSON: {{"description": "...", "topics": ["...", "..."]}}"""


def _code_check_topics(topics, rows) -> list:
    """Keep a topic only if it appears in >=3 messages on >=2 different days."""
    kept = []
    for topic in topics or []:
        phrase = normalize(str(topic))
        if not phrase:
            continue
        hits, days = 0, set()
        for row in rows:
            content = row[2]
            ts = row[4] if len(row) >= 6 else row[3]
            if phrase in normalize(content or ""):
                hits += 1
                days.add(str(ts)[:10])
        if hits >= TOPIC_MIN_MSGS and len(days) >= TOPIC_MIN_DAYS:
            kept.append(phrase)
    return kept[:8]


def _store_profile(jid, name, aliases, description, topics, eligible, reason, msgs_from, msgs_to):
    c = mstate.connect()
    try:
        c.execute(
            "INSERT OR REPLACE INTO group_profile "
            "(chat_jid, name, aliases, description, topics, eligible, reason, refreshed_at, msgs_from, msgs_to) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (jid, name, json.dumps(aliases, ensure_ascii=False),
             description, json.dumps(topics, ensure_ascii=False),
             1 if eligible else 0, reason or "", mstate.now_local().isoformat(),
             msgs_from or "", msgs_to or ""),
        )
        c.commit()
    finally:
        c.close()


def get_profile(jid: str) -> dict:
    c = mstate.connect()
    try:
        row = c.execute("SELECT * FROM group_profile WHERE chat_jid=?", (jid,)).fetchone()
    finally:
        c.close()
    if not row:
        return {}
    d = dict(row)
    for key in ("aliases", "topics"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except Exception:
            d[key] = []
    return d


def profiles(eligible_only: bool = True) -> dict:
    c = mstate.connect()
    try:
        q = "SELECT * FROM group_profile" + (" WHERE eligible=1" if eligible_only else "")
        rows = c.execute(q).fetchall()
    finally:
        c.close()
    out = {}
    for row in rows:
        d = dict(row)
        for key in ("aliases", "topics"):
            try:
                d[key] = json.loads(d.get(key) or "[]")
            except Exception:
                d[key] = []
        out[d["chat_jid"]] = d
    return out


def basic_profile(jid: str, name: str) -> None:
    _store_profile(jid, name, derive_aliases(name), "unclear", [], True, "", "", "")


def refresh_profile(jid: str, name: str) -> bool:
    rows = _recent_messages(jid)
    if not rows:
        basic_profile(jid, name)
        return False
    prompt = _profile_prompt(name, rows)
    fallback = {"description": "unclear", "topics": []}
    schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "topics": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["description", "topics"],
    }
    try:
        data = budget.gemini_json(prompt, fallback, schema, purpose="qa-profile")
    except budget.BudgetPause:
        return False
    if data.get("_gemini_failed"):
        return False
    desc = (data.get("description") or "unclear").strip()
    if desc.lower() == "unclear":
        desc = "unclear"
    topics = _code_check_topics(data.get("topics") or [], rows)
    _store_profile(
        jid, name, derive_aliases(name), desc, topics, True, "",
        str(rows[0][4])[:19] if rows else "", str(rows[-1][4])[:19] if rows else "",
    )
    return True


def ensure_profiles(force: bool = False) -> dict:
    """Build missing profiles and refresh changed ones. Never sends anything."""
    elig = eligibility()
    have = profiles(eligible_only=False)
    now = mstate.now_local()
    built = refreshed = marked = 0
    processed = 0
    for jid, (name, ok, reason) in elig.items():
        if processed >= PROFILE_BATCH:
            break
        processed += 1
        prof = have.get(jid)
        if not ok:
            if prof and prof.get("eligible"):
                c = mstate.connect()
                try:
                    c.execute(
                        "UPDATE group_profile SET eligible=0, reason=? WHERE chat_jid=?",
                        (reason, jid),
                    )
                    c.commit()
                finally:
                    c.close()
                marked += 1
            elif not prof:
                _store_profile(jid, name, derive_aliases(name), "unclear", [], False, reason, "", "")
                marked += 1
            continue
        if not prof:
            basic_profile(jid, name)
            built += 1
            continue
        if not prof.get("eligible"):
            c = mstate.connect()
            try:
                c.execute("UPDATE group_profile SET eligible=1, reason='' WHERE chat_jid=?", (jid,))
                c.commit()
            finally:
                c.close()
        # refresh rules: at most daily, and only after >=20 new messages
        if not force:
            last = prof.get("refreshed_at") or ""
            try:
                from datetime import datetime

                if last and (now - datetime.fromisoformat(last)).total_seconds() < 24 * 3600:
                    continue
            except (TypeError, ValueError):
                pass
            newer = 0
            try:
                import sqlite3

                con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
                try:
                    newer = con.execute(
                        "SELECT COUNT(*) FROM messages WHERE chat_jid=? AND timestamp > ?",
                        (jid, prof.get("msgs_to") or ""),
                    ).fetchone()[0]
                finally:
                    con.close()
            except Exception:
                newer = 0
            if newer < REFRESH_NEW_MSGS and prof.get("description") != "unclear":
                continue
            # budget-aware: skip when the daily ceiling is close
            try:
                if budget.spent_today() >= 0.8 * budget.DAILY_USD_CAP:
                    continue
            except Exception:
                pass
        if refresh_profile(jid, name):
            refreshed += 1
    return {"eligible": sum(1 for v in elig.values() if v[1]), "built": built,
            "refreshed": refreshed, "marked": marked}


# ── aliases ──────────────────────────────────────────────────────────────────
def add_confirmed_alias(jid: str, phrase: str) -> bool:
    phrase = normalize(phrase)
    if not jid or not phrase:
        return False
    prof = get_profile(jid)
    if not prof:
        return False
    aliases = list(prof.get("aliases") or [])
    if phrase not in aliases:
        aliases.append(phrase)
        c = mstate.connect()
        try:
            c.execute("UPDATE group_profile SET aliases=? WHERE chat_jid=?",
                      (json.dumps(aliases, ensure_ascii=False), jid))
            c.commit()
        finally:
            c.close()
    return True


def forget_last_alias(jid: str) -> str:
    prof = get_profile(jid)
    if not prof:
        return ""
    base = set(derive_aliases(prof.get("name") or ""))
    confirmed = [a for a in (prof.get("aliases") or []) if a not in base]
    if not confirmed:
        return ""
    removed = confirmed[-1]
    aliases = [a for a in (prof.get("aliases") or []) if a != removed]
    c = mstate.connect()
    try:
        c.execute("UPDATE group_profile SET aliases=? WHERE chat_jid=?",
                  (json.dumps(aliases, ensure_ascii=False), jid))
        c.commit()
    finally:
        c.close()
    return removed
