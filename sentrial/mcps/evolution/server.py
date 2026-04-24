"""
Evolution MCP — exposes self-improvement tools to the agent itself.

Sentrial uses these tools to:
  - record lessons after notable moments (tier DRAFT)
  - recall relevant lessons at the start of similar tasks (tier READ)
  - trigger a full research cycle on demand (tier SEND — Liam must approve via gate)
  - view proposals it's made and the metrics trend (tier READ)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sentrial.core import reflection
from sentrial.core.confirmation import Tier
from sentrial.core.task_runner import TaskRunner
from sentrial.evolution import loop, metrics, proposals
from sentrial.mcps.base import Registry, Tool

log = logging.getLogger(__name__)


async def remember_lesson(args: dict) -> Any:
    snippet = args.get("snippet", "")
    trigger = args.get("trigger", "manual")
    context = args.get("context", "")
    lesson = await reflection.distill_lesson(snippet, trigger, context)
    return {"ok": True, "lesson": lesson or "(no lesson extracted — trivial snippet)"}


async def recall_lessons(args: dict) -> Any:
    context = args.get("context", "")
    limit = int(args.get("limit", 5))
    hits = reflection.recall_relevant(context, limit=limit)
    return {"lessons": hits, "count": len(hits)}


async def compute_metrics(args: dict) -> Any:
    days = int(args.get("window_days", 7))
    m = metrics.compute_metrics(window_days=days)
    return m.to_dict()


async def list_proposals_tool(args: dict) -> Any:
    status = args.get("status")
    return {"proposals": proposals.list_all(status=status)}


async def run_research_cycle(args: dict) -> Any:
    dry_run = bool(args.get("dry_run", False))
    report = await loop.run_cycle(dry_run=dry_run)
    return report.to_dict()


TOOLS = [
    Tool(
        name="remember_lesson",
        description=(
            "Save a one-line lesson from a notable interaction moment. Use when Liam "
            "corrects, edits, or accepts something with non-obvious positive signal. "
            "The snippet is distilled automatically; don't pre-summarize."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "snippet": {"type": "string"},
                "trigger": {"type": "string", "description": "'edit' | 'denial' | 'praise' | 'manual'"},
                "context": {"type": "string", "description": "Short context tag, e.g. 'proposal-saas'"},
            },
            "required": ["snippet", "trigger"],
        },
        impl=remember_lesson,
        tier=Tier.DRAFT,
    ),
    Tool(
        name="recall_lessons",
        description=(
            "Retrieve relevant past lessons for a new task. Call at the START of any task "
            "that resembles prior work (proposals, audits, outreach) so you apply what "
            "Sentrial has already learned."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "context": {"type": "string", "description": "Short description of the current task"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["context"],
        },
        impl=recall_lessons,
        tier=Tier.READ,
    ),
    Tool(
        name="compute_metrics",
        description="Compute current performance metrics (edit_rate, tool_denial_rate, latency, etc.).",
        input_schema={
            "type": "object",
            "properties": {"window_days": {"type": "integer", "default": 7}},
        },
        impl=compute_metrics,
        tier=Tier.READ,
    ),
    Tool(
        name="list_proposals",
        description="List self-improvement proposals. Optional filter: status='pending'|'applied'|'denied'.",
        input_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
        },
        impl=list_proposals_tool,
        tier=Tier.READ,
    ),
    Tool(
        name="run_research_cycle",
        description=(
            "Kick off a full self-improvement cycle (Karpathy-autoresearch style): compute "
            "metrics, pick focus, generate candidate edits, evaluate, write the best one as "
            "a proposal for Liam to approve. Use sparingly (hourly max). "
            "Pass dry_run=true to generate candidates without writing a proposal."
        ),
        input_schema={
            "type": "object",
            "properties": {"dry_run": {"type": "boolean", "default": False}},
        },
        impl=run_research_cycle,
        tier=Tier.SEND,
    ),
]


def register(registry: Registry, task_runner: TaskRunner) -> None:
    registry.add_group("evolution")
    for t in TOOLS:
        registry.add(t)
