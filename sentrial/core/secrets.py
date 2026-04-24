"""
Secret resolution. Env-vars first (cloud), macOS Keychain as fallback (local Mac dev).

Env var convention: uppercase of the secret key.
  anthropic_api_key  ->  ANTHROPIC_API_KEY
  notion_api_key     ->  NOTION_API_KEY
  webhook_shared_secret -> WEBHOOK_SHARED_SECRET (or SENTRIAL_TOKEN — see ensure_token)

On Railway, set these in the service's Variables tab. Locally, either set shell env
or use `sentrial setup` which writes to Keychain.
"""
from __future__ import annotations

import os
import subprocess
import sys

SERVICE_PREFIX = "com.sentrial"
ACCOUNT = "sentrial"


class KeychainError(Exception):
    pass


def _is_mac() -> bool:
    return sys.platform == "darwin"


def _env_key(key: str) -> str:
    return key.upper()


def _keychain_get(key: str) -> str | None:
    if not _is_mac():
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", f"{SERVICE_PREFIX}.{key}", "-a", ACCOUNT, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
        return r.stdout.rstrip("\n")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get(key: str) -> str | None:
    """Env var first, Keychain fallback."""
    val = os.environ.get(_env_key(key))
    if val:
        return val
    return _keychain_get(key)


def require(key: str) -> str:
    val = get(key)
    if val is None:
        raise KeychainError(
            f"Missing secret '{key}'. Set env var {_env_key(key)} (Railway) "
            f"or run: security add-generic-password -s '{SERVICE_PREFIX}.{key}' "
            f"-a '{ACCOUNT}' -w 'YOUR_VALUE' -U"
        )
    return val


def set(key: str, value: str) -> None:  # noqa: A001
    """Store a secret locally (Mac Keychain). Raises on non-Mac — use env vars there."""
    if not _is_mac():
        raise KeychainError(
            "Cannot set secrets at runtime on non-Mac platforms. "
            "Set environment variables on your cloud host instead."
        )
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-s",
            f"{SERVICE_PREFIX}.{key}",
            "-a",
            ACCOUNT,
            "-w",
            value,
            "-U",
        ],
        check=True,
        capture_output=True,
    )


def delete(key: str) -> bool:
    if not _is_mac():
        return False
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", f"{SERVICE_PREFIX}.{key}", "-a", ACCOUNT],
            capture_output=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def ensure_token() -> str:
    """
    Return the shared secret used to authorize PWA and iOS Shortcut requests.
    Env var SENTRIAL_TOKEN or WEBHOOK_SHARED_SECRET. If neither set on a Mac, generates
    one into Keychain so local dev works without manual setup.
    """
    for k in ("sentrial_token", "webhook_shared_secret"):
        v = get(k)
        if v:
            return v
    if _is_mac():
        import secrets as _stdlib
        token = _stdlib.token_urlsafe(32)
        set("sentrial_token", token)
        return token
    raise KeychainError(
        "No SENTRIAL_TOKEN set. Generate one with `python -c \"import secrets; "
        "print(secrets.token_urlsafe(32))\"` and add it to Railway env vars."
    )
