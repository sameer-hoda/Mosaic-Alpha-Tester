#!/usr/bin/env python3
"""preflight.py — verify the runtime before the bridge starts.

The alpha path is stdlib-only by design. If any runtime module needs a
third-party package, this prints exactly what is missing so `./nembu` can
fix it (or tell you one command to run) instead of dying halfway through a
pairing.

    python3 scripts/preflight.py          # fast checks
    python3 scripts/preflight.py --probe  # also live-probe the Gemini key
"""
import importlib
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

RUNTIME_MODULES = [
    "mstate", "budget", "outbox", "alerts", "conversation", "router",
    "watch_rules", "watcher", "followup", "top10", "voice",
    "worker", "mechat",
]


def main() -> int:
    problems = []

    if sys.version_info < (3, 9):
        problems.append(f"python {sys.version.split()[0]} — need 3.9+ (zoneinfo)")

    try:
        import _envload

        _envload.load(os.path.join(REPO, ".env"))
    except Exception as exc:
        problems.append(f"vendored .env loader failed: {exc}")

    for name in RUNTIME_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            problems.append(f"{name}: missing module '{exc.name}'")
        except Exception as exc:
            problems.append(f"{name}: {type(exc).__name__}: {exc}")

    if not (os.getenv("GEMINI_API_KEY") or "").strip():
        # Not fatal: the TUI collects the key on first run.
        print("note: GEMINI_API_KEY not set — the TUI will ask for it")

    if "--probe" in sys.argv:
        try:
            import engine

            if not engine.ai_available():
                problems.append("Gemini key present but engine reports unavailable")
            else:
                out = engine.gemini_text("Reply with exactly: PREFLIGHT-OK")
                if "PREFLIGHT-OK" not in (out or ""):
                    problems.append(f"Gemini probe returned {out!r}")
        except Exception as exc:
            problems.append(f"Gemini probe failed: {exc}")

    if problems:
        print("PREFLIGHT FAILED")
        for p in problems:
            print("  -", p)
        return 1
    print("PREFLIGHT OK — runtime is stdlib-only and configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
