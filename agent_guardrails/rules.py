"""The checks themselves: bot trailers, bot identities, model attribution."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .attribution import _ERROR as ATTRIBUTION_ERROR
from .attribution import has_model_attribution
from .gitdata import Commit

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


@dataclass(frozen=True)
class Finding:
    title: str
    message: str
    severity: str = "error"


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
