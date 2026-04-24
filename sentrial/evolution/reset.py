"""
Tiered reset — roll back evolved surfaces to a known-good baseline.

Three levels:

  1. "vibes"    — reset only system_prompt.md. Keeps facts + lessons + KG +
                  playbooks + profile.
  2. "behaviors"— level 1 PLUS retire all lessons, wipe playbooks, reset
                  profile to base-with-preserved-vocabulary. Keeps the KG.
  3. "full"     — wipe everything learned: profile, lessons, playbooks, KG,
                  tier overrides. Restores all base templates.

Every reset:
  - makes a timestamped backup into /data/evolution/resets/<ts>/
  - audits the action with the level + backup path
  - is reversible up to the backup retention window (default 30d)

Use from the CLI / HTTP endpoint; never from the agent itself.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sentrial.core import audit, paths
from sentrial.evolution import kg, lessons, playbooks

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent / "base"
SYSTEM_PROMPT_LIVE = Path(__file__).parent.parent / "config" / "system_prompt.md"
PROFILE_LIVE = lambda: paths.data_dir() / "evolution" / "user_profile.yaml"  # noqa: E731
LESSONS_LIVE = lambda: paths.data_dir() / "evolution" / "lessons"  # noqa: E731
PLAYBOOKS_LIVE = lambda: paths.data_dir() / "evolution" / "playbooks"  # noqa: E731
KG_DB_LIVE = lambda: paths.data_dir() / "evolution" / "kg.sqlite"  # noqa: E731

RESET_DIR = lambda: paths.data_dir() / "evolution" / "resets"  # noqa: E731
LEVELS = ("vibes", "behaviors", "full")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_root() -> Path:
    p = RESET_DIR() / _ts()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return True
    return False


def reset(level: str, confirm_token: str | None = None) -> dict[str, Any]:
    """
    Perform a tiered reset. `level` in {"vibes", "behaviors", "full"}.
    `confirm_token` is required for levels ≥ behaviors — pass the exact level
    string back as a second-factor so accidental API calls can't wipe data.
    """
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}; expected one of {LEVELS}")
    if level in ("behaviors", "full") and confirm_token != level:
        raise PermissionError(
            f"reset level {level!r} requires confirm_token={level!r}"
        )

    backup_root = _backup_root()
    wiped: list[str] = []
    restored: list[str] = []

    # ---- Level 1+: system_prompt ----
    _copy_if_exists(SYSTEM_PROMPT_LIVE, backup_root / "system_prompt.md")
    base_prompt = BASE_DIR / "system_prompt.md"
    if base_prompt.exists():
        SYSTEM_PROMPT_LIVE.write_text(base_prompt.read_text())
        restored.append("system_prompt.md")

    if level == "vibes":
        return _finalize(level, backup_root, wiped, restored)

    # ---- Level 2+: behaviors ----
    # Lessons: retire all active ones. Files stay (archive-only).
    lessons_dir = LESSONS_LIVE()
    if lessons_dir.exists():
        _copy_if_exists(lessons_dir, backup_root / "lessons")
        for lesson in lessons.list_all(status="active"):
            lessons.retire(lesson["id"], reason=f"reset:{level}")
        wiped.append("lessons.active")

    # Playbooks: backup + delete all.
    playbooks_dir = PLAYBOOKS_LIVE()
    if playbooks_dir.exists():
        _copy_if_exists(playbooks_dir, backup_root / "playbooks")
        for f in list(playbooks_dir.glob("*.md")) + list(playbooks_dir.glob("*.meta.json")):
            try:
                f.unlink()
            except OSError:
                pass
        wiped.append("playbooks")

    # Profile: reset preferences, schedule, knowledge; keep vocabulary (shorthand
    # Liam taught us is still valid).
    profile_path = PROFILE_LIVE()
    if profile_path.exists():
        _copy_if_exists(profile_path, backup_root / "user_profile.yaml")
        try:
            import yaml
            base = yaml.safe_load((BASE_DIR / "user_profile.yaml").read_text())
            live = yaml.safe_load(profile_path.read_text()) or {}
            preserved_vocab = (live.get("vocabulary") or {}).get("shorthand") or {}
            base.setdefault("vocabulary", {})["shorthand"] = preserved_vocab
            profile_path.write_text(yaml.safe_dump(base, sort_keys=False, allow_unicode=True))
            wiped.append("profile(preferences+schedule+knowledge)")
            restored.append("profile.structure")
        except Exception as e:  # noqa: BLE001
            log.warning("profile reset failed: %s", e)

    if level == "behaviors":
        return _finalize(level, backup_root, wiped, restored)

    # ---- Level 3: full ----
    # KG: backup + drop the sqlite file entirely.
    kg_db = KG_DB_LIVE()
    if kg_db.exists():
        _copy_if_exists(kg_db, backup_root / "kg.sqlite")
        try:
            kg_db.unlink()
            wiped.append("kg")
        except OSError as e:
            log.warning("could not remove kg db: %s", e)

    # Profile: this time also wipe vocabulary.
    if profile_path.exists():
        try:
            import yaml
            base = yaml.safe_load((BASE_DIR / "user_profile.yaml").read_text())
            profile_path.write_text(yaml.safe_dump(base, sort_keys=False, allow_unicode=True))
            wiped.append("profile(vocabulary)")
        except Exception as e:  # noqa: BLE001
            log.warning("profile full reset failed: %s", e)

    return _finalize(level, backup_root, wiped, restored)


def _finalize(
    level: str, backup_root: Path, wiped: list[str], restored: list[str],
) -> dict[str, Any]:
    manifest = {
        "level": level,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backup_root": str(backup_root),
        "wiped": wiped,
        "restored": restored,
    }
    (backup_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    audit.log(
        "user", f"reset:{level}", 3,
        args={"backup_root": str(backup_root)},
        result=f"wiped={len(wiped)} restored={len(restored)}",
    )
    return manifest


def list_backups() -> list[dict]:
    root = RESET_DIR()
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        m = d / "manifest.json"
        if m.exists():
            try:
                out.append(json.loads(m.read_text()))
            except json.JSONDecodeError:
                pass
    return out
