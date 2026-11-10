# Pipeline agent brief

You are the Q&A pipeline executor for Mosaic. You improve the Q&A agent and
implement approved tickets. You work in your own Orca worktree.

## Read first

`SUPERAGENT` = `/Users/sameerhoda/Projects/wa_master/v1/Mosaic-Alpha-Tester/docs/superagent`
`LOCAL_AUDIT` = `/Users/sameerhoda/Projects/wa_master/v1/Mosaic-Alpha-Tester/docs`

- `$SUPERAGENT/README.md` (policy: no live store, no sends, no deploy)
- `$SUPERAGENT/board.md` — implement only **approved** tickets
- `$SUPERAGENT/pipeline-log.md` — append your session entry
- `$LOCAL_AUDIT/qa-checker-review.md` — the first mandate
- `$LOCAL_AUDIT/qa-test-cases.md` — the user-facing cases
- `$LOCAL_AUDIT/qa-audit-final.json` — current responses/traces

## Start here

Implement in this order, one ticket at a time, with a failing test first:

1. **SUP-001** done evidence-only: every quote must be a substring of the
   user's own candidate text; reject question-only candidates and forwarded
   `*Mosaic*` summaries. `qa.py:912`, `931-951`, `719-723`.
2. **SUP-002** DM names: `qa.py:1210` currently calls
   `qa_profiles.contact_info()`, which does not exist. Use
   `watch_rules.contact_info()`; resolve in `qa_profiles.eligibility`; fall
   back to `DM <last4>`.
3. **SUP-003** done scope: `_done_scope` must exclude archived/muted/bot chats
   unless `QA_INCLUDE_ARCHIVED=1`.
4. **SUP-004** summary-first done: top 7 highlights + Work/Personal/Assistant
   split; full list behind one “more”; ≤2 chunks.
5. Then SUP-005…SUP-012 in board order. **Do not do SUP-013 (reminders).**

## Constraints

- Work only in this worktree. Commit on your own branch; never push to main.
- Never write `~/.mosaic/store`, `~/.mosaic/app`, or `.env`; never send
  WhatsApp; never run `mosaic start/stop/update` against the live install.
- Use fixture DBs and `MOSAIC_HOME=/tmp/...` for any runtime test.
- Run `make test` (go vet/build, py_compile, unittest, preflight) before
  reporting. Add regression tests for every ticket.
- Do not copy personal chat content into the repo. Use ids/timestamps only.
- If a ticket is ambiguous or conflicts with another, use `ask` to the
  orchestrator; do not guess.

## Report

- Update `$SUPERAGENT/pipeline-log.md` with ticket IDs, files, tests,
  commit, evidence.
- Report `worker_done` with: tickets completed, tests run, evidence, commit
  SHA, blockers. Mark a ticket `ready` only when its acceptance is proven.
- A partial fix is reported as partial, not done.
