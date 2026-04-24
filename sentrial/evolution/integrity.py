"""
Integrity watchdog — the guardrail on the self-improvement system.

Runs periodically (on-demand via /api/evolution/integrity, plus a scheduled
sweep). Checks for drift, tampering, regression, and conflicts across every
editable surface:

  1. Frozen-surface tamper — hash check against expected values; yells if
     a protected file was modified outside the proposals system.
  2. Backup completeness — every applied proposal must have its backup.
  3. Metric regression — if edit_rate has risen sharply since the last
     applied proposal, auto-revert the most recent change and flag.
  4. Lesson conflicts — detect pairs of active lessons whose rules contradict
     each other (same keywords, opposite directives) and mark for review.
  5. Profile sanity — any field with confidence == 1.0 but evidence_count
     < 5 is suspect (adversarial injection).
  6. KG bloat — warn if entity count grows > growth_rate_cap per day.
  7. Trial guardrail — if a running trial shows > X% degradation on its
     primary metric, auto-stop.

Returns a structured report. Non-fatal warnings don't mutate state; fatal
findings do (auto-revert, auto-retire, auto-stop) and are always audited.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sentrial.core import audit, paths
from sentrial.evolution import lessons, metrics, playbooks, profile, proposals, trials

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent.parent
BASE_DIR = Path(__file__).parent / "base"
FROZEN_ALLOWLIST = (
    "sentrial/core/secrets.py",
    "sentrial/core/confirmation.py",
    "sentrial/evolution/loop.py",
    "sentrial/evolution/metrics.py",
    "sentrial/evolution/proposals.py",
    "sentrial/evolution/integrity.py",
    "sentrial/evolution/replay.py",
    "sentrial/evolution/trials.py",
    "sentrial/evolution/reset.py",
    "sentrial/evolution/distill.py",
)
REGRESSION_EDIT_RATE_DELTA = 0.15       # +15 percentage points after change = auto-revert
TRIAL_DEGRADATION_THRESHOLD = 0.30      # 30% worse → stop
KG_GROWTH_CAP_PER_DAY = 200
PROFILE_SUSPECT_MIN_EVIDENCE = 3


@dataclass
class Finding:
    severity: str        # "info" | "warn" | "fatal"
    area: str
    code: str
    message: str
    data: dict = field(default_factory=dict)
    action_taken: str | None = None


@dataclass
class IntegrityReport:
    at: str
    findings: list[Finding]
    ok: bool
    auto_actions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "ok": self.ok,
            "auto_actions": self.auto_actions,
            "findings": [
                {
                    "severity": f.severity, "area": f.area, "code": f.code,
                    "message": f.message, "data": f.data,
                    "action_taken": f.action_taken,
                }
                for f in self.findings
            ],
        }


# ---- public API ----

def run(full: bool = False) -> IntegrityReport:
    """Run all integrity checks. `full=True` also audits KG growth + retired-lesson conflicts."""
    findings: list[Finding] = []

    findings.extend(_check_frozen_surfaces())
    findings.extend(_check_backup_completeness())
    findings.extend(_check_regression_and_revert())
    findings.extend(_check_lesson_conflicts())
    findings.extend(_check_profile_sanity())
    findings.extend(_check_trial_degradation())
    if full:
        findings.extend(_check_kg_growth())

    # Expire any overdue trials while we're here.
    try:
        expired = trials.expire_due()
        if expired:
            findings.append(Finding(
                severity="info", area="trials", code="expired",
                message=f"{expired} trial(s) auto-completed on time",
            ))
    except Exception as e:  # noqa: BLE001
        findings.append(Finding(
            severity="warn", area="trials", code="expire_failed",
            message=f"trial expiry check failed: {e}",
        ))

    auto_actions = sum(1 for f in findings if f.action_taken)
    ok = not any(f.severity == "fatal" for f in findings)
    report = IntegrityReport(
        at=datetime.now(timezone.utc).isoformat(),
        findings=findings, ok=ok, auto_actions=auto_actions,
    )
    audit.log(
        "sentrial", "integrity_run", 1,
        args={"full": full, "auto_actions": auto_actions},
        result=f"ok={ok} n_findings={len(findings)}",
    )
    return report


# ---- individual checks ----

def _check_frozen_surfaces() -> list[Finding]:
    """
    Every file in FROZEN_ALLOWLIST must match its stored baseline hash. We
    store baselines in /data/evolution/integrity_hashes.json the first time
    we see them; subsequent runs compare.
    """
    hashes_file = paths.data_dir() / "evolution" / "integrity_hashes.json"
    hashes_file.parent.mkdir(parents=True, exist_ok=True)
    import json as _j
    try:
        stored = _j.loads(hashes_file.read_text()) if hashes_file.exists() else {}
    except Exception:  # noqa: BLE001
        stored = {}

    found: list[Finding] = []
    current: dict[str, str] = {}
    for rel in FROZEN_ALLOWLIST:
        p = REPO_ROOT / rel
        if not p.is_file():
            found.append(Finding(
                severity="warn", area="frozen", code="missing",
                message=f"frozen surface missing: {rel}",
            ))
            continue
        h = hashlib.sha1(p.read_bytes()).hexdigest()
        current[rel] = h
        prior = stored.get(rel)
        if prior is None:
            # First sighting — record baseline, don't flag.
            stored[rel] = h
            continue
        if prior != h:
            found.append(Finding(
                severity="fatal", area="frozen", code="tampered",
                message=f"frozen surface changed outside proposals: {rel}",
                data={"prior_sha1": prior, "current_sha1": h},
            ))
    # Persist updated hashes (only when there are no tamper fatals — we don't
    # want to overwrite good baselines when something has already gone wrong).
    if not any(f.severity == "fatal" for f in found):
        stored.update(current)
        hashes_file.write_text(_j.dumps(stored, indent=2))
    return found


def _check_backup_completeness() -> list[Finding]:
    out: list[Finding] = []
    for p in proposals.list_all(status="applied"):
        bp = p.get("backup_path")
        if not bp or not Path(bp).exists():
            out.append(Finding(
                severity="warn", area="proposals", code="no_backup",
                message=f"applied proposal {p['id']} has no backup on disk",
                data={"id": p["id"], "target": p.get("target")},
            ))
    return out


def _check_regression_and_revert() -> list[Finding]:
    """
    If the most recent applied proposal was followed by a significant rise in
    edit_rate (vs. metrics_at_apply), auto-revert.
    """
    applied = [
        p for p in proposals.list_all(status="applied")
        if p.get("metrics_at_apply") and p.get("applied_at")
    ]
    if not applied:
        return []
    # Sort by applied_at DESC (approve writes applied_at).
    applied.sort(key=lambda p: p.get("applied_at") or "", reverse=True)
    latest = applied[0]
    baseline_rate = (latest["metrics_at_apply"] or {}).get("edit_rate")
    if baseline_rate is None:
        return []
    try:
        current = metrics.compute_metrics(window_days=3).to_dict().get("edit_rate")
    except Exception as e:  # noqa: BLE001
        return [Finding("warn", "regression", "metrics_failed", f"{e}")]
    if current is None:
        return []
    delta = current - baseline_rate
    if delta < REGRESSION_EDIT_RATE_DELTA:
        return []
    # Auto-revert.
    try:
        proposals.revert(latest["id"])
        audit.log(
            "sentrial", "auto_revert", 2,
            args={"id": latest["id"], "baseline": baseline_rate, "current": current},
            result="edit_rate regression auto-revert",
        )
        return [Finding(
            severity="fatal", area="regression", code="auto_reverted",
            message=f"auto-reverted {latest['id']} due to edit_rate delta {delta:.2f}",
            data={"proposal_id": latest["id"], "baseline": baseline_rate,
                  "current": current, "delta": round(delta, 4)},
            action_taken="reverted",
        )]
    except Exception as e:  # noqa: BLE001
        return [Finding(
            severity="fatal", area="regression", code="revert_failed",
            message=f"edit_rate regressed {delta:.2f} but revert failed: {e}",
            data={"proposal_id": latest["id"]},
        )]


def _check_lesson_conflicts() -> list[Finding]:
    """
    Naive conflict detection: lessons sharing ≥2 keywords whose rules contain
    opposing imperatives ("always X" vs "never X", "do" vs "don't").
    """
    opposites = [
        (re.compile(r"\balways\b", re.I), re.compile(r"\bnever\b", re.I)),
        (re.compile(r"\bdo\b", re.I), re.compile(r"\bdon'?t\b", re.I)),
        (re.compile(r"\bshould\b", re.I), re.compile(r"\bshouldn'?t\b", re.I)),
    ]
    active = lessons.list_all(status="active")
    found: list[Finding] = []
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            a, b = active[i], active[j]
            shared = set(a.get("keywords") or []) & set(b.get("keywords") or [])
            if len(shared) < 2:
                continue
            for pa, pb in opposites:
                if pa.search(a["rule"]) and pb.search(b["rule"]):
                    found.append(Finding(
                        severity="warn", area="lessons", code="conflict",
                        message=f"lessons {a['id']} / {b['id']} appear to conflict on {sorted(shared)}",
                        data={"a": a["id"], "b": b["id"],
                              "a_rule": a["rule"], "b_rule": b["rule"]},
                    ))
                    break
    return found


def _check_profile_sanity() -> list[Finding]:
    data = profile.load()
    suspects: list[dict] = []
    _collect_suspects(data, "", suspects)
    return [
        Finding(
            severity="warn", area="profile", code="suspect_high_conf",
            message=f"{s['path']} has confidence {s['confidence']} but evidence_count {s['evidence_count']}",
            data=s,
        )
        for s in suspects
    ]


def _collect_suspects(node: Any, path: str, out: list[dict]) -> None:
    if (
        isinstance(node, dict)
        and "value" in node and "confidence" in node
    ):
        conf = float(node.get("confidence") or 0)
        ev = int(node.get("evidence_count") or 0)
        if conf >= 0.9 and ev < PROFILE_SUSPECT_MIN_EVIDENCE:
            out.append({"path": path, "confidence": conf, "evidence_count": ev})
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _collect_suspects(v, f"{path}.{k}" if path else k, out)


def _check_trial_degradation() -> list[Finding]:
    """Auto-stop any running trial where treatment metrics are >30% worse on a known-lower-is-better metric."""
    lower_is_better = {"edit_rate", "tool_denial_rate", "clarification_rate", "avg_latency_s"}
    found: list[Finding] = []
    for t in trials.list_active():
        summary = trials.summarize(t["id"])
        groups = summary.get("metrics") or {}
        base = groups.get("baseline") or {}
        trt = groups.get("treatment") or {}
        for metric in lower_is_better:
            b = (base.get(metric) or {}).get("avg")
            c = (trt.get(metric) or {}).get("avg")
            n = (trt.get(metric) or {}).get("n", 0)
            if b is None or c is None or n < 5:
                continue
            if b > 0 and (c - b) / b > TRIAL_DEGRADATION_THRESHOLD:
                trials.stop_trial(t["id"], reason=f"auto-stop: {metric} degraded {((c-b)/b*100):.0f}%")
                found.append(Finding(
                    severity="fatal", area="trials", code="auto_stop",
                    message=f"stopped trial {t['id']} — {metric} degraded {((c-b)/b*100):.0f}%",
                    data={"trial_id": t["id"], "metric": metric,
                          "baseline_avg": b, "treatment_avg": c, "n": n},
                    action_taken="stopped",
                ))
                break
    return found


def _check_kg_growth() -> list[Finding]:
    from sentrial.evolution import kg
    entities = kg.list_entities(limit=10000)
    if not entities:
        return []
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    recent = 0
    for e in entities:
        try:
            created = datetime.fromisoformat((e.get("created_at") or "").replace("Z", "+00:00"))
            if created >= day_ago:
                recent += 1
        except ValueError:
            continue
    if recent > KG_GROWTH_CAP_PER_DAY:
        return [Finding(
            severity="warn", area="kg", code="rapid_growth",
            message=f"{recent} entities created in the last 24h — cap {KG_GROWTH_CAP_PER_DAY}",
            data={"n_today": recent, "cap": KG_GROWTH_CAP_PER_DAY},
        )]
    return []
