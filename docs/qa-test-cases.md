# Nembu Q&A test cases 🍋

Run these in the **Me chat**, one at a time. Each case says what to send, what
should happen, and which change it proves. Mark pass/fail as you go.

Environment switches (optional, restart the worker after changing):

| Switch | Default | Meaning |
|---|---|---|
| `QA_INCLUDE_ARCHIVED=1` | off | Search archived chats too |
| `QA_INCLUDE_BOT_DMS=0` | on | Skip DMs flagged as bots |
| `QA_DONE_CHATS=200` | 200 | Max chats for "what did I get done" |
| `QA_DONE_BATCH=120` | 120 | Candidate messages per extraction call |
| `NEMBU_QA_MERGE=0` (legacy `MOSAIC_QA_MERGE`) | on | Turn off the merged answer call |
| `NEMBU_BODY_CHARS=4000` (legacy `MOSAIC_BODY_CHARS`) | 4000 | Me-chat message length before "Say more" |

## P0 — bugs and correctness

### 1. Swipe-reply to a group message
**Do:** In a group, swipe-reply (quote) a message, then send `summarise this`.
**Expect:** An answer about that group. No "I couldn't answer that just now".
**Proves:** The swipe-reply jid/name fix.

### 2. Swipe-reply to a direct message
**Do:** In a one-to-one chat, swipe-reply a message, then send `what's pending here?`
**Expect:** An answer about that DM, not a group or a generic error.
**Proves:** Quoted-chat resolution now accepts DMs.

### 3. "Tomorrow" questions
**Do:** Ask `what's the plan for tomorrow?`
**Expect:** Recent context and actual plans for tomorrow. Never "Nothing stored in that window."
**Proves:** Future dates no longer become a message window; the end is clamped to now.

### 4. "Yesterday" does not include today
**Do:** Ask `what happened yesterday?`
**Expect:** Nothing from today (check timestamps in the answer).
**Proves:** `_flow_messages` now applies the window end.

### 5. Pending items reach the writer
**Do:** Ask `what's pending?`
**Expect:** *On you* and *Others* sections with real items, not a facts-only list.
**Proves:** The writer now receives `on_you` / `others` instead of the non-existent `open_items` key.

### 6. "Why" questions get prose
**Do:** Ask `why did we kill the biometric ads?`
**Expect:** A short paragraph with a source line, not chopped bullet fragments.
**Proves:** Lookup detection includes "why"/"plan", and direct answers stay prose.

### 7. Quoted replies and media are visible
**Do:** Reply to a message and ask `what was I answering?`; or ask about a photo/document.
**Expect:** The answer mentions the quoted message; media shows as `[photo]` or `[document: name]`.
**Proves:** Replies are rendered inline, media placeholders exist, media count is computed in code.

## Direct messages and scope

### 8. DMs are searched
**Do:** Ask `what did I miss today?`
**Expect:** The header says **chats**, and at least one DM item appears when a DM had activity.
**Proves:** DMs are in the Q&A scope; active DMs are force-added to global answers.

### 9. Summary includes DMs
**Do:** Ask `summary of chats from today`.
**Expect:** Bullets across 20+ chats, including direct messages.
**Proves:** Call 1's chat list now labels groups vs DMs and the prompt asks for both.

### 10. "What did I get done" is exhaustive
**Do:** Ask `hey what did I get done this week`.
**Expect:** Many items, covering Monday–Sunday, including personal DM items. Footer looks like `822 msgs you sent · 108 chats · 362 media not searchable`. It should **not** say `96 chats not scanned` or `some messages were left out`.
**Proves:** The done path scans every chat you posted in, batches candidates, and reports coverage.

### 11. Person query is filtered
**Do:** Ask `what is pending with Nisha?`
**Expect:** Only Nisha-related items, with the DM and groups she appears in.
**Proves:** Person scope now uses contact/DM/mention matching instead of all chats.

### 12. The word "respect" is not a person
**Do:** Ask `pending with respect to the plan`.
**Expect:** It does not treat "respect" as a person's name.
**Proves:** Person-name extraction is case-sensitive.

### 13. Your name counts as you
**Do:** In a test group, have someone write `@Sam can you send the invoice`, then ask `is anything on me?`
**Expect:** That ask appears under *On you*.
**Proves:** The user's identity is injected into call 1 and call 2.

## Search and shaping

### 14. Lookup from an old message
**Do:** Ask `when is the Merchant MBR?` or another fact from more than a week ago.
**Expect:** The exact answer plus a source line.
**Proves:** FTS5 keyword search, hits + surrounding messages, and code-owned citations.

### 15. Quiet-group lookup
**Do:** Ask about a topic from a group you read but rarely post in (for example `what did the committee decide about parking?`).
**Expect:** An answer from that group.
**Proves:** FTS hits feed the group shortlist, so recent activity is not the only gate.

### 16. Multi-group question
**Do:** Ask `what's pending in OKR and Pensioners?`
**Expect:** One answer covering both, each bullet labelled. No "reply with the number".
**Proves:** The over-eager "which group?" prompt is gone; ambiguity is kept only for duplicate names.

### 17. Long answer fits
**Do:** Ask any weekly summary.
**Expect:** No artificial "Say more" at 1,200 characters. It may still ask to say "more" above ~3,800 characters.
**Proves:** `MAX_BODY_CHARS` is 4,000 and code-formatting keeps every extracted item.

### 18. Merged answer call
**Do:** Ask a normal catch-up or lookup question, then check `nembu logs` (or the TUI trace).
**Expect:** `qa.merged ok` in the trace. If it fails, the answer still arrives via the old two-call path (`qa.merged warn`).
**Proves:** Calls 2 and 3 are merged with a safe fallback.

## Background / reminders

### 19. Follow-up sweep is healthy
**Do:** Check `nembu logs` after 11:00 or 17:00, or run `python3 worker.py tick followup` if available.
**Expect:** Few or no `followup.ledger` errors. If one fails, the trace names the chat and reason.
**Proves:** The sweep paces itself, caps chats, logs failures, and no longer has the stray "handheld" prompt.

### 20. FTS index is alive
**Do:** Send a message containing a unique word (for example `zorblat`) in a chat, wait for ingest, then ask `where did I see zorblat?`
**Expect:** It finds the new message without a restart.
**Proves:** Insert triggers update the FTS index.

## How to record results

For each case, note:

```
#   PASS/FAIL   what you asked   what you expected   what happened
```

If a case fails, include the last 20 lines of `nembu logs` and the exact
question. That is enough to trace it in `pipeline.log` (`qa.start`, `qa.call1`,
`qa.scope`, `qa.search`, `qa.merged`, `qa.done`).

## Known limits

- Archived chats are excluded unless `QA_INCLUDE_ARCHIVED=1`.
- The reminders/open-loops mode (#10 in the review) is **not** built yet; this
  pass adds DMs and person filtering to the existing paths.
- Messages older than 30 days are not searchable.
- The model still chooses which extracted items to show; the code guarantees
  they are all available and formats what it returns.
