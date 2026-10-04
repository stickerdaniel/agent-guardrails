from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

from post_no_bills import event, gitdata
from post_no_bills.main import main

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


_SH = shutil.which("sh")
# The tests replace subprocess.Popen, which is gitdata's too.
_POPEN = subprocess.Popen


def _finished(returncode: int, out: str, err: str, **popen) -> subprocess.Popen:
    """A git process that prints this result and exits. It is a real one, so
    that its group, pipes and exit are real too."""
    script = 'printf "%s" "$1"; printf "%s" "$2" >&2; exit "$3"'
    return _POPEN([_SH, "-c", script, "sh", out, err, str(returncode)], **popen)


def _running(pid: int) -> bool:
    """Whether pid is a process that has not ended. One that has exited and
    waits to be reaped has ended."""
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _ended(pid: int, within: float = 2) -> bool:
    """Whether pid ends within a moment: SIGKILL is not synchronous."""
    deadline = time.monotonic() + within
    while _running(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


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

    def _fake_git(self, argv, *, env, **popen):
        self.timeline.append(("git", list(argv), dict(env)))
        if argv[1] == "--version":
            return _finished(0, "git version 2.31.0\n", "", env=env, **popen)
        if argv[1] == "fetch":
            stderr = (
                f"fatal: unable to access 'https://x-access-token:{TOKEN}@github.com/'\n"
                f"sent {_HEADER}\n\x1b[2K::error::spoofed ##[warning]spoofed"
            )
            return _finished(128, "", stderr, env=env, **popen)
        if argv[1] == "check-ref-format":
            return _finished(0, "main\n", "", env=env, **popen)
        return _finished(0, "", "", env=env, **popen)

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
            self.assertRegex(line, r"^(::add-mask::|::error title=|post-no-bills: )")

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


class _FakeGitTestCase(RemoteTestCase):
    """The entrypoint in this process, where a test can put its own git
    first on PATH."""

    def _main(self, head: str, mode: str = "warn", **overrides: str) -> tuple[int, str, float]:
        self.remote.open_pull_request(head)
        environ = self.remote.environment(
            self.remote.event(head=head), {"hidden-unicode": mode}, **overrides
        )
        stdout = io.StringIO()
        start = time.monotonic()
        code = main(environ, stdout=stdout)
        return code, stdout.getvalue(), time.monotonic() - start

    def _fake_git(self, script: str, *tools: str) -> str:
        """A PATH whose only git is the given shell script. It names each
        tool it runs by its absolute path, as {tool}, and its own directory
        as {bin}."""
        directory = self.remote.root / "fake-bin"
        directory.mkdir()
        git = directory / "git"
        paths = {tool: shutil.which(tool) for tool in tools}
        git.write_text(f"#!/bin/sh\n{script.format(bin=directory, **paths)}\n", encoding="utf-8")
        git.chmod(0o755)
        return str(directory)


class NewSideContractTests(_FakeGitTestCase):
    """git answers a comparison of a new file with nothing by naming the
    blob and then saying nothing about it. That is not a clean file."""

    def test_header_only_new_side_fails_in_either_mode(self) -> None:
        # Everything is real git except that one answer.
        script = (
            "for last; do :; done\n"
            'case " $* " in *" {empty} "*)\n'
            "  printf 'diff --git a/{empty} b/%s\\n' \"$last\"; exit 0;;\n"
            "esac\n"
            'exec {git} "$@"'
        ).replace("{empty}", _EMPTY_BLOB)
        base = self.remote.commit("Base", files={"x.png": b"PNG\0"})
        self.remote.push(base, "refs/heads/main")
        self.remote.base = base
        head = self.remote.commit("Replace", files={"x.png": "new\u200btext\n"})
        path = self._fake_git(script, "git")
        for mode in ("error", "warn"):
            with self.subTest(mode=mode):
                code, stdout, _ = self._main(head, mode, PATH=path)
                self.assertEqual(code, 1, stdout)
                self.assertIn("::error title=post-no-bills::cannot parse the diff\n", stdout)
                self.assertNotIn("checked 1 commit", stdout)

    def test_header_only_creation_fails_in_either_mode(self) -> None:
        # Real git, except for the patch between base and head, which names a
        # new file, or a type change's creation, and says nothing about it.
        # The raw listing still names the real blob, which is not empty.
        script = (
            'case " $* " in *" -U0 "*" -- ") exec {cat} {bin}/main.patch;; esac\n'
            'exec {git} "$@"'
        )
        os.symlink("target", self.remote.work / "link")
        self.remote.git("add", "link")
        base = self.remote.commit("Base")
        self.remote.push(base, "refs/heads/main")
        self.remote.base = base
        (self.remote.work / "link").unlink()
        head = self.remote.commit("Add", files={"link": "a\u200bb\n", "x.md": "new\u200btext\n"})
        path = self._fake_git(script, "cat", "git")
        link = (
            "diff --git a/link b/link\ndeleted file mode 120000\n"
            "@@ -1 +0,0 @@\n-target\n\\ No newline at end of file\n"
            "diff --git a/link b/link\nnew file mode 100644\n"
        )
        x = "diff --git a/x.md b/x.md\nnew file mode 100644\n"
        link_lines = "@@ -0,0 +1 @@\n+a\u200bb\n"
        x_lines = "@@ -0,0 +1 @@\n+new\u200btext\n"
        patches = {
            "complete": link + link_lines + x + x_lines,
            "A header only": link + link_lines + x,
            "T header only": link + x + x_lines,
        }
        for name, patch in patches.items():
            (self.remote.root / "fake-bin" / "main.patch").write_text(patch, encoding="utf-8")
            for mode in ("error", "warn"):
                with self.subTest(name, mode=mode):
                    code, stdout, _ = self._main(head, mode, PATH=path)
                    if name == "complete":
                        # The fake patch is what the reader read.
                        self.assertEqual(code, 1 if mode == "error" else 0, stdout)
                        self.assertEqual(stdout.count("title=Invisible character::"), 2)
                        self.assertIn("checked 1 commit, 2 changed files", stdout)
                    else:
                        self.assertEqual(code, 1, stdout)
                        self.assertIn("::error title=post-no-bills::cannot parse the diff\n", stdout)
                        self.assertNotIn("checked 1 commit", stdout)

    def test_content_change_shown_as_none_fails_in_either_mode(self) -> None:
        # Real git, except for the patch between base and head where a test
        # writes one. plain.md only became executable, so its old hidden
        # character is not new; the other two files gained one.
        script = (
            'case " $* " in *" -U0 "*" -- ")\n'
            "  if [ -f {bin}/main.patch ]; then exec {cat} {bin}/main.patch; fi;;\n"
            "esac\n"
            'exec {git} "$@"'
        )
        base = self.remote.commit(
            "Base", files={"plain.md": "a\u200bb\n", "x.png": b"PNG\0", "y.md": "old\n"}
        )
        self.remote.push(base, "refs/heads/main")
        self.remote.base = base
        for name in ("plain.md", "y.md"):
            (self.remote.work / name).chmod(0o755)
            self.remote.git("add", name)
        head = self.remote.commit(
            "Change", files={"x.png": "new\u200btext\n", "y.md": "new\u200bline\n"}
        )
        path = self._fake_git(script, "cat", "git")
        plain = "diff --git a/plain.md b/plain.md\nold mode 100644\nnew mode 100755\n"
        x_binary = "diff --git a/x.png b/x.png\nBinary files a/x.png and b/x.png differ\n"
        y_modes = "diff --git a/y.md b/y.md\nold mode 100644\nnew mode 100755\n"
        y_lines = "@@ -1 +1 @@\n-old\n+new\u200bline\n"
        patches = {
            "real git": None,
            "complete": plain + x_binary + y_modes + y_lines,
            "binary to text, header only": plain + "diff --git a/x.png b/x.png\n" + y_modes + y_lines,
            "mode and content, modes only": plain + x_binary + y_modes,
        }
        for name, patch in patches.items():
            main_patch = self.remote.root / "fake-bin" / "main.patch"
            if patch is None:
                main_patch.unlink(missing_ok=True)
            else:
                main_patch.write_text(patch, encoding="utf-8")
            for mode in ("error", "warn"):
                with self.subTest(name, mode=mode):
                    code, stdout, _ = self._main(head, mode, PATH=path)
                    if name in ("real git", "complete"):
                        severity = "error" if mode == "error" else "warning"
                        self.assertEqual(code, 1 if mode == "error" else 0, stdout)
                        for changed in ("x.png", "y.md"):
                            self.assertIn(
                                f"::{severity} file={changed},line=1,title=Invisible character::",
                                stdout,
                            )
                        self.assertNotIn("plain.md", stdout)
                        self.assertIn("checked 1 commit, 3 changed files", stdout)
                    else:
                        self.assertEqual(code, 1, stdout)
                        self.assertIn("::error title=post-no-bills::cannot parse the diff\n", stdout)
                        self.assertNotIn("checked 1 commit", stdout)


class AcquisitionLimitTests(_FakeGitTestCase):
    """The output and time limits hold while git runs, and running out fails
    the run in warn mode too."""

    def _helper(self) -> int:
        """The process a fake git left running, and wrote to {bin}/helper.pid."""
        return int((self.remote.root / "fake-bin" / "helper.pid").read_text())

    def _stop_helper(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            pid = self._helper()
            if _running(pid):
                os.kill(pid, signal.SIGKILL)

    def _watched(self, head: str, **overrides: str) -> tuple[int, str, float, dict]:
        """_main, noting at the removal of the temporary repository whether
        the helper had ended, and which git processes were still unreaped or
        had a pipe open."""
        self.addCleanup(self._stop_helper)
        processes: list[subprocess.Popen] = []
        real_popen, real_rmtree = subprocess.Popen, shutil.rmtree

        def popen(argv, *args, **kwargs):
            proc = real_popen(argv, *args, **kwargs)
            if argv[0] == "git":
                processes.append(proc)
            return proc

        at_removal = {}

        def rmtree(path, *args, **kwargs):
            try:
                at_removal["helper ended"] = _ended(self._helper())
            except FileNotFoundError:
                at_removal["helper ended"] = "never started"
            at_removal["unreaped"] = [p.args[1] for p in processes if p.returncode is None]
            at_removal["open pipes"] = [
                p.args[1] for p in processes if not (p.stdout.closed and p.stderr.closed)
            ]
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(gitdata.subprocess, "Popen", popen), mock.patch.object(
            gitdata.shutil, "rmtree", rmtree
        ):
            code, stdout, elapsed = self._main(head, **overrides)
        self.assertEqual(os.listdir(self.remote.runner_temp), [])
        return code, stdout, elapsed, at_removal

    def test_patch_larger_than_the_output_limit_fails(self) -> None:
        head = self.remote.commit("Add data", files={"data.txt": "line\n" * 300_000})
        with mock.patch.object(gitdata, "OUTPUT_LIMIT", 2**20):
            code, stdout, _ = self._main(head)
        self.assertEqual(code, 1, stdout)
        self.assertIn(
            "::error title=post-no-bills::not fully checked: git printed more than 1 MiB",
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
            "::error title=post-no-bills::not fully checked: reading this pull request "
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
        self.assertTrue(failure.startswith("::error title=post-no-bills::git --version failed: eee"))
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

    # A process git starts is not git: stopping git alone leaves it running,
    # holding both pipes and its copy of git's environment.
    _ENDED = {"helper ended": True, "unreaped": [], "open pipes": []}

    def test_helper_of_a_stalled_fetch_is_stopped_at_the_time_limit(self) -> None:
        # A remote helper that never answers, and a git that waits for it.
        script = (
            'if [ "$1" = fetch ]; then\n'
            "  {sleep} 60 &\n"
            "  echo $! > {bin}/helper.pid\n"
            "  {env} > {bin}/helper.env\n"
            "  {sleep} 60\n"
            "fi\n"
            'exec {git} "$@"'
        )
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        with mock.patch.object(gitdata, "TIME_LIMIT", 5):
            code, stdout, elapsed, at_removal = self._watched(
                head, PATH=self._fake_git(script, "sleep", "env", "git")
            )
        self.assertEqual(code, 1, stdout)
        self.assertIn("with git took longer than 5 seconds", stdout)
        self.assertLess(elapsed, 15)
        # The helper did carry the credential, and was gone before the
        # repository was.
        self.assertIn(_HEADER, (self.remote.root / "fake-bin" / "helper.env").read_text())
        self.assertEqual(at_removal, self._ENDED)

    def test_helper_writing_past_the_output_limit_is_stopped(self) -> None:
        script = (
            'if [ "$1" = --version ]; then\n'
            "  ( {head} -c 2000000 /dev/zero; {sleep} 60 ) &\n"
            "  echo $! > {bin}/helper.pid\n"
            "  wait\n"
            "fi\n"
            'exec {git} "$@"'
        )
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        with mock.patch.object(gitdata, "OUTPUT_LIMIT", 2**20), mock.patch.object(gitdata, "TIME_LIMIT", 10):
            code, stdout, elapsed, at_removal = self._watched(
                head, PATH=self._fake_git(script, "head", "sleep", "git")
            )
        self.assertEqual(code, 1, stdout)
        self.assertIn("not fully checked: git printed more than 1 MiB", stdout)
        self.assertLess(elapsed, 8)
        self.assertEqual(at_removal, self._ENDED)

    def test_git_is_stopped_when_a_reader_cannot_start(self) -> None:
        script = (
            'if [ "$1" = --version ]; then\n'
            "  {sleep} 60 &\n"
            "  echo $! > {bin}/helper.pid\n"
            "  {sleep} 60\n"
            "fi\n"
            'exec {git} "$@"'
        )
        real_start = gitdata._Reader.start
        started = []

        def start(reader) -> None:
            started.append(reader)
            if len(started) == 2:
                # Once git has started its helper.
                pid_file = self.remote.root / "fake-bin" / "helper.pid"
                deadline = time.monotonic() + 10
                while not (pid_file.exists() and pid_file.read_text().endswith("\n")):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                raise RuntimeError("can't start new thread")
            real_start(reader)

        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        with mock.patch.object(gitdata._Reader, "start", start):
            code, stdout, elapsed, at_removal = self._watched(
                head, PATH=self._fake_git(script, "sleep", "git")
            )
        self.assertEqual(code, 1, stdout)
        self.assertIn("::error title=post-no-bills::internal error: RuntimeError", stdout)
        self.assertLess(elapsed, 15)
        self.assertEqual(at_removal, self._ENDED)

    def test_overflow_signal_never_follows_the_reap(self) -> None:
        # The stdout reader overflows, and its group signal is held until
        # git is reaped, or for five seconds. Meanwhile the time limit runs
        # out and the main thread cleans up. Once git is reaped its number
        # can name another group, so no signal may come after.
        script = (
            'if [ "$1" = --version ]; then exec {head} -c 2000000 /dev/zero; fi\n'
            'exec {git} "$@"'
        )
        reaped = threading.Event()
        processes: list[subprocess.Popen] = []
        signals: list[tuple[str, bool]] = []
        real_popen, real_killpg = subprocess.Popen, os.killpg

        def popen(argv, *args, **kwargs):
            proc = real_popen(argv, *args, **kwargs)
            # The action's git, not the test's own push.
            if argv[0] == "git" and kwargs.get("start_new_session"):
                processes.append(proc)
                real_wait = proc.wait

                def wait(*wait_args, **wait_kwargs):
                    code = real_wait(*wait_args, **wait_kwargs)
                    reaped.set()
                    return code

                proc.wait = wait
            return proc

        def killpg(group: int, signal_number: int) -> None:
            main_thread = threading.current_thread() is threading.main_thread()
            if not main_thread:
                reaped.wait(5)
            (proc,) = [p for p in processes if p.pid == group]
            signals.append(("main" if main_thread else "reader", proc.returncode is not None))
            # The test itself never signals a number that is free again.
            if proc.returncode is None:
                real_killpg(group, signal_number)

        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        with mock.patch.object(gitdata, "OUTPUT_LIMIT", 2**20), mock.patch.object(
            gitdata, "TIME_LIMIT", 2
        ), mock.patch.object(gitdata.subprocess, "Popen", popen), mock.patch.object(
            gitdata.os, "killpg", killpg
        ):
            code, stdout, elapsed = self._main(head, PATH=self._fake_git(script, "head", "git"))
        self.assertEqual(code, 1, stdout)
        self.assertIn("with git took longer than 2 seconds", stdout)
        self.assertLess(elapsed, 12)
        # Both signals were sent, the reader's too, and neither after the reap.
        self.assertEqual(sorted(signals), [("main", False), ("reader", False)])

    def test_process_that_left_the_group_cannot_hold_the_run(self) -> None:
        # Out of the group's reach, it keeps both pipes open after git ends.
        # The readers are stopped, and only then are the pipes closed.
        script = (
            'if [ "$1" = --version ]; then\n'
            "  {python} -c 'import os, time; os.setsid(); time.sleep(60)' &\n"
            "  echo $! > {bin}/helper.pid\n"
            "  exit 0\n"
            "fi\n"
            'exec {git} "$@"'
        ).replace("{python}", sys.executable)
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        with mock.patch.object(gitdata, "TIME_LIMIT", 3):
            code, stdout, elapsed, at_removal = self._watched(
                head, PATH=self._fake_git(script, "git")
            )
        self.assertEqual(code, 1, stdout)
        self.assertIn("with git took longer than 3 seconds", stdout)
        self.assertLess(elapsed, 10)
        self.assertEqual((at_removal["unreaped"], at_removal["open pipes"]), ([], []))

    def _peak(self, head: str) -> tuple[int, str, int]:
        """_main with the record limit at 10,000, and the most memory Python
        held meanwhile."""
        tracemalloc.start()
        try:
            with mock.patch.object(gitdata, "RECORD_LIMIT", 10_000):
                code, stdout, _ = self._main(head)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        return code, stdout, peak

    _TOO_MANY = "::error title=post-no-bills::not fully checked: git printed more than 10,000 lines"

    def test_many_short_lines_stop_before_they_are_parsed(self) -> None:
        # 400 kB of patch. As lines and records it would take over 10 MB.
        head = self.remote.commit("Add data", files={"data.txt": "a\n" * 200_000})
        code, stdout, peak = self._peak(head)
        self.assertEqual(code, 1, stdout)
        self.assertIn(self._TOO_MANY, stdout)
        self.assertLess(peak, 4 * 2**20)

    def test_many_short_lines_after_a_binary_file_stop_too(self) -> None:
        # The patch says only "Binary files differ"; git's reading of the
        # new file on its own has the lines.
        base = self.remote.commit("Base", files={"x.png": b"PNG\0"})
        self.remote.push(base, "refs/heads/main")
        self.remote.base = base
        head = self.remote.commit("Replace", files={"x.png": "a\n" * 200_000})
        code, stdout, peak = self._peak(head)
        self.assertEqual(code, 1, stdout)
        self.assertIn(self._TOO_MANY, stdout)
        self.assertLess(peak, 4 * 2**20)

    def test_lines_of_a_commit_message_count_too(self) -> None:
        head = self.remote.commit("Add x\n\n" + "m\n" * 20_000, files={"x.txt": "x\n"})
        code, stdout, _ = self._peak(head)
        self.assertEqual(code, 1, stdout)
        self.assertIn(self._TOO_MANY, stdout)


class _Canned:
    """A repository whose git prints fixed output: the raw listing, the
    patch between base and head, and git's reading of a new blob. It counts
    records as the real one does."""

    charge = gitdata._Repository.charge

    def __init__(self, raw: bytes, patch: bytes, new_side: bytes = b"") -> None:
        self._raw = raw
        self._patch = patch
        self._new_side = new_side
        self._records_left = gitdata.RECORD_LIMIT

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


def _raw(
    status: str,
    old_mode: str,
    new_mode: str,
    path: bytes,
    blob: str = _BLOB,
    old_blob: str | None = None,
) -> bytes:
    """One raw listing entry. The old blob is another one than the new,
    unless given."""
    if old_blob is None:
        old_blob = "0" * 40 if status == "A" else "2" * 40
    return f":{old_mode} {new_mode} {old_blob} {blob} {status}".encode() + b"\0" + path + b"\0"


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
        (changed,) = gitdata._changed_files(repo, "b" * 40, "h" * 40, gitdata.BINARY_EXTENSIONS)
        self.assertEqual((changed.kind, changed.added), (gitdata.TEXT, ((1, "clean"), (2, "more"))))

    def test_truncated_patch_fails(self) -> None:
        for last in ((b"+clean",), ()):
            repo = _Canned(
                _raw("A", "000000", "100644", b"x.md"),
                _rows(b"diff --git a/x.md b/x.md", b"@@ -0,0 +1,2 @@", *last),
            )
            with self.subTest(rows=len(last)), self.assertRaisesRegex(gitdata.GitError, "cannot parse"):
                gitdata._changed_files(repo, "b" * 40, "h" * 40, gitdata.BINARY_EXTENSIONS)

    def _mode_change(
        self, new_side: bytes, blob: str = _BLOB, old_blob: str | None = None
    ) -> gitdata.ChangedFile:
        """A change git prints without a hunk. Only the mode changed when the
        blob stayed the same, which it does unless old_blob says otherwise."""
        repo = _Canned(
            _raw("M", "100644", "100755", b"x.md", blob, blob if old_blob is None else old_blob),
            _rows(b"diff --git a/x.md b/x.md", b"old mode 100644", b"new mode 100755"),
            new_side,
        )
        (changed,) = gitdata._changed_files(repo, "b" * 40, "h" * 40, gitdata.BINARY_EXTENSIONS)
        return changed

    def test_content_change_without_a_hunk_fails(self) -> None:
        # git's reading of the new blob is complete; the patch between the
        # two sides says nothing about content that did change.
        header = f"diff --git a/{_EMPTY_BLOB} b/{_BLOB}".encode()
        whole = _rows(header, b"@@ -0,0 +1 @@", b"+changed")
        cases = {
            "mode and content changed": lambda: self._mode_change(whole, old_blob="2" * 40),
            "content changed, header only": lambda: gitdata._changed_files(
                _Canned(
                    _raw("M", "100644", "100644", b"x.md"),
                    _rows(b"diff --git a/x.md b/x.md"),
                    whole,
                ),
                "b" * 40,
                "h" * 40,
                gitdata.BINARY_EXTENSIONS,
            ),
        }
        for name, read in cases.items():
            with self.subTest(name), self.assertRaisesRegex(gitdata.GitError, "cannot parse the diff"):
                read()

    def test_raw_listing_with_an_invalid_old_object_fails(self) -> None:
        # Its patch is complete, so only the old object is wrong.
        patch = _rows(b"diff --git a/x.md b/x.md", b"@@ -1 +1 @@", b"-old", b"+new")
        for old_blob, fails in (("2" * 40, False), ("z" * 40, True)):
            repo = _Canned(_raw("M", "100644", "100644", b"x.md", old_blob=old_blob), patch)
            with self.subTest(old_blob=old_blob):
                if fails:
                    with self.assertRaisesRegex(gitdata.GitError, "cannot parse the diff"):
                        gitdata._changed_files(repo, "b" * 40, "h" * 40, gitdata.BINARY_EXTENSIONS)
                else:
                    (changed,) = gitdata._changed_files(repo, "b" * 40, "h" * 40, gitdata.BINARY_EXTENSIONS)
                    self.assertEqual(changed.added, ((1, "new"),))

    def _binary_before(self, new_side: bytes) -> gitdata.ChangedFile:
        repo = _Canned(
            _raw("M", "100644", "100644", b"x.png"),
            _rows(b"diff --git a/x.png b/x.png", b"Binary files a/x.png and b/x.png differ"),
            new_side,
        )
        (changed,) = gitdata._changed_files(repo, "b" * 40, "h" * 40, gitdata.BINARY_EXTENSIONS)
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
        header = f"diff --git a/{_EMPTY_BLOB} b/{_BLOB}".encode()
        index = f"index {_EMPTY_BLOB[:7]}..{_BLOB[:7]} 100644".encode()
        cases = {
            "another blob": _rows(f"diff --git a/{_EMPTY_BLOB} b/{'3' * 40}".encode(), b"@@ -0,0 +1 @@", b"+ok"),
            "truncated": _rows(header, b"@@ -0,0 +1,2 @@", b"+ok"),
            "nothing": b"",
            # The name of the right blob, and not a word about its content.
            "header only": _rows(header),
            "header lines only": _rows(header, index, b"--- a/x", b"+++ b/x"),
            "a removed line": _rows(header, b"@@ -1 +1 @@", b"-old", b"+ok"),
            "not from the first line": _rows(header, b"@@ -0,0 +2 @@", b"+ok"),
        }
        for read in (self._mode_change, self._binary_before):
            for name, new_side in cases.items():
                with self.subTest(read.__name__, case=name), self.assertRaisesRegex(
                    gitdata.GitError, "cannot parse the diff"
                ):
                    read(new_side)

    _DELETION = (
        b"diff --git a/x.md b/x.md",
        b"deleted file mode 120000",
        b"@@ -1 +0,0 @@",
        b"-target",
        b"\\ No newline at end of file",
    )

    def _created(self, status: str, blob: str, *creation: bytes) -> gitdata.ChangedFile:
        """A new file, or a type change from a symlink, whose creation
        section holds these rows after its header."""
        old_mode = "000000" if status == "A" else "120000"
        before = self._DELETION if status == "T" else ()
        repo = _Canned(
            _raw(status, old_mode, "100644", b"x.md", blob),
            _rows(*before, b"diff --git a/x.md b/x.md", *creation),
        )
        (changed,) = gitdata._changed_files(repo, "b" * 40, "h" * 40, gitdata.BINARY_EXTENSIONS)
        return changed

    def test_creation_that_says_nothing_about_a_file_fails(self) -> None:
        cases = {
            # The right name for a blob that is not empty, and nothing else.
            "header only": (),
            "header lines only": (b"new file mode 100644", b"index 0000000..1111111"),
            "a removed line": (b"@@ -1 +1 @@", b"-old", b"+ok"),
            "not from the first line": (b"@@ -0,0 +2 @@", b"+ok"),
        }
        for status in ("A", "T"):
            for name, rows in cases.items():
                with self.subTest(status=status, case=name), self.assertRaisesRegex(
                    gitdata.GitError, "cannot parse the diff"
                ):
                    self._created(status, _BLOB, *rows)

    def test_creation_of_the_empty_blob_is_empty_text(self) -> None:
        for status in ("A", "T"):
            with self.subTest(status=status):
                changed = self._created(status, _EMPTY_BLOB, b"new file mode 100644")
                self.assertEqual((changed.kind, changed.added), (gitdata.TEXT, ()))

    def test_new_side_that_is_the_empty_blob_is_empty_text(self) -> None:
        changed = self._mode_change(b"", blob=_EMPTY_BLOB)
        self.assertEqual((changed.kind, changed.added), (gitdata.TEXT, ()))


if __name__ == "__main__":
    unittest.main()
