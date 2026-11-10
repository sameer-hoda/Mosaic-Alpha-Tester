# Nembu rename — migration notes

Date: 2026-11-10  
Branch: (current worktree) — commit will be shown in final report

## Inventory of changed surfaces

### Brand identity
- Product/CLI: `nembu` (lowercase), `Nembu` in prose/titles, with lemon 🍋 in assistant labels.
- Logo: copied `/Users/sameerhoda/Downloads/Nembu-Brand/nembu-logo-tile.svg` and `nembu-logo-tile.svg.png` into `assets/brand/`.
- Mark: created `assets/brand/nembu-mark.svg` as original vector recreation from same wedge (no word), using palette lemon `#F9D71C`, deep blue `#2E6DB4`, cream `#FFF4CF`, ink `#1A1C21`, paper `#F7F5F0`. No Adobe Stock reference/watermark used.
- Palette documented in README and brand assets.
- README updated: install path `~/.nembu`, binary `nembu`, legacy `mosaic` wrapper, migration note, logo reference, favicon/OG guidance.
- CHANGELOG: added 1.1.0 Nembu rename entry, updated 1.0.0 historical blurb to reflect Nembu (previously Mosaic).
- SECURITY.md: updated paths to `~/.nembu`, assistant name to Nembu.

### Paths and env vars
- `~/.mosaic` → `~/.nembu` with legacy fallback read.
- Env:
  - `MOSAIC_HOME` → `NEMBU_HOME` (fallback: check NEMBU then MOSAIC, then default). Same for `MOSAIC_PYTHON` → `NEMBU_PYTHON`, `MOSAIC_CONFIG_DIR` → `NEMBU_CONFIG_DIR`, `MOSAIC_GEMINI_BASE` → `NEMBU_GEMINI_BASE`, `MOSAIC_API_BASE` → `NEMBU_API_BASE`, `MOSAIC_VERBOSE` → `NEMBU_VERBOSE`, `MOSAIC_HISTORY_DAYS` → `NEMBU_HISTORY_DAYS`, `MOSAIC_LOG_LEVEL` → `NEMBU_LOG_LEVEL`, `MOSAIC_QR_TERMINAL` → `NEMBU_QR_TERMINAL`, `MOSAIC_DAILY_USD_CAP` → `NEMBU_DAILY_USD_CAP`, `MOSAIC_SWEEP_CHATS` → `NEMBU_SWEEP_CHATS`, `MOSAIC_SWEEP_DELAY_S` → `NEMBU_SWEEP_DELAY_S`, `MOSAIC_CARD_BATCH` → `NEMBU_CARD_BATCH`, `MOSAIC_PROFILE_BATCH` → `NEMBU_PROFILE_BATCH`, `MOSAIC_SEARCH_DB` → `NEMBU_SEARCH_DB`, `MOSAIC_BODY_CHARS` → `NEMBU_BODY_CHARS`, `MOSAIC_DEBUG_PROMPTS` → `NEMBU_DEBUG_PROMPTS`, `MOSAIC_NOW` → `NEMBU_NOW`, `MOSAIC_FAULT` → `NEMBU_FAULT`, `MOSAIC_QA_MERGE` → `NEMBU_QA_MERGE`.
  - Implemented via `_envload.getenv`, `getenv_int`, `getenv_float`, `resolve_home`, and Go helpers `envWithFallback`, `envFlag`, `resolveHome`, `resolveConfigDir`.
- Binary: `mosaic` → `nembu` in `bridge/` build, launcher `nembu`, `install.sh` produces `nembu`. Legacy `mosaic` kept as symlink or deprecated wrapper that prints warning and execs `nembu`.
- LaunchAgent: `ai.mosaic.bridge` → `ai.nembu.bridge` with legacy plist cleanup.
- systemd: `mosaic.service` → `nembu.service` with legacy unit stop/disable.
- Log: `mosaic.log` → `nembu.log` with fallback to old log if new missing.
- Config dir: `.mosaic-config` → `.nembu-config` with legacy fallback existence check.
- `STORE_DIR`, `MESSAGES_DB_PATH`, etc unchanged but now default under `~/.nembu`.

### Go code
- `bridge/main.go`: `deviceName = "Nembu"`, verbose flag uses `NEMBU_VERBOSE` fallback, `historyWindowDays` uses `NEMBU_HISTORY_DAYS`, log level `NEMBU_LOG_LEVEL`, snapshot `NEMBU_SNAPSHOT`, comments updated.
- `bridge/runtime.go`: added env helpers (`envWithFallback`, `envFlag`, `resolveHome`, `resolveConfigDir`), updated `envFilePath`, `pythonCandidates` to use `NEMBU_PYTHON`, QR terminal check to `NEMBU_QR_TERMINAL`, store path fallback preserved.
- `bridge/tui.go`: `nembuConfigDir` new func with `.nembu-config`/`.mosaic-config` existence check, `mosaicConfigDir` kept as alias, `apiBase` and `geminiBaseURL` use NEMBU with fallback, branding `Mosaic` → `Nembu 🍋`, scope question updated.

### Python code
- `mstate.py`: new `_getenv`, `_getenv_int`, `_getenv_float`, `_resolve_home` with legacy fallback, `RETENTION_DAYS`, `DEBUG_PROMPTS`, `MAX_BODY_CHARS`, `now_local` updated, docstring updated, marker comment updated to Nembu.
- `_envload.py`: added `getenv`, `getenv_int`, `getenv_float`, `resolve_home` helpers.
- `agent_config.py`: `config_dir()` checks `NEMBU_CONFIG_DIR` then `MOSAIC_CONFIG_DIR`, checks `NEMBU_HOME`/`MOSAIC_HOME`, checks `~/.nembu-config` then `~/.mosaic-config` existence, fallback to `~/.nembu/config`.
- `budget.py`, `followup.py`, `qa_profiles.py`, `qa_search.py`, `engine.py`, `qa.py`: updated to use `_envload` helpers with fallback.
- `conversation.py`: role `"mosaic"` → `"nembu"` with dual-read (strips both `_Mosaic_` and `_Nembu_` markers), docstring mentions legacy.
- `qa.py`: forward detection `_NEMBU_FORWARD_RE = re.compile(r"^\s*(?:\*Mosaic\*|\*Nembu\*|◆|🍋)")` with alias `_MOSAIC_FORWARD_RE`, headers `*Mosaic*` → `*Nembu* 🍋`, prompts updated, `is_forwarded_mosaic` now checks new regex and doc mentions legacy.
- `mechat.py`: welcome texts `Mosaic` → `Nembu 🍋`, howto uses `nembu status/logs`.
- `worker.py`: failure texts use `nembu` and 🍋.
- `alerts.py`, `ledger.py`, `outbox.py`: comments updated, bot noise includes both `*Mosaic*` and `*Nembu*`.
- `scripts/qa_simulate.py`, `qa_report.py`, `preflight.py`: updated to NEMBU paths and labels, `mosaic-head` → `nembu-head`.
- `scripts/reset_onboarding.sh`: rewritten for NEMBU with legacy fallback, migrates both launch agents and systemd units.

### Launcher and installer
- New launcher `nembu` (12598 bytes) with full NEMBU env, legacy fallback, migration handling for logs, binary, plist, systemd.
- Old `mosaic` file replaced with deprecated wrapper that echoes warning and execs `nembu`.
- `install.sh`: rewritten for NEMBU, repo `sameer-hoda/Nembu-Alpha-Tester`, asset `nembu-${VER}-${PLATFORM}.tar.gz` with fallback to legacy `mosaic-` during transition, migration function `migrate_legacy` that copies `~/.mosaic` → `~/.nembu` with `cp -a`/`rsync -a` preserving perms, `chmod 0700` store, `0600` .env, timestamped backup at `~/.nembu-migration-backup-<ts>`, also migrates `.mosaic-config` → `.nembu-config`, creates both `bin/nembu` and legacy `bin/mosaic` wrapper, PATH line updated to `# Nembu`.

### Makefile, GitHub Actions, docs
- `Makefile`: install URL updated to Nembu repo.
- `.github/workflows/release.yml`: builds `nembu-darwin-*`, packages `nembu-*-*.tar.gz`, with legacy symlink `mosaic` → `nembu` inside pkg, checksums for `nembu-*.tar.gz`.
- `.env.example`: `MOSAIC_HISTORY_DAYS` → `NEMBU_HISTORY_DAYS` with legacy comment.
- `docs/qa-test-cases.md`: `Mosaic` → `Nembu 🍋`, `mosaic logs` → `nembu logs`, env `MOSAIC_QA_MERGE/BODY_CHARS` → `NEMBU_… (legacy MOSAIC_…)`.

### Tests
- `bridge/onboarding_test.go`: `testHome` sets `NEMBU_HOME`/`NEMBU_CONFIG_DIR` plus legacy, env vars `NEMBU_GEMINI_BASE`, `NEMBU_API_BASE`, expected scope view string updated to `Which chats should Nembu watch? 🍋`.
- `tests/test_onboarding.py`: updated to use `.nembu` paths, `NEMBU_HOME`, binary `nembu`, unit `nembu.service`, `ExecStart` check updated, uninstall checks `Nembu removed`, scripts parse includes both `nembu` and `mosaic`, added NEMBU env handling.
- `tests/test_qa_done.py`: prefix `nembu-done-test-`, env `NEMBU_HOME`/`NEMBU_CONFIG_DIR`, forward detection tests now include Nembu headers with 🍋, `*Mosaic*` still tested as legacy.
- New `tests/test_nembu_migration.py`: 13 tests covering legacy env fallback, path fallback (`.mosaic-config` → `.nembu-config`, `~/.mosaic` → `~/.nembu`), migration script presence, launcher new paths, old DB role compatibility (role `nembu` not `mosaic`, stripping `_Mosaic_` marker), forward summary detection supports both brands and 🍋, headers include lemon, Go files use NEMBU.

## Migration behavior

### install.sh
- Defines `NEMBU_HOME="${NEMBU_HOME:-${MOSAIC_HOME:-$HOME/.nembu}}"` and `LEGACY_HOME="${MOSAIC_HOME:-$HOME/.mosaic}"`.
- If `NEMBU_HOME` does not exist and `LEGACY_HOME` exists, `migrate_legacy` runs:
  - Creates timestamp `YYYYMMDD-HHMMSS`, backup dir `~/.nembu-migration-backup-<ts>` (uses `rsync -a` if available, else `cp -a`).
  - Copies entire legacy home into new home (`cp -a legacy/. new/`), ensures `.env` copied if hidden.
  - `chmod 0700 store`, `0600 .env`, `0700 config` and `0600 config/*` if present.
  - Also migrates `~/.mosaic-config` → `~/.nembu-config` if new config missing.
  - Does NOT delete `~/.mosaic`.
- After migration, proceeds with normal install: `rm -rf $APP $PYDIR`, `mv $TMP/app $APP`, etc., creates `bin/nembu` and legacy `bin/mosaic` wrapper with deprecation notice.
- PATH line added as `# Nembu` with new bindir, old `# Mosaic` lines removed on uninstall.
- Asset download: tries `nembu-${VER}-${PLATFORM}.tar.gz` first, fallback to `mosaic-${VER}-${PLATFORM}.tar.gz` during transition.

### launcher `nembu`
- `NEMBU_HOME` with fallback to `MOSAIC_HOME`, `LEGACY_HOME` for fallback paths.
- `STORE_DIR` fallback: if `$NEMBU_HOME/store` missing, tries legacy store.
- `NEMBU_PYTHON` fallback to `MOSAIC_PYTHON`.
- `BRIDGE` resolution: tries `$NEMBU_HOME/app/bin/nembu`, then `$NEMBU_HOME/app/bin/mosaic` (legacy), then legacy home binaries.
- `LOG` fallback: tries `nembu.log`, then `mosaic.log`, then legacy home logs.
- `write_plist` writes `ai.nembu.bridge`, `write_unit` writes `nembu.service`.
- `service_start` cleans up legacy plist/unit (`ai.mosaic.bridge`, `mosaic.service`) before starting new.
- `service_stop` stops both new and legacy agents/services and kills pid files in both new and legacy store locations.
- `status` Python snippet also checks legacy store for `bridge_token` if new missing.
- `update_all` tries both `$NEMBU_HOME/app/install.sh` and legacy.
- `uninstall_all` removes `app`, `python`, `bin/nembu`, `bin/mosaic`, cleans both `# Nembu` and `# Mosaic` PATH lines.
- Quitting TUI does not stop service (same as before).

### Python/Go fallback
- All code that previously did `os.getenv("MOSAIC_X")` now does `_envload.getenv("NEMBU_X", "MOSAIC_X", "")` or `envWithFallback("NEMBU_X", "MOSAIC_X")` — new env preferred, legacy accepted.
- Home resolution: if `NEMBU_HOME` set, use it; else if `MOSAIC_HOME` set, use it; else if `~/.nembu` exists, use it; else if `~/.mosaic` exists, use it (fallback); else default `~/.nembu`.
- Config dir: if `NEMBU_CONFIG_DIR`/`MOSAIC_CONFIG_DIR` set, use it; else if `NEMBU_HOME`/`MOSAIC_HOME` set, use `home/config`; else if `~/.nembu-config` exists, use it; else if `~/.mosaic-config` exists, use it; else `~/.nembu/config`.
- DB role: `conversation.recent_dialogue` now outputs role `nembu`; it strips both `_Mosaic_` and `_Nembu_` markers, so old outbox rows with `_Mosaic_` still render. No DB migration needed, but new writes use `nembu`.
- Forward summary: regex supports `*Mosaic*`, `*Nembu*`, `◆`, `🍋` — legacy summaries still filtered.

## Tests run

Go:
```
cd bridge && go vet ./... && go build ./... && go test ./...
ok  	whatsapp-client	0.495s
```

Python (isolated per file to avoid env pollution):
```
python3 -m py_compile *.py scripts/*.py
for f in tests/test_*.py; do python3 -m unittest tests.<mod>; done
== test_nembu_migration
13 tests OK
== test_onboarding
24 tests OK
== test_qa_done
51 tests OK
```

Preflight:
```
python3 scripts/preflight.py
PREFLIGHT OK — runtime is stdlib-only and configured
```

All 88 Python tests pass (13+24+51), Go tests pass, preflight OK.

## Remaining manual steps

These are out-of-scope for this worktree per task (do not push/deploy/rename GitHub repo, do not touch live ~/.mosaic):

- **GitHub repo rename**: rename `sameer-hoda/Mosaic-Alpha-Tester` → `sameer-hoda/Nembu-Alpha-Tester` on GitHub UI, update remote URLs, transfer issues, update branch protection. The code already points to new name in README/Makefile/install.sh.
- **Live migration/deploy**: on tester Macs, run new `install.sh` from Nembu repo; it will auto-migrate `~/.mosaic` → `~/.nembu` with backup. Verify `nembu status`, `nembu logs`, and that `~/.mosaic` still exists. Then manually remove old LaunchAgent `ai.mosaic.bridge` if any leftover (launcher already does).
- **GTM/posters**: update marketing site copy, posters, onboarding decks from Mosaic to Nembu, use new palette lemon `#F9D71C`, deep blue `#2E6DB4`, cream `#FFF4CF`, ink `#1A1C21`, paper `#F7F5F0`, logo `assets/brand/nembu-logo-tile.svg`, mark `assets/brand/nembu-mark.svg` for favicon/OG. Do not use Adobe Stock reference or watermark.
- **Social preview**: generate new OG image with logo tile on paper background, favicon from mark (256×256 rounded).
- **Docs/superagent**: intentionally not edited per instruction; after rename, update `docs/superagent/README.md`, `board.md`, etc if desired.
- **Release**: tag `v1.1.0` with new artifacts `nembu-*.tar.gz`, publish with `gh release`.
- **Deprecation**: after 1-2 releases, remove legacy `mosaic` wrapper and `MOSAIC_` fallback (currently kept for migration).

## Intentional remaining `mosaic` strings — reason

- `CHANGELOG.md`: historical entries mention Mosaic as previous brand, plus migration notes documenting `~/.mosaic` → `~/.nembu`.
- `install.sh`, `nembu`, `bridge/runtime.go`, `bridge/tui.go`, `agent_config.py`, `_envload.py`, `mstate.py`, `scripts/reset_onboarding.sh`: legacy fallback paths and env vars (`MOSAIC_HOME`, `~/.mosaic`, `.mosaic-config`, `ai.mosaic.bridge`, `mosaic.service`, `mosaic.log`) are intentionally kept to read old installs when new missing. They are guarded by "if new missing and old exists" and never overwrite new.
- `qa.py`: regex `_NEMBU_FORWARD_RE = r"^\s*(?:\*Mosaic\*|\*Nembu\*|◆|🍋)"` includes `*Mosaic*` for backward compatibility — old summaries must still be detected as forwarded and excluded from evidence.
- `conversation.py`: `body.replace("_Mosaic_\n", "")` keeps stripping legacy marker alongside new `_Nembu_`.
- `ledger.py`: `BOT_NOISE` includes `"⚠️ *Mosaic*"` as legacy, plus new `"⚠️ *Nembu* 🍋"`.
- `tests/*`: tests explicitly check legacy `*Mosaic*` detection and fallback behavior to ensure migration works; `test_nembu_migration` asserts both old and new headers.
- `docs/superagent/*`, `docs/superpowers/specs/*`: not edited per task instruction.
- `docs/qa-test-cases.md`: now updated to Nembu, but historical comments may still reference Mosaic in passing (none remain after edit).
- `mosaic` file itself: deprecated wrapper, intentional, prints warning and execs `nembu`.
- `assets/brand/nembu-logo-tile.svg.png` is a PNG preview of SVG, name includes original file name but content is new brand.
