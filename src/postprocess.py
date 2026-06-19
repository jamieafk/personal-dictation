"""Post-processing for Whisper output: vocabulary correction, filler removal, stutter collapse."""

import logging
import os
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from src import config

log = logging.getLogger("dictation")

VOCAB_PATH = os.path.join(config.CONFIG_DIR, "vocab.txt")

# English filler words — compiled once at import time
_FILLER_WORDS = [
    "uh", "um", "uhm", "umm", "uhh", "uhhh",
    "ah", "hmm", "hm", "mmm", "mm", "mh", "eh", "ehh", "ha",
]
_FILLER_PATTERN = re.compile(
    "|".join(rf"(?i)\b{re.escape(w)}\b[,.]?\s*" for w in _FILLER_WORDS)
)


# --- Soundex (standard American Soundex, 20 lines) ---

_SOUNDEX_MAP = {
    c: d for d, chars in enumerate([
        "BFPV", "CGJKQSXZ", "DT", "L", "MN", "R",
    ], 1) for c in chars
}


def _soundex(s: str) -> str:
    """Compute 4-character Soundex code for a string."""
    s = re.sub(r"[^A-Za-z]", "", s).upper()
    if not s:
        return "0000"
    code = s[0]
    prev = _SOUNDEX_MAP.get(s[0], 0)
    for ch in s[1:]:
        digit = _SOUNDEX_MAP.get(ch, 0)
        if digit and digit != prev:
            code += str(digit)
            if len(code) == 4:
                break
        prev = digit
    return code.ljust(4, "0")


# --- Vocabulary loading ---

def load_vocab(path: str = VOCAB_PATH) -> List[str]:
    """Load vocabulary entries from file. Returns empty list if file missing."""
    if not os.path.isfile(path):
        return []
    entries = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append(line)
    return entries


def _build_vocab_index(entries: List[str]) -> Dict[str, str]:
    """Build lookup: normalized form (no spaces, lowercase) -> original entry."""
    return {re.sub(r"\s+", "", e).lower(): e for e in entries}


# --- Fuzzy vocabulary matching ---

def _strip_punctuation(word: str) -> Tuple[str, str, str]:
    """Split word into (leading_punct, core, trailing_punct)."""
    i = 0
    while i < len(word) and not word[i].isalnum():
        i += 1
    j = len(word)
    while j > i and not word[j - 1].isalnum():
        j -= 1
    return word[:i], word[i:j], word[j:]


def _preserve_case(original: str, replacement: str) -> str:
    """Apply the case pattern of original to replacement."""
    if original.isupper():
        return replacement.upper()
    if original.istitle():
        return replacement[0].upper() + replacement[1:] if replacement else replacement
    return replacement


def _score_match(candidate: str, target: str) -> float:
    """Score a candidate against a vocab target. Lower is better. Returns 1.0 for no match."""
    # Length filter — skip if too different
    max_len = max(len(candidate), len(target))
    if max_len == 0:
        return 1.0
    if abs(len(candidate) - len(target)) > max_len * 0.25:
        return 1.0
    if len(candidate) < 2:
        return 1.0

    # Levenshtein-like score via SequenceMatcher (1.0 = identical, 0.0 = nothing in common)
    ratio = SequenceMatcher(None, candidate, target).ratio()
    distance_score = 1.0 - ratio  # Convert to distance (0.0 = perfect)

    # Soundex phonetic boost
    if _soundex(candidate) == _soundex(target):
        distance_score *= 0.3  # 70% boost for phonetic match

    return distance_score


def apply_vocab(text: str, vocab: List[str], threshold: float = config.VOCAB_THRESHOLD) -> str:
    """Replace words in text with vocabulary matches using fuzzy matching."""
    if not vocab:
        return text

    index = _build_vocab_index(vocab)
    words = text.split()
    result = []
    i = 0

    while i < len(words):
        matched = False

        # Try n-grams: 3, 2, 1 (greedy longest-first)
        for n in (3, 2, 1):
            if i + n > len(words):
                continue

            ngram_words = words[i:i + n]

            # Strip punctuation from first/last words for matching
            first_pre, first_core, _ = _strip_punctuation(ngram_words[0])
            _, last_core, last_suf = _strip_punctuation(ngram_words[-1])

            # Build normalized candidate (no spaces, lowercase)
            if n == 1:
                candidate = first_core.lower()
            else:
                middles = [w for w in ngram_words[1:-1]] if n > 2 else []
                parts = [first_core] + middles + [last_core]
                candidate = "".join(parts).lower()

            if not candidate:
                continue

            # Find best match in vocab
            best_score = threshold
            best_entry = None
            for normalized, entry in index.items():
                score = _score_match(candidate, normalized)
                if score < best_score:
                    best_score = score
                    best_entry = entry

            if best_entry is not None:
                # Preserve case from original text
                original_text = " ".join(ngram_words)
                core_original = re.sub(r"[^\w\s]", "", original_text).strip()
                replacement = _preserve_case(core_original, best_entry)
                result.append(first_pre + replacement + last_suf)
                i += n
                matched = True
                break

        if not matched:
            result.append(words[i])
            i += 1

    return " ".join(result)


# --- Filler removal ---

def remove_fillers(text: str) -> str:
    """Remove English filler words (uh, um, hmm, etc.)."""
    cleaned = _FILLER_PATTERN.sub("", text)
    # Capitalize first letter if filler was at the start
    cleaned = cleaned.lstrip()
    if cleaned and cleaned[0].islower() and (not text or text.lstrip()[0].isupper()):
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


# --- Stutter collapse ---

def collapse_stutters(text: str) -> str:
    """Collapse 3+ consecutive repetitions of short words. 'I I I I' -> 'I'."""
    words = text.split()
    result = []
    i = 0

    while i < len(words):
        word = words[i]

        # Only collapse short (1-2 char) alphabetic words
        if len(word) <= 2 and word.isalpha():
            count = 1
            while (i + count < len(words)
                   and words[i + count].lower() == word.lower()):
                count += 1
            if count >= 3:
                result.append(word)
                i += count
                continue

        result.append(word)
        i += 1

    return " ".join(result)


# --- Main entry point ---

def clean(text: str, vocab: Optional[List[str]] = None) -> str:
    """Full post-processing pipeline: vocab correction -> filler removal -> stutter collapse."""
    if not text:
        return text
    text = apply_vocab(text, vocab or [])
    text = remove_fillers(text)
    text = collapse_stutters(text)
    # Clean up whitespace
    text = re.sub(r" {2,}", " ", text).strip()
    return text
