"""
Notification output — how Sentrial reaches Liam when he's not looking at the PWA.

Order of preference:
  1. Web Push  — works when the PWA is closed/backgrounded (iOS 16.4+ from home screen)
  2. Pushover  — optional always-on fallback
  3. iMessage  — only if running on macOS AND LIAM_PHONE is set (local companion mode)
  4. Log-only  — last resort
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from sentrial.core import audit
from sentrial.core import memory as mem
from sentrial.core import secrets as kc

log = logging.getLogger(__name__)


async def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


# --------------------------- Web Push ---------------------------

async def _try_web_push(text: str, title: str = "Sentrial") -> bool:
    subs = mem.list_push_subscriptions()
    if not subs:
        return False
    vapid_private = kc.get("vapid_private_key")
    vapid_claims_email = kc.get("vapid_contact") or "mailto:liamtride@gmail.com"
    if not vapid_private:
        log.warning("web push: no VAPID_PRIVATE_KEY set; skipping")
        return False
    try:
        from pywebpush import WebPushException, webpush  # type: ignore
    except ImportError:
        log.warning("pywebpush not installed; web push disabled")
        return False

    payload = json.dumps({"title": title, "body": text})
    any_ok = False
    loop = asyncio.get_event_loop()

    def _send_one(sub: dict) -> bool:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": sub["keys"],
                },
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": vapid_claims_email},
            )
            return True
        except WebPushException as e:  # type: ignore
            # 410 Gone → subscription invalid; remove
            if getattr(e, "response", None) is not None and e.response.status_code in (404, 410):
                mem.remove_push_subscription(sub["endpoint"])
            log.warning("web push failed: %s", e)
            return False
        except Exception as e:  # noqa: BLE001
            log.warning("web push error: %s", e)
            return False

    for sub in subs:
        ok = await loop.run_in_executor(None, _send_one, sub)
        any_ok = any_ok or ok
    return any_ok


# --------------------------- Pushover ---------------------------

async def _try_pushover(text: str, title: str = "Sentrial") -> bool:
    token = kc.get("pushover_token")
    user = kc.get("pushover_user")
    if not token or not user:
        return False
    cmd = [
        "curl", "-s", "-S", "--max-time", "10",
        "-F", f"token={token}",
        "-F", f"user={user}",
        "-F", f"title={title}",
        "-F", f"message={text}",
        "https://api.pushover.net/1/messages.json",
    ]
    rc, out, _ = await _run(cmd)
    return rc == 0 and '"status":1' in out


# --------------------------- iMessage (Mac-only) ---------------------------

def _is_mac() -> bool:
    return sys.platform == "darwin"


def _applescript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


async def _try_imessage(text: str) -> bool:
    if not _is_mac():
        return False
    phone = kc.get("liam_phone")
    if not phone:
        return False
    script = (
        'tell application "Messages"\n'
        '  set targetService to 1st service whose service type = iMessage\n'
        f'  set targetBuddy to buddy "{_applescript_escape(phone)}" of targetService\n'
        f'  send "{_applescript_escape(text)}" to targetBuddy\n'
        'end tell'
    )
    rc, _, err = await _run(["osascript", "-e", script])
    if rc != 0:
        log.warning("iMessage send failed: %s", err.strip())
        return False
    return True


# --------------------------- Public API ---------------------------

async def send(text: str, title: str = "Sentrial") -> None:
    """Deliver a notification to Liam via the first channel that succeeds."""
    for attempt in (_try_web_push(text, title), _try_pushover(text, title), _try_imessage(text)):
        ok = await attempt
        if ok:
            audit.log("sentrial", "notify", 1, result=text[:400])
            return
    log.info("notify (no channel delivered): %s", text)
    audit.log("sentrial", "notify", 1, result=text[:400], status="no_channel")
