from __future__ import annotations

import itertools
import re
import time
import unittest

from agent_guardrails import rules
from agent_guardrails.gitdata import Commit

from .support import CLAUDE, HUMAN, policy

_SHA = "a" * 40
_DEFAULT = policy()
# v1's trailer pattern, with the built-in addresses between the brackets.
_V1_TRAILER = re.compile(r"\s*co-authored-by:\s.*<" + rules.AI_IDENTITY + r">\s*", re.IGNORECASE)
_TRAILER = "Bot co-author trailer in a commit"
_AUTHOR = "Bot commit author"


def _commit(message: str = "Fix a bug\n", author: str = HUMAN, committer: str = HUMAN) -> Commit:
    return Commit(_SHA, author, committer, message)


def _titles(findings: list[rules.Finding]) -> list[str]:
    return [finding.title for finding in findings]


class CommitTrailerTests(unittest.TestCase):
    """Behaviour 1: a bot Co-Authored-By trailer in a commit message."""

    def test_capitalized_trailer_fails(self) -> None:
        findings = rules.check_commit(
            _commit("Fix a bug\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"), _DEFAULT
        )
        self.assertEqual(_titles(findings), ["Bot co-author trailer in a commit"])
        self.assertIn(_SHA, findings[0].message)

    def test_crlf_trailer_fails(self) -> None:
        findings = rules.check_commit(
            _commit("Fix a bug\r\n\r\nco-authored-by: Claude <noreply@anthropic.com>\r\n"), _DEFAULT
        )
        self.assertEqual(_titles(findings), ["Bot co-author trailer in a commit"])

    def test_indented_github_app_trailer_fails(self) -> None:
        findings = rules.check_commit(
            _commit(
                "Fix a bug\n\n  co-authored-by: Copilot "
                "<123+Copilot@users.noreply.github.com>\n"
            ),
            _DEFAULT,
        )
        self.assertEqual(_titles(findings), ["Bot co-author trailer in a commit"])

    def test_human_co_author_passes(self) -> None:
        self.assertEqual(
            rules.check_commit(_commit("Fix\n\nCo-authored-by: Jane <jane@example.com>\n"), _DEFAULT),
            [],
        )

    def test_trailer_separated_by_other_whitespace_fails(self) -> None:
        findings = rules.check_commit(
            _commit("Fix\n\nCo-authored-by:\t \tClaude <noreply@anthropic.com>\n"), _DEFAULT
        )
        self.assertEqual(_titles(findings), ["Bot co-author trailer in a commit"])
        self.assertEqual(
            rules.check_commit(_commit("Fix\n\nCo-authored-by:<noreply@anthropic.com>\n"), _DEFAULT), []
        )

    def test_long_run_of_spaces_is_read_in_linear_time(self) -> None:
        start = time.monotonic()
        self.assertEqual(rules.bot_trailers("co-authored-by:" + " " * 300_000 + "<x", _DEFAULT), [])
        # Backtracking over every split of the run took about half a minute.
        self.assertLess(time.monotonic() - start, 2)

    def test_address_in_prose_passes(self) -> None:
        self.assertEqual(
            rules.check_commit(
                _commit("Fix\n\nMail noreply@anthropic.com about <noreply@anthropic.com>.\n"), _DEFAULT
            ),
            [],
        )


class CommitIdentityTests(unittest.TestCase):
    """Behaviour 2: a coding agent as author or committer."""

    def test_bot_author_fails(self) -> None:
        findings = rules.check_commit(_commit(author=CLAUDE), _DEFAULT)
        self.assertEqual(_titles(findings), ["Bot commit author"])
        self.assertIn("authored by a coding agent: noreply@anthropic.com", findings[0].message)

    def test_bot_committer_alone_fails(self) -> None:
        findings = rules.check_commit(
            _commit(committer="456+google-labs-jules[bot]@users.noreply.github.com"), _DEFAULT
        )
        self.assertEqual(_titles(findings), ["Bot commit author"])
        self.assertIn("committed by a coding agent", findings[0].message)

    def test_lookalike_domain_passes(self) -> None:
        self.assertEqual(rules.check_commit(_commit(author="noreply@anthropic.com.evil"), _DEFAULT), [])

    def test_bot_name_in_a_human_address_passes(self) -> None:
        self.assertEqual(rules.check_commit(_commit(author="copilot-fan@example.com"), _DEFAULT), [])


class BodyTrailerTests(unittest.TestCase):
    """Behaviour 3: a bot trailer in the raw PR body."""

    def test_trailer_in_body_fails(self) -> None:
        findings = rules.check_body("Done.\n\nCo-authored-by: Claude <noreply@anthropic.com>", _DEFAULT)
        self.assertEqual(_titles(findings), ["Bot co-author trailer in the PR body"])

    def test_trailer_in_fenced_code_block_fails(self) -> None:
        body = "Done.\n\n```\nCo-authored-by: Claude <noreply@anthropic.com>\n```\n"
        self.assertEqual(
            _titles(rules.check_body(body, _DEFAULT)), ["Bot co-author trailer in the PR body"]
        )

    def test_trailer_inside_macroscope_block_fails(self) -> None:
        body = (
            "Done.\n\nGenerated with Claude Opus 5\n\n"
            "<!-- Macroscope's pull request summary starts here -->\n"
            "Co-authored-by: Claude <noreply@anthropic.com>\n"
            "<!-- Macroscope's pull request summary ends here -->\n"
        )
        self.assertEqual(
            _titles(rules.check_body(body, _DEFAULT)), ["Bot co-author trailer in the PR body"]
        )

    def test_prose_mention_passes(self) -> None:
        self.assertEqual(
            rules.check_body("Claude wrote this, see noreply@anthropic.com.", _DEFAULT), []
        )

    def test_human_co_author_passes(self) -> None:
        self.assertEqual(rules.check_body("Co-authored-by: Jane <jane@example.com>", _DEFAULT), [])

    def test_null_body_passes(self) -> None:
        self.assertEqual(rules.check_body(None, _DEFAULT), [])


class AttributionRuleTests(unittest.TestCase):
    """Behaviour 4, per caller, with only the two dependency bots exempt."""

    _TEMPLATE_BODY = (
        "## Problem\n\nx\n\n"
        "<!-- CI accepts ... -->\nGenerated with [model] for [job] in [harness].\n"
    )

    def _fails(self, body: str | None, login: str = "jane") -> bool:
        return bool(rules.check_attribution(body, login, policy({"require-model-attribution": "true"})))

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
        self.assertEqual(rules.check_attribution("No line", "jane", _DEFAULT), [])

    def test_dependency_bots_are_exempt(self) -> None:
        for login in ("renovate[bot]", "dependabot[bot]"):
            with self.subTest(login=login):
                self.assertFalse(self._fails("", login=login))

    def test_added_exemption_covers_attribution_only(self) -> None:
        exempt = policy(
            {"require-model-attribution": "true", "additional-attribution-exemptions": "release-bot"}
        )
        self.assertEqual(rules.check_attribution("No line", "release-bot", exempt), [])
        self.assertTrue(rules.check_attribution("No line", "Release-bot", exempt))
        self.assertTrue(rules.check_attribution("No line", "jane", exempt))
        self.assertTrue(rules.check_attribution("No line", "greptile-apps[bot]", exempt))
        # The PR author's exemption changes no other check.
        trailer = "Done.\n\nCo-authored-by: Claude <noreply@anthropic.com>"
        self.assertEqual(
            _titles(rules.check_body(trailer, exempt)), ["Bot co-author trailer in the PR body"]
        )


class TrailerGrammarTests(unittest.TestCase):
    """The trailer pattern captures the last bracketed address and asks
    is_agent about it. With no identity inputs it matches exactly the lines
    v1 matched with the built-in addresses inside the pattern."""

    _LEADS = ("", " ", "\t", "\u00a0", "Thanks ", "> ")
    _KEYS = ("co-authored-by:", "Co-Authored-By:", "CO-AUTHORED-BY:", "co-authored-by", "coauthored-by:")
    _GAPS = ("", " ", "\t", "\u2028")
    _NAMES = ("", "Claude ", "x <y> ", "<", "a<b ", "<noreply@anthropic.com> ")
    _ADDRESSES = (
        "noreply@anthropic.com",
        "NOREPLY@Anthropic.COM",
        "codex@openai.com",
        "123+Copilot@users.noreply.github.com",
        "Copilot@users.noreply.github.com",
        "x123+Copilot@users.noreply.github.com",
        "456+google-labs-jules[bot]@users.noreply.github.com",
        "noreply@anthropic.com.evil",
        " noreply@anthropic.com",
        "noreply@anthropic.com ",
        "jane@example.com",
        "noreply@anthrop\u0130c.com",
        "",
        "noreply@anthropic.com><x",
    )
    _ENDS = ("", ">", "> ", ">\r", ">\t\r", "> x", ">>", "><noreply@anthropic.com>", ">\u2028")

    def _lines(self) -> list[str]:
        return [
            f"{lead}{key}{gap}{name}<{address}{end}"
            for lead, key, gap, name, address, end in itertools.product(
                self._LEADS, self._KEYS, self._GAPS, self._NAMES, self._ADDRESSES, self._ENDS
            )
        ]

    def test_lines_match_as_in_v1(self) -> None:
        lines = self._lines()
        matched = [line for line in lines if _V1_TRAILER.fullmatch(line)]
        # The corpus holds both outcomes in number.
        self.assertGreater(len(matched), 1000)
        self.assertGreater(len(lines) - len(matched), 10_000)
        differing = [
            line for line in lines
            if bool(_V1_TRAILER.fullmatch(line)) != bool(rules.bot_trailers(line, _DEFAULT))
        ]
        self.assertEqual(differing[:5], [])
        # Split on LF only: a CR or U+2028 stays inside its line.
        self.assertEqual(rules.bot_trailers("\n".join(lines), _DEFAULT), matched)

    def test_trailer_inside_prose_passes(self) -> None:
        for line in (
            "See co-authored-by: Claude <noreply@anthropic.com>",
            "co-authored-by: Claude <noreply@anthropic.com> wrote it",
        ):
            with self.subTest(line=line):
                self.assertEqual(rules.bot_trailers(line, _DEFAULT), [])


class CommitModeTests(unittest.TestCase):
    """co-author-trailers and agent-identities, each on its own."""

    _BOTH = _commit(
        "Fix\n\nCo-authored-by: Claude <noreply@anthropic.com>\n", author=CLAUDE, committer=CLAUDE
    )

    def _check(self, trailers: str, identities: str, commit: Commit = _BOTH) -> list[tuple[str, str]]:
        modes = policy({"co-author-trailers": trailers, "agent-identities": identities})
        return [(finding.title, finding.severity) for finding in rules.check_commit(commit, modes)]

    def test_each_commit_check_has_its_own_mode(self) -> None:
        severity = {"error": "error", "warn": "warning"}
        for trailers, identities in itertools.product(("error", "warn", "off"), repeat=2):
            expected = []
            if trailers != "off":
                expected.append((_TRAILER, severity[trailers]))
            if identities != "off":
                expected += [(_AUTHOR, severity[identities])] * 2
            with self.subTest(trailers=trailers, identities=identities):
                self.assertEqual(self._check(trailers, identities), expected)

    def test_identity_modes_cover_author_and_committer(self) -> None:
        for role, commit in (
            ("authored", _commit(author=CLAUDE)),
            ("committed", _commit(committer=CLAUDE)),
        ):
            with self.subTest(role=role):
                (finding,) = rules.check_commit(commit, policy({"agent-identities": "warn"}))
                self.assertEqual(finding.severity, "warning")
                self.assertIn(f"is {role} by a coding agent", finding.message)
                self.assertEqual(self._check("error", "off", commit), [])

    def test_body_trailer_follows_its_mode(self) -> None:
        body = "Done.\n\nCo-authored-by: Claude <noreply@anthropic.com>"
        (warned,) = rules.check_body(body, policy({"co-author-trailers": "warn"}))
        self.assertEqual(warned.severity, "warning")
        self.assertEqual(rules.check_body(body, policy({"co-author-trailers": "off"})), [])
        self.assertEqual(len(rules.check_body(body, policy({"agent-identities": "off"}))), 1)


class IdentityListTests(unittest.TestCase):
    """allowed-identities and additional-identities, in trailers and in the
    author and committer fields."""

    def _flagged(self, address: str, given: dict[str, str]) -> bool:
        modes = policy(given)
        as_author = bool(rules.check_commit(_commit(author=address), modes))
        in_trailer = bool(rules.bot_trailers(f"Co-authored-by: X <{address}>", modes))
        self.assertEqual(as_author, in_trailer, address)
        return as_author

    def test_allowed_github_handle(self) -> None:
        allowed = {"allowed-identities": "github:Copilot"}
        for address in ("Copilot@users.noreply.github.com", "123+Copilot@users.noreply.github.com",
                        "123+copilot@USERS.noreply.github.com"):
            with self.subTest(address=address):
                self.assertTrue(self._flagged(address, {}))
                self.assertFalse(self._flagged(address, allowed))
        # The vendor address is not the handle's.
        self.assertTrue(self._flagged("copilot@github.com", allowed))

    def test_allowed_email(self) -> None:
        allowed = {"allowed-identities": "email:copilot@github.com"}
        self.assertFalse(self._flagged("copilot@github.com", allowed))
        self.assertFalse(self._flagged("Copilot@GitHub.com", allowed))
        self.assertTrue(self._flagged("123+Copilot@users.noreply.github.com", allowed))

    def test_additional_selectors(self) -> None:
        added = {"additional-identities": "email:agent@example.com\ngithub:my-bot[bot]"}
        for address in ("agent@example.com", "AGENT@Example.COM", "my-bot[bot]@users.noreply.github.com",
                        "42+My-Bot[bot]@users.noreply.github.com"):
            with self.subTest(address=address):
                self.assertFalse(self._flagged(address, {}))
                self.assertTrue(self._flagged(address, added))
        # The built-in list still applies.
        self.assertTrue(self._flagged(CLAUDE, added))

    def test_selectors_match_whole_addresses(self) -> None:
        added = {"additional-identities": "email:noreply@example.com\ngithub:handle"}
        for address in (
            "xnoreply@example.com",
            "noreply@example.com.evil",
            "noreply@example.co",
            "abc+handle@users.noreply.github.com",
            "x42+handle@users.noreply.github.com",
            "+handle@users.noreply.github.com",
            "handle@users.noreply.github.com.evil",
            "handle@github.com",
            "handlex@users.noreply.github.com",
        ):
            with self.subTest(address=address):
                self.assertFalse(self._flagged(address, added))

    def test_non_ascii_addresses_match_no_selector(self) -> None:
        added = {"additional-identities": "email:kelvin@example.com\ngithub:kelvin-bot"}
        self.assertTrue(self._flagged("Kelvin@example.com", added))
        # U+212A KELVIN SIGN lowercases to an ASCII k.
        for address in ("\u212aelvin@example.com", "\u212aelvin-bot@users.noreply.github.com",
                        "kelvin@ex\u0430mple.com"):
            with self.subTest(address=address):
                self.assertFalse(self._flagged(address, added))
        # The built-in list reads the raw address, as in v1, and an allowed
        # selector cannot exempt a non-ASCII address.
        turkish = "noreply@anthrop\u0130c.com"
        self.assertTrue(self._flagged(turkish, {}))
        self.assertTrue(self._flagged(turkish, {"allowed-identities": "email:noreply@anthropic.com"}))
        self.assertFalse(
            self._flagged("noreply@anthropic.com", {"allowed-identities": "email:noreply@anthropic.com"})
        )


if __name__ == "__main__":
    unittest.main()
