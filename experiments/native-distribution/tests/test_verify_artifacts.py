"""tools/verify_artifacts.py: what a committed asset has to be.

Synthetic ELF images keep these tests independent of any build; the last
class checks the two committed assets themselves. Runs anywhere Python does.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import verify_artifacts  # noqa: E402
from verify_artifacts import ArtifactError, verify  # noqa: E402

PT_LOAD, PT_DYNAMIC, PT_INTERP = 1, 2, 3


def elf(machine: int = 62, e_type: int = 2, interp: bool = False, needed: bool | None = None) -> bytes:
    """A 64-bit little-endian ELF image with a load segment and, on request,
    an interpreter or a dynamic section with or without DT_NEEDED."""
    phdrs = [(PT_LOAD, 0, 0x200)]
    tail = b""
    base = 64 + 56 * 3
    if interp:
        phdrs.append((PT_INTERP, base, 16))
        tail += b"/lib/ld-musl.so\0"
    if needed is not None:
        offset = base + len(tail)
        entries = ([(1, 1)] if needed else []) + [(0x1E, 8), (0, 0)]
        dynamic = b"".join(struct.pack("<qQ", tag, value) for tag, value in entries)
        phdrs.append((PT_DYNAMIC, offset, len(dynamic)))
        tail += dynamic
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4], header[5], header[6] = 2, 1, 1
    struct.pack_into("<HHI", header, 16, e_type, machine, 1)
    struct.pack_into("<Q", header, 32, 64)  # e_phoff
    struct.pack_into("<HHH", header, 52, 64, 56, len(phdrs))
    table = b"".join(struct.pack("<IIQQQQQQ", t, 5, off, 0, 0, size, size, 8) for t, off, size in phdrs)
    image = bytes(header) + table
    image += b"\0" * (base - len(image)) + tail
    return image + b"\0" * 64


def canonical(payload: bytes, level: int = 9) -> bytes:
    compressor = zlib.compressobj(level, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = compressor.compress(payload) + compressor.flush()
    return (
        verify_artifacts.CANONICAL_HEADER
        + body
        + struct.pack("<II", zlib.crc32(payload), len(payload) & 0xFFFFFFFF)
    )


class EnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.elf = elf()
        self.stream = canonical(self.elf)

    def refused(self, data: bytes, message: str, arch: str = "x64", **fresh: bytes) -> None:
        with self.assertRaises(ArtifactError) as caught:
            verify(data, arch, fresh.get("elf"), fresh.get("gz"))
        self.assertIn(message, str(caught.exception))

    def test_accepts_the_canonical_stream(self) -> None:
        self.assertEqual(verify(self.stream, "x64", self.elf, self.stream), self.elf)

    def test_refuses_a_raw_file_named_gz(self) -> None:
        self.refused(self.elf, "not a gzip stream")

    def test_refuses_a_header_other_than_the_canonical_one(self) -> None:
        for index, value, what in ((3, 0x08, "file name flag"), (4, 0x01, "mtime"), (8, 0x00, "xfl"), (9, 0xFF, "os")):
            with self.subTest(field=what):
                changed = bytearray(self.stream)
                changed[index] = value
                self.refused(bytes(changed), "is not the canonical")

    def test_refuses_a_truncated_stream(self) -> None:
        self.refused(self.stream[: len(self.stream) // 2], "truncated inside the deflate data")
        self.refused(self.stream[:-3], "truncated inside the trailer")

    def test_refuses_a_bad_crc_or_length(self) -> None:
        crc = bytearray(self.stream)
        crc[-8] ^= 0xFF
        self.refused(bytes(crc), "CRC32 does not match")
        size = bytearray(self.stream)
        size[-1] ^= 0x01
        self.refused(bytes(size), "ISIZE does not match")

    def test_refuses_corrupt_deflate_data(self) -> None:
        corrupt = bytearray(self.stream)
        corrupt[10] = 0xFF  # an invalid block type
        with self.assertRaises(ArtifactError):
            verify(bytes(corrupt), "x64")

    def test_refuses_an_appended_member(self) -> None:
        self.refused(self.stream + canonical(b"more"), "second member")

    def test_refuses_trailing_bytes(self) -> None:
        self.refused(self.stream + b"\0", "trailing bytes")

    def test_refuses_a_different_compression_of_the_same_elf(self) -> None:
        other = canonical(self.elf, level=1)
        self.assertEqual(verify(other, "x64"), self.elf)
        self.refused(other, "differ from a fresh compression", gz=self.stream)

    def test_refuses_a_different_executable_in_a_valid_stream(self) -> None:
        changed = self.elf[:-1] + b"\1"
        self.refused(canonical(changed), "differs from the fresh build", elf=self.elf)


class ElfTests(unittest.TestCase):
    def refused(self, image: bytes, arch: str, message: str) -> None:
        with self.assertRaises(ArtifactError) as caught:
            verify(canonical(image), arch)
        self.assertIn(message, str(caught.exception))

    def test_accepts_static_executables_for_their_machine(self) -> None:
        verify(canonical(elf(machine=62)), "x64")
        verify(canonical(elf(machine=183)), "arm64")
        verify(canonical(elf(e_type=3, needed=False)), "x64")  # a static PIE

    def test_refuses_the_other_architecture(self) -> None:
        self.refused(elf(machine=183), "x64", "is not x64")
        self.refused(elf(machine=62), "arm64", "is not arm64")

    def test_refuses_a_dynamically_linked_executable(self) -> None:
        self.refused(elf(interp=True), "x64", "names a dynamic loader")
        self.refused(elf(e_type=3, needed=True), "x64", "needs a shared library")

    def test_refuses_what_is_not_a_64_bit_executable(self) -> None:
        self.refused(b"#!/bin/sh\necho hi\n", "x64", "not an ELF file")
        image = bytearray(elf())
        image[4] = 1
        self.refused(bytes(image), "x64", "not a 64-bit little-endian")
        self.refused(elf(e_type=1), "x64", "is not an executable")


class CommittedAssetTests(unittest.TestCase):
    """The two assets in bin/ as committed. Their equality with a fresh build
    is build/reproduce.py's job; this is their shape."""

    def test_each_asset_is_canonical_static_and_for_its_machine(self) -> None:
        for arch, directory in (("x64", "linux-x64"), ("arm64", "linux-arm64")):
            with self.subTest(arch=arch):
                asset = (ROOT / "bin" / directory / "agent-guardrails.gz").read_bytes()
                verify(asset, arch)

    def test_swapped_assets_are_refused(self) -> None:
        x64 = (ROOT / "bin" / "linux-x64" / "agent-guardrails.gz").read_bytes()
        arm64 = (ROOT / "bin" / "linux-arm64" / "agent-guardrails.gz").read_bytes()
        with self.assertRaises(ArtifactError):
            verify(x64, "arm64")
        with self.assertRaises(ArtifactError):
            verify(arm64, "x64")

    def test_the_command_line_fails_on_a_bad_asset(self) -> None:
        script = str(ROOT / "tools" / "verify_artifacts.py")
        good = subprocess.run(
            [sys.executable, "-I", script, "--arch", "arm64", "--asset", str(ROOT / "bin" / "linux-arm64" / "agent-guardrails.gz")],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(good.returncode, 0, good.stderr)
        bad = subprocess.run(
            [sys.executable, "-I", script, "--arch", "x64", "--asset", str(ROOT / "bin" / "linux-arm64" / "agent-guardrails.gz")],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(bad.returncode, 1)
        self.assertIn("ELF machine 183 is not x64", bad.stderr)


if __name__ == "__main__":
    unittest.main()
