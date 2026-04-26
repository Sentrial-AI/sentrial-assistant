"""
Background prefetch of user context — calendar, todos, emails, reminders.

Called when voice mode opens (parallel with mic setup, so cache is hot by
the time the user finishes their first sentence) and after every turn ends
(so the next turn benefits from updated state). Each source is fetched in
parallel via asyncio.gather; one source failing doesn't affect the rest.

The cache (core/context_cache) is the only place results are stored. The
agent reads from it via evolution/retrieval's live_context block.

Design notes:
- We call the MCP tool functions DIRECTLY (e.g. notion.list_tasks). Same
  auth + same logic the agent's tool path uses, just bypassing the LLM
  decision so we can prefetch eagerly.
- Sources only fire if their underlying integration is configured (e.g.
  Notion API key present, Google OAuth connected). No noisy failures on
  fresh installs.
- TTLs are tuned per source: calendar's stable across the day, todos and
  email change more often, reminders are local + cheap.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sentrial.core import context_cache, secrets as kc

log = logging.getLogger(__name__)


# Cache key constants — referenced by retrieval.py too.
KEY_TODOS = "todos"
KEY_CALENDAR_TODAY = "calendar_today"
KEY_RECENT_EMAIL = "recent_email"
KEY_REMINDERS = "reminders"


# TTLs in seconds. These are upper bounds; prefetch fires more often than
# this in practice (voice-mode start + post-turn) so cache stays warm.
TTL_TODOS = 180          # ~3 min — todos move around, but not constantly
TTL_CALENDAR = 600       # ~10 min — today's events rarely change
TTL_EMAIL = 180          # ~3 min — keeps "any new emails" fast
TTL_REMINDERS = 180      # ~3 min — local Mac reminders are cheap


def _google_authed() -> bool:
    try:
        from sentrial.core import google_oauth as gauth
        return gauth.is_connected()
    except Exception:  # noqa: BLE001
        return False


def _notion_configured() -> bool:
    return bool(kc.get("notion_api_key") and kc.get("notion_tasks_db_id"))


# ---------------- per-source prefetchers ----------------

async def _prefetch_todos() -> None:
    if not _notion_configured():
        return
    try:
        from sentrial.mcps.notion.server import list_tasks
        res = await asyncio.wait_for(list_tasks({"status": "open"}), timeout=8.0)
        if isinstance(res, dict) and "tasks" in res:
            tasks = res["tasks"][:15]
            await context_cache.cache().put(
                KEY_TODOS, tasks, TTL_TODOS, source="notion.list_tasks", kind="todos",
            )
        elif isinstance(res, dict) and "error" in res:
            await context_cache.cache().put(
                KEY_TODOS, None, TTL_TODOS, source="notion.list_tasks",
                kind="todos", error=str(res["error"])[:200],
            )
    except Exception as e:  # noqa: BLE001
        log.warning("prefetch todos failed: %s", e)


async def _prefetch_calendar_today() -> None:
    if not _google_authed():
        return
    try:
        from sentrial.mcps.calendar.server import list_upcoming_events
        # Window: now → end of tomorrow, so "what's next" and "what's tomorrow"
        # both hit the cache.
        now = datetime.now(timezone.utc)
        end = (now + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        res = await asyncio.wait_for(
            list_upcoming_events({
                "calendar_id": "primary",
                "time_min": now.isoformat(),
                "time_max": end.isoformat(),
                "max_results": 12,
            }),
            timeout=8.0,
        )
        if isinstance(res, dict) and "events" in res:
            await context_cache.cache().put(
                KEY_CALENDAR_TODAY, res["events"], TTL_CALENDAR,
                source="calendar.list_upcoming_events", kind="calendar",
            )
        elif isinstance(res, dict) and "error" in res:
            await context_cache.cache().put(
                KEY_CALENDAR_TODAY, None, TTL_CALENDAR,
                source="calendar.list_upcoming_events", kind="calendar",
                error=str(res["error"])[:200],
            )
    except Exception as e:  # noqa: BLE001
        log.warning("prefetch calendar failed: %s", e)


async def _prefetch_recent_email() -> None:
    if not _google_authed():
        return
    try:
        from sentrial.mcps.gmail.server import list_emails
        res = await asyncio.wait_for(
            list_emails({"max_results": 6, "query": "is:unread newer_than:2d"}),
            timeout=8.0,
        )
        if isinstance(res, dict) and "emails" in res:
            await context_cache.cache().put(
                KEY_RECENT_EMAIL, res["emails"], TTL_EMAIL,
                source="gmail.list_emails", kind="email",
            )
        elif isinstance(res, dict) and "error" in res:
            await context_cache.cache().put(
                KEY_RECENT_EMAIL, None, TTL_EMAIL,
                source="gmail.list_emails", kind="email",
                error=str(res["error"])[:200],
            )
    except Exception as e:  # noqa: BLE001
        log.warning("prefetch email failed: %s", e)


async def _prefetch_reminders() -> None:
    # Mac-only path — skip on Railway (no AppleScript).
    if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"):
        return
    try:
        from sentrial.mcps.reminders.server import list_reminders
        res = await asyncio.wait_for(list_reminders({"status": "open"}), timeout=6.0)
        if isinstance(res, dict) and "reminders" in res:
            await context_cache.cache().put(
                KEY_REMINDERS, res["reminders"][:15], TTL_REMINDERS,
                source="reminders.list_reminders", kind="reminders",
            )
        elif isinstance(res, dict) and "error" in res:
            await context_cache.cache().put(
                KEY_REMINDERS, None, TTL_REMINDERS,
                source="reminders.list_reminders", kind="reminders",
                error=str(res["error"])[:200],
            )
    except Exception as e:  # noqa: BLE001
        log.warning("prefetch reminders failed: %s", e)


# ---------------- public API ----------------

async def prewarm_all() -> dict[str, Any]:
    """Fire every applicable source in parallel. Returns a snapshot of the
    cache state after prewarm completes. Safe to call as often as you like —
    prefetchers no-op when their integration isn't configured."""
    await asyncio.gather(
        _prefetch_todos(),
        _prefetch_calendar_today(),
        _prefetch_recent_email(),
        _prefetch_reminders(),
        return_exceptions=True,
    )
    return {"cache": context_cache.cache().snapshot()}


def schedule_background_refresh() -> asyncio.Task | None:
    """Fire-and-forget prewarm — used by the agent loop to keep the cache
    warm across turns without blocking the reply. Returns the task so the
    caller can keep a reference (Python GCs unawaited tasks)."""
    try:
        loop = asyncio.get_event_loop()
        return loop.create_task(prewarm_all(), name="sentrial_prewarm")
    except Exception as e:  # noqa: BLE001
        log.warning("schedule_background_refresh failed (ignored): %s", e)
        return None
