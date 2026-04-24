"""
Per-interaction reflection. Fast, cheap, runs after every conversation with notable
signal (edits, denials, job feedback). Distills a one-sentence "lesson" that gets
stored in memory and injected into future prompts via recall_relevant.

This is the shorter, real-time counterpart to evolution/loop.py's overnight research.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from anthropic import AsyncAnthropic

from sentrial.core import memory
from sentrial.core import secrets as kc

log = logging.getLogger(__name__)

MODEL_FAST = "claude-haiku-4-5-20251001"
LESSON_SCOPE = "lesson"


async def distill_lesson(
    conversation_snippet: str,
    trigger: str,
    context_hint: str = "",
) -> str | None:
    """
    Given a notable conversation moment, extract a one-sentence lesson. Returns
    None if the snippet doesn't merit a lesson (noise).

    `trigger` — what caused the reflection: 'edit', 'denial', 'job_modified', etc.
    """
    try:
        client = AsyncAnthropic(api_key=kc.require("anthropic_api_key"))
    except Exception as e:  # noqa: BLE001
        log.warning("no api key for reflection: %s", e)
        return None

    prompt = (
        "You are a reflection agent. Read this conversation snippet and the trigger. "
        "If there's a reusable lesson about Liam's preferences, write ONE sentence "
        "starting with a verb (e.g., 'Use shorter scope previews for proposals...'). "
        "If there's no meaningful lesson, output exactly: SKIP.\n\n"
        f"# trigger\n{trigger}\n\n"
        f"# context hint\n{context_hint}\n\n"
        f"# snippet\n{conversation_snippet}\n\n"
        "Lesson (or SKIP):"
    )
    try:
        resp = await client.messages.create(
            model=MODEL_FAST, max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("lesson distill failed: %s", e)
        return None

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    if not text or text.upper().startswith("SKIP"):
        return None

    lesson_key = f"l-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    memory.remember(LESSON_SCOPE, lesson_key, {
        "text": text,
        "trigger": trigger,
        "context": context_hint[:200],
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return text


def recall_relevant(context: str, limit: int = 5) -> list[str]:
    """Keyword-match recall — replace with embeddings in phase 2."""
    context_lower = context.lower()
    keywords = {w for w in context_lower.split() if len(w) > 4}
    lessons = memory.recall_scope(LESSON_SCOPE)

    scored: list[tuple[int, str, str]] = []
    for key, val in lessons.items():
        text = val.get("text", "") if isinstance(val, dict) else str(val)
        ctx = val.get("context", "") if isinstance(val, dict) else ""
        haystack = (text + " " + ctx).lower()
        overlap = sum(1 for kw in keywords if kw in haystack)
        if overlap > 0:
            scored.append((overlap, key, text))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, _, t in scored[:limit]]


def list_all_lessons() -> list[dict]:
    lessons = memory.recall_scope(LESSON_SCOPE)
    return [
        {"key": k, **(v if isinstance(v, dict) else {"text": str(v)})}
        for k, v in lessons.items()
    ]
