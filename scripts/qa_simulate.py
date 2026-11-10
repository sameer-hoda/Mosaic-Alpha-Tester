#!/usr/bin/env python3
"""qa_simulate.py — run the Q&A test questions without sending anything.

    python3 scripts/qa_simulate.py            # all cases
    python3 scripts/qa_simulate.py --only 3   # one case
    python3 scripts/qa_simulate.py --out docs/qa-audit.md

Each case goes through the same router + Q&A path the worker uses, prints the
reply, and records the pipeline trace. Nothing is sent to WhatsApp.
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

os.environ.setdefault("NEMBU_HOME", os.path.expanduser("~/.nembu"))
os.environ.setdefault("MOSAIC_HOME", os.path.expanduser("~/.mosaic"))
STORE = os.environ.setdefault("STORE_DIR", os.path.join(os.environ.get("NEMBU_HOME", os.environ.get("MOSAIC_HOME", os.path.expanduser("~/.nembu"))), "store"))
os.environ.setdefault("MESSAGES_DB_PATH", os.path.join(STORE, "messages.db"))
os.environ.setdefault("ENGINE_DB_PATH", os.path.join(STORE, "engine.db"))

import router  # noqa: E402

CASES = [
    (1, "what's the plan for tomorrow?", "Future date must not empty the window"),
    (2, "what happened yesterday?", "Yesterday must exclude today"),
    (3, "what's pending?", "On you / Others must be populated"),
    (4, "why did we kill the biometric ads?", "Why-question must be prose"),
    (5, "what did I miss today?", "Global scope should include DMs"),
    (6, "summary of chats from today", "Bullets, 20+ chats, DMs included"),
    (7, "hey what did I get done this week", "Exhaustive, no truncation"),
    (8, "what is pending with Nisha?", "Filtered to Nisha only"),
    (9, "pending with respect to the plan", "Do not treat 'respect' as a person"),
    (10, "when is the merchant MBR?", "Keyword lookup with source"),
    (11, "what's pending in OKR and Pensioners?", "Answer both, no group picker"),
    (12, "what did we decide about the UPI pivot?", "Older facts via search"),
    (13, "where did I see zorblat?", "No hallucinated hit"),
    (14, "summarize this", "Must ask what to summarize, not stay silent"),
    (15, "what should I respond to today?", "Reply-mode answer"),
    (16, "who do I need to remind?", "Should surface open asks"),
]


def _bridge_ready():
    try:
        import engine
        return engine.ai_available()
    except Exception:
        return False


def _trace(trace_id):
    path = os.path.join(STORE, "pipeline.log")
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("msg_id") == trace_id:
                    rows.append(r)
    except OSError:
        pass
    return rows


def _full_reply(action):
    tk = action.get("turn_key")
    if not tk:
        return action.get("reply", "")
    try:
        c = sqlite3.connect(f"file:{os.environ['ENGINE_DB_PATH']}?mode=ro", uri=True)
        try:
            row = c.execute("SELECT full_text FROM answers WHERE id=?", (tk,)).fetchone()
            return row[0] if row else action.get("reply", "")
        finally:
            c.close()
    except Exception:
        return action.get("reply", "")


def run_case(num, question, expected):
    trace_id = f"sim-{num}-{int(time.time())}"
    t0 = time.time()
    error = ""
    action = {}
    try:
        action = router.route(question, trace_id=trace_id) or {}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed = round(time.time() - t0, 1)
    events = _trace(trace_id)
    reply = _full_reply(action) if action.get("action") == "qa_answer" else action.get("reply", "")
    return {
        "n": num,
        "question": question,
        "expected": expected,
        "action": action.get("action", ""),
        "expert": action.get("expert", ""),
        "global": action.get("global", None),
        "elapsed_s": elapsed,
        "reply": reply,
        "error": error,
        "events": [
            {"step": e.get("step"), "status": e.get("status"),
             "detail": (e.get("detail") or "")[:220], "ms": e.get("dur_ms")}
            for e in events
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    cases = [c for c in CASES if not args.only or c[0] == args.only]
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    results = []
    for num, question, expected in cases:
        print(f"\n[{num}] {question}")
        r = run_case(num, question, expected)
        results.append(r)
        if r["error"]:
            print(f"  ERROR: {r['error']}")
        print(f"  action={r['action']} expert={r['expert']} global={r['global']} "
              f"{r['elapsed_s']}s")
        print("  " + (r["reply"] or "(no reply)").replace("\n", "\n  ")[:1600])
    out = args.out or os.path.join(REPO, "docs", f"qa-audit-{stamp.replace(':', '').replace(' ', '-')}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"# Nembu Q&A simulation 🍋 — {stamp}\n\n")
        for r in results:
            fh.write(f"## {r['n']}. {r['question']}\n\n")
            fh.write(f"- Expected: {r['expected']}\n")
            fh.write(f"- Action: `{r['action']}` · expert `{r['expert']}` · "
                     f"global `{r['global']}` · {r['elapsed_s']}s\n")
            if r["error"]:
                fh.write(f"- Error: `{r['error']}`\n")
            fh.write("\n```\n" + (r["reply"] or "(no reply)") + "\n```\n\n")
            if r["events"]:
                fh.write("Trace:\n\n")
                for e in r["events"]:
                    fh.write(f"- `{e['step']}` {e['status']} — {e['detail']}\n")
                fh.write("\n")
    with open(out.replace(".md", ".json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
