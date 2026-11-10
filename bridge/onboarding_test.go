package main

// onboarding_test.go — first-run journey tests. No WhatsApp account, no live
// Gemini key: the network is a local httptest server and every path under
// MOSAIC_HOME/STORE_DIR resolves to a temp dir.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

func testHome(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("NEMBU_HOME", home)
	t.Setenv("NEMBU_CONFIG_DIR", filepath.Join(home, "config"))
	t.Setenv("MOSAIC_HOME", home)
	t.Setenv("MOSAIC_CONFIG_DIR", filepath.Join(home, "config"))
	t.Setenv("STORE_DIR", filepath.Join(home, "store"))
	t.Setenv("GEMINI_API_KEY", "")
	t.Setenv("GEMINI_MODEL", "")
	if err := os.MkdirAll(os.Getenv("STORE_DIR"), 0o755); err != nil {
		t.Fatal(err)
	}
	return home
}

func validKey() string { return "AIza" + strings.Repeat("A", 30) }

// ── key screen ───────────────────────────────────────────────────────────────

func TestExtractKey(t *testing.T) {
	cases := map[string]string{
		`AIzaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA`:                     "AIzaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
		`GEMINI_API_KEY="AQ.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"`:        "AQ.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
		"  my key is AIzaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA  thanks": "AIzaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
		"  \"plain-value\"  ":                                         "plain-value",
	}
	for in, want := range cases {
		if got := extractKey(in); got != want {
			t.Errorf("extractKey(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestLooksLikeGeminiKey(t *testing.T) {
	if looksLikeGeminiKey("short") {
		t.Fatal("short string accepted")
	}
	if looksLikeGeminiKey("has a space " + strings.Repeat("x", 30)) {
		t.Fatal("whitespace accepted")
	}
	if !looksLikeGeminiKey(validKey()) {
		t.Fatal("valid key rejected")
	}
}

func TestInitialModelAsksForKeyOnFreshInstall(t *testing.T) {
	testHome(t)
	m := initialModel()
	if m.screen != screenKey {
		t.Fatalf("fresh install screen = %v, want screenKey", m.screen)
	}
}

func TestInitialModelUpgradeMarksScopeWithoutInterrupting(t *testing.T) {
	testHome(t)
	t.Setenv("GEMINI_API_KEY", validKey())
	m := initialModel()
	if m.screen != screenConnect {
		t.Fatalf("upgrade screen = %v, want screenConnect", m.screen)
	}
	if !scopeOnboarded() {
		t.Fatal("upgrade did not mark onboarding complete")
	}
	s := readScope()
	if s.IncludeArchived {
		t.Fatal("upgrade default must be active chats only")
	}
}

// ── scope screen ─────────────────────────────────────────────────────────────

func TestKeyValidationLeadsToScopeThenPairing(t *testing.T) {
	testHome(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("x-goog-api-key"); got != validKey() {
			t.Errorf("key header = %q", got)
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()
	t.Setenv("NEMBU_GEMINI_BASE", srv.URL+"/v1beta/models")
	t.Setenv("GEMINI_MODEL", "alpha-test")

	m := initialModel()
	if m.screen != screenKey {
		t.Fatalf("screen = %v", m.screen)
	}

	// Paste an env line, not just the bare key.
	paste := tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(`GEMINI_API_KEY="` + validKey() + `"`)}
	mi, _ := m.Update(paste)
	m = mi.(model)
	if m.keyInput != validKey() {
		t.Fatalf("paste kept %q", m.keyInput)
	}

	mi, cmd := m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = mi.(model)
	if cmd == nil {
		t.Fatal("enter did not start key validation")
	}
	mi, _ = m.Update(cmd())
	m = mi.(model)
	if m.screen != screenScope {
		t.Fatalf("after valid key screen = %v, want screenScope", m.screen)
	}

	// Choose "active and archived" with the keyboard, then confirm.
	mi, _ = m.Update(tea.KeyMsg{Type: tea.KeyDown})
	m = mi.(model)
	if m.scopeChoice != 1 {
		t.Fatalf("down did not select archived: %d", m.scopeChoice)
	}
	mi, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = mi.(model)
	if m.screen != screenConnect {
		t.Fatalf("after scope screen = %v, want screenConnect", m.screen)
	}
	if s := readScope(); !s.IncludeArchived {
		t.Fatal("archived choice was not persisted")
	}
	if !scopeOnboarded() {
		t.Fatal("marker missing")
	}
}

func TestScopeEscapeChoosesDefault(t *testing.T) {
	testHome(t)
	t.Setenv("GEMINI_API_KEY", validKey())
	m := initialModel()
	m.screen = screenScope
	m.scopeChoice = 1
	mi, _ := m.Update(tea.KeyMsg{Type: tea.KeyEsc})
	m = mi.(model)
	if m.screen != screenConnect {
		t.Fatalf("esc screen = %v", m.screen)
	}
	if readScope().IncludeArchived {
		t.Fatal("esc must persist the active-only default")
	}
}

func TestScopeViewShowsBothChoices(t *testing.T) {
	testHome(t)
	m := initialModel()
	m.screen = screenScope
	out := m.View()
	for _, want := range []string{"Which chats should Nembu watch? 🍋", "Active chats only", "Active and archived chats"} {
		if !strings.Contains(out, want) {
			t.Errorf("scope view missing %q", want)
		}
	}
}

// ── key validation over a fake HTTP server ───────────────────────────────────

func TestValidateKeyStatuses(t *testing.T) {
	cases := []struct {
		name    string
		status  int
		wantOK  bool
		wantErr string
	}{
		{"ok", 200, true, ""},
		{"bad request", 400, false, "isn't valid"},
		{"unauthorized", 401, false, "isn't valid"},
		{"forbidden", 403, false, "isn't valid"},
		{"quota", 429, false, "out of quota"},
		{"server", 500, false, "HTTP 500"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			testHome(t)
			var gotPath string
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				gotPath = r.URL.Path
				w.WriteHeader(tc.status)
			}))
			defer srv.Close()
			t.Setenv("NEMBU_GEMINI_BASE", srv.URL+"/v1beta/models")
			t.Setenv("GEMINI_MODEL", "alpha-test")

			msg := validateKey(validKey())().(keyCheckMsg)
			if msg.ok != tc.wantOK {
				t.Fatalf("ok = %v, want %v (err %q)", msg.ok, tc.wantOK, msg.err)
			}
			if tc.wantErr != "" && !strings.Contains(msg.err, tc.wantErr) {
				t.Fatalf("err = %q, want substring %q", msg.err, tc.wantErr)
			}
			if want := "/v1beta/models/alpha-test:generateContent"; gotPath != want {
				t.Fatalf("path = %q, want %q", gotPath, want)
			}
		})
	}
}

func TestValidateKeyNetworkError(t *testing.T) {
	testHome(t)
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	srv.Close()
	t.Setenv("NEMBU_GEMINI_BASE", url+"/v1beta/models")
	msg := validateKey(validKey())().(keyCheckMsg)
	if msg.ok || !strings.Contains(msg.err, "internet") {
		t.Fatalf("network error msg = %+v", msg)
	}
}

func TestBackspaceEditsKey(t *testing.T) {
	testHome(t)
	m := initialModel()
	m.keyInput = "abc"
	mi, _ := m.Update(tea.KeyMsg{Type: tea.KeyBackspace})
	m = mi.(model)
	if m.keyInput != "ab" {
		t.Fatalf("backspace left %q", m.keyInput)
	}
}

func TestTypingLettersOnKeyScreen(t *testing.T) {
	testHome(t)
	m := initialModel()
	if m.screen != screenKey {
		t.Fatalf("screen = %v", m.screen)
	}
	for _, r := range []string{"d", "q", "l"} {
		mi, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(r)})
		m = mi.(model)
	}
	if m.keyInput != "dql" {
		t.Fatalf("key input = %q, want dql", m.keyInput)
	}
}

func TestSaveKeyTightensExistingEnvMode(t *testing.T) {
	home := testHome(t)
	path := filepath.Join(home, ".env")
	if err := os.WriteFile(path, []byte(`OTHER="keep"`+"\n"+`GEMINI_API_KEY="old"`+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := saveKeyToEnv(validKey()); err != nil {
		t.Fatal(err)
	}
	st, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := st.Mode().Perm(); perm != 0o600 {
		t.Fatalf(".env mode = %o, want 600", perm)
	}
	data, _ := os.ReadFile(path)
	if !strings.Contains(string(data), `OTHER="keep"`) {
		t.Fatal("unrelated .env line was dropped")
	}
	if !strings.Contains(string(data), validKey()) {
		t.Fatal("new key not written")
	}
}

// ── QR / sync / ready state machine ──────────────────────────────────────────

func TestJourneyStateMachine(t *testing.T) {
	testHome(t)
	m := initialModel()

	// Key screen never auto-advances, even with bridge status.
	m.screen = screenKey
	m.advance()
	if m.screen != screenKey {
		t.Fatalf("key screen advanced to %v", m.screen)
	}

	// Unpaired with a QR.
	m.screen = screenConnect
	m.status = bridgeStatus{QR: "qr-data"}
	m.advance()
	if m.screen != screenQR {
		t.Fatalf("unpaired+QR screen = %v, want screenQR", m.screen)
	}

	// Unpaired with no QR falls back to connecting (expired QR).
	m.status.QR = ""
	m.advance()
	if m.screen != screenConnect {
		t.Fatalf("expired QR screen = %v, want screenConnect", m.screen)
	}

	// Paired, not ready → sync progress.
	m.status = bridgeStatus{Paired: true, Sync: syncStatusWire{Progress: 42, Phase: "RECENT"}}
	m.advance()
	if m.screen != screenSync {
		t.Fatalf("paired screen = %v, want screenSync", m.screen)
	}
	if m.syncShownAt.IsZero() {
		t.Fatal("syncShownAt not set")
	}

	// Ready shows the summary for 3 s, then the dashboard.
	m.status.Sync.Ready = true
	m.advance()
	if m.screen != screenReady {
		t.Fatalf("ready screen = %v, want screenReady", m.screen)
	}
	m.readyShownAt = time.Now().Add(-4 * time.Second)
	m.advance()
	if m.screen != screenDash {
		t.Fatalf("post-ready screen = %v, want screenDash", m.screen)
	}
}

func TestEscHidesSyncAndOpensDashboard(t *testing.T) {
	testHome(t)
	m := initialModel()
	m.screen = screenSync
	m.status = bridgeStatus{Paired: true}
	mi, _ := m.Update(tea.KeyMsg{Type: tea.KeyEsc})
	m = mi.(model)
	if m.screen != screenDash {
		t.Fatalf("esc screen = %v", m.screen)
	}
	m.advance()
	if m.screen != screenDash {
		t.Fatalf("hidden sync re-opened %v", m.screen)
	}
}

func TestClampProgress(t *testing.T) {
	for in, want := range map[int]int{-10: 0, 0: 0, 42: 42, 100: 100, 140: 100} {
		if got := clampProgress(in); got != want {
			t.Errorf("clampProgress(%d) = %d, want %d", in, got, want)
		}
	}
}

func TestSyncViewShowsClampedProgress(t *testing.T) {
	testHome(t)
	m := initialModel()
	m.screen = screenSync
	m.width = 96
	m.status.Sync.Progress = 140
	if out := m.View(); !strings.Contains(out, "100%") {
		t.Fatal("sync view did not clamp 140% to 100%")
	}
}

func TestMechatHelpAppearsAfterGrace(t *testing.T) {
	now := time.Now()
	if mechatHelpVisible(time.Time{}, false, now) {
		t.Fatal("help shown without a sync start time")
	}
	if mechatHelpVisible(now.Add(-2*time.Minute), true, now) {
		t.Fatal("help shown when resolved")
	}
	if !mechatHelpVisible(now.Add(-2*time.Minute), false, now) {
		t.Fatal("help not shown after 90s unresolved")
	}
}

func TestSyncViewShowsMechatHelp(t *testing.T) {
	testHome(t)
	m := initialModel()
	m.screen = screenSync
	m.width = 96
	m.syncShownAt = time.Now().Add(-2 * time.Minute)
	if out := m.View(); !strings.Contains(out, "Message yourself") {
		t.Fatal("no actionable Me-chat guidance after timeout")
	}
}

func TestSyncReadyDecision(t *testing.T) {
	now := time.Now()
	boot := now.Add(-5 * time.Minute)
	cases := []struct {
		name       string
		me         bool
		sawInitial bool
		progress   int64
		phase      string
		lastEvent  int64
		progressAt int64
		want       bool
	}{
		{"recent done and idle", true, true, 100, "RECENT", now.Add(-25 * time.Second).Unix(), now.Add(-25 * time.Second).Unix(), true},
		{"recent done but active", true, true, 100, "RECENT", now.Add(-5 * time.Second).Unix(), now.Add(-5 * time.Second).Unix(), false},
		{"partial history idle", true, true, 40, "RECENT", now.Add(-95 * time.Second).Unix(), now.Add(-95 * time.Second).Unix(), true},
		{"no initial event", true, false, 0, "", now.Add(-95 * time.Second).Unix(), 0, false},
		{"full done and idle", true, true, 100, "FULL", now.Add(-21 * time.Second).Unix(), now.Add(-21 * time.Second).Unix(), true},
		{"fresh account", true, false, 0, "", 0, 0, true},
		{"no me chat", false, true, 100, "RECENT", now.Add(-10 * time.Minute).Unix(), now.Add(-10 * time.Minute).Unix(), false},
		{"stalled at 84 but events trickle", true, true, 84, "RECENT", now.Add(-5 * time.Second).Unix(), now.Add(-61 * time.Second).Unix(), true},
		{"84 changed just now", true, true, 84, "RECENT", now.Add(-5 * time.Second).Unix(), now.Add(-10 * time.Second).Unix(), false},
		{"79 flat does not finish", true, true, 79, "RECENT", now.Add(-5 * time.Second).Unix(), now.Add(-10 * time.Minute).Unix(), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := syncReadyDecision(tc.me, tc.sawInitial, tc.progress, tc.phase,
				tc.lastEvent, tc.progressAt, boot, now)
			if got != tc.want {
				t.Fatalf("ready = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestSyncStalled(t *testing.T) {
	now := time.Now()
	if syncStalled(84, now.Add(-61*time.Second).Unix(), now) != true {
		t.Fatal("84% flat for 61s should be finishing")
	}
	if syncStalled(84, now.Add(-10*time.Second).Unix(), now) {
		t.Fatal("84% changed 10s ago is not stalled")
	}
	if syncStalled(79, now.Add(-2*time.Minute).Unix(), now) {
		t.Fatal("79% must not use the finishing shortcut")
	}
	if syncStalled(84, 0, now) {
		t.Fatal("no progress timestamp must not claim stalled")
	}
}

func TestChatSyncing(t *testing.T) {
	now := time.Now()
	if chatSyncing(false, now.Unix(), now.Unix(), now) {
		t.Fatal("unpaired reported syncing")
	}
	if !chatSyncing(true, now.Unix(), now.Unix(), now) {
		t.Fatal("recent write should be syncing")
	}
	if chatSyncing(true, now.Add(-2*time.Hour).Unix(), now.Add(-2*time.Minute).Unix(), now) {
		t.Fatal("stale sync should be finished")
	}
	if !chatSyncing(true, 0, 0, now) {
		t.Fatal("paired with no timestamp should be syncing")
	}
}

func TestEarlyWelcomeDue(t *testing.T) {
	now := time.Now()
	if !earlyWelcomeDue(true, false, 0, time.Time{}, now) {
		t.Fatal("early welcome should fire as soon as Me chat resolves")
	}
	if earlyWelcomeDue(false, false, 0, time.Time{}, now) {
		t.Fatal("early welcome without Me chat")
	}
	if earlyWelcomeDue(true, true, 0, time.Time{}, now) {
		t.Fatal("early welcome after it was sent")
	}
	if earlyWelcomeDue(true, false, 1, now.Add(-5*time.Second), now) {
		t.Fatal("early retry too soon")
	}
	if !earlyWelcomeDue(true, false, 1, now.Add(-time.Minute), now) {
		t.Fatal("early retry not due after a minute")
	}
	if earlyWelcomeDue(true, false, earlyWelcomeMaxAttempts, now.Add(-time.Hour), now) {
		t.Fatal("early retried past the cap")
	}
}

func TestHowtoDue(t *testing.T) {
	now := time.Now()
	if !howtoDue(true, false, 0, time.Time{}, now) {
		t.Fatal("how-to should fire once")
	}
	if howtoDue(false, false, 0, time.Time{}, now) {
		t.Fatal("how-to without Me chat")
	}
	if howtoDue(true, true, 0, time.Time{}, now) {
		t.Fatal("how-to after it was sent")
	}
	if howtoDue(true, false, 1, now.Add(-5*time.Second), now) {
		t.Fatal("how-to retry too soon")
	}
	if !howtoDue(true, false, 1, now.Add(-time.Minute), now) {
		t.Fatal("how-to retry not due after a minute")
	}
	if howtoDue(true, false, howtoMaxAttempts, now.Add(-time.Hour), now) {
		t.Fatal("how-to retried past the cap")
	}
}

// ── status wire / sync progress parsing ──────────────────────────────────────

func TestFetchStatusParsesSyncProgress(t *testing.T) {
	home := testHome(t)
	if err := os.WriteFile(filepath.Join(home, "store", "bridge_token"), []byte("tok"), 0o600); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Bridge-Token") != "tok" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"connected": true, "paired": true, "qr": "",
			"mechat_jid": "me@s.whatsapp.net",
			"sync": map[string]any{
				"phase": "RECENT", "progress": 63, "chats": 12,
				"messages": 345, "oldest": "2026-10-10", "ready": false,
				"mechat_resolved": true,
			},
		})
	}))
	defer srv.Close()
	t.Setenv("NEMBU_API_BASE", srv.URL)

	got := fetchStatus()().(statusMsg)
	st := bridgeStatus(got)
	if st.Err != "" {
		t.Fatalf("status error: %s", st.Err)
	}
	if !st.Paired || st.Sync.Progress != 63 || st.Sync.Chats != 12 || st.Sync.Messages != 345 {
		t.Fatalf("parsed status = %+v", st.Sync)
	}
	if st.Sync.Ready {
		t.Fatal("ready parsed true")
	}
}

func TestFetchStatusWithoutTokenIsStarting(t *testing.T) {
	testHome(t)
	got := fetchStatus()().(statusMsg)
	if got.Err == "" {
		t.Fatal("missing token should report starting bridge")
	}
}

func TestStatusMsgDrivesQRAndSyncScreens(t *testing.T) {
	testHome(t)
	m := initialModel()
	m.screen = screenConnect
	mi, _ := m.Update(statusMsg(bridgeStatus{QR: "abc"}))
	m = mi.(model)
	if m.screen != screenQR {
		t.Fatalf("status qr screen = %v", m.screen)
	}
	mi, _ = m.Update(statusMsg(bridgeStatus{Paired: true, Sync: syncStatusWire{Phase: "RECENT", Progress: 10}}))
	m = mi.(model)
	if m.screen != screenSync {
		t.Fatalf("status sync screen = %v", m.screen)
	}
}

// ── display helpers used by the journey ──────────────────────────────────────

func TestHumanCountAndHistoryDepth(t *testing.T) {
	if got := humanCount(1234567); got != "1,234,567" {
		t.Fatalf("humanCount = %q", got)
	}
	if got := historyDepth("2026-10-11", "2026-11-09"); got != "4 weeks" {
		t.Fatalf("historyDepth = %q", got)
	}
	if got := historyDepth("", "2026-11-09"); got != "—" {
		t.Fatalf("empty historyDepth = %q", got)
	}
}

func TestNewestLag(t *testing.T) {
	raw := time.Now().Add(-10 * time.Minute).Format("2006-01-02 15:04:05")
	if lag := newestLag(raw); lag < 9 || lag > 11 {
		t.Fatalf("newestLag = %v", lag)
	}
	if lag := newestLag("not-a-date"); lag != -1 {
		t.Fatalf("bad newestLag = %v", lag)
	}
}

func TestShortDate(t *testing.T) {
	if got := shortDate("2026-11-09 10:00:00"); got != "09 Nov" {
		t.Fatalf("shortDate = %q", got)
	}
	if got := shortDate("bogus"); got != "—" {
		t.Fatalf("bad shortDate = %q", got)
	}
}

// ── store counts after a partial sync ────────────────────────────────────────

func TestStoreSyncCounts(t *testing.T) {
	testHome(t)
	store, err := NewMessageStore()
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	ts := time.Now().Add(-24 * time.Hour)
	if err := store.StoreChat("123@g.us", "Team", ts); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage("m1", "123@g.us", "me", "hello", ts, true, "", "", "", nil, nil, nil, 0, "", ""); err != nil {
		t.Fatal(err)
	}
	chats, messages, oldest := store.syncCounts()
	if chats != 1 || messages != 1 {
		t.Fatalf("syncCounts = (%d,%d)", chats, messages)
	}
	if oldest != ts.Format("2006-01-02") {
		t.Fatalf("oldest = %q", oldest)
	}
}

// ── launcher/installer-facing env helpers ────────────────────────────────────

func TestGeminiBaseURLDefaultAndOverride(t *testing.T) {
	t.Setenv("NEMBU_GEMINI_BASE", "")
	if got := geminiBaseURL(); got != "https://generativelanguage.googleapis.com/v1beta/models" {
		t.Fatalf("default base = %q", got)
	}
	t.Setenv("NEMBU_GEMINI_BASE", "http://127.0.0.1:9999/v1beta/models/")
	if got := geminiBaseURL(); got != "http://127.0.0.1:9999/v1beta/models" {
		t.Fatalf("override base = %q", got)
	}
}

// ── ONB-005..008: notifications, ETA, stall and dashboard escape ─────────────

func TestSyncETA(t *testing.T) {
	now := time.Now()
	cases := []struct {
		name     string
		samples  []progressSample
		progress int
		want     string
	}{
		{"zero", nil, 0, "still working"},
		{"too soon to know", []progressSample{{now.Add(-10 * time.Second), 12}}, 12, "still working"},
		{"moving", []progressSample{{now.Add(-60 * time.Second), 0}, {now.Add(-10 * time.Second), 12}}, 12, "~7 min left"},
		{"stalled tail", []progressSample{{now.Add(-2 * time.Minute), 84}}, 84, "finishing up"},
		{"almost there", []progressSample{{now.Add(-60 * time.Second), 90}, {now.Add(-10 * time.Second), 98}}, 98, "less than a minute left"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := syncETA(tc.samples, tc.progress, now); !strings.Contains(got, tc.want) {
				t.Fatalf("ETA = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestSyncViewShowsETAAndEscape(t *testing.T) {
	testHome(t)
	m := initialModel()
	m.width = 100
	m.screen = screenSync
	m.status = bridgeStatus{Paired: true, Sync: syncStatusWire{Phase: "RECENT", Progress: 12}}
	m.syncShownAt = time.Now()
	m.progressSamples = []progressSample{{time.Now().Add(-60 * time.Second), 0}, {time.Now().Add(-10 * time.Second), 12}}
	out := m.View()
	for _, want := range []string{"min left", "esc — open dashboard now", "sync continues in the background"} {
		if !strings.Contains(out, want) {
			t.Errorf("sync view missing %q", want)
		}
	}
}

func TestSyncViewStallText(t *testing.T) {
	testHome(t)
	m := initialModel()
	m.width = 100
	m.screen = screenSync
	m.status = bridgeStatus{Paired: true, Sync: syncStatusWire{Phase: "RECENT", Progress: 84}}
	m.syncShownAt = time.Now()
	m.progressSamples = []progressSample{{time.Now().Add(-2 * time.Minute), 84}}
	out := m.View()
	for _, want := range []string{"still syncing 84% — esc — dashboard", "finishing up…"} {
		if !strings.Contains(out, want) {
			t.Errorf("stalled sync view missing %q", want)
		}
	}
}

func TestEscAt12And84OpensDashboard(t *testing.T) {
	for _, progress := range []int{12, 84} {
		t.Run(fmt.Sprintf("%d%%", progress), func(t *testing.T) {
			testHome(t)
			m := initialModel()
			m.screen = screenSync
			m.status = bridgeStatus{Paired: true, Sync: syncStatusWire{Phase: "RECENT", Progress: progress}}
			mi, _ := m.Update(tea.KeyMsg{Type: tea.KeyEsc})
			m = mi.(model)
			if m.screen != screenDash {
				t.Fatalf("esc at %d%% screen = %v", progress, m.screen)
			}
			if !m.hideSync {
				t.Fatal("hideSync not set")
			}
			// A later status poll must not drag the user back to sync.
			mi, _ = m.Update(statusMsg(bridgeStatus{Paired: true, Sync: syncStatusWire{Phase: "RECENT", Progress: progress + 1}}))
			m = mi.(model)
			if m.screen != screenDash {
				t.Fatalf("background sync re-opened %v", m.screen)
			}
		})
	}
}

func TestDAndEnterOpenDashboard(t *testing.T) {
	for _, key := range []tea.KeyMsg{{Type: tea.KeyRunes, Runes: []rune("d")}, {Type: tea.KeyEnter}} {
		testHome(t)
		m := initialModel()
		m.screen = screenSync
		m.status = bridgeStatus{Paired: true, Sync: syncStatusWire{Progress: 12}}
		mi, _ := m.Update(key)
		m = mi.(model)
		if m.screen != screenDash {
			t.Fatalf("%q did not open dashboard: %v", key.String(), m.screen)
		}
	}
	// d toggles back to sync details while not ready.
	testHome(t)
	m := initialModel()
	m.screen = screenDash
	m.status = bridgeStatus{Paired: true, Sync: syncStatusWire{Progress: 12}}
	mi, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("d")})
	m = mi.(model)
	if m.screen != screenSync {
		t.Fatalf("d on dashboard = %v, want sync", m.screen)
	}
}

func TestDashboardShowsBackgroundSync(t *testing.T) {
	testHome(t)
	m := initialModel()
	m.width = 100
	m.screen = screenDash
	m.status = bridgeStatus{Paired: true, Sync: syncStatusWire{Progress: 84}}
	out := m.View()
	for _, want := range []string{"sync continues in the background", "d — sync details"} {
		if !strings.Contains(out, want) {
			t.Errorf("dashboard missing %q", want)
		}
	}
}

func TestRecentEventsLoadingRotates(t *testing.T) {
	testHome(t)
	m := initialModel()
	if out := m.recentEventsBody(60, 5); !strings.Contains(out, "Live pipeline is loading…") ||
		!strings.Contains(out, "what did I miss today?") {
		t.Fatalf("loading view = %q", out)
	}
	m.frame = 3
	if out := m.recentEventsBody(60, 5); !strings.Contains(out, "what's pending with Nisha?") {
		t.Fatalf("second question missing: %q", out)
	}
	m.frame = 15 // 15/3 = 5, cycles back to the first question
	if out := m.recentEventsBody(60, 5); !strings.Contains(out, "what did I miss today?") {
		t.Fatalf("question did not cycle: %q", out)
	}
}

func TestDashboardOpenedNotifiesOnce(t *testing.T) {
	home := testHome(t)
	if err := os.WriteFile(filepath.Join(home, "store", "bridge_token"), []byte("tok"), 0o600); err != nil {
		t.Fatal(err)
	}
	hit := make(chan struct{}, 2)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Bridge-Token") != "tok" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		hit <- struct{}{}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	t.Setenv("NEMBU_API_BASE", srv.URL)

	m := initialModel()
	m.screen = screenDash
	cmd := m.markDashboardOpened()
	if cmd == nil {
		t.Fatal("first dashboard open did not notify")
	}
	cmd()
	select {
	case <-hit:
	case <-time.After(time.Second):
		t.Fatal("bridge never saw the dashboard-open call")
	}
	if m.markDashboardOpened() != nil {
		t.Fatal("dashboard-open notification sent twice")
	}
}

func TestMechatFlagsRead(t *testing.T) {
	home := testHome(t)
	if err := os.WriteFile(filepath.Join(home, "store", "mechat.json"),
		[]byte(`{"jid":"me@s.whatsapp.net","welcomed_early":true,"howto_sent":true}`), 0o600); err != nil {
		t.Fatal(err)
	}
	st := readMechatState()
	if st.JID == "" || !st.WelcomedEarly || !st.HowtoSent {
		t.Fatalf("state = %+v", st)
	}
}
