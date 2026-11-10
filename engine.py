#!/usr/bin/env python3
"""
engine.py
───────────────────
Shared engine:
  - gemini_json()          → call Gemini with JSON schema enforcement + retry
  - gemini_text()          → freeform Gemini call (used by the preflight probe)
  - resolve_send_jid()     → convert @lid JIDs → @s.whatsapp.net for reliable delivery
  - get_messages()         → fetch + format messages as IST-timestamped text
  - utc_to_ist()           → datetime helper
"""

import os
import re
import json
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple

import _envload
import mstate

# ── Config ────────────────────────────────────────────────────────────────────
_dir = os.path.dirname(os.path.abspath(__file__))
_home = _envload.resolve_home(default_name=".nembu")
_envload.load(os.path.join(_home, ".env"))
# Legacy fallback
if not os.path.exists(os.path.join(_home, ".env")):
    _legacy_home = os.getenv("MOSAIC_HOME") or os.path.expanduser("~/.mosaic")
    _envload.load(os.path.join(_legacy_home, ".env"))

GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL      = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
MESSAGES_DB_PATH  = os.getenv("MESSAGES_DB_PATH", os.path.join(_dir, "store", "messages.db"))
WHATSAPP_DB_PATH  = os.getenv("WHATSAPP_DB_PATH", os.path.join(_dir, "store", "whatsapp.db"))

# Logging (the bridge captures stderr; no dedicated command log any more)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M",
)

# ── Gemini setup ─────────────────────────────────────────────────────────────
# We talk to the Generative Language REST API directly (stdlib urllib), so the
# engine has no SDK dependency. GEMINI_MODEL is model-name only.
def _api_key() -> str:
    """Read the key lazily so a key saved while the worker is already running
    is picked up without restarting the process."""
    global GEMINI_API_KEY
    if not GEMINI_API_KEY:
        _home2 = _envload.resolve_home(default_name=".nembu")
        _envload.load(os.path.join(_home2, ".env"))
        GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    return GEMINI_API_KEY or ""


def ai_available() -> bool:
    """True when a Gemini key is configured."""
    return bool(_api_key())


_GEMINI_HOST = "https://generativelanguage.googleapis.com/v1beta/models"


def _gemini_host() -> str:
    """Public API by default; NEMBU_GEMINI_BASE (legacy MOSAIC_GEMINI_BASE) points
    tests/support at a local fake without ever touching a live key."""
    return (_envload.getenv("NEMBU_GEMINI_BASE", "MOSAIC_GEMINI_BASE", "") or _GEMINI_HOST).rstrip("/")

_RETRY_DELAYS = (1.0, 3.0, 8.0)
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}
_FATAL_HTTP = {400, 401, 403, 404}


class GeminiError(RuntimeError):
    """Typed transport failure. reason is rate_limited | network | timeout |
    bad_key | bad_output | bad_request."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason + (f": {detail}" if detail else ""))
        self.reason = reason


def _classify_http(code: int) -> tuple:
    """(reason, retryable) for an HTTP status."""
    if code == 429:
        return "rate_limited", True
    if code in _RETRYABLE_HTTP:
        return "network", True
    if code == 404:
        return "bad_model", False
    if code in _FATAL_HTTP:
        return "bad_key", False
    return "http_error", False


def _post_model(model: str, key: str, payload: bytes) -> tuple:
    """One request. Returns (text, reason, retryable, retry_after)."""
    import urllib.error
    import urllib.request

    url = f"{_gemini_host()}/{model}:generateContent"
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        reason, retryable = _classify_http(exc.code)
        retry_after = 0.0
        try:
            retry_after = max(0.0, float(exc.headers.get("Retry-After", "0")))
        except (TypeError, ValueError):
            retry_after = 0.0
        logging.warning(f"Gemini HTTP {exc.code} ({reason})")
        return None, reason, retryable, retry_after
    except urllib.error.URLError as exc:
        r = "timeout" if "timed out" in str(exc).lower() else "network"
        logging.warning(f"Gemini transport error ({r}): {exc}")
        return None, r, True, 0.0
    except (TimeoutError, ConnectionError, OSError) as exc:
        logging.warning(f"Gemini transport error: {exc}")
        return None, "network", True, 0.0

    try:
        data = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        return None, "bad_output", True, 0.0
    candidates = data.get("candidates") or []
    if not candidates:
        return None, "bad_output", True, 0.0
    finish = candidates[0].get("finishReason")
    if finish and finish != "STOP":
        return None, "bad_output", True, 0.0
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        return None, "bad_output", True, 0.0
    return text, "", False, 0.0


def _rest_generate(prompt: str, config: dict, model: str = "") -> str:
    """Call Gemini with transport retries, Retry-After and a model fallback."""
    import random
    import time

    key = _api_key()
    if not key:
        raise GeminiError("bad_key", "no key configured")
    model = model or os.getenv("GEMINI_MODEL", GEMINI_MODEL)
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": config,
    }).encode()

    last_reason = "bad_output"
    for attempt in range(3):
        text, reason, retryable, retry_after = _post_model(model, key, payload)
        if text is not None:
            return text
        last_reason = reason
        if not retryable:
            raise GeminiError(reason)
        if attempt < 2:
            delay = retry_after or _RETRY_DELAYS[attempt]
            time.sleep(delay + random.uniform(0, 0.5))

    fallback = os.getenv("GEMINI_MODEL_FALLBACK", "gemini-2.5-flash-lite")
    if fallback and fallback != model:
        logging.warning(f"Gemini falling back to {fallback} after {last_reason}")
        text, reason, _retryable, _ra = _post_model(fallback, key, payload)
        if text is not None:
            return text
        last_reason = reason
    raise GeminiError(last_reason)


def gemini_audio(path: str, mime_type: str, prompt: str, model: str = "") -> str:
    """Transcribe/describe an audio file via the same REST endpoint.
    Inline base64 keeps this dependency-free; WhatsApp voice notes are tiny.
    Raises on HTTP/parse error (callers decide the fallback)."""
    import base64
    import urllib.error
    import urllib.request

    if not ai_available():
        raise RuntimeError("no Gemini key configured")
    name = model or os.getenv("GEMINI_MODEL_AUDIO", GEMINI_MODEL)
    url = f"{_gemini_host()}/{name}:generateContent"
    with open(path, "rb") as fh:
        data = base64.b64encode(fh.read()).decode()
    body = json.dumps({
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime_type, "data": data}},
        ]}],
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": _api_key()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read()[:300]!r}") from None
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"no candidates: {str(payload)[:300]}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


def gemini_json(prompt: str, fallback: dict, schema: Optional[dict] = None, model: str = "") -> dict:
    """Call Gemini expecting JSON. Transport failures return the fallback marked
    with ``_gemini_failed`` and a typed ``_reason`` so callers can tell the user
    what actually happened."""
    if not ai_available():
        return {**fallback, "_gemini_failed": True, "_reason": "bad_key"} if isinstance(fallback, dict) else fallback

    config = {"responseMimeType": "application/json"}
    if schema:
        config["responseSchema"] = schema

    for attempt in range(2):
        ask = prompt if attempt == 0 else prompt + "\nReturn only valid JSON."
        try:
            raw = _rest_generate(ask, config, model=model)
        except GeminiError as exc:
            logging.error(f"Gemini call failed ({exc.reason})")
            if isinstance(fallback, dict):
                return {**fallback, "_gemini_failed": True, "_reason": exc.reason}
            return fallback
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logging.warning(f"JSON parse failed (attempt {attempt+1}): {exc}")
    if isinstance(fallback, dict):
        return {**fallback, "_gemini_failed": True, "_reason": "bad_output"}
    return fallback


def gemini_text(prompt: str) -> str:
    """Freeform Gemini call (preflight probe)."""
    if not ai_available():
        return "AI unavailable. Please check GEMINI_API_KEY."
    try:
        return _rest_generate(prompt, {})
    except GeminiError as exc:
        logging.error(f"Gemini text error: {exc.reason}")
        return f"AI Error: {exc.reason}"
    except Exception as exc:
        logging.error(f"Gemini text error: {exc}")
        return f"AI Error: {exc}"


# ── Time helpers ──────────────────────────────────────────────────────────────
def utc_to_ist(dt):  # datetime -> datetime
    """Convert a naive UTC datetime to IST (Asia/Kolkata)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(mstate.local_tz())


def parse_ts(ts) -> Optional[datetime]:
    """Parse a SQLite timestamp (string or datetime) into a naive UTC datetime.

    Handles the Go bridge's RFC3339-style values with a UTC offset
    ("2026-10-17 11:39:31+05:30"): the offset is honored, so utc_to_ist()
    renders the real IST time instead of shifting twice.
    """
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        try:
            clean = ts.replace("T", " ").strip()
            if clean.endswith("Z"):
                clean = clean[:-1].strip()
            offset = None
            m = re.search(r"\s*([+-])(\d{2}):?(\d{2})$", clean)
            if m:
                sign = 1 if m.group(1) == "+" else -1
                offset = sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
                clean = clean[: m.start()].strip()
            base = clean.split(".")[0].strip()
            dt = datetime.strptime(base, "%Y-%m-%d %H:%M:%S")
            if offset is not None:
                dt = dt - offset
            return dt
        except ValueError:
            return None
    if isinstance(ts, (int, float)):
        # Unix timestamp — milliseconds if > 20 billion
        try:
            return datetime.utcfromtimestamp(ts / 1000 if ts > 20_000_000_000 else ts)
        except Exception:
            return None
    return None


# ── LID / JID resolution helpers ─────────────────────────────────────────────

def _wa_db_conn():
    """Open a read-only connection to whatsapp.db only."""
    if not os.path.exists(WHATSAPP_DB_PATH):
        return None
    try:
        return sqlite3.connect(f"file:{WHATSAPP_DB_PATH}?mode=ro", uri=True)
    except Exception:
        return None


def resolve_send_jid(jid: str) -> str:
    """Resolve a @lid JID to its @s.whatsapp.net equivalent for reliable delivery.

    The whatsmeow Go bridge occasionally chokes on @lid JIDs sent via the HTTP API
    (participant list hash mismatch), silently dropping the message on some devices.
    Sending to the real phone-number JID fixes this.

    @g.us group JIDs are returned unchanged.
    """
    if not jid or "@g.us" in jid or "@s.whatsapp.net" in jid:
        return jid  # already fine

    if "@lid" not in jid and not jid.replace("-", "").isdigit():
        return jid  # unknown format, pass through

    # Strip @lid / hyphens / device IDs to get the raw numeric LID
    lid = jid.replace("@lid", "").replace("-", "").strip()
    lid = lid.split(":")[0]

    conn = _wa_db_conn()
    if not conn:
        return jid
    try:
        cur = conn.cursor()
        cur.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (lid,))
        row = cur.fetchone()
        if row and row[0]:
            resolved = f"{row[0]}@s.whatsapp.net"
            logging.info(f"resolve_send_jid: {jid} → {resolved}")
            return resolved
        # Also try with the full LID string (some entries store it with hyphens)
        cur.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (jid.replace("@lid", ""),))
        row = cur.fetchone()
        if row and row[0]:
            resolved = f"{row[0]}@s.whatsapp.net"
            logging.info(f"resolve_send_jid (full): {jid} → {resolved}")
            return resolved
    except Exception as e:
        logging.warning(f"resolve_send_jid error for {jid}: {e}")
    finally:
        conn.close()
    return jid  # fallback: send as-is


def _chats_db_name(jid: str) -> str:
    """Display name fallback from the bridge's chats table (messages.db)."""
    try:
        if not os.path.exists(MESSAGES_DB_PATH):
            return ""
        conn = sqlite3.connect(f"file:{MESSAGES_DB_PATH}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT name FROM chats WHERE jid = ?", (jid,)).fetchone()
        finally:
            conn.close()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def _resolve_sender_name(raw_name: str) -> str:
    """Post-process a SQL-returned sender name.
    If it looks like a raw LID, phone number, or JID, resolve it to a human name.
    """
    if not raw_name:
        return "Unknown"

    # Already looks like a real name (has letters, not just digits/@)
    stripped = raw_name.replace("@lid", "").replace("@s.whatsapp.net", "").strip()
    if not stripped.replace("-", "").isdigit():
        # Contains non-digit chars beyond @/- — likely a real name already
        return raw_name

    # It's a raw LID / phone number — resolve it
    conn = _wa_db_conn()
    if not conn:
        return "Someone"
    try:
        cur = conn.cursor()
        jid = None

        # Try LID → pn → JID
        clean_lid = stripped.replace("-", "")
        cur.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (clean_lid,))
        row = cur.fetchone()
        if row and row[0]:
            jid = f"{row[0]}@s.whatsapp.net"
        else:
            # Treat as phone number directly
            jid = f"{clean_lid}@s.whatsapp.net"

        cur.execute(
            "SELECT COALESCE(push_name, full_name, first_name, business_name) "
            "FROM whatsmeow_contacts WHERE their_jid = ? LIMIT 1",
            (jid,)
        )
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
    except Exception as e:
        logging.warning(f"_resolve_sender_name error for '{raw_name}': {e}")
    finally:
        conn.close()
    chat_name = _chats_db_name(jid or f"{stripped}@s.whatsapp.net")
    if chat_name and not str(chat_name).replace("-", "").isdigit():
        return chat_name
    return "Someone"


# ── DB helpers ────────────────────────────────────────────────────────────────
def _get_conn():
    if not os.path.exists(MESSAGES_DB_PATH):
        logging.error(f"DB connect error: messages db {MESSAGES_DB_PATH} not found")
        return None
    try:
        conn = sqlite3.connect(f"file:{MESSAGES_DB_PATH}?mode=ro", uri=True)
        if os.path.exists(WHATSAPP_DB_PATH):
            conn.execute(f"ATTACH DATABASE '{WHATSAPP_DB_PATH}' AS whatsapp_db")
        return conn
    except Exception as e:
        logging.error(f"DB connect error: {e}")
        return None


_MESSAGES_QUERY = """
    SELECT
        COALESCE(
            c_jid.push_name, c_jid.full_name, c_jid.first_name, c_jid.business_name,
            c_lid.push_name, c_lid.full_name, c_lid.first_name, c_lid.business_name,
            ms.sender_jid,
            m.sender
        ) AS sender_name,
        m.content,
        m.timestamp
    FROM messages m
    LEFT JOIN whatsapp_db.whatsmeow_message_secrets ms
           ON m.id = ms.message_id AND m.chat_jid = ms.chat_jid
    LEFT JOIN whatsapp_db.whatsmeow_contacts c_jid ON ms.sender_jid = c_jid.their_jid
    LEFT JOIN whatsapp_db.whatsmeow_lid_map lm ON ms.sender_jid = lm.lid
    LEFT JOIN whatsapp_db.whatsmeow_contacts c_lid
           ON (lm.pn || '@s.whatsapp.net') = c_lid.their_jid
    WHERE m.chat_jid = ? AND m.timestamp > ? AND m.content IS NOT NULL AND TRIM(m.content) != ''
    ORDER BY m.timestamp DESC
    LIMIT ?
"""

_FALLBACK_QUERY = """
    SELECT sender, content, timestamp FROM messages
    WHERE chat_jid = ? AND timestamp > ? AND content IS NOT NULL AND content != ''
    ORDER BY timestamp DESC LIMIT ?
"""


def get_messages(jid: str, days: int = None, limit: int = 2000) -> List[Tuple]:
    """Returns list of (sender, content, utc_datetime) tuples, oldest first.
    Sender names are post-processed through _resolve_sender_name() to convert
    raw LID / phone fallbacks into human-readable names.
    """
    days = days or mstate.RETENTION_DAYS
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cutoff = datetime.now() - timedelta(days=days)
        try:
            cur.execute(_MESSAGES_QUERY, (jid, cutoff, limit))
        except sqlite3.OperationalError:
            cur.execute(_FALLBACK_QUERY, (jid, cutoff, limit))
        rows = cur.fetchall()
        result = []
        for sender, content, ts in reversed(rows):
            dt = parse_ts(ts)
            resolved = _resolve_sender_name(sender or "")
            result.append((resolved, content, dt))
        return result
    except Exception as e:
        logging.error(f"get_messages error: {e}")
        return []
    finally:
        conn.close()


# Pattern: WhatsApp stores @-mentions as @DIGITS in message content (LID mentions)
_MENTION_RE = re.compile(r'@(\d{9,20})')


def _build_lid_name_map(lids):
    """Bulk-resolve a set of LID strings to names in a single DB transaction.
    Returns {lid_str: name_str} for all found entries.
    """
    if not lids:
        return {}
    conn = _wa_db_conn()
    if not conn:
        return {}
    result = {}
    try:
        cur = conn.cursor()
        placeholders = ",".join("?" * len(lids))
        # Step 1: LID → phone number
        cur.execute(
            f"SELECT lid, pn FROM whatsmeow_lid_map WHERE lid IN ({placeholders})",
            list(lids)
        )
        lid_to_pn = {row[0]: row[1] for row in cur.fetchall()}

        # Step 2: phone numbers → names (one query)
        pns = list(lid_to_pn.values())
        if pns:
            jids = [f"{pn}@s.whatsapp.net" for pn in pns]
            ph2 = ",".join("?" * len(jids))
            cur.execute(
                f"SELECT their_jid, COALESCE(push_name, full_name, first_name, business_name) "
                f"FROM whatsmeow_contacts WHERE their_jid IN ({ph2})",
                jids
            )
            jid_to_name = {row[0]: row[1] for row in cur.fetchall()}

            for lid, pn in lid_to_pn.items():
                name = jid_to_name.get(f"{pn}@s.whatsapp.net")
                if name:
                    result[lid] = name
    except Exception as e:
        logging.warning(f"_build_lid_name_map error: {e}")
    finally:
        conn.close()
    return result


def clean_messages_content(messages):
    """Resolve all @LID mention patterns in a list of (sender, content, dt) tuples.

    Builds a single bulk LID-→name map from all mentions found across all messages,
    then replaces them in one pass. Far faster than one DB call per mention.
    Returns a new list with resolved content.
    """
    # Collect all unique LIDs mentioned across all messages
    all_lids = set()
    for _, content, _ in messages:
        if content and '@' in content:
            all_lids.update(_MENTION_RE.findall(content))

    lid_map = _build_lid_name_map(all_lids) if all_lids else {}

    def _replace(match):
        lid = match.group(1)
        name = lid_map.get(lid)
        return f"@{name}" if name else match.group(0)

    result = []
    for sender, content, dt in messages:
        if content and '@' in content and all_lids:
            content = _MENTION_RE.sub(_replace, content)
        result.append((sender, content, dt))
    return result


# Keep single-string variant for any direct callers
def clean_message_content(text, lid_map=None):
    """Replace @LID mention patterns in a single message string.
    Prefer clean_messages_content() for bulk use.
    """
    if not text or '@' not in text:
        return text
    lids = set(_MENTION_RE.findall(text))
    if not lids:
        return text
    _map = lid_map if lid_map is not None else _build_lid_name_map(lids)

    def _replace(match):
        name = _map.get(match.group(1))
        return f"@{name}" if name else match.group(0)

    return _MENTION_RE.sub(_replace, text)


def format_messages_ist(messages, max_chars=100000) -> str:
    """Format messages as [HH:MM IST] Sender: content lines.
    Bulk-resolves all @LID mentions in one DB round-trip.
    Truncates to `max_chars` to prevent API payload errors, keeping the most recent.
    """
    messages = clean_messages_content(messages)  # bulk mention resolution
    lines = []
    current_chars = 0
    
    # Process from newest to oldest to prioritize recent context
    for sender, content, dt in reversed(messages):
        if dt:
            ist = utc_to_ist(dt)
            ts_str = ist.strftime("%H:%M IST")
        else:
            ts_str = "??"
            
        line = f"[{ts_str}] {sender}: {content or ''}"
        
        if current_chars + len(line) > max_chars:
            lines.append(f"... [Truncated {len(messages) - len(lines)} older messages to fit API limits]")
            break
            
        lines.append(line)
        current_chars += len(line) + 1
        
    return "\n".join(reversed(lines))



