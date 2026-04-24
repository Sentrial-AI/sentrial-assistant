"""
Lessons library — small atomic directives learned from interactions.

A lesson is a single short rule ("for Anne-Britt: never auto-send emails").
Stored one-per-file as JSON at /data/evolution/lessons/<id>.json so each is
independently reversible and auditable.

Lessons are NOT baked into the system prompt. Instead the pre-turn retriever
ranks them by relevance to the current user message + active context and
injects the top N. That keeps prompt size bounded and lets lessons grow
without a trim cycle.

Relevance score (cheap, no embeddings):
    overlap(message_tokens, lesson.tags) * 2
  + overlap(message_tokens, lesson.keywords)
  + recency_boost * 0.3
  + confidence * 0.5
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sentrial.core import audit, paths

log = logging.getLogger(__name__)

MAX_LESSONS_PER_TURN = 6
MIN_CONFIDENCE = 0.2
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}")


def _dir() -> Path:
    p = paths.data_dir() / "evolution" / "lessons"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in TOKEN_RE.findall(text or "")}


# ---- CRUD ----

def create(
    rule: str,
    tags: list[str] | None = None,
    keywords: list[str] | None = None,
    evidence: list[dict] | None = None,
    confidence: float = 0.3,
    source: str = "distillation",
) -> dict:
    """
    Mint a new lesson. `source` is "distillation" (auto), "user" (explicit),
    or "proposal" (came through the proposals approval flow). Confidence and
    source together determine whether the retriever uses it.
    """
    lid = f"lsn_{uuid.uuid4().hex[:10]}"
    doc = {
        "id": lid,
        "rule": rule.strip(),
        "tags": [t.lower() for t in (tags or [])],
        "keywords": [k.lower() for k in (keywords or _auto_keywords(rule))],
        "evidence": evidence or [],
        "confidence": max(0.0, min(1.0, float(confidence))),
        "source": source,
        "created_at": _now(),
        "updated_at": _now(),
        "status": "active",
        "hit_count": 0,
        "last_retrieved_at": None,
    }
    (_dir() / f"{lid}.json").write_text(json.dumps(doc, indent=2))
    audit.log(
        "sentrial", "lesson_created", 1,
        args={"id": lid, "source": source, "tags": doc["tags"]},
        result=rule[:200],
    )
    return doc


def _auto_keywords(rule: str) -> list[str]:
    return sorted({t for t in _tokens(rule) if t not in _STOPWORDS})[:8]


def get(lesson_id: str) -> dict | None:
    p = _dir() / f"{lesson_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def list_all(status: str | None = "active") -> list[dict]:
    out: list[dict] = []
    for f in sorted(_dir().glob("lsn_*.json")):
        try:
            d = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if status and d.get("status") != status:
            continue
        out.append(d)
    return out


def update(lesson_id: str, **fields) -> dict | None:
    d = get(lesson_id)
    if not d:
        return None
    for k, v in fields.items():
        if k in ("id", "created_at"):
            continue
        d[k] = v
    d["updated_at"] = _now()
    (_dir() / f"{lesson_id}.json").write_text(json.dumps(d, indent=2))
    return d


def retire(lesson_id: str, reason: str = "") -> bool:
    d = update(lesson_id, status="retired", retired_reason=reason)
    if d:
        audit.log("user", "lesson_retired", 1, args={"id": lesson_id}, result=reason[:200])
        return True
    return False


def reinforce(lesson_id: str, delta: float = 0.1) -> dict | None:
    d = get(lesson_id)
    if not d:
        return None
    d["confidence"] = min(1.0, float(d.get("confidence") or 0) + delta)
    return update(lesson_id, confidence=d["confidence"])


def weaken(lesson_id: str, delta: float = 0.1) -> dict | None:
    d = get(lesson_id)
    if not d:
        return None
    d["confidence"] = max(0.0, float(d.get("confidence") or 0) - delta)
    if d["confidence"] < 0.05:
        return update(lesson_id, status="retired", confidence=d["confidence"],
                      retired_reason="confidence collapsed")
    return update(lesson_id, confidence=d["confidence"])


# ---- retrieval ----

def retrieve_relevant(
    message: str,
    active_tags: list[str] | None = None,
    max_lessons: int = MAX_LESSONS_PER_TURN,
) -> list[dict]:
    """
    Score active lessons against the incoming message + active tags; return
    the top N above MIN_CONFIDENCE. Cheap token-overlap scoring — no model
    call, runs on every turn.
    """
    msg_toks = _tokens(message)
    active_set = {t.lower() for t in (active_tags or [])}
    now = datetime.now(timezone.utc)

    scored: list[tuple[float, dict]] = []
    for lesson in list_all(status="active"):
        if (lesson.get("confidence") or 0) < MIN_CONFIDENCE:
            continue
        tag_hits = len(active_set.intersection(lesson.get("tags") or []))
        kw_hits = len(msg_toks.intersection(lesson.get("keywords") or []))
        # Recency boost — exponential decay with 14d half-life.
        try:
            updated = datetime.fromisoformat(
                (lesson.get("updated_at") or "").replace("Z", "+00:00")
            )
            age_days = (now - updated).total_seconds() / 86400
            recency = 0.5 ** (age_days / 14)
        except ValueError:
            recency = 0.5
        score = (tag_hits * 2.0) + (kw_hits * 1.0) + (recency * 0.3) + (lesson["confidence"] * 0.5)
        if score > 0.6:
            scored.append((score, lesson))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [l for _, l in scored[:max_lessons]]

    # Mark retrieval hits so dead lessons can be aged out later.
    for lesson in top:
        lesson["hit_count"] = int(lesson.get("hit_count") or 0) + 1
        lesson["last_retrieved_at"] = _now()
        (_dir() / f"{lesson['id']}.json").write_text(json.dumps(lesson, indent=2))
    return top


def render_for_agent(lessons_list: list[dict]) -> str:
    """Turn retrieved lessons into a compact list the agent can read."""
    if not lessons_list:
        return ""
    bullets = [f"- {l['rule']}" for l in lessons_list]
    return "[learned lessons — apply when relevant]\n" + "\n".join(bullets)


# ---- housekeeping ----

def garbage_collect(max_lessons: int = 300) -> int:
    """Retire lessons with low confidence + no hits in 60d. Returns count retired."""
    now = datetime.now(timezone.utc)
    retired = 0
    lessons = list_all(status="active")
    if len(lessons) <= max_lessons:
        # Only prune obvious dead weight, not for space.
        for l in lessons:
            try:
                updated = datetime.fromisoformat(
                    (l.get("updated_at") or "").replace("Z", "+00:00")
                )
                age_days = (now - updated).total_seconds() / 86400
            except ValueError:
                continue
            if age_days > 60 and (l.get("hit_count") or 0) == 0 and (l.get("confidence") or 0) < 0.3:
                retire(l["id"], reason="stale+unused")
                retired += 1
        return retired
    # Space-pressure: retire the weakest first.
    lessons.sort(key=lambda l: (l.get("hit_count") or 0, l.get("confidence") or 0))
    for l in lessons[: len(lessons) - max_lessons]:
        retire(l["id"], reason="eviction")
        retired += 1
    return retired


_STOPWORDS = {
    "the","and","for","with","that","this","from","have","will","would","your","you",
    "can","are","was","were","has","had","but","not","any","all","some","its","it's",
    "they","them","their","what","when","where","which","who","how","why","just",
    "get","got","make","made","use","used","about","into","out","over","also",
    "one","two","very","really","sentrial","liam","please","thanks","should",
}
