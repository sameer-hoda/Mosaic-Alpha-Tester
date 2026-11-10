#!/usr/bin/env python3
"""voice.py — voice notes become text.

Two paths:
  * a voice note sent to the Me chat is transcribed and treated as a turn;
  * voice notes arriving in watched groups are transcribed before triage, so
    an ask that was spoken is not silently missed.

Download is the bridge's token-gated /api/download; transcription is Gemini
audio input (disclosed at onboarding). Transcripts are cached by message id
so a re-scan never pays twice or drifts.
"""

import json
import os
import urllib.request

import budget
import engine
import mstate

_MIME_BY_EXT = {
    ".ogg": "audio/ogg", ".opus": "audio/opus", ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".aac": "audio/aac",
    ".aiff": "audio/aiff", ".aif": "audio/aiff",
}

LAST_ERROR = ""


def _fail(reason: str) -> None:
    global LAST_ERROR
    LAST_ERROR = reason


PROMPT = (
    "Transcribe this voice note verbatim in the language spoken (keep "
    "Hinglish as spoken). No commentary, no timestamps, no speaker labels. "
    "If it is music or noise with no speech, return exactly: [no speech]"
)


def download_voice(chat_jid: str, message_id: str) -> str:
    """Ask the bridge to fetch media for a message; returns local path or ''."""
    from mechat import bridge_token

    url = os.getenv("WA_API_URL", "http://127.0.0.1:8090/api/download")
    req = urllib.request.Request(
        url,
        data=json.dumps({"message_id": message_id, "chat_jid": chat_jid}).encode(),
        headers={"Content-Type": "application/json", "X-Bridge-Token": bridge_token()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                return ""
            data = json.loads(resp.read().decode())
        path = data.get("path") or ""
        return path if path and os.path.exists(path) else ""
    except Exception:
        return ""


def cached(msg_id: str) -> str:
    if not msg_id:
        return ""
    c = mstate.connect()
    try:
        row = c.execute("SELECT text FROM transcripts WHERE msg_id=?", (msg_id,)).fetchone()
        return (row["text"] if row else "") or ""
    finally:
        c.close()


def _store(msg_id: str, chat_jid: str, text: str) -> None:
    if not msg_id or not text:
        return
    c = mstate.connect()
    try:
        c.execute(
            "INSERT OR REPLACE INTO transcripts (msg_id, chat_jid, text, created_at) VALUES (?,?,?,?)",
            (msg_id, chat_jid, text, mstate.now_local().isoformat()),
        )
        c.commit()
    finally:
        c.close()


def transcribe(path: str, msg_id: str = "", chat_jid: str = "") -> tuple:
    """Returns (text, ok). Uses the transcript cache when msg_id is known."""
    if msg_id:
        hit = cached(msg_id)
        if hit:
            return hit, True
    if not path or not os.path.exists(path):
        _fail("audio file not available (download failed or media gone)")
        return "", False
    if not engine.ai_available():
        _fail("AI unavailable (no Gemini key)")
        return "", False
    mime = _MIME_BY_EXT.get(os.path.splitext(path)[1].lower(), "audio/ogg")
    try:
        text = engine.gemini_audio(path, mime, PROMPT)
    except Exception as exc:
        _fail("transcription failed: " + str(exc)[:120])
        return "", False
    text = (text or "").strip()
    if not text or text == "[no speech]":
        _fail("no speech detected in the audio")
        return "", bool(text)
    model = os.getenv("GEMINI_MODEL_AUDIO") or os.getenv("GEMINI_MODEL", "")
    try:
        budget.record(model, PROMPT, text, purpose="voice-transcribe")
    except Exception:
        pass
    _store(msg_id, chat_jid, text)
    return text, True


def transcribe_mechat_voice(msg_id: str, my_jid: str) -> tuple:
    """Voice note sent to self: cache -> download -> transcribe."""
    hit = cached(msg_id)
    if hit:
        return hit, True
    path = download_voice(my_jid, msg_id)
    if not path:
        _fail("WhatsApp would not hand over the audio (403 / media gone)")
        return "", False
    return transcribe(path, msg_id, my_jid)


def transcribe_group_voice(chat_jid: str, msg_id: str) -> str:
    """For the watcher: best-effort transcript of a group voice note."""
    hit = cached(msg_id)
    if hit:
        return hit
    path = download_voice(chat_jid, msg_id)
    if not path:
        return ""
    text, _ok = transcribe(path, msg_id, chat_jid)
    return text
