"""
Pre-turn retrieval orchestrator.

Called at the start of every agent turn. Assembles a small context block that
the agent sees BEFORE the user message — replaces the static system prompt's
job of carrying preferences + aliases + lessons with a dynamic, per-turn view.

Composition (in order, capped so the whole block stays small):
  1. user profile summary (learned preferences, shorthand, active focus)
  2. entity cards for things mentioned in the user message (KG)
  3. matching playbook for detected task kind (if any)
  4. top-N relevant lessons (active + confident enough)

All pieces are no-ops when their source has no data, so on a fresh install the
agent still runs with an empty preamble.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sentrial.evolution import kg, lessons, playbooks, profile

log = logging.getLogger(__name__)

MAX_BLOCK_CHARS = 3000   # safety cap on the full context block


@dataclass
class RetrievedContext:
    profile_summary: str
    entity_cards: str
    playbook: str
    lessons_block: str
    playbook_slug: str | None
    mentioned_entity_ids: list[str]

    def as_preamble(self) -> str:
        parts = [
            _wrap("user profile", self.profile_summary),
            self.entity_cards,       # already self-labeled
            self.playbook,           # already self-labeled
            self.lessons_block,      # already self-labeled
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

    return RetrievedContext(
        profile_summary=prof,
        entity_cards=cards,
        playbook=pb_block,
        lessons_block=lessons_block,
        playbook_slug=slug,
        mentioned_entity_ids=mentioned,
    )


def record_mentions(ctx: RetrievedContext, conversation_id: str | None) -> None:
    """Log KG mentions for retrieval stats (separate from `build` so tests stay pure)."""
    for eid in ctx.mentioned_entity_ids:
        try:
            kg.record_mention(eid, conversation_id)
        except Exception:  # noqa: BLE001
            pass
