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
    from Foundation import NSMakeRect, NSObject, NSURL, NSURLRequest, NSUserDefaults
    from WebKit import WKWebView, WKWebViewConfiguration

    NSApplicationActivationPolicyAccessory = 1
    NSPopoverBehaviorTransient = 1
    NSRectEdgeMinY = 1
    NSViewWidthSizable = 2
    NSViewHeightSizable = 16
    NSEventTypeLeftMouseUp = 2
    NSEventTypeRightMouseUp = 4

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
            self._build()
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
            self._webview = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, POPOVER_W, POPOVER_H), config
            )
            self._webview.setAutoresizingMask_(
                NSViewWidthSizable | NSViewHeightSizable
            )
            self._container.addSubview_(self._webview)
            self._webview.loadRequest_(
                NSURLRequest.requestWithURL_(NSURL.URLWithString_(PWA_URL))
            )
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
