# Onboarding agent brief

You own the first-run onboarding journey end to end. Make it rock solid and
tested. You work in your own Orca worktree.

## Read first

`SUPERAGENT` = `/Users/sameerhoda/Projects/wa_master/v1/Mosaic-Alpha-Tester/docs/superagent`

- `$SUPERAGENT/README.md` (policy: no live install, no sends)
- `$SUPERAGENT/board.md` (ONB-001…ONB-003)
- `$SUPERAGENT/onboarding-log.md` — append your session entry
- `README.md`, `install.sh`, `mosaic`, `bridge/tui.go`, `bridge/runtime.go`,
  `bridge/main.go`, `scripts/preflight.py`, `_envload.py`
- `~/.mosaic/app` only if you must inspect the shipped layout; do not modify it

## Mission

Map every state of the first-run journey, find every gap, add tests, then fix
the gaps. The journey is:

```
launch → key screen → key validation → scope choice → QR → pair → sync %
→ "Ready" → welcome → dashboard
```

Also cover: re-run/update, restart, stop/start, uninstall, wrong key, expired
QR, no WhatsApp, partial history sync, no Me chat, archived-chat choice,
macOS/Linux differences, and a failed Gemini probe.

## First session

1. **ONB-001 baseline audit.** Produce a state-by-state table with: expected
   behaviour, code path, current test coverage, gaps, and evidence.
2. **ONB-002 test harness.** Add automated tests that need no real account:
   TUI model state transitions, key validation with a fake HTTP server, QR
   state, scope choice, sync progress parsing, ready/welcome gating, error
   messages. Prefer Go tests under `bridge/` and shell/Python tests under
   `scripts/` or `tests/`. Use `MOSAIC_HOME=/tmp/mosaic-onboarding-test-*`.
3. **ONB-003 fixes.** Fix the highest-impact gaps; add a test for each; keep
   `make test` green.

## Constraints

- Never touch `~/.mosaic/store`, `~/.mosaic/app`, or `.env`.
- Never pair a real WhatsApp account. Use fake states and fixtures.
- Never send WhatsApp messages; never deploy.
- Do not reuse the live Gemini key for tests; use a fake/local probe.
- Commit on your own branch; do not push to main.
- The scope-choice screen (`screenScope`) may not be implemented yet. Treat
  that as a gap, propose the smallest fix, and add tests around the expected
  state machine.

## Report

- Update `$SUPERAGENT/onboarding-log.md` with state, gap/fix, evidence.
- Report `worker_done` with: states covered, tests added, gaps fixed, gaps
  accepted with reason, `make test` result, commit SHA, blockers.
