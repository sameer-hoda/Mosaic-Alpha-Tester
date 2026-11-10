# Changelog

## 1.1.1 — privacy-cleaned release

- **Privacy scrub:** test fixtures, prompts and docs now use synthetic names, LIDs, phone numbers and message IDs. No real chat identifiers remain in the public tree.
- **Fresh public history:** old audit files and pre-clean commits are not part of the public repository.
- **Stable build:** this release keeps the last verified Q&A build (14/15); the unaudited recall/routing experiments are not included.
- **No keys or chat databases:** no `.env`, `store/`, `messages.db` or `whatsapp.db` is published. Test key strings are obvious dummies.

## 1.1.0 — Nembu rename 🍋

- **Brand:** Mosaic → Nembu. Product/CLI `nembu`, display `Nembu` with lemon 🍋. Logo: `assets/brand/nembu-logo-final.png`, mark: `assets/brand/nembu-mark-final.png`. Palette: lemon `#F9D71C`, deep blue `#2E6DB4`, cream `#FFF4CF`, ink `#1A1C21`, paper `#F7F5F0`.
- **Paths:** `~/.mosaic` → `~/.nembu`, `MOSAIC_HOME`/`MOSAIC_PYTHON` → `NEMBU_HOME`/`NEMBU_PYTHON` with legacy fallback, binary `mosaic` → `nembu`, LaunchAgent `ai.mosaic.bridge` → `ai.nembu.bridge`, systemd `mosaic.service` → `nembu.service`, log `mosaic.log` → `nembu.log`, `.mosaic-config` → `.nembu-config`.
- **Migration:** first-run installer copies `~/.mosaic` → `~/.nembu` with permissions preserved (store 0700, .env 0600) and timestamped backup, never deletes legacy. All code reads legacy as fallback when new missing.
- **Assistant:** display name `nembu`, headers `*Nembu* 🍋 · …`, forward summary detection supports both `*Mosaic*` and `*Nembu*` with 🍋. Conversation role `mosaic` → `nembu` dual-read.
- **GitHub:** `sameer-hoda/Mosaic-Alpha-Tester` → `sameer-hoda/Nembu-Alpha-Tester` in docs/install/Makefile.
- **Legacy alias:** `mosaic` binary/launcher kept as deprecated wrapper forwarding to `nembu`.
- Tests added for migration, legacy env fallback, old DB role compatibility.
- **README:** new hero, launch poster, 10-feature sections, Mermaid architecture and privacy table; `assets/nembu-hero.png`, `assets/nembu-launch-poster.png`, `assets/nembu-og.png`.

## 1.0.1 — Q&A quality

- Broad questions ("this week", "what did I miss", "what should I respond to",
  "what did I get done") are recognised as whole-history questions instead of
  asking you to name a group.
- Q&A now covers up to 12 active groups with more context per answer, and
  shortlists groups before the first model call.
- Q&A uses `gemini-3.5-flash` by default (`GEMINI_MODEL_QA`); the Radar keeps
  `gemini-3.5-flash-lite` for cost.
- Answers are cleaner: no "no items require a response" filler, and multi-day
  windows show both dates.

## 1.0.0 — production-ready tester build (originally Mosaic, now Nembu)

### User-visible changes

- **Linked device is branded "Nembu"** (previously "Mosaic") instead of "whatsmeow". WhatsApp may
  render it as `Nembu` or `<Browser> (Nembu)`; already-linked devices must
  unlink and pair again.
- **One-line installer** for a clean Mac. Downloads a pinned release and a
  private CPython 3.12 into `~/.nembu` (legacy `~/.mosaic` supported); no Homebrew, Go, Python or git needed.
  Re-running installs an update and keeps `store/` and `.env`.
- **Background service.** Nembu keeps running after Terminal closes
  (LaunchAgent `ai.nembu.bridge` on macOS, systemd `nembu.service` on Linux). `nembu` controls it
  (`status`, `logs`, `stop`, `start`, `restart`, `update`, `uninstall`).
- **Strict 30-day retention.** Messages, media, chats and engine state older
  than 30 days are pruned automatically. `pipeline.log` is capped at 7 days or
  5 MB, whichever is smaller.
- **Real sync progress.** QR screen → live percentage, chat/message counts,
  "back to <date>", Me-chat checkmark → Ready. The welcome message arrives only
  after the last 30 days are in.
- **Questions asked mid-sync** are answered automatically once sync finishes.
- **Radar** replaces the 5-minute watcher, 11:00/17:00 sweeps and 08:30 Top 10.
  It scans every 2 hours between **08:00 and 20:00 IST**, groups **and direct
  messages**. The 08:00 scan is the morning brief; 12:00 and 18:00 also run
  follow-ups.
- **Direct messages** are watched when there is recent two-way activity or a
  saved contact. Broadcasts, channels, archived and business/OTP senders are
  excluded.
- **Resilient Q&A.** Transport retries with backoff and a model fallback; the
  group choice can fall back to code; a failed writer still returns facts;
  retryable questions are retried at 1/5/15 minutes.
- **Honest sending.** Disconnected sends pause instead of burning attempts;
  bad recipients are rejected; permanently failed drafts return to
  `draft_pending` and ask you to reply `send`.
- **No overnight queue.** Alerts are never combined at 08:15; anything left
  after 20:00 expires.
- **No web setup, no slash commands.** The terminal TUI and the Me chat are the
  only interfaces. Voice notes in the Me chat still become text.
- Prompt/debug bodies are no longer written to `pipeline.log` unless
  `NEMBU_DEBUG_PROMPTS=1` (legacy `MOSAIC_DEBUG_PROMPTS=1`).

### Removed

Net deletion of about **3,000 lines** (14,621 → 11,577 in tracked source at
this release). Files deleted entirely:

- `bridge/setup_web.go` (replaced by `bridge/runtime.go`)
- `bridge/wizard.html`
- `bridge/agent_push.go`
- `bridge/events.go`
- `bridge/unixsock.go`
- `focus.py`, `inquiry.py`, `wacmd.py`, `formatter.py`
- `scripts/gemini_check.py`

Also removed: the opencode/Muse agent hook, the web setup password and port,
the legacy Q&A fallback chain, the slash-command stack, the 5-minute watcher
cadence, the separate Top-10 and follow-up schedules, the overnight
queue/combine logic, and dead Q&A/ledger/top-10 helpers.
