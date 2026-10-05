"""The real entrypoint, started as action.yml starts it, against a base
repository behind a file:// URL. This proves the git plumbing, not HTTPS
authentication, which only a live run can show."""

from __future__ import annotations

import os
import unittest

from post_no_bills import gitdata

from .support import CLAUDE, TOKEN, RemoteTestCase, foreign_commands

_ATTRIBUTION = "Done.\n\nGenerated with Claude Opus 5 for implementation in Claude Code."
_EMPTY_RANGE = "no commits between base and head"


class CleanPullRequestTests(RemoteTestCase):
    def test_one_commit_pull_request_passes(self) -> None:
        head = self.remote.commit(
            "Add x\n\nCo-authored-by: Jane <jane@example.com>\n", files={"x.txt": "x\n"}
        )
        self.remote.open_pull_request(head)

        result = self.remote.run_action(
            self.remote.event(head=head, body=_ATTRIBUTION),
            {"model-attribution": "model"},
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "checked 1 commit, 1 changed file, and the PR title and body: 0 errors, 0 warnings",
            result.stdout,
        )
        self.assertNotIn("::error", result.stdout)
        self.assertEqual(os.listdir(self.remote.runner_temp), [])

    def test_commits_with_an_empty_diff_pass(self) -> None:
        self.remote.commit("Add x", files={"x.txt": "x\n"})
        (self.remote.work / "x.txt").unlink()
        self.remote.git("rm", "--quiet", "--cached", "x.txt")
        self.remote.commit("Remove x")
        head = self.remote.commit("An empty commit")
        self.remote.open_pull_request(head)
        self.assertEqual(self.remote.git("diff", "--stat", self.remote.base, head), "")

        result = self.remote.run_action(self.remote.event(head=head))

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("checked 3 commits", result.stdout)

    def test_credential_appears_only_in_the_mask_line(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)

        result = self.remote.run_action(self.remote.event(head=head))

        credential = gitdata.encode_credential(TOKEN)
        output = result.stdout + result.stderr
        self.assertNotIn(TOKEN, output)
        self.assertEqual(
            [line for line in output.splitlines() if credential in line],
            [f"::add-mask::{credential}"],
        )
        self.assertTrue(result.stdout.startswith(f"::add-mask::{credential}\n"))


class RuleTests(RemoteTestCase):
    def _run(self, head: str, body: str | None = _ATTRIBUTION, inputs: dict[str, str] | None = None, **overrides: str):
        self.remote.open_pull_request(head)
        return self.remote.run_action(self.remote.event(head=head, body=body), inputs, **overrides)

    def test_empty_commit_with_bot_trailer_fails(self) -> None:
        head = self.remote.commit("Empty\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")
        result = self._run(head)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"::error title=Bot co-author trailer in a commit::Commit {head}", result.stdout)

    def test_bot_author_fails(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"}, GIT_AUTHOR_EMAIL=CLAUDE)
        result = self._run(head)
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error title=Bot commit author::", result.stdout)

    def test_bot_committer_on_an_earlier_commit_fails(self) -> None:
        self.remote.commit(
            "One", GIT_COMMITTER_EMAIL="456+google-labs-jules[bot]@users.noreply.github.com"
        )
        head = self.remote.commit("Two")
        result = self._run(head)
        self.assertEqual(result.returncode, 1)
        self.assertIn("committed by a coding agent", result.stdout)

    def test_body_trailer_fails(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        result = self._run(head, body="Done.\r\n\r\nCo-authored-by: Claude <noreply@anthropic.com>\r\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error title=Bot co-author trailer in the PR body::", result.stdout)

    def test_command_syntax_in_trailer_names_stays_inert(self) -> None:
        trailers = (
            "Co-authored-by: ##[warning]x <noreply@anthropic.com>\n"
            "Co-authored-by: ::warning::x <cursoragent@cursor.com>\n"
        )
        head = self.remote.commit(f"Add x\n\n{trailers}", files={"x.txt": "x\n"})
        result = self._run(head, body=f"Done.\n\n{trailers}")
        self.assertEqual(result.returncode, 1)
        for title in ("Bot co-author trailer in a commit", "Bot co-author trailer in the PR body"):
            self.assertEqual(result.stdout.count(f"::error title={title}::"), 2, title)
        self.assertEqual(foreign_commands(result.stdout), [])
        self.assertEqual(result.stdout.count("<U+0023>#[warning]x"), 4)

    def test_reads_event_path_from_environment(self) -> None:
        """A null body read through GITHUB_EVENT_PATH fails only when required."""
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        required = self._run(head, body=None, inputs={"model-attribution": "model"})
        optional = self._run(head, body=None, inputs={"model-attribution": "off"})
        self.assertEqual(required.returncode, 1)
        self.assertIn("::error title=PR model attribution required::", required.stdout)
        self.assertEqual(optional.returncode, 0, optional.stdout)

    def test_attribution_is_required_unless_turned_off(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        default = self._run(head, body="Done.")
        self.assertEqual(default.returncode, 1, default.stdout)
        self.assertIn("::error title=PR model attribution required::", default.stdout)
        off = self._run(head, body="Done.", inputs={"model-attribution": "off"})
        self.assertEqual(off.returncode, 0, off.stdout)
        self.assertNotIn("::error title=PR model attribution required::", off.stdout)

    def test_hidden_unicode_warn_is_accepted(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.assertEqual(self._run(head, inputs={"hidden-unicode": "warn"}).returncode, 0)

    def test_warnings_alone_pass_and_an_error_fails(self) -> None:
        head = self.remote.commit(
            "Add x\n\nCo-authored-by: Claude <noreply@anthropic.com>\n",
            files={"x.txt": "x\n"},
            GIT_AUTHOR_EMAIL=CLAUDE,
        )
        warned = self._run(head, inputs={"co-author-trailers": "warn", "agent-identities": "warn"})
        self.assertEqual(warned.returncode, 0, warned.stdout)
        self.assertIn("::warning title=Bot co-author trailer in a commit::", warned.stdout)
        self.assertIn("::warning title=Bot commit author::", warned.stdout)
        self.assertIn("0 errors, 2 warnings", warned.stdout)

        failed = self._run(head, inputs={"co-author-trailers": "warn"})
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn("::error title=Bot commit author::", failed.stdout)
        self.assertIn("1 error, 1 warning", failed.stdout)

        quiet = self._run(head, inputs={"co-author-trailers": "off", "agent-identities": "off"})
        self.assertEqual(quiet.returncode, 0, quiet.stdout)
        self.assertIn("0 errors, 0 warnings", quiet.stdout)

    def test_allowed_identity_passes(self) -> None:
        copilot = "198982749+Copilot@users.noreply.github.com"
        head = self.remote.commit(
            f"Add x\n\nCo-authored-by: Copilot <{copilot}>\n",
            files={"x.txt": "x\n"},
            GIT_AUTHOR_EMAIL=copilot,
        )
        self.assertEqual(self._run(head).returncode, 1)
        allowed = self._run(head, inputs={"allowed-identities": "github:Copilot"})
        self.assertEqual(allowed.returncode, 0, allowed.stdout)


class FailClosedTests(RemoteTestCase):
    def _fails_with(self, message: str, result) -> None:
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(message, result.stdout)
        self.assertEqual(os.listdir(self.remote.runner_temp), [])

    def test_head_equal_to_base_fails(self) -> None:
        self.remote.open_pull_request(self.remote.base)
        result = self.remote.run_action(self.remote.event(head=self.remote.base))
        self._fails_with(_EMPTY_RANGE, result)

    def test_head_contained_in_base_fails(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        base = self.remote.commit("Merged on main", files={"y.txt": "y\n"})
        self.remote.push(base, "refs/heads/main")
        self.remote.open_pull_request(head)
        result = self.remote.run_action(self.remote.event(head=head, base=base))
        self._fails_with(_EMPTY_RANGE, result)

    def test_head_moved_since_the_event_fails(self) -> None:
        old = self.remote.commit("Add x", files={"x.txt": "x\n"})
        new = self.remote.commit("Add y", files={"y.txt": "y\n"})
        self.remote.open_pull_request(new)
        result = self.remote.run_action(self.remote.event(head=old))
        self._fails_with("head moved since the event", result)

    def test_base_commit_missing_from_history_fails(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)
        result = self.remote.run_action(self.remote.event(head=head, base="d" * 40))
        self._fails_with(f"the event's base commit {'d' * 40} is not in the fetched history", result)

    def test_missing_base_branch_fails(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)
        result = self.remote.run_action(self.remote.event(head=head, base_ref="gone"))
        self._fails_with("git fetch failed", result)

    def test_invalid_base_ref_fails_before_fetching(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)
        for ref in ("-x", "a..b", "@{-1}", "HEAD", "a b"):
            with self.subTest(ref=ref):
                result = self.remote.run_action(self.remote.event(head=head, base_ref=ref))
                self._fails_with("pull_request.base.ref is not a valid branch name", result)

    def _path_with_git(self, script: str | None) -> str:
        """A PATH whose only git, if any, is the given shell script."""
        directory = self.remote.root / "fake-bin"
        directory.mkdir()
        if script is not None:
            git = directory / "git"
            git.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
            git.chmod(0o755)
        return str(directory)

    def test_missing_git_fails(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)
        result = self.remote.run_action(
            self.remote.event(head=head), PATH=self._path_with_git(None)
        )
        self._fails_with(
            "::error title=post-no-bills::git is not on PATH. This action needs git 2.31 or newer.",
            result,
        )

    def test_git_older_than_2_31_fails(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)
        result = self.remote.run_action(
            self.remote.event(head=head),
            PATH=self._path_with_git('echo "git version 2.30.9"'),
        )
        self._fails_with("::error title=post-no-bills::This action needs git 2.31 or newer.", result)
        self.assertIn("post-no-bills: git version 2.30.9\n", result.stdout)

    def test_malformed_event_fails(self) -> None:
        path = self.remote.root / "event.json"
        path.write_text("{", encoding="utf-8")
        self._fails_with("cannot read the event payload", self.remote.run_action(path))

    def test_pull_request_trigger_fails(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)
        result = self.remote.run_action(
            self.remote.event(head=head), CA_EVENT_NAME="pull_request"
        )
        self._fails_with("runs only under pull_request_target", result)

    def test_invalid_inputs_fail(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)
        event = self.remote.event(head=head)
        self._fails_with(
            "input model-attribution must be off, model, job, tool or host",
            self.remote.run_action(event, {"model-attribution": "yes"}),
        )
        self._fails_with(
            "input hidden-unicode must be error, warn or off",
            self.remote.run_action(event, {"hidden-unicode": "of"}),
        )

    def test_unknown_input_fails_cleanly_before_git(self) -> None:
        """Also the import smoke test: run.py under -I reaches the input
        check, and nothing but its annotation is printed."""
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)
        result = self.remote.run_action(
            self.remote.event(head=head), {"model-attributionn": "model"}
        )
        self._fails_with(
            "::error title=post-no-bills::unknown input(s): 'model-attributionn'\n", result
        )
        self.assertEqual(result.stderr, "")
        self.assertNotIn("git version", result.stdout)
        self.assertNotIn("::add-mask::", result.stdout)


if __name__ == "__main__":
    unittest.main()
