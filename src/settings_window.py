"""Settings window: hold-to-talk key recorder."""

import objc
from AppKit import (
    NSWindow, NSTextField, NSButton, NSFont, NSColor, NSApplication,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable, NSBackingStoreBuffered,
    NSBezelStyleRounded, NSLineBreakByWordWrapping,
)
from Foundation import NSMakeRect, NSObject

from src import hotkey

WIDTH = 440
HEIGHT = 168
PAD = 20

DEFAULT_HINT = "Hold to dictate, release to paste. Escape cancels."
RECORDING_HINT = "Press the key you want to hold. Escape or click again to cancel."
FN_HINT = ("If holding Fn opens the emoji picker, set System Settings › Keyboard › "
           "“Press 🌐 key to” › Do Nothing.")
SPECIAL_HINT = "This key is blocked from the focused app so it only triggers dictation."
FOOTNOTE = ("F1–F12 control brightness and volume by default — hold fn while recording "
            "one, and while dictating.")


def _label(text, frame, size=13, color=None, bold=False, wrap=False):
    label = NSTextField.alloc().initWithFrame_(frame)
    label.setStringValue_(text)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    if color is not None:
        label.setTextColor_(color)
    if wrap:
        label.cell().setWraps_(True)
        label.cell().setLineBreakMode_(NSLineBreakByWordWrapping)
    return label


class _Target(NSObject):
    """ObjC target for buttons + window delegate; forwards to the controller."""

    def initWithController_(self, controller):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def recordClicked_(self, sender):
        self._controller._toggle_recording()

    def resetClicked_(self, sender):
        self._controller._apply(hotkey.DEFAULT_KEYCODE)

    def windowWillClose_(self, notification):
        self._controller._stop_recording()


class SettingsWindowController:
    _instance = None

    def __init__(self, on_hotkey_change):
        # on_hotkey_change(keycode) -> error message or None; persists on success.
        self._on_hotkey_change = on_hotkey_change
        self._recording = False
        self._target = _Target.alloc().initWithController_(self)
        self._build()

    @classmethod
    def show(cls, on_hotkey_change):
        if cls._instance is None:
            cls._instance = cls(on_hotkey_change)
        inst = cls._instance
        inst._refresh(hint=None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        inst._window.makeKeyAndOrderFront_(None)

    def _build(self):
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        window.setTitle_("Personal Dictation Settings")
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self._target)
        window.center()
        content = window.contentView()

        y = HEIGHT - PAD - 28
        content.addSubview_(_label("Hold-to-talk key", NSMakeRect(PAD, y + 4, 140, 20), bold=True))

        rec = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 140, y - 2, 170, 32))
        rec.setBezelStyle_(NSBezelStyleRounded)
        rec.setTarget_(self._target)
        rec.setAction_("recordClicked:")
        rec.setToolTip_("Click, then press the key you want to hold to dictate")
        content.addSubview_(rec)
        self._record_btn = rec

        reset = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 316, y - 2, 84, 32))
        reset.setBezelStyle_(NSBezelStyleRounded)
        reset.setTitle_("Reset")
        reset.setToolTip_("Reset to Right Option")
        reset.setTarget_(self._target)
        reset.setAction_("resetClicked:")
        content.addSubview_(reset)
        self._reset_btn = reset

        self._hint = _label("", NSMakeRect(PAD, y - 46, WIDTH - 2 * PAD, 36),
                            size=12, color=NSColor.secondaryLabelColor(), wrap=True)
        content.addSubview_(self._hint)

        content.addSubview_(_label(FOOTNOTE, NSMakeRect(PAD, 14, WIDTH - 2 * PAD, 32),
                                   size=11, color=NSColor.tertiaryLabelColor(), wrap=True))
        self._window = window

    # --- state ---

    def _refresh(self, hint, error=False):
        code = hotkey.current_keycode()
        if self._recording:
            self._record_btn.setTitle_("Press a key…")
            self._record_btn.setBezelColor_(NSColor.systemPurpleColor())
        else:
            self._record_btn.setTitle_(hotkey.key_label(code))
            self._record_btn.setBezelColor_(None)
        self._reset_btn.setEnabled_(code != hotkey.DEFAULT_KEYCODE and not self._recording)
        if hint is None:
            hint = RECORDING_HINT if self._recording else self._hint_for(code)
        self._hint.setStringValue_(hint)
        self._hint.setTextColor_(NSColor.systemRedColor() if error
                                 else NSColor.secondaryLabelColor())

    @staticmethod
    def _hint_for(code):
        if code == 63:
            return f"{DEFAULT_HINT} {FN_HINT}"
        if not hotkey.is_modifier(code):
            return f"{DEFAULT_HINT} {SPECIAL_HINT}"
        return DEFAULT_HINT

    def _toggle_recording(self):
        if self._recording:
            self._stop_recording()
            self._refresh(hint=None)
        else:
            self._recording = True
            hotkey.begin_capture(self._captured)
            self._refresh(hint=None)

    def _stop_recording(self):
        if self._recording:
            self._recording = False
            hotkey.end_capture()

    def _captured(self, keycode):
        """Tap callback (main thread) — keep it short."""
        self._stop_recording()
        if keycode == hotkey.ESCAPE_KEYCODE:
            self._refresh(hint=None)  # Escape cancels recording
            return
        from PyObjCTools import AppHelper
        # Defer: applying recreates the event tap, which must not happen inside
        # that same tap's callback.
        AppHelper.callAfter(self._apply, keycode)

    def _apply(self, keycode):
        err = self._on_hotkey_change(keycode)
        if err:
            self._refresh(hint=err, error=True)
        else:
            self._refresh(hint=None)
