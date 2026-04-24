"""
Centralized data-directory resolution. Works on Mac (local dev) and Linux (Railway).

Priority:
  1. $SENTRIAL_DATA_DIR env var (explicit override)
  2. /data if it exists + is writable (Railway volume convention)
  3. ~/Library/Application Support/Sentrial (Mac local fallback)
  4. ~/.sentrial (generic Linux/other fallback)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("SENTRIAL_DATA_DIR")
    if override:
        p = Path(override).expanduser()
    elif Path("/data").is_dir() and os.access("/data", os.W_OK):
        p = Path("/data")
    elif sys.platform == "darwin":
        p = Path.home() / "Library/Application Support/Sentrial"
    else:
        p = Path.home() / ".sentrial"
    p.mkdir(parents=True, exist_ok=True)
    return p


def audit_db_path() -> Path:
    return data_dir() / "audit.sqlite"


def memory_db_path() -> Path:
    return data_dir() / "memory.sqlite"


def jobs_dir() -> Path:
    p = data_dir() / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def deliverables_dir() -> Path:
    p = data_dir() / "deliverables"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p
