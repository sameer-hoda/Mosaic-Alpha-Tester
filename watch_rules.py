#!/usr/bin/env python3
"""watch_rules.py — the consideration set: which groups the watcher watches.

A chat is watched when ALL hold (computed in SQL, every hour):
  group (@g.us), user posted in it in the last 14 days (is_from_me=1),
  not archived (whatsmeow_chat_settings.archived=0), not bot-content.

Bot-group detection is conservative about accusing human senders:
  signal 1 (code): one sender dominates recent messages with
  machine-regular timing (near-constant intervals sustained over days);
  signal 2 (code): rigid templates (identical fixed prefixes/headers
  across many messages);
  signal 3 (cheap model call): only when signals 1-2 are borderline.

Policy: a confidently identified bot sender excludes the group even at
minority traffic; borderline groups stay watched with evidence surfaced.
An archived group is NEVER re-included by "watch" — the bot says it is
archived.至此 mute always excludes (with optional 1d / 7d window).
"""

import datetime
import json
import os
import re
import sqlite3
import statistics
from datetime import timedelta

import agent_config
import mstate

MESSAGES_DB_PATH = os.getenv("MESSAGES_DB_PATH", os.path.join(mstate.STORE_DIR, "messages.db"))
WHATSAPP_DB_PATH = os.getenv("WHATSAPP_DB_PATH", os.path.join(mstate.STORE_DIR, "whatsapp.db"))

BOT_INTERVAL_TOL = 0.35
BOT_SHARE = 0.65
BOT_MIN_MSGS = 12
TEMPLATE_SHARE = 0.7

SOFT_CAP_GROUPS = int(os.getenv("SOFT_CAP_GROUPS", "30"))
SOFT_CAP_DMS = int(os.getenv("SOFT_CAP_DMS", "30"))
LOW_WEIGHT = float(os.getenv("LOW_CHAT_WEIGHT", "-3"))
OTP_RE = re.compile(r"\b\d{4,8}\b is your|OTP|verification code|debited|credited|order #", re.I)


def _ro(path):
    if not os.path.exists(path):
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:
        return None


def archived_jids() -> set:
    con = _ro(WHATSAPP_DB_PATH)
    if con is None:
        return set()
    try:
        rows = con.execute(
            "SELECT chat_jid FROM whatsmeow_chat_settings WHERE archived = 1"
        ).fetchall()
        return {r[0] for r in rows if r[0]}
    except Exception:
        return set()
    finally:
        con.close()


def posted_groups(days: int = None) -> dict:
    """Group jids where the user sent at least one message in the window."""
    days = days or mstate.WATCH_WINDOW_DAYS
    cutoff = (mstate.now_local() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    con = _ro(MESSAGES_DB_PATH)
    if con is None:
        return {}
    try:
        rows = con.execute(
            "SELECT chat_jid, MAX(timestamp) as last_mine, COUNT(*) as n_mine "
            "FROM messages WHERE is_from_me=1 AND chat_jid LIKE '%@g.us' "
            "AND timestamp >= ? GROUP BY chat_jid",
            (cutoff,),
        ).fetchall()
        return {r[0]: {"last_posted": r[1], "posted_count": r[2]} for r in rows}
    except Exception:
        return {}
    finally:
        con.close()


def active_senders(chat_jid: str, window_days: int = 7) -> list:
    con = _ro(MESSAGES_DB_PATH)
    if con is None:
        return []
    cutoff = (mstate.now_local() - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = con.execute(
            "SELECT sender, timestamp, content FROM messages "
            "WHERE chat_jid=? AND timestamp >= ? AND content IS NOT NULL AND TRIM(content) != '' "
            "ORDER BY timestamp",
            (chat_jid, cutoff),
        ).fetchall()
        return rows
    except Exception:
        return []
    finally:
        if con:
            con.close()


def _intervals_regular(ts_list: list) -> float:
    """Fraction of consecutive gaps near the median gap (machine timing)."""
    if len(ts_list) < BOT_MIN_MSGS:
        return 0.0
    tss = []
    for ts in ts_list:
        dt = _parse_ts(ts)
        if dt:
            tss.append(dt)
    if len(tss) < BOT_MIN_MSGS:
        return 0.0
    gaps = [(tss[i + 1] - tss[i]).total_seconds() for i in range(len(tss) - 1)]
    gaps = [g for g in gaps if g > 0]
    if len(gaps) < BOT_MIN_MSGS - 1:
        return 0.0
    med = statistics.median(gaps)
    if med <= 0:
        return 0.0
    near = sum(1 for g in gaps if abs(g - med) <= BOT_INTERVAL_TOL * med)
    return near / len(gaps)


def _template_share(contents: list) -> float:
    """Share of messages sharing the same fixed prefix/header shape."""
    if len(contents) < BOT_MIN_MSGS:
        return 0.0
    prefixes = {}
    for c in contents:
        head = re.sub(r"\s+", " ", (c or "")[:24]).strip()
        head = re.sub(r"\d{1,2}:\d{2}", "HH:MM", head)
        head = re.sub(r"\d+", "N", head)
        if head:
            prefixes[head] = prefixes.get(head, 0) + 1
    if not prefixes:
        return 0.0
    return max(prefixes.values()) / len(contents)


def _parse_ts(raw):
    try:
        dt = datetime.datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def bot_evidence(chat_jid: str, use_model=False) -> dict:
    """Signals for one group. Confident 'bot' verdict excludes it; borderline
    stays watched and lists the suspected sender + evidence."""
    rows = active_senders(chat_jid, window_days=7)
    if len(rows) < BOT_MIN_MSGS:
        return {"verdict": "human", "signals": {}, "share": 0.0}
    by_sender = {}
    for sender, ts, content in rows:
        by_sender.setdefault(sender or "?", []).append((ts, content))
    top_sender, msgs = max(by_sender.items(), key=lambda kv: len(kv[1]))
    share = len(msgs) / len(rows)
    regular = _intervals_regular([ts for ts, _c in msgs])
    templ = _template_share([c for _ts, c in msgs])
    score = round(0.5 * regular + 0.5 * templ, 3)
    verdict = "suspected"
    if share >= BOT_SHARE and min(regular, templ) >= 0.8:
        verdict = "bot"
    elif score >= 0.55 and share >= 0.35:
        verdict = "borderline"
    out = {
        "verdict": verdict,
        "top_sender": top_sender,
        "share": round(share, 2),
        "regularity": round(regular, 2),
        "template": round(templ, 2),
    }
    if use_model and verdict in ("borderline", "suspected"):
        out["model_verdict"] = _model_bot_check(chat_jid, [c for _ts, c in rows[-50:]])
        if out["model_verdict"].get("verdict") == "bot" and out["model_verdict"].get("confidence", 0) >= 0.8:
            out["verdict"] = "bot"
    return out


def _model_bot_check(chat_jid: str, contents: list) -> dict:
    import budget

    sample = "\n".join(f"- {c[:120]}" for c in contents[-50:])
    prompt = (
        "Classify these recent group messages: are they Mostly automated (bot digests, "
        "alerts, system pings) or Mostly human conversation? Reply ONLY with JSON: "
        f'{{"verdict": "bot"|"human"|"mixed", "confidence": 0.0-1.0}}\n\n{sample}'
    )
    fallback = {"verdict": "mixed", "confidence": 0.0}
    return budget.gemini_json(prompt, fallback, schema=None, purpose="bot-detect") or fallback


def resolve_chat(name: str):
    """Name → (jid, candidates). Exact then case-insensitive match over the
    chats table; no guess on ambiguity."""
    con = _ro(MESSAGES_DB_PATH)
    if con is None:
        return "", []
    try:
        rows = con.execute("SELECT jid, name FROM chats WHERE name IS NOT NULL").fetchall()
    except Exception:
        return "", []
    finally:
        con.close()
    want = (name or "").strip().lower()
    if not want:
        return "", [dict(jid=r[0], name=r[1]) for r in rows[:8]]
    exact = [r for r in rows if (r[1] or "").strip().lower() == want]
    if len(exact) == 1:
        return exact[0][0], []
    if len(exact) > 1:
        return "", [dict(jid=r[0], name=r[1]) for r in exact]
    contains = [r for r in rows if want in (r[1] or "").lower()]
    if len(contains) == 1:
        return contains[0][0], []
    # No contains match -> no candidates. Returning arbitrary chats would
    # be a guess dressed as an option list.
    return "", [dict(jid=r[0], name=r[1]) for r in contains[:8]]


def _is_dm(jid: str) -> bool:
    return jid.endswith("@s.whatsapp.net") or jid.endswith("@lid")


def _is_group(jid: str) -> bool:
    return jid.endswith("@g.us")


def _is_static_excluded(jid: str) -> bool:
    j = (jid or "").lower()
    return j.startswith("status@") or j.endswith("@broadcast") or j.endswith("@newsletter")


def chat_activity(days: int = None) -> dict:
    """Per-chat activity in the retention window."""
    days = days or mstate.RETENTION_DAYS
    cutoff = (mstate.now_local() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    con = _ro(MESSAGES_DB_PATH)
    if con is None:
        return {}
    try:
        rows = con.execute(
            "SELECT chat_jid, MAX(timestamp), COUNT(*), SUM(is_from_me), "
            "MAX(CASE WHEN is_from_me=1 THEN timestamp END) "
            "FROM messages WHERE timestamp >= ? GROUP BY chat_jid",
            (cutoff,),
        ).fetchall()
        return {
            r[0]: {"last_all": r[1] or "", "n_all": r[2] or 0,
                   "n_mine": r[3] or 0, "last_mine": r[4] or ""}
            for r in rows if r[0]
        }
    except Exception:
        return {}
    finally:
        con.close()


def contact_info() -> dict:
    """{jid: (saved_name, business_name)} from the whatsmeow contact store."""
    con = _ro(WHATSAPP_DB_PATH)
    if con is None:
        return {}
    try:
        rows = con.execute(
            "SELECT their_jid, COALESCE(NULLIF(full_name,''), NULLIF(first_name,''), "
            "NULLIF(push_name,''), ''), COALESCE(business_name,'') FROM whatsmeow_contacts"
        ).fetchall()
        return {r[0]: (r[1] or "", r[2] or "") for r in rows if r[0]}
    except Exception:
        return {}
    finally:
        con.close()


def mentioned_chats(days: int = 7) -> set:
    try:
        import mechat

        phone, lid, _src = mechat.own_identity()
    except Exception:
        phone, lid = None, ""
    own = [x for x in (phone, lid) if x]
    if not own:
        return set()
    cutoff = (mstate.now_local() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    con = _ro(MESSAGES_DB_PATH)
    if con is None:
        return set()
    try:
        rows = con.execute(
            "SELECT chat_jid, content FROM messages "
            "WHERE timestamp >= ? AND is_from_me=0 AND content LIKE '%@%'",
            (cutoff,),
        ).fetchall()
        out = set()
        for jid, content in rows:
            if jid and any(f"@{x}" in (content or "") for x in own):
                out.add(jid)
        return out
    except Exception:
        return set()
    finally:
        con.close()


def _dm_is_bot(jid: str, business: str, mine: int) -> bool:
    if business and mine == 0:
        return True
    for _sender, _ts, content in active_senders(jid, window_days=7):
        if OTP_RE.search(content or ""):
            return True
    return False


def _set_status(conn, chat_jid: str, state: str, reason: str, evidence: list, now: str) -> None:
    conn.execute(
        "INSERT INTO watch_status (chat_jid, state, reason, evidence, updated_at, weight) "
        "VALUES (?,?,?,?,?,0) "
        "ON CONFLICT(chat_jid) DO UPDATE SET state=excluded.state, reason=excluded.reason, "
        "evidence=excluded.evidence, updated_at=excluded.updated_at",
        (chat_jid, state, reason, json.dumps([e for e in evidence if e]), now),
    )


def adjust_weight(chat_jid: str, delta: float) -> None:
    c = mstate.connect()
    try:
        c.execute(
            "UPDATE watch_status SET weight=COALESCE(weight,0)+? WHERE chat_jid=?",
            (delta, chat_jid),
        )
        c.commit()
    finally:
        c.close()


def chat_weight(chat_jid: str) -> float:
    c = mstate.connect()
    try:
        row = c.execute("SELECT weight FROM watch_status WHERE chat_jid=?", (chat_jid,)).fetchone()
        return float(row["weight"] or 0) if row else 0.0
    finally:
        c.close()


def low_weight(chat_jid: str) -> bool:
    return chat_weight(chat_jid) < LOW_WEIGHT


def _decay_weights() -> None:
    now = mstate.now_local()
    last = mstate.get_watermark("weights", "last_decay")
    if last:
        try:
            last_dt = datetime.datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=mstate.local_tz())
            if (now - last_dt).days < 7:
                return
        except (TypeError, ValueError):
            pass
    c = mstate.connect()
    try:
        c.execute("UPDATE watch_status SET weight=COALESCE(weight,0)*0.8")
        c.commit()
    finally:
        c.close()
    mstate.set_watermark("weights", "last_decay", now.isoformat())


def compute_watch_status(use_model=False) -> dict:
    """Full recompute of watched chats (groups and DMs) with reasons."""
    _decay_weights()
    archived = archived_jids()
    activity = chat_activity()
    contacts = contact_info()
    mentioned = mentioned_chats(7)
    names = {}
    con_names = _ro(MESSAGES_DB_PATH)
    if con_names is not None:
        try:
            names = {r[0]: (r[1] or "") for r in
                     con_names.execute("SELECT jid, name FROM chats").fetchall()}
        except Exception:
            names = {}
        finally:
            con_names.close()
    try:
        import mechat

        own_chats = set(mechat.mechat_jids())
    except Exception:
        own_chats = set()
    now = mstate.now_local()
    now_iso = now.isoformat()
    cutoff14 = (now - timedelta(days=mstate.WATCH_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff30 = (now - timedelta(days=mstate.RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")

    watched, excluded, explicit = [], [], set()
    overrides = _load_overrides()

    con = mstate.connect()
    try:
        for jid in sorted(set(activity) | set(overrides)):
            if not jid or jid in own_chats:
                continue
            chat_name = names.get(jid) or (
                "" if _is_group(jid) else contacts.get(jid, ("", ""))[0])
            if agent_config.is_excluded_chat(jid, chat_name):
                excluded.append({"jid": jid, "reason": "excluded"})
                _set_status(con, jid, "excluded", "excluded", [], now_iso)
                continue
            ov = overrides.get(jid)
            if ov and ov["kind"] == "mute" and _in_window(ov):
                excluded.append({"jid": jid, "reason": "muted"})
                _set_status(con, jid, "excluded", "muted", [], now_iso)
                continue
            if ov and ov["kind"] == "watch":
                explicit.add(jid)
                watched.append(jid)
                _set_status(con, jid, "watched", "override: watch", [], now_iso)
                continue
            if _is_static_excluded(jid):
                excluded.append({"jid": jid, "reason": "broadcast or channel"})
                _set_status(con, jid, "excluded", "broadcast", [], now_iso)
                continue
            if jid in archived and not agent_config.include_archived():
                excluded.append({"jid": jid, "reason": "archived"})
                _set_status(con, jid, "excluded", "archived", [], now_iso)
                continue

            if _is_group(jid):
                a = activity.get(jid) or {}
                last_mine = a.get("last_mine") or ""
                posted = (a.get("n_mine") or 0) > 0 and last_mine >= cutoff14
                if not posted and jid not in mentioned:
                    excluded.append({"jid": jid, "reason": "not active in the last 14 days"})
                    _set_status(con, jid, "excluded", "inactive", [], now_iso)
                    continue
                ev = bot_evidence(jid, use_model=use_model)
                if ev.get("verdict") == "bot":
                    excluded.append({"jid": jid, "reason": "bot", "evidence": ev})
                    _set_status(con, jid, "excluded", "bot", [ev], now_iso)
                    continue
                watched.append(jid)
                reason = "posted" if posted else "mentioned"
                _set_status(con, jid, "watched", reason,
                            [ev] if ev.get("verdict") in ("suspected", "borderline") else [], now_iso)
                continue

            # Direct message
            a = activity.get(jid)
            if not a or (a.get("last_all") or "") < cutoff30:
                excluded.append({"jid": jid, "reason": "inactive DM"})
                _set_status(con, jid, "excluded", "inactive", [], now_iso)
                continue
            mine = a.get("n_mine") or 0
            mine_recent = mine > 0 and (a.get("last_mine") or "") >= cutoff30
            saved_name, business = contacts.get(jid, ("", ""))
            if not (mine_recent or saved_name):
                excluded.append({"jid": jid, "reason": "no two-way activity"})
                _set_status(con, jid, "excluded", "one_way", [], now_iso)
                continue
            if _dm_is_bot(jid, business, mine):
                excluded.append({"jid": jid, "reason": "bot or business"})
                _set_status(con, jid, "excluded", "bot", [], now_iso)
                continue
            watched.append(jid)
            _set_status(con, jid, "watched", "dm", [], now_iso)

        inferred_groups = [j for j in watched if _is_group(j) and j not in explicit]
        inferred_dms = [j for j in watched if _is_dm(j) and j not in explicit]
        for lst, cap in ((inferred_groups, SOFT_CAP_GROUPS), (inferred_dms, SOFT_CAP_DMS)):
            if len(lst) <= cap:
                continue
            ranked = sorted(
                lst,
                key=lambda j: ((activity.get(j) or {}).get("n_mine", 0)
                               + (activity.get(j) or {}).get("n_all", 0),
                               (activity.get(j) or {}).get("last_all") or ""),
                reverse=True,
            )
            for jid in ranked[cap:]:
                watched.remove(jid)
                excluded.append({"jid": jid, "reason": "over soft cap"})
                _set_status(con, jid, "excluded", "over_soft_cap", [], now_iso)
        _set_status_cleanup(con, set(watched) | {e["jid"] for e in excluded})
        con.commit()
    finally:
        con.close()
    return {"watched": watched, "excluded": excluded}


def _load_overrides() -> dict:
    c = mstate.connect()
    try:
        rows = c.execute("SELECT chat_jid, kind, until FROM overrides").fetchall()
        return {
            r["chat_jid"]: {"kind": r["kind"], "until": r["until"] or ""}
            for r in rows
        }
    finally:
        c.close()


def _in_window(ov) -> bool:
    until = ov.get("until") or ""
    if not until:
        return True
    dt = _parse_ts(until)
    if not dt:
        return True
    return dt > _parse_ts(mstate.now_local().isoformat())


def muted_jids() -> set:
    c = mstate.connect()
    try:
        rows = c.execute("SELECT chat_jid, until FROM overrides WHERE kind='mute'").fetchall()
        return {
            r["chat_jid"] for r in rows if _in_window({"until": r["until"]})
        }
    finally:
        c.close()


def set_override(chat_jid: str, kind: str, until: str = "") -> None:
    c = mstate.connect()
    try:
        c.execute(
            "INSERT OR REPLACE INTO overrides (chat_jid, kind, until, created_at) VALUES (?,?,?,?)",
            (chat_jid, kind if kind in ("watch", "mute") else "mute", until or "",
             mstate.now_local().isoformat()),
        )
        c.commit()
    finally:
        c.close()


def set_override_by_name(name: str, kind: str, dur: str = "forever") -> dict:
    jid, candidates = resolve_chat(name)
    if not jid:
        return {"ok": False, "candidates": candidates}
    until = ""
    if dur and dur.endswith("d"):
        try:
            days = int(dur[:-1])
            until = (mstate.now_local() + timedelta(days=days)).isoformat()
        except ValueError:
            until = ""
    if kind == "mute":
        set_override(jid, "mute", until)
        adjust_weight(jid, -5)
    else:
        set_override(jid, "watch", "")
        adjust_weight(jid, 2)
    return {"ok": True, "jid": jid}


def _set_status_cleanup(conn, known: set) -> None:
    rows = conn.execute("SELECT chat_jid FROM watch_status").fetchall()
    for (row_jid,) in rows:
        if row_jid not in known:
            conn.execute("DELETE FROM watch_status WHERE chat_jid=?", (row_jid,))


def watched_chats() -> list:
    """Live watcher view: every watched chat (groups and DMs) minus mutes."""
    try:
        con = mstate.connect()
        rows = con.execute("SELECT chat_jid FROM watch_status WHERE state='watched'").fetchall()
        con.close()
        watched = [r["chat_jid"] for r in rows]
    except Exception:
        watched = []
    muted = muted_jids()
    return [j for j in watched if j not in muted]


def watched_groups() -> list:
    return [j for j in watched_chats() if _is_group(j)]


def watched_dms() -> list:
    return [j for j in watched_chats() if _is_dm(j)]


def excluded_report() -> list:
    c = mstate.connect()
    try:
        rows = c.execute(
            "SELECT chat_jid, state, reason, evidence, updated_at FROM watch_status "
            "ORDER BY state, updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()
