package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/proto/waCompanionReg"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	wastore "go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// deviceName is what WhatsApp shows under Linked devices. The phone decides
// the final label, so it may render as "Nembu" or "<Browser> (Nembu)".
const deviceName = "Nembu"

// verboseMode gates per-message console echo (NEMBU_VERBOSE=1, legacy MOSAIC_VERBOSE=1).
var verboseMode = envFlag("NEMBU_VERBOSE", "MOSAIC_VERBOSE")

// Message represents a chat message for our client
type Message struct {
	Time      time.Time
	Sender    string
	Content   string
	IsFromMe  bool
	MediaType string
	Filename  string
}

// Database handler for storing message history
type MessageStore struct {
	db *sql.DB
}

// Initialize message store
func NewMessageStore() (*MessageStore, error) {
	// Create directory for database if it doesn't exist
	if err := os.MkdirAll(storePath(""), 0755); err != nil {
		return nil, fmt.Errorf("failed to create store directory: %v", err)
	}

	// Open SQLite database for messages
	db, err := sql.Open("sqlite3", "file:"+storePath("messages.db")+"?_foreign_keys=on&_journal_mode=WAL&_busy_timeout=5000")
	if err != nil {
		return nil, fmt.Errorf("failed to open message database: %v", err)
	}

	// Create tables if they don't exist
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS chats (
			jid TEXT PRIMARY KEY,
			name TEXT,
			last_message_time TIMESTAMP
		);
		
		CREATE TABLE IF NOT EXISTS messages (
			id TEXT,
			chat_jid TEXT,
			sender TEXT,
			content TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN,
			media_type TEXT,
			filename TEXT,
			url TEXT,
			media_key BLOB,
			file_sha256 BLOB,
			file_enc_sha256 BLOB,
			file_length INTEGER,
			local_path TEXT,
			quoted_id TEXT,
			quoted_sender TEXT,
			PRIMARY KEY (id, chat_jid),
			FOREIGN KEY (chat_jid) REFERENCES chats(jid)
		);

		CREATE TABLE IF NOT EXISTS bot_sends (
			id TEXT PRIMARY KEY,
			chat_jid TEXT,
			timestamp TIMESTAMP
		);
	`)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create tables: %v", err)
	}
	// Migration for older stores: media cached at arrival time.
	_, _ = db.Exec("ALTER TABLE messages ADD COLUMN local_path TEXT")
	_, _ = db.Exec("ALTER TABLE messages ADD COLUMN quoted_id TEXT")
	_, _ = db.Exec("ALTER TABLE messages ADD COLUMN quoted_sender TEXT")
	// Range queries (sync screen) must not scan the whole store.
	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)")

	return &MessageStore{db: db}, nil
}

// Close the database connection
func (store *MessageStore) Close() error {
	return store.db.Close()
}

// MarkBotSend records that this message ID came from one of OUR sends
// (engine/app), not from a human. reads use it to never re-ingest bot text
// as user turns. is_from_me alone can't cover this: an echo or another-device
// send is also is_from_me but the user typed it.
func (store *MessageStore) MarkBotSend(id, chatJID string, ts time.Time) {
	if id == "" {
		return
	}
	_, err := store.db.Exec(
		"INSERT OR REPLACE INTO bot_sends (id, chat_jid, timestamp) VALUES (?, ?, ?)",
		id, chatJID, ts,
	)
	if err == nil {
		noteStoreWrite()
	}
}

// Store a chat in the database
func (store *MessageStore) StoreChat(jid, name string, lastMessageTime time.Time) error {
	_, err := store.db.Exec(
		"INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
		jid, name, lastMessageTime,
	)
	if err == nil {
		noteStoreWrite()
	}
	return err
}

// ── media retry (voice notes from history have stale URLs: 403) ─────────────
type mediaRetryEntry struct {
	chatJID   string
	filename  string
	mediaType string
	mediaKey  []byte
	sha256    []byte
	encSHA256 []byte
	length    uint64
}

var (
	mediaRetryMu    sync.Mutex
	mediaRetryCache = map[types.MessageID]*mediaRetryEntry{}
)

func requestMediaRetry(client *whatsmeow.Client, store *MessageStore, id, chatJID string) {
	var isFromMe bool
	var sender, filename, mediaType string
	var mediaKey, sha, enc []byte
	var length uint64
	err := store.db.QueryRow(
		"SELECT is_from_me, COALESCE(sender,''), COALESCE(filename,''), COALESCE(media_type,''), "+
			"COALESCE(media_key,X''), COALESCE(file_sha256,X''), COALESCE(file_enc_sha256,X''), "+
			"COALESCE(file_length,0) FROM messages WHERE id=? AND chat_jid=?",
		id, chatJID,
	).Scan(&isFromMe, &sender, &filename, &mediaType, &mediaKey, &sha, &enc, &length)
	if err != nil || len(mediaKey) == 0 {
		return
	}
	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return
	}
	info := &types.MessageInfo{
		MessageSource: types.MessageSource{
			Chat:     chat,
			IsFromMe: isFromMe,
			IsGroup:  strings.HasSuffix(chatJID, "@g.us"),
		},
		ID: types.MessageID(id),
	}
	if info.IsGroup && sender != "" {
		if s, err := types.ParseJID(sender); err == nil {
			info.Sender = s
		}
	}
	if err := client.SendMediaRetryReceipt(context.Background(), info, mediaKey); err != nil {
		client.Log.Warnf("media retry request failed for %s: %v", id, err)
		return
	}
	client.Log.Warnf("media retry requested for %s", id)
	if isMeChat(chatJID) {
		appendEvent("bridge", id, "media.retry", "warn", "asked phone to re-upload", 0)
	}
	mediaRetryMu.Lock()
	mediaRetryCache[types.MessageID(id)] = &mediaRetryEntry{
		chatJID: chatJID, filename: filename, mediaType: mediaType,
		mediaKey: mediaKey, sha256: sha, encSHA256: enc, length: length,
	}
	mediaRetryMu.Unlock()
}

func handleMediaRetry(client *whatsmeow.Client, store *MessageStore, evt *events.MediaRetry, logger waLog.Logger) {
	mediaRetryMu.Lock()
	entry := mediaRetryCache[evt.MessageID]
	mediaRetryMu.Unlock()
	if entry == nil {
		return
	}
	notif, err := whatsmeow.DecryptMediaRetryNotification(evt, entry.mediaKey)
	if err != nil {
		logger.Warnf("media retry decrypt failed for %s: %v", evt.MessageID, err)
		if entry != nil && isMeChat(entry.chatJID) {
			appendEvent("bridge", string(evt.MessageID), "media.retry", "error", err.Error(), 0)
		}
		return
	}
	if notif.GetResult() != waMmsRetry.MediaRetryNotification_SUCCESS {
		logger.Warnf("media retry rejected for %s: %v", evt.MessageID, notif.GetResult())
		if entry != nil && isMeChat(entry.chatJID) {
			appendEvent("bridge", string(evt.MessageID), "media.retry", "error",
				"rejected: "+notif.GetResult().String(), 0)
		}
		return
	}
	mt := whatsmeow.MediaAudio
	mms := "audio"
	if entry.mediaType == "image" {
		mt, mms = whatsmeow.MediaImage, "image"
	} else if entry.mediaType == "video" {
		mt, mms = whatsmeow.MediaVideo, "video"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	data, err := client.DownloadMediaWithPath(ctx, notif.GetDirectPath(),
		entry.encSHA256, entry.sha256, entry.mediaKey, mt, mms, false)
	if err != nil {
		logger.Warnf("media retry download failed for %s: %v", evt.MessageID, err)
		return
	}
	dir := storePath(strings.ReplaceAll(entry.chatJID, ":", "_"))
	if err := os.MkdirAll(dir, 0755); err != nil {
		return
	}
	name := entry.filename
	if name == "" {
		name = string(evt.MessageID)
	}
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, data, 0644); err != nil {
		return
	}
	store.SetLocalPath(string(evt.MessageID), entry.chatJID, path)
	if isMeChat(entry.chatJID) {
		appendEvent("bridge", string(evt.MessageID), "media.retry", "ok", path, 0)
	}
	mediaRetryMu.Lock()
	delete(mediaRetryCache, evt.MessageID)
	mediaRetryMu.Unlock()
	logger.Infof("media retry recovered %s", evt.MessageID)
}

// ── pipeline event log (JSONL) for the TUI trace ────────────────────────────
var (
	eventMu   sync.Mutex
	meChatMu  sync.Mutex
	meChatSet = map[string]bool{}
	meChatAt  time.Time
)

// isMeChat reports whether a chat is the user's own chat (either JID form).
func isMeChat(jid string) bool {
	meChatMu.Lock()
	defer meChatMu.Unlock()
	if time.Since(meChatAt) > 30*time.Second || len(meChatSet) == 0 {
		meChatSet = map[string]bool{}
		if st := readMechatState(); st.JID != "" {
			meChatSet[st.JID] = true
		}
		if st := readMechatState(); st.Phone != "" {
			meChatSet[st.Phone+"@s.whatsapp.net"] = true
		}
		meChatAt = time.Now()
	}
	return meChatSet[jid]
}

func appendEvent(source, msgID, step, status, detail string, durMS int64) {
	eventMu.Lock()
	defer eventMu.Unlock()
	rec := map[string]any{
		"ts":     time.Now().Format(time.RFC3339Nano),
		"msg_id": msgID,
		"source": source,
		"step":   step,
		"status": status,
		"detail": truncateStr(detail, 300),
		"dur_ms": durMS,
	}
	b, err := json.Marshal(rec)
	if err != nil {
		return
	}
	f, err := os.OpenFile(storePath("pipeline.log"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.Write(append(b, '\n'))
}

func truncateStr(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// historyWindowDays is how much history we keep. WhatsApp still sends its
// full initial sync; dropping old rows makes the first sync land fast and
// keeps the store small. 0 disables the filter (keep everything).
func historyWindowDays() int {
	raw := strings.TrimSpace(envWithFallback("NEMBU_HISTORY_DAYS", "MOSAIC_HISTORY_DAYS"))
	if raw == "" {
		return 30
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 0 {
		return 30
	}
	return n
}

// pruneHistory deletes messages older than the kept window and the media files
// that belong to them. Timestamps are passed as time.Time so the comparison
// matches exactly what the go-sqlite3 driver wrote.
func (store *MessageStore) pruneHistory(days int) error {
	if days <= 0 {
		return nil
	}
	cutoff := time.Now().AddDate(0, 0, -days)

	rows, err := store.db.Query(
		"SELECT local_path FROM messages WHERE timestamp < ? AND local_path IS NOT NULL AND local_path != ''",
		cutoff,
	)
	if err == nil {
		var paths []string
		for rows.Next() {
			var p string
			if rows.Scan(&p) == nil && p != "" {
				paths = append(paths, p)
			}
		}
		rows.Close()
		for _, p := range paths {
			if err := os.Remove(p); err != nil && !os.IsNotExist(err) {
				// Best effort: a locked or missing file must not stop the prune.
				fmt.Printf("prune: could not remove %s: %v\n", p, err)
			}
		}
	}

	if _, err := store.db.Exec("DELETE FROM messages WHERE timestamp < ?", cutoff); err != nil {
		return err
	}
	if _, err := store.db.Exec("DELETE FROM bot_sends WHERE timestamp < ?", cutoff); err != nil {
		return err
	}
	if _, err := store.db.Exec(
		"DELETE FROM chats WHERE jid NOT IN (SELECT DISTINCT chat_jid FROM messages) AND last_message_time < ?",
		cutoff,
	); err != nil {
		return err
	}

	// incremental_vacuum needs auto_vacuum=INCREMENTAL; when it is not enabled
	// SQLite ignores it harmlessly. A full VACUUM runs at most once a day.
	_, _ = store.db.Exec("PRAGMA incremental_vacuum")
	pruneVacuumMu.Lock()
	if time.Since(pruneVacuumAt) > 24*time.Hour {
		_, _ = store.db.Exec("VACUUM")
		pruneVacuumAt = time.Now()
	}
	pruneVacuumMu.Unlock()
	return nil
}

var (
	pruneVacuumMu sync.Mutex
	pruneVacuumAt time.Time
)

// pruneWhatsmeowSecrets is optional and guarded: it only trims secret rows whose
// message is already gone from messages.db. Every other whatsmeow table holds
// encryption sessions, keys, contacts and the LID map and must never be trimmed.
func pruneWhatsmeowSecrets(store *MessageStore) {
	wa, err := sql.Open("sqlite3", "file:"+storePath("whatsapp.db")+"?_busy_timeout=5000")
	if err != nil {
		return
	}
	defer wa.Close()
	var n int
	if wa.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='whatsmeow_message_secrets'").Scan(&n) != nil || n == 0 {
		return
	}
	_, _ = wa.Exec("ATTACH DATABASE ? AS msg", "file:"+storePath("messages.db")+"?mode=ro")
	defer wa.Exec("DETACH DATABASE msg")
	_, _ = wa.Exec("DELETE FROM whatsmeow_message_secrets WHERE message_id NOT IN (SELECT id FROM msg.messages)")
}

// startHistoryPruner runs the first prune shortly after startup and then every
// six hours. Section 4 re-anchors the first run to the sync-ready transition.
func startHistoryPruner(store *MessageStore) {
	go func() {
		// First prune ~2 minutes after the last 30 days are actually in.
		deadline := time.Now().Add(10 * time.Minute)
		for !syncReady.Load() && time.Now().Before(deadline) {
			time.Sleep(5 * time.Second)
		}
		time.Sleep(2 * time.Minute)
		if err := store.pruneHistory(historyWindowDays()); err != nil {
			fmt.Printf("prune error: %v\n", err)
		}
		pruneWhatsmeowSecrets(store)
		ticker := time.NewTicker(6 * time.Hour)
		defer ticker.Stop()
		for range ticker.C {
			if err := store.pruneHistory(historyWindowDays()); err != nil {
				fmt.Printf("prune error: %v\n", err)
			}
			pruneWhatsmeowSecrets(store)
		}
	}()
}

// shouldStoreMessage decides whether a message is inside the kept window.
func shouldStoreMessage(ts time.Time, now time.Time, days int) bool {
	if days <= 0 {
		return true
	}
	return !ts.Before(now.AddDate(0, 0, -days))
}

// Store a message in the database
func (store *MessageStore) StoreMessage(id, chatJID, sender, content string, timestamp time.Time, isFromMe bool,
	mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64,
	quotedID, quotedSender string) error {
	// Only store if there's actual content or media
	if content == "" && mediaType == "" {
		return nil
	}
	if !shouldStoreMessage(timestamp, time.Now(), historyWindowDays()) {
		return nil
	}

	_, err := store.db.Exec(
		`INSERT OR REPLACE INTO messages 
		(id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length, quoted_id, quoted_sender) 
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		id, chatJID, sender, content, timestamp, isFromMe, mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength,
		quotedID, quotedSender,
	)
	if err == nil {
		noteStoreWrite()
		syncStoredMsgs.Add(1)
	}
	return err
}

// SetLocalPath records where a media file was cached at arrival time. The
// only reliable moment to fetch WhatsApp media is right after the event.
func (store *MessageStore) SetLocalPath(id, chatJID, path string) {
	_, err := store.db.Exec(
		"UPDATE messages SET local_path=? WHERE id=? AND chat_jid=?",
		path, id, chatJID,
	)
	if err == nil {
		noteStoreWrite()
	}
}

// LocalPath returns a cached file path when one exists and is readable.
func (store *MessageStore) LocalPath(id, chatJID string) string {
	var p sql.NullString
	_ = store.db.QueryRow(
		"SELECT local_path FROM messages WHERE id=? AND chat_jid=?", id, chatJID,
	).Scan(&p)
	if p.Valid {
		if _, err := os.Stat(p.String); err == nil {
			return p.String
		}
	}
	return ""
}

// IsBotSend reports whether this message ID was sent by Nembu itself.
func (store *MessageStore) IsBotSend(id string) bool {
	var n int
	_ = store.db.QueryRow("SELECT COUNT(*) FROM bot_sends WHERE id=?", id).Scan(&n)
	return n > 0
}

// ChatCount returns the number of known chats (for the sync loader bar).
func (store *MessageStore) ChatCount() (int, error) {
	var n int
	err := store.db.QueryRow("SELECT COUNT(*) FROM chats").Scan(&n)
	return n, err
}

// Get messages from a chat
func (store *MessageStore) GetMessages(chatJID string, limit int) ([]Message, error) {
	rows, err := store.db.Query(
		"SELECT sender, content, timestamp, is_from_me, media_type, filename FROM messages WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT ?",
		chatJID, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var messages []Message
	for rows.Next() {
		var msg Message
		var timestamp time.Time
		err := rows.Scan(&msg.Sender, &msg.Content, &timestamp, &msg.IsFromMe, &msg.MediaType, &msg.Filename)
		if err != nil {
			return nil, err
		}
		msg.Time = timestamp
		messages = append(messages, msg)
	}

	return messages, nil
}

// Get all chats
func (store *MessageStore) GetChats() (map[string]time.Time, error) {
	rows, err := store.db.Query("SELECT jid, last_message_time FROM chats ORDER BY last_message_time DESC")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	chats := make(map[string]time.Time)
	for rows.Next() {
		var jid string
		var lastMessageTime time.Time
		err := rows.Scan(&jid, &lastMessageTime)
		if err != nil {
			return nil, err
		}
		chats[jid] = lastMessageTime
	}

	return chats, nil
}

// LastMessageAt returns the most recent message timestamp for a chat, or "".
func (store *MessageStore) LastMessageAt(chatJID string) string {
	var ts time.Time
	err := store.db.QueryRow(
		"SELECT MAX(timestamp) FROM messages WHERE chat_jid = ?", chatJID,
	).Scan(&ts)
	if err != nil || ts.IsZero() {
		return ""
	}
	return ts.Format("2006-01-02 15:04:05")
}

// contextInfoFor returns the ContextInfo wherever the message type carries it.
// Voice notes keep the quoted id on AudioMessage.ContextInfo, not on text.
func contextInfoFor(msg *waProto.Message) *waProto.ContextInfo {
	if msg == nil {
		return nil
	}
	switch {
	case msg.GetExtendedTextMessage() != nil:
		return msg.GetExtendedTextMessage().GetContextInfo()
	case msg.GetImageMessage() != nil:
		return msg.GetImageMessage().GetContextInfo()
	case msg.GetVideoMessage() != nil:
		return msg.GetVideoMessage().GetContextInfo()
	case msg.GetAudioMessage() != nil:
		return msg.GetAudioMessage().GetContextInfo()
	case msg.GetDocumentMessage() != nil:
		return msg.GetDocumentMessage().GetContextInfo()
	case msg.GetStickerMessage() != nil:
		return msg.GetStickerMessage().GetContextInfo()
	case msg.GetContactMessage() != nil:
		return msg.GetContactMessage().GetContextInfo()
	case msg.GetLocationMessage() != nil:
		return msg.GetLocationMessage().GetContextInfo()
	}
	return nil
}

// extractQuote returns the quoted (replied-to) message id and sender.
func extractQuote(msg *waProto.Message) (string, string) {
	ci := contextInfoFor(msg)
	if ci == nil {
		return "", ""
	}
	return ci.GetStanzaID(), ci.GetParticipant()
}

// Extract text content from a message
func extractTextContent(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}

	// Try to get text content
	if text := msg.GetConversation(); text != "" {
		return text
	} else if extendedText := msg.GetExtendedTextMessage(); extendedText != nil {
		return extendedText.GetText()
	}

	// For now, we're ignoring non-text messages
	return ""
}

// SendMessageResponse represents the response for the send message API
type SendMessageResponse struct {
	Success   bool   `json:"success"`
	Message   string `json:"message"`
	MessageID string `json:"message_id,omitempty"`
}

// SendMessageRequest represents the request body for the send message API
type SendMessageRequest struct {
	Recipient string `json:"recipient"`
	Message   string `json:"message"`
	MediaPath string `json:"media_path,omitempty"`
}

// Function to send a WhatsApp message
func sendWhatsAppMessage(client *whatsmeow.Client, recipient string, message string, mediaPath string) (bool, string, string) {
	if !client.IsConnected() {
		return false, "Not connected to WhatsApp", ""
	}

	// Create JID for recipient
	var recipientJID types.JID
	var err error

	// Check if recipient is a JID
	isJID := strings.Contains(recipient, "@")

	if isJID {
		// Parse the JID string
		recipientJID, err = types.ParseJID(recipient)
		if err != nil {
			return false, fmt.Sprintf("Error parsing JID: %v", err), ""
		}
	} else {
		// Create JID from phone number
		recipientJID = types.JID{
			User:   recipient,
			Server: "s.whatsapp.net", // For personal chats
		}
	}

	msg := &waProto.Message{}

	// Check if we have media to send
	if mediaPath != "" {
		// Read media file
		mediaData, err := os.ReadFile(mediaPath)
		if err != nil {
			return false, fmt.Sprintf("Error reading media file: %v", err), ""
		}

		// Determine media type and mime type based on file extension
		fileExt := strings.ToLower(mediaPath[strings.LastIndex(mediaPath, ".")+1:])
		var mediaType whatsmeow.MediaType
		var mimeType string

		// Handle different media types
		switch fileExt {
		// Image types
		case "jpg", "jpeg":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/jpeg"
		case "png":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/png"
		case "gif":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/gif"
		case "webp":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/webp"

		// Audio types
		case "ogg":
			mediaType = whatsmeow.MediaAudio
			mimeType = "audio/ogg; codecs=opus"

		// Video types
		case "mp4":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/mp4"
		case "avi":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/avi"
		case "mov":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/quicktime"

		// Document types (for any other file type)
		default:
			mediaType = whatsmeow.MediaDocument
			mimeType = "application/octet-stream"
		}

		// Upload media to WhatsApp servers
		resp, err := client.Upload(context.Background(), mediaData, mediaType)
		if err != nil {
			return false, fmt.Sprintf("Error uploading media: %v", err), ""
		}

		fmt.Println("Media uploaded", resp)

		// Create the appropriate message type based on media type
		switch mediaType {
		case whatsmeow.MediaImage:
			msg.ImageMessage = &waProto.ImageMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaAudio:
			// Handle ogg audio files
			var seconds uint32 = 30 // Default fallback
			var waveform []byte = nil

			// Try to analyze the ogg file
			if strings.Contains(mimeType, "ogg") {
				analyzedSeconds, analyzedWaveform, err := analyzeOggOpus(mediaData)
				if err == nil {
					seconds = analyzedSeconds
					waveform = analyzedWaveform
				} else {
					return false, fmt.Sprintf("Failed to analyze Ogg Opus file: %v", err), ""
				}
			} else {
				fmt.Printf("Not an Ogg Opus file: %s\n", mimeType)
			}

			msg.AudioMessage = &waProto.AudioMessage{
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				Seconds:       proto.Uint32(seconds),
				PTT:           proto.Bool(true),
				Waveform:      waveform,
			}
		case whatsmeow.MediaVideo:
			msg.VideoMessage = &waProto.VideoMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaDocument:
			msg.DocumentMessage = &waProto.DocumentMessage{
				Title:         proto.String(mediaPath[strings.LastIndex(mediaPath, "/")+1:]),
				FileName:      proto.String(mediaPath[strings.LastIndex(mediaPath, "/")+1:]),
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		}
	} else {
		msg.Conversation = proto.String(message)
	}

	// Send message
	resp, err := client.SendMessage(context.Background(), recipientJID, msg)

	if err != nil {
		return false, fmt.Sprintf("Error sending message: %v", err), ""
	}

	return true, fmt.Sprintf("Message sent to %s", recipient), string(resp.ID)
}

// Extract media info from a message
func extractMediaInfo(msg *waProto.Message) (mediaType string, filename string, url string, mediaKey []byte, fileSHA256 []byte, fileEncSHA256 []byte, fileLength uint64) {
	if msg == nil {
		return "", "", "", nil, nil, nil, 0
	}

	// Check for image message
	if img := msg.GetImageMessage(); img != nil {
		return "image", "image_" + time.Now().Format("20060102_150405") + ".jpg",
			img.GetURL(), img.GetMediaKey(), img.GetFileSHA256(), img.GetFileEncSHA256(), img.GetFileLength()
	}

	// Check for video message
	if vid := msg.GetVideoMessage(); vid != nil {
		return "video", "video_" + time.Now().Format("20060102_150405") + ".mp4",
			vid.GetURL(), vid.GetMediaKey(), vid.GetFileSHA256(), vid.GetFileEncSHA256(), vid.GetFileLength()
	}

	// Check for audio message
	if aud := msg.GetAudioMessage(); aud != nil {
		return "audio", "audio_" + time.Now().Format("20060102_150405") + ".ogg",
			aud.GetURL(), aud.GetMediaKey(), aud.GetFileSHA256(), aud.GetFileEncSHA256(), aud.GetFileLength()
	}

	// Check for document message
	if doc := msg.GetDocumentMessage(); doc != nil {
		filename := doc.GetFileName()
		if filename == "" {
			filename = "document_" + time.Now().Format("20060102_150405")
		}
		return "document", filename,
			doc.GetURL(), doc.GetMediaKey(), doc.GetFileSHA256(), doc.GetFileEncSHA256(), doc.GetFileLength()
	}

	return "", "", "", nil, nil, nil, 0
}

// Handle regular incoming messages with media support
func handleMessage(client *whatsmeow.Client, messageStore *MessageStore, msg *events.Message, logger waLog.Logger) {
	// Save message to database
	chatJID := msg.Info.Chat.String()
	sender := msg.Info.Sender.User

	// Get appropriate chat name (pass nil for conversation since we don't have one for regular messages)
	name := GetChatName(client, messageStore, msg.Info.Chat, chatJID, nil, sender, logger)

	// Update chat in database with the message timestamp (keeps last message time updated)
	err := messageStore.StoreChat(chatJID, name, msg.Info.Timestamp)
	if err != nil {
		logger.Warnf("Failed to store chat: %v", err)
	}

	// Extract text content
	content := extractTextContent(msg.Message)

	// Extract media info
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength := extractMediaInfo(msg.Message)

	// Skip if there's no content and no media
	if content == "" && mediaType == "" {
		return
	}

	// Store message in database
	quotedID, quotedSender := extractQuote(msg.Message)
	err = messageStore.StoreMessage(
		msg.Info.ID,
		chatJID,
		sender,
		content,
		msg.Info.Timestamp,
		msg.Info.IsFromMe,
		mediaType,
		filename,
		url,
		mediaKey,
		fileSHA256,
		fileEncSHA256,
		fileLength,
		quotedID,
		quotedSender,
	)

	if err != nil {
		logger.Warnf("Failed to store message: %v", err)
		appendEvent("bridge", string(msg.Info.ID), "msg.store", "error", err.Error(), 0)
	} else {
		if isMeChat(chatJID) && mediaType == "audio" {
			appendEvent("bridge", string(msg.Info.ID), "msg.store", "ok", "audio", 0)
		}
		// Log message reception
		timestamp := msg.Info.Timestamp.Format("2006-01-02 15:04:05")
		direction := "←"
		if msg.Info.IsFromMe {
			direction = "→"
		}

		// Per-message echo is a debugging aid, not a UI: gate it behind
		// MOSAIC_VERBOSE so the console stays clean (the TUI is the UI).
		if verboseMode {
			if mediaType != "" {
				fmt.Printf("[%s] %s %s: [%s: %s] %s\n", timestamp, direction, sender, mediaType, filename, content)
			} else if content != "" {
				fmt.Printf("[%s] %s %s: %s\n", timestamp, direction, sender, content)
			}
		}

		// Cache audio immediately: WhatsApp media URLs expire (403) and
		// voice notes must be transcribable minutes later.
		if mediaType == "audio" {
			go func(id, jid string) {
				if messageStore.LocalPath(id, jid) != "" {
					return
				}
				if isMeChat(jid) {
					appendEvent("bridge", id, "media.download", "info", "arrival attempt", 0)
				}
				t0 := time.Now()
				ok, _, _, path, err := downloadMedia(client, messageStore, id, jid)
				dur := time.Since(t0).Milliseconds()
				if ok && err == nil && path != "" {
					messageStore.SetLocalPath(id, jid, path)
					if isMeChat(jid) {
						appendEvent("bridge", id, "media.download", "ok", path, dur)
					}
				} else {
					reason := "download failed"
					if err != nil {
						reason = err.Error()
					}
					if isMeChat(jid) {
						appendEvent("bridge", id, "media.download", "error", reason, dur)
					}
					requestMediaRetry(client, messageStore, id, jid)
				}
			}(string(msg.Info.ID), chatJID)
		}

		// The Me chat is the only command surface: the worker ingests every
		// message there as a free-text turn. Group messages are only stored.
	}
}

// DownloadMediaRequest represents the request body for the download media API
type DownloadMediaRequest struct {
	MessageID string `json:"message_id"`
	ChatJID   string `json:"chat_jid"`
}

// DownloadMediaResponse represents the response for the download media API
type DownloadMediaResponse struct {
	Success  bool   `json:"success"`
	Message  string `json:"message"`
	Filename string `json:"filename,omitempty"`
	Path     string `json:"path,omitempty"`
}

// Store additional media info in the database
func (store *MessageStore) StoreMediaInfo(id, chatJID, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	_, err := store.db.Exec(
		"UPDATE messages SET url = ?, media_key = ?, file_sha256 = ?, file_enc_sha256 = ?, file_length = ? WHERE id = ? AND chat_jid = ?",
		url, mediaKey, fileSHA256, fileEncSHA256, fileLength, id, chatJID,
	)
	return err
}

// Get media info from the database
func (store *MessageStore) GetMediaInfo(id, chatJID string) (string, string, string, []byte, []byte, []byte, uint64, error) {
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64

	err := store.db.QueryRow(
		"SELECT media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length FROM messages WHERE id = ? AND chat_jid = ?",
		id, chatJID,
	).Scan(&mediaType, &filename, &url, &mediaKey, &fileSHA256, &fileEncSHA256, &fileLength)

	return mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err
}

// MediaDownloader implements the whatsmeow.DownloadableMessage interface
type MediaDownloader struct {
	URL           string
	DirectPath    string
	MediaKey      []byte
	FileLength    uint64
	FileSHA256    []byte
	FileEncSHA256 []byte
	MediaType     whatsmeow.MediaType
}

// GetDirectPath implements the DownloadableMessage interface
func (d *MediaDownloader) GetDirectPath() string {
	return d.DirectPath
}

// GetURL implements the DownloadableMessage interface
func (d *MediaDownloader) GetURL() string {
	return d.URL
}

// GetMediaKey implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaKey() []byte {
	return d.MediaKey
}

// GetFileLength implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileLength() uint64 {
	return d.FileLength
}

// GetFileSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileSHA256() []byte {
	return d.FileSHA256
}

// GetFileEncSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileEncSHA256() []byte {
	return d.FileEncSHA256
}

// GetMediaType implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaType() whatsmeow.MediaType {
	return d.MediaType
}

// Function to download media from a message
func downloadMedia(client *whatsmeow.Client, messageStore *MessageStore, messageID, chatJID string) (bool, string, string, string, error) {
	// Query the database for the message
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64
	var err error

	// First, check if we already have this file
	chatDir := storePath(strings.ReplaceAll(chatJID, ":", "_"))
	localPath := ""

	// Get media info from the database
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err = messageStore.GetMediaInfo(messageID, chatJID)

	if err != nil {
		// Try to get basic info if extended info isn't available
		err = messageStore.db.QueryRow(
			"SELECT media_type, filename FROM messages WHERE id = ? AND chat_jid = ?",
			messageID, chatJID,
		).Scan(&mediaType, &filename)

		if err != nil {
			return false, "", "", "", fmt.Errorf("failed to find message: %v", err)
		}
	}

	// Check if this is a media message
	if mediaType == "" {
		return false, "", "", "", fmt.Errorf("not a media message")
	}

	// Create directory for the chat if it doesn't exist
	if err := os.MkdirAll(chatDir, 0755); err != nil {
		return false, "", "", "", fmt.Errorf("failed to create chat directory: %v", err)
	}

	// Generate a local path for the file
	localPath = fmt.Sprintf("%s/%s", chatDir, filename)

	// Get absolute path
	absPath, err := filepath.Abs(localPath)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to get absolute path: %v", err)
	}

	// Check if file already exists
	if _, err := os.Stat(localPath); err == nil {
		// File exists, return it
		return true, mediaType, filename, absPath, nil
	}

	// If we don't have all the media info we need, we can't download
	if url == "" || len(mediaKey) == 0 || len(fileSHA256) == 0 || len(fileEncSHA256) == 0 || fileLength == 0 {
		return false, "", "", "", fmt.Errorf("incomplete media information for download")
	}

	fmt.Printf("Attempting to download media for message %s in chat %s...\n", messageID, chatJID)

	// Extract direct path from URL
	directPath := extractDirectPathFromURL(url)

	// Create a downloader that implements DownloadableMessage
	var waMediaType whatsmeow.MediaType
	switch mediaType {
	case "image":
		waMediaType = whatsmeow.MediaImage
	case "video":
		waMediaType = whatsmeow.MediaVideo
	case "audio":
		waMediaType = whatsmeow.MediaAudio
	case "document":
		waMediaType = whatsmeow.MediaDocument
	default:
		return false, "", "", "", fmt.Errorf("unsupported media type: %s", mediaType)
	}

	downloader := &MediaDownloader{
		URL:           url,
		DirectPath:    directPath,
		MediaKey:      mediaKey,
		FileLength:    fileLength,
		FileSHA256:    fileSHA256,
		FileEncSHA256: fileEncSHA256,
		MediaType:     waMediaType,
	}

	// Download the media using whatsmeow client
	mediaData, err := client.Download(context.Background(), downloader)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to download media: %v", err)
	}

	// Save the downloaded media to file
	if err := os.WriteFile(localPath, mediaData, 0644); err != nil {
		return false, "", "", "", fmt.Errorf("failed to save media file: %v", err)
	}

	fmt.Printf("Successfully downloaded %s media to %s (%d bytes)\n", mediaType, absPath, len(mediaData))
	return true, mediaType, filename, absPath, nil
}

// Extract direct path from a WhatsApp media URL
func extractDirectPathFromURL(url string) string {
	// The direct path is typically in the URL, we need to extract it
	// Example URL: https://mmg.whatsapp.net/v/t62.7118-24/13812002_111111111111111_3424455886509161511_n.enc?ccb=11-4&oh=...

	// Find the path part after the domain
	parts := strings.SplitN(url, ".net/", 2)
	if len(parts) < 2 {
		return url // Return original URL if parsing fails
	}

	pathPart := parts[1]

	// Remove query parameters
	pathPart = strings.SplitN(pathPart, "?", 2)[0]

	// Create proper direct path format
	return "/" + pathPart
}

// Start the internal REST API (loopback only, token-gated) used by the
// Python engine.
func startInternalServer(client *whatsmeow.Client, messageStore *MessageStore, logger waLog.Logger) {
	// Handler for sending messages
	http.HandleFunc("/api/send", requireToken(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				writeJSON(w, http.StatusInternalServerError, SendMessageResponse{
					Success: false, Message: fmt.Sprintf("internal error: %v", rec),
				})
			}
		}()
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, SendMessageResponse{Success: false, Message: "method not allowed"})
			return
		}
		var req SendMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(w, http.StatusBadRequest, SendMessageResponse{Success: false, Message: "bad request"})
			return
		}
		if req.Recipient == "" {
			writeJSON(w, http.StatusBadRequest, SendMessageResponse{Success: false, Message: "recipient required"})
			return
		}
		if req.Message == "" && req.MediaPath == "" {
			writeJSON(w, http.StatusBadRequest, SendMessageResponse{Success: false, Message: "message required"})
			return
		}
		if strings.Contains(req.Recipient, "@") {
			if _, err := types.ParseJID(req.Recipient); err != nil {
				writeJSON(w, http.StatusBadRequest, SendMessageResponse{Success: false, Message: "invalid recipient"})
				return
			}
		}
		if !client.IsConnected() {
			writeJSON(w, http.StatusServiceUnavailable, SendMessageResponse{Success: false, Message: "not connected"})
			return
		}
		success, message, msgID := sendWhatsAppMessage(client, req.Recipient, req.Message, req.MediaPath)
		if !success {
			code := http.StatusInternalServerError
			if strings.Contains(message, "Error parsing JID") {
				code = http.StatusBadRequest
			}
			writeJSON(w, code, SendMessageResponse{Success: false, Message: message})
			appendEvent("bridge", "", "send", "error", message, 0)
			return
		}
		writeJSON(w, http.StatusOK, SendMessageResponse{Success: true, Message: message, MessageID: msgID})
		if msgID != "" {
			messageStore.MarkBotSend(msgID, req.Recipient, time.Now().UTC())
			appendEvent("bridge", msgID, "send", "ok", "to "+req.Recipient, 0)
		}
	}))

	// Status for the TUI (token-gated like every other loopback call).
	http.HandleFunc("/api/status", requireToken(func(w http.ResponseWriter, r *http.Request) {
		qrMu.Lock()
		qr := currentQRCode
		qrMu.Unlock()
		me := readMechatState()
		workerMu.Lock()
		wrk := workerRunning
		workerMu.Unlock()
		paired := client.Store.ID != nil
		now := time.Now()
		syncing := paired && !syncReady.Load()
		chats, _ := messageStore.ChatCount()
		var oldest, newest string
		if messageStore != nil {
			// What we actually STORE (the kept window), not chat metadata.
			_ = messageStore.db.QueryRow(
				"SELECT COALESCE(MIN(timestamp),''), COALESCE(MAX(timestamp),'') FROM messages",
			).Scan(&oldest, &newest)
		}
		var stored int
		if messageStore != nil {
			_ = messageStore.db.QueryRow("SELECT COUNT(*) FROM messages").Scan(&stored)
		}
		elapsed := int64(0)
		if p := syncPairedAt.Load(); p > 0 {
			elapsed = now.Unix() - p
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"connected":       client.IsConnected(),
			"paired":          paired,
			"qr":              qr,
			"mechat_jid":      me.JID,
			"mechat_resolved": me.JID != "",
			"worker":          wrk,
			"syncing":         syncing,
			"chats":           chats,
			"messages":        stored,
			"oldest":          oldest,
			"newest":          newest,
			"synced_seconds":  elapsed,
			"sync":            syncStatus(messageStore),
		})
	}))

	// The TUI tells the bridge when the dashboard first opens, so the one-shot
	// how-to can be sent even when sync is still running (user pressed esc).
	http.HandleFunc("/api/dashboard-opened", requireToken(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"ok": false, "error": "method not allowed"})
			return
		}
		maybeHowto(logger)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	}))

	// Handler for downloading media
	http.HandleFunc("/api/download", requireToken(func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req DownloadMediaRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "Message ID and Chat JID are required", http.StatusBadRequest)
			return
		}

		if cached := messageStore.LocalPath(req.MessageID, req.ChatJID); cached != "" {
			if isMeChat(req.ChatJID) {
				appendEvent("bridge", req.MessageID, "media.cached", "ok", cached, 0)
			}
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(DownloadMediaResponse{
				Success: true, Message: "cached", Path: cached,
			})
			return
		}

		// Download the media
		success, mediaType, filename, path, err := downloadMedia(client, messageStore, req.MessageID, req.ChatJID)
		if success && path != "" {
			messageStore.SetLocalPath(req.MessageID, req.ChatJID, path)
		}

		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Handle download result
		if !success || err != nil {
			errMsg := "Unknown error"
			if err != nil {
				errMsg = err.Error()
			}

			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(DownloadMediaResponse{
				Success: false,
				Message: fmt.Sprintf("Failed to download media: %s", errMsg),
			})
			return
		}

		// Send successful response
		json.NewEncoder(w).Encode(DownloadMediaResponse{
			Success:  true,
			Message:  fmt.Sprintf("Successfully downloaded %s media", mediaType),
			Filename: filename,
			Path:     path,
		})
	}))

	// Start the server
	serverAddr := "127.0.0.1:8090"
	fmt.Printf("Internal API (loopback, token-gated) on %s...\n", serverAddr)

	// Run server in a goroutine so it doesn't block
	go func() {
		if err := http.ListenAndServe(serverAddr, nil); err != nil {
			fmt.Printf("Internal API server error: %v\n", err)
		}
	}()
}

func main() {
	// Nembu Alpha console: `nembu tui` — a live view over the same store
	// the engine writes. The chat itself remains the product surface.
	if len(os.Args) > 1 && os.Args[1] == "tui" && len(os.Args) > 2 && os.Args[2] == "--snapshot" {
		// Render one frame to stdout (no TTY) — packaging/debug aid.
		m := initialModel()
		m.width, m.height = 96, 30
		if msg, ok := fetchStatus()().(statusMsg); ok {
			m.status = bridgeStatus(msg)
		}
		if envWithFallback("NEMBU_SNAPSHOT", "MOSAIC_SNAPSHOT") == "dash" {
			m.status.Sync.Ready = true
			m.readyShownAt = time.Now().Add(-10 * time.Second)
		}
		m.advance()
		fmt.Println(m.View())
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "tui" {
		if err := runTUI(); err != nil {
			fmt.Printf("tui error: %v\n", err)
			os.Exit(1)
		}
		return
	}

	// Set up logger
	logLevel := strings.ToUpper(strings.TrimSpace(envWithFallback("NEMBU_LOG_LEVEL", "MOSAIC_LOG_LEVEL")))
	if logLevel == "" {
		logLevel = "WARN"
	}
	logger := waLog.Stdout("Client", logLevel, true)
	logger.Infof("Starting WhatsApp client...")

	// Create database connection for storing session data
	dbLog := waLog.Stdout("Database", logLevel, true)

	// Create directory for database if it doesn't exist
	if err := os.MkdirAll(storePath(""), 0755); err != nil {
		logger.Errorf("Failed to create store directory: %v", err)
		return
	}
	writePID("bridge.pid", os.Getpid())
	defer removePID("bridge.pid")

	initToken()

	// Brand the linked device before the store creates or loads a device.
	// These props are read when NewDevice() builds the companion registration.
	wastore.DeviceProps.Os = proto.String(deviceName)
	wastore.DeviceProps.PlatformType = waCompanionReg.DeviceProps_DESKTOP.Enum()
	wastore.DeviceProps.RequireFullSync = proto.Bool(false)
	if wastore.DeviceProps.HistorySyncConfig == nil {
		wastore.DeviceProps.HistorySyncConfig = &waCompanionReg.DeviceProps_HistorySyncConfig{}
	}
	wastore.DeviceProps.HistorySyncConfig.FullSyncDaysLimit = proto.Uint32(30)
	wastore.DeviceProps.HistorySyncConfig.RecentSyncDaysLimit = proto.Uint32(30)

	container, err := sqlstore.New(context.Background(), "sqlite3", "file:"+storePath("whatsapp.db")+"?_foreign_keys=on", dbLog)
	if err != nil {
		logger.Errorf("Failed to connect to database: %v", err)
		return
	}

	// Get device store - This contains session information
	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		if err == sql.ErrNoRows {
			// No device exists, create one
			deviceStore = container.NewDevice()
			logger.Infof("Created new device")
		} else {
			logger.Errorf("Failed to get device: %v", err)
			return
		}
	}

	// Create client instance
	client := whatsmeow.NewClient(deviceStore, logger)
	if client == nil {
		logger.Errorf("Failed to create WhatsApp client")
		return
	}

	// Initialize message store
	messageStore, err := NewMessageStore()
	if err != nil {
		logger.Errorf("Failed to initialize message store: %v", err)
		return
	}
	defer messageStore.Close()

	startHistoryPruner(messageStore)
	startSyncWatcher(client, messageStore, logger)

	// Setup event handling for messages and history sync
	client.AddEventHandler(func(evt interface{}) {
		switch v := evt.(type) {
		case *events.Message:
			// Process regular messages
			handleMessage(client, messageStore, v, logger)

		case *events.MediaRetry:
			handleMediaRetry(client, messageStore, v, logger)

		case *events.HistorySync:
			// Process history sync events
			handleHistorySync(client, messageStore, v, logger)

		case *events.Connected:
			logger.Infof("Connected to WhatsApp")
			if client.Store.ID != nil {
				scheduleResolve(logger)
				startEngineWorker(logger)
			}

		case *events.LoggedOut:
			logger.Warnf("Device logged out, please scan QR code to log in again")
			clearSyncState()
			syncReady.Store(false)
			syncSawInitial.Store(false)
			syncLastEvent.Store(0)
			syncProgress.Store(0)
			go startPairing(client, logger)
		}
	})

	startServers(client, messageStore, logger)

	if client.Store.ID == nil {
		go func() {
			waitForKey()
			waitForOnboarding()
			startPairing(client, logger)
		}()
	} else {
		if err := client.Connect(); err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}
		logger.Infof("Connected with stored session")
		startEngineWorker(logger)
	}
	go func() { time.Sleep(5 * time.Second); scheduleResolve(logger) }()
	go func() { time.Sleep(65 * time.Second); scheduleResolve(logger) }()

	// Create a channel to keep the main goroutine alive
	exitChan := make(chan os.Signal, 1)
	signal.Notify(exitChan, syscall.SIGINT, syscall.SIGTERM)

	if verboseMode {
		fmt.Println("Nembu bridge is running. Press Ctrl+C to disconnect and exit.")
	}

	// Wait for termination signal
	<-exitChan

	fmt.Println("Disconnecting...")
	stopProcess("worker.pid")
	// Disconnect client
	client.Disconnect()
}

// GetChatName determines the appropriate name for a chat based on JID and other info
func GetChatName(client *whatsmeow.Client, messageStore *MessageStore, jid types.JID, chatJID string, conversation interface{}, sender string, logger waLog.Logger) string {
	ownUser := ""
	if client != nil && client.Store != nil && client.Store.ID != nil {
		ownUser = client.Store.ID.User
	}
	// First, check if chat already exists in database with a name
	var existingName string
	err := messageStore.db.QueryRow("SELECT name FROM chats WHERE jid = ?", chatJID).Scan(&existingName)
	if err == nil {
		if clean := cleanChatName(existingName, jid, chatJID, ownUser); clean != "" {
			logger.Infof("Using existing chat name for %s: %s", chatJID, clean)
			return clean
		}
	}

	// Need to determine chat name
	var name string

	if jid.Server == "g.us" {
		// This is a group chat
		logger.Infof("Getting name for group: %s", chatJID)

		// Use conversation data if provided (from history sync)
		if conversation != nil {
			// Extract name from conversation if available
			// This uses type assertions to handle different possible types
			var displayName, convName *string
			// Try to extract the fields we care about regardless of the exact type
			v := reflect.ValueOf(conversation)
			if v.Kind() == reflect.Ptr && !v.IsNil() {
				v = v.Elem()

				// Try to find DisplayName field
				if displayNameField := v.FieldByName("DisplayName"); displayNameField.IsValid() && displayNameField.Kind() == reflect.Ptr && !displayNameField.IsNil() {
					dn := displayNameField.Elem().String()
					displayName = &dn
				}

				// Try to find Name field
				if nameField := v.FieldByName("Name"); nameField.IsValid() && nameField.Kind() == reflect.Ptr && !nameField.IsNil() {
					n := nameField.Elem().String()
					convName = &n
				}
			}

			// Use the name we found
			if displayName != nil && *displayName != "" {
				name = *displayName
			} else if convName != nil && *convName != "" {
				name = *convName
			}
		}

		// If we didn't get a name, try group info — but never pay for a network
		// call per unnamed group while history sync is still streaming.
		if name == "" {
			if !syncReady.Load() {
				name = fmt.Sprintf("Group %s", jid.User)
				queueGroupName(chatJID, jid)
			} else {
				groupInfo, err := client.GetGroupInfo(context.Background(), jid)
				if err == nil && groupInfo.Name != "" {
					name = groupInfo.Name
				} else {
					name = fmt.Sprintf("Group %s", jid.User)
				}
			}
		}

		logger.Infof("Using group name: %s", name)
		if clean := cleanChatName(name, jid, chatJID, ownUser); clean != "" {
			name = clean
		} else {
			name = fmt.Sprintf("Group %s", jid.User)
		}
	} else {
		// This is an individual contact
		logger.Infof("Getting name for contact: %s", chatJID)

		// Just use contact info (full name)
		contact, err := client.Store.Contacts.GetContact(context.Background(), jid)
		if err == nil && contact.FullName != "" {
			name = contact.FullName
		} else if sender != "" {
			// Fallback to sender
			name = sender
		}
		if clean := cleanChatName(name, jid, chatJID, ownUser); clean != "" {
			name = clean
		} else {
			name = dmLabel(chatJID)
		}

		logger.Infof("Using contact name: %s", name)
	}

	return name
}

func onlyDigits(s string) string {
	var b strings.Builder
	for _, r := range s {
		if unicode.IsDigit(r) {
			b.WriteRune(r)
		}
	}
	return b.String()
}

func punctuationOnly(s string) bool {
	for _, r := range s {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			return false
		}
	}
	return true
}

func bareNumeric(s string) bool {
	d := onlyDigits(s)
	return d != "" && d == strings.TrimSpace(s)
}

func dmLabel(chatJID string) string {
	user := strings.Split(chatJID, "@")[0]
	digits := onlyDigits(user)
	if len(digits) >= 4 {
		return "DM " + digits[len(digits)-4:]
	}
	return "DM"
}

// cleanChatName drops the me-LID, bare digits and punctuation-only names so
// the sync never persists an unusable chat label.
func cleanChatName(name string, jid types.JID, chatJID, ownUser string) string {
	n := strings.TrimSpace(name)
	if n == "" || punctuationOnly(n) || bareNumeric(n) {
		return ""
	}
	if ownUser != "" && n == ownUser {
		return ""
	}
	if jid.User != "" && n == jid.User {
		return ""
	}
	return n
}

// Handle history sync events
func handleHistorySync(client *whatsmeow.Client, messageStore *MessageStore, historySync *events.HistorySync, logger waLog.Logger) {
	fmt.Printf("Received history sync event with %d conversations\n", len(historySync.Data.Conversations))
	syncType := historySync.Data.GetSyncType()
	progress := int(historySync.Data.GetProgress())

	syncedCount := 0
	for _, conversation := range historySync.Data.Conversations {
		// Parse JID from the conversation
		if conversation.ID == nil {
			continue
		}

		chatJID := *conversation.ID

		// Try to parse the JID
		jid, err := types.ParseJID(chatJID)
		if err != nil {
			logger.Warnf("Failed to parse JID %s: %v", chatJID, err)
			continue
		}

		// Get appropriate chat name by passing the history sync conversation directly
		name := GetChatName(client, messageStore, jid, chatJID, conversation, "", logger)

		// Process messages
		messages := conversation.Messages
		if len(messages) > 0 {
			// Update chat with latest message timestamp
			latestMsg := messages[0]
			if latestMsg == nil || latestMsg.Message == nil {
				continue
			}

			// Get timestamp from message info
			timestamp := time.Time{}
			if ts := latestMsg.Message.GetMessageTimestamp(); ts != 0 {
				timestamp = time.Unix(int64(ts), 0)
			} else {
				continue
			}

			messageStore.StoreChat(chatJID, name, timestamp)

			// Store messages
			for _, msg := range messages {
				if msg == nil || msg.Message == nil {
					continue
				}

				// Extract text content
				var content string
				if msg.Message.Message != nil {
					if conv := msg.Message.Message.GetConversation(); conv != "" {
						content = conv
					} else if ext := msg.Message.Message.GetExtendedTextMessage(); ext != nil {
						content = ext.GetText()
					}
				}

				// Extract media info
				var mediaType, filename, url string
				var mediaKey, fileSHA256, fileEncSHA256 []byte
				var fileLength uint64

				if msg.Message.Message != nil {
					mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength = extractMediaInfo(msg.Message.Message)
				}

				// Log the message content for debugging
				logger.Infof("Message content: %v, Media Type: %v", content, mediaType)

				// Skip messages with no content and no media
				if content == "" && mediaType == "" {
					continue
				}

				// Determine sender
				var sender string
				isFromMe := false
				if msg.Message.Key != nil {
					if msg.Message.Key.FromMe != nil {
						isFromMe = *msg.Message.Key.FromMe
					}
					if !isFromMe && msg.Message.Key.Participant != nil && *msg.Message.Key.Participant != "" {
						sender = *msg.Message.Key.Participant
					} else if isFromMe {
						sender = client.Store.ID.User
					} else {
						sender = jid.User
					}
				} else {
					sender = jid.User
				}

				// Store message
				msgID := ""
				if msg.Message.Key != nil && msg.Message.Key.ID != nil {
					msgID = *msg.Message.Key.ID
				}

				// Get message timestamp
				timestamp := time.Time{}
				if ts := msg.Message.GetMessageTimestamp(); ts != 0 {
					timestamp = time.Unix(int64(ts), 0)
				} else {
					continue
				}

				hqID, hqSender := extractQuote(msg.Message.Message)
				err = messageStore.StoreMessage(
					msgID,
					chatJID,
					sender,
					content,
					timestamp,
					isFromMe,
					mediaType,
					filename,
					url,
					mediaKey,
					fileSHA256,
					fileEncSHA256,
					fileLength,
					hqID,
					hqSender,
				)
				if err != nil {
					logger.Warnf("Failed to store history message: %v", err)
				} else {
					syncedCount++
					if mediaType == "audio" && isMeChat(chatJID) {
						go func(id, jid string) {
							if messageStore.LocalPath(id, jid) != "" {
								return
							}
							if isMeChat(jid) {
								appendEvent("bridge", id, "media.download", "info", "history-sync arrival", 0)
							}
							ok, _, _, p, e := downloadMedia(client, messageStore, id, jid)
							if ok && e == nil && p != "" {
								messageStore.SetLocalPath(id, jid, p)
								if isMeChat(jid) {
									appendEvent("bridge", id, "media.download", "ok", p, 0)
								}
							} else {
								reason := "download failed"
								if e != nil {
									reason = e.Error()
								}
								if isMeChat(jid) {
									appendEvent("bridge", id, "media.download", "error", reason, 0)
								}
								requestMediaRetry(client, messageStore, id, jid)
							}
						}(msgID, chatJID)
					}
					// Log successful message storage
					if mediaType != "" {
						logger.Infof("Stored message: [%s] %s -> %s: [%s: %s] %s",
							timestamp.Format("2006-01-02 15:04:05"), sender, chatJID, mediaType, filename, content)
					} else {
						logger.Infof("Stored message: [%s] %s -> %s: %s",
							timestamp.Format("2006-01-02 15:04:05"), sender, chatJID, content)
					}
				}
			}
		}
	}

	fmt.Printf("History sync complete. Stored %d messages.\n", syncedCount)
	noteHistoryEvent(syncType, progress, len(historySync.Data.Conversations), syncedCount)
	scheduleResolve(logger)
}

// Request history sync from the server
func requestHistorySync(client *whatsmeow.Client) {
	if client == nil {
		fmt.Println("Client is not initialized. Cannot request history sync.")
		return
	}

	if !client.IsConnected() {
		fmt.Println("Client is not connected. Please ensure you are connected to WhatsApp first.")
		return
	}

	if client.Store.ID == nil {
		fmt.Println("Client is not logged in. Please scan the QR code first.")
		return
	}

	// Build and send a history sync request
	historyMsg := client.BuildHistorySyncRequest(nil, 100)
	if historyMsg == nil {
		fmt.Println("Failed to build history sync request.")
		return
	}

	_, err := client.SendMessage(context.Background(), types.JID{
		Server: "s.whatsapp.net",
		User:   "status",
	}, historyMsg)

	if err != nil {
		fmt.Printf("Failed to request history sync: %v\n", err)
	} else {
		fmt.Println("History sync requested. Waiting for server response...")
	}
}

// analyzeOggOpus tries to extract duration and generate a simple waveform from an Ogg Opus file
func analyzeOggOpus(data []byte) (duration uint32, waveform []byte, err error) {
	// Try to detect if this is a valid Ogg file by checking for the "OggS" signature
	// at the beginning of the file
	if len(data) < 4 || string(data[0:4]) != "OggS" {
		return 0, nil, fmt.Errorf("not a valid Ogg file (missing OggS signature)")
	}

	// Parse Ogg pages to find the last page with a valid granule position
	var lastGranule uint64
	var sampleRate uint32 = 48000 // Default Opus sample rate
	var preSkip uint16 = 0
	var foundOpusHead bool

	// Scan through the file looking for Ogg pages
	for i := 0; i < len(data); {
		// Check if we have enough data to read Ogg page header
		if i+27 >= len(data) {
			break
		}

		// Verify Ogg page signature
		if string(data[i:i+4]) != "OggS" {
			// Skip until next potential page
			i++
			continue
		}

		// Extract header fields
		granulePos := binary.LittleEndian.Uint64(data[i+6 : i+14])
		pageSeqNum := binary.LittleEndian.Uint32(data[i+18 : i+22])
		numSegments := int(data[i+26])

		// Extract segment table
		if i+27+numSegments >= len(data) {
			break
		}
		segmentTable := data[i+27 : i+27+numSegments]

		// Calculate page size
		pageSize := 27 + numSegments
		for _, segLen := range segmentTable {
			pageSize += int(segLen)
		}

		// Check if we're looking at an OpusHead packet (should be in first few pages)
		if !foundOpusHead && pageSeqNum <= 1 {
			// Look for "OpusHead" marker in this page
			pageData := data[i : i+pageSize]
			headPos := bytes.Index(pageData, []byte("OpusHead"))
			if headPos >= 0 && headPos+12 < len(pageData) {
				// Found OpusHead, extract sample rate and pre-skip
				// OpusHead format: Magic(8) + Version(1) + Channels(1) + PreSkip(2) + SampleRate(4) + ...
				headPos += 8 // Skip "OpusHead" marker
				// PreSkip is 2 bytes at offset 10
				if headPos+12 <= len(pageData) {
					preSkip = binary.LittleEndian.Uint16(pageData[headPos+10 : headPos+12])
					sampleRate = binary.LittleEndian.Uint32(pageData[headPos+12 : headPos+16])
					foundOpusHead = true
					fmt.Printf("Found OpusHead: sampleRate=%d, preSkip=%d\n", sampleRate, preSkip)
				}
			}
		}

		// Keep track of last valid granule position
		if granulePos != 0 {
			lastGranule = granulePos
		}

		// Move to next page
		i += pageSize
	}

	if !foundOpusHead {
		fmt.Println("Warning: OpusHead not found, using default values")
	}

	// Calculate duration based on granule position
	if lastGranule > 0 {
		// Formula for duration: (lastGranule - preSkip) / sampleRate
		durationSeconds := float64(lastGranule-uint64(preSkip)) / float64(sampleRate)
		duration = uint32(math.Ceil(durationSeconds))
		fmt.Printf("Calculated Opus duration from granule: %f seconds (lastGranule=%d)\n",
			durationSeconds, lastGranule)
	} else {
		// Fallback to rough estimation if granule position not found
		fmt.Println("Warning: No valid granule position found, using estimation")
		durationEstimate := float64(len(data)) / 2000.0 // Very rough approximation
		duration = uint32(durationEstimate)
	}

	// Make sure we have a reasonable duration (at least 1 second, at most 300 seconds)
	if duration < 1 {
		duration = 1
	} else if duration > 300 {
		duration = 300
	}

	// Generate waveform
	waveform = placeholderWaveform(duration)

	fmt.Printf("Ogg Opus analysis: size=%d bytes, calculated duration=%d sec, waveform=%d bytes\n",
		len(data), duration, len(waveform))

	return duration, waveform, nil
}

// min returns the smaller of x or y
func min(x, y int) int {
	if x < y {
		return x
	}
	return y
}

// placeholderWaveform generates a synthetic waveform for WhatsApp voice messages
// that appears natural with some variability based on the duration
func placeholderWaveform(duration uint32) []byte {
	// WhatsApp expects a 64-byte waveform for voice messages
	const waveformLength = 64
	waveform := make([]byte, waveformLength)

	// Seed the random number generator for consistent results with the same duration
	rand.Seed(int64(duration))

	// Create a more natural looking waveform with some patterns and variability
	// rather than completely random values

	// Base amplitude and frequency - longer messages get faster frequency
	baseAmplitude := 35.0
	frequencyFactor := float64(min(int(duration), 120)) / 30.0

	for i := range waveform {
		// Position in the waveform (normalized 0-1)
		pos := float64(i) / float64(waveformLength)

		// Create a wave pattern with some randomness
		// Use multiple sine waves of different frequencies for more natural look
		val := baseAmplitude * math.Sin(pos*math.Pi*frequencyFactor*8)
		val += (baseAmplitude / 2) * math.Sin(pos*math.Pi*frequencyFactor*16)

		// Add some randomness to make it look more natural
		val += (rand.Float64() - 0.5) * 15

		// Add some fade-in and fade-out effects
		fadeInOut := math.Sin(pos * math.Pi)
		val = val * (0.7 + 0.3*fadeInOut)

		// Center around 50 (typical voice baseline)
		val = val + 50

		// Ensure values stay within WhatsApp's expected range (0-100)
		if val < 0 {
			val = 0
		} else if val > 100 {
			val = 100
		}

		waveform[i] = byte(val)
	}

	return waveform
}
