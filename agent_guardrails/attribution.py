# Ported from scripts/check_pr_model_attribution.py in
# https://github.com/stickerdaniel/linkedin-mcp-server at commit
# 6e7344763bc3e8b99b95d5468b9edeea511be61e, where it was released under
# Apache-2.0. Daniel Sticker is its sole author and relicenses it here under
# the MIT License of this repository. The grammar and the Macroscope handling
# are unchanged; reading the event moved to event.py.
"""Require model attribution as the final non-empty PR body line."""

from __future__ import annotations

import re


_PREFIX = "Generated with "
_PLACEHOLDER_RE = re.compile(r"<[^>]*>|\[[^]]*]")
_RESERVED_MINIMAL_RE = re.compile(r"\b(?:for|in|and|via)\b")
# Macroscope writes its summary into the PR body after the author, between
# these two markers, and appends the pair at the end when it finds none. The
# block is the bot's text, not the author's, so it is dropped before the final
# line is read; an attribution inside it does not count. An author can write
# the markers too, which buys text after the attribution but never a missing
# one; the two cannot be told apart, and disclosure is what the check guards.
# Macroscope puts each marker on a line of its own. A marker quoted in the
# author's text or in Macroscope's summary sits inside backticks or a quote, so
# only whole-line markers count, and the match may not cross another start:
# neither kind of quote can stretch the block over the author's own lines.
_MACROSCOPE_START = (
    r"^" + re.escape("<!-- Macroscope's pull request summary starts here -->") + r"$"
)
_MACROSCOPE_BLOCK_RE = re.compile(
    _MACROSCOPE_START
    + r"(?:(?!"
    + _MACROSCOPE_START
    + r").)*?^"
    + re.escape("<!-- Macroscope's pull request summary ends here -->")
    + r"$",
    re.DOTALL | re.MULTILINE,
)
_ERROR = (
    "Model attribution is required as the final non-empty PR body line. "
    'Model-only is the minimum, with an optional final period: "Generated with '
    'Claude Opus 5" or "Generated with Claude Opus 5." Detailed attribution '
    "with the job, coding-agent harness, optional host, and final period is "
    'recommended: "Generated with Claude Opus 5 for implementation in Claude '
    'Code via T3 Code." Use commas or "/" between jobs for one model; "and" '
    "separates model/job pairs."
)


def _has_model_name(value: str) -> bool:
    return bool(value.strip()) and any(character.isalnum() for character in value)


def _has_valid_model_jobs(attribution: str) -> bool:
    for model_job in attribution.split(" and "):
        model, separator, job = model_job.partition(" for ")
        if not separator or not _has_model_name(model) or not job.strip():
            return False
    return True


def _has_valid_harness(value: str) -> bool:
    if value.startswith("via "):
        return False
    parts = value.split(" via ")
    return len(parts) <= 2 and all(part.strip() for part in parts)


def is_valid_attribution(line: str) -> bool:
    """Return whether a line follows the required attribution grammar."""
    if not line.startswith(_PREFIX) or _PLACEHOLDER_RE.search(line):
        return False

    has_final_period = line.endswith(".")
    attribution = line[len(_PREFIX) : -1] if has_final_period else line[len(_PREFIX) :]
    model_jobs, separator, harness = attribution.rpartition(" in ")
    if separator:
        return (
            has_final_period
            and _has_valid_harness(harness)
            and _has_valid_model_jobs(model_jobs)
        )
    return _has_model_name(attribution) and not _RESERVED_MINIMAL_RE.search(attribution)


def has_model_attribution(body: str | None) -> bool:
    """Check the final non-empty line of a pull request body."""
    if not body:
        return False
    # A body edited on the web arrives with CRLF, which `$` does not match.
    body = _MACROSCOPE_BLOCK_RE.sub("", body.replace("\r\n", "\n"))
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return bool(lines) and is_valid_attribution(lines[-1])
