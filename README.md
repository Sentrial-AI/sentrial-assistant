# Sentrial

Personal assistant for Liam. Cloud-native (Railway) with an installable PWA, approval-gated autonomous jobs, and Notion/Gmail/Calendar MCPs.

---

## Architecture

```
                    iPhone PWA                      Desktop PWA
                         │                                │
                         └────────────────┬───────────────┘
                                          ▼
                                  Railway (HTTPS)
                                          │
                           ┌──────────────┴───────────────┐
                           ▼                              ▼
                    FastAPI server                  Agent (Opus)
                    /inbound /approve               │
                    /api/state /api/push            ├─ Notion MCP
                                                    ├─ Creative MCP (autonomous)
                                                    ├─ Gmail MCP (phase 2)
                                                    └─ Calendar MCP (phase 2)
                           │
                           ▼
                    Railway Volume (/data)
                    • audit.sqlite
                    • memory.sqlite
                    • jobs/*.json
                    • deliverables/
```

One backend. The PWA works on phone and laptop. iOS Shortcut can hit `/inbound` too. Web Push tells Liam when autonomous jobs finish, even when the app is closed.

## Components

- `sentrial/core/` — agent, task runner (autonomous workflow engine), memory, audit, secrets, tier gate
- `sentrial/mcps/` — capability modules (notion, creative, gmail, calendar, sentrial_pipeline)
- `sentrial/inputs/webhook.py` — HTTP server; serves the PWA and API
- `sentrial/ui/` — the PWA (single-page, installable)
- `sentrial/outputs/notify.py` — web push → Pushover → iMessage cascade

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m sentrial.core.daemon setup    # Mac only — Keychain prompts
python -m sentrial.core.daemon run
# → http://127.0.0.1:8765/ui
```

## Deploying to Railway

See [DEPLOY.md](DEPLOY.md).

## Trust model

- No stored passwords. Env vars + macOS Keychain only.
- Every tool call is tiered (read / draft / send / irreversible) with a confirmation gate.
- Every action hits the append-only audit log.
- Per-capability kill switch in Settings.
