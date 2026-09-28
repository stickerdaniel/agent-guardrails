"""The Python contestants: the frozen baseline's own rules, hidden, report and
gitdata._classify, driven from an AGCAP1 capture.

    python3 -I -B py_driver.py <baseline-root> <faithful|tuned> drive <capture>

baseline-root is the agent_guardrails package root at the pinned baseline
commit; bench/measure.py verifies its hashes once before any timing, so this
driver does not repeat that work inside the timed process. "tuned" swaps in
hidden_tuned.py, a copy of the vendored hidden.py with the minimal
algorithmic changes listed there; rules, report and gitdata stay the frozen
baseline. Imports stay minimal on purpose, since process startup is measured.

Derived from the Gate X benchmark prototype's harness/py_driver.py; only the
drive command is kept.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def drive(cap, gitdata, report, rules) -> int:
    """main._check from settings.load onwards, for the hidden Unicode check
    only: the trailer, identity and attribution checks are not ported."""
    for name, value in cap.limits.items():
        setattr(rules, capture_format.LIMIT_NAMES[name], value)
    out = report.Reporter(sys.stdout)
    mode = cap.mode
    budget = rules.Budget()
    findings: list = []
    title = cap.title.decode("utf-8", "replace")
    body = cap.body.decode("utf-8", "replace")
    commits = [(sha, message.decode("utf-8", "replace")) for sha, message in cap.commits]
    try:
        findings += rules.check_unicode(title, "the PR title", mode, budget)
        findings += rules.check_unicode(body, "the PR body", mode, budget)
        for sha, message in commits:
            findings += rules.check_unicode(message, f"the message of commit {sha}", mode, budget)
        for captured in cap.files:
            # gitdata._classify itself, fed the section git would have produced.
            section = gitdata._Section(
                b"", binary=captured.kind == "binary", in_hunks=True, added=list(captured.lines)
            )
            new_mode = gitdata._SUBMODULE_MODE if captured.kind == "submodule" else "100644"
            changed = gitdata._classify(captured.path, "A", "000000", new_mode, section, lambda: section)
            if changed.kind == gitdata.ALLOWED_BINARY:
                out.log(f"not scanned (binary): {changed.path}")
            elif changed.kind == gitdata.SUBMODULE:
                out.log(f"not scanned (submodule): {changed.path}")
            findings += rules.check_changed_file(changed, mode, budget)
    except rules.LimitReached as error:
        for finding in findings + error.findings:
            out.finding(finding)
        out.failure(str(error))
        return 1
    for finding in findings:
        out.finding(finding)
    errors = sum(finding.severity == "error" for finding in findings)
    warnings = len(findings) - errors
    ncommits = len(commits)
    nfiles = len(cap.files)
    out.log(
        f"checked {ncommits} commit{'' if ncommits == 1 else 's'}, "
        f"{nfiles} changed file{'' if nfiles == 1 else 's'}, and the PR title and body: "
        f"{errors} error{'' if errors == 1 else 's'}, "
        f"{warnings} warning{'' if warnings == 1 else 's'}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    root, impl, command, path = sys.argv[1:5]
    if command != "drive" or impl not in ("faithful", "tuned"):
        raise SystemExit("usage: py_driver.py <baseline-root> <faithful|tuned> drive <capture>")
    # -I drops the script directory from sys.path, as run.py notes for the action.
    sys.path.insert(0, root)
    sys.path.insert(1, HERE)
    from agent_guardrails import gitdata, report, rules

    import capture as capture_format

    if impl == "tuned":
        import hidden_tuned

        # rules calls hidden.scan, hidden.mixed_script_words, hidden.hidden_text,
        # hidden.snippet and hidden.describe through this module attribute.
        rules.hidden = hidden_tuned
    with open(path, "rb") as handle:
        cap = capture_format.load(handle.read())
    raise SystemExit(drive(cap, gitdata, report, rules))
