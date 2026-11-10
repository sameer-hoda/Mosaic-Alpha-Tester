# Mosaic onboarding — live end-to-end checklist (ONB-004)

Status: **prepared, not executed.** Sam confirms `--full` vs `--pairing-only`
before any command below is run. No pairing, install or reset has been performed.

Updated for ONB-005..008: early welcome at Me-chat resolution, one how-to at
dashboard/Ready, sync ETA and a bounded 80%+ "finishing" state, a visible
`esc — open dashboard now` path, and the rotating Live Pipeline loader.

This checklist walks the first-run journey in 19 states. Record evidence for each
state as a screenshot or a pasted terminal block; keep the evidence outside
`~/.mosaic` and delete it when done (it may contain personal chat text).

- Code under test: this worktree, branch `sameer-hoda/mosaic-onboarding`
- Install home: `~/.mosaic` (override with `MOSAIC_HOME` only if you know why)
- Live evidence:
  - `~/.mosaic/store/mosaic.log` — bridge log; follow with `mosaic logs`
  - `~/.mosaic/store/pipeline.log` — JSON-line message pipeline (see below)
  - `~/.mosaic/bin/mosaic status` — one-shot state
  - `~/.mosaic/store/engine.db`, `messages.db`, `mechat.json`, `sync_state.json`
  - `~/.mosaic/config/scope.json`, `~/.mosaic/config/.onboarded`
- `pipeline.log` records **message processing**, not onboarding. It is expected
  to be empty until a message is ingested; an empty log during key/QR/sync is a
  pass, never a failure. Each line is JSON with `ts`, `msg_id`, `step`,
  `status`, `detail`; useful steps are `msg.store`, `ingest`, `qa.*`, `send`.

## Safety rules

1. Never run `--full` or `--pairing-only` without `--yes`; the script refuses.
2. Both scripts stop the service before touching state and back up to
   `~/.mosaic/backups/pre-onboarding-<timestamp>` (mode `0700`).
3. The backup contains your key and chat history. Delete it when finished:
   `rm -rf ~/.mosaic/backups/pre-onboarding-<timestamp>`.
4. `--test-home` does not touch the live store, `.env`, config or service.
5. Do not pair a second time while a previous pairing is still listed on the
   phone; unlink the stale "Mosaic" entry first.
6. Alpha warning: unofficial WhatsApp client; use the test number.

## Exact command set

### Prepare the worktree (no live effect)

```bash
cd /Users/sameerhoda/orca/workspaces/Mosaic-Alpha-Tester/mosaic-onboarding
git status --short
git rev-parse --short HEAD
bash -n scripts/reset_onboarding.sh
bash -n install.sh
```

### Reset (only the mode Sam confirms)

```bash
# Fresh key + QR + full re-sync; removes history and .env after backup.
~/orca/workspaces/Mosaic-Alpha-Tester/mosaic-onboarding/scripts/reset_onboarding.sh --full --yes

# Keep history/key; re-pair only; re-resolves Me chat and re-sends welcome.
~/orca/workspaces/Mosaic-Alpha-Tester/mosaic-onboarding/scripts/reset_onboarding.sh --pairing-only --yes

# Safe throwaway home for UI experiments; live install untouched (no service stop).
~/orca/workspaces/Mosaic-Alpha-Tester/mosaic-onboarding/scripts/reset_onboarding.sh --test-home --yes
```

Every run prints the timestamped backup path and exact restore steps. Keep that
output with the evidence.

### Deploy from this worktree

```bash
cd /Users/sameerhoda/orca/workspaces/Mosaic-Alpha-Tester/mosaic-onboarding
./install.sh --from-source --no-start       # rebuild + copy, do not launch yet
~/.mosaic/bin/mosaic start                  # or: ~/.mosaic/bin/mosaic
```

`./install.sh --from-source` (without `--no-start`) runs the same build and then
execs the launcher. On a machine with no TTY it prints:

```
→ Installing from the local checkout…
→ Installing to /Users/sameerhoda/.mosaic…
✓ Mosaic dev installed

Next: open a new Terminal window, or run:  ~/.mosaic/bin/mosaic

! no terminal attached — Mosaic is running in the background
```

What it does, explicitly:

- builds the Go bridge with CGO (`bridge/` → `~/.mosaic/app/bin/mosaic`);
- copies `*.py`, `VERSION`, the launcher and `scripts/preflight.py`;
- **overwrites `~/.mosaic/app`** on every run (`rm -rf "$APP"` first);
- **preserves `~/.mosaic/store` and `~/.mosaic/.env`** — messages, session and
  key survive an update;
- replaces `~/.mosaic/python` with the bundled interpreter (or the local
  `python3` symlink for `--from-source`).

Verify before testing:

```bash
~/.mosaic/bin/mosaic status
ls -l ~/.mosaic/app/bin/mosaic
shasum -a 256 ~/.mosaic/app/bin/mosaic      # note it for the report
```

## The 19-state checklist

Use one fresh run for states 2–7 (choose active-only), then optionally repeat
2–8 with `--full` choosing active+archived. Mark each state PASS / FAIL / N/A
with one line of evidence.

### 1. Pre-flight and deploy

- **Expected:** terminal only; no app screen yet.
- **Sam does:** confirm mode; run reset (§1), then deploy (§2).
- **Inspect:** `~/.mosaic/bin/mosaic status` → `bridge: not running` before
  start; after start, `connected: true` only after pairing. `mosaic logs` shows
  `Created new device` / `Starting WhatsApp client`. `pipeline.log` empty.
- **Pass:** build succeeds, `~/.mosaic/app/bin/mosaic` exists, store and `.env`
  still exist, status reaches the bridge.
- **Fail:** build error, missing app binary, store or `.env` lost, service will
  not start.

### 2. Fresh launch → key screen

- **Expected screen:**
  `Mosaic  your WhatsApp chief of staff ... v1`, then `Gemini API key`, the
  hint `aistudio.google.com/apikey → Create API key`, the panel
  `paste your key, then Enter`, footer `enter to verify · ctrl+c to quit`.
- **Sam does:** ensure no key in `.env` (state 1 used `--full`); run
  `~/.mosaic/bin/mosaic`.
- **Inspect:** `mosaic logs` must show the bridge waiting, not pairing. No QR.
  `ls -l ~/.mosaic/.env` present only if the installer seeded an empty file;
  `pipeline.log` empty.
- **Pass:** key screen appears, no QR has started.
- **Fail:** QR appears before the key, key screen skipped, or the app exits.

### 3. Malformed key (local validation)

- **Expected screen:** the panel shows dots; after Enter the red line
  `that doesn't look like an API key — paste the full key`.
- **Sam does:** type or paste `hello`, press Enter.
- **Inspect:** no request is sent; `~/.mosaic/.env` still has no real key.
  `mosaic logs` unchanged; `pipeline.log` empty.
- **Pass:** local rejection, no busy spinner.
- **Fail:** app accepts it or calls Google.

### 4. API-rejected key

- **Expected screen:** busy line `checking with Google…`, then
  `This key isn't valid or the Gemini API isn't enabled for it`.
- **Sam does:** paste a syntactically valid but revoked/wrong key
  (`AIza…` or `AQ.…`), Enter.
- **Inspect:** `mosaic logs` shows no Gemini setup; `.env` is not written with
  the rejected key; `pipeline.log` empty.
- **Pass:** clear error and the key screen stays usable.
- **Fail:** key saved, crash, or silent advance.

### 5. Network failure during key validation

- **Expected screen:** `Can't reach Google — check your internet`.
- **Sam does:** disable Wi-Fi (or block the host) and press Enter on a
  valid-looking key; restore network afterwards.
- **Inspect:** no key saved in `.env`; `mosaic logs` has no pairing activity;
  `pipeline.log` empty.
- **Pass:** honest network error and retry works.
- **Fail:** false success or an unexplained hang.

### 6. Valid key → scope screen

- **Expected screen:** brief `Saved.` flow, then
  `Which chats should Mosaic watch?` with
  `▸ Active chats only  ← recommended` and `Active and archived chats`, footer
  `↑↓ or y/n to choose · enter to continue · esc for active only`.
  No QR is on screen yet.
- **Sam does:** paste the real key, Enter, wait for the scope screen.
- **Inspect:** `ls -l ~/.mosaic/.env` is mode `0600`; it contains
  `GEMINI_API_KEY`. `~/.mosaic/config/.onboarded` does **not** exist yet.
  `mosaic logs` may show the bridge waiting for onboarding (up to 30 s).
  `pipeline.log` empty.
- **Pass:** scope screen appears before any QR.
- **Fail:** QR appears first, scope skipped, `.env` mode wrong.

### 7. Scope choice: active-only (default)

- **Expected screen:** the same scope screen, highlight on
  `▸ Active chats only ← recommended`.
- **Sam does:** press Enter (or Esc).
- **Inspect:** `~/.mosaic/config/scope.json` is
  `{"include_archived": false}`; `~/.mosaic/config/.onboarded` exists;
  `mosaic logs` then shows pairing starting. `pipeline.log` empty.
- **Pass:** files written, QR follows within a few seconds.
- **Fail:** no files, QR stuck, or chosen value differs.

### 8. Scope choice: active + archived

- **Expected screen:** same screen with `▸ Active and archived chats`.
- **Sam does:** from a fresh `--full` run (states 2–6), press Down/`n` then
  Enter.
- **Inspect:** `scope.json` is `{"include_archived": true}`. After pairing,
  `mosaic status` shows watched groups; an archived chat should be eligible in
  Q&A and Radar (verify in state 17 with a question). `mosaic logs` shows the
  same pairing start as state 7; `pipeline.log` stays empty until the first
  message.
- **Pass:** true persisted and honoured by the engine.
- **Fail:** false persisted, or archived chats remain excluded.

### 9. QR display

- **Expected screen:** `Link your account`; steps
  `1 Open WhatsApp on your phone`,
  `2 Settings → Linked devices → Link a device`,
  `3 Point your camera at this code`; `esc for dashboard · ctrl+c to quit`.
- **Sam does:** do **not** scan yet; compare with the phone's Linked devices.
- **Inspect:** `mosaic status` shows `paired: false`; `mosaic logs` shows the QR
  channel or connection. `pipeline.log` empty.
- **Pass:** QR renders in the terminal and stays readable.
- **Fail:** blank/garbled QR, no QR, or the screen auto-advances.

### 10. Expired QR refresh

- **Expected screen:** after the code times out, a **new** QR replaces the old
  one; it does not freeze on a dead code.
- **Sam does:** leave the QR untouched until WhatsApp expires it, or wait for
  the refresh cycle; do not scan the expired one.
- **Inspect:** `mosaic logs` shows `QR expired or channel closed without
  pairing; requesting a new code` (or `QR code timed out`), then a fresh code.
  `pipeline.log` empty.
- **Pass:** a usable code returns without restarting `mosaic`.
- **Fail:** same code forever, app exits, or pairing channel stops retrying.

### 11. Pair success → sync + early welcome

- **Expected screen:** after scanning, the QR is replaced by
  `Syncing your last 30 days…` with an animated bar, then counts.
- **Expected Me chat:** as soon as the Me chat resolves (before Ready), exactly
  one short message: `✅ Mosaic found your Me chat and is syncing your last
  30 days. Press esc in the terminal to open the dashboard while it finishes.
  A how-to will follow.`
- **Sam does:** scan with WhatsApp → Linked devices → Link a device, then
  confirm the phone shows a linked device; open the Me chat.
- **Inspect:** `mosaic status` → `paired: true`; `mosaic logs` →
  `Paired successfully`, then `Sending early welcome (attempt 1)`.
  `~/.mosaic/store/mechat.json` has `"welcomed_early": true`;
  `grep '"step":"send"' ~/.mosaic/store/pipeline.log | tail -1` → `"status":"ok"`.
- **Pass:** advances to sync, and the early welcome lands before Ready.
- **Fail:** stuck on QR, "logged out", pairing error, no early welcome, or a
  second early welcome after restart.

### 12. Partial history sync, ETA and the 80% tail

- **Expected screen:** `Syncing your last 30 days… NN%`, rows `chats`,
  `messages`, `back to <date>`, `phase` (`recent chats`, `older history`,
  `contact names`), `✓ Me chat found` once resolved, then an ETA line
  (`~N min left` or `still working — esc to dashboard`) and the prominent
  `esc — open dashboard now · d / enter also work · sync continues in the
  background`.
  After 60 s with no progress change at >=80%: `still syncing NN% — esc —
  dashboard` plus `finishing up…`; Ready may arrive without ever showing 100%.
- **Sam does:** let it run. At ~12% press `esc` once, see the dashboard, press
  `d` to return. Repeat at ~84%. Do not wait for 100%.
- **Inspect:** `mosaic status` → `sync: NN% | chats: N | messages: M`;
  `~/.mosaic/store/sync_state.json` is written at Ready; `mosaic logs` shows
  `Received history sync event...` and later `History sync ready`.
  `pipeline.log` stays empty (history does not emit `ingest` events).
- **Pass:** ETA appears only when the rate is known; no fake precision; esc/d/
  enter open the dashboard at 12% and 84%; a flat 84% ends in `finishing up…`
  and Ready within ~60 s, not a permanent wait.
- **Fail:** ETA invents seconds, esc does nothing before Ready, or the screen
  stays at 84% past the finishing window.
- **Why 84% sits:** WhatsApp reports its own conversation/message percentage,
  not elapsed time. The last chunk is older chats and media; event trickle keeps
  the generic idle timer alive, so the bridge now watches the progress value
  itself: 60 s flat at >=80% is a bounded "finishing" state, not a wait for 100%.

### 13. Fresh account / no history

- **Expected screen:** sync screen with the hint
  `if nothing arrives in about 90 seconds, Mosaic starts watching new messages`.
- **Sam does:** use an account with no history (or note this path from
  state 12); wait 90 s.
- **Inspect:** `mosaic logs` shows readiness without history events;
  `sync_state.json` appears with `chats: 0`/small counts. `pipeline.log` empty.
- **Pass:** Ready after the idle window instead of hanging forever; the ETA
  line says `still working — esc to dashboard` rather than a number.
- **Fail:** sync screen never completes, or it shows a fabricated ETA.

### 14. No Me chat (conditional)

- **Expected screen:** `resolving your Me chat…` for up to 90 s, then the red
  line `Couldn't find your Me chat. In WhatsApp open “Message yourself” once,
  then restart Mosaic.` Ready is **not** claimed.
- **Sam does:** do not force this state. Observe a normal run: if resolution
  succeeds, mark **N/A (resolved)**. If it fails naturally, follow the on-screen
  instruction and restart.
- **Inspect:** `~/.mosaic/store/mechat.json` absent or `jid` empty;
  `mosaic logs` shows `Me chat unresolved — no sends anywhere`; `mosaic status`
  shows `me: resolving…`. `pipeline.log` empty.
- **Pass:** either resolution succeeds, or the guidance appears and nothing is
  sent anywhere.
- **Fail:** Ready claimed without a Me chat, or a send attempted to a guess.

### 15. Ready summary and how-to trigger

- **Expected screen:** `✓ Ready`, then
  `30 days synced: N chats, M messages. Mosaic is now watching.` and
  `Opening the dashboard…` for ~3 s.
- **Expected Me chat:** exactly one how-to when the dashboard first opens (or
  when sync is Ready): `Mosaic is set up — ask me in this chat:` followed by
  `what did I miss today?`, `what's pending with <person>?`, `what did I get
  done this week?`, `top 10`, `mute <chat> 7d`, `watch <chat>`, `send`.
- **Sam does:** wait; do not press keys.
- **Inspect:** `~/.mosaic/store/sync_state.json` with `ready: true`, matching
  counts; `mosaic logs` → `History sync ready` and `Sending how-to (attempt 1)`.
  `mechat.json` has `"howto_sent": true`. `pipeline.log` shows the send.
- **Pass:** counts match `sync_state.json`, dashboard opens, one how-to only.
- **Fail:** Ready with zero counts when data exists, no how-to, or two copies.

### 16. Me chat messages: early welcome and how-to

- **Expected:** exactly two messages total in **Me (Message yourself)**:
  1. the short early welcome from state 11, sent while syncing;
  2. the how-to from state 15, sent at dashboard/Ready.
  The old "✅ Mosaic is ready — I've synced…" message is **not** sent by this
  build; on an upgrade the legacy `welcomed: true` flag suppresses the early
  message and the how-to still arrives once.
- **Sam does:** open the Me chat; confirm exactly one of each. Restart the app
  (state 18) and confirm neither is re-sent.
- **Inspect:** `mechat.json` has `"welcomed_early": true` and
  `"howto_sent": true`. `grep '"step":"send"' ~/.mosaic/store/pipeline.log` shows
  two successful sends; `mosaic logs` shows one early-welcome attempt and one
  how-to attempt. `pipeline.log` is otherwise empty until state 17.
- **Pass:** one early welcome before Ready, one how-to after, no duplicates
  across restarts, and no send to any other chat.
- **Fail:** welcome only at the dashboard, duplicate messages, stale "ready"
  welcome, or a send to a guessed chat.

### 17. Dashboard, Live Pipeline loader and message pipeline

- **Expected screen:** header pills `● live`, `engine on`, `me: <number>`;
  panels `MESSAGES newest first`, `STEPS`, `DETAIL`.
  While the dashboard is empty and not Ready, the banner says
  `still syncing NN% — sync continues in the background · d — sync details`.
  On the sync screen the Live Pipeline box shows `Live pipeline is loading…`
  plus a rotating `try: “…”` question every ~4.5 s
  (`what did I miss today?`, `what's pending with Nisha?`, `what did I get
  done this week?`, `top 10`, `mute this group 7d`).
  Once events exist, the real lines replace the loader.
- **Sam does:** watch the rotation; then in the Me chat send `status` (or
  `what did I miss today?`). Wait for the reply. Press `q` only when done.
- **Inspect:** `Pipeline` in the TUI fills with timestamps/steps. Logs:
  `grep -E '"(ingest|qa.candidate|qa.answer|send)"' ~/.mosaic/store/pipeline.log`
  → `ingest` ok, then `qa.*` and `send` ok. `mosaic logs` shows the round trip.
  If state 8 chose archived, ask about a chat that is archived and confirm it is
  searchable.
- **Pass:** loader + rotating questions before events; real events afterward;
  reply arrives in Me chat; no duplicate reply.
- **Fail:** loader says "waiting for the next event…", no rotation, no reply,
  send error, pipeline stuck, or archived choice ignored.

### 18. TUI restart and stop/start persistence

- **Expected:** quitting the TUI does not stop the service. Reopening goes
  straight to sync/dashboard — **no key screen and no scope screen**.
- **Sam does:**
  ```bash
  # in the TUI press q
  ~/.mosaic/bin/mosaic status      # bridge still running
  ~/.mosaic/bin/mosaic             # reopen: no onboarding screens
  ~/.mosaic/bin/mosaic stop
  ~/.mosaic/bin/mosaic start
  ~/.mosaic/bin/mosaic status
  ```
- **Inspect:** `~/.mosaic/config/.onboarded` still exists; `scope.json`
  unchanged; `sync_state.json` still ready. `mosaic logs` shows a reconnect, not
  a new pairing. `pipeline.log` is preserved.
- **Pass:** reopens past onboarding; stop/start reconnects with stored session;
  `mechat.json` still has `welcomed_early` and `howto_sent`, so no message is
  re-sent.
- **Fail:** key/scope re-asked, pairing lost, sync restarts from zero, or the
  early welcome/how-to repeats.

### 19. Update/re-run and uninstall

- **Expected:** re-running the installer replaces the app but keeps store and
  key. Uninstall removes app/python/bin but keeps store/.env when you answer the
  prompt with `n`.
- **Sam does:**
  ```bash
  cd /Users/sameerhoda/orca/workspaces/Mosaic-Alpha-Tester/mosaic-onboarding
  ./install.sh --from-source --no-start
  ~/.mosaic/bin/mosaic start
  ~/.mosaic/bin/mosaic status
  ```
  Only if testing uninstall: `~/.mosaic/bin/mosaic uninstall`, answer `n`, then
  reinstall and confirm messages/key survived.
- **Inspect:** before/after `ls -l ~/.mosaic/store` and `shasum ~/.mosaic/.env`;
  they must be unchanged. `mosaic status` returns the same sync state.
  `mosaic logs` shows a reconnect/start, not a new pairing; `pipeline.log` is
  preserved. After uninstall with `n`, `store/` and `.env` remain.
- **Pass:** data and key survive update; uninstall keeps them when declined.
- **Fail:** store or `.env` reset, history lost, or app fails to start.

## Deploy step (summary)

From this worktree:

```bash
cd /Users/sameerhoda/orca/workspaces/Mosaic-Alpha-Tester/mosaic-onboarding
./install.sh --from-source
```

It rebuilds the Go bridge and copies the Python runtime to `~/.mosaic/app`.
It **preserves `~/.mosaic/store` and `~/.mosaic/.env`**, and it **overwrites
`~/.mosaic/app` every time**. Run with `--no-start` while preparing; start with
`~/.mosaic/bin/mosaic start` when Sam is ready.

## Re-pair and rollback

### Re-pair without losing history (preferred)

```bash
cd /Users/sameerhoda/orca/workspaces/Mosaic-Alpha-Tester/mosaic-onboarding
scripts/reset_onboarding.sh --pairing-only --yes   # prints backup path
~/.mosaic/bin/mosaic start
~/.mosaic/bin/mosaic
```

Scan a new QR. `messages.db` and `engine.db` are kept, so history returns
immediately; `mechat.json` is gone, so the Me chat is re-resolved and the
welcome is sent once more. If the phone still lists the old linked device,
unlink it (WhatsApp → Linked devices) to avoid duplicates.

### Roll back to the backup

The reset script prints the exact commands, including the backup path. In
general:

```bash
~/.mosaic/bin/mosaic stop
rm -rf ~/.mosaic/store ~/.mosaic/.env
cp -pR ~/.mosaic/backups/pre-onboarding-<timestamp>/store ~/.mosaic/store
cp -p  ~/.mosaic/backups/pre-onboarding-<timestamp>/.env  ~/.mosaic/.env
chmod 600 ~/.mosaic/.env
~/.mosaic/bin/mosaic start
~/.mosaic/bin/mosaic status
```

If the restored `whatsapp.db` is no longer valid (for example the phone was
unlinked), the app shows a QR: pair again with `--pairing-only` semantics.

### Roll back the app/code

The installer overwrites `~/.mosaic/app`, so a code rollback is a redeploy from a
known-good commit:

```bash
cd /Users/sameerhoda/orca/workspaces/Mosaic-Alpha-Tester/mosaic-onboarding
git checkout <previous-good-sha>
./install.sh --from-source --no-start
~/.mosaic/bin/mosaic start
```

Store and `.env` are untouched by this. To undo a re-pair, use the store backup
above.

### Full reset

```bash
cd /Users/sameerhoda/orca/workspaces/Mosaic-Alpha-Tester/mosaic-onboarding
scripts/reset_onboarding.sh --full --yes
./install.sh --from-source --no-start
~/.mosaic/bin/mosaic start
```

Next launch asks for the key and a QR; history is rebuilt from WhatsApp.

## Risks

- **Data loss:** `--full` removes history and the key after backup. Confirm the
  mode with Sam before running it.
- **Backup sensitivity:** backups contain the Gemini key and chat history
  (`0700` dir; `.env` copied `0600`). Delete them when the test is over.
- **App gap during deploy:** the installer removes `~/.mosaic/app` before moving
  the new build; a failed build leaves no app until a successful reinstall.
  Keep the terminal output and the previous commit hash.
- **Re-pair side effects:** a new linked-device entry may appear on the phone;
  unlink stale entries. WhatsApp can restrict unofficial clients; use the test
  number.
- **Port 8090:** only one bridge can bind loopback `:8090`. Stop the live
  service before using `--test-home` with a second bridge.
- **Custom `MOSAIC_HOME`:** the launch agent/systemd unit hardcodes `%h/.mosaic`
  paths; this checklist assumes the default home. Do not mix homes for one test.
- **No automatic sends:** the only WhatsApp messages in this test are the
  welcome (Mosaic) and the `status` question you type yourself.
