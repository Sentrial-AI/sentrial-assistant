"""
The self-improvement loop (Karpathy `autoresearch` pattern).

    read program.md → compute metrics → pick focus metric → generate candidates →
    evaluate via subagent judge → rank → write best as proposal → notify Liam

For v1 the evaluation step is a simple subagent call, not the full replay-N-past-
interactions rig. The plumbing is structured so the real eval can slot in later
without changing callers.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from sentrial.core import audit, memory, paths
from sentrial.core import secrets as kc
from sentrial.evolution import metrics, proposals

log = logging.getLogger(__name__)

PROGRAM_PATH = Path(__file__).parent / "program.md"
MODEL = "claude-opus-4-6"
MAX_CANDIDATES = 5
MIN_SCORE_DELTA = 0.5
WALL_CLOCK_LIMIT_S = 15 * 60


@dataclass
class CycleReport:
    baseline: dict
    focus_metric: str | None
    candidates_generated: int
    best_score_delta: float | None
    proposal_id: str | None
    status: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "focus_metric": self.focus_metric,
            "candidates_generated": self.candidates_generated,
            "best_score_delta": self.best_score_delta,
            "proposal_id": self.proposal_id,
            "status": self.status,
            "note": self.note,
        }


def _pending_proposal_count() -> int:
    return len(proposals.list_all(status="pending"))


async def run_cycle(dry_run: bool = False) -> CycleReport:
    """
    Run one self-improvement cycle. Returns a structured report.
    `dry_run=True` generates candidates without writing proposals — useful for testing.
    """
    if _pending_proposal_count() >= 3:
        return CycleReport(
            baseline={}, focus_metric=None, candidates_generated=0,
            best_score_delta=None, proposal_id=None,
            status="skipped", note="3+ pending proposals — resolve before running again",
        )

    program = PROGRAM_PATH.read_text()
    baseline = metrics.compute_metrics(window_days=7).to_dict()
    baseline_28 = metrics.compute_metrics(window_days=28).to_dict()

    focus_metric = _pick_focus_metric(baseline, baseline_28)
    if focus_metric is None:
        return CycleReport(
            baseline=baseline, focus_metric=None, candidates_generated=0,
            best_score_delta=None, proposal_id=None,
            status="no_focus", note="no metric degraded enough to act on",
        )

    audit.log(
        "sentrial", "evolution_cycle_start", 1,
        args={"focus_metric": focus_metric, "baseline": baseline},
    )

    client = AsyncAnthropic(api_key=kc.require("anthropic_api_key"))

    # Gather context for the candidate generator
    sample = _recent_audit_sample(focus_metric)

    candidates = await _generate_candidates(
        client, program=program, baseline=baseline,
        focus_metric=focus_metric, sample=sample,
    )

    if not candidates:
        return CycleReport(
            baseline=baseline, focus_metric=focus_metric, candidates_generated=0,
            best_score_delta=None, proposal_id=None,
            status="no_candidates", note="subagent returned no candidates",
        )

    # Evaluate each
    scored = []
    for c in candidates:
        try:
            score = await _evaluate_candidate(client, c, sample)
            scored.append((score, c))
        except Exception as e:  # noqa: BLE001
            log.warning("candidate eval failed: %s", e)

    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return CycleReport(
            baseline=baseline, focus_metric=focus_metric,
            candidates_generated=len(candidates), best_score_delta=None,
            proposal_id=None, status="eval_failed", note="no candidate scored",
        )

    best_score, best = scored[0]
    if best_score < MIN_SCORE_DELTA:
        return CycleReport(
            baseline=baseline, focus_metric=focus_metric,
            candidates_generated=len(candidates), best_score_delta=best_score,
            proposal_id=None, status="below_threshold",
            note=f"best score {best_score:.2f} below +{MIN_SCORE_DELTA} bar",
        )

    if dry_run:
        return CycleReport(
            baseline=baseline, focus_metric=focus_metric,
            candidates_generated=len(candidates), best_score_delta=best_score,
            proposal_id=None, status="dry_run",
            note=f"would propose: {best['rationale'][:100]}",
        )

    prop = proposals.create(
        target=best["target"],
        before=best["before"],
        after=best["after"],
        rationale=best["rationale"],
        focus_metric=focus_metric,
        baseline=baseline.get(focus_metric),
        predicted=best.get("predicted"),
        score_delta=best_score,
    )

    audit.log(
        "sentrial", "evolution_cycle_end", 1,
        args={"proposal_id": prop["id"], "focus_metric": focus_metric},
        result=best["rationale"][:300],
    )

    return CycleReport(
        baseline=baseline, focus_metric=focus_metric,
        candidates_generated=len(candidates), best_score_delta=best_score,
        proposal_id=prop["id"], status="proposed",
    )


def _pick_focus_metric(week: dict, month: dict) -> str | None:
    """Choose the single metric that regressed most (direction matters)."""
    worse_is_bigger = {"edit_rate", "tool_denial_rate", "clarification_rate", "avg_latency_s"}
    worse_is_smaller = {"scope_preview_acceptance"}
    deltas: list[tuple[str, float]] = []
    for k in worse_is_bigger:
        w, m = week.get(k, 0) or 0, month.get(k, 0) or 0
        if w > m * 1.1 and w >= 0.05:
            deltas.append((k, (w - m)))
    for k in worse_is_smaller:
        w, m = week.get(k, 0) or 0, month.get(k, 0) or 0
        if m > w * 1.1 and m >= 0.05:
            deltas.append((k, (m - w)))
    if not deltas:
        return None
    deltas.sort(key=lambda x: x[1], reverse=True)
    return deltas[0][0]


def _recent_audit_sample(focus_metric: str, limit: int = 20) -> list[dict]:
    return audit.tail(limit)


async def _generate_candidates(
    client: AsyncAnthropic,
    program: str,
    baseline: dict,
    focus_metric: str,
    sample: list[dict],
) -> list[dict]:
    """Ask a subagent to propose up to MAX_CANDIDATES edits."""

    system_prompt = Path(__file__).parent.parent / "config" / "system_prompt.md"
    current_prompt = system_prompt.read_text()

    msg = (
        "You are the self-improvement agent for Sentrial. Read the program, the current "
        "baseline metrics, and the recent audit sample. Propose up to "
        f"{MAX_CANDIDATES} candidate edits to the system prompt that would plausibly "
        f"improve the focus metric `{focus_metric}`.\n\n"
        f"# program.md\n\n{program}\n\n"
        f"# current baseline\n\n{json.dumps(baseline, indent=2)}\n\n"
        f"# current system_prompt.md\n\n{current_prompt}\n\n"
        f"# recent audit sample\n\n{json.dumps(sample[:20], indent=2, default=str)}\n\n"
        "Reply with ONLY a JSON array of candidate objects. Each object:\n"
        "{\n"
        '  "target": "sentrial/config/system_prompt.md",\n'
        '  "before": "<full current content>",\n'
        '  "after": "<full proposed content>",\n'
        '  "rationale": "<one sentence why this helps the focus metric>",\n'
        '  "predicted": <float, predicted new value of focus metric>\n'
        "}\n"
        "If no candidate meets the constraints in program.md, reply with []."
    )

    resp = await client.messages.create(
        model=MODEL, max_tokens=8192,
        messages=[{"role": "user", "content": msg}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    text = text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()

    try:
        arr = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("candidate parse failed: %s", e)
        return []
    if not isinstance(arr, list):
        return []
    # Keep only candidates that changed the content
    return [c for c in arr if isinstance(c, dict) and c.get("before") != c.get("after")][:MAX_CANDIDATES]


async def _evaluate_candidate(
    client: AsyncAnthropic, candidate: dict, sample: list[dict]
) -> float:
    """
    Score a candidate via replay-based evaluation (the real thing) with a
    judge-only fallback when there are no past turns to replay.

    Returns a score delta in roughly [-2, +2]. The loop's MIN_SCORE_DELTA
    threshold (0.5) gates whether the candidate becomes a proposal.
    """
    from sentrial.evolution import replay

    target = candidate.get("target", "")
    surface_kind = "system_prompt" if target.endswith("system_prompt.md") else "system_prompt"

    try:
        rr = await replay.evaluate(
            baseline_surface=candidate.get("before", ""),
            candidate_surface=candidate.get("after", ""),
            surface_kind=surface_kind,
        )
        # Stash replay details on the candidate so proposals carry the receipts.
        candidate["replay"] = rr.to_dict()
        if rr.sample_size > 0:
            return rr.delta
    except Exception as e:  # noqa: BLE001
        log.warning("replay eval failed — falling back to judge-only: %s", e)

    # Fallback: judge-only score (the old behavior). Used on cold start when
    # there are no past turns, and as a safety net when replay errors.
    msg = (
        "You are an evaluator. Rate how much the proposed edit would improve Liam's experience, "
        "given the recent audit sample. Return a single float from 0 to 10 where 5 means "
        "no-change-expected, 7 means clear improvement, and 10 means transformative.\n\n"
        f"# rationale\n{candidate.get('rationale', '')}\n\n"
        f"# diff (first 3000 chars)\n{_mini_diff(candidate.get('before',''), candidate.get('after',''))[:3000]}\n\n"
        f"# recent audit sample\n{json.dumps(sample[:10], indent=2, default=str)}\n\n"
        "Output ONLY the float, no prose."
    )
    resp = await client.messages.create(
        model=MODEL, max_tokens=16,
        messages=[{"role": "user", "content": msg}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    try:
        score = float(text.split()[0])
    except (ValueError, IndexError):
        return 0.0
    # Scale judge-only 0..10 to ~[-2, +2] but dampen because it's unverified.
    return (score - 5.0) * 0.4


def _mini_diff(before: str, after: str) -> str:
    import difflib
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), lineterm=""))
