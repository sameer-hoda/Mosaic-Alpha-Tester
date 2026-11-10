#!/usr/bin/env python3
"""worker.py — the single long-running engine process.

ALWAYS RUNNING (forever loop, one process): Me-chat ingest, outbox drain,
question retries and the Radar. The Radar scans every 2 hours between
08:00 and 20:00 IST; the 08:00 scan is the morning brief. Every job is gated by watermarks in engine.db, so a
restart or laptop wake-up catches up without re-reading full history.

CLI (for the Go app to call):
    python3 worker.py serve                 # forever loop
    python3 worker.py tick <job>            # run one job now, then exit
    jobs: ingest | radar | retention | drain
"""

import datetime
import json
import os
import sys

import alerts
import budget
import conversation
import engine
import followup
import mechat
import mstate
import outbox
import router
import top10
import watch_rules
import watcher

HELP_TEXT = (
    "◆ I watch your groups for asks, deadlines and personal threads, and answer "
    "questions about them.\n"
    "• ask: what's on / what's pending in <group>?\n"
    "• send (or send N) — always explicit; a bare “yes” never sends anything\n"
    "• N = done / context N / more / skip N\n"
    "• mute <group> [Nd|forever] / watch <group>\n"
    "• top 10 (right now) / status / feedback <words>\n"
    "• voice notes to me become text\n"
    "Conservative by design: watched chats only, a strict daily budget of pings, "
    "and the Radar only between 08:00 and 20:00 IST."
)


def _mark(job: str):
    mstate.job_mark_ran(job)


def _job_ts(job: str) -> str:
    return mstate.job_last_ran(job) or ""


def _budget_ok() -> bool:
    try:
        budget.assert_room()
        return True
    except budget.BudgetPause:
        return False


def _sync_get() -> dict:
    """Read the bridge's sync state; fall back to the loopback status API."""
    try:
        with open(os.path.join(mstate.STORE_DIR, "sync_state.json"), "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if state.get("ready"):
            return state
    except Exception:
        pass
    try:
        import urllib.request

        token = ""
        token_path = os.path.join(mstate.STORE_DIR, "bridge_token")
        if os.path.exists(token_path):
            token = open(token_path, "r", encoding="utf-8").read().strip()
        req = urllib.request.Request("http://127.0.0.1:8090/api/status",
                                     headers={"X-Bridge-Token": token})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
        return data.get("sync") or {}
    except Exception:
        return {}


def _bridge_ready() -> bool:
    return bool(_sync_get().get("ready"))


def _sync_progress() -> int:
    try:
        return int(_sync_get().get("progress") or 0)
    except (TypeError, ValueError):
        return 0


RETRY_MINUTES = (1, 5, 15)


def _qa_failure_text(reason: str) -> str:
    if reason == "bad_key":
        return "◆ Your Gemini key was rejected — run `nembu` to update it. 🍋"
    if reason == "budget":
        return "◆ Daily AI budget reached — I'll answer after midnight. 🍋"
    if reason == "no_chats":
        return "◆ I don't have any group history yet — try again once sync settles. 🍋"
    if reason == "bad_model":
        return "◆ The configured Gemini model is unavailable — re-run `nembu` to refresh setup. 🍋"
    if reason in ("rate_limited", "network", "timeout", "bad_output", "http_error"):
        return "◆ Gemini is rate-limiting right now — I'll retry and answer here in about a minute."
    return "◆ I couldn't answer that just now — please ask again in a minute."


def _is_retryable(reason: str) -> bool:
    return reason in {"rate_limited", "network", "timeout", "bad_output", "http_error", "syncing"}


def _queue_retry(msg_id: str, text: str, forced_group: str, reason: str, delay_min: int) -> None:
    now = mstate.now_local()
    c = mstate.connect()
    try:
        c.execute(
            "INSERT INTO retry_questions (msg_id, text, forced_group, attempts, next_at, reason, created_at) "
            "VALUES (?,?,?,0,?,?,?) "
            "ON CONFLICT(msg_id) DO UPDATE SET next_at=excluded.next_at, reason=excluded.reason",
            (msg_id or f"anon:{text[:40]}", text, forced_group or "",
             (now + datetime.timedelta(minutes=delay_min)).isoformat(), reason, now.isoformat()),
        )
        c.commit()
    finally:
        c.close()


def _queue_syncing(msg_id: str, text: str, forced_group: str = "") -> None:
    _queue_retry(msg_id, text, forced_group, "syncing", 1)
    pct = _sync_progress()
    _reply(f"◆ Still syncing your chats ({pct}%). I'll answer this as soon as "
           f"your last 30 days are in.", trace_id=msg_id)


def _delete_retry(msg_id: str) -> None:
    c = mstate.connect()
    try:
        c.execute("DELETE FROM retry_questions WHERE msg_id=?", (msg_id,))
        c.commit()
    finally:
        c.close()


def _process_retries(now=None) -> int:
    now = now or mstate.now_local()
    ready = _bridge_ready()
    c = mstate.connect()
    try:
        rows = c.execute(
            "SELECT msg_id, text, forced_group, attempts, reason FROM retry_questions "
            "WHERE next_at <= ? ORDER BY created_at LIMIT 10",
            (now.isoformat(),),
        ).fetchall()
    finally:
        c.close()
    done = 0
    for row in rows:
        reason = row["reason"] or "bad_output"
        if reason == "syncing" and not ready:
            continue
        attempts = int(row["attempts"] or 0)
        out = None
        if attempts < 3:
            try:
                import qa

                out = qa.answer(row["text"], forced_group=row["forced_group"] or "",
                                trace_id=row["msg_id"])
            except budget.BudgetPause:
                out = {"kind": "failed", "reason": "budget"}
            except Exception:
                out = None

        if out and out.get("kind") == "question" and out.get("reply"):
            _reply(out["reply"], group_jid=out.get("jid", ""), link_kind="answer",
                   link_ref=out.get("turn_key", ""), trace_id=row["msg_id"], question=row["text"])
            _delete_retry(row["msg_id"])
            done += 1
            continue
        if out and out.get("kind") == "action":
            _execute(out)
            _delete_retry(row["msg_id"])
            done += 1
            continue

        new_reason = (out or {}).get("reason") if isinstance(out, dict) else ""
        new_reason = new_reason or ("" if reason == "syncing" else reason) or "bad_output"
        attempts += 1
        if not _is_retryable(new_reason) or attempts >= 3:
            if _is_retryable(new_reason) and attempts >= 3:
                _reply(f"◆ Couldn't answer '{row['text'][:60]}' — please ask again later.")
            else:
                _reply(_qa_failure_text(new_reason))
            _delete_retry(row["msg_id"])
            continue
        delay = 5 if attempts == 1 else 15
        c = mstate.connect()
        try:
            c.execute(
                "UPDATE retry_questions SET attempts=?, next_at=?, reason=? WHERE msg_id=?",
                (attempts,
                 (mstate.now_local() + datetime.timedelta(minutes=delay)).isoformat(),
                 new_reason, row["msg_id"]),
            )
            c.commit()
        finally:
            c.close()
    return done

def tick_ingest(now=None) -> dict:
    """New Me-chat turns -> voice transcription -> router -> side effects.

    A persisted ingest watermark makes replays impossible: history sync can
    stream in for minutes after boot, and a restart must not answer old
    messages. Turns older than the watermark are stored silently (they remain
    queryable), never routed."""
    now = now or mstate.now_local()
    wm_raw = mstate.get_watermark("mechat", "ingest")
    wm = _parse_ts(wm_raw) if wm_raw else (now - datetime.timedelta(minutes=10))
    if wm is None:
        wm = now - datetime.timedelta(minutes=10)

    _process_voice_retries(now)

    handled = 0
    newest = wm
    for turn in conversation.fetch_new_mechat_turns():
        msg_id = turn.get("msg_id") or ""
        text = (turn.get("body") or "").strip()
        is_voice = bool(turn.get("is_voice"))
        ts = _parse_ts(turn.get("ts"))
        stale = ts is not None and ts < wm
        if stale:
            conversation.store_turn(msg_id, "note", text, transcribed=False)
            continue
        mstate.event(msg_id, "ingest", "ok",
                     ("voice note" if is_voice else "text: " + text[:60]))
        if is_voice and not text:
            text = transcribe_mechat(msg_id)
            if not text:
                # A voice note is a command attempt: acknowledge exactly once.
                # The placeholder turn also dedupes by msg_id, and we advance
                # the watermark — otherwise every tick re-ingests the note and
                # re-sends the ack (the bug behind the 7 duplicate messages).
                placeholder = "[voice note — fetching audio]"
                conversation.store_turn(msg_id, "note", placeholder, transcribed=False)
                if ts is not None and ts > newest:
                    newest = ts
                import voice as _v

                mstate.event(msg_id, "voice.queue", "warn",
                             "audio not available yet (" + (_v.LAST_ERROR or "fetch failed") + ") — queued")
                _queue_voice_retry(msg_id, mechat.get_mechat_jid(), turn.get("ts"))
                mstate.set_watermark("mechat", "ingest", newest.isoformat())
                _reply("◆ Got your voice note — WhatsApp is re-uploading the audio; "
                       "I'll reply in a moment.")
                continue
        quoted_id = turn.get("quoted_id") or ""
        quoted_sender = turn.get("quoted_sender") or ""
        conversation.store_turn(msg_id, "note", text, transcribed=is_voice,
                                raw={"quoted_id": quoted_id, "quoted_sender": quoted_sender})
        if ts is not None and ts > newest:
            newest = ts
        if not text:
            continue
        forced = ""
        if quoted_id:
            try:
                _jid, _name = conversation.resolve_quoted_chat(quoted_id)
                forced = _jid or ""
            except Exception:
                forced = ""
        if not _bridge_ready():
            # Before the last 30 days are in, only explicit commands work.
            # Anything that might be a question is stored and answered later.
            det = router.deterministic_intent(text)
            allowed = {
                "help", "status", "more", "send_draft", "alert_skip",
                "alert_edit_reply", "watch_override", "feedback", "top10_now",
                "top10_dismiss", "top10_context", "qa_change_group", "qa_forget_alias",
            }
            if det and det.get("action") in allowed:
                _execute(det)
            else:
                _queue_syncing(msg_id, text, forced)
            continue
        handled += 1
        try:
            decision = router.route(text, forced_group=forced, trace_id=msg_id)
            mstate.event(msg_id, "route", "ok", str(decision.get("action") or "?"))
        except Exception as exc:
            _reply(f"⚠️ Could not process that: {str(exc)[:90]}")
            continue
        _execute(decision)
    mstate.set_watermark("mechat", "ingest", newest.isoformat())
    alerts.expire_stale()
    outbox.drain()
    return {"handled": handled}


def _queue_voice_retry(msg_id: str, chat_jid: str, ts) -> None:
    if not msg_id or not chat_jid:
        return
    now = mstate.now_local()
    c = mstate.connect()
    try:
        c.execute(
            "INSERT OR REPLACE INTO voice_pending (msg_id, chat_jid, ts, attempts, next_at, created_at) "
            "VALUES (?,?,?,1,?,?)",
            (msg_id, chat_jid, str(ts or ""), (now + datetime.timedelta(seconds=10)).isoformat(),
             now.isoformat()),
        )
        c.commit()
    finally:
        c.close()


def _process_voice_retries(now=None) -> int:
    """WhatsApp media URLs are often stale (403); retry a handful of times
    with backoff, then tell the user instead of staying silent."""
    now = now or mstate.now_local()
    import voice

    done = 0
    c = mstate.connect()
    try:
        rows = c.execute(
            "SELECT msg_id, chat_jid, attempts FROM voice_pending WHERE next_at <= ? "
            "ORDER BY created_at LIMIT 3",
            (now.isoformat(),),
        ).fetchall()
    finally:
        c.close()
    for row in rows:
        msg_id, chat_jid, attempts = row["msg_id"], row["chat_jid"], row["attempts"] or 1
        mstate.event(msg_id, "voice.retry", "info", f"attempt {attempts}")
        import time as _time

        _t0 = _time.time()
        text, _ok = voice.transcribe_mechat_voice(msg_id, chat_jid)
        import voice as _v2

        mstate.event(msg_id, "voice.transcript", "ok" if text else "error",
                     (text[:160] if text else (_v2.LAST_ERROR or "could not read audio")),
                     dur_ms=int((_time.time() - _t0) * 1000))
        c = mstate.connect()
        try:
            if text:
                c.execute("DELETE FROM voice_pending WHERE msg_id=?", (msg_id,))
                c.commit()
            else:
                attempts += 1
                if attempts > 2:
                    c.execute("DELETE FROM voice_pending WHERE msg_id=?", (msg_id,))
                else:
                    delay = min(60, 10 * (2 ** (attempts - 1)))
                    c.execute(
                        "UPDATE voice_pending SET attempts=?, next_at=? WHERE msg_id=?",
                        (attempts, (now + datetime.timedelta(seconds=delay)).isoformat(), msg_id),
                    )
                c.commit()
        finally:
            c.close()
        if text:
            done += 1
            forced = ""
            try:
                import sqlite3

                import engine

                con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
                try:
                    q = con.execute("SELECT COALESCE(quoted_id,'') FROM messages WHERE id=?",
                                    (msg_id,)).fetchone()
                finally:
                    con.close()
                if q and q[0]:
                    _g, _n = conversation.resolve_quoted_group(q[0])
                    forced = _n or ""
            except Exception:
                forced = ""
            try:
                c = mstate.connect()
                try:
                    c.execute("UPDATE turns SET text=? WHERE msg_id=?", (text, msg_id))
                    c.commit()
                finally:
                    c.close()
                _execute(router.route(text, forced_group=forced))
            except Exception as exc:
                _reply(f"⚠️ Could not process that: {str(exc)[:90]}")
        elif attempts > 2:
            mstate.event(msg_id, "voice.fail", "error", "audio not retrievable")
            _reply("◆ I can't retrieve that voice note — WhatsApp says the audio is no "
                   "longer available. Voice notes sent from your phone to yourself are "
                   "often blocked from the linked device. Type the question, or forward "
                   "the voice note here and I'll try that.",
                   link_kind="clarify", trace_id=msg_id)
    return done


def _chats_count() -> int:
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        try:
            return int(con.execute("SELECT COUNT(*) FROM chats").fetchone()[0])
        finally:
            con.close()
    except Exception:
        return 0


def _parse_ts(raw) -> datetime.datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=mstate.local_tz())
    return dt


def transcribe_mechat(msg_id: str) -> str:
    import voice

    me = mechat.get_mechat_jid()
    if not me:
        return ""
    transcript, _ok = voice.transcribe_mechat_voice(msg_id, me)
    return (transcript or "").strip()


def _reply(text: str, group_jid: str = "", link_kind: str = "reply", link_ref: str = "",
           trace_id: str = "", question: str = ""):
    me = mechat.get_mechat_jid()
    if me and text:
        # A reply is a distinct event every time: same text twice (e.g. two
        # "nothing more" answers) must still be delivered. The nonce keeps
        # the outbox dedupe key unique without weakening at-least-once.
        import uuid

        outbox.enqueue(
            "reply", f"reply:{uuid.uuid4().hex[:12]}:{text[:40]}", me, text,
            ref_chat=group_jid or None,
            meta={"link_kind": link_kind, "group_jid": group_jid, "link_ref": link_ref,
                  "trace_id": trace_id, "question": question},
        )


def _execute(decision: dict):
    action = decision.get("action") or ""
    if action == "reply_only":
        _reply(decision.get("reply", ""))
    elif action == "send_draft":
        _do_send(decision.get("alert"))
    elif action == "top10_now":
        _do_top10_now()
    elif action == "top10_dismiss":
        _reply(top10.apply_dismissal(decision.get("arg", "")))
    elif action == "top10_context":
        _reply("(kept) " + top10.apply_dismissal(decision.get("arg", "")))
    elif action == "alert_skip":
        _reply(_do_skip(decision.get("n") or 1))
    elif action == "alert_edit_reply":
        _reply(_do_edit(decision.get("text", "")))
    elif action == "qa_answer":
        _reply(decision.get("reply", ""), group_jid=decision.get("jid", ""),
               link_kind="answer", link_ref=decision.get("turn_key", ""),
               trace_id=decision.get("trace_id", ""), question=decision.get("question", ""))
    elif action in ("qa_ambiguous", "qa_unresolved"):
        _reply(decision.get("reply", ""), link_kind="clarify",
               trace_id=decision.get("trace_id", ""))
    elif action == "qa_choice":
        import qa
        import qa_profiles

        jid = decision.get("jid", "")
        if jid:
            mention = decision.get("mention") or decision.get("name") or ""
            try:
                qa_profiles.add_confirmed_alias(jid, mention)
            except Exception:
                pass
        question = decision.get("question", "")
        try:
            out = qa.answer(question, forced_group=jid)
        except Exception:
            out = None
        if out and out.get("kind") == "question" and out.get("reply"):
            _reply(out["reply"], group_jid=out.get("jid", ""), link_kind="answer",
                   link_ref=out.get("turn_key", ""))
        else:
            _reply("◆ I couldn't answer that just now — try again in a minute.")
    elif action == "qa_change_group":
        import qa

        qa.clear_selection()
        _reply("◆ Okay — which group do you mean?")
    elif action == "qa_forget_alias":
        import qa
        import qa_profiles

        sel = qa.get_selection()
        removed = qa_profiles.forget_last_alias(sel.get("chat_jid", "")) if sel else ""
        if removed:
            _reply(f"◆ Forgotten — {removed!r} no longer selects that group.")
        else:
            _reply("◆ There is no saved nickname to forget for this group.")
    elif action == "choice":
        import qa

        c = mstate.connect()
        try:
            c.execute("DELETE FROM pending_choice WHERE id=1")
            c.commit()
        finally:
            c.close()
        try:
            out = qa.answer(decision.get("question", ""), forced_group=decision.get("jid", ""))
            if out and out.get("reply"):
                _reply(out.get("reply", ""), group_jid=out.get("jid", ""),
                       link_kind="answer", link_ref=out.get("turn_key", ""))
            else:
                _reply("◆ I couldn't answer that just now — try again in a minute.")
        except Exception as exc:
            _reply(f"⚠️ Could not answer that: {str(exc)[:90]}")
    elif action == "inquiry":
        _do_inquiry(decision.get("question", ""), trace_id=decision.get("trace_id", ""))
    elif action == "watch_override":
        import watch_rules

        verb = decision.get("verb", "watch")
        target = decision.get("target", "")
        dur = decision.get("dur", "forever")
        out = watch_rules.set_override_by_name(
            target, "mute" if verb == "mute" else "watch", dur
        ) or {}
        if out.get("ok"):
            import alerts as _alerts

            shown = _alerts._chat_name(out.get("jid") or target)
            if verb == "mute":
                _reply(f"◆ Muting *{shown}* ({dur}). It won't ping until it expires.")
            else:
                _reply(f"◆ Watching *{shown}* now.")
        else:
            cands = out.get("candidates") or []
            names = ", ".join(f"*{(c.get('name') or c.get('jid'))[:40]}*" for c in cands[:5])
            _reply(f"◆ I couldn't match \"{target}\" to exactly one group. {('Candidates: ' + names) if names else ''}")
    elif action == "more":
        _has_more, body = conversation.chunk_more()
        _reply(body or "◆ Nothing more right now.")
    elif action == "help":
        _reply(HELP_TEXT)
    elif action == "status":
        _reply(status_text())
    elif action == "note_only":
        pass
    elif action == "feedback":
        txt = (decision.get("arg") or "").strip()
        if txt:
            conversation.store_turn(None, "note", "feedback: " + txt)
            _reply("◆ Noted, thank you.")
        else:
            _reply("◆ What would you change? e.g. feedback shorter pings")
    elif action == "qa_failed":
        reason = decision.get("reason") or "bad_output"
        _reply(_qa_failure_text(reason), trace_id=decision.get("trace_id", ""))
        if _is_retryable(reason):
            _queue_retry(decision.get("trace_id", ""), decision.get("question", ""),
                         decision.get("forced_group", ""), reason, 1)
    elif action == "clarify":
        _reply(decision.get("reply", ""))


def _do_send(alert) -> None:
    if not alert:
        _reply("◆ No open draft to send right now.")
        return
    me = mechat.get_mechat_jid()
    if alert.get("chat_jid") == me:
        _reply("◆ That draft is addressed to me — say “send <group>” for a chat draft.")
        return
    if alert.get("kind") not in ("action", "follow_up") or not (alert.get("draft") or "").strip():
        _reply("◆ That one is for your information only — nothing to send. Say *skip* to clear it.")
        return
    ref = alert.get("dedupe_key") or f"a{alert['id']}"
    outbox.enqueue("alert_send", f"send:{ref}", alert.get("chat_jid") or "",
                   alert.get("draft") or alert.get("headline") or "",
                   ref_chat=alert.get("chat_jid") or "",
                   meta={"alert_id": alert["id"]})
    alerts.set_state(alert["id"], "sent")
    # No "sent" ack here: the one truthful confirmation is WhatsApp's
    # delivery, reported by outbox._on_alert_send_delivered. Saying "sent"
    # before the bridge confirms would be a guess.


def _do_top10_now() -> None:
    try:
        out = top10.run_daily()
    except budget.BudgetPause as pause:
        _reply(f"⚠️ {pause}")
        return
    if out.get("paused"):
        _reply("⚠️ Daily AI budget reached — try again after midnight.")
    elif out.get("skipped") or not out.get("items"):
        _reply("◆ Nothing fresh worth a Top 10 right now.")


def _do_skip(n: int) -> str:
    alert = alerts.find_open_by_number(n)
    if not alert:
        return "◆ Nothing open at that number."
    alerts.set_state(alert["id"], "skipped")
    return "◆ Notification closed, the next time it makes sense."


BARE_ACKS = {"yes", "ok", "okay", "y", "okk", "sure", "done", "hmm", "no", "nope", "nah"}


def _do_edit(instruction: str) -> str:
    if instruction.strip().lower() in BARE_ACKS:
        # "yes" is never permission to send and is not an edit instruction.
        return "◆ I haven't sent anything. Say *send* to send this draft, or tell me what to change."
    alert = alerts.latest_open()
    if not alert or not alert.get("draft"):
        return "◆ No open draft to edit."

    prompt = (
        "Rewrite the user's WhatsApp follow-up draft per the instruction. "
        "Keep it in the user's voice, keep money amounts and deadlines exact, "
        "plain WhatsApp formatting (bold via *), at most 240 chars. Return ONLY "
        f"JSON: {{\"draft\": \"...\"}}.\n\nCurrent draft: {alert['draft']!r}"
        f"\nInstruction: {instruction!r}"
    )
    schema = {"type": "object", "properties": {"draft": {"type": "string"}}, "required": ["draft"]}
    try:
        data = budget.gemini_json(prompt, {"draft": ""}, schema, purpose="alert-edit")
    except budget.BudgetPause:
        return "⚠️ Daily AI budget reached; try again tomorrow."
    new = (data.get("draft") or "").strip()
    if not new:
        return "◆ Couldn't rewrite that — try again."
    alerts.replace_draft(alert["id"], new)
    return f"◆ New draft: {new[:500]}"


def _do_inquiry(question: str, trace_id: str = "") -> None:
    import qa

    try:
        out = qa.answer(question, trace_id=trace_id) or {}
        jid = out.get("jid", "")
        name = out.get("name", "")
        _reply(out.get("reply", "") or "◆ I couldn't answer that just now.",
               group_jid=jid, link_kind="answer",
               link_ref=out.get("turn_key", ""), trace_id=trace_id, question=question)
        if jid and question:
            try:
                qa.set_selection(jid, name or jid, question,
                                 out.get("reply", ""))
            except Exception:
                pass
    except Exception as exc:
        _reply(f"⚠️ Could not answer that: {str(exc)[:90]}", trace_id=trace_id)


def _radar_slots() -> list:
    step = max(1, mstate.RADAR_EVERY_HOURS)
    return list(range(mstate.RADAR_START_HOUR, mstate.RADAR_END_HOUR + 1, step))


def _next_radar(now=None):
    now = now or mstate.now_local()
    slots = _radar_slots()
    if not slots:
        return None
    for h in slots:
        if h > now.hour:
            return now.replace(hour=h, minute=0, second=0, microsecond=0)
    return (now + datetime.timedelta(days=1)).replace(
        hour=slots[0], minute=0, second=0, microsecond=0)


def _last_radar_text() -> str:
    raw = mstate.get_watermark("radar", "last")
    if not raw:
        return "last scan —"
    try:
        d = json.loads(raw)
        at = _parse_ts(d.get("at"))
        clock = at.strftime("%H:%M") if at else "—"
        return f"last {clock} · {d.get('scanned', 0)} chats · {d.get('alerts', 0)} alerts"
    except Exception:
        return "last scan —"


def status_text() -> str:
    pings = mstate.note_pings_status()
    try:
        groups = len(watch_rules.watched_groups() or [])
        dms = len(watch_rules.watched_dms() or [])
    except Exception:
        groups = dms = 0
    nxt = _next_radar()
    nxt_txt = nxt.strftime("%H:%M") if nxt else "—"
    return (f"◆ {pings}. Watching {groups} groups · {dms} DMs. "
            f"Next Radar {nxt_txt}. {_last_radar_text()}.")


def _minutes_since(last_ts: str, now) -> float:
    try:
        dt = datetime.datetime.fromisoformat(last_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=mstate.local_tz())
        return (now - dt).total_seconds() / 60.0
    except (ValueError, TypeError):
        return 999.0


def _refresh_watch_pool(now) -> None:
    last = _job_ts("watch_pool")
    if not last or _minutes_since(last, now) >= 60:
        watch_rules.compute_watch_status(use_model=False)
        _mark("watch_pool")


def tick_radar(now=None) -> dict:
    """One scheduled Radar slot. Never runs a burst after sleep: only the
    most recent missed slot inside the 08:00-20:00 window."""
    now = now or mstate.now_local()
    slots = _radar_slots()
    if not slots:
        return {"skipped": "no-slots"}
    if now.hour < mstate.RADAR_START_HOUR or now.hour > mstate.RADAR_END_HOUR:
        return {"skipped": "outside-window"}
    due = max(h for h in slots if h <= now.hour)
    key = f"{mstate.local_date_key(now)}:{due:02d}"
    if mstate.get_watermark("radar", key):
        return {"skipped": "already-ran", "slot": due}
    if not _budget_ok():
        return {"paused": True, "slot": due}

    # Claim the slot before any work so a restart cannot burst.
    mstate.set_watermark("radar", key, "started")
    results = {"slot": due}
    try:
        _refresh_watch_pool(now)
        if due == slots[0]:
            results["watch"] = watcher.run_cycle(window_note="overnight since 20:00")
            results["digest"] = top10.run_daily(now)
        else:
            results["watch"] = watcher.run_cycle(
                window_note="since the last scan (about 2 hours)")
            if due in (12, 18):
                results["sweep"] = followup.run_sweep(now)
    except budget.BudgetPause as pause:
        results["paused"] = str(pause)
    except Exception as exc:
        results["error"] = str(exc)[:160]
    outbox.drain()

    watch = results.get("watch") if isinstance(results.get("watch"), dict) else {}
    scanned = int(watch.get("groups_seen") or 0)
    alerts_n = int(watch.get("alerts") or 0)
    summary = {"at": now.isoformat(), "slot": due,
               "scanned": scanned, "alerts": alerts_n}
    mstate.set_watermark("radar", key, "done")
    mstate.set_watermark("radar", "last", json.dumps(summary))
    results["scanned"] = scanned
    results["alerts"] = alerts_n
    return results


def tick_cards(now=None) -> dict:
    """Build/refresh Q&A group cards in the background (never sends)."""
    now = now or mstate.now_local()
    last = _job_ts("cards")
    if last and _minutes_since(last, now) < 10:
        return {"skipped": "not-due"}
    try:
        import qa

        n = qa.ensure_cards()
    except Exception as exc:
        return {"error": str(exc)[:140]}
    _mark("cards")
    return {"cards": n}


def tick_profiles(now=None) -> dict:
    """Build/refresh group profiles for Q&A. Never sends a message."""
    now = now or mstate.now_local()
    last = _job_ts("profiles")
    if last and _minutes_since(last, now) < 15:
        return {"skipped": "not-due"}
    try:
        import qa_profiles

        out = qa_profiles.ensure_profiles()
    except Exception as exc:
        return {"error": str(exc)[:140]}
    _mark("profiles")
    return out


def tick_drain(now=None) -> dict:
    outbox.drain()
    return {"drained": True}


def _live_chat_jids() -> set:
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{engine.MESSAGES_DB_PATH}?mode=ro", uri=True)
        try:
            return {r[0] for r in con.execute("SELECT jid FROM chats").fetchall() if r[0]}
        finally:
            con.close()
    except Exception:
        return set()


def _rotate_pipeline_log() -> None:
    """Keep pipeline.log to the last 7 days, then cap it at 5 MB."""
    path = os.path.join(mstate.STORE_DIR, "pipeline.log")
    if not os.path.exists(path):
        return
    cutoff = mstate.now_local() - datetime.timedelta(days=7)
    kept = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                try:
                    ts = datetime.datetime.fromisoformat(
                        json.loads(line).get("ts", "").replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=mstate.local_tz())
                    if ts < cutoff:
                        continue
                except Exception:
                    pass
                kept.append(line)
    except Exception:
        return
    max_bytes = 5 * 1024 * 1024
    size = sum(len(x) + 1 for x in kept)
    while kept and size > max_bytes:
        size -= len(kept.pop(0)) + 1
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            for line in kept:
                fh.write(line + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def tick_retention(now=None) -> dict:
    """Daily prune of engine.db and the pipeline log (03:00 IST or the first
    tick after it). Message history is pruned by the Go bridge."""
    now = now or mstate.now_local()
    if now.hour < 3:
        return {"skipped": "before-window"}
    key = mstate.local_date_key(now)
    if mstate.get_watermark("retention", key):
        return {"skipped": "already-ran"}

    days = mstate.RETENTION_DAYS
    cutoff = (now - datetime.timedelta(days=days)).isoformat()
    c = mstate.connect()
    try:
        for table, col in (
            ("triage", "created_at"),
            ("turns", "created_at"),
            ("top10", "created_at"),
            ("outbox", "created_at"),
            ("sent_links", "sent_at"),
            ("transcripts", "created_at"),
        ):
            c.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
        c.execute(
            "DELETE FROM alerts WHERE state IN ('closed','expired','skipped','sent') "
            "AND created_at < ?", (cutoff,),
        )
        live = _live_chat_jids()
        for row in c.execute("SELECT job, key FROM watermarks").fetchall():
            k = row["key"] or ""
            if "@" in k and k not in live:
                c.execute("DELETE FROM watermarks WHERE job=? AND key=?", (row["job"], k))
        c.commit()
    finally:
        c.close()

    _rotate_pipeline_log()
    mstate.set_watermark("retention", key, "done")
    return {"retention_days": days}


def _ingest_watchdog(now=None, max_lag_s=None) -> dict:
    """Warn when background jobs delay the ingest loop past a safe lag."""
    now = now or mstate.now_local()
    max_lag_s = (float(os.getenv("MOSAIC_INGEST_LAG_S", "120"))
                 if max_lag_s is None else max_lag_s)
    prev = mstate.get_watermark("watchdog", "ingest_ran")
    lag = None
    if prev:
        try:
            lag = (now - datetime.datetime.fromisoformat(prev)).total_seconds()
        except (TypeError, ValueError):
            lag = None
    mstate.set_watermark("watchdog", "ingest_ran", now.isoformat())
    if lag is not None and lag > max_lag_s:
        mstate.event("", "ingest.lag", "warn",
                     f"{int(lag)}s since the last ingest (limit {int(max_lag_s)}s)")
    return {"lag_s": lag, "max_lag_s": max_lag_s}


def run_once(now=None) -> dict:
    tick_ingest()
    watchdog = _ingest_watchdog(now)
    _process_retries()
    if not _bridge_ready():
        return {"skipped": "not-ready", "watchdog": watchdog,
                "drain": tick_drain(), "retention": tick_retention()}
    return {
        "watchdog": watchdog,
        "profiles": tick_profiles(),
        "cards": tick_cards(),
        "radar": tick_radar(now),
        "retention": tick_retention(),
        "drain": tick_drain(),
    }


def _single_instance():
    """Hold an exclusive lock so only one worker drains the outbox."""
    import fcntl
    import os
    try:
        path = os.path.join(mstate.STORE_DIR, "worker.lock")
        fh = open(path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        print("worker already running; exiting")
        return None


def serve(interval: float = 20.0):
    import time

    lock = _single_instance()
    if lock is None:
        return
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"worker error: {exc}")
        time.sleep(interval)


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        serve()
        return 0
    if cmd == "tick":
        job = argv[1] if len(argv) > 1 else ""
        if not _budget_ok() and job == "radar":
            print("budget paused")
            return 1
        fn = {"ingest": tick_ingest, "radar": tick_radar,
              "retention": tick_retention, "drain": tick_drain}.get(job)
        if not fn:
            print(f"unknown job: {job}")
            return 2
        print(fn())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
