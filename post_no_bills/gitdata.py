"""Fetch the pull request into a temporary bare repository and read it.

Nothing from the pull request runs or configures git: there is no checkout,
no index, no hooks, no info/attributes, and HEAD stays unborn. Every git call
gets an environment built from scratch, so no GIT_* variable and no global or
system config or attribute file on the runner changes what git does.
"""

from __future__ import annotations

import base64
import os
import posixpath
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from typing import IO, Callable

from .event import Settings

_SHA = re.compile(r"[0-9a-f]{40}")
_LOG_FORMAT = "%H%x00%ae%x00%ce%x00%B"
_LOG_FIELDS = 4
_VERSION = re.compile(r"git version (\d+)\.(\d+)")
_MINIMUM_VERSION = (2, 31)
_NO_GIT = "git is not on PATH. This action needs git 2.31 or newer."
_OLD_GIT = "This action needs git 2.31 or newer."

# What one run may spend on git. Running out fails the check in either mode:
# a pull request read in part is not clean. The output limit counts stdout of
# every git call together, while it is read. 64 MiB is far above the -U0 patch
# of a regenerated lockfile, and the Unicode check scans that much text in
# about half a minute at worst (see rules.WORK_LIMIT). Bytes alone do not
# bound what parsing makes of them, since every line becomes objects of its
# own: two million empty added lines, 4 MB of patch, took about 300 MB to
# parse and decode, so 64 MiB of them would take gigabytes. The lines and
# fields of every output are therefore counted, again for the whole run,
# before anything splits it. Two million is several times the patch of a
# regenerated lockfile. The time limit covers the fetch of the full history
# and every other git call; a stuck git would otherwise hold the job until its
# own timeout, six hours by default. Of stderr only the start is kept, since
# only an error message comes from it.
OUTPUT_LIMIT = 64 * 2**20
RECORD_LIMIT = 2_000_000
TIME_LIMIT = 600
_ERROR_LIMIT = 4096
_CHUNK = 65536
# How often a reader looks up from an idle pipe to see whether it is stopped,
# and how long it gets to see the end of a pipe once git's group is gone.
_POLL = 0.1
_GRACE = 2
_TOO_MUCH = (
    "not fully checked: git printed more than {limit} MiB for this pull request, "
    "the most this action reads"
)
_TOO_MANY = (
    "not fully checked: git printed more than {limit:,} lines for this pull request, "
    "the most this action parses"
)
_TOO_SLOW = (
    "not fully checked: reading this pull request with git took longer than "
    "{limit} seconds, the most this action allows"
)

# Both readings of the changed files share these options. No rename
# detection, so moved content counts as added. T keeps a symlink or submodule
# that became a regular file. No external diff driver and no textconv.
_DIFF = (
    "diff",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--diff-filter=ACMRT",
)
_UNPARSABLE = "cannot parse the diff"
_DIFF_HEADER = b"diff --git "
_HUNK = re.compile(rb"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_MODE = re.compile(r"[0-7]{6}")
# Without rename detection git reports no R or C, which would carry two paths.
_STATUSES = frozenset("AMT")
_SUBMODULE_MODE = "160000"
_C_ESCAPES = {
    b"a": 0x07, b"b": 0x08, b"t": 0x09, b"n": 0x0A, b"v": 0x0B, b"f": 0x0C,
    b"r": 0x0D, b'"': 0x22, b"\\": 0x5C,
}

# What a changed file turned out to be.
TEXT = "text"
ALLOWED_BINARY = "allowed binary"
REJECTED_BINARY = "rejected binary"
UNDECODABLE = "undecodable"
SUBMODULE = "submodule"

# Formats that are binary by nature. Git calls a file binary when it holds a
# NUL byte, which any text file can be made to hold, so a binary file with any
# other extension cannot be scanned and fails the check. A caller can add
# extensions; _read joins them to these once.
BINARY_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "ico", "pdf", "zip", "gz",
    "woff", "woff2", "ttf", "otf", "mp4", "mov", "mp3", "wav",
})


class GitError(Exception):
    """The pull request could not be read. The message carries no credential."""


@dataclass(frozen=True)
class Commit:
    sha: str
    author_email: str
    committer_email: str
    message: str


@dataclass(frozen=True)
class ChangedFile:
    """A file the pull request adds or changes, compared with the merge base.

    kind is one of TEXT, ALLOWED_BINARY, REJECTED_BINARY, UNDECODABLE and
    SUBMODULE, and describes the new side: git's reading of the new file, not
    of the pair. Only a TEXT file has added lines: (line number in the new
    file, text without its line ending). When the old side was binary every
    line of the new file is added. When only the mode changed nothing is
    added, and TEXT means the whole new file is valid UTF-8."""

    path: str
    status: str
    old_mode: str
    new_mode: str
    kind: str
    added: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class PullRequest:
    commits: list[Commit]
    merge_base: str
    files: list[ChangedFile]


@dataclass
class _Section:
    """One "diff --git" section of a patch."""

    header: bytes
    binary: bool = False
    in_hunks: bool = False
    added: list[tuple[int, bytes]] = field(default_factory=list)
    removed: int = 0


def encode_credential(token: str) -> str:
    """The Basic credential for the job token, as actions/checkout sends it."""
    return base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")


class _Reader(threading.Thread):
    """Drains one pipe of a git process and keeps at most limit bytes of it.
    With overflow set, more than limit is an error and stops git at once;
    without, the rest is read and dropped so git never blocks on a full pipe.
    It reads the descriptor itself, never through the pipe's buffer, and
    only once the descriptor is readable, so it can always be stopped and the
    pipe closed after it."""

    def __init__(self, pipe: IO[bytes], limit: int, overflow: Callable[[], None] | None = None):
        super().__init__(daemon=True)
        self._fd = pipe.fileno()
        self._limit = limit
        self._overflow = overflow
        self._stopping = threading.Event()
        self._chunks: list[bytes] = []
        self._size = 0
        self.cut = False
        self.exceeded = False
        # Set only at the end of the stream. Anything else is a partial read.
        self.complete = False
        self.start()

    def run(self) -> None:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self._fd, selectors.EVENT_READ)
                while not self._stopping.is_set():
                    if not selector.select(_POLL):
                        continue
                    chunk = os.read(self._fd, _CHUNK)
                    if not chunk:
                        self.complete = True
                        return
                    room = self._limit - self._size
                    if room > 0:
                        self._chunks.append(chunk[:room])
                        self._size += min(room, len(chunk))
                    if len(chunk) > room:
                        self.cut = True
                    if self.cut and self._overflow is not None and not self.exceeded:
                        self.exceeded = True
                        self._overflow()
        except (OSError, ValueError):
            return

    def finish(self) -> None:
        """Give the reader a moment to see the end of a pipe that nobody
        writes to any more, then stop it."""
        self.join(_GRACE)
        self._stopping.set()
        self.join()

    @property
    def data(self) -> bytes:
        return b"".join(self._chunks)


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill git and every process it started, which share its group. Called
    by the main thread and by the stdout reader on overflow, both before git
    is reaped, so its number names no other group yet."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Nothing is left running. macOS answers EPERM for a group whose
        # only member is git's unreaped exit.
        pass


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
        self._output_left = OUTPUT_LIMIT
        self._records_left = RECORD_LIMIT
        self._deadline = time.monotonic() + TIME_LIMIT

    def environment(self, *, authenticated: bool = False) -> dict[str, str]:
        """The whole environment of a git call. The credential travels as
        runtime config here, never on the command line, and only to fetch."""
        # fetch otherwise starts a detached "git maintenance run --auto" in
        # the temporary repository, which outlives the call, can still write
        # while the repository is removed, and inherits this environment.
        config = [
            ("core.hooksPath", self.hooks),
            ("core.quotepath", "off"),
            ("maintenance.auto", "false"),
        ]
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
        """Run git within what is left of the run's output and time limits,
        or raise GitError. Both limits are enforced while git runs."""
        too_slow = _TOO_SLOW.format(limit=TIME_LIMIT)
        if time.monotonic() >= self._deadline:
            raise GitError(too_slow)
        try:
            # A group of its own, so that what git starts, such as the remote
            # helper that carries the credential, can be stopped with it.
            proc = subprocess.Popen(
                ["git", *args],
                env=self.environment(authenticated=authenticated),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise GitError(_NO_GIT) from None
        except OSError as error:
            raise GitError(
                f"git {args[0]} could not start: {type(error).__name__}"
            ) from None
        readers: list[_Reader] = []
        try:
            readers.append(
                _Reader(proc.stdout, self._output_left, overflow=partial(_kill_group, proc))
            )
            readers.append(_Reader(proc.stderr, _ERROR_LIMIT))
            # Both pipes end only when git and every process that holds them
            # have ended, so the end of the pipes is what is waited for.
            for reader in readers:
                reader.join(max(0, self._deadline - time.monotonic()))
            timed_out = any(reader.is_alive() for reader in readers)
        finally:
            # On every way out, in this order: nothing git started is left
            # running, the readers have stopped, git is reaped, and only then
            # are the pipes closed. The caller removes the repository after.
            # A stopped reader can no longer kill the group on overflow, so
            # no signal goes out once git's number is free for reuse.
            _kill_group(proc)
            for reader in readers:
                reader.finish()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        stdout, stderr = readers
        if timed_out:
            raise GitError(too_slow)
        if stdout.exceeded:
            raise GitError(_TOO_MUCH.format(limit=OUTPUT_LIMIT // 2**20))
        if not (stdout.complete and stderr.complete):
            raise GitError(f"git {args[0]} failed: its output could not be read")
        output = stdout.data
        self._output_left -= len(output)
        errors = stderr.data
        if stderr.cut:
            # A secret cut off at the end could not be redacted whole.
            errors = errors[: max(0, len(errors) - max(len(secret) for secret in self._secrets))]
        return subprocess.CompletedProcess(proc.args, proc.returncode, output, errors)

    def charge(self, output: bytes) -> bytes:
        """Count the lines and fields of output against what is left of the
        run's record limit, before anything splits it, and return it."""
        self._records_left -= output.count(b"\n") + output.count(b"\0") + 1
        if self._records_left < 0:
            raise GitError(_TOO_MANY.format(limit=RECORD_LIMIT))
        return output

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
    extensions = BINARY_EXTENSIONS | settings.policy.additional_binary_extensions
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
    shas = repo.charge(repo.git("rev-list", revision_range, "--")).decode().split()
    # Head equal to base, or head already contained in base. Nothing to read
    # is not proof of a clean pull request, so it fails.
    if not shas:
        raise GitError("no commits between base and head")

    commits = _commits(repo, revision_range)
    if sorted(commit.sha for commit in commits) != sorted(shas):
        raise GitError("git log and git rev-list disagree about the commits to check")
    diff_base = merge_base.stdout.decode().strip()
    # The pull request is not blamed for what its base branch added.
    files = _changed_files(repo, diff_base, settings.head_sha, extensions)
    return PullRequest(commits, diff_base, files)


def _commits(repo: _Repository, revision_range: str) -> list[Commit]:
    # The lines of each message count too: the checks split it into them.
    raw = repo.charge(
        repo.git(
            "log",
            "-z",
            "--no-show-signature",
            "--no-color",
            "--encoding=UTF-8",
            f"--format={_LOG_FORMAT}",
            revision_range,
            "--",
        )
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


def _changed_files(
    repo: _Repository, base: str, head: str, extensions: frozenset[str]
) -> list[ChangedFile]:
    """Every added or changed file with its added lines. The raw listing
    names each file, its status, its modes and its new blob exactly; the patch
    supplies the lines. A type change prints as a deletion followed by a
    creation, so it owns two sections of the patch. extensions are the
    formats that may be binary."""
    entries = _raw_entries(
        repo.charge(repo.git(*_DIFF, "--raw", "-z", "--no-abbrev", base, head, "--"))
    )
    sections = _sections(
        repo.charge(
            repo.git(*_DIFF, "-U0", "--src-prefix=a/", "--dst-prefix=b/", base, head, "--")
        )
    )
    empty_blob = None

    def empty() -> str:
        nonlocal empty_blob
        if empty_blob is None:
            empty_blob = _write_empty_blob(repo)
        return empty_blob

    def new_side(blob: str) -> _Section:
        return _new_side(repo, empty(), blob)

    files = []
    index = 0
    for status, old_mode, new_mode, old_blob, blob, path in entries:
        group = []
        while index < len(sections) and _names_path(sections[index].header, path):
            group.append(sections[index])
            index += 1
        if len(group) != (2 if status == "T" else 1):
            raise GitError(_UNPARSABLE)
        # A changed file without a hunk or a binary verdict is a mode change,
        # which keeps the same blob. With another blob the content changed
        # and the patch does not show it.
        change = group[-1]
        if (
            status == "M"
            and not (change.binary or change.in_hunks)
            and old_blob != blob
        ):
            raise GitError(_UNPARSABLE)
        # A new file, or a type change's creation, is compared with nothing
        # already. Without a hunk it has to be the empty blob. A submodule's
        # commit is no blob.
        creation = group[-1]
        if (
            status != "M"
            and new_mode != _SUBMODULE_MODE
            and not _whole(creation)
            and (creation.in_hunks or blob != empty())
        ):
            raise GitError(_UNPARSABLE)
        files.append(
            _classify(
                path, status, old_mode, new_mode, group[-1], partial(new_side, blob), extensions
            )
        )
    if index != len(sections):
        raise GitError(_UNPARSABLE)
    return files


def _raw_entries(raw: bytes) -> list[tuple[str, str, str, str, str, bytes]]:
    """(status, old mode, new mode, old object, new object, path) from
    diff --raw -z."""
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise GitError(_UNPARSABLE)
    entries = []
    for meta, path in zip(fields[::2], fields[1::2], strict=True):
        parts = meta.decode("ascii", "replace").split(" ")
        if (
            len(parts) != 5
            or not parts[0].startswith(":")
            or not _MODE.fullmatch(parts[0][1:])
            or not _MODE.fullmatch(parts[1])
            or not _SHA.fullmatch(parts[2])
            or not _SHA.fullmatch(parts[3])
            or parts[4] not in _STATUSES
            or not path
        ):
            raise GitError(_UNPARSABLE)
        entries.append((parts[4], parts[0][1:], parts[1], parts[2], parts[3], path))
    return entries


def _write_empty_blob(repo: _Repository) -> str:
    """The empty blob, written into the temporary repository so that git can
    compare a new file with nothing. stdin is empty for every git call."""
    blob = repo.git("hash-object", "-w", "--stdin").decode("ascii", "replace").strip()
    if not _SHA.fullmatch(blob):
        raise GitError(_UNPARSABLE)
    return blob


def _new_side(repo: _Repository, empty_blob: str, blob: str) -> _Section:
    """git's reading of a new file on its own: the empty blob against it.
    Binary if git calls it binary, otherwise every line of it as added. The
    blob comes from the raw listing, so it is part of the fetched head."""
    patch = repo.git(*_DIFF, "-U0", "--src-prefix=a/", "--dst-prefix=b/", empty_blob, blob)
    sections = _sections(repo.charge(patch))
    if not sections and blob == empty_blob:
        return _Section(b"")
    if len(sections) != 1 or sections[0].header != f"a/{empty_blob} b/{blob}".encode():
        raise GitError(_UNPARSABLE)
    section = sections[0]
    if blob != empty_blob and not _whole(section):
        raise GitError(_UNPARSABLE)
    return section


def _whole(section: _Section) -> bool:
    """Whether a section that compares nothing with a file says what the
    file is. A file that is not empty differs from nothing, so git has to
    call it binary or add every line of it from the first. A header alone
    says neither."""
    return section.binary or (
        bool(section.added)
        and not section.removed
        and all(number == index for index, (number, _) in enumerate(section.added, 1))
    )


def _sections(raw: bytes) -> list[_Section]:
    """Split a -U0 patch into its sections and collect each one's added
    lines. The hunk header says how many lines follow, so a content line that
    starts with "+++ " or "diff --git " is still content."""
    sections: list[_Section] = []
    old = new = number = 0
    rows = raw.split(b"\n")
    if rows and rows[-1] == b"":
        rows.pop()
    for row in rows:
        if old or new:
            if row.startswith(b"+") and new:
                sections[-1].added.append((number, row[1:]))
                number += 1
                new -= 1
            elif row.startswith(b"-") and old:
                sections[-1].removed += 1
                old -= 1
            elif not row.startswith(b"\\"):  # "\ No newline at end of file"
                raise GitError(_UNPARSABLE)
            continue
        if row.startswith(_DIFF_HEADER):
            sections.append(_Section(row[len(_DIFF_HEADER) :]))
            continue
        if not sections:
            raise GitError(_UNPARSABLE)
        section = sections[-1]
        hunk = _HUNK.match(row)
        if hunk:
            old = 1 if hunk[1] is None else int(hunk[1])
            number = int(hunk[2])
            new = 1 if hunk[3] is None else int(hunk[3])
            # git prints no hunk that changes nothing.
            if not (old or new):
                raise GitError(_UNPARSABLE)
            section.in_hunks = True
        elif section.in_hunks:
            if not row.startswith(b"\\"):
                raise GitError(_UNPARSABLE)
        elif row.startswith(b"Binary files ") and row.endswith(b" differ"):
            section.binary = True
        # Anything else before the first hunk is an extended header line.
    # The last hunk promised lines that never came: its additions are partial.
    if old or new:
        raise GitError(_UNPARSABLE)
    return sections


def _names_path(header: bytes, path: bytes) -> bool:
    """Whether "diff --git <header>" is the section of this path. Without
    renames both sides name the same path, C-quoted when it needs quoting."""
    if header == b"a/" + path + b" b/" + path:
        return True
    if not header.startswith(b'"'):
        return False
    old, end = _unquote(header, 0)
    if header[end : end + 2] != b' "':
        return False
    new, end = _unquote(header, end + 1)
    return end == len(header) and old == b"a/" + path and new == b"b/" + path


def _unquote(raw: bytes, start: int) -> tuple[bytes, int]:
    """The C-quoted string that starts at raw[start], and the index after
    its closing quote."""
    value = bytearray()
    index = start + 1
    while index < len(raw):
        char = raw[index : index + 1]
        if char == b'"':
            return bytes(value), index + 1
        if char == b"\\":
            octal = raw[index + 1 : index + 4]
            if len(octal) == 3 and all(0x30 <= digit <= 0x37 for digit in octal):
                value.append(int(octal, 8))
                index += 4
                continue
            escape = _C_ESCAPES.get(raw[index + 1 : index + 2])
            if escape is None:
                raise GitError(_UNPARSABLE)
            value.append(escape)
            index += 2
            continue
        value += char
        index += 1
    raise GitError(_UNPARSABLE)


def _classify(
    raw_path: bytes,
    status: str,
    old_mode: str,
    new_mode: str,
    section: _Section,
    read_new_side: Callable[[], _Section],
    extensions: frozenset[str],
) -> ChangedFile:
    """What the new side of a file is. section is the file's own section, or
    a type change's creation, whose old side is empty."""
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError:
        path = raw_path.decode("utf-8", "replace")
        return ChangedFile(path, status, old_mode, new_mode, UNDECODABLE)
    if new_mode == _SUBMODULE_MODE:
        # A commit of another repository. Its "Subproject commit" line is
        # git's own text, not content.
        return ChangedFile(path, status, old_mode, new_mode, SUBMODULE)
    mode_only = False
    if status == "M" and (section.binary or not section.in_hunks):
        # A patch between two sides is binary when either side is, and has
        # no hunk when only the mode changed. Neither says what the new side
        # is, so git reads that side on its own.
        mode_only = not section.binary
        section = read_new_side()
    if section.binary:
        extension = posixpath.splitext(path)[1][1:].lower()
        kind = ALLOWED_BINARY if extension in extensions else REJECTED_BINARY
        return ChangedFile(path, status, old_mode, new_mode, kind)
    try:
        added = tuple(
            (number, text.decode("utf-8").rstrip("\r")) for number, text in section.added
        )
    except UnicodeDecodeError:
        return ChangedFile(path, status, old_mode, new_mode, UNDECODABLE)
    # A mode change adds no line, even though the whole file was decoded.
    return ChangedFile(path, status, old_mode, new_mode, TEXT, () if mode_only else added)
