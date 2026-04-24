# Sentrial — Personal Assistant

A Jarvis-style personal assistant for Liam, built on the Claude Agent SDK with MCP-based capabilities. Runs as a launchd daemon on macOS. One brain, many hands.

---

## Goals

1. **Ridiculously intelligent** — Opus-powered, long-context, persistent memory, skill orchestration.
2. **Trustable** — no stored passwords, Keychain-only secrets, tiered confirmation gates, append-only audit log.
3. **Useful for Sentrial and Pursuit Visuals** — not an extra step; replaces the "which script do I run next" problem.
4. **Asynchronous-native** — text it a big job, it scopes, you approve, it runs 5–10 min in the background and texts back a deliverable.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                         INPUT LAYER                            │
│  menubar hotkey (Wispr Flow)    iOS Shortcut → webhook         │
│       email-to-self             (later) Telegram/iMessage      │
└──────────────────────────┬─────────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────────┐
│                          BRAIN                                 │
│   Claude Agent SDK (Opus) + system prompt + memory store       │
│   Intent router: conversational  vs.  long-running job         │
└──────────────────────────┬─────────────────────────────────────┘
                           │
         ┌─────────────────┼──────────────────┐
         ▼                                    ▼
┌──────────────────┐                 ┌──────────────────────┐
│   TOOL LAYER     │                 │    TASK RUNNER       │
│   (MCP servers)  │                 │  (async job queue)   │
│                  │                 │                      │
│ • Reminders      │                 │ scope → approve →    │
│ • Gmail          │                 │ dispatch sub-agent → │
│ • Calendar       │                 │ notify on complete   │
│ • Sentrial       │                 │                      │
│   pipeline       │                 │ Used for: proposals, │
│ • Pursuit        │                 │ audits, demo sites,  │
│ • Creative       │◄────────────────┤ demo features        │
│   workflow       │                 │                      │
│ • Filesystem     │                 │                      │
│ • Shell          │                 │                      │
└────────┬─────────┘                 └──────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────────────────┐
│                      SECURITY LAYER                            │
│  Keychain (secrets)  •  Tier gate (read/draft/send/irrev)      │
│  Audit log (append-only SQLite)  •  Per-capability kill switch │
└────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────────┐
│                       OUTPUT LAYER                             │
│  menubar popup reply    iMessage (osascript)    Pushover       │
│  file deliverables (computer:// links)          email          │
└────────────────────────────────────────────────────────────────┘
```

---

## V1 Scope (week 1)

1. **Reminders + scheduling** — Apple Reminders via osascript, cron-style recurring tasks, morning/evening briefs.
2. **Gmail read + draft** — OAuth, read/search inbox, summarize threads, draft replies (no auto-send in v1).
3. **Calendar read + create** — Google Calendar, see schedule, create events, find open slots.
4. **Sentrial pipeline orchestrator** — wraps existing `linkedin-scrape`, `enrich-leads`, `export-leads`, `follow-up` skills.
5. **Creative autonomous workflow** — the flagship. SMS-gated async jobs:
   - Audit a company
   - Build a demo site
   - Build a demo feature
   - Create a proposal (uses existing `proposal` skill with pastel aesthetic)

## V2 Scope (week 2–3)

- Pursuit Visuals integration (client comms tracking + content scheduling)
- Filesystem MCP (scoped project folders)
- Shell MCP (dev/git, sandboxed)
- Slack, Notion, GitHub MCPs
- Browser MCP (Claude in Chrome bridge)

## V3+ (ongoing)

- More integrations on demand
- Voice output (TTS)
- Mobile app or richer iOS integration
- Multi-project coordination (BuildLog/Pynacle aware)

---

## Trust Model

**No passwords stored, ever.** OAuth tokens only. Everything in macOS Keychain via the `security` CLI. No plaintext secrets on disk, not even in config files.

**Action tiers** — every tool call is classified before execution:

| Tier | Scope | Gate |
|------|-------|------|
| 0 Read | List emails, read calendar, search files | None |
| 1 Draft | Save draft, create file in scratchpad, build proposal (not delivered) | None |
| 2 Send / Spend / Delete | Send email, create calendar event, delete reminder, post message | Explicit confirmation |
| 3 Irreversible | Wire transfer, `rm -rf`, send to press list, DNS change | Biometric or typed password |

**Audit log** — every tool call (tier 0+), every confirmation, every approval, every skill invocation appends to `~/Library/Application Support/Sentrial/audit.sqlite`. Read-only CLI to review: `sentrial audit --since yesterday`.

**Per-capability kill switch** — each MCP can be disabled in the menubar menu. Disables propagate immediately.

---

## Approval-Gated Autonomous Workflow

The flagship UX pattern. When Liam texts a request that maps to a long-running skill:

1. **Intake** — assistant parses intent, identifies skill (proposal / audit / demo-site / demo-feature / pipeline).
2. **Scope preview** — assistant texts back a concise summary of what it will produce ("I'll build a proposal for [company] using the transcript you shared. It'll include sections X, Y, Z, and use the pastel aesthetic. ETA ~10 min. Approve?").
3. **Approval gate** — Liam replies: `yes` / `no` / `change: [instructions]`.
4. **Dispatch** — on yes, the task_runner spawns a sub-agent (Claude Agent SDK, same codebase) with the relevant skill loaded. Runs in background.
5. **Status ping at 10 min** — if still running past 10 min, send a "still working on X" update.
6. **Delivery** — when the sub-agent completes, the output is saved to the user's workspace folder. Assistant texts a link: `[computer://...]`.
7. **Audit** — full skill invocation + sub-agent trace logged.

Concurrency: task runner supports N=3 parallel jobs by default (configurable). FIFO queue beyond that.

---

## Input Channels

**Primary: menubar hotkey.** `⌥+Space` (user-configurable) pops a small input box. Liam triggers Wispr Flow, dictates, hits enter. Response appears in the same popup or as a macOS notification.

**Secondary: iOS Shortcut → webhook.** The daemon exposes a localhost HTTP server; Tailscale makes it reachable from his phone. A Shortcut accepts dictated text, POSTs to the webhook with a shared secret. Response comes back as iMessage/SMS.

**Tertiary: email-to-self.** Liam forwards/sends emails to a designated address. Daemon polls, processes, replies via the thread. Good for async "here's context, handle this later."

**Future:** Telegram bot, iMessage full bridge, Discord.

---

## Output Channels

**Menubar popup** — primary for in-person use.
**iMessage** — via `osascript` sending to Liam's own number. Primary for async approval-gated workflows.
**Pushover** — fallback if iMessage fails (Mac asleep, etc.). Also used for scheduled pings (morning brief).
**Email** — lowest priority, used when the input was email.
**File delivery** — deliverables saved to workspace folder, surfaced as `computer://` links.

---

## Tech Stack

- **Language:** Python 3.11+ (Claude Agent SDK is Python-native).
- **Agent framework:** `claude-agent-sdk`.
- **MCP framework:** `mcp` (official).
- **Menubar:** `rumps` (simple) or Swift wrapper later.
- **HTTP:** `fastapi` + `uvicorn` for the webhook.
- **Persistence:** SQLite (stdlib) for memory + audit; `pydantic` for models.
- **Secrets:** macOS `security` CLI wrapped in Python.
- **Process:** `launchd` plist, auto-start on login.
- **Scheduler:** APScheduler or a thin cron wrapper.

---

## Directory Layout

```
Sentrial Assistant/
├── SPEC.md                          # this file
├── pyproject.toml
├── sentrial/
│   ├── __init__.py
│   ├── core/
│   │   ├── agent.py                 # Claude Agent SDK wiring
│   │   ├── memory.py                # SQLite memory store
│   │   ├── audit.py                 # append-only action log
│   │   ├── secrets.py               # Keychain wrapper
│   │   ├── confirmation.py          # tier-based action gate
│   │   ├── task_runner.py           # async job queue (autonomous workflow)
│   │   └── daemon.py                # launchd entrypoint
│   ├── inputs/
│   │   ├── menubar.py               # menubar hotkey + popup
│   │   └── webhook.py               # HTTP server for iOS Shortcut
│   ├── outputs/
│   │   └── notify.py                # iMessage + Pushover + email
│   ├── mcps/
│   │   ├── reminders/server.py
│   │   ├── gmail/server.py
│   │   ├── calendar/server.py
│   │   ├── sentrial_pipeline/server.py
│   │   ├── pursuit/server.py
│   │   └── creative/server.py       # proposal/audit/demo autonomous
│   └── config/
│       ├── settings.example.toml
│       └── system_prompt.md
└── scripts/
    ├── com.sentrial.daemon.plist
    └── install.sh
```

---

## First-run Setup

```bash
cd "Sentrial Assistant"
./scripts/install.sh
# Walks through:
#  1. Install Python deps (uv or pip)
#  2. Prompt for Anthropic API key → Keychain
#  3. Prompt for Google OAuth (Gmail + Calendar) → Keychain
#  4. Prompt for Pushover creds (optional) → Keychain
#  5. Register launchd plist → daemon auto-starts on login
#  6. Install menubar app
#  7. Smoke test: menubar → reminders MCP → confirm write
```

---

## What this assistant explicitly will NOT do

- Store passwords.
- Execute trades or move money (follows the computer-use financial-actions rule).
- Send emails without confirmation in v1.
- Run `rm -rf` on workspace folders without tier-3 gate.
- Persist PII beyond what's already in your Gmail/Calendar/etc. (memory is scoped to project/preference facts, not sensitive personal info).
