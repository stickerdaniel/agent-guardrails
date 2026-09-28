"""Real git repositories and the real entrypoint, for the tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_PY = ROOT / "run.py"
# Synthetic. Never put a real credential in a test.
TOKEN = "ghs_FAKE0123456789abcdefghijklmnopqrstuv"
HUMAN = "jane@example.com"
CLAUDE = "noreply@anthropic.com"


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

    def commit(self, message: str, *, files: dict[str, str] | None = None, **env: str) -> str:
        for name, content in (files or {}).items():
            (self.work / name).write_text(content, encoding="utf-8")
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
        body: str | None = "",
        login: str = "jane",
        number: int = 1,
        base_ref: str = "main",
    ) -> Path:
        payload = {
            "pull_request": {
                "number": number,
                "body": body,
                "user": {"login": login},
                "base": {"ref": base_ref, "sha": base or self.base},
                "head": {"sha": head},
            }
        }
        path = self.root / "event.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def environment(self, event_path: Path, **overrides: str) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "CA_REQUIRE_MODEL_ATTRIBUTION": "false",
            "CA_HIDDEN_UNICODE": "error",
            "CA_TOKEN": TOKEN,
            "CA_SERVER_URL": self.server_url,
            "CA_REPOSITORY": self.repository,
            "CA_EVENT_NAME": "pull_request_target",
            "GITHUB_EVENT_PATH": str(event_path),
            "RUNNER_TEMP": str(self.runner_temp),
        }
        env.update(overrides)
        return env

    def run_action(self, event_path: Path, **overrides: str) -> subprocess.CompletedProcess:
        """The entrypoint exactly as action.yml starts it."""
        return subprocess.run(
            [sys.executable, "-I", str(RUN_PY)],
            env=self.environment(event_path, **overrides),
            capture_output=True,
            text=True,
            timeout=120,
        )


class RemoteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.remote = Remote(Path(directory.name).resolve())
