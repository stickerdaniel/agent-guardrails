"""The release gate that .github/workflows/release.yml runs, one command per
step, in the root of a checkout of the pushed tag.

    check            the tag is vX.Y.Z and lightweight, and it names
                     GITHUB_SHA, which is checked out and is the current tip
                     of main on origin
    test             runs the unit tests; none run counts as a failure
    notes <file>     writes the CHANGELOG.md section of the tag's version
    publish <notes>  creates the GitHub release, or finds it published, then
                     reads it back: its tag, its commit, and immutability

Standard library, git and gh only. Every doubt exits 1.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

# Semantic versioning without leading zeros, so that no version can have two
# tags. The workflow's tag filter is a glob and lets v01.2.3 through.
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
MAIN = "refs/heads/main"
_SHA = re.compile(r"[0-9a-f]{40}")


class Refusal(Exception):
    """A reason not to release."""


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise Refusal(f"{name} is not set")
    return value


def _tag() -> str:
    name = _env("GITHUB_REF_NAME")
    if not TAG.fullmatch(name):
        raise Refusal(f"tag {name!r} is not vX.Y.Z")
    ref = _env("GITHUB_REF")
    if ref != f"refs/tags/{name}":
        raise Refusal(f"{ref!r} is not the tag {name}")
    return name


def _sha() -> str:
    sha = _env("GITHUB_SHA")
    if not _SHA.fullmatch(sha):
        raise Refusal(f"GITHUB_SHA {sha!r} is not a commit SHA")
    return sha


def _git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if proc.returncode:
        raise Refusal(f"git {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout


def check() -> None:
    event = _env("GITHUB_EVENT_NAME")
    if event != "push":
        raise Refusal(f"a release starts from a tag push, not {event!r}")
    tag = _tag()
    sha = _sha()
    head = _git("rev-parse", "HEAD").strip()
    if head != sha:
        raise Refusal(f"the checkout is {head}, not {sha}")

    # Ask origin now rather than read what the checkout fetched: main may have
    # moved since, and a tag may have been replaced. ls-remote prints the
    # peeled line of an annotated tag only for a pattern that names it.
    ref = f"refs/tags/{tag}"
    remote: dict[str, str] = {}
    for line in _git("ls-remote", "origin", ref, f"{ref}^{{}}", MAIN).splitlines():
        object_name, _, name = line.partition("\t")
        remote[name] = object_name
    if f"{ref}^{{}}" in remote:
        raise Refusal(f"{tag} is an annotated tag; push a lightweight one")
    if remote.get(ref) != sha:
        raise Refusal(f"{tag} on origin names {remote.get(ref)}, not {sha}")
    if remote.get(MAIN) != sha:
        raise Refusal(f"{sha} is not the current tip of main, {remote.get(MAIN)}")
    print(f"release: {tag} is a lightweight tag on {sha}, the tip of main")


def test() -> None:
    suite = unittest.TestLoader().discover("tests", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise Refusal("the tests failed")
    executed = result.testsRun - len(result.skipped)
    if executed == 0:
        raise Refusal(f"no tests ran: {result.testsRun} found, all of them skipped")
    print(f"release: {executed} tests passed, {len(result.skipped)} skipped")


def notes(path: str) -> None:
    version = _tag()[1:]
    heading = re.compile(rf"## \[{re.escape(version)}\] - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}")
    lines = Path("CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if heading.fullmatch(line)]
    if len(starts) != 1:
        raise Refusal(
            f"CHANGELOG.md has {len(starts)} headings '## [{version}] - YYYY-MM-DD', not one"
        )
    section: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line.startswith("## "):
            break
        section.append(line)
    text = "\n".join(section).strip("\n")
    if not text.strip():
        raise Refusal(f"the CHANGELOG.md section for {version} is empty")
    Path(path).write_text(text + "\n", encoding="utf-8")
    print(text)


def _api(path: str) -> dict | None:
    """The JSON object GitHub answers for path, or None for a 404. gh prints
    the error body on stdout and exits 1; nothing else proves absence."""
    proc = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    try:
        body = json.loads(proc.stdout)
    except ValueError:
        body = None
    if proc.returncode == 0 and isinstance(body, dict):
        return body
    if proc.returncode and isinstance(body, dict) and body.get("status") == "404":
        return None
    raise Refusal(f"gh api {path} failed: {proc.stderr.strip() or proc.stdout.strip()}")


def _verify(repository: str, tag: str, sha: str, release: dict) -> None:
    if release.get("tag_name") != tag:
        raise Refusal(f"the release for {tag} names the tag {release.get('tag_name')!r}")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise Refusal(f"the release {tag} is a draft or a prerelease")
    if release.get("immutable") is not True:
        raise Refusal(f"the release {tag} is not immutable; enable immutable releases")
    ref = _api(f"repos/{repository}/git/ref/tags/{tag}")
    target = (ref or {}).get("object") or {}
    if (ref or {}).get("ref") != f"refs/tags/{tag}" or target.get("type") != "commit":
        raise Refusal(f"the release {tag} does not rest on a lightweight tag")
    if target.get("sha") != sha:
        raise Refusal(f"the release {tag} is on {target.get('sha')}, not {sha}")
    print(f"release: {release.get('html_url')} is immutable and on {sha}")


def publish(notes_path: str) -> None:
    tag = _tag()
    sha = _sha()
    repository = _env("GITHUB_REPOSITORY")
    release = _api(f"repos/{repository}/releases/tags/{tag}")
    if release is None:
        proc = subprocess.run(
            [
                "gh", "release", "create", tag,
                "--repo", repository,
                "--verify-tag",
                "--title", tag,
                "--notes-file", notes_path,
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode:
            raise Refusal(f"gh release create failed: {proc.stderr.strip()}")
        release = _api(f"repos/{repository}/releases/tags/{tag}")
        if release is None:
            raise Refusal(f"the release {tag} was created but cannot be read back")
    else:
        # A rerun after a publication. It passes only for the same commit.
        print(f"::notice::release {tag} already exists; verifying it instead of creating it")
    _verify(repository, tag, sha, release)


_COMMANDS = {"check": check, "test": test, "notes": notes, "publish": publish}


def main(argv: list[str]) -> int:
    command = _COMMANDS.get(argv[0]) if argv else None
    arguments = argv[1:]
    if command is None or len(arguments) != (command.__code__.co_argcount):
        print(f"usage: release.py {{{','.join(_COMMANDS)}}} [file]", file=sys.stderr)
        return 2
    try:
        command(*arguments)
    except Refusal as refusal:
        message = str(refusal).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::release: {message}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
