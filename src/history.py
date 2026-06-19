"""Transcription history. Persists to ~/.config/dictation/history.txt."""

import os
import datetime
from dataclasses import dataclass
from typing import List

from src import config

HISTORY_DIR = config.CONFIG_DIR
HISTORY_PATH = os.path.join(HISTORY_DIR, "history.txt")
LIFETIME_PATH = os.path.join(HISTORY_DIR, "lifetime_words")


@dataclass
class HistoryEntry:
    timestamp: datetime.datetime
    app_name: str
    text: str

    @property
    def word_count(self) -> int:
        return len(self.text.split())


_cached_word_count = None  # Lazy-loaded, then incremented on each append
_lifetime_count = None     # Lifetime total — survives deletions


def append(app_name: str, text: str) -> HistoryEntry:
    """Append a transcription to history. Creates dir/file if needed."""
    global _cached_word_count
    os.makedirs(HISTORY_DIR, exist_ok=True)
    now = datetime.datetime.now()
    # Escape pipes in text (unlikely in speech, but defensive)
    safe_text = text.replace("|", "\\|")
    line = f"{now.strftime('%Y-%m-%d %H:%M:%S')} | {app_name} | {safe_text}\n"
    with open(HISTORY_PATH, "a") as f:
        f.write(line)
    entry = HistoryEntry(timestamp=now, app_name=app_name, text=text)
    # Increment cached count instead of re-reading file
    if _cached_word_count is not None:
        _cached_word_count += entry.word_count
    _increment_lifetime_words(entry.word_count)
    return entry


def load_all() -> List[HistoryEntry]:
    """Load all entries from disk. Most recent last."""
    if not os.path.exists(HISTORY_PATH):
        return []
    entries = []
    with open(HISTORY_PATH, "r") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            # Parse: "2026-03-21 14:32:05 | Safari | transcribed text here"
            parts = line.split(" | ", 2)
            if len(parts) != 3:
                continue  # Skip malformed lines
            try:
                ts = datetime.datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            text = parts[2].replace("\\|", "|")
            entries.append(HistoryEntry(timestamp=ts, app_name=parts[1], text=text))
    return entries


def total_word_count() -> int:
    """Sum of word counts across all entries. Cached after first load."""
    global _cached_word_count
    if _cached_word_count is None:
        _cached_word_count = sum(e.word_count for e in load_all())
    return _cached_word_count


def lifetime_word_count() -> int:
    """Lifetime words transcribed. Survives deletions. Just a number, no text stored."""
    global _lifetime_count
    if _lifetime_count is not None:
        return _lifetime_count
    if not os.path.exists(LIFETIME_PATH):
        # Seed from current history for existing users
        _lifetime_count = total_word_count()
        if _lifetime_count > 0:
            with open(LIFETIME_PATH, "w") as f:
                f.write(str(_lifetime_count))
        return _lifetime_count
    try:
        with open(LIFETIME_PATH, "r") as f:
            _lifetime_count = int(f.read().strip())
    except (ValueError, OSError):
        _lifetime_count = 0
    return _lifetime_count


def _increment_lifetime_words(count: int):
    """Add to the lifetime counter. Never decremented."""
    global _lifetime_count
    current = lifetime_word_count()
    _lifetime_count = current + count
    os.makedirs(HISTORY_DIR, exist_ok=True)
    with open(LIFETIME_PATH, "w") as f:
        f.write(str(_lifetime_count))


def delete_entry(entry: HistoryEntry):
    """Delete a specific entry from history."""
    global _cached_word_count
    entries = load_all()
    entries = [e for e in entries if not (
        e.timestamp == entry.timestamp
        and e.app_name == entry.app_name
        and e.text == entry.text
    )]
    _rewrite(entries)
    _cached_word_count = None


def delete_all():
    """Delete all history entries."""
    global _cached_word_count
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "w") as f:
            pass
    _cached_word_count = 0


def _rewrite(entries: List[HistoryEntry]):
    """Rewrite the history file with the given entries."""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w") as f:
        for e in entries:
            safe_text = e.text.replace("|", "\\|")
            f.write(f"{e.timestamp.strftime('%Y-%m-%d %H:%M:%S')} | {e.app_name} | {safe_text}\n")
