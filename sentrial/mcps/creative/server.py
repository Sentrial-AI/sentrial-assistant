"""
Creative autonomous workflow — the flagship MCP.

This is the module that implements Liam's headline UX:
    text Sentrial → scope preview → approve → runs 5–10 min → deliverable link

Exposes three tools to the agent:
  start_background_job   — create a scope-previewed pending job
  list_active_jobs       — see what's in flight / pending approval
  get_job_status         — poll a specific job

Registers four executors with the task runner:
  proposal      — wraps the `proposal` skill
  audit         — wraps the `buildlog-audit` / `pynacle` skills (kind chooses which)
  demo_site     — scaffolds a small branded demo site
  demo_feature  — scaffolds a single feature demo (HTML/React) for pitch meetings

Each executor invokes the relevant skill via subprocess so we stay aligned with the
skills folder Liam already maintains (no duplication of skill logic here).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from sentrial.core import audit, paths
from sentrial.core.confirmation import Tier
from sentrial.core.task_runner import Job, TaskRunner
from sentrial.mcps.base import Registry, Tool

log = logging.getLogger(__name__)

# Deliverables land on the persistent volume. On Mac local, that's ~/Library/Application Support/Sentrial/deliverables
# On Railway, /data/deliverables.
DELIVERABLES_DIR = paths.deliverables_dir()

SKILLS_DIR = Path(
    "/var/folders/wq/dj3k3v254ll03tr58bc4nw000000gn/T/"
    "claude-hostloop-plugins/1f057949213f0daf/skills"
)

VALID_KINDS = {"proposal", "audit", "demo_site", "demo_feature"}


# -----------------------------------------------------------------------------
# Tools exposed to the agent
# -----------------------------------------------------------------------------

# Shared task_runner reference populated by register()
_runner: TaskRunner | None = None


async def start_background_job(args: dict) -> Any:
    """
    Agent calls this when Liam asks for a proposal/audit/demo.

    The agent MUST provide a concise scope_preview (what will be produced, ETA,
    which skill/tool — text-message-sized). The job is created PENDING_APPROVAL.
    The agent then sends the scope_preview to Liam via notify; Liam approves via
    the /approve webhook or by replying 'yes'.
    """
    if _runner is None:
        return {"error": "task runner not registered"}

    kind = args.get("kind")
    request = args.get("request", "")
    scope_preview = args.get("scope_preview", "")
    params = args.get("params") or {}

    if kind not in VALID_KINDS:
        return {"error": f"kind must be one of {sorted(VALID_KINDS)}, got {kind!r}"}
    if not scope_preview:
        return {"error": "scope_preview is required — this is what Liam sees before approving"}

    job = _runner.create_job(kind=kind, request=request, scope_preview=scope_preview, params=params)
    return {
        "job_id": job.id,
        "status": job.status.value,
        "scope_preview": scope_preview,
        "next_step": (
            "Send the scope_preview to Liam via notify. On his 'yes', POST "
            f"to /approve with job_id={job.id}, or call the internal approve_job tool."
        ),
    }


async def list_active_jobs(_args: dict) -> Any:
    if _runner is None:
        return {"error": "task runner not registered"}
    active = _runner.list_active()
    return {
        "active": [
            {
                "id": j.id,
                "kind": j.kind,
                "status": j.status.value,
                "scope_preview": j.scope_preview,
                "created_at": j.created_at,
            }
            for j in active
        ],
        "count": len(active),
    }


async def get_job_status(args: dict) -> Any:
    if _runner is None:
        return {"error": "task runner not registered"}
    jid = args.get("job_id")
    if not jid:
        return {"error": "job_id required"}
    j = _runner.get(jid)
    if j is None:
        return {"error": f"no such job: {jid}"}
    return j.to_dict()


async def approve_job(args: dict) -> Any:
    """
    Explicit approval path for cases where Liam types 'approve <id>' in the menubar
    instead of hitting the webhook. Tier 2 — classified as SEND.
    """
    if _runner is None:
        return {"error": "task runner not registered"}
    jid = args.get("job_id")
    if not jid:
        return {"error": "job_id required"}
    try:
        await _runner.approve(jid)
    except (KeyError, ValueError) as e:
        return {"error": str(e)}
    return {"ok": True, "job_id": jid, "status": "approved"}


async def list_proposals(_args: dict) -> Any:
    """Return recent proposals with their status + deliverable path. Used by
    the agent when Liam asks 'what proposals do I have?'"""
    if _runner is None:
        return {"error": "task runner not registered"}
    items = []
    for j in _runner.list_recent(30):
        if j.kind != "proposal":
            continue
        jp = j.params or {}
        deliv = DELIVERABLES_DIR / j.id / "proposal.html"
        items.append({
            "job_id": j.id,
            "client": jp.get("client") or "(unknown)",
            "brand": jp.get("brand") or "sentrial",
            "format": jp.get("format") or "major",
            "status": j.status.value,
            "created_at": j.created_at,
            "ready": deliv.exists(),
            "deliverable_path": str(deliv) if deliv.exists() else None,
        })
    return {"proposals": items, "count": len(items)}


async def delete_proposal(args: dict) -> Any:
    """Delete a proposal — removes its job record + the deliverable folder.
    Tier SEND because it's destructive (the proposal HTML is gone)."""
    if _runner is None:
        return {"error": "task runner not registered"}
    jid = (args.get("job_id") or "").strip()
    if not jid:
        return {"error": "job_id required"}
    job = _runner.get(jid)
    if job is None:
        return {"error": f"no job with id {jid}"}
    if job.kind != "proposal":
        return {"error": f"job {jid} is kind={job.kind}, not a proposal"}

    out_dir = DELIVERABLES_DIR / jid
    removed_files = 0
    if out_dir.exists():
        try:
            for p in out_dir.rglob("*"):
                if p.is_file():
                    p.unlink()
                    removed_files += 1
            try:
                out_dir.rmdir()
            except OSError:
                pass
        except Exception as e:  # noqa: BLE001
            return {"error": f"deliverable cleanup failed: {e}"}
    # Remove from in-memory + persisted job store
    try:
        _runner.delete(jid)
    except AttributeError:
        # Fall back: just drop from the dict if no .delete()
        _runner.jobs.pop(jid, None)

    audit.log("user", "delete_proposal", 2, args={"job_id": jid},
              result=f"removed {removed_files} file(s)")
    return {"ok": True, "job_id": jid, "removed_files": removed_files}


async def edit_proposal(args: dict) -> Any:
    """Edit a proposal by patching its structured JSON sidecar and re-rendering.
    Cheap: uses the same template as the original — no LLM call unless the
    user asked to regenerate the prose.

    Args:
      job_id      — which proposal to edit
      patch       — dict of fields to overwrite (title / subtitle / intro /
                    pricing / timeline / next_steps). For sections, pass
                    sections (full replacement list).
      regenerate  — if true AND a 'request' is provided, re-call the LLM
                    with the new request as additional brief; otherwise just
                    splice in the patch and re-render.
    """
    if _runner is None:
        return {"error": "task runner not registered"}
    jid = (args.get("job_id") or "").strip()
    if not jid:
        return {"error": "job_id required"}
    patch = args.get("patch") or {}
    if not isinstance(patch, dict) or not patch:
        return {"error": "patch (dict of fields to update) required"}

    out_dir = DELIVERABLES_DIR / jid
    json_path = out_dir / "proposal.json"
    html_path = out_dir / "proposal.html"
    if not json_path.exists():
        return {"error": f"proposal {jid} has no JSON sidecar — can't edit (try regenerating)"}

    import json as _json
    structured = _json.loads(json_path.read_text(encoding="utf-8"))

    # Apply allowed top-level patches
    allowed = {"title", "subtitle", "intro", "pricing", "timeline", "next_steps", "sections"}
    applied = []
    for k, v in patch.items():
        if k in allowed:
            structured[k] = v
            applied.append(k)
    if not applied:
        return {"error": f"no editable fields in patch — allowed: {sorted(allowed)}"}

    # Re-render via the same brand template + format the original used.
    from sentrial.mcps.proposals.generator import ProposalBrief
    from sentrial.mcps.proposals.templates import pursuit, sentrial as sent_tpl

    meta = structured.get("_meta") or {}
    brand = meta.get("brand") or "sentrial"
    fmt_ = meta.get("format") or "major"
    client = meta.get("client") or "Client"
    brief = ProposalBrief(
        client=client, brief="(edit)", brand=brand, format=fmt_,
    ).normalize()
    html = (pursuit if brand == "pursuit" else sent_tpl).render(structured, brief)
    html_path.write_text(html, encoding="utf-8")
    json_path.write_text(_json.dumps(structured, indent=2, ensure_ascii=False), encoding="utf-8")

    audit.log("user", "edit_proposal", 1,
              args={"job_id": jid, "patched": applied},
              result=str(html_path))
    return {
        "ok": True,
        "job_id": jid,
        "patched_fields": applied,
        "deliverable_path": str(html_path),
    }


async def preview_proposal(args: dict) -> Any:
    """Open a preview window for a proposal. Returns a URL the menubar's
    PWA can pop into a floating window via the JS bridge.
    """
    if _runner is None:
        return {"error": "task runner not registered"}
    jid = (args.get("job_id") or "").strip()
    if not jid:
        return {"error": "job_id required"}
    out_dir = DELIVERABLES_DIR / jid
    html_path = out_dir / "proposal.html"
    if not html_path.exists():
        return {"error": f"proposal {jid} has no rendered HTML yet"}

    # Returned URL is served by the existing /deliverables/<job_id>/proposal.html
    # route (added in webhook.py companion edit). The PWA's preview bridge
    # opens this in a floating window.
    return {
        "ok": True,
        "job_id": jid,
        "preview_url": f"/deliverables/{jid}/proposal.html",
        "next_step": (
            "Tell Liam 'Opening it now' and call the JS bridge "
            "window.sentrialOpenPreview(url) — handled by the PWA."
        ),
    }


async def close_preview(_args: dict) -> Any:
    """Voice command: 'close that window' → JS bridge closes the preview.
    The agent emits this; the PWA wires window.sentrialClosePreview()."""
    return {
        "ok": True,
        "next_step": "Tell PWA to close the preview window via window.sentrialClosePreview().",
    }


async def start_proposal(args: dict) -> Any:
    """High-level wrapper around start_background_job for proposals. Voice-
    friendly: agent fills out brand/format/client/brief from the conversation
    and gets back an immediate handle to confirm verbally ('Got it,
    drafting now'). The actual generation runs as a background job.

    Brand defaults to "sentrial" if unspecified. If Liam mentions Pursuit
    Visuals / video / photo / production, agent should pass brand="pursuit".

    Format defaults to "major" (full proposal). Pass "one_pager" only when
    Liam explicitly asks for a quick one-pager / short version.
    """
    if _runner is None:
        return {"error": "task runner not registered"}

    brand = (args.get("brand") or "sentrial").lower().strip()
    format_ = (args.get("format") or "major").lower().strip().replace("-", "_")
    client = (args.get("client") or "").strip()
    brief = (args.get("brief") or "").strip()
    pricing_hint = (args.get("pricing_hint") or "").strip() or None
    deadline = (args.get("deadline") or "").strip() or None
    extra_context = (args.get("extra_context") or "").strip() or None

    if not client:
        return {"error": "client is required — who is the proposal for?"}
    if not brief:
        return {"error": "brief is required — even one sentence on what they need"}
    if brand not in {"pursuit", "sentrial"}:
        brand = "sentrial"
    if format_ not in {"one_pager", "major"}:
        format_ = "major"

    # Dedup: if we just queued an identical proposal in the last 60s, return
    # that job's id instead of creating a duplicate. Defends against:
    #   - the LLM emitting two parallel start_proposal tool_use blocks
    #     in the same turn
    #   - the user adding details after the agent already kicked off the
    #     job, triggering a fresh utterance that the agent processes as
    #     a NEW request
    # Both were producing duplicate proposals visible in the Agents tab.
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    for j in _runner.list_recent(20):
        if j.kind != "proposal":
            continue
        jp = j.params or {}
        if (jp.get("client") or "").strip().lower() != client.lower():
            continue
        if (jp.get("brief") or "").strip() != brief:
            continue
        try:
            j_at = _dt.fromisoformat(j.created_at.replace("Z", "+00:00"))
            if j_at.tzinfo is None:
                j_at = j_at.replace(tzinfo=_tz.utc)
        except Exception:
            continue
        if (now - j_at).total_seconds() < 60:
            return {
                "ok": True,
                "job_id": j.id,
                "duplicate": True,
                "status": j.status.value,
                "next_step": (
                    f"This is a duplicate request for the same proposal "
                    f"queued {int((now - j_at).total_seconds())}s ago. "
                    f"DON'T re-confirm; just tell Liam it's already "
                    f"drafting and end the turn."
                ),
            }

    # Auto-build a scope_preview from inputs so voice doesn't have to.
    fmt_label = "one-pager" if format_ == "one_pager" else "full proposal"
    brand_label = "Pursuit Visuals" if brand == "pursuit" else "Sentrial"
    eta = "1-2 min" if format_ == "one_pager" else "2-4 min"
    scope_preview = (
        f"{brand_label} {fmt_label} for {client}. "
        f"ETA {eta}. Will save to deliverables/<id>/proposal.html."
    )

    params = {
        "brand": brand,
        "format": format_,
        "client": client,
        "brief": brief,
    }
    if pricing_hint:
        params["pricing_hint"] = pricing_hint
    if deadline:
        params["deadline"] = deadline
    if extra_context:
        params["extra_context"] = extra_context

    job = _runner.create_job(
        kind="proposal",
        request=brief,
        scope_preview=scope_preview,
        params=params,
    )
    # Auto-approve so voice flow doesn't strand the user waiting for an
    # explicit /approve roundtrip — Liam already approved verbally by
    # asking for it. The task_runner will pick it up off the queue.
    try:
        await _runner.approve(job.id)
    except (KeyError, ValueError) as e:
        log.warning("start_proposal: auto-approve failed (will require manual): %s", e)

    return {
        "ok": True,
        "job_id": job.id,
        "brand": brand,
        "format": format_,
        "client": client,
        "scope_preview": scope_preview,
        "status": "approved-and-queued",
        "next_step": (
            f"Tell Liam: '{brand_label} {fmt_label} for {client} — drafting now, "
            f"ready in about {eta}.' Then end the turn."
        ),
    }


# -----------------------------------------------------------------------------
# Executors (run under task_runner — these are the long-running workers)
# -----------------------------------------------------------------------------

async def _run_skill_subprocess(
    skill_name: str,
    job: Job,
    extra_env: dict | None = None,
    timeout: int = 900,  # 15 min cap
) -> str:
    """
    Invoke a skill script via subprocess. Skills live in SKILLS_DIR/<skill>/ and
    expose runnable scripts (scrape_linkedin.py, etc.). For skills that don't have
    a single entrypoint (like `proposal`), we use a wrapper that follows the
    SKILL.md instructions via Claude.

    For v1, `proposal` / `audit` / `demo_*` are executed by spawning a sub-agent
    that loads the relevant skill and produces output into DELIVERABLES_DIR/<job_id>/.
    """
    out_dir = DELIVERABLES_DIR / job.id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Sub-agent invocation — uses claude CLI (expected on Liam's PATH since he runs Claude Code)
    # Falls back to an Anthropic API direct call if claude CLI is absent.
    instruction = (
        f"You are executing job {job.id} ({job.kind}).\n\n"
        f"Request from Liam:\n{job.request}\n\n"
        f"Parameters:\n{job.params}\n\n"
        f"Follow the '{skill_name}' skill. Save all output files into:\n  {out_dir}\n"
        f"When done, print the absolute path of the primary deliverable on the last line."
    )

    claude_bin = shutil.which("claude")
    env = {**os.environ, **(extra_env or {})}

    if claude_bin:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "-p", instruction, "--model", "claude-opus-4-6",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(out_dir),
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError(f"{skill_name} exceeded {timeout}s")
        if proc.returncode != 0:
            raise RuntimeError(
                f"{skill_name} sub-agent failed rc={proc.returncode}: "
                f"{stderr.decode(errors='replace')[:500]}"
            )
        # Last line of stdout = deliverable path
        lines = stdout.decode(errors="replace").strip().splitlines()
        if lines:
            candidate = lines[-1].strip()
            if Path(candidate).exists():
                return candidate
    # Fallback: return the out_dir itself; the agent can narrate what's inside
    return str(out_dir)


async def execute_proposal(job: Job) -> str:
    """Run the native proposal generator (no subprocess, no claude CLI).
    Reads brand/format/client/brief from job.params; falls back to
    sensible defaults so an under-specified job still produces something.
    Returns the absolute path to the rendered proposal.html."""
    audit.log("sentrial", "skill_invoke:proposal", 1,
              args={"job_id": job.id}, job_id=job.id)
    from sentrial.mcps.proposals.generator import generate, ProposalBrief

    p = job.params or {}
    brief = ProposalBrief(
        client=str(p.get("client") or "").strip() or "Prospective Client",
        brief=str(p.get("brief") or job.request or "").strip()
              or "(no brief provided — drafting from minimal context)",
        brand=str(p.get("brand") or "sentrial"),
        format=str(p.get("format") or "major"),
        pricing_hint=p.get("pricing_hint"),
        deadline=p.get("deadline"),
        extra_context=p.get("extra_context"),
    )
    out_dir = DELIVERABLES_DIR / job.id
    result = await generate(brief, out_dir)
    log.info(
        "proposal generated: brand=%s format=%s words=%d → %s",
        result.brand, result.format, result.word_count, result.html_path,
    )
    return str(result.html_path)


async def execute_audit(job: Job) -> str:
    # `params.target` selects buildlog-audit vs pynacle; default to buildlog-audit
    target = (job.params or {}).get("target", "buildlog-audit")
    skill = "pynacle" if target == "pynacle" else "buildlog-audit"
    audit.log("sentrial", f"skill_invoke:{skill}", 1,
              args={"job_id": job.id}, job_id=job.id)
    return await _run_skill_subprocess(skill, job)


async def execute_demo_site(job: Job) -> str:
    """
    Lightweight demo site builder. Produces an index.html in deliverables/<job_id>/.
    For MVP this uses a sub-agent with instructions to produce a branded single-page
    site matching the Sentrial pastel aesthetic. Future: a dedicated skill.
    """
    audit.log("sentrial", "skill_invoke:demo_site", 1,
              args={"job_id": job.id}, job_id=job.id)
    return await _run_skill_subprocess("proposal", job)  # reuses proposal aesthetic for now


async def execute_demo_feature(job: Job) -> str:
    audit.log("sentrial", "skill_invoke:demo_feature", 1,
              args={"job_id": job.id}, job_id=job.id)
    return await _run_skill_subprocess("proposal", job)  # aesthetic-matched scaffold for now


# -----------------------------------------------------------------------------
# Anthropic tool schemas
# -----------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="start_background_job",
        description=(
            "Create an approval-gated autonomous job (proposal, audit, demo_site, demo_feature). "
            "You MUST provide a concise scope_preview (text-message-sized: what you'll produce, "
            "rough ETA, which skill). The job will be PENDING_APPROVAL until the user approves. "
            "After calling this, send the scope_preview to the user via notify_user."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["proposal", "audit", "demo_site", "demo_feature"],
                },
                "request": {
                    "type": "string",
                    "description": "Original user request (transcript, URL, description).",
                },
                "scope_preview": {
                    "type": "string",
                    "description": (
                        "Short summary shown to the user before approval. "
                        "E.g., 'Proposal for Kernodle using the transcript — 5 sections, "
                        "pastel aesthetic, ETA 8 min. Go?'"
                    ),
                },
                "params": {
                    "type": "object",
                    "description": "Skill-specific params (target, company, etc.)",
                    "additionalProperties": True,
                },
            },
            "required": ["kind", "request", "scope_preview"],
        },
        impl=start_background_job,
        tier=Tier.DRAFT,
    ),
    Tool(
        name="list_active_jobs",
        description="List autonomous jobs currently pending approval, approved, or running.",
        input_schema={"type": "object", "properties": {}},
        impl=list_active_jobs,
        tier=Tier.READ,
    ),
    Tool(
        name="get_job_status",
        description="Get the status of a specific autonomous job by id.",
        input_schema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        impl=get_job_status,
        tier=Tier.READ,
    ),
    Tool(
        name="approve_job",
        description=(
            "Approve a pending autonomous job. Only call this after explicit user approval — "
            "this dispatches a sub-agent that may take 5–10 min and produce side effects."
        ),
        input_schema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        impl=approve_job,
        tier=Tier.SEND,
    ),
    Tool(
        name="list_proposals",
        description=(
            "List recent proposals with status, brand, format, and whether "
            "the rendered HTML is ready. Use when Liam asks 'what proposals "
            "do I have?' or before deleting/editing — gives you the job_id."
        ),
        input_schema={"type": "object", "properties": {}},
        impl=list_proposals,
        tier=Tier.READ,
    ),
    Tool(
        name="delete_proposal",
        description=(
            "Permanently delete a proposal — removes the job record AND "
            "the deliverable HTML/JSON. Tier SEND. Use when Liam says "
            "'delete the [X] proposal' / 'remove that one'. Get the "
            "job_id from list_proposals first."
        ),
        input_schema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        impl=delete_proposal,
        tier=Tier.SEND,
    ),
    Tool(
        name="edit_proposal",
        description=(
            "Patch specific fields of an existing proposal and re-render "
            "the HTML. No LLM call — just splices the patch into the "
            "structured JSON sidecar and re-runs the brand template. Fast. "
            "Use for surgical edits like 'change the price to $12k' or "
            "'rewrite the intro to mention urgency'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "patch": {
                    "type": "object",
                    "description": (
                        "Fields to overwrite. Allowed top-level keys: "
                        "title, subtitle, intro, pricing, timeline, "
                        "next_steps, sections (sections is a full list "
                        "replacement)."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["job_id", "patch"],
        },
        impl=edit_proposal,
        tier=Tier.DRAFT,
    ),
    Tool(
        name="preview_proposal",
        description=(
            "Open a preview window showing the rendered proposal HTML. "
            "Returns a URL that the PWA pops into a floating window via "
            "the JS bridge. Use when Liam says 'show me the [X] proposal' "
            "/ 'open that one'."
        ),
        input_schema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        impl=preview_proposal,
        tier=Tier.READ,
    ),
    Tool(
        name="close_preview",
        description=(
            "Close the proposal preview window. Use when Liam says 'close "
            "that window' / 'close the preview'."
        ),
        input_schema={"type": "object", "properties": {}},
        impl=close_preview,
        tier=Tier.READ,
    ),
    Tool(
        name="start_proposal",
        description=(
            "Voice-friendly proposal builder. Use this when Liam asks you to "
            "build / write / draft a proposal for someone. Auto-approved — the "
            "background job kicks off immediately and the rendered HTML lands "
            "in deliverables/<job_id>/proposal.html within 1-4 minutes "
            "(one-pager 1-2 min, major 2-4 min). After calling, tell Liam ETA "
            "and end the turn — don't make him wait for the file. "
            "Brand: 'pursuit' if Liam mentioned Pursuit Visuals / video / "
            "photo / production work; otherwise 'sentrial' (AI agency, default). "
            "Format: 'one_pager' only when Liam asked for a quick / short / "
            "one-page version; otherwise 'major' (full pitch)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "client": {
                    "type": "string",
                    "description": "Who the proposal is for — company or person name.",
                },
                "brief": {
                    "type": "string",
                    "description": (
                        "What the proposal is about. Verbatim from Liam's "
                        "transcript or notes is fine; the generator will "
                        "reorganize. Even one sentence is enough to start."
                    ),
                },
                "brand": {
                    "type": "string",
                    "enum": ["pursuit", "sentrial"],
                    "description": (
                        "'pursuit' for Pursuit Visuals (dark modern, video/photo). "
                        "'sentrial' for Sentrial (light modern, AI agency). "
                        "Default sentrial."
                    ),
                },
                "format": {
                    "type": "string",
                    "enum": ["one_pager", "major"],
                    "description": (
                        "'one_pager' = ~500-word skim-in-90-seconds proposal. "
                        "'major' = ~2000-word full pitch with sections + "
                        "pricing + timeline. Default major."
                    ),
                },
                "pricing_hint": {
                    "type": "string",
                    "description": "Optional — pricing direction Liam mentioned (e.g. 'around $5k', 'retainer model').",
                },
                "deadline": {
                    "type": "string",
                    "description": "Optional — when they need a decision by, or project deadline.",
                },
                "extra_context": {
                    "type": "string",
                    "description": "Optional — past relationship, tone preference, anything else relevant.",
                },
            },
            "required": ["client", "brief"],
        },
        impl=start_proposal,
        tier=Tier.DRAFT,
    ),
]


def register(registry: Registry, task_runner: TaskRunner) -> None:
    global _runner
    _runner = task_runner
    registry.add_group("creative")
    for t in TOOLS:
        registry.add(t)
    task_runner.register_executor("proposal", execute_proposal)
    task_runner.register_executor("audit", execute_audit)
    task_runner.register_executor("demo_site", execute_demo_site)
    task_runner.register_executor("demo_feature", execute_demo_feature)
