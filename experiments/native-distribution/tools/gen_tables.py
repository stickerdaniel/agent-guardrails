"""Generate rust/src/tables.rs, the Unicode property tables of the drive.

Every bit comes from calling the frozen baseline's own predicates, or the
exact str/re/unicodedata call the baseline makes, on every code point of the
running CPython. The tables therefore freeze CPython 3.14's Unicode 16.0.0
semantics, the policy Gate U accepted, not whatever the compiler or a runner
ships. The output is ASCII Rust source only; no binary table file exists.

    python3.14 -I tools/gen_tables.py --baseline DIR           # write
    python3.14 -I tools/gen_tables.py --baseline DIR --check   # compare

DIR is the agent_guardrails package root at baseline.BASELINE_COMMIT. --check
fails when the committed file differs from a fresh generation, or when the
generated property values differ from the benchmark prototype's recorded
tables (VALUES_SHA256, STAGE2_SHA256), which Gate U reviewed.

Derived from the Gate X benchmark prototype's harness/gen_tables.py, which
wrote the second stage as a raw stage2.bin; that representation is replaced
by the decimal array below.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baseline  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TABLES = os.path.join(os.path.dirname(HERE), "rust", "src", "tables.rs")

# The prototype's recorded outputs (results/tables-manifest.json): the
# little-endian 16-bit property value of every code point, and its raw
# second stage. Equal hashes mean the ASCII tables carry the same data.
VALUES_SHA256 = "6bb8f501a593a07491727fee3181f7732329fd348a390277386dc61c418b29f9"
STAGE2_SHA256 = "d3430b0007b4241e5a1aaf8d126093d869c9f2a42046c85853372adae048bc71"
PYTHON = (3, 14)
UNIDATA = "16.0.0"

INVISIBLE, CONTROL, USPACE, PRIVATE, ESCAPE, RTL, DIGIT, WORD = (1 << b for b in range(8))
ALPHA, PRINTABLE, LOOKALIKE, DOMAIN = (1 << b for b in range(8, 12))
SCRIPT_SHIFT = 12  # three bits
SCRIPTS = {"LATIN": 1, "GREEK": 2, "CJK": 3, "HIRAGANA": 4, "KATAKANA": 4, "HANGUL": 4,
           "IDEOGRAPHIC": 4, "MONGOLIAN": 5}
_WORD = re.compile(r"[^\W\d_]")
MAX = 0x110000
SHIFT = 7


def generate(hidden) -> tuple[str, dict]:
    def script_class(cp: int) -> int:
        name = hidden._script(cp)
        if name in SCRIPTS:
            return SCRIPTS[name]
        if name in hidden._JOINING_SCRIPTS:
            return 6
        return 0

    def in_describe_domain(cp: int) -> bool:
        """Every code point scan() or mixed_script_words() can report, so the
        only ones describe() is ever asked about."""
        return (
            cp < 0x20 or cp == 0x7F
            or hidden.is_tag(cp) or hidden.is_variation_selector(cp)
            or cp in (hidden._ZWJ, hidden._ZWNJ, hidden._BOM, hidden._POP_ISOLATE)
            or cp in hidden._BIDI_MARKS or cp in hidden._ISOLATES or cp in hidden._MONGOLIAN_FORMAT
            or hidden.is_invisible(cp) or hidden.is_control(cp)
            or hidden.is_unusual_space(cp) or hidden.is_private_use(cp)
            or cp in hidden.LOOKALIKES or cp in hidden.LATIN_LOOKALIKES or hidden._fake_latin(cp)
        )

    def props(cp: int) -> int:
        ch = chr(cp)
        bits = 0
        if hidden.is_invisible(cp):
            bits |= INVISIBLE
        if hidden.is_control(cp):
            bits |= CONTROL
        if hidden.is_unusual_space(cp):
            bits |= USPACE
        if hidden.is_private_use(cp):
            bits |= PRIVATE
        if hidden._needs_escape(cp):
            bits |= ESCAPE
        if unicodedata.bidirectional(ch) in ("R", "AL"):
            bits |= RTL
        if ch.isdigit():
            bits |= DIGIT
        if _WORD.match(ch):
            bits |= WORD
        if ch.isalpha():
            bits |= ALPHA
        if ch.isprintable():
            bits |= PRINTABLE
        if cp in hidden.LOOKALIKES:
            bits |= LOOKALIKE
        if in_describe_domain(cp):
            bits |= DOMAIN
        return bits | (script_class(cp) << SCRIPT_SHIFT)

    values = [props(cp) for cp in range(MAX)]

    # Assumptions the drive relies on, checked here rather than trusted.
    strip_set = {cp for cp in range(MAX) if chr(cp).isspace() and (cp == 9 or not hidden._needs_escape(cp))}
    assert strip_set == {0x09, 0x20}, strip_set
    assert all(values[cp] & ALPHA == (ALPHA if chr(cp).isalpha() else 0) for cp in range(0x80))

    combos = sorted(set(values))
    assert len(combos) <= 256, len(combos)
    index = {value: position for position, value in enumerate(combos)}
    block = 1 << SHIFT
    stage1: list[int] = []
    blocks: dict[bytes, int] = {}
    stage2 = bytearray()
    for start in range(0, MAX, block):
        chunk = bytes(index[values[cp]] for cp in range(start, start + block))
        if chunk not in blocks:
            blocks[chunk] = len(blocks)
            stage2 += chunk
        stage1.append(blocks[chunk])
    assert len(blocks) < 65536

    names = []
    for cp in range(MAX):
        if values[cp] & DOMAIN:
            described = hidden.describe(cp)
            bare = f"U+{cp:04X}"
            if described != bare:
                assert described.startswith(bare + " ")
                names.append((cp, described[len(bare) + 1:]))

    header = (
        f"Generated by tools/gen_tables.py from CPython {PYTHON[0]}.{PYTHON[1]}, "
        f"unicodedata {UNIDATA}, and the frozen baseline {baseline.BASELINE_COMMIT}. Do not edit."
    )
    lines = [f"// {header}", f"pub const SHIFT: u32 = {SHIFT};",
             f"pub const UNIDATA_VERSION: &str = \"{UNIDATA}\";",
             f"pub static STAGE1: [u16; {len(stage1)}] = ["]
    for start in range(0, len(stage1), 24):
        lines.append("    " + ", ".join(str(v) for v in stage1[start:start + 24]) + ",")
    lines.append("];")
    lines.append(f"pub static STAGE2: [u8; {len(stage2)}] = [")
    for start in range(0, len(stage2), 32):
        lines.append("    " + ", ".join(str(v) for v in stage2[start:start + 32]) + ",")
    lines.append("];")
    lines.append(f"pub static PROPS: [u16; {len(combos)}] = [")
    for start in range(0, len(combos), 16):
        lines.append("    " + ", ".join(str(v) for v in combos[start:start + 16]) + ",")
    lines.append("];")
    lines.append(f"pub static NAMES: [(u32, &str); {len(names)}] = [")
    for cp, name in names:
        assert name.isascii() and '"' not in name and "\\" not in name, name
        lines.append(f'    (0x{cp:X}, "{name}"),')
    lines.append("];")
    source = "\n".join(lines) + "\n"
    assert source.isascii()

    manifest = {
        "python": sys.version.split()[0],
        "unidata_version": unicodedata.unidata_version,
        "baseline_commit": baseline.BASELINE_COMMIT,
        "block_shift": SHIFT,
        "stage1_entries": len(stage1),
        "stage2_unique_blocks": len(blocks),
        "stage2_bytes": len(stage2),
        "property_combinations": len(combos),
        "named_code_points": len(names),
        "values_sha256": hashlib.sha256(b"".join(v.to_bytes(2, "little") for v in values)).hexdigest(),
        "stage2_sha256": hashlib.sha256(bytes(stage2)).hexdigest(),
        "tables_rs_sha256": hashlib.sha256(source.encode("ascii")).hexdigest(),
    }
    return source, manifest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--baseline", required=True, help="package root at the baseline commit")
    parser.add_argument("--check", action="store_true", help="compare with the committed file")
    parser.add_argument("--manifest", help="also write the generation record here as JSON")
    args = parser.parse_args(argv)

    if sys.version_info[:2] != PYTHON or unicodedata.unidata_version != UNIDATA:
        print(
            f"gen_tables: needs CPython {PYTHON[0]}.{PYTHON[1]} with unicodedata {UNIDATA}, "
            f"found {sys.version.split()[0]} with {unicodedata.unidata_version}",
            file=sys.stderr,
        )
        return 1
    hidden, _rules, _report, _gitdata = baseline.import_modules(args.baseline)
    source, manifest = generate(hidden)
    if args.manifest:
        with open(args.manifest, "w", encoding="ascii") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
    problems = []
    if manifest["values_sha256"] != VALUES_SHA256:
        problems.append("property values differ from the recorded prototype tables")
    if manifest["stage2_sha256"] != STAGE2_SHA256:
        problems.append("second stage differs from the recorded prototype stage2.bin")
    if args.check:
        with open(TABLES, "rb") as handle:
            committed = handle.read()
        if committed != source.encode("ascii"):
            problems.append(f"{os.path.relpath(TABLES)} differs from a fresh generation")
    else:
        with open(TABLES, "w", encoding="ascii", newline="\n") as handle:
            handle.write(source)
    print(json.dumps(manifest, indent=2))
    for problem in problems:
        print(f"gen_tables: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
