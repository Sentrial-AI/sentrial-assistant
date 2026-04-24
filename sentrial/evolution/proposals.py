"""
Proposal CRUD. A proposal is a JSON file in /data/proposals/<id>.json describing
a candidate edit to an editable surface. Liam approves or denies via the PWA.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sentrial.core import audit, paths


def _dir() -> Path:
    p = paths.data_dir() / "proposals"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _backup_dir() -> Path:
    p = paths.data_dir() / "proposals_backup"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(proposal_id: str) -> Path:
    return _dir() / f"{proposal_id}.json"


def create(
    target: str,
    before: str,
    after: str,
    rationale: str,
    focus_metric: str | None = None,
    baseline: float | None = None,
    predicted: float | None = None,
    score_delta: float | None = None,
) -> dict[str, Any]:
    pid = uuid.uuid4().hex[:12]
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{target}",
            tofile=f"b/{target}",
            n=3,
        )
    )
    doc = {
        "id": pid,
        "created_at": _now(),
        "target": target,
        "rationale": rationale,
        "focus_metric": focus_metric,
        "baseline": baseline,
        "predicted": predicted,
        "score_delta": score_delta,
        "before": before,
        "after": after,
        "before_sha": _sha(before),
        "after_sha": _sha(after),
        "diff": diff,
        "status": "pending",
    }
    _path(pid).write_text(json.dumps(doc, indent=2))
    audit.log(
        "sentrial", "proposal_created", 1,
        args={"target": target, "id": pid},
        result=rationale[:200],
    )
    return doc


def list_all(status: str | None = None) -> list[dict]:
    out: list[dict] = []
    for f in sorted(_dir().glob("*.json"), reverse=True):
        try:
            d = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if status and d.get("status") != status:
            continue
        out.append({k: v for k, v in d.items() if k not in ("before", "after")})
    return out


def get(proposal_id: str) -> dict | None:
    p = _path(proposal_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def deny(proposal_id: str, reason: str = "") -> bool:
    p = _path(proposal_id)
    if not p.exists():
        return False
    d = json.loads(p.read_text())
    d["status"] = "denied"
    d["denied_at"] = _now()
    d["denial_reason"] = reason
    p.write_text(json.dumps(d, indent=2))
    audit.log("user", "proposal_denied", 1, args={"id": proposal_id}, result=reason[:200])
    return True


def approve(proposal_id: str) -> dict:
    d = get(proposal_id)
    if not d:
        raise KeyError(proposal_id)
    if d.get("status") != "pending":
        raise ValueError(f"proposal {proposal_id} is {d.get('status')}, not pending")

    # Snapshot metrics at apply time so we can measure realized impact later.
    try:
        from sentrial.evolution import metrics
        d["metrics_at_apply"] = metrics.compute_metrics(window_days=7).to_dict()
    except Exception:  # noqa: BLE001
        d["metrics_at_apply"] = None

    target_path = Path(d["target"])
    if not target_path.is_absolute():
        # Resolve relative to repo root (one up from sentrial/)
        target_path = Path(__file__).parent.parent.parent / d["target"]

    # Safety: refuse to edit frozen surfaces
    if _is_frozen(d["target"]):
        raise PermissionError(f"target {d['target']} is frozen per program.md")

    # Verify current state matches "before" to avoid clobbering
    if not target_path.exists():
        raise FileNotFoundError(str(target_path))
    current = target_path.read_text()
    if _sha(current) != d["before_sha"]:
        raise RuntimeError(
            f"target file changed since proposal was made (sha mismatch) — refusing to apply"
        )

    # Backup
    backup_path = _backup_dir() / f"{proposal_id}-{target_path.name}"
    backup_path.write_text(current)

    # Apply
    target_path.write_text(d["after"])
    d["status"] = "applied"
    d["applied_at"] = _now()
    d["backup_path"] = str(backup_path)
    _path(proposal_id).write_text(json.dumps(d, indent=2))
    audit.log(
        "user", "proposal_applied", 2,
        args={"id": proposal_id, "target": d["target"]},
        result=d.get("rationale", "")[:200],
    )
    return d


def revert(proposal_id: str) -> dict:
    d = get(proposal_id)
    if not d or d.get("status") != "applied":
        raise ValueError(f"proposal {proposal_id} is not applied")
    backup_path = Path(d["backup_path"])
    if not backup_path.exists():
        raise FileNotFoundError(str(backup_path))

    target_path = Path(d["target"])
    if not target_path.is_absolute():
        target_path = Path(__file__).parent.parent.parent / d["target"]

    target_path.write_text(backup_path.read_text())
    d["status"] = "reverted"
    d["reverted_at"] = _now()
    _path(proposal_id).write_text(json.dumps(d, indent=2))
    audit.log("user", "proposal_reverted", 2, args={"id": proposal_id})
    return d


FROZEN_PATTERNS = [
    "sentrial/core/secrets.py",
    "sentrial/core/confirmation.py",
    "scripts/",
    "Dockerfile",
    "railway.toml",
    "pyproject.toml",
    "requirements.txt",
    "sentrial/evolution/",
]


def _is_frozen(target: str) -> bool:
    for pat in FROZEN_PATTERNS:
        if target.startswith(pat) or target == pat.rstrip("/"):
            return True
    return False
