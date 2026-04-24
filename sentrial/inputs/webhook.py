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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
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
        if registry is not None and hasattr(registry, "tools"):
            # Group by module prefix if present
            names_seen: set[str] = set()
            for t in registry.tools:
                name = t.get("name", "")
                prefix = name.split("_", 1)[0]
                if prefix in names_seen:
                    continue
                names_seen.add(prefix)
            # Flat list of capability groups: hardcoded for now since we know what's loaded
            for cap in ("notion", "creative", "gmail", "calendar", "sentrial_pipeline"):
                cap_tools = [t for t in registry.tools if cap in t.get("name", "")]
                if cap_tools:
                    mcps_out.append({"name": cap, "status": "active", "tools": len(cap_tools)})
                else:
                    mcps_out.append({"name": cap, "status": "disabled", "tools": 0})

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
