# History Deletion + Lifetime Counter

## Goal

Add the ability to delete transcriptions from the history window (individual + bulk), and keep a persistent word count that survives deletions.

## What We Did

1. **Fixed history window not opening** — `_reload_and_show()` called `_apply_search()` (which calls `_populate()`) before `_build_window()`. On first click, `_window` was None → silent crash. Moved window creation before the search call.

2. **Per-entry delete with undo** — Each row in the history window gets a Delete button. Clicking it changes to "Undo" for 3 seconds. If the timer fires, the entry is permanently removed from `history.txt` and the list refreshes. Clicking Undo cancels.

3. **Delete All with confirmation** — "Delete All" button in the history window toolbar. Shows native NSAlert with "Delete All" / "Cancel". Truncates `history.txt` on confirm.

4. **Lifetime word counter** — Single integer stored in `~/.config/dictation/lifetime_words`. Incremented on each dictation, never decremented. Menubar shows lifetime total. Seeds from existing history on first run so existing users don't lose their count. Privacy-friendly — no text or metadata, just a number.

5. **Audio stream reuse** (prior uncommitted work) — `stop_recording()` now stops the stream without closing it. Eliminates ~50-200ms stream recreation cost. Added `shutdown()` for clean app quit.

## Outcome

All four features committed. No remote to push to. App needs restart to pick up changes.

## What We Learned

- rumps silently swallows exceptions in menu callbacks — no error in the log when the history window crash happened. Makes debugging harder; consider wrapping callbacks with explicit try/except logging.
