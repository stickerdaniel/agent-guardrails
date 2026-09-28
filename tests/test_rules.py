from __future__ import annotations

import time
import unittest

from agent_guardrails import rules
from agent_guardrails.gitdata import Commit

from .support import CLAUDE, HUMAN

_SHA = "a" * 40


def _commit(message: str = "Fix a bug\n", author: str = HUMAN, committer: str = HUMAN) -> Commit:
    return Commit(_SHA, author, committer, message)


def _titles(findings: list[rules.Finding]) -> list[str]:
    return [finding.title for finding in findings]


class CommitTrailerTests(unittest.TestCase):
    """Behaviour 1: a bot Co-Authored-By trailer in a commit message."""

    def test_capitalized_trailer_fails(self) -> None:
        findings = rules.check_commit(
            _commit("Fix a bug\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")
        )
        self.assertEqual(_titles(findings), ["Bot co-author trailer in a commit"])
        self.assertIn(_SHA, findings[0].message)

    def test_crlf_trailer_fails(self) -> None:
        findings = rules.check_commit(
            _commit("Fix a bug\r\n\r\nco-authored-by: Claude <noreply@anthropic.com>\r\n")
        )
        self.assertEqual(_titles(findings), ["Bot co-author trailer in a commit"])

    def test_indented_github_app_trailer_fails(self) -> None:
        findings = rules.check_commit(
            _commit(
                "Fix a bug\n\n  co-authored-by: Copilot "
                "<123+Copilot@users.noreply.github.com>\n"
            )
        )
        self.assertEqual(_titles(findings), ["Bot co-author trailer in a commit"])

    def test_human_co_author_passes(self) -> None:
        self.assertEqual(
            rules.check_commit(_commit("Fix\n\nCo-authored-by: Jane <jane@example.com>\n")),
            [],
        )

    def test_trailer_separated_by_other_whitespace_fails(self) -> None:
        findings = rules.check_commit(
            _commit("Fix\n\nCo-authored-by:\t \tClaude <noreply@anthropic.com>\n")
        )
        self.assertEqual(_titles(findings), ["Bot co-author trailer in a commit"])
        self.assertEqual(
            rules.check_commit(_commit("Fix\n\nCo-authored-by:<noreply@anthropic.com>\n")), []
        )

    def test_long_run_of_spaces_is_read_in_linear_time(self) -> None:
        start = time.monotonic()
        self.assertEqual(rules.bot_trailers("co-authored-by:" + " " * 300_000 + "<x"), [])
        # Backtracking over every split of the run took about half a minute.
        self.assertLess(time.monotonic() - start, 2)

    def test_address_in_prose_passes(self) -> None:
        self.assertEqual(
            rules.check_commit(
                _commit("Fix\n\nMail noreply@anthropic.com about <noreply@anthropic.com>.\n")
            ),
            [],
        )


class CommitIdentityTests(unittest.TestCase):
    """Behaviour 2: a coding agent as author or committer."""

    def test_bot_author_fails(self) -> None:
        findings = rules.check_commit(_commit(author=CLAUDE))
        self.assertEqual(_titles(findings), ["Bot commit author"])
        self.assertIn("authored by a coding agent: noreply@anthropic.com", findings[0].message)

    def test_bot_committer_alone_fails(self) -> None:
        findings = rules.check_commit(
            _commit(committer="456+google-labs-jules[bot]@users.noreply.github.com")
        )
        self.assertEqual(_titles(findings), ["Bot commit author"])
        self.assertIn("committed by a coding agent", findings[0].message)

    def test_lookalike_domain_passes(self) -> None:
        self.assertEqual(rules.check_commit(_commit(author="noreply@anthropic.com.evil")), [])

    def test_bot_name_in_a_human_address_passes(self) -> None:
        self.assertEqual(rules.check_commit(_commit(author="copilot-fan@example.com")), [])


class BodyTrailerTests(unittest.TestCase):
    """Behaviour 3: a bot trailer in the raw PR body."""

    def test_trailer_in_body_fails(self) -> None:
        findings = rules.check_body("Done.\n\nCo-authored-by: Claude <noreply@anthropic.com>")
        self.assertEqual(_titles(findings), ["Bot co-author trailer in the PR body"])

    def test_trailer_in_fenced_code_block_fails(self) -> None:
        body = "Done.\n\n```\nCo-authored-by: Claude <noreply@anthropic.com>\n```\n"
        self.assertEqual(
            _titles(rules.check_body(body)), ["Bot co-author trailer in the PR body"]
        )

    def test_trailer_inside_macroscope_block_fails(self) -> None:
        body = (
            "Done.\n\nGenerated with Claude Opus 5\n\n"
            "<!-- Macroscope's pull request summary starts here -->\n"
            "Co-authored-by: Claude <noreply@anthropic.com>\n"
            "<!-- Macroscope's pull request summary ends here -->\n"
        )
        self.assertEqual(
            _titles(rules.check_body(body)), ["Bot co-author trailer in the PR body"]
        )

    def test_prose_mention_passes(self) -> None:
        self.assertEqual(
            rules.check_body("Claude wrote this, see noreply@anthropic.com."), []
        )

    def test_human_co_author_passes(self) -> None:
        self.assertEqual(rules.check_body("Co-authored-by: Jane <jane@example.com>"), [])

    def test_null_body_passes(self) -> None:
        self.assertEqual(rules.check_body(None), [])


class AttributionRuleTests(unittest.TestCase):
    """Behaviour 4, per caller, with only the two dependency bots exempt."""

    _TEMPLATE_BODY = (
        "## Problem\n\nx\n\n"
        "<!-- CI accepts ... -->\nGenerated with [model] for [job] in [harness].\n"
    )

    def _fails(self, body: str | None, login: str = "jane") -> bool:
        return bool(rules.check_attribution(body, login, required=True))

    def test_unfilled_template_line_fails(self) -> None:
        self.assertTrue(self._fails(self._TEMPLATE_BODY))

    def test_null_body_fails(self) -> None:
        self.assertTrue(self._fails(None))

    def test_review_bot_is_not_exempt(self) -> None:
        self.assertTrue(self._fails("Summary without attribution", login="greptile-apps[bot]"))

    def test_model_only_attribution_passes(self) -> None:
        self.assertFalse(self._fails("Done.\n\nGenerated with Claude Opus 5"))

    def test_detailed_attribution_passes(self) -> None:
        self.assertFalse(
            self._fails("Done.\n\nGenerated with Claude Opus 5 for implementation in Claude Code.")
        )

    def test_attribution_followed_by_macroscope_block_passes(self) -> None:
        body = (
            "Done.\n\nGenerated with Claude Opus 5\n\n"
            "<!-- Macroscope's pull request summary starts here -->\n"
            "> summary\n"
            "<!-- Macroscope's pull request summary ends here -->\n"
        )
        self.assertFalse(self._fails(body))

    def test_not_required_passes_without_attribution(self) -> None:
        self.assertEqual(rules.check_attribution("No line", "jane", required=False), [])

    def test_dependency_bots_are_exempt(self) -> None:
        for login in ("renovate[bot]", "dependabot[bot]"):
            with self.subTest(login=login):
                self.assertFalse(self._fails("", login=login))


if __name__ == "__main__":
    unittest.main()
