"""
Pre-turn retrieval orchestrator.

Called at the start of every agent turn. Assembles a small context block that
the agent sees BEFORE the user message — replaces the static system prompt's
job of carrying preferences + aliases + lessons with a dynamic, per-turn view.

Composition (in order, capped so the whole block stays small):
  1. live context — pre-fetched calendar/todos/email/reminders from cache
  2. user profile summary (learned preferences, shorthand, active focus)
  3. entity cards for things mentioned in the user message (KG)
  4. matching playbook for detected task kind (if any)
  5. top-N relevant lessons (active + confident enough)

The live_context block is what makes voice queries fast: instead of the LLM
calling list_tasks → wait → list_events → wait, it sees the answer already
in the prompt (cache hot from voice-mode-start prewarm). Mutating asks like
"remove that todo" turn into ONE tool call (the remove) instead of two.

All pieces are no-ops when their source has no data, so on a fresh install the
agent still runs with an empty preamble.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sentrial.evolution import kg, lessons, playbooks, profile

log = logging.getLogger(__name__)

MAX_BLOCK_CHARS = 3500   # safety cap on the full context block (raised for live ctx)
MAX_LIVE_CHARS = 1400    # ceiling for the live-context section specifically


@dataclass
class RetrievedContext:
    profile_summary: str
    entity_cards: str
    playbook: str
    lessons_block: str
    playbook_slug: str | None
    mentioned_entity_ids: list[str]
    live_context: str = ""

    def as_preamble(self) -> str:
        parts = [
            self.live_context,        # already self-labeled
            _wrap("user profile", self.profile_summary),
            self.entity_cards,        # already self-labeled
            self.playbook,            # already self-labeled
            self.lessons_block,       # already self-labeled
        ]
        body = "\n\n".join(p for p in parts if p).strip()
        if not body:
            return ""
        if len(body) > MAX_BLOCK_CHARS:
            body = body[:MAX_BLOCK_CHARS] + "\n…[truncated]"
        return body + "\n\n"


def _wrap(label: str, body: str) -> str:
    if not body:
        return ""
    return f"[{label}]\n{body}"


# ---------- live context (pre-fetched calendar/todos/email/reminders) ----------

def _fmt_when(iso: str | None) -> str:
    """Render a calendar event time as a short local-ish string. Best effort —
    if parsing fails, return the raw value."""
    if not iso:
        return ""
    try:
        # Calendar tools often emit Z-terminated ISO; tolerate both.
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        # "Mon 2:30pm" — short and unambiguous for voice context.
        return local.strftime("%a %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    except Exception:  # noqa: BLE001
        return str(iso)[:25]


def _format_todos(tasks: list) -> str:
    if not tasks:
        return "  (no open todos)"
    lines = []
    for t in tasks[:10]:
        if not isinstance(t, dict):
            continue
        title = (t.get("title") or t.get("name") or "untitled")[:90]
        due = t.get("due") or t.get("due_date")
        suffix = f" (due {_fmt_when(due)})" if due else ""
        tid = t.get("id") or t.get("task_id") or ""
        if tid:
            lines.append(f"  - {title}{suffix}  [id: {tid}]")
        else:
            lines.append(f"  - {title}{suffix}")
    return "\n".join(lines)


def _format_calendar(events: list) -> str:
    if not events:
        return "  (no events in window)"
    lines = []
    for ev in events[:8]:
        if not isinstance(ev, dict):
            continue
        title = (ev.get("summary") or ev.get("title") or "untitled")[:80]
        start = ev.get("start") or (ev.get("when") or {}).get("start")
        if isinstance(start, dict):
            start = start.get("dateTime") or start.get("date")
        eid = ev.get("id") or ""
        when = _fmt_when(start)
        prefix = f"  - {when}: {title}" if when else f"  - {title}"
        if eid:
            lines.append(f"{prefix}  [id: {eid}]")
        else:
            lines.append(prefix)
    return "\n".join(lines)


def _format_email(emails: list) -> str:
    if not emails:
        return "  (no recent unread)"
    lines = []
    for m in emails[:6]:
        if not isinstance(m, dict):
            continue
        subj = (m.get("subject") or "(no subject)")[:80]
        sender = (m.get("from") or m.get("sender") or "")[:40]
        mid = m.get("id") or ""
        head = f"{sender} — {subj}" if sender else subj
        lines.append(f"  - {head}" + (f"  [id: {mid}]" if mid else ""))
    return "\n".join(lines)


def _format_reminders(rems: list) -> str:
    if not rems:
        return "  (no open reminders)"
    lines = []
    for r in rems[:8]:
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or r.get("name") or "untitled")[:90]
        due = r.get("due") or r.get("due_date") or r.get("remind_at")
        suffix = f" (due {_fmt_when(due)})" if due else ""
        rid = r.get("id") or ""
        lines.append(f"  - {title}{suffix}" + (f"  [id: {rid}]" if rid else ""))
    return "\n".join(lines)


def _build_live_context() -> str:
    """Read the prefetch cache and render a compact [live context] block.
    Empty string if the cache hasn't been populated yet (cold start)."""
    try:
        from sentrial.core import context_cache, context_prefetch
    except Exception:  # noqa: BLE001
        return ""

    c = context_cache.cache()
    sections: list[str] = []

    todos = c.get_fresh(context_prefetch.KEY_TODOS)
    if todos and isinstance(todos.value, list):
        sections.append("Today's open todos (from cache):\n" + _format_todos(todos.value))

    cal = c.get_fresh(context_prefetch.KEY_CALENDAR_TODAY)
    if cal and isinstance(cal.value, list):
        sections.append("Calendar (next ~36h):\n" + _format_calendar(cal.value))

    email = c.get_fresh(context_prefetch.KEY_RECENT_EMAIL)
    if email and isinstance(email.value, list):
        sections.append("Unread email (last 2 days):\n" + _format_email(email.value))

    rem = c.get_fresh(context_prefetch.KEY_REMINDERS)
    if rem and isinstance(rem.value, list):
        sections.append("Mac reminders (open):\n" + _format_reminders(rem.value))

    if not sections:
        return ""

    body = "\n\n".join(sections)
    if len(body) > MAX_LIVE_CHARS:
        body = body[:MAX_LIVE_CHARS] + "\n…[truncated]"

    # The framing matters — tell the model these are AUTHORITATIVE current
    # values so it doesn't waste a tool call double-checking.
    header = (
        "You ALREADY have the user's current calendar, todos, email, and "
        "reminders below — they were just fetched. ANSWER FROM THIS DATA. "
        "Do NOT call list_tasks / list_events / list_emails to look them up "
        "again. Use the IDs shown when you need to act on a specific item."
    )
    return f"[live context]\n{header}\n\n{body}"


def build_lite(_user_message: str) -> RetrievedContext:
    """Voice-mode retrieval. Skips lessons, playbooks, and KG card lookups —
    those are valuable for chat but add ~50-150ms of pre-LLM latency and
    100-400 tokens of input that voice doesn't actually need (live_context
    already carries the actionable data, and voice replies are too short to
    benefit from playbook scaffolding).

    Keeps: profile summary (terseness preference, focus, etc.) + live context.
    Both are basically free — profile is in-memory cached, live context is
    pre-fetched. Net effect: voice turns get to the LLM call ~100-300ms
    sooner.

    The `_user_message` arg is kept for signature parity with build() so the
    caller can swap implementations without changing the call site."""
    try:
        prof = profile.summary_for_agent()
    except Exception as e:  # noqa: BLE001
        log.warning("profile summary failed: %s", e)
        prof = ""
    try:
        live = _build_live_context()
    except Exception as e:  # noqa: BLE001
        log.warning("live context build failed: %s", e)
        live = ""
    return RetrievedContext(
        profile_summary=prof,
        entity_cards="",
        playbook="",
        lessons_block="",
        playbook_slug=None,
        mentioned_entity_ids=[],
        live_context=live,
    )


def build(user_message: str, active_tags: list[str] | None = None) -> RetrievedContext:
    # Profile.
    try:
        prof = profile.summary_for_agent()
    except Exception as e:  # noqa: BLE001
        log.warning("profile summary failed: %s", e)
        prof = ""

    # KG entity cards for things mentioned in the message.
    try:
        cards = kg.cards_for_text(user_message, max_cards=4)
        mentioned = [e["id"] for e in kg.mention_index(user_message)]
    except Exception as e:  # noqa: BLE001
        log.warning("kg lookup failed: %s", e)
        cards, mentioned = "", []

    # Playbook for the detected task kind.
    slug = None
    pb_block = ""
    try:
        body, meta = playbooks.retrieve_for_message(user_message, active_tags)
        if body and meta:
            slug = meta.get("slug")
            pb_block = playbooks.render_for_agent(body, meta)
    except Exception as e:  # noqa: BLE001
        log.warning("playbook lookup failed: %s", e)

    # Lessons by relevance.
    try:
        tags = list(active_tags or [])
        if slug:
            tags.append(f"task:{slug}")
        for ent in mentioned[:3]:
            tags.append(f"entity:{ent}")
        relevant = lessons.retrieve_relevant(user_message, active_tags=tags)
        lessons_block = lessons.render_for_agent(relevant)
    except Exception as e:  # noqa: BLE001
        log.warning("lessons retrieval failed: %s", e)
        lessons_block = ""

    # Live context (pre-fetched calendar/todos/email/reminders). Cheap to
    # build — just reads the in-memory cache. Empty string on cold start.
    try:
        live = _build_live_context()
    except Exception as e:  # noqa: BLE001
        log.warning("live context build failed: %s", e)
        live = ""

    return RetrievedContext(
        profile_summary=prof,
        entity_cards=cards,
        playbook=pb_block,
        lessons_block=lessons_block,
        playbook_slug=slug,
        mentioned_entity_ids=mentioned,
        live_context=live,
    )


def record_mentions(ctx: RetrievedContext, conversation_id: str | None) -> None:
    """Log KG mentions for retrieval stats (separate from `build` so tests stay pure)."""
    for eid in ctx.mentioned_entity_ids:
        try:
            kg.record_mention(eid, conversation_id)
        except Exception:  # noqa: BLE001
            pass
