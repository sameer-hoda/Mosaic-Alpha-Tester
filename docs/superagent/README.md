# Mosaic Superagent

An Orca-supervised adaptation of DeepSeek's `dsh-superagent` idea: **take as
little human attention as possible, and give as much value as possible.**

This is one repo, three workstreams, one approver, and one orchestrator.

## Roles

| Role | Who | Owns | Cannot |
|---|---|---|---|
| Orchestrator | the current opencode session | `board.md`, dispatch, user communication, sign-off gate | edit worker code directly without a ticket |
| Audit (Eval) | Orca worker `mosaic-audit` | reading logs, outputs, DB, code; writing `asks.md` and `audit-log.md` | edit code, write the live DB, send WhatsApp, dispatch the pipeline |
| Pipeline (Executor) | Orca worker `mosaic-pipeline` | Q&A code and tests in its worktree | touch `~/.mosaic`, send WhatsApp, write the live store, deploy |
| Onboarding (Executor) | Orca worker `mosaic-onboarding` | onboarding code, TUI flow, installer tests in its worktree | touch `~/.mosaic`, pair a real account, deploy |

## Loop

```
Source ──► Audit/Eval ──► Ask (proposed) ──► Orchestrator
                                                │
                                      Sam sign-off (Mandate)
                                                │
                                          Ticket approved
                                                │
                              ┌─────────────────┴─────────────────┐
                              ▼                                   ▼
                       Pipeline executes                  Onboarding executes
                              │                                   │
                              └────────────► Report ◄─────────────┘
                                                │
                                          Audit verifies
```

1. **Source.** `~/.mosaic/store/pipeline.log`, `engine.db`, `messages.db`,
   `docs/qa-audit-*.json`, `docs/qa-checker-review.md`, `make test` output, and
   Sam's messages.
2. **Audit.** Reads the source, finds gaps, and writes asks to `asks.md`.
   It never edits code and never dispatches work.
3. **Orchestrator.** Deduplicates, ranks, and asks Sam one concrete
   question at a time. Only Sam can approve.
4. **Mandate.** An approved ask becomes a ticket in `board.md` with
   `approved_by`, timestamp, scope, and acceptance.
5. **Execution.** The pipeline or onboarding worker implements only approved
   tickets, runs the evidence, and reports.
6. **Verification.** The audit re-checks the result and marks it `verified` or
   `reopened`.

## Policy

- **Accept mode: observe.** Audit proposes; nothing is auto-dispatched.
- **Only Sam approves.** A ticket without `approved_by: sameer` is not work.
- **One question at a time.** The orchestrator asks only when a person must
  decide, a result is ready, or work is blocked.
- **Honest reports.** Draft is draft; failure is failure; a limit is never
  bypassed to avoid asking.
- **Data boundary.** Workers may read the live store read-only. They must not
  write `~/.mosaic/store`, `~/.mosaic/app`, or `.env`, and must not send
  WhatsApp messages.
- **Test boundary.** Pipeline and onboarding use fixtures and a temporary
  `MOSAIC_HOME` (e.g. `/tmp/mosaic-onboarding-test`), never the live install.
- **No deployment.** Deploying to `~/.mosaic/app` needs a separate explicit
  approval from Sam.
- **No push to main.** Workers commit on their own branch and report the
  commit; merging is an orchestrator/Sam decision.
- **Stop by impact, not effort.** A change goes as far as a reviewable result;
  a design decision stops with a clear question.
- **Non-goals (until approved):** reminders mode (review #10), live WhatsApp
  sends, image/document understanding, switching the live worker.

## Board statuses

`proposed → approved → in-progress → ready → verified`, plus `reopened`,
`blocked`, `failed`.

## Shared paths

All workers share the main checkout's board files:

```
/Users/sameerhoda/Projects/wa_master/v1/Mosaic-Alpha-Tester/docs/superagent/
  board.md            orchestrator-owned
  asks.md             audit-owned
  audit-log.md        audit-owned
  pipeline-log.md     pipeline-owned
  onboarding-log.md   onboarding-owned
  prompts/*.md        role briefs
```

Workers write their own log and `asks.md`; only the orchestrator writes
`board.md`.
