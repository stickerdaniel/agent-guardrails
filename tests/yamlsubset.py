"""Just enough YAML to read this repository's own action and workflow files.

Block mappings, block sequences, literal block scalars, flow sequences of
scalars, and plain or quoted scalars without escapes. Anything else raises,
so a construct this reader does not know cannot be misread in silence. Keys
stay strings: "on" is not turned into True.
"""

from __future__ import annotations

import re
from typing import Any

_KEY = re.compile(r"([A-Za-z0-9_.\-/]+):(?: +(.*))?")
_INTEGER = re.compile(r"-?[0-9]+")
# The YAML core schema's null spellings, read like an empty mapping value.
_NULLS = ("null", "Null", "NULL", "~")


def load(text: str) -> Any:
    parser = _Parser(text.split("\n"))
    value = parser.block(0)
    parser.skip()
    if parser.index != len(parser.lines):
        raise ValueError(f"line {parser.index + 1}: cannot parse {parser.lines[parser.index]!r}")
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_comment(text: str) -> str:
    quote = None
    for index, character in enumerate(text):
        if quote:
            if character == quote:
                quote = None
        elif character in "'\"" and (index == 0 or text[index - 1] in " [,"):
            quote = character
        elif character == "#" and (index == 0 or text[index - 1] == " "):
            return text[:index].rstrip()
    if quote:
        raise ValueError(f"unterminated quote in {text!r}")
    return text.rstrip()


def _scalar(text: str) -> Any:
    if text.startswith("["):
        if not text.endswith("]"):
            raise ValueError(f"unsupported flow sequence {text!r}")
        inner = text[1:-1].strip()
        if not inner:
            return []
        parts = [part.strip() for part in inner.split(",")]
        if not all(parts):
            raise ValueError(f"unsupported empty entry in {text!r}")
        return [_scalar(part) for part in parts]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        if "\\" in text or text[0] in text[1:-1]:
            raise ValueError(f"unsupported quoted scalar {text!r}")
        return text[1:-1]
    if text in _NULLS:
        return None
    if text in ("true", "false"):
        return text == "true"
    if _INTEGER.fullmatch(text):
        return int(text)
    if text[:1] in ("{", "&", "*", "!", "|", ">", "'", '"', "@", "`"):
        raise ValueError(f"unsupported scalar {text!r}")
    return text


class _Parser:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.index = 0

    def skip(self) -> None:
        while self.index < len(self.lines):
            stripped = self.lines[self.index].strip()
            if stripped and not stripped.startswith("#"):
                return
            self.index += 1

    def block(self, minimum: int) -> Any:
        self.skip()
        if self.index == len(self.lines):
            return None
        line = self.lines[self.index]
        if "\t" in line[: _indent(line) + 1]:
            raise ValueError(f"line {self.index + 1}: tab in indentation")
        indent = _indent(line)
        if indent < minimum:
            return None
        if line[indent:].startswith("-"):
            return self.sequence(indent)
        return self.mapping(indent)

    def mapping(self, indent: int) -> dict:
        result: dict = {}
        while True:
            self.skip()
            if self.index == len(self.lines):
                return result
            line = self.lines[self.index]
            if _indent(line) < indent:
                return result
            match = _KEY.fullmatch(line[indent:]) if _indent(line) == indent else None
            if match is None:
                raise ValueError(f"line {self.index + 1}: expected a key in {line!r}")
            key = match.group(1)
            if key in result:
                raise ValueError(f"line {self.index + 1}: duplicate key {key!r}")
            self.index += 1
            result[key] = self.value(_strip_comment(match.group(2) or ""), indent)

    def value(self, rest: str, indent: int) -> Any:
        if rest in ("|", "|-"):
            return self.literal(indent, keep_newline=rest == "|")
        if rest:
            return _scalar(rest)
        return self.block(indent + 1)

    def literal(self, indent: int, *, keep_newline: bool) -> str:
        lines: list[str] = []
        block_indent = None
        while self.index < len(self.lines):
            line = self.lines[self.index]
            if line.strip():
                if _indent(line) <= indent:
                    break
                if block_indent is None:
                    block_indent = _indent(line)
                if _indent(line) < block_indent:
                    raise ValueError(f"line {self.index + 1}: literal block dedents")
                lines.append(line[block_indent:])
            else:
                lines.append("")
            self.index += 1
        while lines and not lines[-1]:
            lines.pop()
        text = "\n".join(lines)
        return text + "\n" if keep_newline and text else text

    def sequence(self, indent: int) -> list:
        items: list = []
        while True:
            self.skip()
            if self.index == len(self.lines):
                return items
            line = self.lines[self.index]
            if _indent(line) < indent:
                return items
            if _indent(line) > indent or not line[indent:].startswith("- "):
                raise ValueError(f"line {self.index + 1}: expected a list item in {line!r}")
            content = line[indent + 2 :]
            inner = indent + 2 + _indent(content)
            content = content.lstrip(" ")
            if _KEY.fullmatch(content):
                self.lines[self.index] = " " * inner + content
                items.append(self.mapping(inner))
            else:
                self.index += 1
                items.append(_scalar(_strip_comment(content)))
