# Ported from tests/test_pr_model_attribution.py in
# https://github.com/stickerdaniel/linkedin-mcp-server at commit
# 6e7344763bc3e8b99b95d5468b9edeea511be61e, where it was released under
# Apache-2.0. Daniel Sticker is its sole author and relicenses it here under
# the MIT License of this repository. The pytest parametrizations are subTest
# loops. The cases are kept and re-sorted to the level grammar. The
# event-reading and workflow tests moved to test_event.py, test_entrypoint.py,
# and test_workflows.py.
from __future__ import annotations

import io
import unittest

from post_no_bills import attribution, rules
from post_no_bills.report import Reporter

from .support import ROOT, policy

_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
_LEVELS = ("model", "job", "tool", "host")

_NO_LINE = "The PR body has no last line"
_NOT_PREFIX = 'The last line does not start with "Generated with"'
_PLACEHOLDER = "The last line still holds a placeholder"
_FIELD = (
    "A name contains a separator word (for, in, and, via), or a part is empty "
    "or contains no alphanumeric character"
)
_MIXED = "Some models have a job and others do not"
_PERIOD = "The last line needs a final period"
_NO_JOB = "A model has no job"
_NO_TOOL = "The last line names no tool"
_NO_HOST = "The last line names no host"

# The README table, in order. (body, passes at model, job, tool, host).
_README = (
    (None, False, False, False, False),
    ("Generated with [model] for [job] in [tool].", False, False, False, False),
    ("Generated with Claude Opus 5.5", True, False, False, False),
    ("Generated with Claude Opus 5.5 and GPT-6 Pro", True, False, False, False),
    ("Generated with Claude Opus 5.5 in Claude Code.", True, False, False, False),
    ("Generated with Claude Opus 5.5 for implementation.", True, True, False, False),
    (
        "Generated with Claude Opus 5.5 for implementation and GPT-6 Pro for review.",
        True,
        True,
        False,
        False,
    ),
    (
        "Generated with Claude Opus 5.5 for implementation, tests in Claude Code.",
        True,
        True,
        True,
        False,
    ),
    (
        "Generated with Claude Opus 5.5 for implementation and GPT-6 Pro for review in Claude Code.",
        True,
        True,
        True,
        False,
    ),
    (
        "Generated with Claude Opus 5.5 for implementation in Claude Code via T3 Code.",
        True,
        True,
        True,
        True,
    ),
    (
        "Generated with Claude Opus 5.5 for implementation and GPT-6 Pro for review "
        "in Claude Code via T3 Code.",
        True,
        True,
        True,
        True,
    ),
    (
        "Generated with Claude Opus 5.5 for implementation and GPT-6 Pro in Claude Code.",
        False,
        False,
        False,
        False,
    ),
    (
        "Generated with Claude Opus 5.5 for implementation in Claude Code and GPT-6 Pro.",
        False,
        False,
        False,
        False,
    ),
)

# Shown after "in this form: ", including the sentence period.
_FORMS = {
    "model": "Generated with <model>.",
    "job": "Generated with <model> for <job>.",
    "tool": "Generated with <model> for <job> in <tool>.",
    "host": "Generated with <model> for <job> in <tool> via <host>.",
}

# The old accept list, then former rejects the grammar now accepts.
# (line, passes job, passes tool, passes host). Every line passes model.
_ACCEPTED = (
    ("Generated with Claude Opus 5", False, False, False),
    ("Generated with Claude Opus 5.", False, False, False),
    ("Generated with GPT-5.6 Sol", False, False, False),
    ("Generated with GPT-5.6 Sol.", False, False, False),
    ("Generated with Salesforce xGen", False, False, False),
    ("Generated with Gemini 3 Pro Preview.", False, False, False),
    ("Generated with Command R", False, False, False),
    ("Generated with Claude Sonnet 4.5 for implementation in Claude Code.", True, True, False),
    ("Generated with Claude Opus 5 for implementation in Claude Code via T3 Code.", True, True, True),
    (
        "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for review in Claude Code.",
        True,
        True,
        False,
    ),
    ("Generated with GPT-5.6 Sol for planning, implementation, review in T3 Code.", True, True, False),
    ("Generated with GPT-5.6 Sol for implementation/testing in T3 Code.", True, True, False),
    ("Generated with GPT-5.6 Sol for security review for CI in T3 Code.", True, True, False),
    (
        "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for "
        "review and Codex for testing in T3 Code.",
        True,
        True,
        False,
    ),
    ("Generated with GPT-5.6 in Claude Code.", False, False, False),
    ("Generated with GPT-5.6 for implementation.", True, False, False),
    ("Generated with GPT-5.6 and Claude Opus 5.", False, False, False),
    ("Generated with GPT-5.6 and Claude Opus 5", False, False, False),
)

# The old reject list, minus the lines _ACCEPTED took, plus lines the stricter
# grammar newly rejects. Bodies as well as lines: check() reads the last line.
_REJECTED = (
    "## Summary\n\nNo attribution here.",
    "Generated with GPT-5.6 for implementation in Claude Code.\n\nA later non-empty line.",
    "Generated with GPT-5.6 via T3 Code.",
    "Generated with GPT-5.6 for implementation",
    "Generated with GPT-5.6 in Claude Code",
    "Generated with GPT-5.6 via T3 Code",
    "Generated with GPT-5.6 for implementation in via T3 Code.",
    "Generated with GPT-5.6 for implementation in Claude Code via .",
    "Generated with GPT-5.6 for implementation in Claude Code via T3 Code via another wrapper.",
    "Generated with",
    "Generated with .",
    "Generated with ..",
    "Generated with <model>",
    "Generated with <model>.",
    "Generated with [model]",
    "Generated with [model].",
    "Generated with . for implementation in Claude Code.",
    "Generated with GPT-5.6 for implementation and testing in T3 Code.",
    "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for review and Codex in T3 Code.",
    "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for in Claude Code.",
    "Generated with Claude Sonnet 4.5 for implementation and  for review in Claude Code.",
    "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for review and Codex for in Claude Code.",
    "Generated with <model> for <job> in <harness>.",
    "Generated with [model] for [job] in [harness].",
    "Generated with GPT-5.6 and-Research.",
    "Generated with Claude Opus 5.5 for / in Claude Code.",
    "Generated with Claude Opus 5.5 for implementation in -.",
    "Generated with Claude Opus 5.5 for implementation in Claude Code via ---.",
    "Generated with Claude Opus 5.5 for implementation in Code in a Box.",
    "",
    None,
)


def _passes(body: str | None, level: str) -> bool:
    return attribution.check(body, level) is None


class ReadmeTableTests(unittest.TestCase):
    def test_every_row_at_every_level(self) -> None:
        self.assertEqual(len(_README), 13)
        for body, *expected in _README:
            for level, ok in zip(_LEVELS, expected, strict=True):
                with self.subTest(body=body, level=level):
                    self.assertEqual(_passes(body, level), ok)


class AttributionGrammarTests(unittest.TestCase):
    def test_accepted_lines_pass_the_levels_they_satisfy(self) -> None:
        for line, job, tool, host in _ACCEPTED:
            expected = {"model": True, "job": job, "tool": tool, "host": host}
            for level, ok in expected.items():
                with self.subTest(line=line, level=level):
                    self.assertEqual(_passes(line, level), ok)

    def test_rejected_lines_fail_at_every_level(self) -> None:
        for body in _REJECTED:
            for level in _LEVELS:
                with self.subTest(body=body, level=level):
                    self.assertFalse(_passes(body, level))

    def test_dangling_separators_are_rejected(self) -> None:
        for keyword in ("for", "in", "and", "via"):
            for line in (
                f"Generated with GPT-5.6 {keyword}",
                f"Generated with GPT-5.6 {keyword}.",
                f"Generated with {keyword} GPT-5.6",
            ):
                for level in _LEVELS:
                    with self.subTest(line=line, level=level):
                        self.assertFalse(_passes(line, level))

    def test_accepts_trailing_blank_lines(self) -> None:
        body = "## Summary\n\nDone.\n\nGenerated with GPT-5.6 for implementation in Claude Code.\n\n"
        self.assertTrue(_passes(body, "tool"))
        self.assertFalse(_passes(body, "host"))

    def test_parse_reads_the_fields(self) -> None:
        self.assertEqual(
            attribution.parse(
                "Generated with Claude Opus 5.5 for security review for CI in Claude Code via T3 Code."
            ),
            ((("Claude Opus 5.5", "security review for CI"),), "Claude Code", "T3 Code"),
        )
        self.assertEqual(
            attribution.parse("Generated with Claude Opus 5.5 and GPT-6 Pro"),
            ((("Claude Opus 5.5", None), ("GPT-6 Pro", None)), None, None),
        )
        self.assertEqual(
            attribution.parse(
                "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for "
                "review and Codex for testing in T3 Code."
            ),
            (
                (
                    ("Claude Sonnet 4.5", "implementation"),
                    ("GPT-5.6", "review"),
                    ("Codex", "testing"),
                ),
                "T3 Code",
                None,
            ),
        )

    def test_documented_limits(self) -> None:
        self.assertEqual(
            attribution.parse("Generated with Claude Opus 5.5 for work in progress."),
            ((("Claude Opus 5.5", "work"),), "progress", None),
        )
        self.assertTrue(_passes("Generated with Claude Opus 5.5 for work in progress.", "tool"))
        self.assertEqual(
            attribution.parse("Generated with Research and Development."),
            ((("Research", None), ("Development", None)), None, None),
        )
        self.assertEqual(
            attribution.parse(
                "Generated with Claude Opus 5.5 for implementation in Claude Code & GPT-6 Pro."
            ),
            ((("Claude Opus 5.5", "implementation"),), "Claude Code & GPT-6 Pro", None),
        )
        self.assertEqual(
            attribution.parse(
                "Generated with Claude Opus 5.5 for implementation in Claude Code AND GPT-6 Pro."
            ),
            ((("Claude Opus 5.5", "implementation"),), "Claude Code AND GPT-6 Pro", None),
        )

    def test_parse_returns_a_line_reason(self) -> None:
        self.assertEqual(attribution.parse("Done."), _NOT_PREFIX)
        self.assertEqual(attribution.parse("Generated with [model]."), _PLACEHOLDER)
        self.assertEqual(attribution.parse("Generated with GPT-5.6 and"), _FIELD)
        self.assertEqual(attribution.parse("Generated with and GPT-5.6"), _FIELD)
        self.assertEqual(attribution.parse("Generated with GPT-5.6 and."), _FIELD)
        self.assertEqual(
            attribution.parse("Generated with Claude Opus 5.5 for implementation and GPT-6 Pro."),
            _MIXED,
        )
        self.assertEqual(attribution.parse("Generated with Claude Opus 5.5 for implementation"), _PERIOD)


class ReasonTests(unittest.TestCase):
    def test_each_reason_leads_the_message(self) -> None:
        cases = (
            ("model", None, _NO_LINE),
            ("model", "Done.", _NOT_PREFIX),
            ("model", "Generated with [model].", _PLACEHOLDER),
            ("model", "Generated with GPT-5.6 and", _FIELD),
            ("model", "Generated with Claude Opus 5.5 for implementation and GPT-6 Pro.", _MIXED),
            ("model", "Generated with Claude Opus 5.5 for implementation", _PERIOD),
            ("job", "Generated with Claude Opus 5.5", _NO_JOB),
            ("tool", "Generated with Claude Opus 5.5 for implementation.", _NO_TOOL),
            ("host", "Generated with Claude Opus 5.5 for implementation in Claude Code.", _NO_HOST),
        )
        for level, body, reason in cases:
            with self.subTest(reason=reason):
                message = attribution.check(body, level)
                self.assertTrue(message.startswith(reason + "."), message)
                self.assertNotIn("\n", message)
                self.assertNotIn("..", message)

    def test_an_earlier_reason_wins(self) -> None:
        cases = (
            ("model", "\n\n", _NO_LINE, _NOT_PREFIX),
            ("model", "See [model]", _NOT_PREFIX, _PLACEHOLDER),
            (
                "model",
                "Generated with [model] for implementation and GPT-6 Pro in Code in a Box.",
                _PLACEHOLDER,
                _FIELD,
            ),
            (
                "model",
                "Generated with Claude Opus 5.5 for implementation and for review.",
                _FIELD,
                _MIXED,
            ),
            (
                "model",
                "Generated with Claude Opus 5.5 for implementation and GPT-6 Pro in Claude Code",
                _MIXED,
                _PERIOD,
            ),
            (
                "job",
                "Generated with Claude Opus 5.5 for implementation and GPT-6 Pro in Claude Code.",
                _MIXED,
                _NO_JOB,
            ),
            ("job", "Generated with Claude Opus 5.5 in Claude Code", _PERIOD, _NO_JOB),
            ("tool", "Generated with Claude Opus 5.5", _NO_JOB, _NO_TOOL),
            ("host", "Generated with Claude Opus 5.5 for implementation.", _NO_TOOL, _NO_HOST),
        )
        for level, body, earlier, later in cases:
            with self.subTest(body=body, level=level):
                message = attribution.check(body, level)
                self.assertTrue(message.startswith(earlier + "."), message)
                self.assertFalse(message.startswith(later + "."), message)

    def test_each_message_example_passes_its_level(self) -> None:
        for level in _LEVELS:
            message = attribution.check(None, level)
            with self.subTest(level=level):
                self.assertNotIn("\n", message)
                self.assertNotIn("..", message)
                form = message.split("in this form: ", 1)[1].split(" Example: ", 1)[0]
                example, several = message.split("Example: ", 1)[1].split(" Several models: ", 1)
                self.assertEqual(form, _FORMS[level])
                self.assertIsNone(attribution.check(example, level))
                self.assertIsNone(attribution.check(several, level))

    def test_the_message_does_not_quote_the_body(self) -> None:
        body = "zebra-marker is not an attribution"
        for level in _LEVELS:
            with self.subTest(level=level):
                self.assertNotIn("zebra-marker", attribution.check(body, level))


#: Macroscope's block as it lands in a PR body, trimmed from linkedin-mcp-server#1132.
_MACROSCOPE = (
    "<!-- Macroscope's pull request summary starts here -->\n"
    "<!-- Macroscope will only edit the content between these invisible "
    "markers, and the markers themselves will not be visible in the GitHub "
    "rendered markdown. -->\n"
    "> [!NOTE]\n"
    "> ### Exempt Renovate PRs from the changelog fragment requirement\n"
    "> <!-- Macroscope's review summary starts here -->\n"
    "> <sup>Macroscope summarized de6f451.</sup>\n"
    "> <!-- Macroscope's review summary ends here -->\n"
    ">\n"
    "<!-- Macroscope's pull request summary ends here -->\n"
)
_LINE = "Generated with Claude Opus 5.5"


class MacroscopeTests(unittest.TestCase):
    def test_ignores_the_macroscope_summary(self) -> None:
        bodies = {
            "summary-after": f"## Summary\n\nDone.\n\n{_LINE}\n\n{_MACROSCOPE}",
            "summary-before": f"## Summary\n\nDone.\n\n{_MACROSCOPE}\n{_LINE}\n",
            "marker-quoted-above": (
                "Macroscope opens with `<!-- Macroscope's pull request summary "
                f"starts here -->`.\n\n{_LINE}\n\n{_MACROSCOPE}"
            ),
            "marker-line-in-code-block": (
                "The block starts with:\n\n```\n"
                "<!-- Macroscope's pull request summary starts here -->\n```\n\n"
                f"{_LINE}\n\n{_MACROSCOPE}"
            ),
            "marker-quoted-inside": (
                f"## Summary\n\nDone.\n\n{_LINE}\n\n"
                + _MACROSCOPE.replace(
                    "> [!NOTE]\n",
                    "> [!NOTE]\n> It skips `<!-- Macroscope's pull request summary "
                    "starts here -->` blocks.\n",
                )
            ),
        }
        for name, body in bodies.items():
            for newline_name, newline in (("lf", "\n"), ("crlf", "\r\n")):
                with self.subTest(body=name, newline=newline_name):
                    self.assertTrue(_passes(body.replace("\n", newline), "model"))

    def test_the_macroscope_summary_carries_no_attribution(self) -> None:
        bodies = {
            "only-inside-the-summary": (
                f"## Summary\n\nDone.\n\n{_MACROSCOPE.replace('> [!NOTE]', _LINE)}"
            ),
            # Without its end marker the text is not Macroscope's block.
            "unterminated": (
                f"{_LINE}\n\n<!-- Macroscope's pull request summary starts here -->\nx"
            ),
        }
        for name, body in bodies.items():
            with self.subTest(body=name):
                self.assertFalse(_passes(body, "model"))


class TemplateTests(unittest.TestCase):
    def test_template_ends_with_editable_attribution_placeholder(self) -> None:
        lines = [
            line.strip()
            for line in _TEMPLATE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        self.assertEqual(lines[-1], "Generated with [model] for [job] in [tool].")
        self.assertIn('"Generated with <model>" with or without a final period', lines[-2])
        self.assertIn("detailed form below, which requires its final period", lines[-2])
        self.assertIn("coding-agent runtime", lines[-2])
        self.assertIn("in Claude Code via T3 Code", lines[-2])
        self.assertEqual(attribution.parse(lines[-1]), _PLACEHOLDER)


class AnnotationTests(unittest.TestCase):
    def test_failure_emits_actionable_github_annotation(self) -> None:
        stream = io.StringIO()
        findings = rules.check_attribution("No attribution", "jane", policy({"model-attribution": "model"}))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].title, "PR model attribution required")
        self.assertEqual(findings[0].message, attribution.check("No attribution", "model"))
        self.assertNotIn("\n", findings[0].message)
        for finding in findings:
            Reporter(stream).finding(finding)

        output = stream.getvalue()
        self.assertTrue(output.startswith("::error title=PR model attribution required::"))
        self.assertNotIn("%0A", output.splitlines()[0])
        self.assertIn(_NOT_PREFIX, output)
        self.assertIn("Generated with <model>", output)
        self.assertIn("Generated with Claude Opus 5.5", output)


if __name__ == "__main__":
    unittest.main()
