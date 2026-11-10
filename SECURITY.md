# Security notes

This is a local-first alpha.

- The WhatsApp session and message history live under `~/.nembu/store/` (legacy `~/.mosaic/store/` fallback) on this machine.
- The Gemini API key lives in `~/.nembu/.env` (mode `0600`), created by the first-run TUI.
- The bridge's send API is bound to loopback and uses a per-run token in `store/bridge_token`.
- Nembu sends only the message text needed for a request to the Gemini API.
- No Nembu telemetry or central database is used.

Do not share `.env`, `store/`, or the contents of this folder after pairing.

This project uses an unofficial WhatsApp protocol client. That carries account-level risk. Use a test number if possible.
