"""Run every check and turn the result into an exit code."""

from __future__ import annotations

import os
import sys
from typing import Mapping, TextIO

from . import event, gitdata, rules
from .report import Reporter


def main(environ: Mapping[str, str] | None = None, stdout: TextIO | None = None) -> int:
    """Exit 1 if any check fails or cannot run to the end, else 0."""
    out = Reporter(sys.stdout if stdout is None else stdout)
    try:
        return _check(os.environ if environ is None else environ, out)
    except Exception as error:  # A crash must fail the check, never pass it.
        out.failure(f"internal error: {type(error).__name__}")
        return 1


def _check(environ: Mapping[str, str], out: Reporter) -> int:
    try:
        settings = event.load(environ)
    except event.InvalidEvent as error:
        out.failure(str(error))
        return 1

    findings: list[rules.Finding] = []
    try:
        pull_request = _inspect(settings, out, findings)
    except gitdata.GitError as error:
        return _stopped(out, findings, str(error))
    except rules.LimitReached as error:
        return _stopped(out, findings + error.findings, str(error))
    for finding in findings:
        out.finding(finding)

    errors = sum(finding.severity == "error" for finding in findings)
    warnings = len(findings) - errors
    commits = len(pull_request.commits)
    files = len(pull_request.files)
    out.log(
        f"checked {commits} commit{'' if commits == 1 else 's'}, "
        f"{files} changed file{'' if files == 1 else 's'}, and the PR title and body: "
        f"{errors} error{'' if errors == 1 else 's'}, "
        f"{warnings} warning{'' if warnings == 1 else 's'}"
    )
    return 1 if errors else 0


def _stopped(out: Reporter, findings: list[rules.Finding], reason: str) -> int:
    """What was found before the checks stopped, then why the rest was not
    checked. A pull request checked in part fails in either mode."""
    for finding in findings:
        out.finding(finding)
    out.failure(reason)
    return 1


def _inspect(
    settings: event.Settings, out: Reporter, findings: list[rules.Finding]
) -> gitdata.PullRequest:
    """Run every check, adding to findings as it goes. Raises GitError or
    LimitReached when the pull request cannot be checked to the end."""
    mode = settings.hidden_unicode
    budget = rules.Budget()
    findings += rules.check_body(settings.body, budget)
    findings += rules.check_attribution(
        settings.body, settings.login, settings.require_model_attribution
    )
    findings += rules.check_unicode(settings.title, "the PR title", mode, budget)
    # The raw body, Macroscope block included: all of it can reach the squash
    # commit message.
    findings += rules.check_unicode(settings.body, "the PR body", mode, budget)

    credential = gitdata.encode_credential(settings.token)
    out.mask(credential)
    pull_request = gitdata.fetch_pull_request(settings, credential, out.log)

    for commit in pull_request.commits:
        findings += rules.check_commit(commit, budget)
        findings += rules.check_unicode(
            commit.message, f"the message of commit {commit.sha}", mode, budget
        )
    for changed in pull_request.files:
        if changed.kind == gitdata.ALLOWED_BINARY:
            out.log(f"not scanned (binary): {changed.path}")
        elif changed.kind == gitdata.SUBMODULE:
            out.log(f"not scanned (submodule): {changed.path}")
        findings += rules.check_changed_file(changed, mode, budget)
    return pull_request
