#!/usr/bin/env python3
"""router.py — the Me-chat free-text router (pattern from the hourlyB
reference). Deterministic first, model second:

- exact high-frequency phrases ("top 10", "done 3", "mute OKRAK 1d",
  "send", "send 2", "skip", "more", "help") match in code with no LLM;
- everything else goes to one constrained intent-parse call returning
  strict JSON; if it fails, the router asks a short clarifying question
  instead of guessing.

Approval discipline: a draft is sent ONLY on an explicit scoped "send"
(quote-reply counts when stored) — bare "yes"/"ok" never fire a send.
With a draft pending and unclear intent, the router asks:
"Send the draft to <group>? Reply send to confirm."
"""

import re

import alerts
import mstate


def deterministic_intent(text: str, quoted_alert_id=None) -> dict:
    """Returns {} when it is not a deterministic phrase."""
    body = (text or "").strip()
    low = body.lower().strip()

    if not low:
        return {}

    # Answering a "which group?" question with a number.
    m_num = re.fullmatch(r"(?:group\s*)?(\d{1,2})", low)
    if m_num:
        import qa

        choice = qa.get_choice()
        cands = choice.get("candidates") or []
        idx = int(m_num.group(1)) - 1
        if cands and 0 <= idx < len(cands):
            return {"action": "qa_choice", "jid": cands[idx]["jid"],
                    "name": cands[idx]["name"], "question": choice.get("question", ""),
                    "mention": choice.get("mention", ""), "raw": body}

    m = re.fullmatch(r"send(?:\s+(\d{1,2}))?", low)
    if m:
        n = int(m.group(1)) if m.group(1) else 0
        alert = alerts.find_open_by_number(n) if n else alerts.latest_open()
        if alert is None:
            return {"action": "reply_only", "reply": "◆ No open draft to send right now."}
        return {"action": "send_draft", "alert": alert, "scope": "explicit-send"}

    if re.fullmatch(r"(top ?10|top ?ten|today'?s top ?10)", low):
        return {"action": "top10_now"}

    if re.fullmatch(r"(?:done|dismissed?)\s+(\d{1,2}(?:\s*,\s*\d{1,2})*|all)", low):
        return {"action": "top10_dismiss", "arg": low}

    if re.fullmatch(r"context\s+\d{1,2}", low):
        return {"action": "top10_context", "arg": low}

    if re.fullmatch(r"skip(?:\s+(\d{1,2}))?", low):
        return {"action": "alert_skip", "n": int(re.fullmatch(r"skip(?:\s+(\d{1,2}))?", low).group(1) or 1)}

    if low == "more":
        return {"action": "more"}

    if low in ("help", "?"):
        return {"action": "help"}

    if low.startswith("feedback"):
        return {"action": "feedback", "arg": body[len("feedback"):].strip(" :—-")}

    if low == "status":
        return {"action": "status"}

    if re.fullmatch(r"(different group|not that one|change group)", low):
        return {"action": "qa_change_group"}

    if re.fullmatch(r"forget that nickname", low):
        return {"action": "qa_forget_alias"}

    m = re.fullmatch(r"(?P<verb>mute|watch)\s+(?P<target>.+?)(?:\s+(?P<dur>\d+d|forever))?\s*$", low)
    if m:
        return {
            "action": "watch_override",
            "verb": m.group("verb"),
            "target": m.group("target"),
            "dur": m.group("dur") or "forever",
        }

    return {}


def _needs_summarize_clarify(text: str, forced_group: str = "") -> bool:
    """Bare 'summarize this' clarifies only when no chat is selected/quoted."""
    low = (text or "").strip().lower()
    if not re.fullmatch(r"(?:summari[sz]e|tl;?dr)(?: this| that)?[.!?]*", low):
        return False
    if forced_group:
        return False
    try:
        import qa

        return not qa.selection_active(qa.get_selection())
    except Exception:
        return True


def route(text: str, quoted_alert_id=None, forced_group: str = "", trace_id: str = "") -> dict:
    """Router main entry. Deterministic commands first, then the qa flow."""
    det = deterministic_intent(text, quoted_alert_id=quoted_alert_id)
    if det:
        return det

    low = (text or "").strip().lower()
    if _needs_summarize_clarify(text, forced_group):
        return {"action": "clarify",
                "reply": "◆ Reply to the message you want summarized."}
    try:
        import qa

        sel = qa.get_selection()
        correction = bool(re.match(r"^(no|not|wrong|different)\b", low))
        exclude = sel.get("chat_jid", "") if (correction and sel) else ""
        if correction and exclude:
            qa.clear_selection()
        bare = low in mstate.YES_WORDS
        if not bare:
            out = qa.answer(text, forced_group=forced_group, exclude_jid=exclude, trace_id=trace_id)
            if out is not None and out.get("kind") == "failed":
                return {"action": "qa_failed", "reason": out.get("reason") or "bad_output",
                        "question": text, "forced_group": forced_group, "trace_id": trace_id}
            if out is not None and out.get("kind") == "question":
                if out.get("decision") == "ambiguous":
                    return {"action": "qa_ambiguous", "reply": out.get("reply", ""), "trace_id": trace_id}
                if out.get("decision") == "unresolved":
                    return {"action": "qa_unresolved", "reply": out.get("reply", ""), "trace_id": trace_id}
                return {"action": "qa_answer", "reply": out.get("reply", ""),
                        "jid": out.get("jid", ""), "name": out.get("name", ""),
                        "turn_key": out.get("turn_key", ""), "expert": out.get("expert", ""),
                        "draft": out.get("draft", ""), "question": text, "trace_id": trace_id}
            if out is not None and out.get("kind") == "action":
                act = out.get("action")
                if act == "note_only":
                    return {"action": "note_only"}
                if act == "legacy_delegate":
                    task = out.get("task")
                    txt = out.get("text") or text
                    if task == "send":
                        alert = alerts.latest_open()
                        if alert is None:
                            return {"action": "reply_only", "reply": "◆ No open draft to send right now."}
                        return {"action": "send_draft", "alert": alert, "scope": "explicit-send"}
                    if task == "alert_action":
                        if re.search(r"\bskip\b", txt, re.I):
                            alert = alerts.latest_open()
                            if alert:
                                return {"action": "alert_skip", "n": 1}
                        return {"action": "alert_edit_reply", "text": txt}
                    det2 = deterministic_intent(txt)
                    if det2:
                        return det2
                    return {"action": "clarify", "reply": "◆ I couldn't map that to a command."}
            if out is not None and out.get("kind") == "not_question":
                return {"action": "note_only"}
    except Exception as exc:
        mstate.event(trace_id, "route", "error",
                     f"{type(exc).__name__}: {str(exc)[:180]}")
        return {"action": "clarify",
                "reply": "◆ I hit an error answering that. Try again, or name the chat."}

    return {"action": "clarify", "reply": "◆ I couldn't answer that just now. Try again in a minute."}
