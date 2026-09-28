"""Write findings as workflow commands and plain log lines.

Text from the pull request reaches the log only through annotate() and log(),
and both render it with _render() first. A newline, an escape sequence, or an
invisible character shows as <U+XXXX>, so the text cannot start a line of its
own, and the fixed prefix keeps a plain line from starting with "::". The
runner's legacy parser also accepts "##[" anywhere in a line, not only at its
start, so _render() shows the first "#" of every "##[" as <U+0023>.
"""

from __future__ import annotations

import re
from typing import TextIO

from .hidden import visible
from .rules import Finding

_SEVERITIES = ("error", "warning")
_PREFIX = "agent-guardrails: "
# Each "#" that begins a "##[". What is left, "#[", starts no command.
_LEGACY_COMMAND = re.compile(r"#(?=#\[)")


def _render(text: str) -> str:
    """The one way outside text reaches the log."""
    return _LEGACY_COMMAND.sub("<U+0023>", visible(text))


def _data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _property(text: str) -> str:
    return _data(text).replace(":", "%3A").replace(",", "%2C")


class Reporter:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def _write(self, line: str) -> None:
        self._stream.write(line + "\n")
        self._stream.flush()

    def mask(self, value: str) -> None:
        """Register a secret with the runner. Call before git sees it."""
        self._write(f"::add-mask::{value}")

    def log(self, text: str) -> None:
        # The fixed prefix keeps the line from ever starting with "::".
        self._write(_PREFIX + _render(text))

    def annotate(
        self,
        severity: str,
        title: str,
        message: str,
        *,
        file: str | None = None,
        line: int | None = None,
    ) -> None:
        """The only writer of annotations: one workflow command, then the
        same text as a plain log line."""
        if severity not in _SEVERITIES:
            raise ValueError(f"unknown severity {severity!r}")
        properties = []
        if file is not None:
            properties.append(f"file={_property(_render(file))}")
            if line is not None:
                properties.append(f"line={int(line)}")
        properties.append(f"title={_property(_render(title))}")
        self._write(f"::{severity} {','.join(properties)}::{_data(_render(message))}")
        self.log(f"{severity}: {title}: {message}")

    def finding(self, finding: Finding) -> None:
        self.annotate(finding.severity, finding.title, finding.message)

    def failure(self, message: str) -> None:
        """The check could not run to the end, which fails it."""
        self.annotate("error", "agent-guardrails", message)
