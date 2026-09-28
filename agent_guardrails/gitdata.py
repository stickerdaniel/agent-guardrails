"""Fetch the pull request into a temporary bare repository and read it.

Nothing from the pull request runs or configures git: there is no checkout,
no index, no hooks, no info/attributes, and HEAD stays unborn. Every git call
gets an environment built from scratch, so no GIT_* variable and no global or
system config or attribute file on the runner changes what git does.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable

from .event import Settings

_SHA = re.compile(r"[0-9a-f]{40}")
_LOG_FORMAT = "%H%x00%ae%x00%ce%x00%B"
_LOG_FIELDS = 4
_VERSION = re.compile(r"git version (\d+)\.(\d+)")
_MINIMUM_VERSION = (2, 31)
_NO_GIT = "git is not on PATH. This action needs git 2.31 or newer."
_OLD_GIT = "This action needs git 2.31 or newer."


class GitError(Exception):
    """The pull request could not be read. The message carries no credential."""


@dataclass(frozen=True)
class Commit:
    sha: str
    author_email: str
    committer_email: str
    message: str


@dataclass(frozen=True)
class PullRequest:
    commits: list[Commit]
    merge_base: str


def encode_credential(token: str) -> str:
    """The Basic credential for the job token, as actions/checkout sends it."""
    return base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")


class _Repository:
    def __init__(self, root: str, settings: Settings, credential: str) -> None:
        self.root = root
        self.git_dir = os.path.join(root, "repo.git")
        self.hooks = os.path.join(root, "no-hooks")
        os.mkdir(self.hooks)
        self._path = settings.path
        self._header_key = f"http.{settings.server_url}/.extraheader"
        self._header = f"AUTHORIZATION: basic {credential}"
        self._secrets = (settings.token, credential)

    def environment(self, *, authenticated: bool = False) -> dict[str, str]:
        """The whole environment of a git call. The credential travels as
        runtime config here, never on the command line, and only to fetch."""
        config = [("core.hooksPath", self.hooks), ("core.quotepath", "off")]
        if authenticated:
            config.append((self._header_key, self._header))
        env = {
            "PATH": self._path,
            "HOME": self.root,
            "XDG_CONFIG_HOME": self.root,
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_DIR": self.git_dir,
            "GIT_CONFIG_COUNT": str(len(config)),
        }
        for index, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        return env

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def run(self, *args: str, authenticated: bool = False) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *args],
                env=self.environment(authenticated=authenticated),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError:
            raise GitError(_NO_GIT) from None
        except OSError as error:
            raise GitError(
                f"git {args[0]} could not start: {type(error).__name__}"
            ) from None

    def git(self, *args: str, authenticated: bool = False) -> bytes:
        """Run git and return its output. On failure only git's own stderr,
        redacted, reaches the error; argv and the environment never do."""
        proc = self.run(*args, authenticated=authenticated)
        if proc.returncode != 0:
            detail = self.redact(proc.stderr.decode("utf-8", "replace").strip())
            raise GitError(f"git {args[0]} failed: {detail}")
        return proc.stdout


def fetch_pull_request(
    settings: Settings, credential: str, log: Callable[[str], None]
) -> PullRequest:
    """Fetch base and head from the base repository and read the commits
    between them. Every doubt about the range raises GitError."""
    root = tempfile.mkdtemp(prefix="agent-guardrails-", dir=settings.runner_temp)
    try:
        repo = _Repository(root, settings, credential)
        _require_git(repo, log)
        return _read(repo, settings)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        if os.path.exists(root):
            log(f"could not remove the temporary repository {root}")


def _require_git(repo: _Repository, log: Callable[[str], None]) -> None:
    """The version check is a git call like any other: after the mask, and in
    the environment built from scratch, never the runner's."""
    version = repo.git("--version").decode("utf-8", "replace").strip()
    log(version)
    match = _VERSION.match(version)
    if not match or (int(match[1]), int(match[2])) < _MINIMUM_VERSION:
        raise GitError(_OLD_GIT)


def _read(repo: _Repository, settings: Settings) -> PullRequest:
    repo.git("init", "--bare", "--quiet", "--template=")

    # Run inside the fresh repository: there is no reflog for @{-N} to expand.
    branch = repo.run("check-ref-format", "--branch", settings.base_ref)
    if branch.returncode != 0 or branch.stdout.rstrip(b"\n") != settings.base_ref.encode():
        raise GitError("pull_request.base.ref is not a valid branch name")

    # Always the base repository, also for a pull request from a fork. The
    # full history of exactly two refs: no depth, no filter, no tags.
    repo.git(
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-recurse-submodules",
        f"{settings.server_url}/{settings.repository}.git",
        f"refs/heads/{settings.base_ref}:refs/base",
        f"refs/pull/{settings.number}/head:refs/head",
        authenticated=True,
    )

    fetched_head = repo.git("rev-parse", "--verify", "refs/head^{commit}").decode().strip()
    if fetched_head != settings.head_sha:
        raise GitError("head moved since the event; the run for the new head decides")
    for name, sha in (("base", settings.base_sha), ("head", settings.head_sha)):
        if repo.run("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
            raise GitError(f"the event's {name} commit {sha} is not in the fetched history")

    merge_base = repo.run("merge-base", settings.base_sha, settings.head_sha)
    if merge_base.returncode != 0:
        raise GitError("base and head have no common history")

    revision_range = f"{settings.base_sha}..{settings.head_sha}"
    shas = repo.git("rev-list", revision_range, "--").decode().split()
    # Head equal to base, or head already contained in base. Nothing to read
    # is not proof of a clean pull request, so it fails.
    if not shas:
        raise GitError("no commits between base and head")

    commits = _commits(repo, revision_range)
    if sorted(commit.sha for commit in commits) != sorted(shas):
        raise GitError("git log and git rev-list disagree about the commits to check")
    return PullRequest(commits, merge_base.stdout.decode().strip())


def _commits(repo: _Repository, revision_range: str) -> list[Commit]:
    raw = repo.git(
        "log",
        "-z",
        "--no-show-signature",
        "--no-color",
        "--encoding=UTF-8",
        f"--format={_LOG_FORMAT}",
        revision_range,
        "--",
    )
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % _LOG_FIELDS:
        raise GitError("cannot parse the commit log")
    commits = []
    for index in range(0, len(fields), _LOG_FIELDS):
        sha, author, committer, message = (
            field.decode("utf-8", "replace") for field in fields[index : index + _LOG_FIELDS]
        )
        if not _SHA.fullmatch(sha):
            raise GitError("cannot parse the commit log")
        commits.append(Commit(sha, author, committer, message))
    return commits
