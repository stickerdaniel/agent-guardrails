from __future__ import annotations

import io
import unittest

from agent_guardrails.report import Reporter

from .support import foreign_commands

_SPOOF = "a%b\n::error::spoof"


class ReportTests(unittest.TestCase):
    def _annotate(self, **kwargs: object) -> str:
        stream = io.StringIO()
        Reporter(stream).annotate("error", **kwargs)
        return stream.getvalue()

    def test_pull_request_text_cannot_start_a_workflow_command(self) -> None:
        lines = self._annotate(title=_SPOOF, message=_SPOOF).splitlines()
        self.assertEqual(len(lines), 2)
        annotation, plain = lines
        self.assertIn("<U+000A>", annotation)
        self.assertIn("%25", annotation)
        self.assertTrue(annotation.startswith("::error title=a%25b<U+000A>%3A%3Aerror%3A%3Aspoof::"))
        self.assertTrue(plain.startswith("agent-guardrails: error: "))

    def test_plain_title_passes_unchanged(self) -> None:
        output = self._annotate(title="Bot commit author", message="Commit abc is authored by x.")
        self.assertEqual(
            output.splitlines()[0], "::error title=Bot commit author::Commit abc is authored by x."
        )

    def test_properties_escape_commas_and_colons(self) -> None:
        output = self._annotate(title="t", message="m", file="docs/a,b:c.md", line=3)
        self.assertEqual(output.splitlines()[0], "::error file=docs/a%2Cb%3Ac.md,line=3,title=t::m")

    def test_plain_log_line_renders_controls(self) -> None:
        stream = io.StringIO()
        Reporter(stream).log("::warning::x\r\n")
        self.assertEqual(stream.getvalue(), "agent-guardrails: ::warning::x<U+000D><U+000A>\n")

    def test_legacy_command_opener_is_shown_not_run(self) -> None:
        stream = io.StringIO()
        reporter = Reporter(stream)
        reporter.annotate(
            "warning", "##[warning]t", "m ###[error]m", file="a##[debug]b.md", line=2
        )
        reporter.log("##[set-output name=x]y")
        output = stream.getvalue()
        self.assertEqual(foreign_commands(output), [])
        self.assertEqual(len(output.splitlines()), 3)
        self.assertIn("title=<U+0023>#[warning]t::m #<U+0023>#[error]m", output)
        self.assertIn("agent-guardrails: <U+0023>#[set-output name=x]y\n", output)


if __name__ == "__main__":
    unittest.main()
