"""
HTTP server — the single entry point for PWA, iOS Shortcut, and remote API.

Endpoints:
  GET  /                    → redirect to /ui
  GET  /ui                  → PWA (index.html)
  GET  /ui/*                → PWA static assets
  GET  /manifest.json       → PWA manifest
  GET  /sw.js               → service worker
  GET  /icon.svg            → app icon

  POST /inbound             → user message → agent turn
  POST /approve             → approve/deny a pending job
  GET  /api/state           → dashboard data (jobs, audit, mcps, pins, stats)
  POST /api/push/subscribe  → register a web-push subscription
  POST /api/push/unsubscribe
  GET  /api/push/vapid      → public VAPID key for the client

  GET  /health              → liveness probe

Auth: Bearer token in Authorization header. Token is SENTRIAL_TOKEN env var (cloud)
or auto-generated into Keychain on first Mac boot. PWA bootstraps via /ui/bootstrap
when reaching the server from 127.0.0.1 (local dev only).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sentrial.core import audit, memory, paths
from sentrial.core import secrets as kc

if TYPE_CHECKING:
    from sentrial.core.agent import Agent
    from sentrial.core.task_runner import TaskRunner

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).parent.parent / "ui"
BOOT_TIME = time.time()


# -------------------- models --------------------

class Inbound(BaseModel):
    text: str
    channel: str = "pwa"
    conversation_id: str | None = None


class ApproveBody(BaseModel):
    job_id: str
    approve: bool = True


class PushSub(BaseModel):
    endpoint: str
    keys: dict


class PushUnsub(BaseModel):
    endpoint: str


# -------------------- auth --------------------

def _verify_token(auth_header: str, x_sentrial_token: str) -> None:
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    token = token or x_sentrial_token
    expected = kc.ensure_token()
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


async def require_auth(
    authorization: str = Header(default=""),
    x_sentrial_token: str = Header(default=""),
) -> None:
    _verify_token(authorization, x_sentrial_token)


# -------------------- app factory --------------------

def build_app(
    task_runner: "TaskRunner | None" = None,
    agent: "Agent | None" = None,
    registry=None,
) -> FastAPI:
    api = FastAPI(title="Sentrial")

    if UI_DIR.is_dir():
        api.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")

    # ---- public (no auth) ----

    @api.get("/health")
    async def health():
        return {"ok": True, "uptime": int(time.time() - BOOT_TIME)}

    @api.get("/")
    async def root():
        return RedirectResponse("/ui/", status_code=307)

    @api.get("/manifest.json")
    async def manifest():
        p = UI_DIR / "manifest.json"
        return FileResponse(p) if p.exists() else JSONResponse({"error": "no manifest"}, 404)

    @api.get("/sw.js")
    async def sw():
        p = UI_DIR / "sw.js"
        if not p.exists():
            return JSONResponse({"error": "no sw"}, 404)
        r = FileResponse(p, media_type="application/javascript")
        r.headers["Service-Worker-Allowed"] = "/"
        return r

    @api.get("/icon.svg")
    async def icon():
        p = UI_DIR / "icon.svg"
        return FileResponse(p, media_type="image/svg+xml") if p.exists() else JSONResponse({}, 404)

    # Token bootstrap — local-only convenience so Mac dev doesn't need to paste tokens.
    @api.get("/ui/bootstrap")
    async def bootstrap(request: Request):
        client = request.client
        if not client or client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(status_code=403, detail="bootstrap only allowed from localhost")
        return {"token": kc.ensure_token()}

    @api.get("/api/voice/config", dependencies=[Depends(require_auth)])
    async def voice_config():
        """Voice settings the PWA reads at voice-mode start. Override the
        Aura voice via the `sentrial_voice` keychain entry; default is a
        deeper, more butler-toned voice rather than Orion's snappier read."""
        # Default: Aura-1 helios — confirmed UK male, "M"/MI6 energy.
        # NB: aura-2-helios-en (the Aura-2 namespace) doesn't exist — Deepgram
        # returns 400 — and Aura-2's catalogue is currently US-only despite
        # the Greek/Roman naming. Aura-1 is where the real British accent
        # lives. Client-side localStorage override takes precedence; this
        # is just the fresh-device fallback.
        voice = kc.get("sentrial_voice") or "aura-helios-en"
        return {"voice": voice}

    @api.get("/api/voice/greeting", dependencies=[Depends(require_auth)])
    async def voice_greeting():
        """Short opening line spoken when voice mode opens. Reads the
        prefetch cache to decide between a casual hi and a brief urgency
        callout. Kept SHORT — never more than ~12 words — because the user
        wanted "Sir." or "Yeah?" energy, not a status report."""
        import random
        from datetime import datetime, timezone, timedelta

        try:
            from sentrial.core import context_cache, context_prefetch
            c = context_cache.cache()
            todos = c.get_fresh(context_prefetch.KEY_TODOS)
            cal = c.get_fresh(context_prefetch.KEY_CALENDAR_TODAY)
        except Exception:  # noqa: BLE001
            todos, cal = None, None

        # Count "urgent" items: todos due today/overdue, calendar within 60min.
        now = datetime.now(timezone.utc)
        soon = now + timedelta(minutes=60)
        end_of_today = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )

        urgent_n = 0
        if todos and isinstance(todos.value, list):
            for t in todos.value:
                if not isinstance(t, dict):
                    continue
                due = t.get("due") or t.get("due_date")
                if not due:
                    continue
                try:
                    s = str(due).replace("Z", "+00:00")
                    d = datetime.fromisoformat(s)
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=timezone.utc)
                    if d < end_of_today:
                        urgent_n += 1
                except Exception:  # noqa: BLE001
                    continue

        imminent_event = None
        if cal and isinstance(cal.value, list):
            for ev in cal.value:
                if not isinstance(ev, dict):
                    continue
                start = ev.get("start") or (ev.get("when") or {}).get("start")
                if isinstance(start, dict):
                    start = start.get("dateTime") or start.get("date")
                if not start:
                    continue
                try:
                    s = str(start).replace("Z", "+00:00")
                    d = datetime.fromisoformat(s)
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=timezone.utc)
                    if now <= d <= soon:
                        imminent_event = (ev.get("summary") or ev.get("title") or "")[:50]
                        break
                except Exception:  # noqa: BLE001
                    continue

        if imminent_event:
            return {"text": f"Sir — {imminent_event} starts soon."}
        if urgent_n >= 3:
            return {"text": f"Sir, {urgent_n} items due today."}
        if urgent_n == 2:
            return {"text": "Sir, two items due today."}
        if urgent_n == 1:
            return {"text": "Sir — one thing's due today."}

        # Casual ack — vary so it doesn't feel canned.
        casual = [
            "Sir.",
            "Yeah?",
            "Mm.",
            "Sir, what's up?",
            "What can I do?",
            "Yes sir.",
            "Hey, what's up?",
            "Mhm.",
        ]
        return {"text": random.choice(casual)}

    @api.post("/api/voice/prewarm", dependencies=[Depends(require_auth)])
    async def voice_prewarm():
        """Fire all context prefetchers in parallel and return a snapshot of
        what landed in the cache. Called by the PWA at voice-mode start so
        the cache is hot by the time the user finishes their first sentence.

        Safe to call as often as you like — each prefetcher no-ops when its
        underlying integration isn't configured."""
        try:
            from sentrial.core import context_prefetch
            return await context_prefetch.prewarm_all()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200]}

    @api.get("/api/voice/diag")
    async def voice_diag():
        """No-auth diagnostic endpoint that lets ME (and any operator) check
        the voice path WITHOUT the Sentrial Bearer token. Returns ONLY
        sanitized status flags — no key values, no token, no PII. Safe to
        expose because:
          - Doesn't return the Deepgram key
          - Doesn't return the Sentrial Bearer token
          - Only HTTP status codes + key length / prefix-hash info
        Used to settle "is the key the problem?" without needing terminal
        access to the user's keychain or Railway shell."""
        import sys, httpx, hashlib
        out: dict = {
            "deepgram_key_present": False,
            "deepgram_key_source": None,
            "deepgram_key_length": 0,
            "deepgram_key_had_whitespace": False,
            "deepgram_key_prefix_hash": None,
            "deepgram_test_status": None,
            "deepgram_test_error": None,
            "deepgram_test_body_preview": None,
            "sentrial_token_present": False,
            "sentrial_token_source": None,
            "platform": "linux" if not sys.platform.startswith("darwin") else "darwin",
            "railway": bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID")),
        }

        # Locate Deepgram key (same waterfall as the real endpoint)
        for source, k in [
            ("kc:nova3_api_key", kc.get("nova3_api_key")),
            ("kc:deepgram_api_key", kc.get("deepgram_api_key")),
            ("env:NOVA3_API_KEY", os.environ.get("NOVA3_API_KEY")),
            ("env:DEEPGRAM_API_KEY", os.environ.get("DEEPGRAM_API_KEY")),
        ]:
            if k:
                out["deepgram_key_present"] = True
                out["deepgram_key_source"] = source
                raw_len = len(k)
                k_clean = k.strip().strip('"').strip("'")
                out["deepgram_key_length"] = len(k_clean)
                out["deepgram_key_had_whitespace"] = (raw_len != len(k_clean))
                # Hash the prefix (so reload-with-same-key vs different-key is
                # detectable without exposing the value).
                out["deepgram_key_prefix_hash"] = hashlib.sha256(
                    k_clean[:8].encode()
                ).hexdigest()[:12]
                # Test it
                try:
                    async with httpx.AsyncClient(timeout=8) as client:
                        r = await client.get(
                            "https://api.deepgram.com/v1/projects",
                            headers={"Authorization": f"Token {k_clean}"},
                        )
                    out["deepgram_test_status"] = r.status_code
                    if r.status_code != 200:
                        out["deepgram_test_body_preview"] = r.text[:240]
                except Exception as e:  # noqa: BLE001
                    out["deepgram_test_error"] = str(e)[:200]
                break

        # Sentrial token presence (don't return value)
        for source, t in [
            ("kc:sentrial_token", kc.get("sentrial_token")),
            ("kc:webhook_shared_secret", kc.get("webhook_shared_secret")),
            ("env:SENTRIAL_TOKEN", os.environ.get("SENTRIAL_TOKEN")),
            ("env:WEBHOOK_SHARED_SECRET", os.environ.get("WEBHOOK_SHARED_SECRET")),
        ]:
            if t:
                out["sentrial_token_present"] = True
                out["sentrial_token_source"] = source
                break

        return out

    @api.get("/api/voice/test_key", dependencies=[Depends(require_auth)])
    async def voice_test_key():
        """Definitive check: does our Deepgram key actually authenticate?
        Hits Deepgram's /v1/projects endpoint (cheap authed read) and reports
        the HTTP status. 200 = key works (so 1006 close is something else);
        401/403 = key is bad / billing issue (the actual cause)."""
        import httpx
        k = (
            kc.get("nova3_api_key")
            or kc.get("deepgram_api_key")
            or os.environ.get("NOVA3_API_KEY")
            or os.environ.get("DEEPGRAM_API_KEY")
        )
        if not k:
            return {"ok": False, "error": "no key configured anywhere"}
        raw_len = len(k)
        k = k.strip().strip('"').strip("'")
        had_whitespace = (raw_len != len(k))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://api.deepgram.com/v1/projects",
                    headers={"Authorization": f"Token {k}"},
                )
            return {
                "ok": r.status_code == 200,
                "status": r.status_code,
                "key_prefix": k[:6] + "..." + k[-2:] if len(k) >= 8 else "<short>",
                "key_length": len(k),
                "had_whitespace": had_whitespace,
                "body": r.text[:300] if r.status_code != 200 else "ok",
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200]}

    @api.get("/api/voice/deepgram_key", dependencies=[Depends(require_auth)])
    async def deepgram_key():
        """
        Hand the Deepgram listen API key to the authed browser so it can open
        a WebSocket from the WKWebView directly. Avoids routing PCM through
        our server and sidesteps the macOS TCC problem for bundled Python.
        The browser is authenticated so this is scoped to the owner.
        """
        k = (
            kc.get("nova3_api_key")
            or kc.get("deepgram_api_key")
            or os.environ.get("NOVA3_API_KEY")
            or os.environ.get("DEEPGRAM_API_KEY")
        )
        if not k:
            raise HTTPException(status_code=503, detail="no deepgram key configured")
        # Strip whitespace + quotes that often sneak in when pasting from
        # terminals or Railway's env var UI. Deepgram rejects auth subprotocols
        # with a trailing \n / surrounding quotes, with a confusing "connection
        # error" rather than a structured 4001.
        k = k.strip().strip('"').strip("'")
        return {"key": k}

    @api.get("/api/push/vapid")
    async def vapid_public():
        key = kc.get("vapid_public_key")
        if not key:
            return {"key": None, "hint": "set VAPID_PUBLIC_KEY env var to enable web push"}
        return {"key": key}

    # ---- authed ----

    @api.post("/inbound", dependencies=[Depends(require_auth)])
    async def inbound(body: Inbound):
        audit.log(
            "user", "inbound", 0,
            args={"channel": body.channel, "conv_id": body.conversation_id},
            result=body.text[:300],
        )
        if agent is None:
            return {"ok": True, "received": body.text, "reply": None}
        reply = await agent.turn(body.text, channel=body.channel, conversation_id=body.conversation_id)
        return {"ok": True, "reply": reply}

    @api.post("/inbound_stream", dependencies=[Depends(require_auth)])
    async def inbound_stream(body: Inbound):
        """Streaming variant of /inbound. Returns SSE events as the model
        generates so the UI can start TTS on the first sentence rather than
        waiting for the whole reply. Each event is one JSON object on a single
        `data:` line per the SSE spec.

        Voice mode is the primary consumer — the per-sentence TTS path in
        index.html turns this into perceived sub-1s latency."""
        import json as _json
        audit.log(
            "user", "inbound_stream", 0,
            args={"channel": body.channel, "conv_id": body.conversation_id},
            result=body.text[:300],
        )

        async def gen():
            if agent is None:
                yield "data: " + _json.dumps({"type": "error", "message": "agent not wired"}) + "\n\n"
                return
            try:
                async for ev in agent.turn_stream(
                    body.text,
                    channel=body.channel,
                    conversation_id=body.conversation_id,
                ):
                    yield "data: " + _json.dumps(ev) + "\n\n"
            except Exception as e:  # noqa: BLE001
                yield "data: " + _json.dumps({"type": "error", "message": str(e)}) + "\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                # Disable proxy buffering so events arrive immediately.
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @api.post("/approve", dependencies=[Depends(require_auth)])
    async def approve(body: ApproveBody):
        if task_runner is None:
            raise HTTPException(status_code=503, detail="task runner not wired")
        if body.approve:
            try:
                await task_runner.approve(body.job_id)
            except (KeyError, ValueError) as e:
                raise HTTPException(status_code=400, detail=str(e))
            return {"ok": True, "action": "approved", "job_id": body.job_id}
        task_runner.deny(body.job_id)
        return {"ok": True, "action": "denied", "job_id": body.job_id}

    @api.get("/api/state", dependencies=[Depends(require_auth)])
    async def state():
        jobs_out: list[dict] = []
        if task_runner:
            for j in task_runner.list_recent(20):
                d = j.to_dict()
                jobs_out.append({
                    "id": d["id"],
                    "kind": d["kind"],
                    "status": d["status"],
                    "scope_preview": d["scope_preview"],
                    "created_at": d["created_at"],
                    "started_at": d.get("started_at"),
                    "deliverable_path": d.get("deliverable_path"),
                })

        mcps_out: list[dict] = []
        if registry is not None:
            # Prefer the registry's own grouping (Tool.group + add_group()).
            # Falls back to an empty list only if someone is running an older
            # registry shape.
            if hasattr(registry, "groups"):
                # Refresh the google-backed groups' status against live auth
                # state so mid-session OAuth connects flip them to 'active'.
                try:
                    from sentrial.core import google_oauth
                    google_status = "active" if google_oauth.is_connected() else "pending_auth"
                    for g in ("gmail", "calendar"):
                        if g in registry._status:  # noqa: SLF001
                            registry.set_status(g, google_status)
                except Exception:  # noqa: BLE001
                    pass
                mcps_out = registry.groups()

        return {
            "jobs": jobs_out,
            "audit": audit.tail(40),
            "mcps": mcps_out,
            "pins": memory.list_pins()[:30],
            "stats": {
                "uptime_seconds": int(time.time() - BOOT_TIME),
                "api_today": audit.count_today(),
                "jobs_total": len(task_runner.jobs) if task_runner else 0,
            },
        }

    # ---- Chat history ----

    @api.get("/api/conversations", dependencies=[Depends(require_auth)])
    async def list_conversations_ep(limit: int = 50):
        return {"conversations": memory.list_conversations(limit=limit)}

    @api.get("/api/conversations/{conv_id}", dependencies=[Depends(require_auth)])
    async def get_conversation_ep(conv_id: str):
        conv = memory.get_conversation(conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="not found")
        return conv

    @api.post("/api/push/subscribe", dependencies=[Depends(require_auth)])
    async def push_subscribe(sub: PushSub):
        memory.save_push_subscription(sub.endpoint, sub.keys)
        return {"ok": True}

    @api.post("/api/push/unsubscribe", dependencies=[Depends(require_auth)])
    async def push_unsubscribe(body: PushUnsub):
        memory.remove_push_subscription(body.endpoint)
        return {"ok": True}

    # ---- Proposals (self-improvement) ----

    @api.get("/api/proposals", dependencies=[Depends(require_auth)])
    async def list_proposals_ep(status: str | None = None):
        from sentrial.evolution import proposals as props
        return {"proposals": props.list_all(status=status)}

    @api.get("/api/proposals/{pid}", dependencies=[Depends(require_auth)])
    async def get_proposal_ep(pid: str):
        from sentrial.evolution import proposals as props
        p = props.get(pid)
        if not p:
            raise HTTPException(status_code=404, detail="not found")
        return p

    @api.post("/api/proposals/{pid}/approve", dependencies=[Depends(require_auth)])
    async def approve_proposal_ep(pid: str):
        from sentrial.evolution import proposals as props
        try:
            p = props.approve(pid)
        except (KeyError, ValueError, FileNotFoundError, RuntimeError, PermissionError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "proposal": {k: v for k, v in p.items() if k not in ("before","after")}}

    @api.post("/api/proposals/{pid}/deny", dependencies=[Depends(require_auth)])
    async def deny_proposal_ep(pid: str, body: dict | None = None):
        from sentrial.evolution import proposals as props
        reason = (body or {}).get("reason", "")
        ok = props.deny(pid, reason=reason)
        if not ok:
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    @api.post("/api/proposals/{pid}/revert", dependencies=[Depends(require_auth)])
    async def revert_proposal_ep(pid: str):
        from sentrial.evolution import proposals as props
        try:
            p = props.revert(pid)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "proposal": {k: v for k, v in p.items() if k not in ("before","after")}}

    @api.post("/api/evolution/run", dependencies=[Depends(require_auth)])
    async def run_evolution_ep(body: dict | None = None):
        from sentrial.evolution import loop as evo_loop
        dry = bool((body or {}).get("dry_run", False))
        report = await evo_loop.run_cycle(dry_run=dry)
        return report.to_dict()

    @api.get("/api/metrics", dependencies=[Depends(require_auth)])
    async def metrics_ep(window_days: int = 7):
        from sentrial.evolution import metrics as evo_metrics
        return evo_metrics.compute_metrics(window_days=window_days).to_dict()

    @api.get("/api/today", dependencies=[Depends(require_auth)])
    async def today_ep():
        """
        Dashboard-shaped aggregate for the Today tab: integrations status, Notion
        todos, warnings, placeholders for emails/calendar. Server-side direct calls
        (no LLM) for speed.
        """
        from sentrial.core import audit as _audit
        from sentrial.core import secrets as _kc
        from sentrial.evolution import proposals as _props

        # ---- integrations ----
        # Real status per capability:
        #   active       — ready to use right now
        #   pending_auth — creds configured but user hasn't completed OAuth
        #   disabled     — no creds configured at all
        from sentrial.core import google_oauth as _gauth
        integrations: list[dict] = []
        notion_active = bool(_kc.get("notion_api_key"))
        integrations.append({
            "key": "notion", "name": "Notion",
            "status": "active" if notion_active else "disabled",
            "detail": "Connected" if notion_active else "Needs API key or OAuth",
        })
        google_creds = bool(
            _kc.get("google_client_id") or os.environ.get("GOOGLE_CLIENT_ID")
        )
        google_authed = _gauth.is_connected()
        for key, label in (("gmail", "Gmail"), ("calendar", "Calendar")):
            if google_authed:
                status, detail = "active", "Connected"
            elif google_creds:
                status, detail = "pending_auth", "Authorize at /api/oauth/google/start"
            else:
                status, detail = "disabled", "Google OAuth not configured"
            integrations.append({"key": key, "name": label,
                                  "status": status, "detail": detail})

        # ---- Notion todos (direct tool call, no LLM) ----
        todos: list[dict] = []
        todos_error: str | None = None
        if _kc.get("notion_api_key") and _kc.get("notion_tasks_db_id"):
            try:
                from sentrial.mcps.notion.server import list_tasks as notion_list
                res = await notion_list({"status": "open"})
                if isinstance(res, dict) and "tasks" in res:
                    todos = res["tasks"][:20]
                elif isinstance(res, dict) and "error" in res:
                    todos_error = res["error"][:200]
            except Exception as e:  # noqa: BLE001
                todos_error = str(e)[:200]

        # ---- warnings ----
        warnings: list[dict] = []
        if task_runner is not None:
            for j in task_runner.list_recent(20):
                if j.status.value == "failed":
                    warnings.append({
                        "type": "job_failed",
                        "text": f"{j.kind} job failed: {(j.error or '')[:120]}",
                        "ref": j.id,
                        "time": j.finished_at or j.created_at,
                    })
        pending_props = _props.list_all(status="pending")
        if pending_props:
            warnings.append({
                "type": "proposals_pending",
                "text": (
                    f"{len(pending_props)} self-improvement "
                    f"proposal{'s' if len(pending_props) > 1 else ''} awaiting review"
                ),
                "ref": pending_props[0]["id"] if pending_props else None,
                "time": pending_props[0].get("created_at"),
            })
        error_rows = [r for r in _audit.tail(50) if r["status"] == "error"]
        if error_rows:
            warnings.append({
                "type": "recent_errors",
                "text": (
                    f"{len(error_rows)} tool error"
                    f"{'s' if len(error_rows) > 1 else ''} in the last 50 actions"
                ),
                "time": error_rows[0]["timestamp"],
            })
        if todos_error:
            warnings.append({
                "type": "integration_error",
                "text": f"Notion query failed: {todos_error}",
                "time": None,
            })

        # ---- email + calendar ----
        # Real data when Google is connected; else legacy placeholders.
        from sentrial.core import google_oauth
        emails = {"status": "not_connected", "items": []}
        calendar_slot = {"status": "not_connected", "events": []}
        if google_oauth.is_connected():
            # Recent inbox items (unread first).
            try:
                from sentrial.mcps.gmail.server import list_emails as _list_emails
                res = await _list_emails({"limit": 8, "label": "INBOX"})
                if isinstance(res, dict) and "emails" in res:
                    emails = {"status": "active", "items": res["emails"]}
                else:
                    emails = {"status": "error", "items": [],
                              "error": (res or {}).get("error", "unknown")}
            except Exception as e:  # noqa: BLE001
                emails = {"status": "error", "items": [], "error": str(e)[:200]}
            # Next ~24h of events on primary.
            try:
                from sentrial.mcps.calendar.server import list_upcoming_events as _list_events
                res = await _list_events({"limit": 10, "hours_window": 24})
                if isinstance(res, dict) and "events" in res:
                    calendar_slot = {"status": "active", "events": res["events"]}
                else:
                    calendar_slot = {"status": "error", "events": [],
                                     "error": (res or {}).get("error", "unknown")}
            except Exception as e:  # noqa: BLE001
                calendar_slot = {"status": "error", "events": [], "error": str(e)[:200]}
        elif _kc.get("google_client_id") and _kc.get("google_client_secret"):
            emails["status"] = "pending_auth"
            calendar_slot["status"] = "pending_auth"

        return {
            "integrations": integrations,
            "todos": todos,
            "todos_error": todos_error,
            "warnings": warnings,
            "emails": emails,
            "calendar": calendar_slot,
        }

    # ---- Reminders (cross-platform, web-push backed) ----

    @api.get("/api/reminders", dependencies=[Depends(require_auth)])
    async def reminders_list(status: str = "upcoming", limit: int = 50):
        from sentrial.core import reminders as _rem
        if status == "upcoming":
            return {"reminders": _rem.list_upcoming(limit=limit)}
        return {"reminders": _rem.list_all(status=status, limit=limit)}

    @api.post("/api/reminders", dependencies=[Depends(require_auth)])
    async def reminders_create(body: dict):
        from sentrial.core import reminders as _rem
        try:
            return _rem.create(
                title=str(body.get("title") or ""),
                due_at=str(body.get("due_at") or ""),
                body=str(body.get("body") or ""),
                channels=list(body.get("channels") or ["push"]),
                source=str(body.get("source") or "user"),
                notion_task_id=body.get("notion_task_id"),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @api.delete("/api/reminders/{rid}", dependencies=[Depends(require_auth)])
    async def reminders_cancel(rid: str):
        from sentrial.core import reminders as _rem
        if not _rem.cancel(rid):
            raise HTTPException(status_code=404, detail="not found or already fired")
        return {"ok": True}

    @api.post("/api/reminders/{rid}/snooze", dependencies=[Depends(require_auth)])
    async def reminders_snooze(rid: str, body: dict):
        from sentrial.core import reminders as _rem
        r = _rem.snooze(rid, int(body.get("minutes") or 0))
        if not r:
            raise HTTPException(status_code=404, detail="not found or not scheduled")
        return r

    # ---- Quick notes (small dashboard-backed scratchpad over memory.facts) ----

    @api.get("/api/notes", dependencies=[Depends(require_auth)])
    async def notes_list():
        notes = memory.recall_scope("notes") or {}
        # Sort newest-first by key (keys are timestamps).
        items = [
            {"key": k, "text": (v.get("text") if isinstance(v, dict) else str(v)),
             "pinned": bool(v.get("pinned", False)) if isinstance(v, dict) else False,
             "updated_at": (v.get("updated_at") if isinstance(v, dict) else None)}
            for k, v in notes.items()
        ]
        items.sort(key=lambda x: (not x["pinned"], -(int(x["key"]) if x["key"].isdigit() else 0)))
        return {"notes": items}

    @api.post("/api/notes", dependencies=[Depends(require_auth)])
    async def notes_upsert(body: dict):
        import time as _time
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        key = str(body.get("key") or int(_time.time() * 1000))
        memory.remember("notes", key, {
            "text": text[:2000],
            "pinned": bool(body.get("pinned", False)),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        return {"ok": True, "key": key}

    @api.delete("/api/notes/{key}", dependencies=[Depends(require_auth)])
    async def notes_delete(key: str):
        ok = memory.forget("notes", key)
        if not ok:
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    # ---- Weather (proxy to Open-Meteo so the browser can stay keyless) ----

    _WEATHER_CACHE: dict = {}

    @api.get("/api/weather", dependencies=[Depends(require_auth)])
    async def weather(lat: float, lon: float, timezone: str = "auto"):
        """
        Current conditions + 3-day forecast from Open-Meteo. Cached 10 min per
        (rounded lat/lon) so rapid dashboard refreshes don't hammer the API.
        """
        import httpx
        bucket = f"{round(lat, 2)}_{round(lon, 2)}_{timezone}"
        entry = _WEATHER_CACHE.get(bucket)
        now_ts = int(time.time())
        if entry and (now_ts - entry["at"] < 600):
            return entry["data"]
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m,apparent_temperature"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"
            "&forecast_days=3&temperature_unit=fahrenheit&wind_speed_unit=mph"
            f"&timezone={timezone}"
        )
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(url)
                if r.status_code != 200:
                    raise HTTPException(status_code=502, detail=f"open-meteo {r.status_code}")
                data = r.json()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(e))
        _WEATHER_CACHE[bucket] = {"at": now_ts, "data": data}
        return data

    # ---- Plan-day passthrough (so dashboard can call the solver directly) ----

    @api.post("/api/plan_day", dependencies=[Depends(require_auth)])
    async def plan_day_ep(body: dict):
        from sentrial.mcps.scheduling.server import plan_day
        return await plan_day(body)

    # ---- OAuth scaffolding (Notion + Google — tokens land in memory.facts) ----

    @api.get("/api/oauth/notion/start")
    async def oauth_notion_start():
        cid = kc.get("notion_oauth_client_id") or os.environ.get("NOTION_OAUTH_CLIENT_ID")
        if not cid:
            raise HTTPException(status_code=503, detail="Notion OAuth client id not configured")
        redirect = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + "/api/oauth/notion/callback"
        from urllib.parse import urlencode
        url = "https://api.notion.com/v1/oauth/authorize?" + urlencode({
            "client_id": cid,
            "response_type": "code",
            "owner": "user",
            "redirect_uri": redirect,
        })
        return RedirectResponse(url, status_code=307)

    @api.get("/api/oauth/notion/callback")
    async def oauth_notion_callback(code: str | None = None, error: str | None = None):
        if error or not code:
            raise HTTPException(status_code=400, detail=error or "missing code")
        import base64, httpx as _httpx
        cid = kc.get("notion_oauth_client_id") or os.environ.get("NOTION_OAUTH_CLIENT_ID")
        cs = kc.get("notion_oauth_client_secret") or os.environ.get("NOTION_OAUTH_CLIENT_SECRET")
        redirect = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + "/api/oauth/notion/callback"
        if not cid or not cs:
            raise HTTPException(status_code=503, detail="Notion OAuth not configured")
        auth = base64.b64encode(f"{cid}:{cs}".encode()).decode()
        try:
            async with _httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    "https://api.notion.com/v1/oauth/token",
                    headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
                    json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
                )
                if r.status_code != 200:
                    raise HTTPException(status_code=502, detail=f"token exchange {r.status_code}: {r.text[:200]}")
                tok = r.json()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(e))
        memory.remember("oauth", "notion", tok)
        audit.log("user", "oauth_connected:notion", 2, result="saved")
        return RedirectResponse("/ui/#settings", status_code=307)

    @api.get("/api/oauth/google/start")
    async def oauth_google_start(scope: str = "openid email profile"):
        cid = kc.get("google_client_id") or os.environ.get("GOOGLE_CLIENT_ID")
        if not cid:
            raise HTTPException(status_code=503, detail="Google OAuth client id not configured")
        redirect = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + "/api/oauth/google/callback"
        from urllib.parse import urlencode
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": cid,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": scope,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        })
        return RedirectResponse(url, status_code=307)

    @api.get("/api/oauth/google/callback")
    async def oauth_google_callback(code: str | None = None, error: str | None = None):
        if error or not code:
            raise HTTPException(status_code=400, detail=error or "missing code")
        import httpx as _httpx
        cid = kc.get("google_client_id") or os.environ.get("GOOGLE_CLIENT_ID")
        cs = kc.get("google_client_secret") or os.environ.get("GOOGLE_CLIENT_SECRET")
        redirect = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + "/api/oauth/google/callback"
        if not cid or not cs:
            raise HTTPException(status_code=503, detail="Google OAuth not configured")
        try:
            async with _httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={"code": code, "client_id": cid, "client_secret": cs,
                          "redirect_uri": redirect, "grant_type": "authorization_code"},
                )
                if r.status_code != 200:
                    raise HTTPException(status_code=502, detail=f"token exchange {r.status_code}: {r.text[:200]}")
                tok = r.json()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(e))
        # Normalize + persist via the shared helper so expires_at is set and
        # subsequent refreshes preserve the refresh_token if Google omits it.
        from sentrial.core import google_oauth
        google_oauth.save_token(tok)
        audit.log("user", "oauth_connected:google", 2, result="saved")
        return RedirectResponse("/ui/#settings", status_code=307)

    # ---- Evolution surfaces (profile / lessons / playbooks / KG / trials / integrity / reset) ----

    @api.get("/api/evolution/profile", dependencies=[Depends(require_auth)])
    async def ev_profile_get():
        from sentrial.evolution import profile as prof
        return {"profile": prof.load(), "summary": prof.summary_for_agent()}

    @api.post("/api/evolution/profile/observe", dependencies=[Depends(require_auth)])
    async def ev_profile_observe(body: dict):
        from sentrial.evolution import profile as prof
        path = body.get("path")
        if not path or "value" not in body:
            raise HTTPException(status_code=400, detail="path + value required")
        res = prof.observe(
            str(path), body["value"],
            weight=float(body.get("weight") or 0.4),
            reason=str(body.get("reason") or "manual"),
        )
        return res

    @api.get("/api/evolution/lessons", dependencies=[Depends(require_auth)])
    async def ev_lessons_list(status: str = "active"):
        from sentrial.evolution import lessons as lsn
        return {"lessons": lsn.list_all(status=status)}

    @api.post("/api/evolution/lessons", dependencies=[Depends(require_auth)])
    async def ev_lessons_create(body: dict):
        from sentrial.evolution import lessons as lsn
        rule = str(body.get("rule") or "").strip()
        if not rule:
            raise HTTPException(status_code=400, detail="rule required")
        doc = lsn.create(
            rule=rule,
            tags=list(body.get("tags") or []),
            keywords=list(body.get("keywords") or []),
            confidence=float(body.get("confidence") or 0.6),
            source="user",
        )
        return doc

    @api.post("/api/evolution/lessons/{lid}/retire", dependencies=[Depends(require_auth)])
    async def ev_lessons_retire(lid: str, body: dict | None = None):
        from sentrial.evolution import lessons as lsn
        ok = lsn.retire(lid, reason=(body or {}).get("reason", ""))
        if not ok:
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    @api.get("/api/evolution/playbooks", dependencies=[Depends(require_auth)])
    async def ev_playbooks_list():
        from sentrial.evolution import playbooks as pb
        return {"playbooks": pb.list_all()}

    @api.get("/api/evolution/playbooks/{slug}", dependencies=[Depends(require_auth)])
    async def ev_playbooks_get(slug: str):
        from sentrial.evolution import playbooks as pb
        body, meta = pb.read(slug)
        if meta is None and body is None:
            raise HTTPException(status_code=404, detail="not found")
        return {"meta": meta, "body": body}

    @api.post("/api/evolution/playbooks", dependencies=[Depends(require_auth)])
    async def ev_playbooks_upsert(body: dict):
        from sentrial.evolution import playbooks as pb
        slug = str(body.get("slug") or "").strip()
        label = str(body.get("label") or slug)
        md = str(body.get("body_md") or "")
        if not slug or not md:
            raise HTTPException(status_code=400, detail="slug + body_md required")
        return pb.create_or_update(slug=slug, label=label, body_md=md, source="user")

    @api.delete("/api/evolution/playbooks/{slug}", dependencies=[Depends(require_auth)])
    async def ev_playbooks_delete(slug: str):
        from sentrial.evolution import playbooks as pb
        ok = pb.delete(slug)
        if not ok:
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    @api.get("/api/evolution/kg", dependencies=[Depends(require_auth)])
    async def ev_kg_list(type: str | None = None, limit: int = 200):
        from sentrial.evolution import kg
        return {"entities": kg.list_entities(etype=type, limit=limit)}

    @api.get("/api/evolution/kg/{entity_id}", dependencies=[Depends(require_auth)])
    async def ev_kg_get(entity_id: str):
        from sentrial.evolution import kg
        card = kg.card(entity_id)
        if not card:
            raise HTTPException(status_code=404, detail="not found")
        return {"card": card, "edges": kg.edges_from(entity_id)}

    @api.post("/api/evolution/kg", dependencies=[Depends(require_auth)])
    async def ev_kg_upsert(body: dict):
        from sentrial.evolution import kg
        etype = str(body.get("type") or "").strip()
        name = str(body.get("name") or "").strip()
        if not etype or not name:
            raise HTTPException(status_code=400, detail="type + name required")
        eid = kg.upsert_entity(
            etype=etype, name=name,
            attrs=body.get("attrs") or {},
            aliases=list(body.get("aliases") or []),
            confidence=float(body.get("confidence") or 0.7),
        )
        return {"id": eid}

    @api.get("/api/evolution/trials", dependencies=[Depends(require_auth)])
    async def ev_trials_list():
        from sentrial.evolution import trials as tr
        return {"trials": tr.list_all()}

    @api.get("/api/evolution/trials/{tid}", dependencies=[Depends(require_auth)])
    async def ev_trials_get(tid: str):
        from sentrial.evolution import trials as tr
        return tr.summarize(tid)

    @api.post("/api/evolution/trials", dependencies=[Depends(require_auth)])
    async def ev_trials_start(body: dict):
        from sentrial.evolution import trials as tr
        try:
            return tr.start_trial(
                name=str(body.get("name") or "untitled"),
                target=str(body.get("target") or ""),
                baseline_body=str(body.get("baseline_body") or ""),
                variant_body=str(body.get("variant_body") or ""),
                treatment_pct=int(body.get("treatment_pct") or 25),
                max_duration_h=int(body.get("max_duration_h") or 168),
            )
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @api.post("/api/evolution/trials/{tid}/stop", dependencies=[Depends(require_auth)])
    async def ev_trials_stop(tid: str, body: dict | None = None):
        from sentrial.evolution import trials as tr
        reason = (body or {}).get("reason", "manual")
        if not tr.stop_trial(tid, reason=reason):
            raise HTTPException(status_code=404, detail="not running or not found")
        return {"ok": True}

    @api.get("/api/evolution/integrity", dependencies=[Depends(require_auth)])
    async def ev_integrity(full: bool = False):
        from sentrial.evolution import integrity
        return integrity.run(full=full).to_dict()

    @api.post("/api/evolution/reset", dependencies=[Depends(require_auth)])
    async def ev_reset(body: dict):
        from sentrial.evolution import reset as rst
        level = str(body.get("level") or "")
        confirm = body.get("confirm_token")
        try:
            manifest = rst.reset(level=level, confirm_token=confirm)
        except (ValueError, PermissionError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return manifest

    @api.get("/api/evolution/reset/backups", dependencies=[Depends(require_auth)])
    async def ev_reset_backups():
        from sentrial.evolution import reset as rst
        return {"backups": rst.list_backups()}

    @api.get("/api/metrics/trend", dependencies=[Depends(require_auth)])
    async def metrics_trend_ep():
        """Compare current 7d metrics vs previous 7d to surface direction of change."""
        from sentrial.evolution import metrics as evo_metrics
        cur = evo_metrics.compute_metrics(window_days=7).to_dict()
        prev_14 = evo_metrics.compute_metrics(window_days=14).to_dict()
        # "Previous 7d" ~= 14d - 7d. Rough but directional.
        LOWER_BETTER = {"edit_rate", "tool_denial_rate", "clarification_rate", "avg_latency_s"}
        HIGHER_BETTER = {"scope_preview_acceptance"}
        deltas = {}
        for k in list(LOWER_BETTER) + list(HIGHER_BETTER):
            c = cur.get(k)
            p = prev_14.get(k)
            if c is None or p is None:
                continue
            # Directional improvement: positive means getting better
            if k in LOWER_BETTER:
                improvement = (p - c)
            else:
                improvement = (c - p)
            deltas[k] = {
                "current": c,
                "previous": p,
                "improvement": round(improvement, 4),
                "better_direction": "lower" if k in LOWER_BETTER else "higher",
            }
        return {"current": cur, "previous_14d_avg": prev_14, "deltas": deltas}

    return api


async def serve(
    host: str | None = None,
    port: int | None = None,
    task_runner: "TaskRunner | None" = None,
    agent: "Agent | None" = None,
    registry=None,
) -> None:
    host = host or os.environ.get("HOST", "0.0.0.0")
    port = port or int(os.environ.get("PORT", "8765"))
    api = build_app(task_runner=task_runner, agent=agent, registry=registry)
    config = uvicorn.Config(api, host=host, port=port, log_level="info", access_log=False)
    await uvicorn.Server(config).serve()
