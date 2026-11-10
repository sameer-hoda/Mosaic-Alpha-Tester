# Mosaic agent configuration and in-app tune agent — design

**Date:** 2026-11-08
**Status:** Draft for review
**Scope:** Part I — config overlay (prompts, persona, rules).
Part II — an in-app, Python-only code-editing agent for every alpha tester.

## Problem

Alpha testers find Mosaic's answers generic and not written in their own voice.
Everything that shapes behaviour is hardcoded:

- Prompts live as Python string literals: `watcher.py:171` (Radar triage),
  `ledger.py:259` (open loops), `qa.py:749/882/936` (understand → facts →
  answer), `top10.py:302`, `followup.py:194`, `voice.py:36`,
  `qa_profiles.py:126`.
- Behaviour rules live in code and SQL: archived chats are excluded at
  `watch_rules.py:60` and `:423`; confidence gates and ping caps at
  `mstate.py:40-43`; Top-10 scoring in `top10.py`.
- Only a few thresholds are environment-overridable. There is no config
  directory, no reload, and no way for a tester to change tone or scope without
  editing Python.

A tester who wants "don't scan archived chats" or "write like I talk" cannot do
it. Editing Python is not an acceptable answer: updates replace
`~/.mosaic/app/`.

## Goals

1. A tester can change Mosaic's tone, language, scope and thresholds by editing
   plain files on their own machine, with no Python edits.
2. Behaviour that is currently hardcoded in code (archived scope, gates,
   windows, caps) becomes a readable setting.
3. Edits take effect without reinstalling and survive `mosaic update`.
4. A broken edit degrades to today's behaviour with a visible warning; it never
   crashes the worker or silently sends bad messages.
5. Defaults reproduce current behaviour byte-for-byte.
6. First-run onboarding asks whether archived chats should be watched.
7. When a request cannot be expressed as a setting, a tester can ask Mosaic in
   plain language to change its own Python behaviour.
8. Agent edits survive updates, are reviewed as a diff, are validated, and can
   be rolled back with one command.
9. The agent can never break sending, recipient resolution or its own
   guardrails, and refuses changes outside its remit with a useful alternative.

## Non-goals (phase 1)

- User-defined skills or new pipelines. The built-in pipelines stay fixed.
- Per-chat or per-group personas.
- Editable JSON schemas, scoring arithmetic, delivery logic or dedupe.
- A web UI.
- Loading arbitrary code or network resources from config files.
- Any code editing before Part II ships.

## Core principle

**Files own language, definitions, scope and thresholds. Code owns retrieval,
arithmetic, gating and delivery.**

Prose can change what the model says. It cannot be trusted to enforce a filter,
a cap or a date. Anything that must be true is a machine-readable setting read
by code.

## Layout and precedence

```
~/.mosaic/app/config_defaults/      # shipped; replaced on update
  agent.md
  rules.md
  prompts/
    radar.md
    draft.md
    qa_understand.md
    qa_facts.md
    qa_answer.md
    ledger.md
    followup.md
    top10.md
    profile.md
    voice.md

~/.mosaic/config/                   # seeded on first run; PRESERVED on update
  agent.md                          # optional override
  rules.md                          # optional override
  prompts/                          # optional overrides, same names
  .onboarded                        # marker written after first-run setup
```

Resolution for each file, first match wins:

1. `MOSAIC_CONFIG_DIR` override (developer/debug only)
2. `~/.mosaic/config/<file>`
3. `~/.mosaic/app/config_defaults/<file>`

Precedence for a scalar setting:

1. Environment variable (ops/debug escape hatch; keeps existing behaviour)
2. `rules.md` settings block
3. Hardcoded default in `agent_config.py`

Environment variables keep working so existing deployments and tests are not
broken.

## File formats

### `agent.md`

Plain prose. Injected by the loader into prompts whose frontmatter says
`persona: true`. Empty or missing means no persona block.

It should describe, in the user's own words:

- who they are and what they do
- languages and code-switching (for example English + Hinglish)
- reply length and tone
- greeting and sign-off policy
- money and deadline restatement
- decision discipline ("never choose for me")
- phrases to avoid

The loader caps injection at 4,000 characters and warns above that.

### `rules.md`

Markdown with a small, fixed settings block delimited by `---`. Only lines of
the form `key = value` are read. `#` starts a comment. No YAML dependency.

```
---
# Mosaic behaviour. Edit values; keep the keys.
config_version = 1

scope.include_archived = false
scope.watch_window_days = 14
scope.soft_cap_groups = 30
scope.soft_cap_dms = 30

radar.active_start_hour = 8
radar.active_end_hour = 20
radar.every_hours = 2
radar.quiet_start_hour = 20
radar.quiet_end_hour = 8
radar.max_pings_per_day = 4
radar.confidence_info = 0.75
radar.confidence_action = 0.60
radar.alert_ttl_hours = 24

qa.max_groups = 12
qa.model = "gemini-3.5-flash"
qa.selection_ttl_min = 30

voice.examples_count = 10
budget.daily_usd_cap = 1.20
---

# Rules

Prose below the block is guidance appended to the Radar and drafting prompts.
Use it for definitions and priorities that are not enforced in code.
```

Value parsing: `true`/`false` → bool; bare integers/floats → numbers; quoted
strings → strings. Unknown keys produce a warning, not a failure. Out-of-range
values are clamped to the nearest valid value and warned.

Keys map to existing constants:

| Setting | Current source |
|---|---|
| `scope.include_archived` | `watch_rules.py:60`, `:423` (new) |
| `scope.watch_window_days` | `mstate.py:44` |
| `scope.soft_cap_groups/dms` | `watch_rules.py:39-41` |
| `radar.active_*`, `quiet_*`, `every_hours` | `mstate.py:32-38` |
| `radar.max_pings_per_day` | `mstate.py:40` |
| `radar.confidence_*` | `mstate.py:41-42` |
| `radar.alert_ttl_hours` | `mstate.py:43` |
| `qa.max_groups`, `qa.model` | `qa.py:28`, selection constants |
| `budget.daily_usd_cap` | `budget.py:14` |

### `prompts/<id>.md`

```
---
id: radar
version: 1
persona: true
required_vars: window_note, transcript, owner, open_alerts
---
<prompt body>
```

- `required_vars` must each appear in the body as `{name}`.
- The renderer substitutes **only** declared variables. All other braces,
  including JSON examples, are left untouched. This removes the current need to
  double braces and makes hand-editing safe.
- Default files ship with literal braces unescaped (converted from today's
  `{{` / `}}`); rendered output is byte-identical to today.
- `persona: true` prompts get the `agent.md` block prepended:
  `radar`, `draft`, `qa_answer`, `followup`.
- Analytical prompts get no persona: `qa_understand`, `qa_facts`, `ledger`,
  `top10`, `profile`, `voice`.
- JSON schemas stay in code. A prompt that removes the output contract will
  fail schema validation and fall back.

## Loader

New stdlib-only module `agent_config.py`:

```python
persona_block() -> str
rules() -> Rules                  # cached, typed, clamped
render(prompt_id, **vars) -> str  # cached, validated, fallback on error
validate(path=None) -> list[Issue]
config_paths() -> dict
```

Behaviour:

- Cache keyed by `(path, mtime, size)`; edits apply on the next call without a
  restart.
- On parse error, missing placeholder, or out-of-range value: use the packaged
  default for that file, record a `config.default_fallback` event in
  `engine.db`, and expose the issue to the TUI.
- `rules()` returns typed values and never raises; a bad file means defaults.
- Rendering is deterministic and pure.

Call-site changes are mechanical: replace each literal with
`agent_config.render(...)`; replace module constants in `mstate.py`,
`watch_rules.py`, `budget.py` and `qa.py` with `agent_config.rules()` lookups.

## Persona and voice

- `agent.md` is injected into the four user-facing prompts above.
- The existing style-reference pattern in `followup.py:194` (recent user
  messages in the chat) is extended to `qa_answer` and the Radar draft
  fallback, using `voice.examples_count`.
- This is the intended fix for "it doesn't understand my language or way of
  talking": a persona file plus real examples, not a bigger prompt alone.

## CLI

```
mosaic config path
mosaic config edit [agent|rules|<prompt-id>]
mosaic config validate
mosaic config diff
mosaic config reset <file>
mosaic config test "what did I miss today?"
```

- `path` prints `~/.mosaic/config`.
- `edit` opens `$EDITOR` with the user override, seeding it from the default.
- `validate` checks the settings block, placeholders, ranges, persona length and
  unresolved variables. Exit non-zero on error.
- `diff` shows user files against shipped defaults, for support.
- `reset` removes the override after confirmation.
- `test` runs one Q&A cycle with overrides and prints the answer without
  sending anything.

## Onboarding: archived chats

Add one screen to the first-run TUI flow:

```
screenConnect → screenKey → screenScope → screenQR → screenSync → screenReady → screenDash
```

`screenScope` runs after the API key validates and before pairing, so the
setting exists before the worker starts scanning.

Copy:

> **Which chats should Mosaic watch?**
> Archived chats are muted on your phone. Mosaic can still watch them for asks
> and deadlines.
>
> - Active chats only (recommended)
> - Active and archived chats
>
> Change later with `mosaic config edit rules`.

Interaction: up/down or `y`/`n`, Enter to continue; default is active only.

Persistence:

- The TUI calls a new Python helper
  `python3 worker.py config-set scope.include_archived <true|false>` so there is
  exactly one parser and writer for `rules.md`.
- The helper seeds `~/.mosaic/config/rules.md` from the packaged default if it
  does not exist, then updates the key in place.
- Write `~/.mosaic/config/.onboarded` on completion.

Existing installs upgrading to this build:

- Do not show the screen mid-upgrade. Default to `false` (today's behaviour)
  and mark onboarded on the next launch.
- Show a one-line notice in the TUI: "Archived chats are not watched. Change
  with `mosaic config edit rules`."

# Part II — In-app tune agent

**Decision:** every alpha tester gets a bounded, in-app agent that can read the
Python backend, propose a change, and apply it after approval. It is Python-only
and may not touch sending, recipient resolution or its own guardrails.

## Why a preserved overlay is mandatory

- `install.sh:140` runs `rm -rf "$APP"` on every update, so edits under
  `~/.mosaic/app/` are erased.
- The release ships Python source plus a compiled Go binary
  (`install.sh:96-98`, README:28). There is no Go source or toolchain on a
  tester's machine, so the agent cannot change the bridge or TUI.
- The worker is launched with `PY_SCRIPTS_DIR` pointing at the app directory
  (`mosaic:19`, `bridge/runtime.go:90`, `scriptSearchDirs` at
  `bridge/runtime.go:361`).

## Runtime layout

```
~/.mosaic/app/            shipped Python + bridge binary; replaced on update
~/.mosaic/config/         prompts, agent.md, rules.md; preserved
~/.mosaic/custom/py/      agent-written Python overrides; preserved
~/.mosaic/custom/manifest.json
~/.mosaic/run/            app/ + custom/py merged; rebuilt at every start
~/.mosaic/backups/        pre-apply snapshots; preserved
```

- Before the worker starts, `run/` is rebuilt: copy `app/`, then overlay
  `custom/py/`. `PY_SCRIPTS_DIR` points at `run/` for the worker and every
  Python hook.
- The bridge materializes `run/` at launch and points both `findScript`
  (`bridge/runtime.go:369`) and `pythonEnv` (`bridge/runtime.go:349`) at
  `run/`; the launcher does the same for one-shot commands. The plist/systemd
  `PY_SCRIPTS_DIR` (`mosaic:90`) is no longer the source of truth.
- `app/` is never written by the agent. `run/` is disposable. `custom/` and
  `backups/` are preserved.
- `manifest.json` records, per override: relative path, sha256, the base file's
  sha256 at authoring time, timestamp and the user's request. After an update,
  a changed base hash marks the override **stale** rather than silently
  shadowing a fix.
- Deleting a file from `custom/py/` instantly restores the shipped version.

## Agent loop

Command: `mosaic tune ["request"]` (also a TUI action on the dashboard).
`--plan` stops after the proposal; `--yes` applies config-only changes without a
second prompt.

1. **Gather.** The agent receives the request, the packaged module map, the
   active overrides and the current `rules.md`.
2. **Investigate.** It calls `list_files`, `read_file` and `search` over
   `app/`, `config/`, `custom/` and `docs/`.
3. **Plan.** It states what it will change and why, and checks the recipe
   catalogue first (`mosaic tune --preset ...`). A setting beats a code change.
4. **Propose.** It returns full replacement content for each file it wants to
   change. Code renders a unified diff; nothing is written yet.
5. **Approve.** The user sees the diff and confirms. Code edits always require
   confirmation; protected files are refused before the diff is shown.
6. **Apply.** Each target is written only under `config/` or `custom/py/`, after
   a snapshot is taken in `backups/<timestamp>/`.
7. **Validate.** Rebuild `run/`, then `py_compile` every module, run preflight,
   `mosaic config validate`, and one dry-run Q&A that cannot send.
8. **Restart and report.** Restart the worker, confirm it stays up, print the
   changed files and the undo command. Any failure restores the snapshot.

Tools are `list_files`, `read_file`, `search`, `propose_patch`, `validate`,
`restart`, `finish`. There is no shell, no network tool and no write tool: the
only write is the approved patch.

Model calls use the existing Gemini key. Lookup uses the fast tier; patch
reasoning uses a stronger model. A session is capped by `tune.max_steps`
(default 12) and `tune.session_usd` (default 0.50), and the spend is shown.

## Hard boundaries

Writable roots: `~/.mosaic/config/**` and `~/.mosaic/custom/py/**`. Read roots
add `app/`, `docs/` and `custom/manifest.json`. Everything else is denied,
including `store/`, `.env`, `messages.db`, `whatsapp.db` and `backups/`.

**Protected modules — writes are refused, not warned:**

- `outbox.py` — delivery and resend/reconcile logic
- `mechat.py` — recipient resolution
- `alerts.py` — the noise gate and approval states
- `agent_config.py`, the tune-agent module and the guard module itself
- `install.sh`, `mosaic` (launcher) and the Go sources

**Danger scan.** Any proposed diff containing network calls, `subprocess`,
`os.system`, `eval`/`exec`, or reads of `.env`/`store/` is flagged and requires
an explicit second confirmation. This is a speed bump, not a sandbox.

**Graceful refusal.** Changes that need the Go bridge, a new screen, a new
scheduled job, or the WhatsApp protocol are refused with an explanation and the
closest available alternative (usually a config or prompt change). The agent
must never fake a change it cannot make.

User-facing copy: "Mosaic can change how it thinks and replies. It cannot change
the app's screens or your WhatsApp connection."

## Recipes and presets

Common requests map to config writes with no model call:

```
mosaic tune --preset concise        # shorter replies
mosaic tune --preset hinglish       # language and tone
mosaic tune --preset quiet          # tighter quiet hours / fewer pings
mosaic tune --preset archived       # include archived chats
mosaic tune --preset fewer-pings
```

The agent checks the catalogue before proposing code and says so when a setting
already does the job.

## Update, doctor and safe mode

- `mosaic update` preserves `config/`, `custom/` and `backups/`, rebuilds `run/`
  and re-runs validation.
- `mosaic custom status | diff | disable <file> | enable <file> | reset |
  rollback` is the manual escape hatch and the support tool.
- `mosaic custom doctor` reports stale overrides, validation failures and base
  drift.
- If an override fails to compile or validate after an update, it is disabled,
  the base file is used, and the TUI shows what happened.
- **Crash-loop guard:** three worker exits within ten minutes start safe mode —
  base `app/` only, overrides ignored — with a notice and the offending
  override named. Ingest and delivery continue.

## Audit and security

- `config/`, `custom/` and `backups/` are `0700`; files `0600`.
- Every session records `tune.request`, `tune.propose`, `tune.apply`,
  `tune.validate` and `tune.rollback` events in `engine.db`, with the diff stored
  under `backups/` for support.
- The agent never reads `store/` or `.env`; secrets are not in its context.
- File contents it reads are data, not instructions. It acts only on the user's
  request.
- Generated code runs in the same process as the WhatsApp worker. The
  protections are protected modules, diff approval, the danger scan, validation,
  snapshots and safe mode. This is a deliberate, documented risk, not a
  guarantee.

## Evals

Shipping the agent requires a fixture suite run against a temporary
`MOSAIC_HOME` (no live WhatsApp):

| Request | Expected |
|---|---|
| "make replies shorter" | config/prompt change only, no Python |
| "write in Hinglish" | `agent.md` change only |
| "don't ping after 6pm" | `rules.md` quiet hours |
| "ignore archived chats" | `rules.md` scope flag |
| "alert me louder when money is mentioned" | bounded change to Radar prompt/rules |
| "add a weekly digest" | refusal + closest alternative |
| "fix the ledger age bug" | `custom/py/ledger.py` override, tests pass |
| "change the dashboard colours" | refusal (Go/TUI) |

Each case asserts: only allowed paths changed, protected files untouched,
validation passes, defaults unchanged elsewhere, and rollback restores the
previous state. The suite runs in `make test` for the agent and in CI.

## Tune-agent testing

- Unit: path allowlist, symlink/`..` rejection, protected-module refusal, danger
  scan, session caps, manifest staleness, snapshot and rollback.
- Integration: temp home + base app + fixture request → merged `run/` compiles
  and preflight passes.
- Safe mode: a deliberately broken override triggers disable-and-start-base.
- Update: apply an override, replace `app/`, confirm the override is preserved
  and reported as active or stale.
- No test may call a live model; the agent loop is tested with a scripted
  model adapter.

## Part II rollout

1. **2a — overlay runtime.** `custom/py/`, `run/` composition, manifest,
   `mosaic custom *`, doctor, safe mode. No agent yet; manual overrides work.
2. **2b — presets.** Recipe catalogue and `mosaic tune --preset`.
3. **2c — agent.** Investigate → plan → diff → apply, validation, snapshots,
   audit, caps.
4. **2d — product.** TUI entry, onboarding copy, docs and the eval suite.

## Part II success criteria

- A tester asks for a behavioural change in plain language, reviews a diff,
  applies it, and sees it in the Me chat without editing Python.
- No agent write ever lands in `app/`, `store/` or `.env`.
- Protected files are never written; unsafe diffs are flagged.
- `mosaic update` keeps overrides, and a stale or broken one is disabled with a
  visible reason.
- Crash-loop safe mode recovers a broken override without losing messages.
- The agent refuses out-of-scope requests and offers a real alternative.

## Update and install safety

- `install.sh` and `mosaic update` already preserve `store/` and `.env`
  (README:28-32). Add `config/`, `custom/` and `backups/` to the preserved list;
  rebuild `run/` from the new `app/` plus the preserved `custom/py/`.
- The updater seeds only missing files; it never overwrites a user file.
- When a release adds a new default key, `mosaic config diff` shows it. The
  updater may append the new key to the user's settings block with a
  `# new in <version>` comment, but only if the key is absent.
- `config_version` lives in the settings block so future migrations are
  explicit.

## Security and limits

- `~/.mosaic/config` is created `0700`; files `0600`.
- Config files are treated as personal content. No telemetry and no network
  reads.
- `validate` warns if a value looks like an API key.
- Caps: persona 4,000 chars; each prompt 40,000 chars; `qa.max_groups` 1–20;
  confidence 0–1; hours 0–23; TTL 1–168; caps 1–60; daily budget 0.10–10.00.
- No shell, Python evaluation or template expressions beyond variable
  substitution.

## Error handling

| Failure | Behaviour |
|---|---|
| Missing file | Use default |
| Unreadable file | Use default, warn in TUI, event in `engine.db` |
| Bad settings line | Skip line, keep other settings, warn |
| Unknown key | Warn, ignore |
| Out-of-range value | Clamp, warn |
| Missing/extra placeholder | Use default for that prompt, warn |
| Prompt renders but model returns bad JSON | Existing retry/fallback chain is unchanged |
| Broken `agent.md` (too long) | Truncate to cap, warn |

A broken config must never stop ingest, delivery or retention.

## Testing

- **Golden render:** capture the current prompt strings with fixture variables
  before extraction; assert the extracted defaults render identically.
- **Unit:** precedence order, env overrides, mtime reload, broken-file fallback,
  clamping, unknown-key warning, placeholder validation, persona truncation,
  `config-set` writer.
- **Onboarding:** model-state test for key → scope → QR; marker prevents a
  re-ask; false is written when the default is chosen.
- **Preflight:** `scripts/preflight.py` renders every default prompt, validates
  the packaged `rules.md`, and validates a user config if present.
- **Manual:** change `agent.md` tone, run `mosaic config test`, confirm the
  answer without a restart; toggle archived and confirm the Radar scope in the
  TUI.
- `make test` continues to run `go vet`/`go build`, `py_compile` and preflight.

## Rollout

1. **Part I — 1a: loader and defaults.** Extract prompts and settings, add
   `agent_config.py`, wire call sites, golden tests. No user-visible change.
2. **Part I — 1b: CLI and validation.** `mosaic config *`, TUI Agent pane,
   update-time seeding and diff.
3. **Part I — 1c: onboarding.** `screenScope`, `config-set`, `.onboarded`,
   upgrade notice.
4. **Part II — 2a: overlay runtime.** `custom/py/`, `run/`, manifest, doctor,
   safe mode. Manual overrides usable before the agent exists.
5. **Part II — 2b: presets.** Recipe catalogue.
6. **Part II — 2c: agent.** Investigate → plan → diff → apply, validation,
   snapshots, audit, caps, evals.
7. **Part II — 2d: product.** TUI entry, docs, onboarding copy.
8. **Deferred.** Per-chat persona overrides and user-defined skills. The layout
   leaves room; neither ships in these parts.

## Success criteria

- A tester changes tone and archived scope by editing `agent.md` and `rules.md`,
  with no Python edits.
- Edits apply without reinstalling and survive `mosaic update`.
- `mosaic config validate` catches every realistic mistake.
- Defaults reproduce current behaviour byte-for-byte.
- The first run asks about archived chats, and the answer is honoured by the
  Radar and Q&A catalog.
- A tester can request a behavioural code change in plain language, approve a
  diff, see it work, and undo it with one command.
- Agent overrides survive updates, and no agent write ever reaches `app/`,
  `store/` or `.env`.

## Resolved decisions

1. **Persona scope:** one global persona in phase 1. Per-chat is deferred.
2. **Skills:** not implemented. No `skills/` directory is created; the layout
   can add one later.
3. **Settings format:** Markdown with a `key = value` block, not YAML, to stay
   stdlib-only and human-editable.
4. **Writer ownership:** Python owns parsing and writing of `rules.md`; the Go
   TUI calls the Python helper.
5. **Agent audience:** every alpha tester, on the installed app.
6. **Agent language scope:** Python only. The Go bridge and TUI are out of
   reach because their source is not shipped.
7. **Agent trust boundary:** diff approval; protected modules (`outbox.py`,
   `mechat.py`, `alerts.py`, guard files, installer/launcher) are refused.
8. **Edit storage:** `custom/py/` overlay merged into a disposable `run/` at
   start. `app/` stays pristine.
9. **Agent capabilities:** fixed tools, no shell, no network, session step and
   cost caps, snapshots, rollback and crash-loop safe mode.
10. **Verification:** the agent does not ship without the fixture eval suite and
    the safe-mode/update tests.

## Risks

- **Prompt regression.** Mitigated by golden renders and fallback.
- **Users break JSON output.** Mitigated by schema validation, retry and
  fallback to the default prompt.
- **Higher token spend from persona/prose.** Mitigated by length caps and the
  existing daily budget.
- **Support confusion.** Mitigated by `mosaic config diff` and the TUI Agent
  pane showing active overrides.
- **Two config owners (Go and Python).** Mitigated by making Python the only
  parser/writer and having the TUI shell out.
- **Self-modifying code with WhatsApp access.** The agent can only write
  `config/` and `custom/py/`, never `app/`, `store/` or `.env`; sending,
  recipient resolution, gates and guard files are refused; diffs need approval;
  unsafe patterns are flagged; every apply is snapshotted. This lowers the risk,
  it does not eliminate it.
- **Base drift after updates.** `manifest.json` records the base hash; stale
  overrides are reported by `custom doctor`, disabled when they fail validation,
  and safe mode starts from the base app after a crash loop.
- **Model errors or runaway cost.** Plan-then-diff, step and USD caps, evals and
  one-command rollback.
- **Unbounded expectations.** The UI states what the agent cannot change and
  refuses out-of-scope requests instead of faking a result.
- **A bad override blamed on Mosaic.** The audit log, `custom diff`, `reset` and
  safe mode give support a clear recovery path.
