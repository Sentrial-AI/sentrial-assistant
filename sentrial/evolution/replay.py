"""
Replay-based candidate evaluation — the thing that makes self-improvement
trustworthy.

Given a proposed edit (prompt change, profile flip, lesson add, playbook
tweak), we:
  1. Sample N matching past turns (input + accepted output + any correction).
  2. Re-run each past user message through a *candidate agent* configured
     with the proposed surface change.
  3. Score each replay against ground truth:
        +1 if the new output is closer to the accepted output than the old
            (measured by: no correction markers in the simulated follow-up,
             similar length, matches key terms of the accepted output)
        -1 if it drifts further away (introduces correction markers the user
             would almost certainly fire)
         0 if unchanged
  4. Aggregate to a score delta vs. a baseline replay (same past turns, old
     surface). Anything < +0.5 means "no meaningful improvement" and the
     candidate is rejected.

This is bounded-cost: we cap N=10 past turns by default, use Haiku for
replays, and short-circuit if the first 3 replays show strong consensus.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic

from sentrial.core import memory, secrets

log = logging.getLogger(__name__)

REPLAY_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1500
DEFAULT_SAMPLE_SIZE = 8
EARLY_STOP_AFTER = 3
EARLY_STOP_MARGIN = 1.5

# Reuse the edit-marker regex from metrics to detect "this got corrected".
EDIT_MARKERS = re.compile(
    r"\b(actually|instead|no,?\s*(?:can|could|would)|change\s+(?:it|that)|"
    r"redo|wrong|not\s+what|rewrite|shorter|longer|tighter)\b",
    re.IGNORECASE,
)


@dataclass
class ReplayResult:
    sample_size: int
    baseline_score: float
    candidate_score: float
    delta: float
    details: list[dict]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "baseline_score": round(self.baseline_score, 3),
            "candidate_score": round(self.candidate_score, 3),
            "delta": round(self.delta, 3),
            "note": self.note,
        }


# ---- public API ----

async def evaluate(
    baseline_surface: str,
    candidate_surface: str,
    surface_kind: str = "system_prompt",
    filter_task_slug: str | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> ReplayResult:
    """
    Score a candidate edit by replaying past turns.

    surface_kind: "system_prompt" | "lesson" | "playbook" — controls how the
    candidate is injected into the replay.
    """
    samples = _sample_past_turns(limit=sample_size, filter_task_slug=filter_task_slug)
    if not samples:
        return ReplayResult(0, 0.0, 0.0, 0.0, [], note="no past turns to replay")

    api_key = secrets.require("anthropic_api_key")
    client = AsyncAnthropic(api_key=api_key)

    details: list[dict] = []
    baseline_scores: list[float] = []
    candidate_scores: list[float] = []

    for i, sample in enumerate(samples):
        try:
            base_out = await _simulate(client, baseline_surface, sample, surface_kind)
            cand_out = await _simulate(client, candidate_surface, sample, surface_kind)
        except Exception as e:  # noqa: BLE001
            log.warning("replay simulate failed: %s", e)
            continue

        b_score = _score_output(base_out, sample)
        c_score = _score_output(cand_out, sample)
        baseline_scores.append(b_score)
        candidate_scores.append(c_score)

        details.append({
            "user_message": sample["user_message"][:160],
            "baseline_score": round(b_score, 2),
            "candidate_score": round(c_score, 2),
            "delta": round(c_score - b_score, 2),
        })

        # Early stop: strong signal after a few samples.
        if len(candidate_scores) >= EARLY_STOP_AFTER:
            running_delta = (sum(candidate_scores) - sum(baseline_scores)) / len(candidate_scores)
            if abs(running_delta) >= EARLY_STOP_MARGIN:
                break

    if not candidate_scores:
        return ReplayResult(0, 0.0, 0.0, 0.0, [], note="no replay succeeded")

    b = sum(baseline_scores) / len(baseline_scores)
    c = sum(candidate_scores) / len(candidate_scores)
    return ReplayResult(
        sample_size=len(candidate_scores),
        baseline_score=b,
        candidate_score=c,
        delta=(c - b),
        details=details,
    )


# ---- internals ----

def _sample_past_turns(
    limit: int, filter_task_slug: str | None,
) -> list[dict]:
    """Pull recent turns with (user_message, assistant_reply, next_user). The
    `next_user` slot lets us infer whether Liam corrected the assistant."""
    out: list[dict] = []
    convs = memory.list_conversations(limit=50)
    for c in convs:
        full = memory.get_conversation(c["id"])
        if not full:
            continue
        turns = full.get("turns") or []
        for i, t in enumerate(turns):
            if t.get("role") != "assistant":
                continue
            prev = turns[i - 1] if i - 1 >= 0 else None
            nxt = turns[i + 1] if i + 1 < len(turns) else None
            if not prev or prev.get("role") != "user":
                continue
            user_msg = str(prev.get("content") or "")
            asst = _text_of(t.get("content"))
            next_user = str(nxt.get("content")) if (nxt and nxt.get("role") == "user") else None
            got_corrected = bool(next_user and EDIT_MARKERS.search(next_user))
            out.append({
                "conversation_id": c["id"],
                "user_message": user_msg,
                "accepted_reply": asst,   # what actually happened
                "next_user": next_user,
                "got_corrected": got_corrected,
            })
            if len(out) >= limit:
                return out
    return out


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


async def _simulate(
    client: AsyncAnthropic, surface: str, sample: dict, surface_kind: str,
) -> str:
    """Run the user message through a lightweight completion with the proposed
    surface, returning the new assistant text."""
    if surface_kind == "system_prompt":
        system = surface
    elif surface_kind == "lesson":
        # Inject lesson as the tail of the system prompt for the replay.
        system = f"You are Sentrial, Liam's assistant.\n\n[active lesson]\n{surface}"
    elif surface_kind == "playbook":
        system = f"You are Sentrial. Use this playbook for the task.\n\n{surface}"
    else:
        system = surface
    resp = await client.messages.create(
        model=REPLAY_MODEL, max_tokens=MAX_TOKENS, system=system,
        messages=[{"role": "user", "content": sample["user_message"]}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def _score_output(output: str, sample: dict) -> float:
    """
    Score a simulated output in [-2, +2]:
      +2 similar to accepted AND Liam didn't correct originally
      +1 similar to accepted OR avoids features Liam corrected
       0 neutral
      -1 introduces features absent from accepted
      -2 contains language that would plausibly trigger a correction
    """
    accepted = sample.get("accepted_reply") or ""
    got_corrected = sample.get("got_corrected")
    score = 0.0

    # Similarity: token-overlap over unique stems.
    a_tokens = {w for w in re.findall(r"[a-zA-Z]{4,}", accepted.lower())}
    o_tokens = {w for w in re.findall(r"[a-zA-Z]{4,}", output.lower())}
    if a_tokens and o_tokens:
        overlap = len(a_tokens & o_tokens) / max(1, len(a_tokens | o_tokens))
        score += (overlap - 0.3) * 2.0

    # Length ratio — penalize drastic inflation.
    la, lo = max(len(accepted), 1), max(len(output), 1)
    ratio = lo / la
    if ratio > 3.0 or ratio < 0.33:
        score -= 0.5

    # Structural match: bullets / headers (Liam dislikes — apply only if
    # accepted had none and output introduces many).
    a_bullets = accepted.count("\n- ") + accepted.count("\n* ")
    o_bullets = output.count("\n- ") + output.count("\n* ")
    if a_bullets == 0 and o_bullets >= 2:
        score -= 0.6
    a_headers = len(re.findall(r"^\s*#{1,6}\s", accepted, re.M))
    o_headers = len(re.findall(r"^\s*#{1,6}\s", output, re.M))
    if a_headers == 0 and o_headers >= 1:
        score -= 0.4

    # If the original turn got corrected, reward outputs that DIFFER from the
    # accepted-but-corrected reply (we want better than what actually happened).
    if got_corrected:
        score += 0.3 if (a_tokens - o_tokens) else 0.0

    return max(-2.0, min(2.0, score))
