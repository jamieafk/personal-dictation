"""Settings window: hold-to-talk key recorder (main key + optional second key)."""

import objc
from AppKit import (
    NSWindow, NSTextField, NSButton, NSFont, NSColor, NSApplication,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable, NSBackingStoreBuffered,
    NSBezelStyleRounded, NSLineBreakByWordWrapping,
)
from Foundation import NSMakeRect, NSObject

from src import hotkey

WIDTH = 440
HEIGHT = 220
PAD = 20
ROW_GAP = 40

DEFAULT_HINT = "Hold to dictate, release to paste. Escape cancels."
RECORDING_HINT = "Press the key you want to hold. Escape or click again to cancel."
FN_HINT = ("If holding Fn opens the emoji picker, set System Settings › Keyboard › "
           "“Press 🌐 key to” › Do Nothing.")
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


def _button(title, frame, target, action, tooltip):
    btn = NSButton.alloc().initWithFrame_(frame)
    btn.setBezelStyle_(NSBezelStyleRounded)
    btn.setTitle_(title)
    btn.setToolTip_(tooltip)
    btn.setTarget_(target)
    btn.setAction_(action)
    return btn


class _Target(NSObject):
    """ObjC target for buttons + window delegate; forwards to the controller."""

    def initWithController_(self, controller):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def recordClicked_(self, sender):
        self._controller._toggle_recording(0)

    def recordSecondClicked_(self, sender):
        self._controller._toggle_recording(1)

    def resetClicked_(self, sender):
        self._controller._apply(0, hotkey.DEFAULT_KEYCODE)

    def clearClicked_(self, sender):
        self._controller._apply(1, None)

    def windowWillClose_(self, notification):
        self._controller._stop_recording()


class SettingsWindowController:
    _instance = None

    def __init__(self, on_hotkey_change):
        # on_hotkey_change(slot, keycode) -> error message or None; persists on
        # success. slot 0 = main key, 1 = second key (keycode None clears it).
        self._on_hotkey_change = on_hotkey_change
        self._recording_slot = None
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
        t = self._target

        y = HEIGHT - PAD - 28
        content.addSubview_(_label("Hold-to-talk key", NSMakeRect(PAD, y + 4, 140, 20), bold=True))
        self._record_btns = [
            _button("", NSMakeRect(PAD + 140, y - 2, 170, 32), t, "recordClicked:",
                    "Click, then press the key you want to hold to dictate"),
        ]
        self._reset_btn = _button("Reset", NSMakeRect(PAD + 316, y - 2, 84, 32), t,
                                  "resetClicked:", "Reset to Right Option")

        y -= ROW_GAP
        content.addSubview_(_label("Second key", NSMakeRect(PAD, y + 4, 140, 20), bold=True))
        self._record_btns.append(
            _button("", NSMakeRect(PAD + 140, y - 2, 170, 32), t, "recordSecondClicked:",
                    "Optional — a second key that also dictates, e.g. one your laptop "
                    "keyboard has"))
        self._clear_btn = _button("Clear", NSMakeRect(PAD + 316, y - 2, 84, 32), t,
                                  "clearClicked:", "Remove the second key")
        for view in (*self._record_btns, self._reset_btn, self._clear_btn):
            content.addSubview_(view)

        self._hint = _label("", NSMakeRect(PAD, y - 58, WIDTH - 2 * PAD, 48),
                            size=12, color=NSColor.secondaryLabelColor(), wrap=True)
        content.addSubview_(self._hint)

        content.addSubview_(_label(FOOTNOTE, NSMakeRect(PAD, 14, WIDTH - 2 * PAD, 32),
                                   size=11, color=NSColor.tertiaryLabelColor(), wrap=True))
        self._window = window

    # --- state ---

    def _refresh(self, hint, error=False):
        codes = hotkey.current_keycodes()
        slots = (codes[0], codes[1] if len(codes) > 1 else None)
        recording = self._recording_slot is not None
        for slot, (btn, code) in enumerate(zip(self._record_btns, slots)):
            if self._recording_slot == slot:
                btn.setTitle_("Press a key…")
                btn.setBezelColor_(NSColor.systemPurpleColor())
            else:
                btn.setTitle_(hotkey.key_label(code) if code is not None else "None")
                btn.setBezelColor_(None)
        self._reset_btn.setEnabled_(slots[0] != hotkey.DEFAULT_KEYCODE and not recording)
        self._clear_btn.setEnabled_(slots[1] is not None and not recording)
        if hint is None:
            hint = RECORDING_HINT if recording else self._hint_for(codes)
        self._hint.setStringValue_(hint)
        self._hint.setTextColor_(NSColor.systemRedColor() if error
                                 else NSColor.secondaryLabelColor())

    @staticmethod
    def _hint_for(codes):
        parts = [DEFAULT_HINT]
        blocked = [hotkey.key_label(c) for c in codes if not hotkey.is_modifier(c)]
        if len(blocked) == 1:
            parts.append(f"{blocked[0]} is blocked from the focused app so it only "
                         "triggers dictation.")
        elif blocked:
            parts.append(f"{' and '.join(blocked)} are blocked from the focused app so "
                         "they only trigger dictation.")
        if 63 in codes:
            parts.append(FN_HINT)
        return " ".join(parts)

    def _toggle_recording(self, slot):
        was = self._recording_slot
        self._stop_recording()
        if was != slot:
            self._recording_slot = slot
            hotkey.begin_capture(self._captured)
        self._refresh(hint=None)

    def _stop_recording(self):
        if self._recording_slot is not None:
            self._recording_slot = None
            hotkey.end_capture()

    def _captured(self, keycode):
        """Tap callback (main thread) — keep it short."""
        slot = self._recording_slot
        self._stop_recording()
        if keycode == hotkey.ESCAPE_KEYCODE:
            self._refresh(hint=None)  # Escape cancels recording
            return
        from PyObjCTools import AppHelper
        # Defer: applying recreates the event tap, which must not happen inside
        # that same tap's callback.
        AppHelper.callAfter(self._apply, slot, keycode)

    def _apply(self, slot, keycode):
        err = self._on_hotkey_change(slot, keycode)
        if err:
            self._refresh(hint=err, error=True)
        else:
            self._refresh(hint=None)
