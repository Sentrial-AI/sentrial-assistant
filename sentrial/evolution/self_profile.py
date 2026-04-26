"""
Sentrial's self-profile — the assistant's own evolving identity.

Mirrors the shape of evolution/profile.py (which is about Liam) but stores
Sentrial's persona, communication style, accumulated memories, and growth
log. The retrieval layer pulls a compact summary into every turn's system
prompt so personality stays coherent across conversations and grows as
Sentrial learns what works.

Stored at /data/evolution/self_profile.yaml — diff-reviewable, easy to
inspect or roll back. First-run seeds from base/self_profile.yaml.

Public API:
  load() / save() — full document round-trip
  summary_for_prompt() — compact identity block to inject into system prompt
  add_memory(summary, why) — record a meaningful moment (capped, FIFO)
  add_growth(lesson, kind) — record something to do or stop doing
  observe_trait(trait) — append/strengthen a persona trait
  bump_stats(turns_delta, new_conversation) — keep counters current

Writes are tolerant: a corrupt YAML file falls back to the base template
rather than crashing the agent. All writes go through the same atomic
write-temp-then-rename pattern as profile.py.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from sentrial.core import paths

log = logging.getLogger(__name__)

BASE_PROFILE = Path(__file__).parent / "base" / "self_profile.yaml"
SCHEMA_VERSION = 1

MAX_MEMORIES = 40       # rolling window — newest wins
MAX_GROWTH = 60         # rolling window — same
MAX_TRAITS = 24         # cap to keep system prompt slim
MAX_PROMPT_CHARS = 1400 # the summary block has a hard ceiling


# ---------- file I/O ----------

def _path() -> Path:
    p = paths.data_dir() / "evolution" / "self_profile.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_exists() -> Path:
    p = _path()
    if not p.exists():
        shutil.copy(BASE_PROFILE, p)
    return p


def _load_base() -> dict:
    return yaml.safe_load(BASE_PROFILE.read_text())


def load() -> dict:
    p = _ensure_exists()
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        log.warning("self_profile corrupt — restoring from base: %s", e)
        shutil.copy(BASE_PROFILE, p)
        data = yaml.safe_load(p.read_text()) or {}
    base = _load_base()
    _merge_missing(data, base)
    return data


_SUMMARY_CACHE: str | None = None
_SUMMARY_CACHE_AT: float = 0.0
_SUMMARY_CACHE_TTL_S: float = 30.0


def _invalidate_summary_cache() -> None:
    global _SUMMARY_CACHE, _SUMMARY_CACHE_AT
    _SUMMARY_CACHE = None
    _SUMMARY_CACHE_AT = 0.0


def save(profile: dict) -> None:
    p = _ensure_exists()
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(profile, sort_keys=False, allow_unicode=True))
    tmp.replace(p)
    _invalidate_summary_cache()


def _merge_missing(target: dict, base: dict) -> None:
    for k, v in base.items():
        if k not in target:
            target[k] = v
        elif isinstance(v, dict) and isinstance(target.get(k), dict):
            _merge_missing(target[k], v)


# ---------- mutators ----------

def add_memory(summary: str, why_it_matters: str = "") -> None:
    """Record a moment Sentrial chose to remember. Newest wins; capped at MAX_MEMORIES."""
    summary = (summary or "").strip()
    if not summary:
        return
    p = load()
    mems = list(p.get("memories") or [])
    mems.append({
        "at": _now(),
        "summary": summary[:280],
        "why_it_matters": (why_it_matters or "").strip()[:200],
    })
    if len(mems) > MAX_MEMORIES:
        mems = mems[-MAX_MEMORIES:]
    p["memories"] = mems
    save(p)


def add_growth(lesson: str, kind: str = "do") -> None:
    """Append a self-improvement lesson. kind is 'do' (keep doing) or 'stop'."""
    lesson = (lesson or "").strip()
    if not lesson:
        return
    if kind not in ("do", "stop"):
        kind = "do"
    p = load()
    gs = list(p.get("growth") or [])
    # Dedup near-identical entries — same kind + same lesson lowercase.
    key = (kind, lesson.lower())
    if any((g.get("kind"), (g.get("lesson") or "").lower()) == key for g in gs):
        return
    gs.append({"at": _now(), "lesson": lesson[:240], "kind": kind})
    if len(gs) > MAX_GROWTH:
        gs = gs[-MAX_GROWTH:]
    p["growth"] = gs
    save(p)


def observe_trait(trait: str, source: str = "distilled") -> None:
    """Strengthen an existing trait or add a new one. Caps total count."""
    trait = (trait or "").strip()
    if not trait:
        return
    p = load()
    traits = list(p.get("persona_traits") or [])
    key = trait.lower()
    for t in traits:
        if (t.get("trait") or "").lower() == key:
            t["evidence_count"] = int(t.get("evidence_count") or 0) + 1
            t["last_seen"] = _now()
            t["source"] = source
            save(p)
            return
    traits.append({
        "trait": trait[:200],
        "evidence_count": 1,
        "last_seen": _now(),
        "source": source,
    })
    # If we're over cap, drop the lowest-evidence entry (not the newest seed).
    if len(traits) > MAX_TRAITS:
        traits.sort(key=lambda t: int(t.get("evidence_count") or 0), reverse=True)
        traits = traits[:MAX_TRAITS]
    p["persona_traits"] = traits
    save(p)


def bump_stats(turns_delta: int = 1, new_conversation: bool = False) -> None:
    """Update counters after a turn or at the start of a new conversation."""
    p = load()
    stats = dict(p.get("stats") or {})
    stats["total_turns"] = int(stats.get("total_turns") or 0) + max(0, turns_delta)
    if new_conversation:
        stats["total_conversations"] = int(stats.get("total_conversations") or 0) + 1
    if not stats.get("first_turn_at"):
        stats["first_turn_at"] = _now()
    stats["last_turn_at"] = _now()
    p["stats"] = stats
    save(p)


# ---------- read views ----------

def summary_for_prompt() -> str:
    """A compact identity block for the system prompt. Hard-capped at
    MAX_PROMPT_CHARS so it never blows up token budget. Composition order is
    deliberate: identity → traits → values → recent growth → recent memory.
    Recent items dominate so personality reflects current Sentrial.

    Cached in-process for 30s — the agent fires this on every turn and the
    YAML parse + render adds 5-10ms per call. add_memory/add_growth/etc.
    invalidate the cache via save() so updates show up immediately.
    """
    import time as _time
    global _SUMMARY_CACHE, _SUMMARY_CACHE_AT
    now = _time.monotonic()
    if _SUMMARY_CACHE is not None and (now - _SUMMARY_CACHE_AT) < _SUMMARY_CACHE_TTL_S:
        return _SUMMARY_CACHE

    try:
        p = load()
    except Exception as e:  # noqa: BLE001
        log.warning("self_profile load failed (returning empty): %s", e)
        return ""

    lines: list[str] = []

    ident = p.get("identity") or {}
    name = ident.get("name") or "Sentrial"
    role = ident.get("role") or ""
    voice = ident.get("voice_id") or ""
    lines.append(f"You are {name} — {role}.")
    if voice:
        lines.append(f"You speak as voice '{voice}'.")

    traits = sorted(
        (p.get("persona_traits") or []),
        key=lambda t: int(t.get("evidence_count") or 0),
        reverse=True,
    )[:8]
    if traits:
        lines.append("")
        lines.append("Who you are (lived, not authored):")
        for t in traits:
            lines.append(f"  - {t.get('trait')}")

    vals = (p.get("values") or [])[:6]
    if vals:
        lines.append("")
        lines.append("What you weight:")
        for v in vals:
            lines.append(f"  - {v}")

    avoided = (p.get("communication_style") or {}).get("avoided_phrases") or []
    if avoided:
        lines.append("")
        lines.append("Phrases you never use: " + ", ".join(f'"{a}"' for a in avoided[:8]))

    growth = (p.get("growth") or [])[-8:]
    if growth:
        lines.append("")
        lines.append("Things you've learned (recent → keep going):")
        for g in growth:
            kind = g.get("kind") or "do"
            verb = "DO" if kind == "do" else "STOP"
            lines.append(f"  - [{verb}] {g.get('lesson')}")

    mems = (p.get("memories") or [])[-5:]
    if mems:
        lines.append("")
        lines.append("Recent moments worth remembering:")
        for m in mems:
            lines.append(f"  - {m.get('summary')}")

    body = "\n".join(lines).strip()
    if len(body) > MAX_PROMPT_CHARS:
        body = body[:MAX_PROMPT_CHARS] + "\n…[truncated]"
    _SUMMARY_CACHE = body
    _SUMMARY_CACHE_AT = now
    return body
