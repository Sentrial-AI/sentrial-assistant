# Sentrial self-improvement program

This is the equivalent of Karpathy's `program.md` in `karpathy/autoresearch`. It's the
single document the evolution loop reads to decide what to try, what not to touch, and
when to stop. Keep it tight.

---

## Goal

Improve Liam's experience on repeat tasks by learning from every interaction.
Specifically: reduce the number of times Liam has to correct, redo, or repeat himself.

## Editable surfaces (the agent may propose diffs to these)

- `sentrial/config/system_prompt.md` — how Sentrial talks and when it acts
- `sentrial/mcps/creative/scope_templates/*.md` — scope-preview phrasing per job kind (will be created as needed)
- `sentrial/evolution/lessons/*.md` — distilled lessons added after notable interactions
- `tier_overrides` in the `facts` table (scope="tier_overrides") — learned exceptions to the default tier classifier

## Frozen surfaces (the agent must NOT propose changes here)

- `sentrial/core/secrets.py` and anything Keychain/env-var related
- `sentrial/core/confirmation.py` base tiers (Tier enum, TIER_HINTS, EXPLICIT_TIERS entries for tier 3)
- The security section of `system_prompt.md` ("Trust and authority — action tiers" and "Security rules — non-negotiable")
- Anything under `scripts/`, `Dockerfile`, `railway.toml`, `pyproject.toml`, `requirements.txt`
- The evolution loop itself (this module)

If the agent believes a frozen surface needs changing, it must emit a `safety_concern`
proposal explaining the case, for Liam to manually action — never a direct edit.

## Metrics (quantitative signal — lower is better unless noted)

| Metric | Direction | Source |
|---|---|---|
| `edit_rate` | ↓ | fraction of user turns containing "actually" / "no" / "change" / "wrong" / "instead" within 2 turns of a Sentrial response |
| `tool_denial_rate` | ↓ | `audit.tier=2 status=denied` / all `audit.tier=2` |
| `clarification_rate` | ↓ | fraction of Sentrial responses ending in "?" that aren't scope-preview confirmations |
| `scope_preview_acceptance` | ↑ | jobs approved on first scope preview / all created jobs |
| `avg_latency_s` | ↓ | wall-clock seconds from user message to final Sentrial reply |
| `avg_response_tokens` | ≈ | should track Liam's terseness preference — alert if >180 when the task doesn't warrant it |

Compute metrics with `sentrial.evolution.metrics.compute_metrics(window_days=7)`.

## Loop

1. Read last 7 days of audit + conversations + jobs.
2. Compute current metrics. Record baseline.
3. Pick the single metric with the worst regression vs the 28-day baseline (the "focus metric").
4. Identify the most common interaction pattern that contributes to the regression.
   (Example: "proposals for SaaS clients have a 38% edit rate — higher than 22% overall.")
5. Generate up to **5 candidate diffs** to a single editable surface that plausibly improve the focus metric for that pattern.
6. For each candidate:
   a. Apply the diff to a copy of the surface (never the live file).
   b. Replay the last 10 matching past interactions through a subagent using the modified surface.
   c. Score each replay via an LLM judge (0–10) with the rubric: "does this response better match Liam's preferences?"
   d. Record mean score and standard deviation.
7. Rank candidates by score delta over baseline. Require delta ≥ +0.5 to pass.
8. Best candidate → write a proposal file to `/data/proposals/<iso-ts>-<id>.json` with:
   `target`, `before`, `after`, `diff`, `rationale`, `baseline_metric`, `predicted_metric`, `score_delta`.
9. Notify Liam via the PWA Proposals tab (badge) + web push if enabled.
10. On Liam's approval → apply the diff to the live surface, archive the proposal as applied.
    On deny → archive as denied, save the rationale so the loop doesn't repeat it.

## Stopping criteria

- No candidate passes the +0.5 bar → loop ends, emit "no improvement" summary.
- Wall-clock 15 minutes per cycle → hard cutoff.
- Max 3 proposals pending at once — don't pile up review work on Liam.
- If `edit_rate` trends up after a recent approved change → auto-revert the most recent change and flag.

## Cadence

- **On-demand** — Liam triggers via `"run a reflection"` or the Proposals tab "Scan for improvements" button.
- **Nightly** — scheduled at 03:30 America/New_York, low-traffic time.
- **Post-job** — after any autonomous job with edits/denial, run a *lesson* distillation (cheaper, no full loop).

## Output shape

Proposals are JSON:
```json
{
  "id": "e8a3d2",
  "created_at": "2026-04-23T08:14:00Z",
  "target": "sentrial/config/system_prompt.md",
  "rationale": "edit_rate on proposal jobs rose 14% this week; 6/10 edits were Liam shortening the scope preview. Proposing a tighter template.",
  "focus_metric": "edit_rate",
  "baseline": 0.22,
  "predicted": 0.17,
  "score_delta": 1.1,
  "diff": "...",
  "before_sha": "...",
  "after_sha": "...",
  "status": "pending"
}
```
