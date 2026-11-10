#!/usr/bin/env python3
"""_envload.py — minimal .env loader (no python-dotenv dependency).
Understands KEY=value lines, #/blank lines, and "quoted" values. Existing
environment always wins unless override=True, like python-dotenv.

Also provides helpers for NEMBU_ env with legacy MOSAIC_ fallback.
"""

import os


def load(path: str, override: bool = False) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key or "$" in key:
                    continue
                value = value.split(" #")[0].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if override or key not in os.environ:
                    os.environ[key] = value
        return True
    except OSError:
        return False


def getenv(new_key: str, old_key: str = "", default: str = "") -> str:
    v = os.getenv(new_key)
    if v is not None and v != "":
        return v
    if old_key:
        ov = os.getenv(old_key)
        if ov is not None and ov != "":
            return ov
    return default


def getenv_int(new_key: str, old_key: str = "", default: int = 0) -> int:
    raw = getenv(new_key, old_key, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def getenv_float(new_key: str, old_key: str = "", default: float = 0.0) -> float:
    raw = getenv(new_key, old_key, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def resolve_home(new_key="NEMBU_HOME", old_key="MOSAIC_HOME", default_name=".nembu") -> str:
    nh = os.getenv(new_key)
    if nh:
        return nh
    oh = os.getenv(old_key)
    if oh:
        return oh
    base = os.path.expanduser("~")
    nembu = os.path.join(base, ".nembu")
    mosaic = os.path.join(base, ".mosaic")
    if os.path.exists(nembu):
        return nembu
    if os.path.exists(mosaic):
        return mosaic
    if default_name != ".nembu":
        return os.path.join(base, default_name)
    return nembu
