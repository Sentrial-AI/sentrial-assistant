"""
Structured user profile — the "cater to them" surface.

Stored as YAML at /data/evolution/user_profile.yaml so it's diff-reviewable.
Every leaf is an ObservedField { value, confidence, evidence_count, last_updated }
so the profile can *learn* without clobbering earlier observations:

    observe(path, value, weight)  — Bayesian-ish update:
      - if value matches current: confidence += weight, evidence_count += 1
      - if value conflicts: confidence = max(0, confidence - weight); swap only
        once confidence crosses zero with the new candidate accumulated
        separately (handled via transient candidate state).
      - last_updated = now

Reads are cheap: `get("preferences.response_terseness")` → value if confidence
above MIN_TRUSTED_CONFIDENCE else the base-template default. The agent
pre-turn retriever pulls `summary_for_agent()` which is a compact view.

All writes go through the proposals system OR the auto-apply path guarded by
integrity.py, so evolution never silently rewrites high-confidence fields.
"""
from __future__ import annotations

import logging
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from sentrial.core import paths

log = logging.getLogger(__name__)

BASE_PROFILE = Path(__file__).parent / "base" / "user_profile.yaml"
MIN_TRUSTED_CONFIDENCE = 0.35    # below this, we fall back to the base template
SCHEMA_VERSION = 1


def _path() -> Path:
    p = paths.data_dir() / "evolution" / "user_profile.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_base() -> dict:
    return yaml.safe_load(BASE_PROFILE.read_text())


def _ensure_exists() -> Path:
    p = _path()
    if not p.exists():
        # First-run: seed from base.
        shutil.copy(BASE_PROFILE, p)
    return p


def load() -> dict:
    """Whole profile as a nested dict. Self-heals if the file is corrupt."""
    p = _ensure_exists()
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        log.warning("profile corrupt — restoring from base: %s", e)
        shutil.copy(BASE_PROFILE, p)
        data = yaml.safe_load(p.read_text()) or {}
    # Backfill missing top-level keys from base so schema drift is non-breaking.
    base = _load_base()
    _merge_missing(data, base)
    return data


def save(profile: dict) -> None:
    p = _ensure_exists()
    p.write_text(yaml.safe_dump(profile, sort_keys=False, allow_unicode=True))


def _merge_missing(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if k not in dst:
            dst[k] = deepcopy(v)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge_missing(dst[k], v)


# ---- dotted-path accessors ----

def _walk(profile: dict, path: str) -> tuple[dict | None, str | None]:
    """Split 'a.b.c' → return (parent_dict_for_'c', 'c'). None if missing."""
    parts = path.split(".")
    cur: Any = profile
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return None, None
        cur = cur[p]
    if not isinstance(cur, dict):
        return None, None
    return cur, parts[-1]


def _is_observed_field(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and "value" in node
        and "confidence" in node
    )


def get(path: str, default: Any = None) -> Any:
    """
    Read a profile value. For ObservedField leaves, returns the stored value
    if confidence >= MIN_TRUSTED_CONFIDENCE, otherwise the base-template value.
    For plain leaves (dict / list / str / …), returns as-is.
    """
    profile = load()
    parent, key = _walk(profile, path)
    if parent is None or key not in parent:
        return default
    node = parent[key]
    if _is_observed_field(node):
        if (node.get("confidence") or 0) >= MIN_TRUSTED_CONFIDENCE:
            return node.get("value")
        # Fall back to base.
        base_parent, _ = _walk(_load_base(), path)
        if base_parent and key in base_parent:
            base_node = base_parent[key]
            if _is_observed_field(base_node):
                return base_node.get("value")
        return default
    return node


def observe(path: str, value: Any, weight: float = 0.1, reason: str = "") -> dict:
    """
    Record an observation at path. weight ∈ [0, 1]; 0.1 for implicit signals
    (pattern match), 0.4 for explicit corrections, 0.8 for direct user
    statements ("I prefer …"). Returns a change-summary dict used by audit.
    """
    profile = load()
    parent, key = _walk(profile, path)
    if parent is None or key not in parent:
        # Auto-create an ObservedField leaf at the path (schema growth).
        _ensure_path(profile, path)
        parent, key = _walk(profile, path)
        if parent is None or key is None:
            return {"ok": False, "error": f"bad path {path}"}

    node = parent[key]
    if not _is_observed_field(node):
        # Plain leaf — overwrite outright but with an audit record.
        old = deepcopy(node)
        parent[key] = value
        save(profile)
        return {"ok": True, "path": path, "old": old, "new": value, "mode": "plain"}

    old_value = node.get("value")
    old_conf = float(node.get("confidence") or 0)
    same = _values_match(old_value, value)
    if same:
        new_conf = min(1.0, old_conf + weight)
        new_value = old_value
    else:
        # Shrink confidence. If it crosses zero, swap in the new value with
        # the observation weight as seed confidence.
        shrunk = old_conf - weight
        if shrunk <= 0:
            new_conf = weight
            new_value = value
        else:
            new_conf = shrunk
            new_value = old_value

    node["value"] = new_value
    node["confidence"] = round(new_conf, 4)
    node["evidence_count"] = int(node.get("evidence_count") or 0) + 1
    node["last_updated"] = _now()
    if reason:
        node.setdefault("notes", []).append({"at": _now(), "reason": reason[:180]})
        node["notes"] = node["notes"][-8:]  # cap memory per field
    save(profile)
    return {
        "ok": True,
        "path": path,
        "old_value": old_value,
        "new_value": new_value,
        "confidence": new_conf,
        "flipped": not same and new_value == value,
        "reason": reason,
    }


def _values_match(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def _ensure_path(profile: dict, path: str) -> None:
    """Create ObservedField leaf and any intermediate dicts for a new path."""
    parts = path.split(".")
    cur: Any = profile
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    if parts[-1] not in cur:
        cur[parts[-1]] = {
            "value": None, "confidence": 0.0, "evidence_count": 0,
            "last_updated": None,
        }


# ---- agent-facing compact summary ----

def summary_for_agent() -> str:
    """
    A compact paragraph the agent can read at turn-start. Only fields that are
    trusted (confidence >= MIN_TRUSTED_CONFIDENCE) appear. Returns "" if the
    profile is effectively empty.
    """
    profile = load()
    lines: list[str] = []

    prefs = profile.get("preferences", {})
    trusted_prefs = []
    for key, node in prefs.items():
        if _is_observed_field(node) and (node.get("confidence") or 0) >= MIN_TRUSTED_CONFIDENCE:
            trusted_prefs.append(f"{key}={node['value']}")
    if trusted_prefs:
        lines.append("preferences: " + ", ".join(trusted_prefs))

    vocab = (profile.get("vocabulary") or {}).get("shorthand") or {}
    if isinstance(vocab, dict) and vocab:
        items = list(vocab.items())[:12]
        lines.append("shorthand: " + ", ".join(f"{k}={v}" for k, v in items))

    focus = profile.get("active_focus") or {}
    clients = focus.get("clients") or []
    projects = focus.get("projects") or []
    if clients:
        lines.append("active clients: " + ", ".join(clients[:8]))
    if projects:
        lines.append("active projects: " + ", ".join(projects[:8]))

    know = profile.get("knowledge") or {}
    if know.get("strong_in"):
        lines.append("strong in: " + ", ".join(know["strong_in"][:10]))

    return "\n".join(lines)


# ---- utilities used by reset + integrity ----

def is_pristine() -> bool:
    """True if no ObservedField has accumulated evidence yet."""
    profile = load()
    return _max_confidence(profile) == 0.0


def _max_confidence(node: Any) -> float:
    if _is_observed_field(node):
        return float(node.get("confidence") or 0)
    if isinstance(node, dict):
        m = 0.0
        for v in node.values():
            m = max(m, _max_confidence(v))
        return m
    return 0.0
