#!/usr/bin/env python3
"""agent_config.py — user-owned persona for the answer writer.

Files own language; code owns retrieval, arithmetic and delivery. The only
file this phase reads is ``agent.md``:

    $NEMBU_CONFIG_DIR/agent.md
    $NEMBU_HOME/config/agent.md
    ~/.nembu/config/agent.md

Legacy fallbacks: $MOSAIC_CONFIG_DIR, $MOSAIC_HOME, ~/.mosaic

The file is seeded once with a starter persona and is never overwritten.
Empty or missing means no persona block. Injection is capped at 4000 chars
so a runaway edit cannot blow up the prompt or the budget.
"""

import json
import os

PERSONA_CHARS = 4000

DEFAULT_AGENT_MD = """# About me
I use WhatsApp for work and personal coordination. Replace this with your name,
role, team and the language you actually use.

# How I want answers
- Be concise and direct. WhatsApp-native bullets, no preamble, no filler.
- Lead with what needs my attention or a decision from me.
- Keep dates, amounts and names exact. Use IST.
- Attribute each item to the group and person, with the deciding quote.
- Do not write "no items require a response" unless that is literally the answer.
- When I ask what I got done, list concrete completed items, not chatter.
- Write in my voice: short, practical, operator-style. Hinglish is fine if I use it.
"""


def _resolve_home_fallback() -> str:
    # Use NEMBU_HOME, fallback MOSAIC_HOME, then ~/.nembu with legacy check
    new_home = os.getenv("NEMBU_HOME")
    if new_home:
        return new_home
    old_home = os.getenv("MOSAIC_HOME")
    if old_home:
        return old_home
    base = os.path.expanduser("~")
    nembu_d = os.path.join(base, ".nembu")
    mosaic_d = os.path.join(base, ".mosaic")
    if os.path.exists(nembu_d):
        return nembu_d
    if os.path.exists(mosaic_d):
        return mosaic_d
    return nembu_d


def config_dir() -> str:
    d = os.getenv("NEMBU_CONFIG_DIR") or os.getenv("MOSAIC_CONFIG_DIR")
    if d:
        return d
    # Check explicit home env
    nh = os.getenv("NEMBU_HOME")
    mh = os.getenv("MOSAIC_HOME")
    if nh:
        return os.path.join(nh, "config")
    if mh:
        return os.path.join(mh, "config")
    # Dev/legacy fallback: prefer ~/.nembu-config, then ~/.mosaic-config, then ~/.nembu/config
    base = os.path.expanduser("~")
    nembu_cfg = os.path.join(base, ".nembu-config")
    mosaic_cfg = os.path.join(base, ".mosaic-config")
    if os.path.exists(nembu_cfg):
        return nembu_cfg
    if os.path.exists(mosaic_cfg):
        return mosaic_cfg
    home = _resolve_home_fallback()
    return os.path.join(home, "config")


def _ensure_seeded() -> str:
    d = config_dir()
    path = os.path.join(d, "agent.md")
    if os.path.exists(path):
        return path
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(DEFAULT_AGENT_MD)
        os.chmod(path, 0o600)
    except OSError:
        return path
    return path


def persona() -> str:
    path = _ensure_seeded()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return ""
    return text[:PERSONA_CHARS]


def include_archived() -> bool:
    """Archived-chat scope chosen during onboarding.

    ``QA_INCLUDE_ARCHIVED`` remains the explicit operational override; the
    first-run choice lives in ``config/scope.json`` so it survives updates.
    """
    env = os.getenv("QA_INCLUDE_ARCHIVED")
    if env is not None and env.strip() != "":
        return env.strip() == "1"
    try:
        with open(os.path.join(config_dir(), "scope.json"), "r", encoding="utf-8") as fh:
            return bool(json.load(fh).get("include_archived", False))
    except (OSError, ValueError):
        return False


EXCLUDE_FILE = "exclude.txt"
DEFAULT_EXCLUDE_TXT = """# Chats excluded from every pipeline (Q&A, done, Radar).
# One entry per line: an exact JID or a case-insensitive name substring.
# Lines starting with # are ignored.
Instinct
"""

_EXCLUDE_CACHE = {"key": None, "entries": []}


def exclude_path() -> str:
    return os.path.join(config_dir(), EXCLUDE_FILE)


def _ensure_exclude_seeded() -> str:
    path = exclude_path()
    if os.path.exists(path):
        return path
    try:
        os.makedirs(config_dir(), mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(DEFAULT_EXCLUDE_TXT)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def excluded_chats() -> list:
    """Explicit operator exclusions: NEMBU_EXCLUDE_CHATS (comma-separated),
    then one entry per line in config/exclude.txt. Entries are exact JIDs or
    case-insensitive name substrings. The file is read per call but cached by
    mtime, so edits apply without a restart.
    """
    entries = []
    env = os.getenv("NEMBU_EXCLUDE_CHATS") or os.getenv("MOSAIC_EXCLUDE_CHATS") or ""
    entries.extend(x.strip() for x in env.split(",") if x.strip())
    path = _ensure_exclude_seeded()
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
        if _EXCLUDE_CACHE["key"] != key:
            with open(path, "r", encoding="utf-8") as fh:
                lines = [ln.split("#", 1)[0].strip() for ln in fh]
            _EXCLUDE_CACHE["entries"] = [ln for ln in lines if ln]
            _EXCLUDE_CACHE["key"] = key
        entries.extend(_EXCLUDE_CACHE["entries"])
    except OSError:
        pass
    return entries


def is_excluded_chat(jid: str = "", name: str = "") -> bool:
    """True when a chat is in the operator exclusion list."""
    jid_l = str(jid or "").strip().lower()
    name_l = str(name or "").strip().lower()
    for entry in excluded_chats():
        e = entry.lower()
        if not e:
            continue
        if e == jid_l or (jid_l and e == jid_l.split(":")[0]):
            return True
        if "@" not in e and len(e) >= 3 and e in name_l:
            return True
    return False


def persona_block() -> str:
    text = persona()
    if not text:
        return ""
    return (
        "Who the user is and how they want you to write. Follow this voice and "
        "these priorities:\n" + text + "\n\n"
    )
