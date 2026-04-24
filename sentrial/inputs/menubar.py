"""
Native macOS menubar app — NSStatusItem + NSPopover + WKWebView, with detach-to-
floating-panel support.

  Left-click the icon      → toggle popover (drops down, transient)
  Option-click the icon    → detach popover into a floating, draggable, resizable panel
  Close the floating panel → reattach (next click shows popover again)

The floating panel persists its frame (position + size) across launches via
NSUserDefaults. The WebKit view is reparented between popover and panel without
losing state (localStorage, conversation, scroll position).

Local daemon is NOT required; the WebView talks straight to Railway.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

ICON_PATH = str(Path(__file__).parent.parent / "assets" / "menubar-icon.png")

DEFAULT_URL = "https://sentrial-assistant-production.up.railway.app/ui/"
PWA_URL = os.environ.get("SENTRIAL_URL", DEFAULT_URL)

POPOVER_W = 440
POPOVER_H = 700
PANEL_MIN_W = 300
PANEL_MIN_H = 360
FRAME_DEFAULTS_KEY = "sentrial.panel.frame"

# Module-level strong reference — prevents ARC from reclaiming the controller.
_controller_ref = None


def _require_mac():
    if sys.platform != "darwin":
        print("menubar.py runs on macOS only.", file=sys.stderr)
        sys.exit(1)


def run():
    _require_mac()
    logging.basicConfig(level=logging.INFO)

    # Imports inside run() so the module can still be imported on Linux.
    import objc
    from AppKit import (
        NSApplication,
        NSBackingStoreBuffered,
        NSEvent,
        NSEventMaskFlagsChanged,
        NSEventMaskLeftMouseUp,
        NSEventMaskRightMouseUp,
        NSEventModifierFlagOption,
        NSFloatingWindowLevel,
        NSImage,
        NSPanel,
        NSPopover,
        NSStatusBar,
        NSView,
        NSViewController,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskHUDWindow,
        NSWindowStyleMaskResizable,
        NSWindowStyleMaskTitled,
        NSWindowStyleMaskUtilityWindow,
    )
    from Foundation import (
        NSMakeRect,
        NSObject,
        NSURL,
        NSURLRequest,
        NSURLRequestReloadIgnoringLocalCacheData,
        NSUserDefaults,
    )
    from WebKit import (
        WKUserContentController,
        WKWebView,
        WKWebViewConfiguration,
    )

    from sentrial.core import secrets as kc
    from sentrial.inputs.voice import VoiceSession
    from sentrial.inputs import tts as tts_mod

    NSApplicationActivationPolicyAccessory = 1
    NSPopoverBehaviorTransient = 1
    NSRectEdgeMinY = 1
    NSViewWidthSizable = 2
    NSViewHeightSizable = 16
    NSEventTypeLeftMouseUp = 2
    NSEventTypeRightMouseUp = 4
    NSEventTypeFlagsChanged = 12

    # Raw device-specific modifier masks (preserved in NSEvent.modifierFlags)
    RIGHT_OPTION_MASK = 0x00000040  # NX_DEVICERALTKEYMASK

    def _saved_frame():
        ud = NSUserDefaults.standardUserDefaults()
        raw = ud.stringForKey_(FRAME_DEFAULTS_KEY)
        if not raw:
            return NSMakeRect(120, 120, POPOVER_W, POPOVER_H)
        try:
            x, y, w, h = (float(v) for v in raw.split(","))
            if w < PANEL_MIN_W or h < PANEL_MIN_H:
                w, h = POPOVER_W, POPOVER_H
            return NSMakeRect(x, y, w, h)
        except (ValueError, AttributeError):
            return NSMakeRect(120, 120, POPOVER_W, POPOVER_H)

    def _save_frame(frame):
        ud = NSUserDefaults.standardUserDefaults()
        o = frame.origin
        s = frame.size
        ud.setObject_forKey_(
            f"{o.x},{o.y},{s.width},{s.height}", FRAME_DEFAULTS_KEY
        )

    class SentrialController(NSObject):
        def init(self):
            self = objc.super(SentrialController, self).init()
            if self is None:
                return None
            self._detached = False
            self._r_opt_down = False
            self._voice = None
            self._voice_monitor = None
            self._build()
            self._install_voice_hotkey()
            return self

        # ------------ construction ------------

        def _build(self):
            self._status_bar = NSStatusBar.systemStatusBar()
            self._status_item = self._status_bar.statusItemWithLength_(-1.0)
            btn = self._status_item.button()

            img = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
            if img is not None:
                img.setSize_((18, 18))
                img.setTemplate_(True)
                btn.setImage_(img)
            else:
                btn.setTitle_("S")
                log.warning("menubar icon not found at %s — fallback to text", ICON_PATH)

            # Fire action on left AND right mouse up so we can branch by button/modifier
            btn.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
            btn.setTarget_(self)
            btn.setAction_("onClick:")

            # Build the single WebView + container that we reparent between popover and panel
            self._container = NSView.alloc().initWithFrame_(
                NSMakeRect(0, 0, POPOVER_W, POPOVER_H)
            )
            config = WKWebViewConfiguration.alloc().init()
            # Register a script message handler so the PWA can postMessage to native
            # (used to hand Sentrial's reply text back for TTS playback in Voice Mode).
            config.userContentController().addScriptMessageHandler_name_(self, "sentrial")

            # Note: we don't proactively wipe WKWebsiteDataStore caches here. An earlier
            # version of this code called removeDataOfTypes_:modifiedSince_:completionHandler_
            # with None as the completion handler — PyObjC can't marshal that into a valid
            # objc block and WebKit segfaults when it tries to invoke it (0x10 deref).
            # Cache invalidation is handled instead by (a) service worker v3 network-first
            # for HTML and (b) NSURLRequestReloadIgnoringLocalCacheData on the initial load.

            self._webview = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, POPOVER_W, POPOVER_H), config
            )
            self._webview.setAutoresizingMask_(
                NSViewWidthSizable | NSViewHeightSizable
            )
            self._container.addSubview_(self._webview)
            # Force a fresh fetch on first load (belt-and-suspenders with cache clear above)
            req = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(
                NSURL.URLWithString_(PWA_URL),
                NSURLRequestReloadIgnoringLocalCacheData,
                30.0,
            )
            self._webview.loadRequest_(req)
            log.info("menubar loaded PWA → %s", PWA_URL)

            # Popover wraps the container via a view controller
            self._popover_vc = NSViewController.alloc().init()
            self._popover_vc.setView_(self._container)
            self._popover = NSPopover.alloc().init()
            self._popover.setBehavior_(NSPopoverBehaviorTransient)
            self._popover.setContentSize_((POPOVER_W, POPOVER_H))
            self._popover.setContentViewController_(self._popover_vc)

            # Panel is created lazily on first detach
            self._panel = None

        def _make_panel(self):
            style = (
                NSWindowStyleMaskTitled
                | NSWindowStyleMaskClosable
                | NSWindowStyleMaskResizable
                | NSWindowStyleMaskUtilityWindow
                | NSWindowStyleMaskHUDWindow
            )
            panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                _saved_frame(), style, NSBackingStoreBuffered, False
            )
            panel.setTitle_("Sentrial")
            panel.setFloatingPanel_(True)
            panel.setLevel_(NSFloatingWindowLevel)
            panel.setHidesOnDeactivate_(False)
            panel.setReleasedWhenClosed_(False)
            panel.setMovableByWindowBackground_(True)
            panel.setMinSize_((PANEL_MIN_W, PANEL_MIN_H))
            panel.setDelegate_(self)
            self._panel = panel

        # ------------ click router ------------

        def onClick_(self, sender):
            app = NSApplication.sharedApplication()
            evt = app.currentEvent()
            is_right = evt is not None and evt.type() == NSEventTypeRightMouseUp
            is_option = evt is not None and bool(
                int(evt.modifierFlags()) & int(NSEventModifierFlagOption)
            )

            if is_right or is_option:
                self._toggle_detach()
            else:
                self._toggle_visible()

        # ------------ visibility ------------

        def _toggle_visible(self):
            if self._detached:
                if self._panel.isVisible():
                    self._panel.orderOut_(None)
                else:
                    self._panel.makeKeyAndOrderFront_(None)
            else:
                if self._popover.isShown():
                    self._popover.performClose_(None)
                else:
                    btn = self._status_item.button()
                    self._popover.showRelativeToRect_ofView_preferredEdge_(
                        btn.bounds(), btn, NSRectEdgeMinY
                    )
                    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

        # ------------ detach / reattach ------------

        def _toggle_detach(self):
            if self._detached:
                self._reattach()
            else:
                self._detach()

        def _detach(self):
            if self._popover.isShown():
                self._popover.performClose_(None)

            if self._panel is None:
                self._make_panel()

            # Reparent the container into the panel's content view
            content = self._panel.contentView()
            for sub in list(content.subviews()):
                sub.removeFromSuperview()
            self._container.setFrame_(content.bounds())
            self._container.setAutoresizingMask_(
                NSViewWidthSizable | NSViewHeightSizable
            )
            content.addSubview_(self._container)

            self._panel.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self._detached = True
            log.info("menubar: detached to floating panel")

        def _reattach(self):
            # Save frame, close panel, put container back in popover VC
            if self._panel is not None and self._panel.isVisible():
                _save_frame(self._panel.frame())
                self._panel.orderOut_(None)
            # Remove container from panel content, hand back to popover VC
            self._container.removeFromSuperview()
            self._container.setFrame_(NSMakeRect(0, 0, POPOVER_W, POPOVER_H))
            self._popover_vc.setView_(self._container)
            self._detached = False
            log.info("menubar: reattached to popover")

        # ------------ voice hotkey ------------

        def _install_voice_hotkey(self):
            """
            Global monitor on flagsChanged. Tap Right-Option toggles Voice Mode on/off.
            On: open popover + start continuous VoiceSession.
            Off: close session; TTS playback also stopped.

            Requires Input Monitoring permission (user grants on first run via TCC prompt).
            """
            def handler(event):
                try:
                    flags = int(event.modifierFlags())
                    is_down = bool(flags & RIGHT_OPTION_MASK)
                    if is_down and not self._r_opt_down:
                        self._r_opt_down = True
                        self._toggle_voice_mode()
                    elif not is_down and self._r_opt_down:
                        self._r_opt_down = False
                        # release is a no-op — we toggle only on key press
                except Exception as e:  # noqa: BLE001
                    log.warning("voice hotkey handler error: %s", e)

            self._voice_monitor = (
                NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                    NSEventMaskFlagsChanged, handler
                )
            )
            if self._voice_monitor is None:
                log.warning(
                    "voice hotkey monitor not installed — grant Input Monitoring "
                    "in System Settings → Privacy & Security"
                )

        def _toggle_voice_mode(self):
            if self._voice is None:
                self._voice_start()
            else:
                self._voice_stop()

        def _nova3_key(self) -> str | None:
            # Primary name is nova3_api_key / NOVA3_API_KEY. Keep legacy deepgram names as fallback.
            return (
                kc.get("nova3_api_key")
                or kc.get("deepgram_api_key")
                or os.environ.get("NOVA3_API_KEY")
                or os.environ.get("DEEPGRAM_API_KEY")
            )

        def _voice_start(self):
            api_key = self._nova3_key()
            if not api_key:
                log.warning("no NOVA3_API_KEY — voice disabled")
                self._inject_js(
                    'window.sentrialVoiceError && window.sentrialVoiceError('
                    '"Set NOVA3_API_KEY — see sentrial setup")'
                )
                return

            # Bring the popover or panel up so the user sees the live transcript
            if self._detached:
                if self._panel is not None and not self._panel.isVisible():
                    self._panel.makeKeyAndOrderFront_(None)
            else:
                if not self._popover.isShown():
                    btn = self._status_item.button()
                    self._popover.showRelativeToRect_ofView_preferredEdge_(
                        btn.bounds(), btn, NSRectEdgeMinY
                    )
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

            self._inject_js("window.sentrialVoiceStart && window.sentrialVoiceStart()")

            def on_interim(text):
                self._inject_js(
                    f"window.sentrialVoiceUpdate && "
                    f"window.sentrialVoiceUpdate({json.dumps(text)})"
                )

            def on_final(text):
                # Per-utterance — submit immediately through the PWA.
                self._inject_js(
                    f"window.sentrialVoiceTurn && "
                    f"window.sentrialVoiceTurn({json.dumps(text)})"
                )

            def on_error(err):
                self._inject_js(
                    f"window.sentrialVoiceError && "
                    f"window.sentrialVoiceError({json.dumps(err)})"
                )

            try:
                self._voice = VoiceSession(
                    api_key=api_key,
                    on_interim=on_interim,
                    on_final=on_final,
                    on_error=on_error,
                )
                self._voice.start()
                log.info("voice session started (continuous mode)")
            except Exception as e:  # noqa: BLE001
                log.warning("voice start failed: %s", e)
                self._inject_js(
                    f"window.sentrialVoiceError && "
                    f"window.sentrialVoiceError({json.dumps(str(e))})"
                )
                self._voice = None

        def _voice_stop(self):
            if self._voice is None:
                return
            try:
                self._voice.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("voice stop error: %s", e)
            self._voice = None
            # Stop any TTS still playing
            try:
                tts_mod.stop_playback()
            except Exception:  # noqa: BLE001
                pass
            log.info("voice session ended")
            self._inject_js("window.sentrialVoiceExit && window.sentrialVoiceExit()")

        def _inject_js(self, script: str) -> None:
            try:
                self._webview.evaluateJavaScript_completionHandler_(script, None)
            except Exception as e:  # noqa: BLE001
                log.debug("JS inject failed: %s", e)

        # ---- WKScriptMessageHandler: PWA → native ----
        def userContentController_didReceiveScriptMessage_(self, _controller, message):
            """
            Messages from the PWA, addressed to `window.webkit.messageHandlers.sentrial`.
            Expected payloads:
              { type: "voice_reply", text: "Sentrial's reply text" }
              { type: "voice_exit" }
            """
            try:
                body = message.body()
                if isinstance(body, dict):
                    kind = str(body.get("type", ""))
                    text = str(body.get("text", ""))
                else:
                    return
            except Exception as e:  # noqa: BLE001
                log.debug("bad script message: %s", e)
                return

            if kind == "voice_reply" and text:
                api_key = self._nova3_key()
                voice = kc.get("sentrial_voice") or "aura-2-orion-en"
                import threading as _t
                _t.Thread(
                    target=self._speak_blocking,
                    args=(text, api_key, voice),
                    daemon=True,
                ).start()
            elif kind == "voice_exit":
                tts_mod.stop_playback()
                # Also make sure the voice session is torn down
                if self._voice is not None:
                    try:
                        self._voice.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    self._voice = None
            elif kind == "voice_request_start":
                # Mic button inside the popover — same effect as tapping Right-Option
                if self._voice is None:
                    self._voice_start()

        def _speak_blocking(self, text: str, api_key: str | None, voice: str) -> None:
            """Runs off-main-thread. Announces state transitions to the PWA orb."""
            self._inject_js("window.sentrialOrbState && window.sentrialOrbState('speaking')")
            try:
                tts_mod.speak(text, api_key=api_key, voice=voice)
            except Exception as e:  # noqa: BLE001
                log.warning("tts failed: %s", e)
            finally:
                self._inject_js("window.sentrialOrbState && window.sentrialOrbState('idle')")

        # ------------ NSWindowDelegate ------------

        def windowWillClose_(self, note):
            # User hit the panel's close button → reattach so next menubar click works.
            if self._panel is not None and note.object() == self._panel:
                _save_frame(self._panel.frame())
                self._container.removeFromSuperview()
                self._container.setFrame_(NSMakeRect(0, 0, POPOVER_W, POPOVER_H))
                self._popover_vc.setView_(self._container)
                self._detached = False
                log.info("menubar: panel closed, reattached")

        def windowDidResize_(self, note):
            if self._panel is not None and note.object() == self._panel:
                _save_frame(self._panel.frame())

        def windowDidMove_(self, note):
            if self._panel is not None and note.object() == self._panel:
                _save_frame(self._panel.frame())

    # Start the app
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    global _controller_ref
    _controller_ref = SentrialController.alloc().init()

    app.run()


if __name__ == "__main__":
    run()
