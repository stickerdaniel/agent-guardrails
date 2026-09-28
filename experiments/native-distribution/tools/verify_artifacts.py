"""Verify a Gate X asset: a canonical gzip stream holding one static Linux ELF
for the named architecture, and, when given, the exact bytes of a fresh build.

    python3 tools/verify_artifacts.py --arch x64 --asset bin/linux-x64/agent-guardrails.gz \
        [--elf <fresh ELF>] [--gz <fresh gzip of it>]

The required checker only sees that the file name ends in .gz; it cannot tell
a real archive from a renamed executable, or which program is inside. This is
where that is decided. Any deviation exits 1 with the reason: a missing or
different header, a truncated stream, a bad CRC or length, a second member or
trailing bytes, an ELF for another machine or one that needs a dynamic loader,
and any byte that differs from the fresh build or its fresh compression.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
import zlib

# 1f 8b magic, 08 deflate, 00 no flags, 00000000 no mtime, 02 best
# compression, 03 Unix. What `gzip -9 -c` writes for stdin.
CANONICAL_HEADER = bytes.fromhex("1f8b0800000000000203")
MACHINES = {"x64": 62, "arm64": 183}  # EM_X86_64, EM_AARCH64
PT_DYNAMIC, PT_INTERP = 2, 3
DT_NULL, DT_NEEDED = 0, 1


class ArtifactError(Exception):
    """The asset is not what the recipe promises."""


def decompress_canonical(data: bytes) -> bytes:
    """The one member's payload, after checking the whole envelope."""
    if data[:2] != b"\x1f\x8b":
        raise ArtifactError("not a gzip stream: the magic bytes are missing")
    if data[:10] != CANONICAL_HEADER:
        raise ArtifactError(f"gzip header {data[:10].hex()} is not the canonical {CANONICAL_HEADER.hex()}")
    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    try:
        payload = inflater.decompress(data[10:])
    except zlib.error as error:
        raise ArtifactError(f"deflate data is corrupt: {error}") from None
    if not inflater.eof:
        raise ArtifactError("gzip stream is truncated inside the deflate data")
    trailer = inflater.unused_data
    if len(trailer) < 8:
        raise ArtifactError("gzip stream is truncated inside the trailer")
    crc, size = struct.unpack("<II", trailer[:8])
    if crc != zlib.crc32(payload):
        raise ArtifactError("gzip CRC32 does not match the payload")
    if size != len(payload) & 0xFFFFFFFF:
        raise ArtifactError("gzip ISIZE does not match the payload")
    rest = trailer[8:]
    if rest[:2] == b"\x1f\x8b":
        raise ArtifactError("gzip stream has a second member")
    if rest:
        raise ArtifactError(f"gzip stream has {len(rest)} trailing bytes")
    return payload


def check_elf(elf: bytes, arch: str) -> None:
    """A 64-bit little-endian executable for arch, with no interpreter and no
    shared library it needs: nothing outside the file has to exist to run it."""
    if elf[:4] != b"\x7fELF":
        raise ArtifactError("payload is not an ELF file")
    if len(elf) < 64 or elf[4] != 2 or elf[5] != 1:
        raise ArtifactError("payload is not a 64-bit little-endian ELF file")
    e_type, e_machine = struct.unpack_from("<HH", elf, 16)
    if e_type not in (2, 3):  # ET_EXEC, or ET_DYN for a static PIE
        raise ArtifactError(f"ELF type {e_type} is not an executable")
    if e_machine != MACHINES[arch]:
        raise ArtifactError(f"ELF machine {e_machine} is not {arch} ({MACHINES[arch]})")
    phoff, = struct.unpack_from("<Q", elf, 32)
    phentsize, phnum = struct.unpack_from("<HH", elf, 54)
    if phnum == 0 or phentsize < 56 or phoff + phnum * phentsize > len(elf):
        raise ArtifactError("ELF program headers are missing or out of bounds")
    for index in range(phnum):
        base = phoff + index * phentsize
        p_type, = struct.unpack_from("<I", elf, base)
        if p_type == PT_INTERP:
            raise ArtifactError("ELF names a dynamic loader, so it is not statically linked")
        if p_type == PT_DYNAMIC:
            offset, = struct.unpack_from("<Q", elf, base + 8)
            filesz, = struct.unpack_from("<Q", elf, base + 32)
            if offset + filesz > len(elf):
                raise ArtifactError("ELF dynamic section is out of bounds")
            for entry in range(offset, offset + filesz - 15, 16):
                tag, = struct.unpack_from("<q", elf, entry)
                if tag == DT_NULL:
                    break
                if tag == DT_NEEDED:
                    raise ArtifactError("ELF needs a shared library, so it is not statically linked")


def verify(asset: bytes, arch: str, fresh_elf: bytes | None = None, fresh_gz: bytes | None = None) -> bytes:
    """The ELF inside asset, after every check; raises ArtifactError."""
    elf = decompress_canonical(asset)
    check_elf(elf, arch)
    if fresh_elf is not None and elf != fresh_elf:
        raise ArtifactError("decompressed ELF differs from the fresh build")
    if fresh_gz is not None and asset != fresh_gz:
        raise ArtifactError("gzip bytes differ from a fresh compression of the fresh build")
    return elf


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--arch", required=True, choices=sorted(MACHINES))
    parser.add_argument("--asset", required=True)
    parser.add_argument("--elf", help="a fresh build the payload must equal")
    parser.add_argument("--gz", help="a fresh compression the asset must equal")
    args = parser.parse_args(argv)

    def read(path: str | None) -> bytes | None:
        if path is None:
            return None
        with open(path, "rb") as handle:
            return handle.read()

    asset = read(args.asset)
    try:
        elf = verify(asset, args.arch, read(args.elf), read(args.gz))
    except ArtifactError as error:
        print(f"verify_artifacts: {args.asset}: {error}", file=sys.stderr)
        return 1
    print(
        f"verify_artifacts: {args.asset}: ok, {args.arch}, gzip {len(asset)} bytes "
        f"sha256 {hashlib.sha256(asset).hexdigest()}, ELF {len(elf)} bytes "
        f"sha256 {hashlib.sha256(elf).hexdigest()}"
        + (", equals the fresh build" if args.elf else "")
        + (", equals the fresh compression" if args.gz else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
