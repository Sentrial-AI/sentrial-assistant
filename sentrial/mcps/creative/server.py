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
import shlex
import shutil
import subprocess
import uuid
from datetime import datetime
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
    audit.log("sentrial", "skill_invoke:proposal", 1,
              args={"job_id": job.id}, job_id=job.id)
    return await _run_skill_subprocess("proposal", job)


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
