"""Global hotkey via CGEvent tap. Detects Right Option hold-to-talk + Escape cancel."""

import logging
import time
from typing import Callable, Optional
import Quartz

from src import config

log = logging.getLogger("dictation")

# Keycodes
RIGHT_OPTION_KEYCODE = 61
ESCAPE_KEYCODE = 53

# State
_last_flags = 0
_press_time = 0.0
_is_pressed = False
_tap = None

# Tap health monitoring
_timeout_count = 0
_timeout_window_start = 0.0
_events_seen = 0  # a created tap can still be starved by a stale Input Monitoring grant

# Callbacks set by setup_hotkey()
_on_press: Optional[Callable] = None
_on_release: Optional[Callable] = None
_on_cancel: Optional[Callable] = None
_on_tap_lost: Optional[Callable] = None  # Called when tap appears broken


def _event_callback(proxy, event_type, event, refcon):
    """CGEvent tap callback. Fires on the main thread."""
    global _last_flags, _press_time, _is_pressed
    global _timeout_count, _timeout_window_start, _events_seen

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

    # Escape key cancels recording
    if event_type == Quartz.kCGEventKeyDown and _is_pressed:
        keycode = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode
        )
        if keycode == ESCAPE_KEYCODE:
            _is_pressed = False
            if _on_cancel:
                _on_cancel()
            return event

    if event_type != Quartz.kCGEventFlagsChanged:
        return event

    keycode = Quartz.CGEventGetIntegerValueField(
        event, Quartz.kCGKeyboardEventKeycode
    )
    flags = Quartz.CGEventGetFlags(event)

    if keycode == RIGHT_OPTION_KEYCODE:
        option_now = bool(flags & Quartz.kCGEventFlagMaskAlternate)
        option_was = bool(_last_flags & Quartz.kCGEventFlagMaskAlternate)

        if option_now and not option_was:
            # Right Option pressed
            _is_pressed = True
            _press_time = time.monotonic()
            if _on_press:
                _on_press()

        elif not option_now and option_was and _is_pressed:
            # Right Option released
            _is_pressed = False
            held_duration = time.monotonic() - _press_time
            if held_duration < config.HOLD_CANCEL_S:
                if _on_cancel:
                    _on_cancel()
            else:
                if _on_release:
                    _on_release()

    _last_flags = flags
    return event


def events_seen() -> int:
    """Key events the tap has delivered since launch (0 = likely stale TCC grant)."""
    return _events_seen


def setup_hotkey(
    on_press: Callable,
    on_release: Callable,
    on_cancel: Callable,
    on_tap_lost: Optional[Callable] = None,
) -> bool:
    """Set up the CGEvent tap for Right Option detection + Escape cancel.
    Must be called before the main run loop starts.
    Returns True if successful, False if Accessibility permission is missing.
    """
    global _on_press, _on_release, _on_cancel, _on_tap_lost, _tap

    _on_press = on_press
    _on_release = on_release
    _on_cancel = on_cancel
    _on_tap_lost = on_tap_lost

    # Listen for modifier flag changes + key down (for Escape cancel)
    event_mask = (
        Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
        | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
    )
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        event_mask,
        _event_callback,
        None,
    )

    if tap is None:
        return False

    _tap = tap

    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetMain(),
        source,
        Quartz.kCFRunLoopCommonModes,
    )
    Quartz.CGEventTapEnable(tap, True)

    return True
