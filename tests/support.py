"""Real git repositories and the real entrypoint, for the tests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from post_no_bills import event

from . import yamlsubset

ROOT = Path(__file__).resolve().parent.parent
RUN_PY = ROOT / "run.py"
# Synthetic. Never put a real credential in a test.
TOKEN = "ghs_FAKE0123456789abcdefghijklmnopqrstuv"
HUMAN = "jane@example.com"
CLAUDE = "noreply@anthropic.com"
# The default action inputs require it, so a fixture PR body carries it unless
# the test is about attribution.
ATTRIBUTION = "Generated with Claude Opus 5.5 for implementation in Claude Code via T3 Code."
# A3: every variable a git call may see, besides its GIT_CONFIG_KEY_<n> and
# GIT_CONFIG_VALUE_<n> pairs.
GIT_ENVIRONMENT = frozenset({
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "LANG",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_ATTR_NOSYSTEM",
    "GIT_TERMINAL_PROMPT",
    "GIT_DIR",
    "GIT_CONFIG_COUNT",
})


def git_environment(config_count: int) -> set[str]:
    """The exact variable names of a git call with that many config pairs."""
    return set(GIT_ENVIRONMENT) | {
        f"GIT_CONFIG_{kind}_{index}" for kind in ("KEY", "VALUE") for index in range(config_count)
    }


@lru_cache(maxsize=None)
def _defaults() -> tuple[tuple[str, str], ...]:
    inputs = yamlsubset.load((ROOT / "action.yml").read_text(encoding="utf-8"))["inputs"]
    return tuple((name, spec["default"]) for name, spec in inputs.items())


def runner_inputs(given: Mapping[str, str] | None = None) -> dict[str, str]:
    """The inputs context the runner hands a composite action, as measured
    on a hosted runner: every key the caller wrote, in its spelling, unknown
    ones too, then each declared default that no caller key names when case
    is ignored."""
    inputs = dict(given or {})
    written = {name.lower() for name in inputs}
    for name, default in _defaults():
        if name.lower() not in written:
            inputs[name] = default
    return inputs


def policy(given: Mapping[str, str] | None = None) -> event.Policy:
    """The policy the entrypoint builds for a caller's inputs."""
    return event._policy(event._inputs({event.INPUTS: json.dumps(runner_inputs(given))}))

# The workflow commands the action writes itself: the mask, and annotations
# whose properties are escaped, so they hold no raw ":" or ",".
_OWN_COMMAND = re.compile(
    r"::add-mask::[A-Za-z0-9+/=]+"
    r"|::(error|warning) (file=[^:,]*,(line=[0-9]+,)?)?title=[^:,]*::.*"
)


def foreign_commands(output: str) -> list[str]:
    """Lines of output that the Actions runner would read as a workflow
    command the action did not write. actions/runner reads a line as one when,
    after TrimStart, it starts with "::" (ActionCommand.TryParseV2), and when
    "##[" occurs anywhere in it (the legacy TryParse, an unanchored IndexOf)."""
    return [
        line
        for line in output.splitlines()
        if "##[" in line or (line.lstrip().startswith("::") and not _OWN_COMMAND.fullmatch(line))
    ]


def git_env(home: Path, **extra: str) -> dict[str, str]:
    """An environment that keeps the developer's own git config (signing,
    hooks, templates) out of the fixtures."""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Jane",
        "GIT_AUTHOR_EMAIL": HUMAN,
        "GIT_COMMITTER_NAME": "Jane",
        "GIT_COMMITTER_EMAIL": HUMAN,
    }
    env.update(extra)
    return env


class Remote:
    """A base repository at file://<server>/owner/repo.git, a working clone
    to make commits in, and the event payload that describes a pull request."""

    repository = "owner/repo"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.runner_temp = root / "runner"
        self.runner_temp.mkdir()
        server = root / "server"
        self.server_url = server.as_uri()
        self.bare = server / "owner" / "repo.git"
        self.work = root / "work"
        self.git("init", "--bare", "--quiet", str(self.bare), cwd=root)
        self.git("init", "--quiet", str(self.work), cwd=root)
        self.base = self.commit("Initial commit", files={"README.md": "hello\n"})
        self.push(self.base, "refs/heads/main")

    def git(self, *args: str, cwd: Path | None = None, stdin: str | None = None, **env: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd or self.work,
            env=git_env(self.home, **env),
            input=None if stdin is None else stdin.encode(),
            capture_output=True,
        )
        if proc.returncode:
            raise AssertionError(f"git {args[0]} failed: {proc.stderr.decode()}")
        return proc.stdout.decode().strip()

    def commit(
        self, message: str, *, files: dict[str, str | bytes] | None = None, **env: str
    ) -> str:
        for name, content in (files or {}).items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
            self.git("add", name)
        self.git(
            "commit", "--quiet", "--allow-empty", "--no-verify", "--cleanup=verbatim",
            "-F", "-", stdin=message, **env,
        )
        return self.git("rev-parse", "HEAD")

    def push(self, sha: str, ref: str) -> None:
        self.git("push", "--quiet", "--force", str(self.bare), f"{sha}:{ref}")

    def open_pull_request(self, head: str, *, number: int = 1) -> None:
        self.push(head, f"refs/pull/{number}/head")

    def event(
        self,
        *,
        head: str,
        base: str | None = None,
        title: str = "Add x",
        body: str | None = ATTRIBUTION,
        login: str = "jane",
        number: int = 1,
        base_ref: str = "main",
    ) -> Path:
        payload = {
            "pull_request": {
                "number": number,
                "title": title,
                "body": body,
                "user": {"login": login},
                "base": {"ref": base_ref, "sha": base or self.base},
                "head": {"sha": head},
            }
        }
        path = self.root / "event.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def environment(
        self, event_path: Path, inputs: Mapping[str, str] | None = None, **overrides: str
    ) -> dict[str, str]:
        """The step's environment for a caller that wrote these inputs.
        overrides replace variables, CA_INPUTS included."""
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "CA_INPUTS": json.dumps(runner_inputs(inputs)),
            "CA_TOKEN": TOKEN,
            "CA_SERVER_URL": self.server_url,
            "CA_REPOSITORY": self.repository,
            "CA_EVENT_NAME": "pull_request_target",
            "GITHUB_EVENT_PATH": str(event_path),
            "RUNNER_TEMP": str(self.runner_temp),
        }
        env.update(overrides)
        return env

    def run_action(
        self, event_path: Path, inputs: Mapping[str, str] | None = None, **overrides: str
    ) -> subprocess.CompletedProcess:
        """The entrypoint exactly as action.yml starts it."""
        return subprocess.run(
            [sys.executable, "-I", str(RUN_PY)],
            env=self.environment(event_path, inputs, **overrides),
            capture_output=True,
            text=True,
            timeout=120,
        )


class RemoteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.remote = Remote(Path(directory.name).resolve())
