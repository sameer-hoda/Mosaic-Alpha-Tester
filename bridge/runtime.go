package main

// runtime.go — paths, loopback token, python hooks, pairing and the engine
// worker. This is the launch layer; there is no web UI.

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/mdp/qrterminal"
	"go.mau.fi/whatsmeow"
	waHistorySync "go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// ── env helpers with legacy MOSAIC_ fallback ─────────────────────────────────

func envWithFallback(newKey, oldKey string) string {
	if v := strings.TrimSpace(os.Getenv(newKey)); v != "" {
		return v
	}
	if oldKey != "" {
		if v := strings.TrimSpace(os.Getenv(oldKey)); v != "" {
			return v
		}
	}
	return ""
}

func envFlag(newKey, oldKey string) bool {
	return envWithFallback(newKey, oldKey) == "1"
}

func resolveHome() string {
	// New env first
	if h := envWithFallback("NEMBU_HOME", "MOSAIC_HOME"); h != "" {
		return h
	}
	// Default to ~/.nembu, but fallback to ~/.mosaic if that is the only existing layout
	homeBase := os.Getenv("HOME")
	if homeBase == "" {
		homeBase = os.Getenv("USERPROFILE")
	}
	nembuDefault := filepath.Join(homeBase, ".nembu")
	mosaicDefault := filepath.Join(homeBase, ".mosaic")
	if _, err := os.Stat(nembuDefault); err == nil {
		return nembuDefault
	}
	if _, err := os.Stat(mosaicDefault); err == nil {
		return mosaicDefault
	}
	return nembuDefault
}

func resolveConfigDir() string {
	if d := envWithFallback("NEMBU_CONFIG_DIR", "MOSAIC_CONFIG_DIR"); d != "" {
		return d
	}
	if h := envWithFallback("NEMBU_HOME", "MOSAIC_HOME"); h != "" {
		return filepath.Join(h, "config")
	}
	homeBase := os.Getenv("HOME")
	if homeBase == "" {
		homeBase = os.Getenv("USERPROFILE")
	}
	// Prefer ~/.nembu-config, fallback to ~/.mosaic-config
	nembuCfg := filepath.Join(homeBase, ".nembu-config")
	mosaicCfg := filepath.Join(homeBase, ".mosaic-config")
	if _, err := os.Stat(nembuCfg); err == nil {
		return nembuCfg
	}
	if _, err := os.Stat(mosaicCfg); err == nil {
		return mosaicCfg
	}
	return nembuCfg
}

var (
	bridgeTokenVal string

	qrMu          sync.Mutex
	currentQRCode string

	// History-sync tracking. Atomics: written on every store, read on every
	// status poll.
	syncPairedAt   atomic.Int64
	syncLastWrite  atomic.Int64
	syncStoredMsgs atomic.Int64

	// Real sync progress reported by the phone.
	syncPhaseMu    sync.Mutex
	syncPhase      string
	syncProgress   atomic.Int64
	syncProgressAt atomic.Int64 // unix time the progress VALUE last changed
	syncConvs      atomic.Int64
	syncLastEvent  atomic.Int64
	syncSawInitial atomic.Bool
	syncReady      atomic.Bool
	syncBootAt     = time.Now()

	groupNameQueue sync.Map // chatJID -> types.JID

	resolveMu     sync.Mutex
	lastResolveAt time.Time
)

// ── sync state (real progress + ready persistence) ───────────────────────────

type syncSnapshot struct {
	Ready    bool   `json:"ready"`
	ReadyAt  string `json:"ready_at,omitempty"`
	Chats    int    `json:"chats"`
	Messages int    `json:"messages"`
	Oldest   string `json:"oldest,omitempty"`
}

func syncStatePath() string { return storePath("sync_state.json") }

func loadSyncState() syncSnapshot {
	var s syncSnapshot
	if b, err := os.ReadFile(syncStatePath()); err == nil {
		_ = json.Unmarshal(b, &s)
	}
	return s
}

func clearSyncState() { _ = os.Remove(syncStatePath()) }

func (store *MessageStore) syncCounts() (chats, messages int, oldest string) {
	if store == nil || store.db == nil {
		return 0, 0, ""
	}
	_ = store.db.QueryRow("SELECT COUNT(*) FROM chats").Scan(&chats)
	_ = store.db.QueryRow("SELECT COUNT(*) FROM messages").Scan(&messages)
	var ts string
	if store.db.QueryRow("SELECT COALESCE(MIN(timestamp),'') FROM messages").Scan(&ts) == nil && len(ts) >= 10 {
		oldest = ts[:10]
	}
	return chats, messages, oldest
}

func writeSyncState(store *MessageStore) {
	chats, messages, oldest := store.syncCounts()
	s := syncSnapshot{
		Ready: true, ReadyAt: time.Now().Format(time.RFC3339),
		Chats: chats, Messages: messages, Oldest: oldest,
	}
	if b, err := json.Marshal(s); err == nil {
		_ = os.WriteFile(syncStatePath(), b, 0644)
	}
}

func noteHistoryEvent(syncType waHistorySync.HistorySync_HistorySyncType, progress, convs, stored int) {
	syncPhaseMu.Lock()
	syncPhase = syncType.String()
	syncPhaseMu.Unlock()
	p := int64(progress)
	if syncProgress.Load() != p {
		syncProgressAt.Store(time.Now().Unix())
	}
	syncProgress.Store(p)
	syncConvs.Add(int64(convs))
	syncStoredMsgs.Add(int64(stored))
	syncLastEvent.Store(time.Now().Unix())
	if syncType == waHistorySync.HistorySync_INITIAL_BOOTSTRAP || syncType == waHistorySync.HistorySync_RECENT {
		syncSawInitial.Store(true)
	}
}

func currentSyncPhase() string {
	syncPhaseMu.Lock()
	defer syncPhaseMu.Unlock()
	return syncPhase
}

// syncStatus is the "sync" object exposed by /api/status.
func syncStatus(store *MessageStore) map[string]any {
	chats, messages, oldest := store.syncCounts()
	return map[string]any{
		"phase":           currentSyncPhase(),
		"progress":        int(syncProgress.Load()),
		"chats":           chats,
		"messages":        messages,
		"oldest":          oldest,
		"ready":           syncReady.Load(),
		"mechat_resolved": readMechatState().JID != "",
	}
}

func markSyncReady(client *whatsmeow.Client, store *MessageStore, logger waLog.Logger) {
	if !syncReady.CompareAndSwap(false, true) {
		return
	}
	writeSyncState(store)
	logger.Infof("History sync ready")
	// The one-shot messages are driven by the sync watcher: early welcome at
	// Me-chat resolution and how-to at Ready/dashboard. Each has its own
	// persisted flag and bounded retry, so neither repeats across restarts.
	go resolveQueuedGroups(client, store, logger)
}

// syncStalled reports the tail of history sync: WhatsApp's percentage is not
// time-linear, so a long flat stretch at >=80% means "finishing", not "stuck".
func syncStalled(progress, progressChangedAt int64, now time.Time) bool {
	if progress < 80 || progressChangedAt == 0 {
		return false
	}
	return now.Sub(time.Unix(progressChangedAt, 0)) >= 60*time.Second
}

// syncReadyDecision is the pure readiness rule, extracted so partial-history,
// fresh-account, stalled-tail and no-history paths are testable without a phone.
func syncReadyDecision(me, sawInitial bool, progress int64, phase string,
	lastEventUnix, progressChangedAt int64, bootAt, now time.Time) bool {
	idle := now.Sub(time.Unix(lastEventUnix, 0))
	if lastEventUnix == 0 {
		idle = now.Sub(bootAt)
	}
	// Do not wait for a nominal 100%: the last chunk can trickle indefinitely.
	if me && syncStalled(progress, progressChangedAt, now) {
		return true
	}
	progressDone := progress >= 100 && (phase == "RECENT" || phase == "FULL")
	if me && sawInitial {
		if (progressDone && idle >= 20*time.Second) || idle >= 90*time.Second {
			return true
		}
	}
	// Fresh account: no history arrives at all. Continue after 90 s.
	if me && lastEventUnix == 0 && idle >= 90*time.Second {
		return true
	}
	return false
}

const (
	earlyWelcomeMaxAttempts = 5
	howtoMaxAttempts        = 3
	notificationRetryEvery  = 30 * time.Second
)

var (
	earlyMu       sync.Mutex
	earlyAttempts int
	earlyLast     time.Time

	howtoMu       sync.Mutex
	howtoAttempts int
	howtoLast     time.Time
)

// earlyWelcomeDue fires as soon as the Me chat is resolved, never gated on sync.
func earlyWelcomeDue(me, sent bool, attempts int, lastAttempt, now time.Time) bool {
	if !me || sent || attempts >= earlyWelcomeMaxAttempts {
		return false
	}
	if lastAttempt.IsZero() {
		return true
	}
	return now.Sub(lastAttempt) >= notificationRetryEvery
}

// howtoDue fires once the dashboard opens or sync is Ready.
func howtoDue(me, sent bool, attempts int, lastAttempt, now time.Time) bool {
	if !me || sent || attempts >= howtoMaxAttempts {
		return false
	}
	if lastAttempt.IsZero() {
		return true
	}
	return now.Sub(lastAttempt) >= notificationRetryEvery
}

func maybeEarlyWelcome(logger waLog.Logger) {
	state := readMechatState()
	if state.JID == "" {
		return
	}
	earlyMu.Lock()
	due := earlyWelcomeDue(true, state.WelcomedEarly || state.Welcomed,
		earlyAttempts, earlyLast, time.Now())
	if due {
		earlyAttempts++
		earlyLast = time.Now()
	}
	earlyMu.Unlock()
	if due {
		logger.Infof("Sending early welcome (attempt %d)", earlyAttempts)
		go runPython("mechat.py", "--early-welcome")
	}
}

func maybeHowto(logger waLog.Logger) {
	state := readMechatState()
	if state.JID == "" {
		return
	}
	howtoMu.Lock()
	due := howtoDue(true, state.HowtoSent, howtoAttempts, howtoLast, time.Now())
	if due {
		howtoAttempts++
		howtoLast = time.Now()
	}
	howtoMu.Unlock()
	if due {
		logger.Infof("Sending how-to (attempt %d)", howtoAttempts)
		go runPython("mechat.py", "--howto")
	}
}

// startSyncWatcher flips historyReady when the phone is done (or when it never
// sends history at all), and persists the transition across restarts. It also
// drives the two one-shot messages: early welcome before Ready, how-to after.
func startSyncWatcher(client *whatsmeow.Client, store *MessageStore, logger waLog.Logger) {
	if st := loadSyncState(); st.Ready {
		syncReady.Store(true)
	}
	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			if !syncReady.Load() {
				maybeEarlyWelcome(logger)
			}
			if syncReady.Load() {
				maybeHowto(logger)
				continue
			}
			me := readMechatState().JID != ""
			last := syncLastEvent.Load()
			ready := syncReadyDecision(me, syncSawInitial.Load(),
				syncProgress.Load(), currentSyncPhase(), last,
				syncProgressAt.Load(), syncBootAt, time.Now())
			if ready {
				markSyncReady(client, store, logger)
			}
		}
	}()
}

func queueGroupName(chatJID string, jid types.JID) {
	groupNameQueue.Store(chatJID, jid)
}

// resolveQueuedGroups resolves every group name that history sync skipped in
// one background pass, instead of making a network call per unnamed group.
func resolveQueuedGroups(client *whatsmeow.Client, store *MessageStore, logger waLog.Logger) {
	groupNameQueue.Range(func(k, v any) bool {
		chatJID, _ := k.(string)
		jid, ok := v.(types.JID)
		if !ok {
			return true
		}
		groupInfo, err := client.GetGroupInfo(context.Background(), jid)
		if err == nil && groupInfo.Name != "" {
			ownUser := ""
			if client.Store != nil && client.Store.ID != nil {
				ownUser = client.Store.ID.User
			}
			if clean := cleanChatName(groupInfo.Name, jid, chatJID, ownUser); clean != "" {
				_ = store.StoreChat(chatJID, clean, time.Now())
			}
		}
		groupNameQueue.Delete(k)
		return true
	})
}

func storeDir() string {
	if dir := os.Getenv("STORE_DIR"); dir != "" {
		return dir
	}
	return "store"
}

func storePath(name string) string {
	return filepath.Join(storeDir(), name)
}

func absStorePath(name string) string {
	abs, err := filepath.Abs(storePath(name))
	if err != nil {
		return storePath(name)
	}
	return abs
}

// envFilePath resolves .env under NEMBU_HOME (legacy MOSAIC_HOME) when
// installed, and the repo directory when running from a checkout.
func envFilePath() string {
	if home := envWithFallback("NEMBU_HOME", "MOSAIC_HOME"); home != "" {
		cand := filepath.Join(home, ".env")
		if _, err := os.Stat(cand); err == nil {
			return cand
		}
		// If new home missing but legacy exists, fallback check
		if home == os.Getenv("NEMBU_HOME") {
			if legacy := strings.TrimSpace(os.Getenv("MOSAIC_HOME")); legacy != "" {
				lc := filepath.Join(legacy, ".env")
				if _, err := os.Stat(lc); err == nil {
					return lc
				}
			}
		}
		return cand
	}
	// Also try legacy file locations for fallback
	if legacy := strings.TrimSpace(os.Getenv("MOSAIC_HOME")); legacy != "" {
		_ = legacy
	}
	return ".env"
}

func writePID(name string, pid int) {
	_ = os.WriteFile(storePath(name), []byte(strconv.Itoa(pid)), 0644)
}

func removePID(name string) { _ = os.Remove(storePath(name)) }

func stopProcess(name string) {
	b, err := os.ReadFile(storePath(name))
	if err == nil {
		if pid, convErr := strconv.Atoi(strings.TrimSpace(string(b))); convErr == nil && pid > 0 {
			_ = syscall.Kill(pid, syscall.SIGTERM)
		}
	}
	removePID(name)
}

// looksLikeGeminiKey accepts both the classic AIza... keys and the newer
// AQ.... keys issued by AI Studio. The API call is the real validation.
func looksLikeGeminiKey(k string) bool {
	k = strings.TrimSpace(k)
	if len(k) < 20 {
		return false
	}
	return !strings.ContainsAny(k, " \t\r\n\"'")
}

// hasGeminiKey checks the process env and the installed .env. Pairing and the
// worker wait for a key so the QR never expires behind the key screen.
func hasGeminiKey() bool {
	if looksLikeGeminiKey(os.Getenv("GEMINI_API_KEY")) {
		return true
	}
	data, err := os.ReadFile(envFilePath())
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "GEMINI_API_KEY=") {
			v := strings.Trim(strings.SplitN(line, "=", 2)[1], "\"' ")
			return looksLikeGeminiKey(v)
		}
	}
	return false
}

func waitForKey() {
	for !hasGeminiKey() {
		time.Sleep(2 * time.Second)
	}
}

func randomHex(n int) string {
	buf := make([]byte, n)
	if _, err := rand.Read(buf); err != nil {
		return fmt.Sprintf("fallback-%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(buf)
}

func initToken() {
	token := os.Getenv("BRIDGE_TOKEN")
	if token == "" {
		token = randomHex(12)
	}
	bridgeTokenVal = token
	if err := os.WriteFile(storePath("bridge_token"), []byte(token), 0600); err != nil {
		fmt.Printf("bridge token persist error: %v\n", err)
	}
	// WriteFile only applies the mode on create; tighten an existing file too.
	_ = os.Chmod(storePath("bridge_token"), 0600)
}

// waitForOnboarding keeps pairing behind the scope-choice screen on a fresh
// install, while still self-healing for upgraded/background installs that never
// show the screen: after a bounded wait it writes today's default and proceeds.
func waitForOnboarding() {
	deadline := time.Now().Add(30 * time.Second)
	for {
		if scopeOnboarded() {
			return
		}
		if time.Now().After(deadline) {
			_ = persistScope(false)
			return
		}
		time.Sleep(2 * time.Second)
	}
}

func bridgeToken() string { return bridgeTokenVal }

func requireToken(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		got := r.Header.Get("X-Bridge-Token")
		if subtle.ConstantTimeCompare([]byte(got), []byte(bridgeTokenVal)) != 1 {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "invalid token"})
			return
		}
		next(w, r)
	}
}

func writeJSON(w http.ResponseWriter, code int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(payload)
}

// ── Me-chat state (single source of truth: store/mechat.json) ────────────────

type mechatState struct {
	JID           string  `json:"jid"`
	How           string  `json:"how"`
	Phone         string  `json:"phone"`
	Resolved      float64 `json:"resolved_at"`
	Welcomed      bool    `json:"welcomed"`
	WelcomedEarly bool    `json:"welcomed_early"`
	HowtoSent     bool    `json:"howto_sent"`
}

func readMechatState() mechatState {
	var state mechatState
	data, err := os.ReadFile(storePath("mechat.json"))
	if err != nil {
		return state
	}
	_ = json.Unmarshal(data, &state)
	return state
}

// ── python bridge helpers ─────────────────────────────────────────────────────

func pythonEnv() []string {
	return append(os.Environ(),
		"BRIDGE_TOKEN="+bridgeTokenVal,
		"STORE_DIR="+absStorePath(""),
		"MESSAGES_DB_PATH="+absStorePath("messages.db"),
		"WHATSAPP_DB_PATH="+absStorePath("whatsapp.db"),
		"WA_API_URL=http://127.0.0.1:8090/api/send",
	)
}

// scriptSearchDirs orders Python lookup: the bundled app sets PY_SCRIPTS_DIR;
// dev checkouts fall back to CWD, scripts/, parent, parent scripts/.
func scriptSearchDirs() []string {
	dirs := []string{}
	if d := os.Getenv("PY_SCRIPTS_DIR"); d != "" {
		dirs = append(dirs, d)
	}
	return append(dirs, ".", "scripts", "..", filepath.Join("..", "scripts"))
}

func findScript(name string) string {
	for _, dir := range scriptSearchDirs() {
		cand := filepath.Join(dir, name)
		if _, err := os.Stat(cand); err == nil {
			return cand
		}
	}
	return name
}

// bundlePythonFrom maps a Contents/MacOS binary to its bundled interpreter.
func bundlePythonFrom(exe string) string {
	if filepath.Base(filepath.Dir(exe)) != "MacOS" {
		return ""
	}
	p := filepath.Join(filepath.Dir(filepath.Dir(exe)), "Resources", "python-runtime", "bin", "python3")
	if st, err := os.Stat(p); err == nil && !st.IsDir() {
		return p
	}
	return ""
}

func bundlePython() string {
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	return bundlePythonFrom(exe)
}

func pythonCandidates() []string {
	c := []string{}
	if p := envWithFallback("NEMBU_PYTHON", "MOSAIC_PYTHON"); p != "" {
		c = append(c, p)
	}
	if p := bundlePython(); p != "" {
		c = append(c, p)
	}
	return append(c, "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "python3")
}

var cachedPython = ""

func findPython() string {
	if cachedPython != "" {
		return cachedPython
	}
	for _, cand := range pythonCandidates() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		cmd := exec.CommandContext(ctx, cand, "-c", "import sqlite3, zoneinfo, json")
		err := cmd.Run()
		cancel()
		if err == nil {
			cachedPython = cand
			return cand
		}
	}
	return "python3"
}

func pythonCmd(name string, args ...string) (*exec.Cmd, error) {
	script := findScript(name)
	abs, err := filepath.Abs(script)
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(findPython(), append([]string{abs}, args...)...)
	cmd.Env = pythonEnv()
	return cmd, nil
}

func runPython(name string, args ...string) {
	cmd, err := pythonCmd(name, args...)
	if err != nil {
		fmt.Printf("[python] %s path error: %v\n", name, err)
		return
	}
	output, err := cmd.CombinedOutput()
	if err != nil {
		fmt.Printf("[python] %s failed: %v — %s\n", name, err, strings.TrimSpace(string(output)))
		return
	}
	if len(output) > 0 {
		fmt.Printf("[python] %s: %s\n", name, strings.TrimSpace(string(output)))
	}
}

// ── engine worker ────────────────────────────────────────────────────────────
// One long-running Python process owns the alpha loop. It talks to this bridge
// only through the token-gated loopback API. Supervised here so the app is the
// single thing the user launches.

var (
	workerMu      sync.Mutex
	workerRunning bool
)

func startEngineWorker(logger waLog.Logger) {
	workerMu.Lock()
	defer workerMu.Unlock()
	if workerRunning {
		return
	}
	workerRunning = true
	go func() {
		waitForKey()
		waitForOnboarding()
		restarts := 0
		for {
			cmd, err := pythonCmd("worker.py", "serve")
			if err == nil {
				cmd.Stdout = os.Stdout
				cmd.Stderr = os.Stderr
				if err = cmd.Start(); err == nil {
					writePID("worker.pid", cmd.Process.Pid)
					logger.Infof("engine worker started (pid %d)", cmd.Process.Pid)
					err = cmd.Wait()
					removePID("worker.pid")
					logger.Warnf("engine worker exited: %v", err)
				} else {
					logger.Errorf("engine worker start failed: %v", err)
				}
			} else {
				logger.Errorf("engine worker path error: %v", err)
			}
			restarts++
			delay := time.Duration(restarts) * 5 * time.Second
			if delay > time.Minute {
				delay = time.Minute
			}
			time.Sleep(delay)
		}
	}()
}

// resolveNow re-verifies the Me chat unconditionally (post-pairing, post-history).
func resolveNow(logger waLog.Logger) {
	resolveMu.Lock()
	lastResolveAt = time.Now()
	resolveMu.Unlock()
	go func() {
		logger.Infof("Resolving Me chat (mechat.py --resolve)")
		runPython("mechat.py", "--resolve")
	}()
}

// scheduleResolve re-verifies the Me chat (reconnects, boot) — debounced.
func scheduleResolve(logger waLog.Logger) {
	resolveMu.Lock()
	if time.Since(lastResolveAt) < 10*time.Second {
		resolveMu.Unlock()
		return
	}
	resolveMu.Unlock()
	resolveNow(logger)
}

// ── pairing ──────────────────────────────────────────────────────────────────

// startPairing keeps a fresh QR on screen: when a code expires or the channel
// closes without a scan, the stale code is cleared and a new one is requested.
func startPairing(client *whatsmeow.Client, logger waLog.Logger) {
	for {
		if client.Store.ID != nil {
			return
		}
		paired, err := pairOnce(client, logger)
		qrMu.Lock()
		currentQRCode = ""
		qrMu.Unlock()
		// A scan can land while the channel is closing; never re-pair then.
		if paired || client.Store.ID != nil {
			return
		}
		if err != nil {
			logger.Errorf("Failed to start pairing: %v", err)
		} else {
			logger.Warnf("QR expired or channel closed without pairing; requesting a new code")
		}
		if client.IsConnected() {
			// A closed QR channel can leave an unpaired socket open; drop it so
			// the next GetQRChannel starts from a clean state.
			client.Disconnect()
		}
		time.Sleep(3 * time.Second)
	}
}

func pairOnce(client *whatsmeow.Client, logger waLog.Logger) (bool, error) {
	qrChan, err := client.GetQRChannel(context.Background())
	if err != nil {
		return false, fmt.Errorf("get QR channel: %w", err)
	}
	if err := client.Connect(); err != nil {
		return false, fmt.Errorf("connect for pairing: %w", err)
	}
	for evt := range qrChan {
		switch evt.Event {
		case "code":
			qrMu.Lock()
			currentQRCode = evt.Code
			qrMu.Unlock()
			if envFlag("NEMBU_QR_TERMINAL", "MOSAIC_QR_TERMINAL") {
				fmt.Println("\nScan this QR code with your WhatsApp app:")
				qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
			}
		case "success":
			qrMu.Lock()
			currentQRCode = ""
			qrMu.Unlock()
			logger.Infof("Paired successfully")
			time.Sleep(2 * time.Second)
			resolveNow(logger)
			return true, nil
		case "timeout":
			logger.Warnf("QR code timed out")
			return false, nil
		}
	}
	return false, nil
}

// noteStoreWrite records history-sync progress for the loader.
func noteStoreWrite() {
	now := time.Now().Unix()
	syncLastWrite.Store(now)
	syncPairedAt.CompareAndSwap(0, now)
}

// chatSyncing reports whether initial history sync is still running.
func chatSyncing(paired bool, pairedAtUnix, lastWriteUnix int64, now time.Time) bool {
	if !paired {
		return false
	}
	if pairedAtUnix == 0 {
		return true
	}
	if now.Sub(time.Unix(pairedAtUnix, 0)) > 30*time.Minute {
		return false
	}
	return now.Sub(time.Unix(lastWriteUnix, 0)) < 90*time.Second
}

// ── servers ──────────────────────────────────────────────────────────────────

func startServers(client *whatsmeow.Client, messageStore *MessageStore, logger waLog.Logger) {
	startInternalServer(client, messageStore, logger)
}
