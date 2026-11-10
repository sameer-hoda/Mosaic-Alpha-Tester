# Superagent board

Owner: orchestrator only. Workers read this file; they do not edit it.
Standing mandate from Sam: address the checker findings below, skip
reminders mode (#10), no live deployment without a separate approval.

## Approved — pipeline

| ID | Sev | Ticket | Acceptance | Status |
|---|---|---|---|---|
| SUP-001 | P0 | Done evidence-only | Every `done` quote is verbatim from the user's own candidate message; question-only candidates and forwarded `*Mosaic*` summaries are rejected; test proves attribution. | verified — audit 3bfa531 |
| SUP-002 | P0 | DM contact names | `watch_rules.contact_info()` is used; no raw LIDs/phones when a saved name exists; fallback `DM <last4>`; test. | verified — audit 3bfa531 |
| SUP-003 | P0 | Done scope | Archived/muted/bot chats excluded unless `QA_INCLUDE_ARCHIVED=1`; test. | verified — audit 3bfa531 |
| SUP-004 | P0 | Summary-first weekly done | First message: top 7 highlights + Work/Personal split; full list behind one “more”; ≤2 chunks. | reopened — audit 3bfa531; see ASK-10…15 |
| SUP-005 | P1 | Honest coverage | Report `read N msgs from M chats; K in scope`; add counts to open-items; test. | verified — audit 3bfa531 |
| SUP-006 | P1 | Person precision | A person query includes only their DM/thread and direct mentions; no unrelated DM leak; test. | reopened — audit 3bfa531; see ASK-10…15 |
| SUP-007 | P1 | Open-items ranking | `chat, person, deadline, age_days, next_action, priority`, `Do first`, overdue flags; test. | reopened — audit 3bfa531; see ASK-10…15 |
| SUP-008 | P1 | Future commitments | “Tomorrow”/date questions scan recent messages for go-live/launch/visit; test case 1. | reopened — audit 3bfa531; see ASK-10…15 |
| SUP-009 | P1 | Decision window | Decision/why questions use 30 days or hedge “in the last N days”; test. | verified — audit 3bfa531 |
| SUP-010 | P1 | Done latency | Skip call1 for done; batch ~250 or 3–4 parallel batches; measure <45s. | reopened — audit 3bfa531; see ASK-10…15 |
| SUP-011 | P1 | Reply shape/urgency | “What should I respond to” returns an urgency-ordered reply list, not On you/Others; test. | verified — audit 3bfa531 |
| SUP-012 | P2 | Formatting polish | Sanitize sentence-like group names; split Work/Personal/Assistant; split media wording; active-selection “summarize this”; search precision. | reopened — audit 3bfa531; see ASK-10…15 |
| SUP-013 | P2 | Reminders mode | Person → item → waiting days → draft. | **NOT APPROVED** (skipped by Sam) |
| SUP-014 | P0 | Background job starvation | Bound `ensure_cards`/`ensure_profiles`; ingest must run every loop; add a large-store (1,500-chat) fixture and an ingest-lag watchdog. Live hotfix applied (cards 10, profiles 20). | in-progress — live hotfix; canonical fix + large-store test pending |
| SUP-015 | P0 | Decision/why: biometric kill recommendation | Treat kill/stop/pause/go/no-go statements as facts; cite the deciding message; if only a recommendation, say "recommendation, not a confirmed decision". Case 4 quotes `B1C2D3E4F5A60718293B` (2026-11-06 15:57:05). | approved |
| SUP-016 | P0 | Sync chats.name repair | Stop writing the me-LID or a bare digit as `chats.name`; resolve from contacts; fall back to `DM <last4>`; test the polluted-DM fixture. | approved |

## Approved — onboarding

| ID | Sev | Ticket | Acceptance | Status |
|---|---|---|---|---|
| ONB-001 | P0 | Onboarding baseline audit | End-to-end journey map with every state and failure path; gaps listed with evidence; no live install. | ready — `c5759a3`, 19-state table in onboarding-log |
| ONB-002 | P0 | Onboarding test harness | Automated tests for key validation, QR, scope choice, sync progress, welcome, errors, restart/update; temp `MOSAIC_HOME`. | ready — 28 Go (41 with subtests) + 21 Python/shell tests |
| ONB-003 | P0 | Onboarding gap fixes | Every gap in ONB-001 fixed or explicitly accepted; `make test` green; no live install. | ready — scope screen, welcome retry, QR retry, 0600 modes; `make test` green |
| ONB-004 | P0 | Live E2E onboarding test | Safe reset + deploy plan, 19-state manual checklist, Sam scans QR, agent verifies logs; no wipe without backup. | ready — reset script + checklist; live reset executed with backup |
| ONB-005 | P0 | Early welcome + dashboard how-to | One welcome when the Me chat resolves, before sync/Ready/dashboard; one how-to/explainer on dashboard load with example questions; durable flags; tests. | ready — `e1f2d59`, tests green, not deployed |
| ONB-006 | P0 | Sync ETA + 84% stall | "~N min left" from progress rate; "finishing up" when stalled; bounded finishing state after 80% with no progress for 60s; tests. | ready — `e1f2d59`, tests green, not deployed |
| ONB-007 | P0 | Live Pipeline loading + scroller | "Live pipeline is loading…" when idle; rotating example-question animation; real events unchanged; tests. | ready — `e1f2d59`, tests green, not deployed |
| ONB-008 | P0 | Faster dashboard escape | Prominent "esc — open dashboard now"; esc works at 12%/84% before Ready; dashboard shows sync continues in background; tests. | ready — `e1f2d59`, tests green, not deployed |

## Approved — GTM (draft only; publishing needs sign-off)

| ID | Sev | Ticket | Acceptance | Status |
|---|---|---|---|---|
| GTM-001 | P0 | GitHub page / README | Hero, problem, how it works, features, privacy, install, screenshots, architecture, alpha warnings, FAQ, CTA. No invented claims. | approved |
| GTM-002 | P0 | Demo video | 60–90s script, storyboard, shot list, captions, synthetic data, 16:9 + 9:16 draft or recording plan. | approved |
| GTM-003 | P1 | Launch kit | X/LinkedIn thread, tester WhatsApp, HN/PH copy, email, one-page site copy. Drafts only. | approved |
| GTM-004 | P1 | Visual kit | Wordmark, color/type tokens, OG image, 3–4 synthetic screenshots, favicon. | approved |

## Audit queue

Audit writes proposed asks to `asks.md`. The orchestrator moves approved ones
here with `approved_by: sameer` and a timestamp. Nothing else becomes work.

## Reports

Workers write their own logs (`pipeline-log.md`, `onboarding-log.md`) and report
evidence to the orchestrator via `worker_done`. The audit writes `audit-log.md`
and `asks.md`.
