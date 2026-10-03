# Ported from tests/test_pr_model_attribution.py in
# https://github.com/stickerdaniel/linkedin-mcp-server at commit
# 6e7344763bc3e8b99b95d5468b9edeea511be61e, where it was released under
# Apache-2.0. Daniel Sticker is its sole author and relicenses it here under
# the MIT License of this repository. The pytest parametrizations are subTest
# loops; every case is kept. The event-reading and workflow tests moved to
# test_event.py, test_entrypoint.py, and test_workflows.py.
from __future__ import annotations

import io
import unittest

from agent_guardrails import attribution, rules
from agent_guardrails.report import Reporter

from .support import ROOT, policy

_TEMPLATE = ROOT / ".github" / "pull_request_template.md"


class AttributionGrammarTests(unittest.TestCase):
    def test_accepts_supported_attribution_forms(self) -> None:
        for line in [
            "Generated with Claude Opus 5",
            "Generated with Claude Opus 5.",
            "Generated with GPT-5.6 Sol",
            "Generated with GPT-5.6 Sol.",
            "Generated with Salesforce xGen",
            "Generated with Gemini 3 Pro Preview.",
            "Generated with Command R",
            "Generated with Claude Sonnet 4.5 for implementation in Claude Code.",
            "Generated with Claude Opus 5 for implementation in Claude Code via T3 Code.",
            (
                "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 "
                "for review in Claude Code."
            ),
            "Generated with GPT-5.6 Sol for planning, implementation, review in T3 Code.",
            "Generated with GPT-5.6 Sol for implementation/testing in T3 Code.",
            "Generated with GPT-5.6 Sol for security review for CI in T3 Code.",
            (
                "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for "
                "review and Codex for testing in T3 Code."
            ),
        ]:
            with self.subTest(line=line):
                self.assertTrue(attribution.has_model_attribution(line))

    def test_accepts_trailing_blank_lines(self) -> None:
        body = (
            "## Summary\n\nDone.\n\n"
            "Generated with GPT-5.6 for implementation in Claude Code.\n\n"
        )

        self.assertTrue(attribution.has_model_attribution(body))

    def test_rejects_trailing_reserved_minimal_tokens(self) -> None:
        for keyword in ["for", "in", "and", "via"]:
            for period in ["", "."]:
                with self.subTest(keyword=keyword, period=period):
                    self.assertFalse(
                        attribution.has_model_attribution(
                            f"Generated with GPT-5.6 {keyword}{period}"
                        )
                    )

    def test_rejects_leading_reserved_minimal_tokens(self) -> None:
        for keyword in ["for", "in", "and", "via"]:
            with self.subTest(keyword=keyword):
                self.assertFalse(
                    attribution.has_model_attribution(f"Generated with {keyword} GPT-5.6")
                )

    def test_rejects_invalid_or_missing_attribution(self) -> None:
        for body in [
            "## Summary\n\nNo attribution here.",
            (
                "Generated with GPT-5.6 for implementation in Claude Code.\n\n"
                "A later non-empty line."
            ),
            "Generated with GPT-5.6 in Claude Code.",
            "Generated with GPT-5.6 for implementation.",
            "Generated with GPT-5.6 and Claude Opus 5.",
            "Generated with GPT-5.6 via T3 Code.",
            "Generated with GPT-5.6 for implementation",
            "Generated with GPT-5.6 in Claude Code",
            "Generated with GPT-5.6 and Claude Opus 5",
            "Generated with GPT-5.6 via T3 Code",
            "Generated with GPT-5.6 for implementation in via T3 Code.",
            "Generated with GPT-5.6 for implementation in Claude Code via .",
            (
                "Generated with GPT-5.6 for implementation in Claude Code via T3 Code "
                "via another wrapper."
            ),
            "Generated with",
            "Generated with .",
            "Generated with ..",
            "Generated with <model>",
            "Generated with <model>.",
            "Generated with [model]",
            "Generated with [model].",
            "Generated with . for implementation in Claude Code.",
            "Generated with GPT-5.6 for implementation and testing in T3 Code.",
            (
                "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for "
                "review and Codex in T3 Code."
            ),
            (
                "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for "
                "in Claude Code."
            ),
            (
                "Generated with Claude Sonnet 4.5 for implementation and  for review "
                "in Claude Code."
            ),
            (
                "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for "
                "review and Codex for in Claude Code."
            ),
            "Generated with <model> for <job> in <harness>.",
            "Generated with [model] for [job] in [harness].",
            "",
            None,
        ]:
            with self.subTest(body=body):
                self.assertFalse(attribution.has_model_attribution(body))


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
                    self.assertTrue(
                        attribution.has_model_attribution(body.replace("\n", newline))
                    )

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
                self.assertFalse(attribution.has_model_attribution(body))


class TemplateTests(unittest.TestCase):
    def test_template_ends_with_editable_attribution_placeholder(self) -> None:
        lines = [
            line.strip()
            for line in _TEMPLATE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        self.assertEqual(lines[-1], "Generated with [model] for [job] in [harness].")
        self.assertIn('"Generated with <model>" with or without a final period', lines[-2])
        self.assertIn("detailed form below, which requires its final period", lines[-2])
        self.assertIn("coding-agent runtime", lines[-2])
        self.assertIn("in Claude Code via T3 Code", lines[-2])
        self.assertFalse(attribution.is_valid_attribution(lines[-1]))


class AnnotationTests(unittest.TestCase):
    def test_failure_emits_actionable_github_annotation(self) -> None:
        stream = io.StringIO()
        findings = rules.check_attribution(
            "No attribution", "jane", policy({"require-model-attribution": "true"})
        )
        for finding in findings:
            Reporter(stream).finding(finding)

        output = stream.getvalue()
        self.assertTrue(output.startswith("::error title=PR model attribution required::"))
        self.assertIn("final non-empty PR body line", output)
        self.assertIn("Model-only is the minimum, with an optional final period", output)
        self.assertIn("Detailed attribution with the job, coding-agent harness", output)
        self.assertIn(
            '"Generated with Claude Opus 5" or "Generated with Claude Opus 5."', output
        )
        self.assertIn(
            "Generated with Claude Opus 5 for implementation in Claude Code via T3 Code.",
            output,
        )


if __name__ == "__main__":
    unittest.main()
