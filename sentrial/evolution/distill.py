"""
Post-turn distillation — the learning hook.

After each turn ends, we check for signal and extract at most one of:
  - a profile observation (preference / vocabulary / schedule)
  - a lesson (atomic rule)
  - a KG entity (new person / project / client)
  - a playbook candidate (when a task-kind repeats with positive outcome)

Safe-by-default posture:
  - Most outputs are LOW-confidence on creation; integrity watchdog promotes
    over time if they keep agreeing with new observations.
  - High-impact surfaces (playbook creation, profile preference flips) go
    through the proposals system instead of auto-apply.
  - Explicit "actually / no / change" user turns are STRONG signal; implicit
    acceptance is weak signal.

Runs asynchronously from the agent turn loop so it never blocks a reply.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic

from sentrial.core import audit, secrets
from sentrial.evolution import kg, lessons, playbooks, profile, self_profile

log = logging.getLogger(__name__)

DISTILL_MODEL = "claude-haiku-4-5-20251001"   # fast + cheap — we run on every turn
MAX_TOKENS = 1024

EDIT_MARKERS = re.compile(
    r"\b(actually|instead|no,?\s*(?:can|could|would)|change\s+(?:it|that)|"
    r"redo|wrong|not\s+what|rewrite|shorter|longer|tighter|"
    r"i\s+prefer|don'?t\s+(?:like|want))\b",
    re.IGNORECASE,
)


@dataclass
class DistilledUpdate:
    profile_changes: list[dict]   # list of {path, value, weight, reason}
    new_lessons: list[dict]       # list of {rule, tags, keywords, confidence}
    kg_upserts: list[dict]        # list of {type, name, attrs, aliases, confidence}
    playbook_candidate: dict | None  # {slug, label, body_md, metadata}
    raw_signal: str               # "strong" | "weak" | "none"
    # Sentrial self-observations — these accrete in evolution/self_profile.yaml
    # and shape personality across conversations.
    self_memories: list[dict]     # list of {summary, why_it_matters}
    self_growth: list[dict]       # list of {lesson, kind: "do"|"stop"}
    self_traits: list[str]        # list of trait strings

    def summary(self) -> dict[str, Any]:
        return {
            "profile": len(self.profile_changes),
            "lessons": len(self.new_lessons),
            "kg": len(self.kg_upserts),
            "playbook": bool(self.playbook_candidate),
            "self_mem": len(self.self_memories),
            "self_growth": len(self.self_growth),
            "self_trait": len(self.self_traits),
            "signal": self.raw_signal,
        }


# ---- public API ----

async def distill_turn(
    user_message: str,
    assistant_reply: str,
    prev_assistant: str | None = None,
    conversation_id: str | None = None,
    playbook_slug: str | None = None,
) -> DistilledUpdate:
    """
    Run a post-turn distillation. `prev_assistant` is Sentrial's previous reply
    in the same conversation (or None if this was the first turn); used to
    detect when `user_message` is a correction.
    """
    signal = _classify_signal(user_message, prev_assistant)
    if signal == "none":
        return DistilledUpdate([], [], [], None, signal, [], [], [])

    try:
        api_key = secrets.require("anthropic_api_key")
    except Exception:  # noqa: BLE001
        return DistilledUpdate([], [], [], None, signal, [], [], [])

    client = AsyncAnthropic(api_key=api_key)

    prompt = _build_prompt(
        user_message=user_message,
        assistant_reply=assistant_reply,
        prev_assistant=prev_assistant,
        signal=signal,
        playbook_slug=playbook_slug,
    )
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=DISTILL_MODEL, max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=15,
        )
    except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
        log.warning("distill model call failed: %s", e)
        return DistilledUpdate([], [], [], None, signal, [], [], [])

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    parsed = _parse_json(text)
    if not isinstance(parsed, dict):
        return DistilledUpdate([], [], [], None, signal, [], [], [])

    update = _coerce_update(parsed, signal)
    _apply(update, conversation_id=conversation_id, signal=signal)
    audit.log(
        "sentrial", "distill", 1,
        args={"signal": signal, **update.summary()},
        result="",
    )
    return update


# ---- internals ----

def _classify_signal(user_message: str, prev_assistant: str | None) -> str:
    """strong = explicit correction; weak = neutral follow-up; none = nothing worth learning.

    Sentrial-self learning needs distill to fire on more turns, not fewer —
    even a casual "thanks" is signal about tone and pacing. So the bar for
    'weak' is permissive: anything beyond a trivial 1-2 word ack qualifies."""
    if not user_message or len(user_message.strip()) < 3:
        return "none"
    if prev_assistant and EDIT_MARKERS.search(user_message):
        return "strong"
    # Anything that's clearly more than a stop-word ack is worth distilling
    # for self-observations even if there's no profile/KG update to make.
    stripped = user_message.strip().lower()
    if stripped in {"ok", "okay", "k", "yep", "yes", "no", "thanks", "thx", "got it"}:
        return "none"
    return "weak"


def _build_prompt(
    *, user_message: str, assistant_reply: str,
    prev_assistant: str | None, signal: str,
    playbook_slug: str | None,
) -> str:
    return f"""You distill small durable learnings from a single chat turn for Sentrial — Liam's personal assistant.

Signal: {signal}  (strong = user corrected / contradicted the assistant; weak = neutral follow-up)

Previous assistant reply (may be null if none):
{prev_assistant or "(no prior turn)"}

User message:
{user_message}

Assistant reply:
{assistant_reply}

{"Detected task kind: " + playbook_slug if playbook_slug else "No task kind detected."}

Return ONLY a JSON object with these keys (each may be empty):
{{
  "profile_changes": [
    {{"path": "preferences.response_terseness", "value": "terse", "weight": 0.4, "reason": "..."}}
  ],
  "lessons": [
    {{"rule": "short rule", "tags": ["client:name", "email"], "keywords": ["..."], "confidence": 0.3}}
  ],
  "kg_upserts": [
    {{"type": "client|project|person|company|deal", "name": "Canonical Name",
      "attrs": {{"role": "...", "notes": "..."}},
      "aliases": ["Nickname"], "confidence": 0.5}}
  ],
  "playbook_candidate": null,

  "self_memories": [
    {{"summary": "what just happened (one sentence, first-person — 'Liam asked X, I answered Y')",
      "why_it_matters": "why this is worth Sentrial remembering"}}
  ],
  "self_growth": [
    {{"lesson": "specific behavior to repeat or stop, written as imperative",
      "kind": "do | stop"}}
  ],
  "self_traits": [
    "single phrase describing a personality trait Sentrial just exhibited or that Liam reinforced"
  ]
}}

Rules:
- For signal=strong, prefer adding a lesson or a profile_change with weight 0.4.
- For signal=weak, prefer kg_upserts for new names/entities; add lessons only if the message stated a preference ("I prefer X", "always do Y").
- NEVER emit a profile_change unless you're reasonably sure which field it belongs to.
- NEVER emit a lesson that restates what's already in the system prompt.
- Lessons must be SHORT (one sentence, imperative or declarative).
- Only emit playbook_candidate if the message clearly described a recurring task type NOT in: proposal, audit, demo, followup_email, cold_outreach, daily_brief, notion_update.

Self-observation rules (these power Sentrial's evolving identity — DO emit them when warranted):
- self_memories: emit ONE entry only when something genuinely worth remembering happened — a milestone, a new project mention, a strong reaction, an emotional moment, a goal stated. Skip routine Q&A.
- self_growth: emit when the user corrected Sentrial (kind: "stop") OR when Sentrial chose a noticeably good approach the user accepted (kind: "do"). Both are precious. Skip if the turn was unremarkable.
- self_traits: emit at most one when a clear personality moment happened — Sentrial was decisive, or witty, or appropriately tender, or refused to hedge. These accrete; don't repeat traits that obviously already exist.
- Conservative: when uncertain, emit fewer items. Empty arrays are fine."""


def _parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _coerce_update(parsed: dict, signal: str) -> DistilledUpdate:
    profile_changes = []
    for p in parsed.get("profile_changes") or []:
        if not isinstance(p, dict) or "path" not in p or "value" not in p:
            continue
        weight = float(p.get("weight") or (0.4 if signal == "strong" else 0.1))
        profile_changes.append({
            "path": str(p["path"]),
            "value": p["value"],
            "weight": max(0.0, min(1.0, weight)),
            "reason": str(p.get("reason") or "")[:200],
        })

    new_lessons = []
    for ll in parsed.get("lessons") or []:
        if not isinstance(ll, dict) or not ll.get("rule"):
            continue
        new_lessons.append({
            "rule": str(ll["rule"])[:300],
            "tags": [str(t) for t in (ll.get("tags") or [])][:8],
            "keywords": [str(k) for k in (ll.get("keywords") or [])][:12],
            "confidence": max(0.0, min(1.0, float(ll.get("confidence") or 0.3))),
        })

    kg_upserts = []
    for u in parsed.get("kg_upserts") or []:
        if not isinstance(u, dict) or not u.get("name") or not u.get("type"):
            continue
        kg_upserts.append({
            "type": str(u["type"])[:40],
            "name": str(u["name"])[:120],
            "attrs": u.get("attrs") or {},
            "aliases": [str(a) for a in (u.get("aliases") or [])][:8],
            "confidence": max(0.0, min(1.0, float(u.get("confidence") or 0.5))),
        })

    pb = parsed.get("playbook_candidate")
    playbook_candidate = None
    if isinstance(pb, dict) and pb.get("slug") and pb.get("body_md"):
        playbook_candidate = {
            "slug": str(pb["slug"])[:64],
            "label": str(pb.get("label") or pb["slug"])[:80],
            "body_md": str(pb["body_md"])[:4000],
            "metadata": pb.get("metadata") or {},
        }

    self_memories = []
    for m in parsed.get("self_memories") or []:
        if not isinstance(m, dict) or not m.get("summary"):
            continue
        self_memories.append({
            "summary": str(m["summary"])[:280],
            "why_it_matters": str(m.get("why_it_matters") or "")[:200],
        })

    self_growth = []
    for g in parsed.get("self_growth") or []:
        if not isinstance(g, dict) or not g.get("lesson"):
            continue
        kind = str(g.get("kind") or "do").lower()
        if kind not in ("do", "stop"):
            kind = "do"
        self_growth.append({
            "lesson": str(g["lesson"])[:240],
            "kind": kind,
        })

    self_traits = []
    for t in parsed.get("self_traits") or []:
        if isinstance(t, str) and t.strip():
            self_traits.append(t.strip()[:200])

    return DistilledUpdate(
        profile_changes=profile_changes,
        new_lessons=new_lessons,
        kg_upserts=kg_upserts,
        playbook_candidate=playbook_candidate,
        raw_signal=signal,
        self_memories=self_memories,
        self_growth=self_growth,
        self_traits=self_traits,
    )


def _apply(update: DistilledUpdate, conversation_id: str | None, signal: str) -> None:
    """
    Apply the update. Low-risk writes (lessons under MAX_CONF, KG upserts,
    weak profile observations) go straight through — they're reversible via
    the per-surface retire/forget paths and watched by integrity.py. Playbook
    creation always goes through the proposals flow (create-only skipped
    here, handled in loop.py / integrity.py).
    """
    # Profile
    for ch in update.profile_changes:
        try:
            profile.observe(
                ch["path"], ch["value"], weight=ch["weight"],
                reason=ch.get("reason") or f"distilled from {signal} signal",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("profile.observe failed: %s", e)

    # Lessons — accept anything with confidence <= 0.5 auto; higher goes through
    # proposals. For now we create them all as low-confidence and rely on
    # integrity / reinforce loops to promote.
    for ll in update.new_lessons:
        try:
            lessons.create(
                rule=ll["rule"],
                tags=ll["tags"],
                keywords=ll["keywords"],
                confidence=min(0.5, ll["confidence"]),
                source="distillation",
                evidence=[{"conversation_id": conversation_id, "signal": signal}],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("lessons.create failed: %s", e)

    # KG
    for u in update.kg_upserts:
        try:
            kg.upsert_entity(
                etype=u["type"], name=u["name"],
                attrs=u.get("attrs") or {},
                aliases=u.get("aliases") or [],
                confidence=min(0.6, u["confidence"]),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("kg.upsert_entity failed: %s", e)

    # Playbook candidate: the proposals system owns creation so we don't
    # auto-apply. Route stays in loop.py (which can ask the user to approve).
    # TODO: wire playbook_candidate → proposals.create when a writer shows up.

    # Sentrial self-observations — accrete into self_profile.yaml. Each
    # writer is independently fail-safe so a corrupt yaml file never breaks
    # the post-turn loop.
    for m in update.self_memories:
        try:
            self_profile.add_memory(
                summary=m["summary"], why_it_matters=m.get("why_it_matters") or "",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("self_profile.add_memory failed: %s", e)

    for g in update.self_growth:
        try:
            self_profile.add_growth(lesson=g["lesson"], kind=g.get("kind") or "do")
        except Exception as e:  # noqa: BLE001
            log.warning("self_profile.add_growth failed: %s", e)

    for t in update.self_traits:
        try:
            self_profile.observe_trait(t, source="distilled")
        except Exception as e:  # noqa: BLE001
            log.warning("self_profile.observe_trait failed: %s", e)


def fire_and_forget(
    user_message: str,
    assistant_reply: str,
    prev_assistant: str | None = None,
    conversation_id: str | None = None,
    playbook_slug: str | None = None,
) -> asyncio.Task:
    """
    Schedule distill_turn without blocking the caller. Returns the Task so
    the caller can keep a reference (important: Python GCs unawaited tasks).
    """
    loop = asyncio.get_event_loop()
    return loop.create_task(
        distill_turn(
            user_message=user_message,
            assistant_reply=assistant_reply,
            prev_assistant=prev_assistant,
            conversation_id=conversation_id,
            playbook_slug=playbook_slug,
        ),
        name="sentrial_distill",
    )
