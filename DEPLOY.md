# Deploying Sentrial to Railway

This is the one-path walkthrough from "I have a Railway account" to "I'm using Sentrial on my iPhone home screen."

---

## Prerequisites

- A Railway account
- Your existing Sentrial AI GitHub repo (or create one and push this code there)
- An Anthropic API key
- A Notion integration token + tasks database

## 1. Push the code to GitHub

From the repo root:

```bash
git init   # if not already a repo
git add -A
git commit -m "sentrial: v1 cloud"
git remote add origin git@github.com:<you>/<your-sentrial-repo>.git
git push -u origin main
```

## 2. Create a Railway service

1. railway.app → New Project → Deploy from GitHub repo → pick this repo.
2. Railway will auto-detect the `Dockerfile` and start building.

## 3. Add a persistent volume

Without a volume, all state (audit log, memory, jobs, deliverables) is lost on redeploy.

- Service → Settings → Volumes → **New Volume**
- Mount path: `/data`
- Size: 1 GB is plenty for v1

The Dockerfile already sets `SENTRIAL_DATA_DIR=/data` so SQLite / jobs / deliverables all land there automatically.

## 4. Set environment variables

Service → Variables. Paste each one:

| Variable                | Required | Notes                                                            |
|-------------------------|----------|------------------------------------------------------------------|
| `ANTHROPIC_API_KEY`     | yes      | From console.anthropic.com                                       |
| `SENTRIAL_TOKEN`        | yes      | Generate: `python -m sentrial.core.daemon gen-token` (locally)   |
| `NOTION_API_KEY`        | yes*     | Your Notion integration token                                    |
| `NOTION_TASKS_DB_ID`    | yes*     | UUID of your tasks database                                      |
| `VAPID_PUBLIC_KEY`      | no       | For web push — run `gen-vapid` locally                           |
| `VAPID_PRIVATE_KEY`     | no       | Multi-line PEM — paste the full block                            |
| `VAPID_CONTACT`         | no       | `mailto:you@example.com`                                         |
| `PUSHOVER_TOKEN`        | no       | Backup notification channel                                      |
| `PUSHOVER_USER`         | no       |                                                                  |

\* Notion is technically optional (Sentrial starts without it) but nothing task-related will work.

Generate the tokens locally first:

```bash
# In the project dir with your venv activated
python -m sentrial.core.daemon gen-token     # copy to SENTRIAL_TOKEN
python -m sentrial.core.daemon gen-vapid     # copy both VAPID_* values
```

## 5. Set up Notion

1. Go to https://www.notion.so/profile/integrations → **New integration**.
2. Name it "Sentrial", grant it "Read", "Update", "Insert" content permissions.
3. Copy the **Internal Integration Token** → set as `NOTION_API_KEY` on Railway.
4. In Notion, create (or pick) a database for tasks. It needs properties:
   - `Name` (title)
   - `Status` (status type, with at minimum `To do` / `In progress` / `Done`)
   - `Due` (date, optional)
5. Click the "•••" on the database → **Connect to** → select your Sentrial integration.
6. Copy the database ID from its URL. The URL looks like `notion.so/<workspace>/<db-name>-<UUID>?v=...`. The UUID is a 32-char hex string. Set it as `NOTION_TASKS_DB_ID`.

## 6. Get a public URL

Service → Settings → Networking → **Generate Domain**. You'll get something like `sentrial-production-a7b2.up.railway.app`.

Wait for the deploy to go green (Deployments tab).

Smoke check: `https://<your-domain>/health` should return `{"ok": true, ...}`.

## 7. Pair the PWA

Open `https://<your-domain>/ui/` on your phone or laptop.

It'll show a "Pair this device" modal. Paste your `SENTRIAL_TOKEN`. The token is saved to localStorage — future visits don't ask.

### Install to iPhone home screen

1. Open the URL in **Safari** (not Chrome — iOS push only works from Safari-installed PWAs).
2. Share sheet → **Add to Home Screen**.
3. Tap the Sentrial icon. It opens in standalone mode, no browser chrome.
4. Settings tab → **Enable push**. Grant permission.

### Desktop

Same URL. Chrome / Edge: address bar install icon → "Install Sentrial". Safari: Dock → ••• → Add to Dock.

## 8. First real test

From your phone (walking to a meeting, etc.):

```
Open Sentrial → "Build a proposal for Acme using this transcript: [paste]"
```

You should see:
1. Sentrial types a typing indicator, then replies with a **scope preview** ("Proposal for Acme — 5 sections, pastel aesthetic, ETA 8 min. Approve?")
2. Jobs tab shows the pending job with Approve / Deny buttons
3. Tap Approve — job flips to Running
4. 5–10 min later, your phone gets a push notification: "Done: proposal — [link]"
5. Jobs tab shows the completed job with the deliverable path

The file lives in the Railway volume at `/data/deliverables/<job-id>/`. Right now that's not directly downloadable from your phone — phase 2 adds a `/deliverables/<id>` endpoint to stream the file through the server. For the first deploy, `railway volume` CLI or a terminal-in-Railway is how you fetch it.

## What's NOT included in this first deploy

- **Gmail / Calendar MCPs** — OAuth flows pending. Set up is 15 min of work each.
- **Sentrial pipeline MCP** — the `linkedin-scrape` / `enrich-leads` scripts live on your Mac. We need to port them to cloud or keep them local and use a relay.
- **Confirmation UI** — tier-2 actions currently auto-approve (v1 stub). Before turning on Gmail send or anything with real blast radius, this gets replaced with a real approve-in-PWA dialog.

## Troubleshooting

**PWA stuck on "Pair this device" after pasting token**
- Check that `SENTRIAL_TOKEN` on Railway exactly matches what you're pasting.
- Hit `/health` — if that's 200, the server's up.

**Push notifications don't arrive**
- Confirm `VAPID_PUBLIC_KEY` + `VAPID_PRIVATE_KEY` are set on Railway.
- iOS only supports web push from home-screen-installed PWAs, *and* iOS 16.4+.
- Disable and re-enable push in Settings to refresh subscription.

**Jobs fail with "no such skill"**
- The creative MCP's sub-agent dispatcher expects the `claude` CLI to be available. On Railway it isn't — phase 2 swaps the dispatcher for an in-process Anthropic SDK call. For the first deploy, jobs that don't need an external skill runner (conversation-only answers) work fine; autonomous proposal/audit will need the follow-up PR.

**Server won't start**
- Check Railway deploy logs. Common: `Missing secret 'anthropic_api_key'` → env var name is case-insensitive in code, make sure the value is set, not just the key.
