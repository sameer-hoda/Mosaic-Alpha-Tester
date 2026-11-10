package main

// tui.go — the only interface. First run: paste key -> QR pairs in-terminal ->
// live dashboard. Later runs: straight to the dashboard.
//
// It talks to the bridge over the token-gated loopback API (status/send) and
// reads engine.db for the alpha's own numbers. No web page is involved.

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal"
)

// ── palette ──────────────────────────────────────────────────────────────────
var (
	cInk    = lipgloss.Color("252")
	cDim    = lipgloss.Color("243")
	cFaint  = lipgloss.Color("240")
	cAccent = lipgloss.Color("111")
	cMint   = lipgloss.Color("114")
	cGold   = lipgloss.Color("179")
	cRose   = lipgloss.Color("174")
	cBlue   = lipgloss.Color("110")

	stBrand  = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("213"))
	stSubtle = lipgloss.NewStyle().Foreground(cDim)
	stFaint  = lipgloss.NewStyle().Foreground(cFaint)
	stInk    = lipgloss.NewStyle().Foreground(cInk)
	stGood   = lipgloss.NewStyle().Foreground(cMint)
	stWarn   = lipgloss.NewStyle().Foreground(cGold)
	stBad    = lipgloss.NewStyle().Foreground(cRose)
	stAccent = lipgloss.NewStyle().Foreground(cAccent)
	stBlue   = lipgloss.NewStyle().Foreground(cBlue)

	stPanel = lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(lipgloss.Color("238")).
		Padding(0, 1)

	stHeader = lipgloss.NewStyle().
			Border(lipgloss.Border{Bottom: "─"}, false, false, true, false).
			BorderForeground(lipgloss.Color("237")).
			PaddingBottom(1)

	stKey = lipgloss.NewStyle().
		Background(lipgloss.Color("236")).
		Foreground(lipgloss.Color("252")).
		Padding(0, 1)
)

var geminiKeyRe = regexp.MustCompile(`(?:AIza[0-9A-Za-z_\-]{20,}|AQ\.[0-9A-Za-z_\-\.]{20,})`)

// extractKey pulls the actual key out of whatever the clipboard held:
// env lines, quotes, surrounding prose, multiple lines.
func extractKey(raw string) string {
	if m := geminiKeyRe.FindString(raw); m != "" {
		return m
	}
	return strings.Trim(raw, " \t\r\n\"'")
}

// clipboardRead grabs the system clipboard; tests override it.
var clipboardRead = func() string {
	if out, err := exec.Command("pbpaste").Output(); err == nil {
		return strings.TrimSpace(string(out))
	}
	if out, err := exec.Command("xclip", "-selection", "clipboard", "-o").Output(); err == nil {
		return strings.TrimSpace(string(out))
	}
	return ""
}

// ── status wire ──────────────────────────────────────────────────────────────
type syncStatusWire struct {
	Phase          string `json:"phase"`
	Progress       int    `json:"progress"`
	Chats          int64  `json:"chats"`
	Messages       int64  `json:"messages"`
	Oldest         string `json:"oldest"`
	Ready          bool   `json:"ready"`
	MechatResolved bool   `json:"mechat_resolved"`
}

type bridgeStatus struct {
	Connected     bool           `json:"connected"`
	Paired        bool           `json:"paired"`
	QR            string         `json:"qr"`
	Mechat        string         `json:"mechat_jid"`
	Worker        bool           `json:"worker"`
	Syncing       bool           `json:"syncing"`
	Chats         int64          `json:"chats"`
	Messages      int64          `json:"messages"`
	Oldest        string         `json:"oldest"`
	Newest        string         `json:"newest"`
	SyncedSeconds int64          `json:"synced_seconds"`
	Sync          syncStatusWire `json:"sync"`
	Err           string         `json:"-"`
}

type statusMsg bridgeStatus
type keyCheckMsg struct {
	key string
	ok  bool
	err string
}
type tickMsg time.Time

func readToken() string {
	b, err := os.ReadFile(storePath("bridge_token"))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func apiBase() string {
	if base := envWithFallback("NEMBU_API_BASE", "MOSAIC_API_BASE"); base != "" {
		return base
	}
	return "http://127.0.0.1:8090"
}

func notifyDashboardOpened() tea.Cmd {
	return func() tea.Msg {
		token := readToken()
		if token == "" {
			return nil
		}
		req, err := http.NewRequest("POST", apiBase()+"/api/dashboard-opened", nil)
		if err != nil {
			return nil
		}
		req.Header.Set("X-Bridge-Token", token)
		cl := &http.Client{Timeout: 2 * time.Second}
		if resp, err := cl.Do(req); err == nil {
			resp.Body.Close()
		}
		return nil
	}
}

// markDashboardOpened fires the one-shot how-to trigger the first time the
// dashboard is shown, including when the user escaped sync early.
func (m *model) markDashboardOpened() tea.Cmd {
	if m.screen != screenDash || m.dashNotified {
		return nil
	}
	m.dashNotified = true
	return notifyDashboardOpened()
}

func fetchStatus() tea.Cmd {
	return func() tea.Msg {
		token := readToken()
		if token == "" {
			return statusMsg{Err: "starting bridge…"}
		}
		req, _ := http.NewRequest("GET", apiBase()+"/api/status", nil)
		req.Header.Set("X-Bridge-Token", token)
		cl := &http.Client{Timeout: 2 * time.Second}
		resp, err := cl.Do(req)
		if err != nil {
			return statusMsg{Err: "starting bridge…"}
		}
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			return statusMsg{Err: "bridge not ready"}
		}
		var st bridgeStatus
		if err := json.NewDecoder(resp.Body).Decode(&st); err != nil {
			return statusMsg{Err: "bridge not ready"}
		}
		return statusMsg(st)
	}
}

// ── engine numbers ───────────────────────────────────────────────────────────
type engineNums struct {
	Watched, Excluded, PingsDelivered, PingsCreated int
	Queued, Retrying, FailedToday                   int
	Spend                                           float64
	Alerts                                          []alertRow
	Sends                                           []sendRow
	Messages                                        int
	TotalMessages                                   int64
	LastMessage                                     string
	LastRadar                                       string
	NextRadar                                       string
}

var istZone = time.FixedZone("IST", 5*60*60+30*60)

func radarSlotHours() []int {
	start, end, step := 8, 20, 2
	if v, err := strconv.Atoi(os.Getenv("RADAR_START_HOUR")); err == nil {
		start = v
	}
	if v, err := strconv.Atoi(os.Getenv("RADAR_END_HOUR")); err == nil {
		end = v
	}
	if v, err := strconv.Atoi(os.Getenv("RADAR_EVERY_HOURS")); err == nil && v > 0 {
		step = v
	}
	var slots []int
	for h := start; h <= end; h += step {
		slots = append(slots, h)
	}
	return slots
}

func nextRadarLabel(now time.Time) string {
	slots := radarSlotHours()
	if len(slots) == 0 {
		return "—"
	}
	for _, h := range slots {
		if h > now.Hour() {
			return fmt.Sprintf("%02d:00", h)
		}
	}
	return fmt.Sprintf("%02d:00", slots[0])
}

type alertRow struct{ Kind, State, Headline, At string }
type sendRow struct{ Kind, Status, Body string }

func readEngine() engineNums {
	var n engineNums
	dbPath := storePath("engine.db")
	if _, err := os.Stat(dbPath); err == nil {
		db, err := sql.Open("sqlite3", "file:"+dbPath+"?mode=ro&_busy_timeout=1500")
		if err == nil {
			defer db.Close()
			_ = db.QueryRow("SELECT COUNT(*) FROM watch_status WHERE state='watched'").Scan(&n.Watched)
			_ = db.QueryRow("SELECT COUNT(*) FROM watch_status WHERE state='excluded'").Scan(&n.Excluded)
			day := time.Now().Format("2006-01-02")
			_ = db.QueryRow("SELECT delivered, created FROM daily_pings WHERE day=?", day).
				Scan(&n.PingsDelivered, &n.PingsCreated)
			_ = db.QueryRow("SELECT usd FROM spend WHERE day=?", day).Scan(&n.Spend)
			_ = db.QueryRow("SELECT COUNT(*) FROM outbox WHERE status='queued'").Scan(&n.Queued)
			_ = db.QueryRow("SELECT COUNT(*) FROM outbox WHERE status='held_unknown'").Scan(&n.Retrying)
			_ = db.QueryRow("SELECT COUNT(*) FROM outbox WHERE status='failed' AND substr(created_at,1,10)=?", day).Scan(&n.FailedToday)
			n.NextRadar = nextRadarLabel(time.Now().In(istZone))
			var radarRaw string
			if db.QueryRow("SELECT value FROM watermarks WHERE job='radar' AND key='last'").Scan(&radarRaw) == nil {
				var d struct {
					At      string `json:"at"`
					Scanned int    `json:"scanned"`
					Alerts  int    `json:"alerts"`
				}
				if json.Unmarshal([]byte(radarRaw), &d) == nil && d.At != "" {
					at, _ := time.Parse(time.RFC3339, d.At)
					n.LastRadar = fmt.Sprintf("%s · %d chats · %d alerts",
						at.In(istZone).Format("15:04"), d.Scanned, d.Alerts)
				}
			}
			if n.LastRadar == "" {
				n.LastRadar = "—"
			}

			rows, err := db.Query("SELECT kind, state, headline, created_at FROM alerts ORDER BY id DESC LIMIT 5")
			if err == nil {
				for rows.Next() {
					var a alertRow
					if rows.Scan(&a.Kind, &a.State, &a.Headline, &a.At) == nil {
						n.Alerts = append(n.Alerts, a)
					}
				}
				rows.Close()
			}
			rows, err = db.Query("SELECT kind, status, body FROM outbox ORDER BY id DESC LIMIT 4")
			if err == nil {
				for rows.Next() {
					var s sendRow
					if rows.Scan(&s.Kind, &s.Status, &s.Body) == nil {
						n.Sends = append(n.Sends, s)
					}
				}
				rows.Close()
			}
		}
	}
	dbPath = storePath("messages.db")
	if _, err := os.Stat(dbPath); err == nil {
		db, err := sql.Open("sqlite3", "file:"+dbPath+"?mode=ro&_busy_timeout=1500")
		if err == nil {
			defer db.Close()
			_ = db.QueryRow("SELECT COUNT(*) FROM chats").Scan(&n.Messages)
			_ = db.QueryRow("SELECT COUNT(*) FROM messages").Scan(&n.TotalMessages)
			_ = db.QueryRow("SELECT COALESCE(MAX(timestamp),'') FROM messages").Scan(&n.LastMessage)
		}
	}
	return n
}

// ── model ────────────────────────────────────────────────────────────────────
type screen int

const (
	screenConnect screen = iota
	screenKey
	screenScope
	screenQR
	screenSync
	screenReady
	screenDash
)

type model struct {
	screen          screen
	width           int
	height          int
	status          bridgeStatus
	n               engineNums
	keyInput        string
	keyBusy         bool
	keyErr          string
	scopeChoice     int
	scopeErr        string
	notice          string
	frame           int
	syncDoneAt      time.Time
	syncShownAt     time.Time
	readyShownAt    time.Time
	waitSince       time.Time
	hideSync        bool
	lastCount       int64
	lastSample      time.Time
	rate            float64
	progressSamples []progressSample
	prevOldest      string
	deepenedAt      time.Time
	showFullLog     bool
	selTrace        int
	selStep         int
	expandStep      bool
	dashNotified    bool
}

func initialModel() model {
	m := model{screen: screenConnect, selTrace: 0, selStep: -1}
	if !envHasKey() {
		m.screen = screenKey
	} else if !scopeOnboarded() {
		// Returning tester upgrading to this build: keep today's behaviour
		// (active chats only), mark onboarded, and never interrupt a launch.
		_ = persistScope(false)
	}
	m.n = readEngine()
	return m
}

// ── scope choice (archived chats) ────────────────────────────────────────────

type scopeConfig struct {
	IncludeArchived bool `json:"include_archived"`
}

func nembuConfigDir() string {
	if d := envWithFallback("NEMBU_CONFIG_DIR", "MOSAIC_CONFIG_DIR"); d != "" {
		return d
	}
	if h := envWithFallback("NEMBU_HOME", "MOSAIC_HOME"); h != "" {
		return filepath.Join(h, "config")
	}
	// dev fallback: prefer .nembu-config, legacy .mosaic-config
	home := os.Getenv("HOME")
	if home != "" {
		nc := filepath.Join(home, ".nembu-config")
		mc := filepath.Join(home, ".mosaic-config")
		if _, err := os.Stat(nc); err == nil {
			return nc
		}
		if _, err := os.Stat(mc); err == nil {
			return mc
		}
	}
	return ".nembu-config"
}

// legacy alias
func mosaicConfigDir() string { return nembuConfigDir() }

func scopeConfigPath() string     { return filepath.Join(nembuConfigDir(), "scope.json") }
func onboardedMarkerPath() string { return filepath.Join(nembuConfigDir(), ".onboarded") }

func scopeOnboarded() bool {
	_, err := os.Stat(onboardedMarkerPath())
	return err == nil
}

// persistScope writes the choice and the onboarded marker atomically enough for
// first-run purposes: the marker only lands after the config write succeeds.
func persistScope(includeArchived bool) error {
	dir := mosaicConfigDir()
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	b, err := json.MarshalIndent(scopeConfig{IncludeArchived: includeArchived}, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(scopeConfigPath(), append(b, '\n'), 0600); err != nil {
		return err
	}
	_ = os.Chmod(scopeConfigPath(), 0600)
	return os.WriteFile(onboardedMarkerPath(), []byte("1\n"), 0600)
}

func readScope() scopeConfig {
	var s scopeConfig
	if b, err := os.ReadFile(scopeConfigPath()); err == nil {
		_ = json.Unmarshal(b, &s)
	}
	return s
}

func envHasKey() bool {
	if looksLikeGeminiKey(os.Getenv("GEMINI_API_KEY")) {
		return true
	}
	data, err := os.ReadFile(envFilePath())
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), "GEMINI_API_KEY=") {
			v := strings.Trim(strings.SplitN(line, "=", 2)[1], "\"' ")
			return looksLikeGeminiKey(v)
		}
	}
	return false
}

func saveKeyToEnv(key string) error {
	path := envFilePath()
	data, _ := os.ReadFile(path)
	lines := strings.Split(strings.TrimRight(string(data), "\n"), "\n")
	if len(lines) == 1 && lines[0] == "" {
		lines = nil
	}
	out := make([]string, 0, len(lines)+4)
	seen := map[string]bool{}
	for _, line := range lines {
		k := strings.SplitN(strings.TrimSpace(line), "=", 2)[0]
		if k == "GEMINI_API_KEY" || k == "GEMINI_MODEL" || k == "GEMINI_MODEL_AUDIO" || k == "GEMINI_MODEL_PRO" {
			continue
		}
		out = append(out, line)
		seen[k] = true
	}
	out = append(out,
		fmt.Sprintf("GEMINI_API_KEY=%q", key),
		`GEMINI_MODEL="gemini-3.5-flash-lite"`,
		`GEMINI_MODEL_AUDIO="gemini-3.5-flash-lite"`,
		`GEMINI_MODEL_PRO="gemini-3.1-pro-preview"`,
		`GEMINI_MODEL_QA="gemini-3.5-flash"`,
	)
	if err := os.WriteFile(path, []byte(strings.Join(out, "\n")+"\n"), 0600); err != nil {
		return err
	}
	// os.WriteFile only applies the mode when it creates the file, so an
	// existing .env with permissive bits must be tightened explicitly.
	_ = os.Chmod(path, 0600)
	os.Setenv("GEMINI_API_KEY", key)
	return nil
}

func configuredModel() string {
	if m := strings.TrimSpace(os.Getenv("GEMINI_MODEL")); m != "" {
		return m
	}
	if data, err := os.ReadFile(envFilePath()); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			line = strings.TrimSpace(line)
			if strings.HasPrefix(line, "GEMINI_MODEL=") {
				v := strings.Trim(strings.SplitN(line, "=", 2)[1], "\"' ")
				if v != "" {
					return v
				}
			}
		}
	}
	return "gemini-3.5-flash-lite"
}

// geminiBaseURL lets tests and support point validation at a fake or local
// endpoint without touching Google. Production uses the public API.
func geminiBaseURL() string {
	base := envWithFallback("NEMBU_GEMINI_BASE", "MOSAIC_GEMINI_BASE")
	if base == "" {
		base = "https://generativelanguage.googleapis.com/v1beta/models"
	}
	return strings.TrimRight(base, "/")
}

func validateKey(key string) tea.Cmd {
	return func() tea.Msg {
		body := `{"contents":[{"parts":[{"text":"Reply with exactly: OK"}]}],` +
			`"generationConfig":{"maxOutputTokens":1}}`
		url := geminiBaseURL() + "/" + configuredModel() + ":generateContent"
		req, err := http.NewRequest("POST", url, strings.NewReader(body))
		if err != nil {
			return keyCheckMsg{key: key, err: "Can't reach Google — check your internet"}
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("x-goog-api-key", key)
		cl := &http.Client{Timeout: 20 * time.Second}
		resp, err := cl.Do(req)
		if err != nil {
			return keyCheckMsg{key: key, err: "Can't reach Google — check your internet"}
		}
		defer resp.Body.Close()
		switch {
		case resp.StatusCode == 200:
			return keyCheckMsg{key: key, ok: true}
		case resp.StatusCode == 400 || resp.StatusCode == 401 || resp.StatusCode == 403:
			return keyCheckMsg{key: key, err: "This key isn't valid or the Gemini API isn't enabled for it"}
		case resp.StatusCode == 429:
			return keyCheckMsg{key: key, err: "This key is out of quota — try again later or use another key"}
		default:
			return keyCheckMsg{key: key, err: fmt.Sprintf("Gemini said HTTP %d", resp.StatusCode)}
		}
	}
}

func (m model) Init() tea.Cmd { return tea.Batch(fetchStatus(), tick()) }

func tick() tea.Cmd {
	return tea.Tick(1500*time.Millisecond, func(t time.Time) tea.Msg { return tickMsg(t) })
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height

	case statusMsg:
		m.status = bridgeStatus(msg)
		now := time.Now()
		if !m.lastSample.IsZero() && m.status.Messages >= m.lastCount {
			dt := now.Sub(m.lastSample).Seconds()
			if dt > 0.4 {
				inst := float64(m.status.Messages-m.lastCount) / dt
				if m.rate == 0 {
					m.rate = inst
				} else {
					m.rate = 0.7*m.rate + 0.3*inst
				}
			}
		}
		m.lastCount = m.status.Messages
		m.lastSample = now
		if m.prevOldest != "" && m.status.Oldest != "" && m.status.Oldest != m.prevOldest {
			m.deepenedAt = now
		}
		m.prevOldest = m.status.Oldest
		m.recordProgress(now)
		m.advance()
		m.n = readEngine()
		cmd := m.markDashboardOpened()
		return m, cmd

	case keyCheckMsg:
		m.keyBusy = false
		if !msg.ok {
			m.keyErr = msg.err
			return m, nil
		}
		if err := saveKeyToEnv(msg.key); err != nil {
			m.keyErr = "could not save .env: " + err.Error()
			return m, nil
		}
		m.keyErr = ""
		m.notice = ""
		m.screen = screenScope
		m.scopeChoice = 0
		return m, nil

	case tickMsg:
		m.frame++
		m.advance()
		cmds := []tea.Cmd{tick()}
		if m.screen != screenKey && m.screen != screenScope {
			cmds = append(cmds, fetchStatus())
		}
		if cmd := m.markDashboardOpened(); cmd != nil {
			cmds = append(cmds, cmd)
		}
		return m, tea.Batch(cmds...)

	case tea.KeyMsg:
		switch msg.String() {
		case "ctrl+c":
			return m, tea.Quit
		case "q":
			if m.screen == screenKey {
				m.keyInput += "q"
				return m, nil
			}
			if m.screen != screenScope {
				return m, tea.Quit
			}
		case "enter", " ":
			if m.screen == screenKey {
				if m.keyBusy {
					return m, nil
				}
				k := extractKey(strings.TrimSpace(m.keyInput))
				m.keyInput = k
				if !looksLikeGeminiKey(k) {
					m.keyErr = "that doesn't look like an API key — paste the full key"
					return m, nil
				}
				m.keyBusy = true
				m.keyErr = ""
				return m, validateKey(k)
			}
			if m.screen == screenScope {
				if err := persistScope(m.scopeChoice == 1); err != nil {
					m.scopeErr = "could not save the choice: " + err.Error()
					return m, nil
				}
				m.scopeErr = ""
				m.notice = "Saved. Starting pairing…"
				m.screen = screenConnect
				return m, tea.Batch(fetchStatus(), tick())
			}
			if m.screen == screenSync {
				m.hideSync = true
				m.screen = screenDash
				cmd := m.markDashboardOpened()
				return m, cmd
			}
			m.expandStep = !m.expandStep
		case "backspace":
			if m.screen == screenKey && len(m.keyInput) > 0 {
				m.keyInput = m.keyInput[:len(m.keyInput)-1]
			}
		case "l":
			if m.screen == screenKey {
				m.keyInput += "l"
				return m, nil
			}
			m.showFullLog = !m.showFullLog
		case "d":
			if m.screen == screenKey {
				m.keyInput += "d"
				return m, nil
			}
			if m.screen == screenSync {
				m.hideSync = true
				m.screen = screenDash
				cmd := m.markDashboardOpened()
				return m, cmd
			}
			if m.screen == screenDash && !m.status.Sync.Ready {
				m.hideSync = false
				m.screen = screenSync
				return m, nil
			}
		case "up", "down", "k", "j", "y", "n":
			if m.screen == screenScope {
				switch msg.String() {
				case "down", "j", "n":
					m.scopeChoice = 1
				default:
					m.scopeChoice = 0
				}
				return m, nil
			}
			switch msg.String() {
			case "up":
				if m.selTrace > 0 {
					m.selTrace--
					m.selStep = -1
				}
			case "down":
				m.selTrace++
				m.selStep = -1
			case "k":
				m.selStep--
			case "j":
				m.selStep++
			}
		case "esc":
			if m.screen == screenScope {
				_ = persistScope(false)
				m.notice = "Saved. Starting pairing…"
				m.screen = screenConnect
				return m, tea.Batch(fetchStatus(), tick())
			}
			if m.screen == screenQR || m.screen == screenSync {
				m.hideSync = true
				m.screen = screenDash
				cmd := m.markDashboardOpened()
				return m, cmd
			}
		case "ctrl+v":
			if m.screen == screenKey {
				if clip := clipboardRead(); clip != "" {
					m.keyInput = extractKey(clip)
				}
			}
		default:
			if m.screen == screenKey && len(msg.Runes) > 0 {
				// Terminals deliver a paste as one KeyMsg with many runes
				// (bracketed paste), not as a stream of single keys.
				var b strings.Builder
				for _, r := range msg.Runes {
					if r >= 32 && r < 127 {
						b.WriteRune(r)
					}
				}
				m.keyInput += b.String()
				// If that paste contained a key amid other text, keep only
				// the key — never make the operator hand-edit it.
				if k := extractKey(m.keyInput); k != m.keyInput {
					m.keyInput = k
				}
			}
		}
	}
	return m, nil
}

// ── views ────────────────────────────────────────────────────────────────────
func (m model) View() string {
	switch m.screen {
	case screenKey:
		return m.viewKey()
	case screenScope:
		return m.viewScope()
	case screenQR:
		return m.viewQR()
	case screenSync:
		return m.viewSync()
	case screenReady:
		return m.viewReady()
	case screenDash:
		return m.viewDash()
	default:
		return m.viewConnect()
	}
}

func (m model) frameLine() string {
	logo := stBrand.Render("Nembu 🍋")
	tag := stSubtle.Render("your WhatsApp chief of staff")
	right := stFaint.Render("v1")
	gap := 2
	width := m.width
	if width <= 0 {
		width = 80
	}
	used := lipgloss.Width(logo) + lipgloss.Width(tag) + gap + lipgloss.Width(right)
	pad := width - used - 2
	if pad < 1 {
		pad = 1
	}
	return stHeader.Width(width).Render(
		logo + "  " + tag + strings.Repeat(" ", pad) + right,
	)
}

func (m model) viewConnect() string {
	lines := []string{
		"",
		stSubtle.Render("Connecting to WhatsApp…"),
		"",
		stFaint.Render("If this takes more than a few seconds, the bridge is still starting."),
	}
	if m.notice != "" {
		lines = append(lines, "", stGood.Render(m.notice))
	}
	return m.frameLine() + "\n\n" + stInk.Render(strings.Join(lines, "\n")) + "\n"
}

func (m model) viewKey() string {
	label := stSubtle.Render("Gemini API key")
	help := stFaint.Render("aistudio.google.com/apikey  →  Create API key")
	masked := strings.Repeat("•", len(m.keyInput))
	if masked == "" {
		masked = stFaint.Render("paste your key, then Enter")
	} else {
		if len(masked) > 52 {
			masked = "…" + masked[len(masked)-51:]
		}
		masked = stInk.Render(masked) + stFaint.Render(fmt.Sprintf("  (%d)", len(m.keyInput)))
	}
	status := stFaint.Render("enter to verify · ctrl+c to quit")
	if m.keyBusy {
		status = stWarn.Render("checking with Google…")
	} else if m.keyErr != "" {
		status = stBad.Render(m.keyErr)
	}
	body := strings.Join([]string{
		"", "  " + label, "", "  " + stPanel.Width(64).Render(masked), "",
		"  " + help, "", "  " + status,
	}, "\n")
	return m.frameLine() + "\n" + body + "\n"
}

func (m model) viewScope() string {
	active := "  " + stInk.Render("Active chats only") + stGood.Render("  ← recommended")
	both := "  " + stInk.Render("Active and archived chats")
	if m.scopeChoice == 1 {
		active = "  " + stInk.Render("Active chats only")
		both = stGood.Render("▸ ") + stInk.Render("Active and archived chats")
	} else {
		active = stGood.Render("▸ ") + stInk.Render("Active chats only") + stGood.Render("  ← recommended")
	}
	status := stFaint.Render("↑↓ or y/n to choose · enter to continue · esc for active only")
	if m.scopeErr != "" {
		status = stBad.Render(m.scopeErr)
	}
	body := strings.Join([]string{
		"",
		"  " + stBrand.Render("Which chats should Nembu watch? 🍋"),
		"",
		"  " + stSubtle.Render("Archived chats are muted on your phone. Nembu can still watch"),
		"  " + stSubtle.Render("them for asks and deadlines if you choose to."),
		"",
		active,
		both,
		"",
		"  " + status,
	}, "\n")
	return m.frameLine() + "\n" + body + "\n"
}

func (m model) viewQR() string {
	var sb strings.Builder
	qrterminal.GenerateHalfBlock(m.status.QR, qrterminal.L, &sb)
	art := stInk.Render(sb.String())
	steps := strings.Join([]string{
		stSubtle.Render("1") + "  Open WhatsApp on your phone",
		stSubtle.Render("2") + "  Settings → Linked devices → Link a device",
		stSubtle.Render("3") + "  Point your camera at this code",
	}, "\n")
	left := lipgloss.JoinVertical(lipgloss.Left, art)
	right := lipgloss.JoinVertical(lipgloss.Left,
		stBrand.Render("Link your account"),
		"",
		steps,
		"",
		stFaint.Render("This screen advances by itself once linked."),
		stFaint.Render("esc for dashboard · ctrl+c to quit"),
	)
	body := lipgloss.JoinHorizontal(lipgloss.Top, left, "   ", right)
	return m.frameLine() + "\n\n" + stPanel.Render(body) + "\n"
}

func (m *model) advance() {
	if m.screen == screenKey || m.screen == screenScope {
		return
	}
	if !m.status.Paired {
		m.syncShownAt = time.Time{}
		if m.status.QR != "" {
			m.screen = screenQR
		} else {
			m.screen = screenConnect
		}
		return
	}
	if !m.status.Sync.Ready {
		m.readyShownAt = time.Time{}
		if m.hideSync {
			m.screen = screenDash
		} else {
			if m.screen != screenSync || m.syncShownAt.IsZero() {
				m.syncShownAt = time.Now()
			}
			m.screen = screenSync
		}
		return
	}
	m.syncShownAt = time.Time{}
	// Ready transition: show the summary briefly, then the dashboard.
	if m.readyShownAt.IsZero() {
		m.readyShownAt = time.Now()
	}
	if !m.hideSync && time.Since(m.readyShownAt) < 3*time.Second {
		m.screen = screenReady
		return
	}
	m.screen = screenDash
}

// mechatHelpVisible surfaces the no-Me-chat failure instead of leaving the
// sync screen on "resolving your Me chat…" forever. Answers cannot be delivered
// without it, so the user gets an actionable hint after a short grace period.
func mechatHelpVisible(syncShownAt time.Time, resolved bool, now time.Time) bool {
	if resolved || syncShownAt.IsZero() {
		return false
	}
	return now.Sub(syncShownAt) >= 90*time.Second
}

var spinnerFrames = []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"}

func clampProgress(p int) int {
	if p < 0 {
		return 0
	}
	if p > 100 {
		return 100
	}
	return p
}

type progressSample struct {
	at       time.Time
	progress int
}

// recordProgress keeps a short in-memory history of changes only. Flat stretches
// are the signal for "finishing up", not repeated polling.
func (m *model) recordProgress(now time.Time) {
	p := clampProgress(m.status.Sync.Progress)
	if n := len(m.progressSamples); n == 0 || m.progressSamples[n-1].progress != p {
		m.progressSamples = append(m.progressSamples, progressSample{at: now, progress: p})
	}
	cut := now.Add(-2 * time.Minute)
	i := 0
	for i < len(m.progressSamples) && m.progressSamples[i].at.Before(cut) {
		i++
	}
	if i > 0 {
		m.progressSamples = m.progressSamples[i:]
	}
}

// syncStalledUI mirrors the bridge's bounded "finishing" state for display.
func syncStalledUI(samples []progressSample, progress int, now time.Time) bool {
	if progress < 80 || len(samples) == 0 {
		return false
	}
	return now.Sub(samples[len(samples)-1].at) >= 60*time.Second
}

// syncETA estimates the remaining time from movement in the last 30s+. It never
// invents precision: unknown, flat or extreme rates fall back to plain wording.
func syncETA(samples []progressSample, progress int, now time.Time) string {
	if progress <= 0 {
		return "still working — esc to dashboard"
	}
	if syncStalledUI(samples, progress, now) {
		return "finishing up…"
	}
	for _, s := range samples {
		if s.progress >= progress || now.Sub(s.at) < 30*time.Second {
			continue
		}
		rate := float64(progress-s.progress) / now.Sub(s.at).Seconds()
		if rate <= 0 {
			break
		}
		mins := float64(100-progress) / rate / 60
		switch {
		case mins < 1:
			return "less than a minute left"
		case mins > 120:
			return "still working — esc to dashboard"
		default:
			return fmt.Sprintf("~%d min left", int(mins+0.5))
		}
	}
	return "still working — esc to dashboard"
}

func humanCount(n int64) string {
	s := strconv.FormatInt(n, 10)
	out := ""
	for i, ch := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			out += ","
		}
		out += string(ch)
	}
	return out
}

func shortDate(raw string) string {
	raw = strings.TrimSpace(raw)
	if len(raw) >= 10 {
		if t, err := time.Parse("2006-01-02", raw[:10]); err == nil {
			if t.Year() == time.Now().Year() {
				return t.Format("02 Jan")
			}
			return t.Format("02 Jan 2006")
		}
	}
	return "—"
}

// historyDepth renders how much time the synced store spans.
func historyDepth(oldest, newest string) string {
	parse := func(raw string) (time.Time, bool) {
		raw = strings.TrimSpace(raw)
		if len(raw) < 10 {
			return time.Time{}, false
		}
		t, err := time.Parse("2006-01-02", raw[:10])
		return t, err == nil
	}
	o, ok1 := parse(oldest)
	n, ok2 := parse(newest)
	if !ok1 || !ok2 || n.Before(o) {
		return "—"
	}
	days := n.Sub(o).Hours() / 24
	switch {
	case days >= 300:
		return fmt.Sprintf("%.1f years", days/365)
	case days >= 60:
		return fmt.Sprintf("%.0f months", days/30)
	case days >= 14:
		return fmt.Sprintf("%.0f weeks", days/7)
	default:
		return fmt.Sprintf("%.0f days", days)
	}
}

func oldestAgeDays(raw string) float64 {
	raw = strings.TrimSpace(raw)
	if len(raw) >= 10 {
		if t, err := time.Parse("2006-01-02", raw[:10]); err == nil {
			return time.Since(t).Hours() / 24
		}
	}
	return -1
}

// newestLag returns minutes between the newest synced message and now;
// -1 when the timestamp cannot be parsed.
func newestLag(raw string) float64 {
	raw = strings.TrimSpace(raw)
	if t, err := time.Parse("2006-01-02 15:04:05-07:00", raw); err == nil {
		return time.Since(t).Minutes()
	}
	// Zone-less rows are local wall-clock values (WhatsApp/IST), not UTC.
	if t, err := time.ParseInLocation("2006-01-02 15:04:05", raw, time.Local); err == nil {
		return time.Since(t).Minutes()
	}
	return -1
}

func (m model) viewSync() string {
	width := m.width
	if width <= 0 {
		width = 90
	}
	barW := width - 14
	if barW < 24 {
		barW = 24
	}
	if barW > 54 {
		barW = 54
	}
	progress := clampProgress(m.status.Sync.Progress)

	var bar string
	pct := ""
	if progress > 0 {
		filled := barW * progress / 100
		bar = stGood.Render(strings.Repeat("▰", filled)) +
			stFaint.Render(strings.Repeat("─", barW-filled))
		pct = fmt.Sprintf(" %d%%", progress)
	} else {
		// Indeterminate shimmer until the first progress value arrives.
		head := m.frame % (barW + 14)
		cells := make([]string, barW)
		for i := 0; i < barW; i++ {
			d := i - head
			switch {
			case d == 0:
				cells[i] = stAccent.Render("▰")
			case d > -4 && d < 0:
				cells[i] = stBlue.Render("▰")
			case d < -4 && d > -9:
				cells[i] = stFaint.Render("▰")
			default:
				cells[i] = stFaint.Render("─")
			}
		}
		bar = strings.Join(cells, "")
	}

	title := stBrand.Render(spinnerFrames[(m.frame/2)%len(spinnerFrames)]) + "  " +
		stInk.Render("Syncing your last 30 days…"+pct)
	phase := ""
	switch m.status.Sync.Phase {
	case "RECENT", "INITIAL_BOOTSTRAP":
		phase = "recent chats"
	case "FULL":
		phase = "older history"
	case "PUSH_NAME":
		phase = "contact names"
	case "ON_DEMAND":
		phase = "older history on demand"
	}
	if phase == "" {
		phase = "waiting for WhatsApp"
	}

	meLine := stFaint.Render("resolving your Me chat…")
	if m.status.Sync.MechatResolved {
		meLine = stGood.Render("✓ Me chat found")
	}
	covering := shortDate(m.status.Sync.Oldest)
	if covering == "—" {
		covering = "—"
	}
	rows := []string{
		stSubtle.Render("chats") + "     " + stInk.Render(humanCount(m.status.Sync.Chats)),
		stSubtle.Render("messages") + "  " + stInk.Render(humanCount(m.status.Sync.Messages)),
		stSubtle.Render("back to") + "   " + stInk.Render(covering),
		stSubtle.Render("phase") + "     " + stInk.Render(phase),
		meLine,
	}
	if mechatHelpVisible(m.syncShownAt, m.status.Sync.MechatResolved, time.Now()) {
		rows = append(rows, "", stBad.Render(
			"Couldn't find your Me chat. In WhatsApp open “Message yourself” once, then restart Nembu."))
	}
	eta := syncETA(m.progressSamples, progress, time.Now())
	etaLine := stInk.Render(eta)
	stallNote := ""
	if syncStalledUI(m.progressSamples, progress, time.Now()) {
		etaLine = stWarn.Render(fmt.Sprintf("still syncing %d%% — esc — dashboard", progress))
		stallNote = "\n" + stFaint.Render(eta)
	}
	escape := stWarn.Render("esc — open dashboard now · d / enter also work · sync continues in the background")
	hint := stFaint.Render("history backfills in the background")
	if m.status.Sync.Chats == 0 && m.status.Sync.Phase == "" {
		hint = stFaint.Render("if nothing arrives in about 90 seconds, Nembu starts watching new messages")
	}
	pipe := m.recentEventsBody(width-8, 5)
	body := lipgloss.JoinVertical(lipgloss.Left,
		title, "", "  "+bar, "",
		strings.Join(rows, "\n"), "",
		etaLine+stallNote, escape, "",
		stSubtle.Render("LIVE PIPELINE")+"\n"+pipe, "", hint,
	)
	return m.frameLine() + "\n\n" + stPanel.Width(width-4).Render(body) + "\n"
}

func (m model) viewReady() string {
	width := m.width
	if width <= 0 {
		width = 90
	}
	line := fmt.Sprintf("30 days synced: %s chats, %s messages. Nembu is now watching. 🍋",
		humanCount(m.status.Sync.Chats), humanCount(m.status.Sync.Messages))
	body := lipgloss.JoinVertical(lipgloss.Left,
		stGood.Render("✓")+"  "+stInk.Render("Ready"),
		"",
		stInk.Render(line),
		"",
		stFaint.Render("Opening the dashboard…"),
	)
	return m.frameLine() + "\n\n" + stPanel.Width(width-4).Render(body) + "\n"
}

type pipeEvent struct {
	Ts       string `json:"ts"`
	MsgID    string `json:"msg_id"`
	Source   string `json:"source"`
	Step     string `json:"step"`
	Status   string `json:"status"`
	Detail   string `json:"detail"`
	DurMS    int64  `json:"dur_ms"`
	Prompt   string `json:"prompt"`
	Response string `json:"response"`
}

func readPipeline(max int) []pipeEvent {
	f, err := os.Open(storePath("pipeline.log"))
	if err != nil {
		return nil
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil
	}
	const tail = int64(96 * 1024)
	off := int64(0)
	if st.Size() > tail {
		off = st.Size() - tail
	}
	if _, err := f.Seek(off, 0); err != nil {
		return nil
	}
	data, err := io.ReadAll(f)
	if err != nil {
		return nil
	}
	lines := strings.Split(string(data), "\n")
	if off > 0 && len(lines) > 0 {
		lines = lines[1:] // drop a partial first line
	}
	out := []pipeEvent{}
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var e pipeEvent
		if json.Unmarshal([]byte(line), &e) == nil && e.Step != "" {
			out = append(out, e)
		}
	}
	if len(out) > max {
		out = out[len(out)-max:]
	}
	return out
}

func clockOf(ts string) string {
	if len(ts) >= 19 {
		return ts[11:19]
	}
	return ts
}

func stepLabel(e pipeEvent) string {
	switch e.Step {
	case "msg.store":
		if e.Detail == "audio" {
			return "audio received"
		}
		return "message received"
	case "media.download":
		if e.Status == "error" {
			return "audio fetch failed"
		}
		return "fetching audio"
	case "media.cached":
		return "audio cached"
	case "media.retry":
		if e.Status == "error" {
			return "phone: audio gone"
		}
		if e.Status == "ok" {
			return "audio recovered"
		}
		return "asking phone"
	case "ingest":
		if e.Detail != "" && !strings.HasPrefix(e.Detail, "voice") {
			return "message received"
		}
		return "voice received"
	case "voice.queue":
		return "waiting for audio"
	case "voice.retry":
		return "audio retry"
	case "voice.transcript":
		if e.Status == "ok" {
			return "transcribed"
		}
		return "transcribe failed"
	case "voice.fail":
		return "audio unavailable"
	case "classify":
		return "tool chosen"
	case "llm.request":
		return "prompt sent"
	case "llm.response":
		return "model replied"
	case "qa.candidate":
		return "candidate"
	case "qa.start":
		return "reading question"
	case "qa.signal":
		return "group matched"
	case "qa.understand":
		return "understanding"
	case "qa.resolve":
		return "resolving group"
	case "qa.retrieve":
		return "reading history"
	case "qa.expert":
		return "writing answer"
	case "qa.answer":
		return "answer ready"
	case "route":
		return "routing"
	case "send":
		return "sent"
	}
	return e.Step
}

func detailLabel(e pipeEvent) string {
	d := strings.ReplaceAll(e.Detail, "\n", " ")
	if e.Step == "media.download" && strings.Contains(d, "status code 403") {
		return "403 — media link expired"
	}
	if e.Step == "media.download" && e.Status != "error" && d == "arrival attempt" {
		return "from the new message"
	}
	if e.Step == "media.download" && e.Status != "error" && strings.Contains(d, "history") {
		return "from history sync"
	}
	if e.Step == "media.download" && e.Status == "ok" {
		return "audio saved"
	}
	if e.Step == "ingest" && d == "voice note" {
		return ""
	}
	if e.Step == "media.retry" && strings.Contains(d, "media no longer available") {
		return "WhatsApp says the audio is gone"
	}
	if e.Step == "voice.transcript" && e.Status == "ok" {
		return "\u201c" + d + "\u201d"
	}
	return d
}

func nextHint(evs []pipeEvent) string {
	if len(evs) == 0 {
		return ""
	}
	for _, e := range evs {
		if e.Step == "voice.fail" {
			return "done: audio could not be read — I asked you to type or resend"
		}
	}
	transcribed := false
	for _, e := range evs {
		if e.Step == "voice.transcript" && e.Status == "ok" {
			transcribed = true
		}
	}
	last := evs[len(evs)-1]
	if last.Step == "send" && transcribed {
		return "done: transcribed, answered and sent"
	}
	switch last.Step {
	case "media.retry":
		if last.Status == "error" {
			return "next: audio is gone — I'll ask you to type or resend"
		}
		return "next: waiting for your phone to re-send the audio"
	case "voice.queue", "voice.retry", "voice.transcript":
		if last.Status == "error" {
			return "next: one more try, then I'll tell you it failed"
		}
		return "next: transcribing, then answering"
	case "voice.fail":
		return "done: told you the audio could not be read"
	case "qa.start", "qa.signal":
		return "next: understanding the question"
	case "qa.understand":
		return "next: picking which messages to read"
	case "qa.retrieve":
		return "next: writing the answer"
	case "qa.expert":
		return "next: formatting and sending"
	case "qa.answer":
		return "next: sending to you"
	case "send":
		return "done"
	}
	return ""
}

func renderEventLines(evs []pipeEvent, width int) string {
	var b []string
	for _, e := range evs {
		icon := stFaint.Render("●")
		stepStyle := stInk
		switch e.Status {
		case "ok":
			icon = stGood.Render("✓")
		case "warn":
			icon = stWarn.Render("!")
			stepStyle = stWarn
		case "error":
			icon = stBad.Render("✗")
			stepStyle = stBad
		}
		step := stepLabel(e)
		if len(step) > 20 {
			step = step[:20]
		}
		room := width - 12 - len(step) - 8
		if room < 8 {
			room = 8
		}
		detail := truncate(detailLabel(e), room)
		dur := ""
		if e.DurMS > 0 {
			dur = stFaint.Render(fmt.Sprintf(" %dms", e.DurMS))
		}
		b = append(b, stFaint.Render(clockOf(e.Ts))+" "+icon+" "+
			stepStyle.Render(fmt.Sprintf("%-20s", step))+" "+stFaint.Render(detail)+dur)
	}
	return strings.Join(b, "\n")
}

var exampleQuestions = []string{
	"what did I miss today?",
	"what's pending with Nisha?",
	"what did I get done this week?",
	"top 10",
	"mute this group 7d",
}

func (m model) recentEventsBody(width int, n int) string {
	evs := readPipeline(n)
	if len(evs) == 0 {
		// 3 ticks at 1.5 s = 4.5 s per question: cheap and deterministic.
		q := exampleQuestions[(m.frame/3)%len(exampleQuestions)]
		return stFaint.Render("  Live pipeline is loading…") + "\n" +
			stInk.Render("  try: “"+q+"”")
	}
	return renderEventLines(evs, width)
}

func (m model) pipelineBody(width int, lines int) string {
	all := readPipeline(lines)
	if len(all) == 0 {
		return stFaint.Render("  waiting for the next message…")
	}
	var b []string
	for _, e := range all {
		icon := stFaint.Render("●")
		stepStyle := stInk
		switch e.Status {
		case "ok":
			icon = stGood.Render("✓")
		case "warn":
			icon = stWarn.Render("!")
			stepStyle = stWarn
		case "error":
			icon = stBad.Render("✗")
			stepStyle = stBad
		}
		step := stepLabel(e)
		if len(step) > 18 {
			step = step[:18]
		}
		msg := ""
		if e.MsgID != "" {
			msg = " " + shortID(e.MsgID)
		}
		room := width - 12 - len(step) - 8 - len(msg)
		if room < 6 {
			room = 6
		}
		detail := truncate(detailLabel(e), room)
		dur := ""
		if e.DurMS > 0 {
			dur = stFaint.Render(fmt.Sprintf(" %dms", e.DurMS))
		}
		b = append(b, stFaint.Render(clockOf(e.Ts))+" "+icon+" "+
			stepStyle.Render(fmt.Sprintf("%-18s", step))+" "+stFaint.Render(detail)+dur+
			stFaint.Render(msg))
	}
	if h := nextHint(all); h != "" {
		b = append(b, stFaint.Render(h))
	}
	return strings.Join(b, "\n")
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func shortID(id string) string {
	r := []rune(id)
	if len(r) <= 10 {
		return id
	}
	return string(r[:6]) + "…"
}

type traceItem struct {
	id      string
	events  []pipeEvent
	start   time.Time
	last    time.Time
	summary string
	status  string
}

func parseTS(s string) time.Time {
	t, _ := time.Parse(time.RFC3339Nano, s)
	return t
}

func statusIcon(status string) string {
	switch status {
	case "ok":
		return stGood.Render("✓")
	case "warn":
		return stWarn.Render("!")
	case "error":
		return stBad.Render("✗")
	}
	return stFaint.Render("●")
}

func buildTraces(evs []pipeEvent) []traceItem {
	order := []string{}
	byID := map[string]*traceItem{}
	for _, e := range evs {
		if e.MsgID == "" {
			continue
		}
		tr, ok := byID[e.MsgID]
		if !ok {
			tr = &traceItem{id: e.MsgID}
			byID[e.MsgID] = tr
			order = append(order, e.MsgID)
		}
		tr.events = append(tr.events, e)
		ts := parseTS(e.Ts)
		if tr.start.IsZero() || ts.Before(tr.start) {
			tr.start = ts
		}
		if ts.After(tr.last) {
			tr.last = ts
		}
		if e.Status == "error" {
			tr.status = "error"
		} else if e.Status == "warn" && tr.status == "" {
			tr.status = "warn"
		}
	}
	out := []traceItem{}
	for _, id := range order {
		tr := byID[id]
		if tr.status == "" {
			tr.status = "ok"
		}
		sum := ""
		for _, e := range tr.events {
			if e.Step == "ingest" && e.Status == "ok" {
				sum = strings.TrimPrefix(e.Detail, "text: ")
				break
			}
			if e.Step == "voice.queue" || (e.Step == "msg.store" && e.Detail == "audio") {
				sum = "voice note"
			}
		}
		if sum == "" {
			sum = "message"
		}
		tr.summary = strings.TrimSpace(sum)
		out = append(out, *tr)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].last.After(out[j].last) })
	return out
}

func (m model) inspectorHeader(width int, traces []traceItem) string {
	pill := stBad.Render("● offline")
	switch {
	case m.status.Paired && m.status.Connected && m.status.Mechat != "":
		pill = stGood.Render("● live")
	case m.status.Paired:
		pill = stWarn.Render("● linking")
	}
	engine := stFaint.Render("engine idle")
	if m.status.Worker {
		engine = stGood.Render("engine on")
	}
	sync := ""
	if m.status.Syncing {
		sync = stWarn.Render(fmt.Sprintf("syncing %s chats", humanCount(m.status.Chats)))
	} else if m.status.Chats > 0 {
		sync = stFaint.Render(fmt.Sprintf("%s chats · %s msgs", humanCount(m.status.Chats), humanCount(m.n.TotalMessages)))
	}
	bits := []string{pill, engine}
	if m.status.Mechat != "" {
		bits = append(bits, stFaint.Render("me: "+shortJID(m.status.Mechat)))
	}
	if sync != "" {
		bits = append(bits, sync)
	}
	bits = append(bits, stFaint.Render(fmt.Sprintf("pings %d/4 · $%.3f", m.n.PingsDelivered, m.n.Spend)))
	bits = append(bits, stFaint.Render(fmt.Sprintf("outbox %d queued · %d retrying · %d failed today",
		m.n.Queued, m.n.Retrying, m.n.FailedToday)))
	bits = append(bits, stFaint.Render(fmt.Sprintf("radar %s · next %s",
		m.n.LastRadar, m.n.NextRadar)))
	return strings.Join(bits, stFaint.Render("  ·  "))
}

func (m model) viewDash() string {
	width := m.width
	if width <= 0 {
		width = 96
	}
	evs := readPipeline(800)
	traces := buildTraces(evs)
	if m.selTrace >= len(traces) {
		m.selTrace = maxInt(0, len(traces)-1)
	}
	if m.selTrace < 0 {
		m.selTrace = 0
	}

	header := m.frameLine()
	banner := ""
	if !m.status.Sync.Ready {
		pct := m.status.Sync.Progress
		if pct < 0 {
			pct = 0
		}
		banner = stWarn.Render(fmt.Sprintf(
			"still syncing %d%% — sync continues in the background · d — sync details", pct)) + "\n"
	}
	status := m.inspectorHeader(width, traces)

	if len(traces) == 0 {
		body := stPanel.Width(width - 4).Render(
			stSubtle.Render("PIPELINE") + "\n\n" +
				stFaint.Render("Waiting for the next Me-chat message…") + "\n\n" +
				stFaint.Render("Every step will appear here: tool choice, group, prompt,") + "\n" +
				stFaint.Render("model response, retrieval, answer and delivery."))
		hint := "q quit  ·  ↑↓ message  ·  j/k step  ·  enter expand  ·  l full text"
		if !m.status.Sync.Ready {
			hint = "sync continues in the background  ·  d — sync details  ·  " + hint
		}
		return header + "\n" + banner + status + "\n\n" + body + "\n\n" +
			stFaint.Render(hint)
	}

	sel := traces[m.selTrace]
	n := len(sel.events)
	if m.selStep < 0 {
		m.selStep = n - 1
		for i := n - 1; i >= 0; i-- {
			if sel.events[i].Prompt != "" || sel.events[i].Response != "" {
				m.selStep = i
				break
			}
		}
	}
	if m.selStep >= n {
		m.selStep = n - 1
	}
	if m.selStep < 0 {
		m.selStep = 0
	}

	// trace list
	var tl []string
	for i, tr := range traces {
		if i > 7 {
			break
		}
		line := fmt.Sprintf("%s %s  %s  %-38s %s",
			statusIcon(tr.status), stFaint.Render(tr.start.Format("15:04:05")),
			stFaint.Render(shortID(tr.id)), truncate(tr.summary, 38),
			stFaint.Render(fmt.Sprintf("%d steps", len(tr.events))))
		if i == m.selTrace {
			line = lipgloss.NewStyle().Background(lipgloss.Color("237")).Render("▸ " + line)
		} else {
			line = "  " + line
		}
		tl = append(tl, line)
	}
	list := stPanel.Width(width - 4).Render(
		stSubtle.Render("MESSAGES") + stFaint.Render("   newest first") + "\n\n" +
			strings.Join(tl, "\n"))

	// steps of the selected trace
	var sl []string
	for i, e := range sel.events {
		step := stepLabel(e)
		if len(step) > 20 {
			step = step[:20]
		}
		detail := detailLabel(e)
		room := width - 44
		if room < 8 {
			room = 8
		}
		dur := ""
		if e.DurMS > 0 {
			dur = stFaint.Render(fmt.Sprintf(" %dms", e.DurMS))
		}
		line := fmt.Sprintf("%s %s %s %s%s",
			stFaint.Render(clockOf(e.Ts)), statusIcon(e.Status),
			stInk.Render(fmt.Sprintf("%-22s", step)),
			stFaint.Render(truncate(detail, room)), dur)
		if i == m.selStep {
			line = lipgloss.NewStyle().Background(lipgloss.Color("236")).Render("▸ " + line)
		} else {
			line = "  " + line
		}
		sl = append(sl, line)
	}
	steps := stPanel.Width(width - 4).Render(
		stSubtle.Render("STEPS") + stFaint.Render("   "+shortID(sel.id)) + "\n\n" +
			strings.Join(sl, "\n"))

	// detail of the selected step
	step := sel.events[m.selStep]
	limit := 600
	if m.expandStep || m.showFullLog {
		limit = 2000
	}
	var dl []string
	dl = append(dl, stSubtle.Render("DETAIL")+" "+stFaint.Render(fmt.Sprintf("%s · %s", shortID(sel.id), stepLabel(step))))
	dl = append(dl, "")
	if step.Prompt != "" {
		dl = append(dl, stBlue.Render(fmt.Sprintf("PROMPT (%d chars)", len(step.Prompt))))
		dl = append(dl, indent(truncate(unescape(step.Prompt), limit), "  "))
		dl = append(dl, "")
	}
	if step.Response != "" {
		dl = append(dl, stAccent.Render(fmt.Sprintf("RESPONSE (%d chars)", len(step.Response))))
		dl = append(dl, indent(truncate(unescape(step.Response), limit), "  "))
		dl = append(dl, "")
	}
	if step.Prompt == "" && step.Response == "" {
		dl = append(dl, stInk.Render(step.Detail))
		if step.DurMS > 0 {
			dl = append(dl, "", stFaint.Render(fmt.Sprintf("took %dms", step.DurMS)))
		}
	}
	if h := nextHint(sel.events); h != "" {
		dl = append(dl, "", stFaint.Render(h))
	}
	detail := stPanel.Width(width - 4).Render(strings.Join(dl, "\n"))

	footer := stFaint.Render("q quit  ·  ↑↓ message  ·  j/k step  ·  enter " +
		map[bool]string{true: "collapse", false: "expand"}[m.expandStep] +
		"  ·  l " + map[bool]string{true: "short text", false: "full text"}[m.showFullLog])
	return header + "\n" + banner + status + "\n\n" + list + "\n" + steps + "\n" + detail + "\n\n" + footer + "\n"
}

func unescape(s string) string {
	s = strings.ReplaceAll(s, "\\n", "\n")
	s = strings.ReplaceAll(s, "\\t", "\t")
	s = strings.ReplaceAll(s, "\\\"", "\"")
	return s
}

func indent(s string, pad string) string {
	lines := strings.Split(s, "\n")
	for i := range lines {
		lines[i] = pad + lines[i]
	}
	return strings.Join(lines, "\n")
}

func shortJID(j string) string {
	if j == "" {
		return "resolving…"
	}
	return strings.Split(j, "@")[0]
}

func truncate(s string, n int) string {
	if n < 8 {
		n = 8
	}
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-1]) + "…"
}

func humanTime(ts string) string {
	if ts == "" {
		return "—"
	}
	for _, layout := range []string{"2006-01-02 15:04:05-07:00", "2006-01-02 15:04:05"} {
		if t, err := time.Parse(layout, ts); err == nil {
			d := time.Since(t)
			if d < time.Minute {
				return "just now"
			}
			if d < time.Hour {
				return fmt.Sprintf("%dm ago", int(d.Minutes()))
			}
			if d < 24*time.Hour {
				return fmt.Sprintf("%dh ago", int(d.Hours()))
			}
			return fmt.Sprintf("%dd ago", int(d.Hours()/24))
		}
	}
	return ts
}

func runTUI() error {
	p := tea.NewProgram(initialModel(), tea.WithAltScreen())
	_, err := p.Run()
	return err
}

// keeps filepath imported for storePath usage parity
var _ = filepath.Join
