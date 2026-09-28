"""The captured-data format every contestant reads: AGCAP1.

A capture stands in for what gitdata.fetch_pull_request and event.load hand
to the checks, after git has run and before any check has: the PR title and
body, each commit message, and per changed file its raw path, git's reading of
the new side (text, binary or submodule) and the raw bytes of its added lines
with their line numbers. Decoding, classification by extension, the checks
and the report are left to the contestant. Length-prefixed, so any byte can
appear in any field.

    AGCAP1\\n
    mode <error|warn>\\n
    limit <work|findings|line_length|hit> <int>\\n     optional, for parity cases
    title <n>\\n<n bytes>\\n
    body <n>\\n<n bytes>\\n
    commit <sha> <n>\\n<n bytes>\\n
    file <text|binary|submodule> <n>\\n<n bytes of path>\\n
    line <number> <n>\\n<n bytes>\\n                   belongs to the last file
    end\\n
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAGIC = b"AGCAP1\n"
LIMIT_NAMES = {
    "work": "WORK_LIMIT",
    "findings": "FINDINGS_LIMIT",
    "line_length": "LINE_LENGTH_LIMIT",
    "hit": "HIT_LIMIT",
}


@dataclass
class CapturedFile:
    kind: str
    path: bytes
    lines: list[tuple[int, bytes]] = field(default_factory=list)


@dataclass
class Capture:
    mode: str = "error"
    limits: dict[str, int] = field(default_factory=dict)
    title: bytes = b""
    body: bytes = b""
    commits: list[tuple[str, bytes]] = field(default_factory=list)
    files: list[CapturedFile] = field(default_factory=list)

    def dump(self) -> bytes:
        out = bytearray(MAGIC)
        out += f"mode {self.mode}\n".encode()
        for name, value in self.limits.items():
            out += f"limit {name} {value}\n".encode()
        out += b"title %d\n%s\n" % (len(self.title), self.title)
        out += b"body %d\n%s\n" % (len(self.body), self.body)
        for sha, message in self.commits:
            out += b"commit %s %d\n%s\n" % (sha.encode(), len(message), message)
        for changed in self.files:
            out += b"file %s %d\n%s\n" % (changed.kind.encode(), len(changed.path), changed.path)
            for number, raw in changed.lines:
                out += b"line %d %d\n%s\n" % (number, len(raw), raw)
        out += b"end\n"
        return bytes(out)


def load(data: bytes) -> Capture:
    if not data.startswith(MAGIC):
        raise ValueError("not an AGCAP1 capture")
    capture = Capture()
    pos = len(MAGIC)

    def header() -> list[bytes]:
        nonlocal pos
        end = data.index(b"\n", pos)
        parts = data[pos:end].split(b" ")
        pos = end + 1
        return parts

    def payload(size: int) -> bytes:
        nonlocal pos
        chunk = data[pos:pos + size]
        if len(chunk) != size or data[pos + size:pos + size + 1] != b"\n":
            raise ValueError("truncated capture")
        pos += size + 1
        return chunk

    while True:
        parts = header()
        tag = parts[0]
        if tag == b"end":
            return capture
        if tag == b"mode":
            capture.mode = parts[1].decode()
        elif tag == b"limit":
            capture.limits[parts[1].decode()] = int(parts[2])
        elif tag == b"title":
            capture.title = payload(int(parts[1]))
        elif tag == b"body":
            capture.body = payload(int(parts[1]))
        elif tag == b"commit":
            capture.commits.append((parts[1].decode(), payload(int(parts[2]))))
        elif tag == b"file":
            capture.files.append(CapturedFile(parts[1].decode(), payload(int(parts[2]))))
        elif tag == b"line":
            number = int(parts[1])
            capture.files[-1].lines.append((number, payload(int(parts[2]))))
        else:
            raise ValueError(f"unknown record {tag!r}")
