# Ported from scripts/check_pr_model_attribution.py in
# https://github.com/stickerdaniel/linkedin-mcp-server at commit
# 6e7344763bc3e8b99b95d5468b9edeea511be61e, where it was released under
# Apache-2.0. Daniel Sticker is its sole author and relicenses it here under
# the MIT License of this repository. The Macroscope handling is unchanged;
# reading the event moved to event.py.
"""Require model attribution as the final non-empty PR body line."""

from __future__ import annotations

import re


_PREFIX = "Generated with "
_PLACEHOLDER_RE = re.compile(r"<[^>]*>|\[[^]]*]")
# Lowercase whole words. Splitting uses the spaced form (` for `, ` in `,
# ` and `, ` via `). A name that merely contains one is still rejected: the
# parser does not guess which occurrence was meant as a separator.
_SEPARATOR_WORD = re.compile(r"\b(?:for|in|and|via)\b")
# A job may contain `for` ("security review for CI"). The other three stay
# reserved there, or a job could not be told from a tool or a further model.
_JOB_WORD = re.compile(r"\b(?:in|and|via)\b")
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

_NOT_PREFIX = 'The last line does not start with "Generated with"'
_PLACEHOLDER = "The last line still holds a placeholder"
_FIELD = (
    "A name contains a separator word (for, in, and, via), or a part is empty "
    "or contains no alphanumeric character"
)
_MIXED = "Some models have a job and others do not"
_PERIOD = "The last line needs a final period"
_NO_LINE = "The PR body has no last line"
_NO_JOB = "A model has no job"
_NO_TOOL = "The last line names no tool"
_NO_HOST = "The last line names no host"

# What the annotation tells the author to write. The model form has no period;
# the others include the period the grammar requires.
_FORMS = {
    "model": "Generated with <model>",
    "job": "Generated with <model> for <job>.",
    "tool": "Generated with <model> for <job> in <tool>.",
    "host": "Generated with <model> for <job> in <tool> via <host>.",
}
_EXAMPLES = {
    "model": "Generated with Claude Opus 5.5",
    "job": "Generated with Claude Opus 5.5 for implementation.",
    "tool": "Generated with Claude Opus 5.5 for implementation in Claude Code.",
    "host": "Generated with Claude Opus 5.5 for implementation in Claude Code via T3 Code.",
}
_SEVERAL = {
    "model": "Generated with Claude Opus 5.5 and GPT-6 Pro",
    "job": "Generated with Claude Opus 5.5 for implementation and GPT-6 Pro for review.",
    "tool": (
        "Generated with Claude Opus 5.5 for implementation and GPT-6 Pro for review "
        "in Claude Code."
    ),
    "host": (
        "Generated with Claude Opus 5.5 for implementation and GPT-6 Pro for review "
        "in Claude Code via T3 Code."
    ),
}

# (model, jobs). jobs is None when the entry has no " for ".
_Entry = tuple[str, str | None]
# (entries, tool, host). tool and host are None when the line names neither.
Parsed = tuple[tuple[_Entry, ...], str | None, str | None]


def _bad_field(value: str, *, job: bool = False) -> bool:
    if not any(character.isalnum() for character in value):
        return True
    return (_JOB_WORD if job else _SEPARATOR_WORD).search(value) is not None


def _sentence(text: str) -> str:
    """text as a sentence. A form that already ends with its period keeps it."""
    return text if text.endswith(".") else text + "."


def _message(reason: str, level: str) -> str:
    return (
        f"{reason}. End the PR body with a line in this form: {_sentence(_FORMS[level])} "
        f"Example: {_sentence(_EXAMPLES[level])} "
        f"Several models: {_sentence(_SEVERAL[level])}"
    )


def parse(line: str) -> Parsed | str:
    """The line's fields, or the first reason it is not a valid attribution.

    Level requirements are not decided here: a thin line is valid, and check()
    says when a level asks for more.
    """
    if not line.startswith(_PREFIX):
        return _NOT_PREFIX
    if _PLACEHOLDER_RE.search(line):
        return _PLACEHOLDER

    period = line.endswith(".")
    rest = line[len(_PREFIX) : -1] if period else line[len(_PREFIX) :]
    # A second ` in ` is a separator inside a name, not "the tool starts at
    # the last one". ` via ` only introduces a host after ` in `.
    if rest.count(" in ") > 1 or (" via " in rest and " in " not in rest):
        return _FIELD

    entries_text, in_at, suffix = rest.partition(" in ")
    tool = None
    host = None
    if in_at:
        if suffix.count(" via ") > 1:
            return _FIELD
        tool_text, via_at, host_text = suffix.partition(" via ")
        tool = tool_text.strip()
        if _bad_field(tool):
            return _FIELD
        if via_at:
            host = host_text.strip()
            if _bad_field(host):
                return _FIELD
    else:
        entries_text = rest

    entries = []
    for entry in entries_text.split(" and "):
        model_text, for_at, jobs_text = entry.partition(" for ")
        model = model_text.strip()
        if _bad_field(model):
            return _FIELD
        if for_at:
            jobs = jobs_text.strip()
            if _bad_field(jobs, job=True):
                return _FIELD
        else:
            jobs = None
        entries.append((model, jobs))

    has_jobs = [jobs is not None for _, jobs in entries]
    if any(has_jobs) and not all(has_jobs):
        return _MIXED
    # The period is optional only for the job-less, tool-less form.
    if (any(has_jobs) or tool is not None) and not period:
        return _PERIOD
    return (tuple(entries), tool, host)


def _last_line(body: str | None) -> str | None:
    if not body:
        return None
    # A body edited on the web arrives with CRLF, which `$` does not match.
    text = _MACROSCOPE_BLOCK_RE.sub("", body.replace("\r\n", "\n"))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else None


def check(body: str | None, level: str) -> str | None:
    """The one-line failure for level, or None when the body passes it.

    `off` checks nothing. The message names the first problem and never quotes
    the body. A valid line that is too thin for the level fails after the
    grammar, so a broken line is not reported as merely missing a host.
    """
    if level == "off":
        return None
    line = _last_line(body)
    if line is None:
        return _message(_NO_LINE, level)
    parsed = parse(line)
    if isinstance(parsed, str):
        return _message(parsed, level)
    entries, tool, host = parsed
    if level in ("job", "tool", "host") and any(jobs is None for _, jobs in entries):
        return _message(_NO_JOB, level)
    if level in ("tool", "host") and tool is None:
        return _message(_NO_TOOL, level)
    if level == "host" and host is None:
        return _message(_NO_HOST, level)
    return None
