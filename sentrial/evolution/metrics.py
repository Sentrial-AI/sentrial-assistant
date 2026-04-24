"""
Quantitative signal for the self-improvement loop.

Metrics are computed from the audit log + conversations in memory.sqlite. See
`program.md` for definitions and directions. All metrics are defensive — if a
source is empty, returns 0.0, not NaN.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sentrial.core import paths

EDIT_MARKERS = re.compile(
    r"\b(actually|instead|no,? (?:can|could|would)|change it|change that|redo|wrong|not what|rewrite|shorter|longer|tighter)\b",
    re.IGNORECASE,
)


@dataclass
class Metrics:
    window_days: int
    n_conversations: int
    n_jobs: int
    edit_rate: float
    tool_denial_rate: float
    clarification_rate: float
    scope_preview_acceptance: float
    avg_latency_s: float
    avg_response_tokens: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _since(window_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()


def _count(con: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = con.execute(sql, params).fetchone()
    return row[0] if row and row[0] else 0


def _audit_conn() -> sqlite3.Connection:
    return sqlite3.connect(paths.audit_db_path())


def _memory_conn() -> sqlite3.Connection:
    return sqlite3.connect(paths.memory_db_path())


def compute_metrics(window_days: int = 7) -> Metrics:
    since = _since(window_days)

    # ----- audit-based metrics -----
    con = _audit_conn()
    try:
        # ensure table exists (it should from audit module)
        con.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER)")
    except sqlite3.OperationalError:
        pass

    # tool denial rate at tier 2
    tier2_total = _count(con, "SELECT COUNT(*) FROM audit WHERE tier = 2 AND timestamp >= ?", (since,))
    tier2_denied = _count(
        con, "SELECT COUNT(*) FROM audit WHERE tier = 2 AND status = 'denied' AND timestamp >= ?", (since,)
    )
    tool_denial_rate = (tier2_denied / tier2_total) if tier2_total else 0.0

    # scope preview acceptance (approved / created)
    jobs_created = _count(
        con, "SELECT COUNT(*) FROM audit WHERE action LIKE 'job_created:%' AND timestamp >= ?", (since,)
    )
    jobs_approved = _count(
        con, "SELECT COUNT(*) FROM audit WHERE action LIKE 'job_approved:%' AND timestamp >= ?", (since,)
    )
    scope_preview_acceptance = (jobs_approved / jobs_created) if jobs_created else 0.0

    con.close()

    # ----- conversation-based metrics -----
    con = _memory_conn()
    try:
        rows = con.execute(
            "SELECT id, turns_json, started_at FROM conversations WHERE started_at >= ?", (since,)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()

    n_conversations = len(rows)
    edit_hits = 0
    total_user_after_sentrial = 0
    clarification_hits = 0
    sentrial_responses = 0
    total_response_chars = 0

    for _cid, turns_json, _started in rows:
        try:
            turns = json.loads(turns_json)
        except (json.JSONDecodeError, TypeError):
            continue

        for i, turn in enumerate(turns):
            content = str(turn.get("content", ""))
            role = turn.get("role")
            if role == "assistant":
                sentrial_responses += 1
                total_response_chars += len(content)
                stripped = content.rstrip()
                if stripped.endswith("?") and not _looks_like_scope_preview(content):
                    clarification_hits += 1
            elif role == "user" and i > 0 and turns[i - 1].get("role") == "assistant":
                total_user_after_sentrial += 1
                if EDIT_MARKERS.search(content):
                    edit_hits += 1

    edit_rate = (edit_hits / total_user_after_sentrial) if total_user_after_sentrial else 0.0
    clarification_rate = (clarification_hits / sentrial_responses) if sentrial_responses else 0.0

    # crude char→tokens approx
    avg_response_tokens = (total_response_chars / sentrial_responses / 4) if sentrial_responses else 0.0

    # ----- latency from audit pairs -----
    con = _audit_conn()
    try:
        events = con.execute(
            "SELECT action, timestamp FROM audit WHERE timestamp >= ? AND "
            "(action = 'inbound' OR action LIKE 'tool:%' OR action LIKE 'tool_denied:%') ORDER BY timestamp",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        events = []
    con.close()

    latencies: list[float] = []
    last_inbound: datetime | None = None
    for action, ts in events:
        if action == "inbound":
            try:
                last_inbound = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                last_inbound = None
        elif last_inbound and action.startswith("tool:"):
            try:
                end = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                latencies.append((end - last_inbound).total_seconds())
                last_inbound = None
            except ValueError:
                pass
    avg_latency_s = (sum(latencies) / len(latencies)) if latencies else 0.0

    return Metrics(
        window_days=window_days,
        n_conversations=n_conversations,
        n_jobs=jobs_created,
        edit_rate=round(edit_rate, 4),
        tool_denial_rate=round(tool_denial_rate, 4),
        clarification_rate=round(clarification_rate, 4),
        scope_preview_acceptance=round(scope_preview_acceptance, 4),
        avg_latency_s=round(avg_latency_s, 2),
        avg_response_tokens=round(avg_response_tokens, 1),
    )


def _looks_like_scope_preview(text: str) -> bool:
    return bool(re.search(r"\b(approve\??|go\??|proceed\??|shall I|should I)\s*\??$", text.strip(), re.IGNORECASE))
