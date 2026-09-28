from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from agent_guardrails import gitdata
from agent_guardrails.main import main

from .support import TOKEN, RemoteTestCase

_CREDENTIAL = gitdata.encode_credential(TOKEN)
_HEADER_KEY = "http.https://github.com/.extraheader"
_HEADER = f"AUTHORIZATION: basic {_CREDENTIAL}"
_ISOLATED_KEYS = {
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "LANG",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_ATTR_NOSYSTEM",
    "GIT_TERMINAL_PROMPT",
    "GIT_DIR",
    "GIT_CONFIG_COUNT",
}


class _Recorder(io.StringIO):
    """stdout that records each write in the same timeline as git calls."""

    def __init__(self, timeline: list) -> None:
        super().__init__()
        self._timeline = timeline

    def write(self, text: str) -> int:
        self._timeline.append(("out", text))
        return super().write(text)


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
        if argv[1] == "fetch":
            stderr = (
                f"fatal: unable to access 'https://x-access-token:{TOKEN}@github.com/'\n"
                f"sent {_HEADER}\n\x1b[2K::error::spoofed"
            ).encode()
            return subprocess.CompletedProcess(argv, 128, b"", stderr)
        if argv[1] == "check-ref-format":
            return subprocess.CompletedProcess(argv, 0, b"main\n", b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def _run(self) -> tuple[int, str, str]:
        stdout, stderr = _Recorder(self.timeline), io.StringIO()
        with mock.patch.object(gitdata.subprocess, "run", self._fake_git), contextlib.redirect_stderr(stderr):
            code = main(self.environ, stdout=stdout)
        return code, stdout.getvalue(), stderr.getvalue()

    def _calls(self) -> list:
        return [entry for entry in self.timeline if entry[0] == "git"]

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
            count = int(env["GIT_CONFIG_COUNT"])
            expected = _ISOLATED_KEYS | {
                f"GIT_CONFIG_{kind}_{index}" for kind in ("KEY", "VALUE") for index in range(count)
            }
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


if __name__ == "__main__":
    unittest.main()
