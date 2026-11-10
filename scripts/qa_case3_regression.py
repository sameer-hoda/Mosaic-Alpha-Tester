#!/usr/bin/env python3
"""qa_case3_regression.py — real-store regression for "what's pending?".

Run against a *copied* store, never the live one:

    STORE_DIR=/tmp/copy/store python3 scripts/qa_case3_regression.py

It exits 0 only when case 3 returns at least 3 On-you items and 2 Others.
This is the ASK-20261109-11 acceptance check; it needs a model key.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import qa_simulate  # noqa: E402


def main() -> int:
    result = qa_simulate.run_case(
        3, "what's pending?", "On you / Others must be populated")
    reply = result.get("reply") or ""
    print(f"action={result.get('action')} expert={result.get('expert')} "
          f"elapsed={result.get('elapsed_s')}s")
    if "*On you*" not in reply or "*Others*" not in reply:
        print("FAIL: missing On you / Others sections")
        print(reply)
        return 1
    on = reply.split("*On you*", 1)[1].split("*Others*", 1)[0]
    others = reply.split("*Others*", 1)[1]
    on_n = len(re.findall(r"^- ", on, re.M))
    oth_n = len(re.findall(r"^- ", others, re.M))
    print(f"On you={on_n} Others={oth_n}")
    print(reply[:1600])
    if on_n < 3 or oth_n < 2:
        print("FAIL: need >=3 On you and >=2 Others")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
