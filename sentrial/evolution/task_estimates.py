"""
Learned task-duration estimates — the "what can Liam actually get done in
the time he has" surface.

Each estimate is keyed by a task *pattern* — a short string like
"proposal_saas" or "audit_site" — and carries running statistics over a
rolling window of observed durations:
    p50_minutes / p90_minutes / sample_count / confidence / last_observed_at

The reschedule tool reads these to decide whether it can squeeze N tasks
into the remaining window; distillation writes them based on completion
timestamps and explicit "took me 2 hours" statements.

Stored one-per-file at /data/evolution/task_estimates/<pattern>.json so
each is independently reviewable and reversible. SQLite would be overkill
for a few dozen patterns.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sentrial.core import audit, paths

log = logging.getLogger(__name__)

WINDOW_SAMPLES = 30
MIN_SAMPLES_FOR_TRUST = 3
_SLUG_RE = re.compile(r"[^a-z0-9_-]")

# Sensible cold-start defaults so reschedule_day has something to work with
# before learning kicks in. Tuned to knowledge-worker averages; will be
# overridden by real observations as samples accumulate.
COLD_START: dict[str, int] = {
    "proposal":           60,
    "proposal_saas":      60,
    "audit":              45,
    "audit_site":         45,
    "demo":               90,
    "followup_email":     10,
    "cold_outreach":      15,
    "daily_brief":        10,
    "notion_update":       5,
    "meeting":            30,
    "1on1":               30,
    "deep_work":          90,
    "admin":              15,
    "default":            30,
}


def _dir() -> Path:
    p = paths.data_dir() / "evolution" / "task_estimates"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(pattern: str) -> str:
    return _SLUG_RE.sub("", pattern.strip().lower().replace(" ", "_"))[:64] or "default"


def _path(pattern: str) -> Path:
    return _dir() / f"{_slugify(pattern)}.json"


# ---- CRUD / observation ----

def record(pattern: str, minutes: float, source: str = "observation") -> dict:
    """
    Record one observed duration for a pattern. Maintains a rolling window of
    the last WINDOW_SAMPLES samples, recomputes p50/p90/mean, and bumps
    confidence toward 1.0 with each new sample.
    """
    if minutes <= 0 or minutes > 24 * 60:
        return {"ok": False, "error": f"implausible minutes={minutes}"}
    p = _path(pattern)
    doc = _load_or_seed(pattern)
    samples: list[float] = doc.get("samples", [])
    samples.append(float(minutes))
    samples = samples[-WINDOW_SAMPLES:]
    doc["samples"] = samples
    doc["sample_count"] = len(samples)
    doc["last_observed_at"] = _now()
    doc["updated_at"] = _now()
    doc["sources"] = (doc.get("sources") or []) + [source]
    doc["sources"] = doc["sources"][-WINDOW_SAMPLES:]
    doc.update(_compute_stats(samples))
    # Confidence climbs fast early, asymptotes at ~0.95.
    n = len(samples)
    doc["confidence"] = round(1 - math.exp(-n / 6), 4)
    p.write_text(json.dumps(doc, indent=2))
    audit.log(
        "sentrial", "task_estimate_observed", 1,
        args={"pattern": _slugify(pattern), "minutes": minutes, "source": source},
        result=f"p50={doc['p50_minutes']} n={n}",
    )
    return doc


def get(pattern: str) -> dict:
    """Fetch the estimate for a pattern. If unseen, returns a cold-start seed."""
    return _load_or_seed(pattern)


def list_all() -> list[dict]:
    out: list[dict] = []
    for f in sorted(_dir().glob("*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def p50(pattern: str) -> int:
    """Cheap accessor for the reschedule solver — returns p50 minutes."""
    return int(get(pattern).get("p50_minutes") or _cold_start(pattern))


def p90(pattern: str) -> int:
    return int(get(pattern).get("p90_minutes") or _cold_start(pattern) * 1.5)


def estimate_fits(pattern: str, available_minutes: int, risk: str = "p50") -> bool:
    est = p50(pattern) if risk == "p50" else p90(pattern)
    return est <= available_minutes


# ---- internals ----

def _cold_start(pattern: str) -> int:
    slug = _slugify(pattern)
    if slug in COLD_START:
        return COLD_START[slug]
    # Prefix match — e.g., "proposal_long_tail" → "proposal".
    for k, v in COLD_START.items():
        if slug.startswith(k + "_") or slug.startswith(k):
            return v
    return COLD_START["default"]


def _load_or_seed(pattern: str) -> dict:
    p = _path(pattern)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    cold = _cold_start(pattern)
    seed = {
        "pattern": _slugify(pattern),
        "samples": [],
        "sample_count": 0,
        "p50_minutes": cold,
        "p90_minutes": int(cold * 1.5),
        "mean_minutes": cold,
        "confidence": 0.0,
        "source": "cold_start",
        "created_at": _now(),
        "updated_at": _now(),
        "last_observed_at": None,
    }
    return seed


def _compute_stats(samples: list[float]) -> dict:
    if not samples:
        return {"p50_minutes": 0, "p90_minutes": 0, "mean_minutes": 0}
    s = sorted(samples)
    n = len(s)

    def _quantile(q: float) -> float:
        if n == 1:
            return s[0]
        pos = q * (n - 1)
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return s[lo]
        return s[lo] + (s[hi] - s[lo]) * (pos - lo)

    return {
        "p50_minutes": int(round(_quantile(0.5))),
        "p90_minutes": int(round(_quantile(0.9))),
        "mean_minutes": int(round(sum(s) / n)),
    }
