# BENCHMARK-ONLY VARIANT, not for production. A copy of the vendored
# agent_guardrails/hidden.py at 40382e2 (itself no_ai_marks/chars.py at
# 747c07a, Copyright (c) 2026 Misha Nasledov, MIT License), with four minimal
# algorithmic changes, each marked "TUNED":
#   1. _script is memoised per code point (unicodedata.name built a string
#      per call).
#   2. scan() skips runs of allowed ASCII with one C-level regex, and returns
#      at once for an all-ASCII line after reporting its controls.
#   3. scan() computes _balanced_isolates and the CJK-line test once per line
#      instead of once per isolate / ideographic space (O(n*k) -> O(n)).
#   4. mixed_script_words() returns at once for an all-ASCII line.
# Output must stay identical to the baseline; harness/parity.py checks it.

"""Invisible characters, unusual spaces, private-use code points, and
mixed-script words.

Every character here is written as an escape so this file passes its own
checks.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from typing import Iterator

# Prepended concatenation marks are category Cf but render visibly.
_VISIBLE_FORMAT = frozenset({
    0x0600, 0x0601, 0x0602, 0x0603, 0x0604, 0x0605, 0x06DD, 0x070F,
    0x0890, 0x0891, 0x08E2, 0x110BD, 0x110CD,
})

# Invisible characters outside category Cf.
_INVISIBLE_EXTRA = frozenset({
    0x034F,                          # COMBINING GRAPHEME JOINER
    0x115F, 0x1160, 0x3164, 0xFFA0,  # Hangul fillers
    0x17B4, 0x17B5,                  # Khmer inherent vowels
    0x180B, 0x180C, 0x180D, 0x180F,  # Mongolian free variation selectors
    0x2800,                          # BRAILLE PATTERN BLANK
    0x1D159,                         # MUSICAL SYMBOL NULL NOTEHEAD (renders blank)
})

# Bidi isolates: legitimate around embedded text in right-to-left lines.
_ISOLATES = frozenset({0x2066, 0x2067, 0x2068})
_POP_ISOLATE = 0x2069

# Mongolian free variation selectors and vowel separator shape Mongolian.
_MONGOLIAN_FORMAT = frozenset({0x180B, 0x180C, 0x180D, 0x180E, 0x180F})

# Arabic-script letters that never join the next letter, so a ZWNJ after
# them has no shaping purpose.
_NON_JOINING_ARABIC = frozenset({
    0x0622, 0x0623, 0x0624, 0x0625, 0x0627, 0x062F, 0x0630, 0x0631, 0x0632,
    0x0648, 0x0698,
})

# Spaces used inside numbers: "12 000", "10 %".
_NUMBER_SPACES = frozenset({0x00A0, 0x2007, 0x2009, 0x202F})

_BIDI_MARKS = frozenset({0x061C, 0x200E, 0x200F})
_ZWNJ = 0x200C
_ZWJ = 0x200D
_BOM = 0xFEFF
_BLACK_FLAG = 0x1F3F4
_CANCEL_TAG = 0xE007F
_IDEOGRAPHIC_SPACE = 0x3000

# Scripts where ZWJ and ZWNJ shape letters (Persian, Indic conjuncts, ...).
_JOINING_SCRIPTS = frozenset({
    "ARABIC", "SYRIAC", "NKO", "MANDAIC", "MONGOLIAN", "DEVANAGARI", "BENGALI",
    "GURMUKHI", "GUJARATI", "ORIYA", "TAMIL", "TELUGU", "KANNADA", "MALAYALAM",
    "SINHALA", "MYANMAR", "KHMER", "TIBETAN", "BALINESE", "JAVANESE",
})
_CJK_SCRIPTS = frozenset({"CJK", "HIRAGANA", "KATAKANA", "HANGUL", "IDEOGRAPHIC"})

# Control characters that ordinary text uses: tab, newline, form feed (page
# breaks in Lisp and Emacs sources), and carriage return.
_ALLOWED_CONTROLS = frozenset({0x09, 0x0A, 0x0C, 0x0D})

# Non-Latin letters that pass for Latin ones. Each maps to a single ASCII
# letter in the Unicode confusables table (UTS #39); see
# THIRD_PARTY_NOTICES.md for its license.
LOOKALIKES = frozenset({
    # Cyrillic
    0x0405, 0x0406, 0x0408, 0x0410, 0x0412, 0x0415, 0x041A, 0x041C, 0x041D,
    0x041E, 0x0420, 0x0421, 0x0422, 0x0423, 0x0425, 0x042C, 0x0430, 0x0433,
    0x0435, 0x043E, 0x0440, 0x0441, 0x0443, 0x0445, 0x0448, 0x0455, 0x0456,
    0x0458, 0x0475, 0x04AE, 0x04AF, 0x04BB, 0x04BD, 0x04C0, 0x04CF, 0x0501,
    0x050C, 0x051A, 0x051B, 0x051C, 0x051D,
    # Greek
    0x037F, 0x0391, 0x0392, 0x0395, 0x0396, 0x0397, 0x0399, 0x039A, 0x039C,
    0x039D, 0x039F, 0x03A1, 0x03A4, 0x03A5, 0x03A7, 0x03B1, 0x03B3, 0x03B9,
    0x03BD, 0x03BF, 0x03C1, 0x03C3, 0x03C5, 0x03F2,
    # Armenian
    0x054D, 0x054F, 0x0555, 0x0561, 0x0563, 0x0566, 0x0570, 0x0578, 0x057C,
    0x057D, 0x0581, 0x0582, 0x0584, 0x0585,
    # Cherokee
    0x13A0, 0x13A1, 0x13A2, 0x13A9, 0x13AA, 0x13AB, 0x13AC, 0x13B3, 0x13B7,
    0x13BB, 0x13C0, 0x13C2, 0x13C3, 0x13CF, 0x13D2, 0x13DA,
}) | frozenset(range(0xA4D0, 0xA4F8))  # Lisu letters

# Latin-script letters (IPA, small capitals) that pass for ASCII ones.
LATIN_LOOKALIKES = frozenset({
    0x0251, 0x0261, 0x026A, 0x1D04, 0x1D0F, 0x1D1C, 0x1D20, 0x1D21, 0x1D22, 0xA731,
})

_WORD = re.compile(r"[^\W\d_]+")


def describe(cp: int) -> str:
    name = unicodedata.name(chr(cp), "")
    if not name and unicodedata.category(chr(cp)) == "Cc":
        name = "control character"
    return f"U+{cp:04X} {name}" if name else f"U+{cp:04X}"


def is_variation_selector(cp: int) -> bool:
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF


def is_tag(cp: int) -> bool:
    return 0xE0000 <= cp <= 0xE007F


def is_invisible(cp: int) -> bool:
    if cp in _VISIBLE_FORMAT:
        return False
    return (
        cp in _INVISIBLE_EXTRA
        or is_variation_selector(cp)
        or unicodedata.category(chr(cp)) == "Cf"
        # Default-ignorable but unassigned: renders as nothing.
        or cp == 0x2065 or 0xFFF0 <= cp <= 0xFFF8 or 0xE0080 <= cp <= 0xE0FFF
    )


def is_control(cp: int) -> bool:
    return unicodedata.category(chr(cp)) == "Cc" and cp not in _ALLOWED_CONTROLS


def is_unusual_space(cp: int) -> bool:
    return cp != 0x20 and unicodedata.category(chr(cp)) in ("Zs", "Zl", "Zp")


def is_private_use(cp: int) -> bool:
    return unicodedata.category(chr(cp)) == "Co"


@functools.cache  # TUNED 1
def _script(cp: int) -> str:
    return unicodedata.name(chr(cp), "").split(" ", 1)[0]


# TUNED 2: every character scan() can act on. Tab, LF, FF, CR and printable
# ASCII fall through its loop with nothing but i += 1.
_CANDIDATES = re.compile(r"[^\t\n\x0c\r\x20-\x7e]+")


def _emojiish(cp: int) -> bool:
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2190 <= cp <= 0x2BFF
        or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299)
    )


def _joiner_ok(cps: list[int], i: int) -> bool:
    """ZWJ inside an emoji sequence, or ZWJ/ZWNJ between letters of a
    script that uses them for shaping."""
    j = i - 1
    while j >= 0 and (is_variation_selector(cps[j]) or 0x1F3FB <= cps[j] <= 0x1F3FF):
        j -= 1
    before = cps[j] if j >= 0 else None
    after = cps[i + 1] if i + 1 < len(cps) else None
    if cps[i] == _ZWJ and before is not None and after is not None:
        if _emojiish(before) and _emojiish(after):
            return True
    prev = cps[i - 1] if i > 0 else None
    if prev in (_ZWJ, _ZWNJ) or (cps[i] == _ZWNJ and prev in _NON_JOINING_ARABIC):
        return False
    return (
        prev is not None
        and _script(prev) in _JOINING_SCRIPTS
        and (after is None or after == 0x20 or _script(after) in _JOINING_SCRIPTS)
    )


def _balanced_isolates(cps: list[int]) -> bool:
    depth = 0
    for cp in cps:
        if cp in _ISOLATES:
            depth += 1
        elif cp == _POP_ISOLATE:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _variation_ok(cps: list[int], i: int) -> bool:
    """A single selector after an emoji, symbol, keycap base, or (for the
    ideographic selectors) a CJK ideograph."""
    if i == 0:
        return False
    base, cp = cps[i - 1], cps[i]
    if cp >= 0xE0100:
        return _script(base) == "CJK"
    if base < 0x80:
        return chr(base) in "0123456789#*"
    return _script(base) != "LATIN"


def _flag_ok(cps: list[int], start: int, end: int) -> bool:
    """Subdivision flags: black flag, tag letters, cancel tag."""
    return (
        start > 0
        and cps[start - 1] == _BLACK_FLAG
        and end - start >= 2
        and cps[end - 1] == _CANCEL_TAG
        and all(0xE0020 <= c <= 0xE007E for c in cps[start:end - 1])
    )


def scan(text: str, *, at_file_start: bool = False, allowed: frozenset = frozenset()) -> Iterator[tuple[str, int, int]]:
    """Yield (rule, column, code point) for suspicious characters in one
    line. Columns are 1-based code point offsets."""
    if text.isascii():  # TUNED 2: only controls can be reported here.
        for run in _CANDIDATES.finditer(text):
            for i in range(run.start(), run.end()):
                cp = ord(text[i])
                if cp not in allowed:
                    yield "invisible-char", i + 1, cp
        return
    cps = [ord(c) for c in text]
    n = len(cps)
    rtl = None
    balanced = None  # TUNED 3
    cjk_line = None  # TUNED 3

    def rtl_line() -> bool:
        nonlocal rtl
        if rtl is None:
            # Bidi marks are class R themselves, so look past them.
            rtl = any(
                unicodedata.bidirectional(c) in ("R", "AL")
                for c in text if ord(c) not in _BIDI_MARKS
            )
        return rtl

    for run in _CANDIDATES.finditer(text):  # TUNED 2
        i = run.start()
        end = run.end()
        while i < end:
            cp = cps[i]
            if cp in allowed:
                i += 1
                continue
            if cp < 0x80:
                # Every ASCII candidate is a control the baseline reports.
                yield "invisible-char", i + 1, cp
                i += 1
                continue
            if is_tag(cp) or is_variation_selector(cp):
                same_kind = is_tag if is_tag(cp) else is_variation_selector
                j = i
                while j < n and same_kind(cps[j]):
                    j += 1
                ok = _flag_ok(cps, i, j) if is_tag(cp) else (j - i == 1 and _variation_ok(cps, i))
                if not ok:
                    for k in range(i, j):
                        yield "invisible-char", k + 1, cps[k]
                i = j
                continue
            if cp == _BOM and i == 0 and at_file_start:
                pass
            elif cp in (_ZWJ, _ZWNJ):
                if not _joiner_ok(cps, i):
                    yield "invisible-char", i + 1, cp
            elif cp in _BIDI_MARKS:
                if not rtl_line():
                    yield "invisible-char", i + 1, cp
            elif cp in _ISOLATES or cp == _POP_ISOLATE:
                ok = False
                if rtl_line():
                    if balanced is None:  # TUNED 3
                        balanced = _balanced_isolates(cps)
                    ok = balanced
                if not ok:
                    yield "invisible-char", i + 1, cp
            elif cp in _MONGOLIAN_FORMAT:
                if not (i > 0 and _script(cps[i - 1]) == "MONGOLIAN"):
                    yield "invisible-char", i + 1, cp
            elif is_invisible(cp) or is_control(cp):
                yield "invisible-char", i + 1, cp
            elif is_unusual_space(cp):
                cjk = False
                if cp == _IDEOGRAPHIC_SPACE:
                    if cjk_line is None:  # TUNED 3
                        cjk_line = any(
                            ord(c) != _IDEOGRAPHIC_SPACE and _script(ord(c)) in _CJK_SCRIPTS for c in text
                        )
                    cjk = cjk_line
                in_number = (
                    cp in _NUMBER_SPACES and 0 < i < n - 1
                    and chr(cps[i - 1]).isdigit() and (chr(cps[i + 1]).isdigit() or cps[i + 1] == 0x25)
                )
                if not (cjk or in_number):
                    yield "unusual-space", i + 1, cp
            elif is_private_use(cp):
                yield "private-use", i + 1, cp
            i += 1


def hidden_text(cps: list[int]) -> str:
    """Decode text smuggled in tag characters, variation selector runs, or
    zero-width binary (U+200B is 0, U+200C is 1)."""
    parts = []
    bits = "".join("0" if cp == 0x200B else "1" for cp in cps if cp in (0x200B, 0x200C))
    if len(bits) >= 8:
        data = bytes(int(bits[k:k + 8], 2) for k in range(0, len(bits) - len(bits) % 8, 8))
        decoded = data.decode("utf-8", "replace")
        if decoded.isascii() and decoded.isprintable() and decoded.strip():
            parts.append(decoded)
    tags = "".join(chr(cp - 0xE0000) for cp in cps if 0xE0020 <= cp <= 0xE007E)
    if len(tags) > 1:
        parts.append(tags)
    selectors = [cp for cp in cps if is_variation_selector(cp)]
    if len(selectors) > 1:
        data = bytes(cp - 0xFE00 if cp <= 0xFE0F else cp - 0xE0100 + 16 for cp in selectors)
        decoded = data.decode("utf-8", "replace")
        if decoded.isprintable():
            parts.append(decoded)
    return " ".join(parts)


def _fake_latin(cp: int) -> bool:
    """Fullwidth and mathematical alphanumeric Latin letters, the Kelvin
    sign, and Roman numerals."""
    return (
        0xFF21 <= cp <= 0xFF3A or 0xFF41 <= cp <= 0xFF5A or 0x1D400 <= cp <= 0x1D6A3
        or cp == 0x212A or 0x2160 <= cp <= 0x217F
    )


def mixed_script_words(text: str) -> Iterator[tuple[int, str, list[int]]]:
    """Yield (column, word, offending code points) for words that pass for
    Latin but aren't: Latin mixed with look-alikes from another script, IPA
    or small capitals among ASCII letters, or a word made only of
    look-alikes on an otherwise Latin line (a Cyrillic "a" used as the
    English article)."""
    if text.isascii():  # TUNED 4: every word is ASCII and skipped.
        return
    latin_line = foreign_line = None
    for m in _WORD.finditer(text):
        word = m.group()
        if word.isascii():
            continue
        cps = [ord(c) for c in word]
        if any(cp < 0x80 or _script(cp) == "LATIN" for cp in cps):
            odd = [cp for cp in cps if cp in LOOKALIKES or _fake_latin(cp)]
            if not odd and all(cp < 0x80 or cp in LATIN_LOOKALIKES for cp in cps):
                odd = [cp for cp in cps if cp in LATIN_LOOKALIKES]
        else:
            if latin_line is None:
                latin_line = any(c.isascii() and c.isalpha() for c in text)
                foreign_line = any(
                    c.isalpha() and not c.isascii() and ord(c) not in LOOKALIKES
                    and _script(ord(c)) != "LATIN"
                    for c in text
                )
            # Greek is left out so math like "angle \U000003B1" stays clean.
            whole = latin_line and not foreign_line and all(
                cp in LOOKALIKES and _script(cp) != "GREEK" for cp in cps
            )
            odd = cps if whole else []
        if odd:
            yield m.start() + 1, word, odd


def _needs_escape(cp: int) -> bool:
    return (
        cp < 0x20 or 0x7F <= cp < 0xA0
        or is_invisible(cp) or is_tag(cp)
        or is_unusual_space(cp) or is_private_use(cp)
    )


def visible(text: str) -> str:
    """Show invisible and control characters as <U+XXXX>."""
    return "".join(
        ch if ch == "\t" or not _needs_escape(ord(ch)) else f"<U+{ord(ch):04X}>"
        for ch in text
    )


def snippet(line: str, column: int | None, width: int = 120) -> str:
    start = max(0, (column or 1) - 41)
    piece = line[start:start + width]
    text = visible(piece).strip()
    if start > 0:
        text = "..." + text
    if start + width < len(line):
        text += "..."
    return text
