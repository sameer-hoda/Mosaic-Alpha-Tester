#!/usr/bin/env python3
"""budget.py — one daily token/cost ceiling across Radar triage, Q&A
answers, Top-10 and follow-up drafts.

Spend is per-call, persisted in mstate.spend. When the ceiling is hit,
analysis pauses (BudgetPause) while ingest continues: nothing is lost,
the TUI shows it, and one notice reaches the Me chat if reachable.
"""

import os

import _envload
import mstate

DAILY_USD_CAP = _envload.getenv_float("NEMBU_DAILY_USD_CAP", "MOSAIC_DAILY_USD_CAP", 1.20)
FAST_IN, FAST_OUT = 0.10, 0.40
PRO_IN, PRO_OUT = 2.00, 10.00

MODEL_PRICES = {
    "gemini-3.5-flash-lite": (FAST_IN, FAST_OUT),
    "gemini-3.1-flash-lite": (FAST_IN, FAST_OUT),
    "gemini-3.1-flash-lite-preview": (FAST_IN, FAST_OUT),
    "gemini-2.5-flash-lite": (FAST_IN, FAST_OUT),
    "gemini-3.5-flash": (0.60, 1.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3.1-pro-preview": (PRO_IN, PRO_OUT),
    "gemini-2.5-pro": (PRO_IN, PRO_OUT),
}


def estimate(model: str, prompt: str, out_text: str) -> float:
    pin, pout = MODEL_PRICES.get(model, (FAST_IN, FAST_OUT))
    return (len(prompt) / 4 / 1e6) * pin + (len(out_text) / 4 / 1e6) * pout


def record(model: str, prompt: str, out_text: str, purpose: str = "", conn=None) -> float:
    usd = estimate(model, prompt, out_text)
    own = conn is None
    c = conn or mstate.connect()
    try:
        day = mstate.local_date_key()
        c.execute(
            "INSERT INTO spend (day, in_chars, out_chars, usd, calls) VALUES (?,?,?,?,1) "
            "ON CONFLICT(day) DO UPDATE SET in_chars=in_chars+?, out_chars=out_chars+?, "
            "usd=usd+?, calls=calls+1",
            (day, len(prompt), len(out_text), usd, len(prompt), len(out_text), usd),
        )
        c.commit()
    finally:
        if own:
            c.close()
    return usd


def spent_today() -> float:
    c = mstate.connect()
    try:
        row = c.execute(
            "SELECT usd FROM spend WHERE day=?", (mstate.local_date_key(),)
        ).fetchone()
        return row["usd"] if row else 0.0
    finally:
        c.close()


class BudgetPause(Exception):
    pass


def assert_room() -> float:
    spent = spent_today()
    if spent >= DAILY_USD_CAP:
        raise BudgetPause(f"daily ceiling ${spent:.2f}/${DAILY_USD_CAP:.2f} hit")
    return spent


def gemini_json(prompt: str, fallback: dict, schema=None, model: str = "",
                purpose: str = "", trace_id: str = ""):
    import time as _t

    import engine

    assert_room()
    if not engine.ai_available():
        out = dict(fallback)
        out["_gemini_failed"] = True
        out["_reason"] = "bad_key"
        return out
    _t0 = _t.time()
    mstate.event(trace_id, "llm.request", "info",
                 f"{purpose or 'call'} · {len(prompt):,} chars · {model or getattr(engine, 'GEMINI_MODEL', '')}",
                 prompt=prompt)
    data = engine.gemini_json(prompt, fallback, schema, model=model)
    dur = int((_t.time() - _t0) * 1000)
    failed = bool(data.get("_gemini_failed"))
    mstate.event(trace_id, "llm.response", "error" if failed else "ok",
                 f"{purpose or 'call'} · {len(str(data)):,} chars",
                 dur_ms=dur, response=str(data))
    if failed:
        return data
    if isinstance(data.get("_gemini_failed"), bool) and data["_gemini_failed"]:
        return data
    record(model or getattr(engine, "GEMINI_MODEL", ""), prompt, engine.json.dumps(data), purpose)
    return data
