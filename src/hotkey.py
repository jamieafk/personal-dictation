"""Global hotkey via CGEvent tap. Hold-to-talk on one or two configurable keys
(e.g. Page Down on a desk keyboard + Right Command on the laptop) + Escape cancel.

Modifier hotkeys (Option, Command, ...) arrive as kCGEventFlagsChanged and need
only a listen-only tap (Input Monitoring). Other allowed keys (F-keys, arrows,
Home/End...) arrive as keyDown/keyUp and are swallowed so they don't also act in
the focused app, which needs an active tap (Accessibility).
"""

import logging
import time
from typing import Callable, Optional
import Quartz

from src import config

log = logging.getLogger("dictation")

ESCAPE_KEYCODE = 53
CAPS_LOCK_KEYCODE = 57
DEFAULT_KEYCODE = 61  # Right Option

# Holdable modifiers: keycode -> (generic flag mask, label). Left/right share a
# mask, so press/release is tracked per keycode, not from the mask alone.
MODIFIER_KEYS = {
    61: (Quartz.kCGEventFlagMaskAlternate, "Right Option"),
    58: (Quartz.kCGEventFlagMaskAlternate, "Left Option"),
    54: (Quartz.kCGEventFlagMaskCommand, "Right Command"),
    55: (Quartz.kCGEventFlagMaskCommand, "Left Command"),
    62: (Quartz.kCGEventFlagMaskControl, "Right Control"),
    59: (Quartz.kCGEventFlagMaskControl, "Left Control"),
    60: (Quartz.kCGEventFlagMaskShift, "Right Shift"),
    56: (Quartz.kCGEventFlagMaskShift, "Left Shift"),
    63: (Quartz.kCGEventFlagMaskSecondaryFn, "Fn"),
}

# Non-modifier keys that don't type text, so swallowing them is safe.
SPECIAL_KEYS = {
    122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6", 98: "F7",
    100: "F8", 101: "F9", 109: "F10", 103: "F11", 111: "F12", 105: "F13",
    107: "F14", 113: "F15", 106: "F16", 64: "F17", 79: "F18", 80: "F19", 90: "F20",
    115: "Home", 119: "End", 116: "Page Up", 121: "Page Down", 114: "Help",
    117: "Forward Delete", 123: "Left Arrow", 124: "Right Arrow",
    125: "Down Arrow", 126: "Up Arrow",
}


def key_label(keycode: int) -> str:
    if keycode in MODIFIER_KEYS:
        return MODIFIER_KEYS[keycode][1]
    return SPECIAL_KEYS.get(keycode, f"Key {keycode}")


def validate_key(keycode: int) -> Optional[str]:
    """Error message if keycode can't be the hold-to-talk key, else None."""
    if keycode == ESCAPE_KEYCODE:
        return "Escape is reserved for cancelling a dictation."
    if keycode == CAPS_LOCK_KEYCODE:
        return "Caps Lock toggles instead of holding — pick another key."
    if keycode in MODIFIER_KEYS or keycode in SPECIAL_KEYS:
        return None
    return "That key types text. Pick a modifier (Option, Command, Fn…) or a function key."


def is_modifier(keycode: int) -> bool:
    return keycode in MODIFIER_KEYS


# State
_keycodes = (DEFAULT_KEYCODE,)
_held: Optional[int] = None  # hotkey that started the current dictation; the other is ignored
_press_time = 0.0
_tap = None
_source = None

# Key recorder (Settings): while set, keys are reported here instead of dictating.
_capture_cb: Optional[Callable[[int], None]] = None
_capture_mod = None  # modifier held during capture; captured on its release if alone

# Tap health monitoring
_timeout_count = 0
_timeout_window_start = 0.0
_events_seen = 0  # a created tap can still be starved by a stale Input Monitoring grant

# Callbacks set by setup_hotkey()
_on_press: Optional[Callable] = None
_on_release: Optional[Callable] = None
_on_cancel: Optional[Callable] = None
_on_tap_lost: Optional[Callable] = None  # Called when tap appears broken


def _keycode_of(event) -> int:
    return Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)


def _modifier_down(keycode: int, flags: int) -> bool:
    return bool(flags & MODIFIER_KEYS[keycode][0])


def _capture_event(event_type, event):
    """Key recorder: a plain key is captured on keyDown; a modifier is captured on
    its release only if no other key was pressed meanwhile (so fn+F5 records F5)."""
    global _capture_mod
    keycode = _keycode_of(event)
    if event_type == Quartz.kCGEventKeyDown:
        if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
            return
        _capture_mod = None
        _capture_cb(keycode)
    elif event_type == Quartz.kCGEventFlagsChanged and keycode in MODIFIER_KEYS:
        if _modifier_down(keycode, Quartz.CGEventGetFlags(event)):
            _capture_mod = keycode
        elif _capture_mod == keycode:
            _capture_mod = None
            _capture_cb(keycode)


def _press(keycode: int):
    global _held, _press_time
    _held = keycode
    _press_time = time.monotonic()
    if _on_press:
        _on_press()


def _release():
    global _held
    _held = None
    if time.monotonic() - _press_time < config.HOLD_CANCEL_S:
        if _on_cancel:
            _on_cancel()
    elif _on_release:
        _on_release()


def _event_callback(proxy, event_type, event, refcon):
    """CGEvent tap callback. Fires on the main thread."""
    global _held, _timeout_count, _timeout_window_start, _events_seen

    # Re-enable tap if macOS disabled it due to timeout
    if event_type == Quartz.kCGEventTapDisabledByTimeout:
        log.warning("CGEvent tap disabled by timeout — re-enabling")
        if _tap is not None:
            Quartz.CGEventTapEnable(_tap, True)

        # Track timeout frequency — 3+ in 60s means something is wrong
        now = time.monotonic()
        if now - _timeout_window_start > 60:
            _timeout_count = 0
            _timeout_window_start = now
        _timeout_count += 1
        if _timeout_count >= 3 and _on_tap_lost:
            _on_tap_lost()

        return event

    _events_seen += 1

    if _capture_cb is not None:
        _capture_event(event_type, event)
        return event

    keycode = _keycode_of(event)

    # Escape key cancels recording
    if event_type == Quartz.kCGEventKeyDown and _held is not None and keycode == ESCAPE_KEYCODE:
        _held = None
        if _on_cancel:
            _on_cancel()
        return event

    if keycode not in _keycodes:
        return event

    if is_modifier(keycode):
        if event_type == Quartz.kCGEventFlagsChanged:
            # Each flagsChanged for a hotkey keycode is a state change of that key.
            # The mask tells "down" only when nothing is held (the twin left/right
            # modifier can keep the shared mask set on release).
            if _held is None and _modifier_down(keycode, Quartz.CGEventGetFlags(event)):
                _press(keycode)
            elif _held == keycode:
                _release()
        return event

    # Non-modifier hotkey: swallow its events (active tap) so it doesn't also act
    # in the focused app.
    if event_type == Quartz.kCGEventKeyDown:
        if _held is None and not Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventAutorepeat):
            _press(keycode)
        return None
    if event_type == Quartz.kCGEventKeyUp:
        if _held == keycode:
            _release()
        return None
    return event


def events_seen() -> int:
    """Key events the tap has delivered since launch (0 = likely stale TCC grant)."""
    return _events_seen


def current_keycodes() -> tuple:
    return _keycodes


def keys_label(keycodes) -> str:
    return " + ".join(key_label(k) for k in keycodes)


def _cancel_held():
    global _held
    if _held is not None:
        _held = None
        if _on_cancel:
            _on_cancel()


def begin_capture(callback: Callable[[int], None]):
    """Route the next key to callback(keycode) instead of dictating (Settings
    recorder). Cancels a dictation in progress."""
    global _capture_cb, _capture_mod
    _cancel_held()
    _capture_mod = None
    _capture_cb = callback


def end_capture():
    global _capture_cb, _capture_mod
    _capture_cb = None
    _capture_mod = None


def _create_tap(keycodes):
    # One non-modifier key is enough to need the active tap.
    listen_only = all(is_modifier(k) for k in keycodes)
    event_mask = (
        Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
        | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
    )
    return Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly if listen_only else Quartz.kCGEventTapOptionDefault,
        event_mask,
        _event_callback,
        None,
    )


def _install_tap(tap):
    global _tap, _source
    _tap = tap
    _source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetMain(), _source, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)


def _remove_tap():
    global _tap, _source
    if _tap is not None:
        Quartz.CGEventTapEnable(_tap, False)
        if _source is not None:
            Quartz.CFRunLoopRemoveSource(Quartz.CFRunLoopGetMain(), _source,
                                         Quartz.kCFRunLoopCommonModes)
        Quartz.CFMachPortInvalidate(_tap)
    _tap = None
    _source = None


def setup_hotkey(
    on_press: Callable,
    on_release: Callable,
    on_cancel: Callable,
    on_tap_lost: Optional[Callable] = None,
    keycodes: tuple = (DEFAULT_KEYCODE,),
) -> bool:
    """Set up the CGEvent tap for the hotkeys + Escape cancel. Call on the main thread.
    Returns True if successful, False if the permission is missing."""
    global _on_press, _on_release, _on_cancel, _on_tap_lost, _keycodes

    _on_press = on_press
    _on_release = on_release
    _on_cancel = on_cancel
    _on_tap_lost = on_tap_lost

    tap = _create_tap(keycodes)
    if tap is None:
        return False
    _remove_tap()
    _keycodes = tuple(keycodes)
    _install_tap(tap)
    return True


def set_hotkeys(keycodes: tuple) -> Optional[str]:
    """Switch the hotkeys live. Main thread. Returns an error message, or None on
    success. The old tap stays in place if the new one can't be created."""
    global _keycodes
    for keycode in keycodes:
        err = validate_key(keycode)
        if err:
            return err
    if len(set(keycodes)) != len(keycodes):
        return "That key is already set — pick a different one."
    tap = _create_tap(keycodes)
    if tap is None:
        return ("Couldn't watch that key — function keys need Accessibility permission "
                "for Personal Dictation.")
    _cancel_held()
    _remove_tap()
    _keycodes = tuple(keycodes)
    _install_tap(tap)
    log.info("Hotkey set to %s (keycodes %s)", keys_label(keycodes), list(keycodes))
    return None
