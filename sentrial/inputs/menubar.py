"""
Menubar app — rumps-based. Must run on the macOS main thread (separate process
from the async daemon). Launched by scripts/menubar.py via a second launchd plist.

The menubar is the primary in-person input channel. Flow:
  1. User presses hotkey (system-wide, configured via macOS) or clicks the icon
  2. Popup appears; user dictates via Wispr Flow or types
  3. Popup sends text to the daemon's /inbound endpoint
  4. Reply surfaces as a macOS notification
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import requests
import rumps

from sentrial.core import audit
from sentrial.core import secrets as kc

log = logging.getLogger(__name__)

WEBHOOK_URL = "http://127.0.0.1:8765/inbound"
APPROVE_URL = "http://127.0.0.1:8765/approve"

# Menubar icon — black-on-transparent template PNG; macOS auto-tints for dark/light.
ICON_PATH = str(Path(__file__).parent.parent / "assets" / "menubar-icon.png")


class SentrialMenubar(rumps.App):
    def __init__(self):
        # name="" drops the "Sentrial" text; icon + template=True gives a clean
        # vector-like rendering that adapts to light/dark menubar.
        super().__init__("", icon=ICON_PATH, template=True, quit_button=None)
        self.menu = [
            rumps.MenuItem("Ask…", callback=self._ask),
            None,
            rumps.MenuItem("View Audit Log", callback=self._audit),
            rumps.MenuItem("Active Jobs", callback=self._jobs),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]

    def _ask(self, _):
        window = rumps.Window(
            title="Sentrial",
            message="Dictate (Wispr Flow) or type:",
            default_text="",
            ok="Send",
            cancel="Cancel",
            dimensions=(420, 120),
        )
        response = window.run()
        if not (response.clicked and response.text.strip()):
            return
        text = response.text.strip()
        audit.log("user", "inbound:menubar", 0, result=text[:300])
        threading.Thread(target=self._send, args=(text,), daemon=True).start()

    def _send(self, text: str):
        token = kc.get("webhook_shared_secret") or ""
        try:
            r = requests.post(
                WEBHOOK_URL,
                headers={"X-Sentrial-Token": token},
                json={"text": text, "channel": "menubar"},
                timeout=120,  # agent turn with tools can take up to ~30s
            )
            r.raise_for_status()
            data = r.json() or {}
            reply = data.get("reply") or "(no reply)"
            rumps.notification(title="Sentrial", subtitle="", message=reply[:240])
        except Exception as e:  # noqa: BLE001
            log.exception("menubar send failed")
            rumps.notification(title="Sentrial error", subtitle="", message=str(e)[:200])

    def _audit(self, _):
        from sentrial.core import audit as a
        rows = a.tail(20)
        lines = [f"{r['timestamp'][:19]} [{r['tier']}] {r['action']}" for r in rows]
        rumps.alert("Audit — last 20", "\n".join(lines) if lines else "(empty)")

    def _jobs(self, _):
        from sentrial.core.task_runner import TaskRunner
        tr = TaskRunner(notifier=_noop_notify, executors={})
        jobs = tr.list_recent(20)
        if not jobs:
            rumps.alert("Jobs", "(none)")
            return
        lines = [f"{j.id}  {j.kind:15s}  {j.status.value}" for j in jobs]
        rumps.alert("Jobs — recent 20", "\n".join(lines))


async def _noop_notify(_msg: str) -> None:
    pass


def _hide_from_dock() -> None:
    """
    Hide the Python process from the Dock and Cmd-Tab switcher. This turns the
    app into a menubar-only "accessory" — what most menubar tools (1Password,
    Bartender, etc.) do.
    """
    try:
        from AppKit import NSApplication  # type: ignore
        # NSApplicationActivationPolicyAccessory = 1
        NSApplication.sharedApplication().setActivationPolicy_(1)
    except Exception as e:  # noqa: BLE001
        log.warning("could not hide from Dock (AppKit unavailable): %s", e)


def run():
    logging.basicConfig(level=logging.INFO)
    _hide_from_dock()
    SentrialMenubar().run()


if __name__ == "__main__":
    run()
