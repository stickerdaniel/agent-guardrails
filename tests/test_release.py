"""The release gate, run the way release.yml runs it: its steps in order, in a
fresh clone of a synthetic origin with real tags, and a stand-in for gh that
answers the way gh 2.100 answers the GitHub API."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable

from . import yamlsubset
from .support import ROOT, TOKEN, git_env

_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
_HELPER = ROOT / ".github" / "release.py"
_REPOSITORY = "owner/repo"
_CHECKOUT = "actions/checkout@"
_SETUP_PYTHON = "actions/setup-python@"

_CHANGELOG = """\
# Changelog

## [Unreleased]

### Added

- Something not released yet

## [1.2.30] - 2026-09-01

- A version whose name starts like the released one

## [1.2.3] - 2026-09-28

### Added

- The released change

### Fixed

- A released fix

## [1.2.2] - 2026-08-01

- An older release
"""
_SECTION = "### Added\n\n- The released change\n\n### Fixed\n\n- A released fix\n"

_PASSING = "import unittest\n\n\nclass Passing(unittest.TestCase):\n    def test_passes(self):\n        pass\n"
_SKIPPED = (
    "import unittest\n\n\n@unittest.skip('always')\n"
    "class Skipped(unittest.TestCase):\n    def test_skipped(self):\n        pass\n"
)
_FAILING = "import unittest\n\n\nclass Failing(unittest.TestCase):\n    def test_fails(self):\n        self.fail()\n"

# The calls release.py makes, answered from a state file and from the refs of
# the synthetic origin. Errors take the shape gh 2.100 gives them: the API's
# JSON body on stdout, "gh: <message> (HTTP <status>)" on stderr, exit 1.
_FAKE_GH = """\
#!@PYTHON@
import json, os, subprocess, sys

with open(os.environ["FAKE_GH_STATE"], encoding="utf-8") as handle:
    state = json.load(handle)
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")


def answer(body, status=None):
    print(json.dumps(body), end="")
    if status:
        print(f"gh: {body['message']} (HTTP {status})", file=sys.stderr)
    sys.exit(1 if status else 0)


def tag_object(tag):
    listed = subprocess.run(
        ["git", "--git-dir", state["origin"], "for-each-ref",
         "--format=%(objectname) %(objecttype)", "refs/tags/" + tag],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    return listed or None


if os.environ.get("GH_TOKEN") != state["token"]:
    print("gh: set the GH_TOKEN environment variable", file=sys.stderr)
    sys.exit(4)

prefix = "repos/" + state["repository"] + "/"
if len(args) == 2 and args[0] == "api" and args[1].startswith(prefix):
    path = args[1][len(prefix):]
    if state.get("api_status"):
        answer({"message": "Server Error", "status": str(state["api_status"])}, state["api_status"])
    if path.startswith("releases/tags/"):
        if state.get("move_ref_during_lookup") and not state.get("lookup_moved"):
            ref, sha = state["move_ref_during_lookup"]
            subprocess.run(
                ["git", "--git-dir", state["origin"], "update-ref", ref, sha], check=True,
            )
            state["lookup_moved"] = True
            with open(os.environ["FAKE_GH_STATE"], "w", encoding="utf-8") as handle:
                json.dump(state, handle)
        release = state["releases"].get(path[len("releases/tags/"):])
        answer(release) if release else answer({"message": "Not Found", "status": "404"}, 404)
    if path.startswith("git/ref/tags/"):
        tag = path[len("git/ref/tags/"):]
        found = tag_object(tag)
        if not found:
            answer({"message": "Not Found", "status": "404"}, 404)
        answer({"ref": "refs/tags/" + tag, "object": {"sha": found[0], "type": found[1]}})

if args[:2] == ["release", "create"] and len(args) > 2:
    tag, rest, options, flags = args[2], args[3:], {}, set()
    while rest:
        if rest[0] == "--verify-tag":
            flags.add(rest.pop(0))
        elif rest[0] in ("--repo", "--title", "--notes-file") and len(rest) > 1:
            option = rest.pop(0)
            options[option] = rest.pop(0)
        else:
            print(f"unexpected option {rest[0]}", file=sys.stderr)
            sys.exit(2)
    if options.get("--repo") != state["repository"]:
        print("gh: wrong repository", file=sys.stderr)
        sys.exit(1)
    if "--verify-tag" in flags and tag_object(tag) is None:
        print(f"tag {tag} doesn't exist in the repo, aborting due to --verify-tag flag", file=sys.stderr)
        sys.exit(1)
    if state.get("create_status") or tag in state["releases"]:
        print("HTTP 422: Validation Failed", file=sys.stderr)
        sys.exit(1)
    with open(options["--notes-file"], encoding="utf-8") as handle:
        body = handle.read()
    state["releases"][tag] = {
        "tag_name": tag,
        "name": options.get("--title", tag),
        "body": body,
        "draft": False,
        "prerelease": False,
        "immutable": state["immutable"],
        "html_url": "https://github.com/" + state["repository"] + "/releases/tag/" + tag,
    }
    with open(os.environ["FAKE_GH_STATE"], "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    print(state["releases"][tag]["html_url"])
    sys.exit(0)

print("unexpected gh call: " + " ".join(args), file=sys.stderr)
sys.exit(2)
"""


def _release(tag: str, *, immutable: bool = True) -> dict:
    return {
        "tag_name": tag,
        "name": tag,
        "body": _SECTION,
        "draft": False,
        "prerelease": False,
        "immutable": immutable,
        "html_url": f"https://github.com/{_REPOSITORY}/releases/tag/{tag}",
    }


class Origin:
    """A bare origin whose main holds the helper, a CHANGELOG and one passing
    test, a working clone to commit in, and a directory with python3, python
    and the fake gh for the steps' PATH. tip is the latest commit made, pushed
    to main or not."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.bare = root / "origin.git"
        self.work = root / "work"
        self.git("init", "--bare", "--quiet", str(self.bare), cwd=root)
        self.git("--git-dir", str(self.bare), "symbolic-ref", "HEAD", "refs/heads/main", cwd=root)
        self.git("init", "--quiet", str(self.work), cwd=root)
        self.tip = self.commit(
            "Initial commit",
            files={
                ".github/release.py": _HELPER.read_text(encoding="utf-8"),
                "CHANGELOG.md": _CHANGELOG,
                "tests/__init__.py": "",
                "tests/test_passing.py": _PASSING,
            },
        )
        self.push_main(self.tip)

        self.bin = root / "bin"
        self.bin.mkdir()
        for name in ("python3", "python"):
            (self.bin / name).symlink_to(sys.executable)
        gh = self.bin / "gh"
        gh.write_text(_FAKE_GH.replace("@PYTHON@", sys.executable), encoding="utf-8")
        gh.chmod(0o755)
        self.state = root / "gh-state.json"
        self.log = root / "gh-log.jsonl"
        self.log.touch()
        self.set_state()

    def git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=cwd or self.work, env=git_env(self.home), capture_output=True, text=True
        )
        if proc.returncode:
            raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout.strip()

    def commit(self, message: str, *, files: dict[str, str | None] | None = None) -> str:
        for name, content in (files or {}).items():
            path = self.work / name
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "--allow-empty", "--no-verify", "-m", message)
        self.tip = self.git("rev-parse", "HEAD")
        return self.tip

    def push_main(self, sha: str) -> None:
        self.git("push", "--quiet", "--force", str(self.bare), f"{sha}:refs/heads/main")

    def tag(self, name: str, sha: str, *, annotated: bool = False) -> None:
        self.git("tag", *(["--annotate", "--message", name] if annotated else []), name, sha)
        self.git("push", "--quiet", str(self.bare), f"refs/tags/{name}")

    def checkout(self, sha: str) -> Path:
        """What actions/checkout leaves with fetch-depth 0: every ref of
        origin fetched, and sha checked out detached."""
        directory = Path(tempfile.mkdtemp(prefix="checkout-", dir=self.root))
        self.git("clone", "--quiet", str(self.bare), str(directory), cwd=self.root)
        self.git("checkout", "--quiet", "--detach", sha, cwd=directory)
        return directory

    def set_state(self, **changes: object) -> None:
        state = {
            "origin": str(self.bare),
            "repository": _REPOSITORY,
            "token": TOKEN,
            "immutable": True,
            "releases": {},
        }
        if self.state.exists():
            state = json.loads(self.state.read_text(encoding="utf-8"))
        state.update(changes)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def releases(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))["releases"]

    def gh_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def creates(self) -> list[list[str]]:
        return [call for call in self.gh_calls() if call[:2] == ["release", "create"]]

    def env(self, tag: str, sha: str, **overrides: str) -> dict[str, str]:
        """The runner's variables for a push of tag at sha."""
        env = git_env(self.home)
        for name in list(env):
            if name.startswith(("GIT_AUTHOR", "GIT_COMMITTER")):
                del env[name]
        env.update({
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": f"refs/tags/{tag}",
            "GITHUB_REF_NAME": tag,
            "GITHUB_SHA": sha,
            "GITHUB_REPOSITORY": _REPOSITORY,
            "RUNNER_TEMP": tempfile.mkdtemp(prefix="runner-", dir=self.root),
            "FAKE_GH_STATE": str(self.state),
            "FAKE_GH_LOG": str(self.log),
        })
        env.update(overrides)
        return env

    def run(self, cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
        """One release.py command, as a step starts it."""
        return subprocess.run(
            [sys.executable, ".github/release.py", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )


def _expand(value: object, context: dict[str, str]) -> object:
    if not isinstance(value, str) or "${{" not in value:
        return value
    return context[re.fullmatch(r"\$\{\{ (\S+) \}\}", value).group(1)]


class Run:
    """The outcome of every job, and the log of each job that ran."""

    def __init__(self) -> None:
        self.outcomes: dict[str, str] = {}
        self.logs: dict[str, str] = {}
        self.failed_step: dict[str, str] = {}


def run_workflow(
    origin: Origin, tag: str, sha: str, *, after: dict[str, Callable[[], None]] | None = None
) -> Run:
    """release.yml's jobs in their needs order. A job whose needs did not all
    succeed is skipped, as GitHub skips it. Each job gets its own checkout and
    RUNNER_TEMP; a run step starts the way the runner's default shell does."""
    jobs = yamlsubset.load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    context = {"github.sha": sha, "github.token": TOKEN}
    run = Run()
    pending = list(jobs)
    while pending:
        name = next(
            job
            for job in pending
            if not set(_needs(jobs[job])) & set(pending)
        )
        pending.remove(name)
        if any(run.outcomes[need] != "success" for need in _needs(jobs[name])):
            run.outcomes[name] = "skipped"
            continue
        env = origin.env(tag, sha)
        workspace: Path | None = None
        log: list[str] = []
        run.outcomes[name] = "success"
        for step in jobs[name]["steps"]:
            label = step.get("name") or step["uses"]
            if "uses" in step:
                if step["uses"].startswith(_CHECKOUT):
                    workspace = origin.checkout(_expand(step["with"]["ref"], context))
                elif not step["uses"].startswith(_SETUP_PYTHON):
                    raise AssertionError(f"no stand-in for {step['uses']}")
                continue
            step_env = dict(env)
            step_env.update({key: _expand(value, context) for key, value in step.get("env", {}).items()})
            proc = subprocess.run(
                ["bash", "-e", "-c", step["run"]],
                cwd=workspace,
                env=step_env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            log.append(f"--- {label}\n{proc.stdout}{proc.stderr}")
            if proc.returncode:
                run.outcomes[name] = "failure"
                run.failed_step[name] = label
                break
        run.logs[name] = "".join(log)
        if run.outcomes[name] == "success" and after and name in after:
            after[name]()
    return run


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else needs


class _OriginTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.origin = Origin(Path(directory.name).resolve())


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash")
class WorkflowTests(_OriginTestCase):
    def test_a_lightweight_tag_on_the_tip_of_main_becomes_an_immutable_release(self) -> None:
        sha = self.origin.tip
        self.origin.tag("v1.2.3", sha)
        run = run_workflow(self.origin, "v1.2.3", sha)

        self.assertEqual(run.outcomes, {"verify": "success", "publish": "success"}, run.logs)
        self.assertEqual(set(self.origin.releases()), {"v1.2.3"})
        release = self.origin.releases()["v1.2.3"]
        self.assertEqual(release["body"], _SECTION)
        self.assertEqual(release["name"], "v1.2.3")
        (create,) = self.origin.creates()
        self.assertIn("--verify-tag", create)
        self.assertIn("release: 1 tests passed, 0 skipped", run.logs["verify"])
        self.assertIn(f"is immutable and on {sha}", run.logs["publish"])

    def test_a_run_without_tests_publishes_nothing(self) -> None:
        sha = self.origin.commit("Drop the tests", files={"tests/test_passing.py": None})
        self.origin.push_main(sha)
        self.origin.tag("v1.2.3", sha)
        run = run_workflow(self.origin, "v1.2.3", sha)

        self.assertEqual(run.outcomes, {"verify": "failure", "publish": "skipped"}, run.logs)
        self.assertEqual(run.failed_step["verify"], "Run the tests")
        self.assertIn("no tests ran", run.logs["verify"])
        self.assertEqual(self.origin.gh_calls(), [])

    def test_publish_refuses_when_main_moved_after_the_tests(self) -> None:
        sha = self.origin.tip
        self.origin.tag("v1.2.3", sha)
        moved = lambda: self.origin.push_main(self.origin.commit("Land another change"))
        run = run_workflow(self.origin, "v1.2.3", sha, after={"verify": moved})

        self.assertEqual(run.outcomes, {"verify": "success", "publish": "failure"}, run.logs)
        self.assertIn("is not the current tip of main", run.logs["publish"])
        self.assertEqual(self.origin.gh_calls(), [])

    def test_main_moving_during_release_lookup_prevents_creation(self) -> None:
        sha = self.origin.tip
        self.origin.tag("v1.2.3", sha)
        later = self.origin.commit("Land another change")
        self.origin.git("push", str(self.origin.bare), f"{later}:refs/heads/staging")
        self.origin.set_state(move_ref_during_lookup=["refs/heads/main", later])

        run = run_workflow(self.origin, "v1.2.3", sha)

        self.assertEqual(run.outcomes, {"verify": "success", "publish": "failure"}, run.logs)
        self.assertIn("is not the current tip of main", run.logs["publish"])
        self.assertEqual(self.origin.creates(), [])

    def test_tag_moving_during_release_lookup_prevents_creation(self) -> None:
        sha = self.origin.tip
        self.origin.tag("v1.2.3", sha)
        later = self.origin.commit("Land another change")
        self.origin.git("push", str(self.origin.bare), f"{later}:refs/heads/staging")
        self.origin.set_state(move_ref_during_lookup=["refs/tags/v1.2.3", later])

        run = run_workflow(self.origin, "v1.2.3", sha)

        self.assertEqual(run.outcomes, {"verify": "success", "publish": "failure"}, run.logs)
        self.assertEqual(self.origin.creates(), [])
        self.assertIn("v1.2.3 on origin names", run.logs["publish"])

    def test_only_a_tag_push_starts_it(self) -> None:
        workflow = yamlsubset.load(_WORKFLOW.read_text(encoding="utf-8"))
        self.assertEqual(workflow["on"], {"push": {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}})

    def test_only_the_publish_step_holds_a_token_that_can_write(self) -> None:
        workflow = yamlsubset.load(_WORKFLOW.read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(jobs["verify"]["permissions"], {"contents": "read"})
        self.assertEqual(jobs["publish"]["permissions"], {"contents": "write"})
        holders = [
            (job, step.get("name"))
            for job, definition in jobs.items()
            for step in definition["steps"]
            if "github.token" in json.dumps(step)
        ]
        self.assertEqual(holders, [("publish", "Publish the release and read it back")])
        for job, definition in jobs.items():
            for step in definition["steps"]:
                with self.subTest(job=job, step=step.get("name") or step["uses"]):
                    # Values reach a script through env, never spliced into it.
                    self.assertNotIn("${{", step.get("run", ""))
                    if step.get("uses", "").startswith(_CHECKOUT):
                        self.assertEqual(
                            step["with"],
                            {"ref": "${{ github.sha }}", "fetch-depth": 0, "persist-credentials": False},
                        )


class CheckTests(_OriginTestCase):
    def _check(self, tag: str, *, checkout: str | None = None, **overrides: str):
        sha = overrides.pop("sha", self.origin.tip)
        workspace = self.origin.checkout(checkout or sha)
        return self.origin.run(workspace, self.origin.env(tag, sha, **overrides), "check")

    def test_accepts_exactly_vX_Y_Z(self) -> None:
        accepted = ["v0.1.0", "v1.2.3", "v10.20.30"]
        refused = [
            "v1.2", "1.2.3", "V1.2.3", "vv1.2.3", "v1.2.3.4", "v1.2.3a", "v1.2.3-rc.1",
            "v01.2.3", "v1.02.3", "v1.2.03",
        ]
        # Only accepted names get a tag: a refused one never reaches git, and
        # V1.2.3 would collide with v1.2.3 on a case-insensitive file system.
        for name in accepted:
            self.origin.tag(name, self.origin.tip)
        for name in accepted:
            with self.subTest(tag=name):
                result = self._check(name)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in refused:
            with self.subTest(tag=name):
                result = self._check(name)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"::error::release: tag '{name}' is not vX.Y.Z", result.stdout)

    def test_the_ref_must_be_that_tag(self) -> None:
        self.origin.tag("v1.2.3", self.origin.tip)
        result = self._check("v1.2.3", GITHUB_REF="refs/heads/v1.2.3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'refs/heads/v1.2.3' is not the tag v1.2.3", result.stdout)

    def test_only_a_push_releases(self) -> None:
        self.origin.tag("v1.2.3", self.origin.tip)
        result = self._check("v1.2.3", GITHUB_EVENT_NAME="workflow_dispatch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not 'workflow_dispatch'", result.stdout)

    def test_an_annotated_tag_is_refused(self) -> None:
        self.origin.tag("v1.2.3", self.origin.tip, annotated=True)
        result = self._check("v1.2.3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("v1.2.3 is an annotated tag", result.stdout)

    def test_a_tag_behind_the_tip_of_main_is_refused(self) -> None:
        tagged = self.origin.tip
        self.origin.tag("v1.2.3", tagged)
        tip = self.origin.commit("Land another change")
        self.origin.push_main(tip)
        result = self._check("v1.2.3", sha=tagged)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{tagged} is not the current tip of main, {tip}", result.stdout)

    def test_main_is_read_from_origin_at_check_time(self) -> None:
        """The checkout's own origin/main still names the tag; origin moved on."""
        sha = self.origin.tip
        self.origin.tag("v1.2.3", sha)
        workspace = self.origin.checkout(sha)
        self.origin.push_main(self.origin.commit("Land another change"))
        result = self.origin.run(workspace, self.origin.env("v1.2.3", sha), "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not the current tip of main", result.stdout)

    def test_the_tag_must_name_the_event_commit(self) -> None:
        parent = self.origin.tip
        self.origin.tag("v1.2.3", parent)
        tip = self.origin.commit("Land another change")
        self.origin.push_main(tip)
        result = self._check("v1.2.3", sha=tip)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"v1.2.3 on origin names {parent}, not {tip}", result.stdout)

    def test_the_checkout_must_be_the_event_commit(self) -> None:
        parent = self.origin.tip
        tip = self.origin.commit("Land another change")
        self.origin.push_main(tip)
        self.origin.tag("v1.2.3", tip)
        result = self._check("v1.2.3", checkout=parent, sha=tip)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"the checkout is {parent}, not {tip}", result.stdout)


class TestCountTests(unittest.TestCase):
    def _test(self, **files: str) -> subprocess.CompletedProcess:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / ".github").mkdir()
        shutil.copy(_HELPER, root / ".github" / "release.py")
        (root / "tests").mkdir()
        (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        for name, content in files.items():
            (root / "tests" / f"{name}.py").write_text(content, encoding="utf-8")
        return subprocess.run(
            [sys.executable, ".github/release.py", "test"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_passing_tests_pass(self) -> None:
        result = self._test(test_passing=_PASSING)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("release: 1 tests passed, 0 skipped", result.stdout)

    def test_no_tests_fail(self) -> None:
        result = self._test()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no tests ran: 0 found", result.stdout)

    def test_only_skipped_tests_fail(self) -> None:
        result = self._test(test_skipped=_SKIPPED)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no tests ran: 1 found, all of them skipped", result.stdout)

    def test_a_failing_test_fails(self) -> None:
        result = self._test(test_passing=_PASSING, test_failing=_FAILING)
        self.assertEqual(result.returncode, 1)
        self.assertIn("the tests failed", result.stdout)


class NotesTests(unittest.TestCase):
    def _notes(self, changelog: str, tag: str = "v1.2.3") -> tuple[subprocess.CompletedProcess, Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
        out = root / "notes.md"
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "GITHUB_REF": f"refs/tags/{tag}",
            "GITHUB_REF_NAME": tag,
        }
        result = subprocess.run(
            [sys.executable, str(_HELPER), "notes", str(out)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result, out

    def test_writes_exactly_the_section_of_the_tag(self) -> None:
        result, out = self._notes(_CHANGELOG)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(out.read_text(encoding="utf-8"), _SECTION)

    def test_the_last_section_ends_the_notes_at_the_end_of_the_file(self) -> None:
        result, out = self._notes(_CHANGELOG, tag="v1.2.2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(out.read_text(encoding="utf-8"), "- An older release\n")

    def test_refuses_anything_but_one_dated_section_with_content(self) -> None:
        cases = {
            "missing": _CHANGELOG.replace("## [1.2.3] - 2026-09-28", "## [1.2.4] - 2026-09-28"),
            "only a longer version": _CHANGELOG.replace("## [1.2.3] - 2026-09-28\n", ""),
            "undated": _CHANGELOG.replace("## [1.2.3] - 2026-09-28", "## [1.2.3]"),
            "twice": _CHANGELOG + "\n## [1.2.3] - 2026-09-29\n\n- Again\n",
            "empty": _CHANGELOG.replace(_SECTION, "\n"),
        }
        for case, changelog in cases.items():
            with self.subTest(case=case):
                result, out = self._notes(changelog)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("::error::release: ", result.stdout)
                self.assertFalse(out.exists())


class PublishTests(_OriginTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.notes = self.origin.root / "notes.md"
        self.notes.write_text(_SECTION, encoding="utf-8")

    def _publish(self, sha: str | None = None) -> subprocess.CompletedProcess:
        sha = sha or self.origin.tip
        workspace = self.origin.checkout(sha)
        env = self.origin.env("v1.2.3", sha, GH_TOKEN=TOKEN)
        return self.origin.run(workspace, env, "publish", str(self.notes))

    def test_a_release_that_is_not_immutable_fails(self) -> None:
        self.origin.tag("v1.2.3", self.origin.tip)
        self.origin.set_state(immutable=False)
        result = self._publish()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the release v1.2.3 is not immutable", result.stdout)

    def test_an_existing_release_on_the_same_commit_is_verified_not_created(self) -> None:
        self.origin.tag("v1.2.3", self.origin.tip)
        self.origin.set_state(releases={"v1.2.3": _release("v1.2.3")})
        result = self._publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::notice::release v1.2.3 already exists; verifying it", result.stdout)
        self.assertIn(f"is immutable and on {self.origin.tip}", result.stdout)
        self.assertEqual(self.origin.creates(), [])

    def test_an_existing_release_on_another_commit_fails(self) -> None:
        parent = self.origin.tip
        self.origin.tag("v1.2.3", parent)
        tip = self.origin.commit("Land another change")
        self.origin.push_main(tip)
        self.origin.set_state(releases={"v1.2.3": _release("v1.2.3")})
        result = self._publish(tip)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"the release v1.2.3 is on {parent}, not {tip}", result.stdout)
        self.assertEqual(self.origin.creates(), [])

    def test_an_existing_mutable_release_fails(self) -> None:
        self.origin.tag("v1.2.3", self.origin.tip)
        self.origin.set_state(releases={"v1.2.3": _release("v1.2.3", immutable=False)})
        result = self._publish()
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not immutable", result.stdout)

    def test_an_existing_release_on_an_annotated_tag_fails(self) -> None:
        self.origin.tag("v1.2.3", self.origin.tip, annotated=True)
        self.origin.set_state(releases={"v1.2.3": _release("v1.2.3")})
        result = self._publish()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not rest on a lightweight tag", result.stdout)

    def test_only_a_404_proves_there_is_no_release(self) -> None:
        self.origin.tag("v1.2.3", self.origin.tip)
        self.origin.set_state(api_status=502)
        result = self._publish()
        self.assertEqual(result.returncode, 1)
        self.assertIn("gh api repos/owner/repo/releases/tags/v1.2.3 failed", result.stdout)
        self.assertEqual(self.origin.creates(), [])

    def test_a_failed_create_fails(self) -> None:
        self.origin.tag("v1.2.3", self.origin.tip)
        self.origin.set_state(create_status=422)
        result = self._publish()
        self.assertEqual(result.returncode, 1)
        self.assertIn("gh release create failed", result.stdout)


if __name__ == "__main__":
    unittest.main()
