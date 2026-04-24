"""
Per-task playbooks — learned recipes for recurring task kinds.

A playbook is a Markdown file at /data/evolution/playbooks/<slug>.md. It
describes how a specific task type should be handled: opener, structure,
tone hints, sign-off, what tools to prefer. The retriever detects the task
kind from the user's message + active context, picks the matching playbook,
and injects its content at turn-start.

Task detection is intentionally shallow: a keyword-triggered classifier that
falls back to None when nothing strong matches. We never guess — an unmatched
turn gets zero playbook, not a wrong one.

Playbooks go through the proposals system for edits, but creation is auto:
when distillation sees a repeating pattern (N turns of the same kind with
positive outcome), it proposes a new playbook.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sentrial.core import audit, paths

log = logging.getLogger(__name__)

SLUG_RE = re.compile(r"[^a-z0-9_-]")


def _dir() -> Path:
    p = paths.data_dir() / "evolution" / "playbooks"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _meta_path(slug: str) -> Path:
    return _dir() / f"{slug}.meta.json"


def _body_path(slug: str) -> Path:
    return _dir() / f"{slug}.md"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskKind:
    slug: str
    label: str
    trigger_phrases: tuple[str, ...]
    trigger_tags: tuple[str, ...] = ()

    def matches(self, message: str, context_tags: list[str] | None = None) -> float:
        """Return a match score in [0, 1]. 0 = not this kind, 1 = very likely."""
        msg = (message or "").lower()
        phrase_hits = sum(1 for p in self.trigger_phrases if p in msg)
        tag_hits = 0
        if context_tags:
            tag_set = {t.lower() for t in context_tags}
            tag_hits = len(tag_set.intersection(t.lower() for t in self.trigger_tags))
        if phrase_hits == 0 and tag_hits == 0:
            return 0.0
        # Each hit adds 0.4; cap at 1.
        return min(1.0, 0.4 * (phrase_hits + tag_hits))


# Built-in task kinds. Evolution may propose additions via the proposals flow.
BUILTIN_KINDS: tuple[TaskKind, ...] = (
    TaskKind("proposal", "Client proposal",
             ("proposal", "scope of work", "sow", "statement of work"),
             ("sales", "client")),
    TaskKind("audit", "Website / business audit",
             ("audit", "review the site", "feedback on the site", "site review"),
             ("discovery", "client")),
    TaskKind("demo", "Demo / prototype",
             ("demo", "prototype", "mockup", "quick build"),
             ("demo",)),
    TaskKind("followup_email", "Follow-up email",
             ("follow up", "follow-up", "draft a follow", "chase email"),
             ("email",)),
    TaskKind("cold_outreach", "Cold outreach",
             ("cold email", "cold outreach", "reach out to", "intro email"),
             ("email", "sales")),
    TaskKind("daily_brief", "Daily brief / planning",
             ("what's on my plate", "today's agenda", "plan for today", "what do i need"),
             ("planning",)),
    TaskKind("notion_update", "Notion task management",
             ("add to notion", "new task", "notion task", "todo for"),
             ("notion",)),
)


def classify(message: str, context_tags: list[str] | None = None) -> str | None:
    """Return the task-kind slug with the strongest match, or None."""
    best_score = 0.0
    best_slug: str | None = None
    for kind in BUILTIN_KINDS:
        s = kind.matches(message, context_tags)
        if s > best_score:
            best_score, best_slug = s, kind.slug
    # Threshold — below this we don't guess.
    if best_score < 0.4:
        return None
    return best_slug


# ---- CRUD ----

def _slugify(name: str) -> str:
    s = name.strip().lower().replace(" ", "_")
    return SLUG_RE.sub("", s)[:64]


def create_or_update(
    slug: str,
    label: str,
    body_md: str,
    source: str = "distillation",
    metadata: dict | None = None,
) -> dict:
    slug = _slugify(slug)
    _body_path(slug).write_text(body_md)
    meta = {
        "slug": slug,
        "label": label,
        "source": source,
        "created_at": _now(),
        "updated_at": _now(),
        "metadata": metadata or {},
    }
    prev_meta = read_meta(slug)
    if prev_meta:
        meta["created_at"] = prev_meta.get("created_at", meta["created_at"])
    _meta_path(slug).write_text(json.dumps(meta, indent=2))
    audit.log(
        "sentrial", "playbook_upserted", 1,
        args={"slug": slug, "source": source}, result=label[:200],
    )
    return meta


def read(slug: str) -> tuple[str | None, dict | None]:
    slug = _slugify(slug)
    bp, mp = _body_path(slug), _meta_path(slug)
    body = bp.read_text() if bp.exists() else None
    meta = None
    if mp.exists():
        try:
            meta = json.loads(mp.read_text())
        except json.JSONDecodeError:
            meta = None
    return body, meta


def read_meta(slug: str) -> dict | None:
    _, meta = read(slug)
    return meta


def list_all() -> list[dict]:
    out: list[dict] = []
    for f in sorted(_dir().glob("*.meta.json")):
        try:
            meta = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        slug = meta.get("slug") or f.stem.replace(".meta", "")
        body_p = _body_path(slug)
        meta["has_body"] = body_p.exists()
        meta["body_bytes"] = body_p.stat().st_size if body_p.exists() else 0
        out.append(meta)
    return out


def delete(slug: str) -> bool:
    slug = _slugify(slug)
    hit = False
    for p in (_body_path(slug), _meta_path(slug)):
        if p.exists():
            p.unlink()
            hit = True
    if hit:
        audit.log("user", "playbook_deleted", 2, args={"slug": slug})
    return hit


def retrieve_for_message(
    message: str, context_tags: list[str] | None = None,
) -> tuple[str | None, dict | None]:
    """Classify → return the playbook body + meta, or (None, None)."""
    slug = classify(message, context_tags)
    if not slug:
        return None, None
    body, meta = read(slug)
    return body, meta


def render_for_agent(body: str | None, meta: dict | None) -> str:
    """Compact agent-facing header wrapping the playbook body."""
    if not body or not meta:
        return ""
    return (
        f"[playbook — {meta.get('label', meta.get('slug', 'task'))}]\n"
        f"{body.strip()}"
    )
