"""The checks themselves: bot trailers, bot identities, model attribution,
hidden Unicode."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import attribution, gitdata, hidden
from .event import Policy, selects
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
# absorbs it the way [[:space:]]* did under grep. One \s where grep had
# [[:space:]]+: .* takes the rest of the run, and a line holds no LF, so the
# lines that match are the same, while \s+ followed by .* would try every
# split of a long run of spaces and take quadratic time. The address is the
# last bracketed one, captured raw for is_agent: no built-in address holds a
# bracket or ends in a space, so this matches the lines that embedding
# AI_IDENTITY between the brackets matched.
_TRAILER_RE = re.compile(r"\s*co-authored-by:\s.*<([^<>]*)>\s*", re.IGNORECASE)

# Dependency bots write no model output, so they owe no attribution line. They
# are exempt from nothing else. The logins a caller adds are exempt the same way.
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
# The rules the vendored scanner reports; look-alike words have their own.
_SCANNED_RULES = tuple(_NOUNS)
# The severity of each mode that runs; off runs nothing.
_SEVERITIES = {"error": "error", "warn": "warning"}
_UNSCANNABLE = "Cannot scan a changed file"

# What one run may spend on the checks. A unit is one code point that the
# vendored scanner visits. A line costs its length, and its length again for
# each bidi isolate and each ideographic space in it: the scanner searches
# the whole line again at each of those, so a short line of them costs as
# much as a long file. Measured on an Apple M-series host with Python 3.14,
# the dearest linear text (CJK, three bytes a code point) takes 1 microsecond
# a unit and the repeated searches at most 160 nanoseconds. The 64 MiB git
# output limit leaves at most 22 million CJK units, so the worst case of this
# limit came to about 35 seconds there, while ordinary text never reaches it.
# The findings limit bounds memory and the log; GitHub shows only ten
# annotations of each severity per step anyway.
WORK_LIMIT = 100_000_000
FINDINGS_LIMIT = 1000
# What one line may hold in memory while it is checked, both enforced before
# the memory is taken. The scanner copies a line into a list of code points
# and the look-alike check copies its words again, up to about 80 bytes a
# code point together, so a longer line is not scanned. Each suspicious
# character is kept as a (column, code point) pair of about 100 bytes until
# the line's findings are built, so a line with more of them stops the
# collection. A line of 60 MiB of control characters would otherwise have
# held more than 5 GiB of pairs for its one finding.
LINE_LENGTH_LIMIT = 4_000_000
HIT_LIMIT = 100_000
# How much of a trailer line, a word, or a decoded payload a message quotes,
# so that the findings limit also bounds the log.
_QUOTE_LIMIT = 200
_REPEATED_SEARCHES = ("\u2066", "\u2067", "\u2068", "\u2069", "\u3000")


class LimitReached(Exception):
    """The checks stopped before the end, which fails the run in either mode."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        # What the check that stopped had found before it stopped. Each
        # level it passes through puts its own earlier findings in front.
        self.findings: list[Finding] = []


class Budget:
    """The work and findings one run may spend. Share one across every check
    of a run; each check makes its own when given none."""

    def __init__(self) -> None:
        self._work = 0
        self._findings = 0

    def spend(self, line: str, where: str) -> None:
        """Charge one line before the scanner sees it."""
        if len(line) > LINE_LENGTH_LIMIT:
            raise LimitReached(
                f"not fully checked: the hidden Unicode check stopped at {where}, "
                f"longer than the {LINE_LENGTH_LIMIT:,} characters this action scans in one line"
            )
        searches = sum(line.count(char) for char in _REPEATED_SEARCHES)
        self._work += len(line) * (1 + searches)
        if self._work > WORK_LIMIT:
            raise LimitReached(
                f"not fully checked: the hidden Unicode check stopped at {where}, "
                f"past the {WORK_LIMIT:,} steps this action allows for one pull request"
            )

    def found(self, count: int = 1) -> None:
        self._findings += count
        if self._findings > FINDINGS_LIMIT:
            raise LimitReached(
                f"not fully checked: stopped after {FINDINGS_LIMIT} findings, "
                "the most this action reports for one pull request"
            )


@dataclass(frozen=True)
class Finding:
    title: str
    message: str
    severity: str = "error"
    file: str | None = None
    line: int | None = None


def is_agent(address: str, policy: Policy) -> bool:
    """Whether an address is a coding agent's: built in or added by the
    caller, and not allowed by the caller. The built-in list sees the raw
    address, as it always has."""
    if any(selects(selector, address) for selector in policy.allowed_identities):
        return False
    if any(selects(selector, address) for selector in policy.additional_identities):
        return True
    return _IDENTITY_RE.fullmatch(address) is not None


def bot_trailers(text: str, policy: Policy) -> list[str]:
    """Lines of text that are Co-Authored-By trailers naming a coding agent."""
    trailers = []
    for line in text.split("\n"):
        match = _TRAILER_RE.fullmatch(line)
        if match and is_agent(match[1], policy):
            trailers.append(line)
    return trailers


def check_commit(commit: Commit, policy: Policy, budget: Budget | None = None) -> list[Finding]:
    """Behaviours 1 and 2: a trailer in the message, an agent as author or
    committer. Squash merging can carry these trailers into the merge commit.
    Each has its own mode, and one that is off leaves the other running."""
    budget = Budget() if budget is None else budget
    findings: list[Finding] = []
    try:
        if policy.co_author_trailers != "off":
            for line in bot_trailers(commit.message, policy):
                budget.found()
                findings.append(
                    Finding(
                        "Bot co-author trailer in a commit",
                        f"Commit {commit.sha} contains a bot Co-Authored-By line: "
                        f"{_quote(line.strip())}. Reword the commit with git rebase -i and "
                        "remove the line.",
                        _SEVERITIES[policy.co_author_trailers],
                    )
                )
        if policy.agent_identities != "off":
            for role, address in (
                ("authored", commit.author_email),
                ("committed", commit.committer_email),
            ):
                if is_agent(address, policy):
                    budget.found()
                    findings.append(
                        Finding(
                            "Bot commit author",
                            f"Commit {commit.sha} is {role} by a coding agent: {address}. "
                            "Rewrite it with git commit --amend --reset-author --no-edit, "
                            "which makes you its author and committer.",
                            _SEVERITIES[policy.agent_identities],
                        )
                    )
    except LimitReached as error:
        error.findings = findings + error.findings
        raise
    return findings


def check_body(body: str | None, policy: Policy, budget: Budget | None = None) -> list[Finding]:
    """Behaviour 3. The body can become the squash commit message, so a
    trailer there reaches the default branch like one in a commit. Reads the
    raw body: a Macroscope block lands in that commit message too."""
    budget = Budget() if budget is None else budget
    if policy.co_author_trailers == "off":
        return []
    findings: list[Finding] = []
    try:
        for line in bot_trailers(body or "", policy):
            budget.found()
            findings.append(
                Finding(
                    "Bot co-author trailer in the PR body",
                    f"The PR body contains a bot Co-Authored-By line: {_quote(line.strip())}. "
                    "Edit the PR description and remove it.",
                    _SEVERITIES[policy.co_author_trailers],
                )
            )
    except LimitReached as error:
        error.findings = findings + error.findings
        raise
    return findings


def check_attribution(body: str | None, login: str, policy: Policy) -> list[Finding]:
    """Behaviour 4. Off, and the exempt logins, produce nothing."""
    if policy.model_attribution == "off":
        return []
    if login in EXEMPT_FROM_ATTRIBUTION or login in policy.additional_attribution_exemptions:
        return []
    message = attribution.check(body, policy.model_attribution)
    if message is None:
        return []
    return [Finding("PR model attribution required", message)]


def check_unicode(
    text: str | None, where: str, policy: Policy, budget: Budget | None = None
) -> list[Finding]:
    """Behaviour 5 for the title, the raw body, or a commit message, line by
    line. None of them is a file, so a BOM is never at a file's start there.
    where names the text, such as "the PR body". Exclusions never apply."""
    budget = Budget() if budget is None else budget
    lines = (text or "").split("\n")
    findings: list[Finding] = []
    try:
        for number, line in enumerate(lines, 1):
            label = where if len(lines) == 1 else f"line {number} of {where}"
            findings += _unicode_line(line.rstrip("\r"), label, policy, budget)
    except LimitReached as error:
        error.findings = findings + error.findings
        raise
    return findings


def is_excluded(path: str, policy: Policy) -> bool:
    """Whether the caller excluded a path from the hidden Unicode check: an
    entry names it exactly, or ends in a slash and starts it. Literal and
    case-sensitive."""
    return any(
        path == entry or (entry.endswith("/") and path.startswith(entry))
        for entry in policy.exclude_paths
    )


def check_changed_file(
    changed: ChangedFile, policy: Policy, budget: Budget | None = None
) -> list[Finding]:
    """Behaviour 5 for the added lines of one file. A file that cannot be
    read fails whatever the mode and wherever it is, because unread is not
    clean. Only then do exclusions and modes apply."""
    budget = Budget() if budget is None else budget
    if changed.kind == gitdata.REJECTED_BINARY:
        budget.found()
        extensions = gitdata.BINARY_EXTENSIONS | policy.additional_binary_extensions
        formats = ", ".join(sorted(extensions))
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
        budget.found()
        return [
            Finding(
                _UNSCANNABLE,
                f"cannot decode {changed.path}: its name or its content is not "
                "valid UTF-8.",
                file=changed.path,
            )
        ]
    if is_excluded(changed.path, policy):
        return []
    findings: list[Finding] = []
    try:
        for number, line in changed.added:
            findings += _unicode_line(
                line,
                f"line {number} of {changed.path}",
                policy,
                budget,
                at_file_start=number == 1,
                file=changed.path,
                number=number,
            )
    except LimitReached as error:
        error.findings = findings + error.findings
        raise
    return findings


def _quote(text: str) -> str:
    """At most _QUOTE_LIMIT characters of text, for a message."""
    return text if len(text) <= _QUOTE_LIMIT else text[:_QUOTE_LIMIT] + "..."


def _names(code_points) -> str:
    """The distinct names of code points, the first three of them."""
    names = list(dict.fromkeys(hidden.describe(code_point) for code_point in code_points))
    return ", ".join(names[:3]) + (", ..." if len(names) > 3 else "")


def _mode(rule: str, policy: Policy) -> str | None:
    """The mode a hidden Unicode rule runs in, or None when it is off."""
    mode = {
        "invisible-char": policy.hidden_unicode,
        "private-use": policy.hidden_unicode,
        "unusual-space": policy.unicode_unusual_spaces,
        "homoglyph": policy.unicode_homoglyphs,
    }[rule]
    return None if mode == "off" else mode


def _unicode_line(
    line: str,
    where: str,
    policy: Policy,
    budget: Budget,
    *,
    at_file_start: bool = False,
    file: str | None = None,
    number: int | None = None,
) -> list[Finding]:
    """One finding per rule that a line breaks, as upstream groups them.
    With every rule off the line is not scanned and costs nothing."""
    modes = {rule: _mode(rule, policy) for rule in _UNICODE_TITLES}
    if not any(modes.values()):
        return []
    budget.spend(line, where)

    def finding(rule: str, message: str, column: int) -> Finding:
        budget.found()
        text = (
            f"{where[0].upper()}{where[1:]} {message}; the line reads: "
            f"{hidden.snippet(line, column)}"
        )
        return Finding(_UNICODE_TITLES[rule], text, _SEVERITIES[modes[rule]], file, number)

    # Only a rule that is on keeps its hits, and only those count toward
    # HIT_LIMIT, so a line full of what is off neither stops the run nor
    # takes the memory.
    hits: dict[str, list[tuple[int, int]]] = {}
    if any(modes[rule] for rule in _SCANNED_RULES):
        seen = 0
        for rule, column, code_point in hidden.scan(line, at_file_start=at_file_start):
            if modes[rule] is None:
                continue
            seen += 1
            if seen > HIT_LIMIT:
                raise LimitReached(
                    f"not fully checked: the hidden Unicode check stopped at {where}, which has "
                    f"more than the {HIT_LIMIT:,} suspicious characters this action collects "
                    "in one line"
                )
            hits.setdefault(rule, []).append((column, code_point))
    findings: list[Finding] = []
    try:
        for rule, found in hits.items():
            count = len(found)
            message = (
                f"has {count} {_NOUNS[rule]}{'' if count == 1 else 's'}: "
                f"{_names(code_point for _, code_point in found)}"
            )
            if rule == "invisible-char":
                # Text smuggled in tag characters, selectors, or zero-width bits.
                payload = hidden.hidden_text([code_point for _, code_point in found])
                if payload:
                    message += f" (hidden text: '{_quote(payload)}')"
            findings.append(finding(rule, message, found[0][0]))
        if modes["homoglyph"]:
            for column, word, odd in hidden.mixed_script_words(line):
                findings.append(
                    finding(
                        "homoglyph",
                        f"has the word '{_quote(word)}', which mixes Latin with {_names(odd)}",
                        column,
                    )
                )
    except LimitReached as error:
        error.findings = findings + error.findings
        raise
    return findings
