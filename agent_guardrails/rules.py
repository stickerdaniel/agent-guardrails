"""The checks themselves: bot trailers, bot identities, model attribution,
hidden Unicode."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import gitdata, hidden
from .attribution import _ERROR as ATTRIBUTION_ERROR
from .attribution import has_model_attribution
from .gitdata import ChangedFile, Commit

# Bare identities, matched against a trailer address and against the
# author/committer fields. Each is a vendor-controlled address or a known
# agent's GitHub App handle. The optional digits cover GitHub's
# "<id>+<handle>@users.noreply.github.com" form, whose id varies per app. This
# is the AI_IDENTITY_RE literal shared by every caller this action replaces.
AI_IDENTITY = (
    r"(noreply@anthropic\.com|cursoragent@cursor\.com|(noreply|codex)@openai\.com"
    r"|noreply@aider\.chat|openhands@all-hands\.dev|noreply@opencode\.ai"
    r"|noreply@continue\.dev|clio-agent@sisyphuslabs\.ai|roomote@roocode\.com"
    r"|copilot@github\.com|([0-9]+[+])?(Copilot|gemini-code-assist\[bot\]"
    r"|greptile-apps\[bot\]|coderabbitai\[bot\]|ellipsis-dev\[bot\]"
    r"|qodo-merge-pro\[bot\]|sweep-ai\[bot\]|bito-code-review\[bot\]"
    r"|roomote\[bot\]|factory-droid\[bot\]|opencode-agent\[bot\]"
    r"|google-labs-jules\[bot\])@users\.noreply\.github\.com)"
)
_IDENTITY_RE = re.compile(AI_IDENTITY, re.IGNORECASE)
# Anchored to a whole trailer line, so a mention in prose does not match. Lines
# are split on LF only, so a CRLF line keeps its CR, and the trailing \s*
# absorbs it the way [[:space:]]* did under grep.
_TRAILER_RE = re.compile(
    r"\s*co-authored-by:\s+.*<" + AI_IDENTITY + r">\s*", re.IGNORECASE
)

# Dependency bots write no model output, so they owe no attribution line. They
# are exempt from nothing else.
EXEMPT_FROM_ATTRIBUTION = frozenset({"renovate[bot]", "dependabot[bot]"})

# The annotation title of each hidden Unicode rule.
_UNICODE_TITLES = {
    "invisible-char": "Invisible character",
    "private-use": "Private-use character",
    "unusual-space": "Unusual space",
    "homoglyph": "Look-alike letter",
}
_NOUNS = {
    "invisible-char": "invisible character",
    "private-use": "private-use character",
    "unusual-space": "unusual space",
}
# Unusual spaces are common in pasted prose, so they only warn. Every other
# rule is an error unless the caller chose warn.
_WARNING_RULES = frozenset({"unusual-space"})
_UNSCANNABLE = "Cannot scan a changed file"


@dataclass(frozen=True)
class Finding:
    title: str
    message: str
    severity: str = "error"
    file: str | None = None
    line: int | None = None


def bot_trailers(text: str) -> list[str]:
    """Lines of text that are Co-Authored-By trailers naming a coding agent."""
    return [line for line in text.split("\n") if _TRAILER_RE.fullmatch(line)]


def is_bot_identity(address: str) -> bool:
    return _IDENTITY_RE.fullmatch(address) is not None


def check_commit(commit: Commit) -> list[Finding]:
    """Behaviours 1 and 2: a trailer in the message, an agent as author or
    committer. Squash merging can carry these trailers into the merge commit."""
    findings = [
        Finding(
            "Bot co-author trailer in a commit",
            f"Commit {commit.sha} contains a bot Co-Authored-By line: "
            f"{line.strip()}. Reword the commit with git rebase -i and "
            "remove the line.",
        )
        for line in bot_trailers(commit.message)
    ]
    for role, address in (
        ("authored", commit.author_email),
        ("committed", commit.committer_email),
    ):
        if is_bot_identity(address):
            findings.append(
                Finding(
                    "Bot commit author",
                    f"Commit {commit.sha} is {role} by a coding agent: {address}. "
                    "Rewrite it with git commit --amend --reset-author --no-edit, "
                    "which makes you its author and committer.",
                )
            )
    return findings


def check_body(body: str | None) -> list[Finding]:
    """Behaviour 3. The body can become the squash commit message, so a
    trailer there reaches the default branch like one in a commit. Reads the
    raw body: a Macroscope block lands in that commit message too."""
    return [
        Finding(
            "Bot co-author trailer in the PR body",
            f"The PR body contains a bot Co-Authored-By line: {line.strip()}. "
            "Edit the PR description and remove it.",
        )
        for line in bot_trailers(body or "")
    ]


def check_attribution(body: str | None, login: str, required: bool) -> list[Finding]:
    """Behaviour 4, only where the caller asks for it."""
    if not required or login in EXEMPT_FROM_ATTRIBUTION:
        return []
    if has_model_attribution(body):
        return []
    return [Finding("PR model attribution required", ATTRIBUTION_ERROR)]


def check_unicode(text: str | None, where: str, mode: str) -> list[Finding]:
    """Behaviour 5 for the title, the raw body, or a commit message, line by
    line. None of them is a file, so a BOM is never at a file's start there.
    where names the text, such as "the PR body"."""
    lines = (text or "").split("\n")
    findings = []
    for number, line in enumerate(lines, 1):
        label = where if len(lines) == 1 else f"line {number} of {where}"
        findings += _unicode_line(line.rstrip("\r"), label, mode)
    return findings


def check_changed_file(changed: ChangedFile, mode: str) -> list[Finding]:
    """Behaviour 5 for the added lines of one file. A file that cannot be
    read fails whatever the mode, because unread is not clean."""
    if changed.kind == gitdata.REJECTED_BINARY:
        formats = ", ".join(sorted(gitdata.BINARY_EXTENSIONS))
        return [
            Finding(
                _UNSCANNABLE,
                f"cannot scan {changed.path}: binary content. Git treats the file "
                "as binary, so its lines cannot be checked. Only these formats "
                f"may be binary: {formats}.",
                file=changed.path,
            )
        ]
    if changed.kind == gitdata.UNDECODABLE:
        return [
            Finding(
                _UNSCANNABLE,
                f"cannot decode {changed.path}: its name or its added lines are "
                "not valid UTF-8.",
                file=changed.path,
            )
        ]
    findings = []
    for number, line in changed.added:
        findings += _unicode_line(
            line,
            f"line {number} of {changed.path}",
            mode,
            at_file_start=number == 1,
            file=changed.path,
            number=number,
        )
    return findings


def _unicode_line(
    line: str,
    where: str,
    mode: str,
    *,
    at_file_start: bool = False,
    file: str | None = None,
    number: int | None = None,
) -> list[Finding]:
    """One finding per rule that a line breaks, as upstream groups them."""

    def finding(rule: str, message: str, column: int) -> Finding:
        severity = "warning" if mode == "warn" or rule in _WARNING_RULES else "error"
        text = (
            f"{where[0].upper()}{where[1:]} {message}; the line reads: "
            f"{hidden.snippet(line, column)}"
        )
        return Finding(_UNICODE_TITLES[rule], text, severity, file, number)

    hits: dict[str, list[tuple[int, int]]] = {}
    for rule, column, code_point in hidden.scan(line, at_file_start=at_file_start):
        hits.setdefault(rule, []).append((column, code_point))
    findings = []
    for rule, found in hits.items():
        names = list(dict.fromkeys(hidden.describe(code_point) for _, code_point in found))
        count = len(found)
        message = f"has {count} {_NOUNS[rule]}{'' if count == 1 else 's'}: {', '.join(names[:3])}"
        if len(names) > 3:
            message += ", ..."
        if rule == "invisible-char":
            # Text smuggled in tag characters, selectors, or zero-width bits.
            payload = hidden.hidden_text([code_point for _, code_point in found])
            if payload:
                message += f" (hidden text: '{payload}')"
        findings.append(finding(rule, message, found[0][0]))
    for column, word, odd in hidden.mixed_script_words(line):
        names = ", ".join(dict.fromkeys(hidden.describe(code_point) for code_point in odd))
        findings.append(
            finding("homoglyph", f"has the word '{word}', which mixes Latin with {names}", column)
        )
    return findings
