from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_guardrails import event, gitdata
from agent_guardrails.main import main

from .support import TOKEN, RemoteTestCase, foreign_commands, git_environment

_CREDENTIAL = gitdata.encode_credential(TOKEN)
_HEADER_KEY = "http.https://github.com/.extraheader"
_HEADER = f"AUTHORIZATION: basic {_CREDENTIAL}"


class _Recorder(io.StringIO):
    """stdout that records each write in the same timeline as git calls."""

    def __init__(self, timeline: list) -> None:
        super().__init__()
        self._timeline = timeline

    def write(self, text: str) -> int:
        self._timeline.append(("out", text))
        return super().write(text)


class _Finished:
    """A git process that has already exited with this result."""

    def __init__(self, argv: list, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.args = argv
        self.returncode = returncode
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


class CredentialTests(RemoteTestCase):
    """Synthetic token, a patched subprocess, and a fetch that fails with
    both forms of the secret and a terminal escape in git's stderr."""

    def setUp(self) -> None:
        super().setUp()
        self.timeline: list = []
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.environ = self.remote.environment(
            self.remote.event(head=head), CA_SERVER_URL="https://github.com"
        )

    def _fake_git(self, argv, *, env, **kwargs):
        self.timeline.append(("git", list(argv), dict(env)))
        if argv[1] == "--version":
            return _Finished(argv, 0, b"git version 2.31.0\n", b"")
        if argv[1] == "fetch":
            stderr = (
                f"fatal: unable to access 'https://x-access-token:{TOKEN}@github.com/'\n"
                f"sent {_HEADER}\n\x1b[2K::error::spoofed ##[warning]spoofed"
            ).encode()
            return _Finished(argv, 128, b"", stderr)
        if argv[1] == "check-ref-format":
            return _Finished(argv, 0, b"main\n", b"")
        return _Finished(argv, 0, b"", b"")

    def _run(self) -> tuple[int, str, str]:
        stdout, stderr = _Recorder(self.timeline), io.StringIO()
        with mock.patch.object(gitdata.subprocess, "Popen", self._fake_git), contextlib.redirect_stderr(stderr):
            code = main(self.environ, stdout=stdout)
        return code, stdout.getvalue(), stderr.getvalue()

    def _calls(self) -> list:
        calls = [entry for entry in self.timeline if entry[0] == "git"]
        self.assertIn("fetch", [argv[1] for _, argv, _ in calls])
        return calls

    def test_credential_never_reaches_child_argv(self) -> None:
        self._run()
        argv = "\n".join(" ".join(call[1]) for call in self._calls())
        self.assertIn("fetch", argv)
        self.assertNotIn(TOKEN, argv)
        self.assertNotIn(_CREDENTIAL, argv)

    def test_credential_travels_as_runtime_config_to_fetch_only(self) -> None:
        self._run()
        for _, argv, env in self._calls():
            pairs = {
                env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
                for index in range(int(env["GIT_CONFIG_COUNT"]))
            }
            with self.subTest(command=argv[1]):
                if argv[1] == "fetch":
                    self.assertEqual(pairs.get(_HEADER_KEY), _HEADER)
                else:
                    self.assertNotIn(_HEADER_KEY, pairs)

    def test_credential_never_reaches_output_except_the_mask(self) -> None:
        code, stdout, stderr = self._run()
        self.assertEqual(code, 1)
        self.assertIn("git fetch failed", stdout)
        self.assertNotIn(TOKEN, stdout + stderr)
        leaking = [line for line in stdout.splitlines() if _CREDENTIAL in line]
        self.assertEqual(leaking, [f"::add-mask::{_CREDENTIAL}"])
        self.assertNotIn(_CREDENTIAL, stderr)

    def test_git_errors_cannot_inject_workflow_commands(self) -> None:
        _, stdout, _ = self._run()
        self.assertIn("<U+001B>", stdout)
        self.assertNotIn("\x1b", stdout)
        self.assertEqual(foreign_commands(stdout), [])
        self.assertIn("<U+0023>#[warning]spoofed", stdout)
        for line in stdout.splitlines():
            self.assertRegex(line, r"^(::add-mask::|::error title=|agent-guardrails: )")

    def test_mask_precedes_the_first_git_call(self) -> None:
        self._run()
        kinds = [
            "mask" if entry[0] == "out" and entry[1].startswith("::add-mask::") else entry[0]
            for entry in self.timeline
        ]
        self.assertIn("mask", kinds)
        self.assertLess(kinds.index("mask"), kinds.index("git"))

    def test_git_environment_is_built_from_scratch(self) -> None:
        inherited = {
            "GIT_TRACE": "1",
            "GIT_ATTR_SOURCE": "HEAD",
            "GIT_CONFIG_PARAMETERS": "'core.hookspath'='/tmp'",
            "GIT_DIR": "/elsewhere",
            "GIT_INDEX_FILE": "/elsewhere/index",
            "GIT_TEMPLATE_DIR": "/elsewhere/templates",
        }
        self.environ.update(inherited)
        with mock.patch.dict(os.environ, inherited):
            self._run()
        runner_temp = str(self.remote.runner_temp)
        for _, argv, env in self._calls():
            expected = git_environment(int(env["GIT_CONFIG_COUNT"]))
            with self.subTest(command=argv[1]):
                self.assertEqual(set(env), expected)
                self.assertTrue(env["GIT_DIR"].startswith(runner_temp))
                self.assertEqual(env["HOME"], env["XDG_CONFIG_HOME"])
                self.assertEqual(env["GIT_ATTR_NOSYSTEM"], "1")


class TemporaryRepositoryTests(RemoteTestCase):
    """The real git, against a file:// base repository."""

    def test_repository_has_no_index_hooks_or_attributes_and_is_removed(self) -> None:
        seen: dict[str, list[str]] = {}
        real_rmtree = shutil.rmtree

        def inspect_then_remove(path, *args, **kwargs):
            repo = Path(path) / "repo.git"
            seen["repo"] = sorted(os.listdir(repo))
            seen["heads"] = sorted(os.listdir(repo / "refs" / "heads"))
            seen["hooks"] = sorted(os.listdir(Path(path) / "no-hooks"))
            return real_rmtree(path, *args, **kwargs)

        head = self.remote.commit("Add attributes", files={".gitattributes": "* binary\n"})
        self.remote.open_pull_request(head)
        environ = self.remote.environment(self.remote.event(head=head))
        with mock.patch.object(gitdata.shutil, "rmtree", inspect_then_remove):
            code = main(environ, stdout=io.StringIO())

        self.assertEqual(code, 0)
        for name in ("index", "info", "hooks"):
            self.assertNotIn(name, seen["repo"])
        self.assertEqual(seen["heads"], [])
        self.assertEqual(seen["hooks"], [])
        self.assertEqual(os.listdir(self.remote.runner_temp), [])

    def test_fetch_starts_no_background_maintenance(self) -> None:
        trace = Path(self.remote.root) / "trace2.json"
        real_environment = gitdata._Repository.environment

        def traced(repo, *args, **kwargs):
            env = real_environment(repo, *args, **kwargs)
            env["GIT_TRACE2_EVENT"] = str(trace)
            return env

        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)
        environ = self.remote.environment(self.remote.event(head=head))
        with mock.patch.object(gitdata._Repository, "environment", traced):
            code = main(environ, stdout=io.StringIO())

        self.assertEqual(code, 0)
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertIn("fetch", [e["argv"][1] for e in events if e["event"] == "start"])
        children = [e["argv"] for e in events if e["event"] == "child_start"]
        self.assertEqual([argv for argv in children if "maintenance" in argv], [])

    def test_base_history_reached_through_a_merge_is_not_scanned(self) -> None:
        # A PR that merged its base branch: a trailer already on the base is
        # not part of the PR. A shallow fetch walks past the cut-off and
        # blames the PR for it.
        old = self.remote.commit("feat: Old change\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n")
        self.remote.push(old, "refs/heads/main")
        work = self.remote.commit("feat: PR change", files={"x.txt": "x\n"})
        self.remote.git("checkout", "--quiet", old)
        later = self.remote.commit("chore: Later change on main", files={"y.txt": "y\n"})
        self.remote.push(later, "refs/heads/main")
        self.remote.git("checkout", "--quiet", work)
        self.remote.git("merge", "--quiet", "--no-ff", "-m", "Merge main", later)
        head = self.remote.git("rev-parse", "HEAD")
        self.remote.open_pull_request(head)
        environ = self.remote.environment(self.remote.event(head=head, base=later))
        stdout = io.StringIO()

        pull_request = gitdata.fetch_pull_request(event.load(environ), _CREDENTIAL, lambda _: None)
        code = main(environ, stdout=stdout)

        self.assertEqual(sorted(c.sha for c in pull_request.commits), sorted([work, head]))
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertNotIn("cursoragent@cursor.com", stdout.getvalue())


class AcquisitionLimitTests(RemoteTestCase):
    """The output and time limits hold while git runs, and running out fails
    the run in warn mode too."""

    def _main(self, head: str, **overrides: str) -> tuple[int, str, float]:
        self.remote.open_pull_request(head)
        environ = self.remote.environment(
            self.remote.event(head=head), CA_HIDDEN_UNICODE="warn", **overrides
        )
        stdout = io.StringIO()
        start = time.monotonic()
        code = main(environ, stdout=stdout)
        return code, stdout.getvalue(), time.monotonic() - start

    def _fake_git(self, script: str, *tools: str) -> str:
        """A PATH whose only git is the given shell script. It names each
        tool it runs by its absolute path, as {tool}."""
        directory = self.remote.root / "fake-bin"
        directory.mkdir()
        git = directory / "git"
        paths = {tool: shutil.which(tool) for tool in tools}
        git.write_text(f"#!/bin/sh\n{script.format(**paths)}\n", encoding="utf-8")
        git.chmod(0o755)
        return str(directory)

    def test_patch_larger_than_the_output_limit_fails(self) -> None:
        head = self.remote.commit("Add data", files={"data.txt": "line\n" * 300_000})
        with mock.patch.object(gitdata, "OUTPUT_LIMIT", 2**20):
            code, stdout, _ = self._main(head)
        self.assertEqual(code, 1, stdout)
        self.assertIn(
            "::error title=agent-guardrails::not fully checked: git printed more than 1 MiB",
            stdout,
        )
        self.assertEqual(os.listdir(self.remote.runner_temp), [])

    def test_output_limit_counts_every_git_call_together(self) -> None:
        # The log and the patch each stay below the limit; together they do not.
        head = self.remote.commit("Add data\n\n" + "m" * 700_000, files={"data.txt": "d" * 700_000})
        with mock.patch.object(gitdata, "OUTPUT_LIMIT", 2**20):
            code, stdout, _ = self._main(head)
        self.assertEqual(code, 1, stdout)
        self.assertIn("not fully checked: git printed more than 1 MiB", stdout)

    def test_endless_output_is_cut_off_at_the_limit(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        with mock.patch.object(gitdata, "OUTPUT_LIMIT", 2**20), mock.patch.object(gitdata, "TIME_LIMIT", 30):
            code, stdout, elapsed = self._main(head, PATH=self._fake_git("exec {yes}", "yes"))
        self.assertEqual(code, 1, stdout)
        self.assertIn("not fully checked: git printed more than 1 MiB", stdout)
        self.assertLess(elapsed, 15)

    def test_git_that_does_not_finish_is_stopped(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        with mock.patch.object(gitdata, "TIME_LIMIT", 1):
            code, stdout, elapsed = self._main(
                head, PATH=self._fake_git("exec {sleep} 60", "sleep")
            )
        self.assertEqual(code, 1, stdout)
        self.assertIn(
            "::error title=agent-guardrails::not fully checked: reading this pull request "
            "with git took longer than 1 seconds",
            stdout,
        )
        self.assertLess(elapsed, 15)

    def test_only_the_start_of_stderr_is_kept(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        script = "{head} -c 1000000 /dev/zero | {tr} '\\000' e >&2; exit 1"
        code, stdout, _ = self._main(head, PATH=self._fake_git(script, "head", "tr"))
        self.assertEqual(code, 1, stdout)
        (failure,) = [line for line in stdout.splitlines() if line.startswith("::error")]
        self.assertTrue(failure.startswith("::error title=agent-guardrails::git --version failed: eee"))
        self.assertLess(len(failure), 5000)

    def test_a_secret_cut_off_by_the_stderr_limit_is_not_shown_in_part(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        for offset in (4060, 4080, 4095):
            with self.subTest(offset=offset):
                shutil.rmtree(self.remote.root / "fake-bin", ignore_errors=True)
                script = f"{{head}} -c {offset} /dev/zero | {{tr}} '\\000' e >&2; echo {TOKEN} >&2; exit 1"
                code, stdout, _ = self._main(head, PATH=self._fake_git(script, "head", "tr"))
                self.assertEqual(code, 1, stdout)
                self.assertIn("git --version failed: eee", stdout)
                self.assertNotIn(TOKEN[:6], stdout)


class _Canned:
    """A repository whose git prints fixed output: the raw listing, the
    patch between base and head, and git's reading of a new blob."""

    def __init__(self, raw: bytes, patch: bytes, new_side: bytes = b"") -> None:
        self._raw = raw
        self._patch = patch
        self._new_side = new_side

    def git(self, *args: str) -> bytes:
        if args[0] == "hash-object":
            return _EMPTY_BLOB.encode() + b"\n"
        if "--raw" in args:
            return self._raw
        return self._new_side if _EMPTY_BLOB in args else self._patch


_EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
_BLOB = "1" * 40


def _rows(*rows: bytes) -> bytes:
    return b"".join(row + b"\n" for row in rows)


def _raw(status: str, old_mode: str, new_mode: str, path: bytes) -> bytes:
    old_blob = "0" * 40 if status == "A" else "2" * 40
    return f":{old_mode} {new_mode} {old_blob} {_BLOB} {status}".encode() + b"\0" + path + b"\0"


class PatchParserTests(unittest.TestCase):
    """_sections trusts a hunk's line counts, so a count left unpaid at the
    end is a patch it cannot have read whole."""

    _HEADER = b"diff --git a/x.md b/x.md"

    def test_complete_hunks_are_read(self) -> None:
        (section,) = gitdata._sections(
            _rows(self._HEADER, b"@@ -0,0 +1,2 @@", b"+one", b"+two", b"@@ -5,2 +6,0 @@", b"-a", b"-b")
        )
        self.assertEqual(section.added, [(1, b"one"), (2, b"two")])

    def test_unfinished_last_hunk_fails(self) -> None:
        cases = {
            "missing addition": (b"@@ -0,0 +1,2 @@", b"+one"),
            "missing removal": (b"@@ -1,2 +0,0 @@", b"-one"),
            "missing both": (b"@@ -1 +1 @@", b"-one"),
            "no content at all": (b"@@ -0,0 +1 @@",),
        }
        for name, rows in cases.items():
            with self.subTest(name), self.assertRaisesRegex(gitdata.GitError, "cannot parse the diff"):
                gitdata._sections(_rows(self._HEADER, *rows))

    def test_hunk_that_promises_nothing_fails(self) -> None:
        with self.assertRaisesRegex(gitdata.GitError, "cannot parse the diff"):
            gitdata._sections(_rows(self._HEADER, b"@@ -1,0 +1,0 @@"))


class ReaderRecordTests(unittest.TestCase):
    """_changed_files on fixed git output: what it returns, and what it
    refuses."""

    def test_complete_patch_gives_the_added_lines(self) -> None:
        repo = _Canned(
            _raw("A", "000000", "100644", b"x.md"),
            _rows(b"diff --git a/x.md b/x.md", b"@@ -0,0 +1,2 @@", b"+clean", b"+more"),
        )
        (changed,) = gitdata._changed_files(repo, "b" * 40, "h" * 40)
        self.assertEqual((changed.kind, changed.added), (gitdata.TEXT, ((1, "clean"), (2, "more"))))

    def test_truncated_patch_fails(self) -> None:
        for last in ((b"+clean",), ()):
            repo = _Canned(
                _raw("A", "000000", "100644", b"x.md"),
                _rows(b"diff --git a/x.md b/x.md", b"@@ -0,0 +1,2 @@", *last),
            )
            with self.subTest(rows=len(last)), self.assertRaisesRegex(gitdata.GitError, "cannot parse"):
                gitdata._changed_files(repo, "b" * 40, "h" * 40)

    def _mode_change(self, new_side: bytes) -> gitdata.ChangedFile:
        repo = _Canned(
            _raw("M", "100644", "100755", b"x.md"),
            _rows(b"diff --git a/x.md b/x.md", b"old mode 100644", b"new mode 100755"),
            new_side,
        )
        (changed,) = gitdata._changed_files(repo, "b" * 40, "h" * 40)
        return changed

    def test_mode_change_reads_the_new_blob_and_adds_nothing(self) -> None:
        header = f"diff --git a/{_EMPTY_BLOB} b/{_BLOB}".encode()
        changed = self._mode_change(_rows(header, b"@@ -0,0 +1 @@", b"+ok"))
        self.assertEqual((changed.kind, changed.added), (gitdata.TEXT, ()))
        binary = self._mode_change(_rows(header, b"Binary files a/x and b/y differ"))
        self.assertEqual(binary.kind, gitdata.REJECTED_BINARY)
        undecodable = self._mode_change(_rows(header, b"@@ -0,0 +1 @@", b"+caf\xe9"))
        self.assertEqual(undecodable.kind, gitdata.UNDECODABLE)

    def test_unreadable_new_side_fails(self) -> None:
        cases = {
            "another blob": _rows(f"diff --git a/{_EMPTY_BLOB} b/{'3' * 40}".encode(), b"@@ -0,0 +1 @@", b"+ok"),
            "truncated": _rows(f"diff --git a/{_EMPTY_BLOB} b/{_BLOB}".encode(), b"@@ -0,0 +1,2 @@", b"+ok"),
            "nothing": b"",
        }
        for name, new_side in cases.items():
            with self.subTest(name), self.assertRaisesRegex(gitdata.GitError, "cannot parse the diff"):
                self._mode_change(new_side)


if __name__ == "__main__":
    unittest.main()
