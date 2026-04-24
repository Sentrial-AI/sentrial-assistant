"""
Google OAuth token lifecycle — shared helper for the Gmail + Calendar MCPs.

Token storage layout (in memory.facts, scope='oauth', key='google'):
  {
    access_token:  str,
    refresh_token: str,
    token_type:    "Bearer",
    scope:         str (space-separated),
    expires_in:    int (seconds, from Google),
    issued_at:     ISO datetime,   <- we add this at save time so we can
                                      check expiry without another call
    expires_at:    ISO datetime,   <- ditto
  }

API surface:
  is_connected()              -> bool
  ensure_access_token()       -> str      (refreshes if expired; raises on fail)
  get_stored_token()          -> dict|None
  save_token(raw_token_dict)  -> None    (called from the OAuth callback)
  disconnect()                -> bool    (forget stored token)
  scopes()                    -> set[str]

The callback in webhook.py uses save_token() so expires_at is populated on
every fresh authorization; the MCPs use ensure_access_token() for every
request. No direct Google SDK dependency — plain httpx against the REST
endpoints keeps the install lean.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sentrial.core import audit, memory, secrets

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
REFRESH_SAFETY_S = 60      # refresh if <60s left on the access_token


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---- public API ----

def is_connected() -> bool:
    tok = get_stored_token()
    return bool(tok and tok.get("access_token"))


def get_stored_token() -> dict | None:
    return memory.recall("oauth", "google")


def scopes() -> set[str]:
    tok = get_stored_token()
    if not tok:
        return set()
    return set((tok.get("scope") or "").split())


def disconnect() -> bool:
    ok = memory.forget("oauth", "google")
    if ok:
        audit.log("user", "oauth_disconnect:google", 2, result="forgot stored token")
    return ok


def save_token(raw: dict, prior_refresh_token: str | None = None) -> dict:
    """
    Normalize and persist a raw Google token response. Google doesn't always
    re-issue a refresh_token on subsequent authorizations — if missing, we
    keep the prior one. `prior_refresh_token` is passed in by the refresh
    flow; the first-time callback flow relies on the raw dict carrying it.
    """
    now = _now()
    expires_in = int(raw.get("expires_in") or 3600)
    refresh = raw.get("refresh_token")
    if not refresh and prior_refresh_token:
        refresh = prior_refresh_token
    doc = {
        "access_token":  raw.get("access_token"),
        "refresh_token": refresh,
        "token_type":    raw.get("token_type") or "Bearer",
        "scope":         raw.get("scope") or "",
        "id_token":      raw.get("id_token"),
        "expires_in":    expires_in,
        "issued_at":     _iso(now),
        "expires_at":    _iso(now + timedelta(seconds=expires_in)),
    }
    memory.remember("oauth", "google", doc)
    return doc


async def ensure_access_token() -> str:
    """
    Return a valid access_token, refreshing if the stored one is near/past
    its expiry. Raises RuntimeError with a clear message if Google isn't
    connected or the refresh fails — MCP tools should surface this to the
    agent so it can tell the user to reconnect.
    """
    tok = get_stored_token()
    if not tok or not tok.get("access_token"):
        raise RuntimeError(
            "Google not connected — visit /api/oauth/google/start to authorize."
        )

    expires_at = _parse(tok.get("expires_at"))
    needs_refresh = (
        expires_at is None
        or (expires_at - _now()).total_seconds() < REFRESH_SAFETY_S
    )
    if not needs_refresh:
        return tok["access_token"]

    # Refresh flow.
    refresh = tok.get("refresh_token")
    if not refresh:
        raise RuntimeError(
            "Google access token expired and no refresh_token stored — "
            "reconnect at /api/oauth/google/start?prompt=consent."
        )
    client_id = secrets.get("google_client_id") or os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = secrets.get("google_client_secret") or os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not configured; "
            "cannot refresh access token."
        )

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code != 200:
        log.warning("google refresh failed %s: %s", r.status_code, r.text[:200])
        raise RuntimeError(
            f"Google token refresh failed ({r.status_code}). Reconnect required."
        )
    new = save_token(r.json(), prior_refresh_token=refresh)
    audit.log("sentrial", "oauth_refresh:google", 1, result="ok")
    return new["access_token"]
