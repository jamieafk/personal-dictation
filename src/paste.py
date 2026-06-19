"""Clipboard save/restore and text insertion via CGEvent paste."""

import threading
import time
import Quartz
from AppKit import NSPasteboard, NSPasteboardItem, NSData

from src import config

# Serializes the save->set->paste->restore critical section. The clipboard is a
# single process-global resource; concurrent inserts (e.g. a re-paste click while
# another paste is mid-flight) would otherwise clobber each other's saved value.
_paste_lock = threading.Lock()

# Modern constant; fall back to UTI string if not available
try:
    from AppKit import NSPasteboardTypeString
except ImportError:
    NSPasteboardTypeString = "public.utf8-plain-text"

# Virtual keycode for "V"
_V_KEYCODE = 0x09


def save_clipboard():
    """Save all pasteboard items with all their types. Returns a list of items,
    each containing a list of (type_string, NSData) tuples."""
    pb = NSPasteboard.generalPasteboard()
    saved = []
    items = pb.pasteboardItems()
    if items is None:
        return saved
    for item in items:
        item_data = []
        for t in item.types():
            data = item.dataForType_(t)
            if data is not None:
                item_data.append((t, data))
        if item_data:
            saved.append(item_data)
    return saved


def restore_clipboard(saved):
    """Restore previously saved clipboard contents."""
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    if not saved:
        return
    items = []
    for item_data in saved:
        item = NSPasteboardItem.alloc().init()
        for type_str, data in item_data:
            item.setData_forType_(data, type_str)
        items.append(item)
    pb.writeObjects_(items)


def set_clipboard_text(text):
    """Place text on the clipboard."""
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def paste():
    """Simulate Cmd+V via CGEvent keystroke. Much faster than AppleScript subprocess."""
    # Key down with Command flag
    event_down = Quartz.CGEventCreateKeyboardEvent(None, _V_KEYCODE, True)
    Quartz.CGEventSetFlags(event_down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGAnnotatedSessionEventTap, event_down)

    # Key up
    event_up = Quartz.CGEventCreateKeyboardEvent(None, _V_KEYCODE, False)
    Quartz.CGEventSetFlags(event_up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGAnnotatedSessionEventTap, event_up)


def insert_text(text, saved_clipboard=None):
    """Full pipeline: paste transcript, restore clipboard.
    If saved_clipboard is provided, skip the save step (already done earlier).
    Clipboard is ALWAYS restored, even if paste fails.
    The whole critical section is serialized so concurrent inserts can't clobber
    each other's saved clipboard value."""
    with _paste_lock:
        if saved_clipboard is None:
            saved_clipboard = save_clipboard()
        try:
            set_clipboard_text(text)
            paste()
            time.sleep(config.PASTE_DELAY_S)  # conservative for Electron apps (Slack, VS Code, Discord)
        finally:
            restore_clipboard(saved_clipboard)
