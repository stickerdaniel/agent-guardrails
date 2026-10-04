"""The contracts this repository's YAML makes: the action's inputs and their
wiring, the dogfood caller, the required check names, and the pins."""

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

from post_no_bills import event

from . import yamlsubset
from .support import ROOT, TOKEN, RemoteTestCase, git_environment, runner_inputs

_WORKFLOWS = ROOT / ".github" / "workflows"
_PINNED = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}")
_VERSION_COMMENT = re.compile(r" # v[0-9]+\.[0-9]+\.[0-9]+$")


def _load(path) -> dict:
    return yamlsubset.load(path.read_text(encoding="utf-8"))


def _step(action: dict) -> dict:
    steps = action["runs"]["steps"]
    if len(steps) != 1:
        raise AssertionError(f"expected one composite step, found {len(steps)}")
    return steps[0]


class ActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.action = _load(ROOT / "action.yml")

    def test_is_published_as_post_no_bills(self) -> None:
        self.assertEqual(self.action["name"], "Post No Bills")

    def test_declares_the_inputs_with_literal_safe_defaults(self) -> None:
        inputs = self.action["inputs"]
        defaults = {name: spec["default"] for name, spec in inputs.items()}
        self.assertEqual(
            defaults,
            {
                "require-model-attribution": "true",
                "co-author-trailers": "error",
                "agent-identities": "error",
                "hidden-unicode": "error",
                "unicode-homoglyphs": "inherit",
                "unicode-unusual-spaces": "inherit",
                "unicode-exclude-paths": "",
                "allowed-identities": "",
                "additional-identities": "",
                "additional-binary-extensions": "",
                "additional-attribution-exemptions": "",
            },
        )
        # The entrypoint knows exactly these names.
        self.assertEqual(tuple(inputs), event.INPUT_NAMES)
        for name, default in defaults.items():
            with self.subTest(name=name):
                # An expression would make the default depend on its caller.
                self.assertIsInstance(default, str)
                self.assertNotIn("${{", default)
                self.assertIs(inputs[name]["required"], False)

    def test_names_every_value_the_entrypoint_reads(self) -> None:
        step = _step(self.action)
        self.assertEqual(self.action["runs"]["using"], "composite")
        self.assertEqual(
            step["env"],
            {
                event.INPUTS: "${{ toJSON(inputs) }}",
                event.TOKEN: "${{ github.token }}",
                event.SERVER_URL: "${{ github.server_url }}",
                event.REPOSITORY: "${{ github.repository }}",
                event.EVENT_NAME: "${{ github.event_name }}",
            },
        )
        self.assertEqual(
            step["run"].rstrip("\n").splitlines()[-1],
            'python3 -I "$GITHUB_ACTION_PATH/run.py"',
        )


# Stands in for git on the step's PATH. It appends its arguments and the names
# in its environment to the file that is also the step's stdout, so git calls
# and output lines share one timeline, then runs the real git.
_RECORDER = """\
#!{python} -I
import json, os, sys
record = {{
    "argv": sys.argv[1:],
    "env": sorted(os.environ),
    "config_count": int(os.environ.get("GIT_CONFIG_COUNT", "0")),
}}
with open({timeline!r}, "a", encoding="utf-8") as handle:
    handle.write("@@git " + json.dumps(record) + "\\n")
os.execv({git!r}, [{git!r}, *sys.argv[1:]])
"""
_RECORD = "@@git "


@unittest.skipUnless(shutil.which("bash") and shutil.which("python3"), "needs bash and python3")
class CompositeStepTests(RemoteTestCase):
    """Runs the composite step's script the way the runner would, with the
    expressions in its env block replaced by the caller's values. Of the
    expression language only the forms action.yml uses are known, and
    toJSON(inputs) is the map a hosted runner was measured to build."""

    def _recorder(self, timeline: Path) -> Path:
        """A directory whose git records each call in timeline."""
        directory = Path(tempfile.mkdtemp(dir=self.remote.root))
        git = directory / "git"
        git.write_text(
            _RECORDER.format(python=sys.executable, timeline=str(timeline), git=shutil.which("git")),
            encoding="utf-8",
        )
        git.chmod(0o755)
        return directory

    def _interpreter_additions(self) -> set[str]:
        """Names the recorder's own interpreter adds to what it was given, such
        as LC_CTYPE from locale coercion. They cannot be told from inherited
        ones, so the environment check below leaves only these out."""
        timeline = self.remote.root / "control"
        given = {"PATH": str(self._recorder(timeline)), "LANG": "C"}
        subprocess.run(["git", "--version"], env=given, capture_output=True, check=True)
        (line,) = timeline.read_text(encoding="utf-8").splitlines()
        return set(json.loads(line[len(_RECORD):])["env"]) - set(given)

    def _run_step(
        self, inputs: dict[str, str], body: str
    ) -> tuple[subprocess.CompletedProcess, list[tuple[str, object]]]:
        """The step's result, with stdout and every git call in the order they
        happened."""
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)
        context = {
            "toJSON(inputs)": json.dumps(runner_inputs(inputs)),
            "github.token": TOKEN,
            "github.server_url": self.remote.server_url,
            "github.repository": self.remote.repository,
            "github.event_name": "pull_request_target",
        }
        step = _step(_load(ROOT / "action.yml"))
        timeline = Path(tempfile.mkdtemp(dir=self.remote.root)) / "stdout"
        path = self.remote.environment(self.remote.root)["PATH"]
        env = {
            "PATH": f"{self._recorder(timeline)}{os.pathsep}{path}",
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_EVENT_PATH": str(self.remote.event(head=head, body=body)),
            "RUNNER_TEMP": str(self.remote.runner_temp),
        }
        for name, expression in step["env"].items():
            key = re.fullmatch(r"\$\{\{ (\S+) \}\}", expression).group(1)
            env[name] = context[key]
        with open(timeline, "a", encoding="utf-8") as stdout:
            result = subprocess.run(
                ["bash", "-e", "-c", step["run"]],
                env=env,
                stdout=stdout,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )
        events: list[tuple[str, object]] = [
            ("git", json.loads(line[len(_RECORD):])) if line.startswith(_RECORD) else ("out", line)
            for line in timeline.read_text(encoding="utf-8").splitlines()
        ]
        result.stdout = "".join(f"{line}\n" for kind, line in events if kind == "out")
        return result, events

    def test_inputs_reach_the_entrypoint(self) -> None:
        required, _ = self._run_step(
            {"require-model-attribution": "true", "hidden-unicode": "error"}, body="No line"
        )
        self.assertEqual(required.returncode, 1, required.stdout + required.stderr)
        self.assertIn("::error title=PR model attribution required::", required.stdout)

        optional, _ = self._run_step(
            {"require-model-attribution": "false", "hidden-unicode": "warn"}, body="No line"
        )
        self.assertEqual(optional.returncode, 0, optional.stdout + optional.stderr)

        invalid, _ = self._run_step(
            {"require-model-attribution": "false", "hidden-unicode": "of"}, body="No line"
        )
        self.assertIn("input hidden-unicode must be error, warn or off", invalid.stdout)

    def test_a_name_in_another_case_is_that_input(self) -> None:
        # The runner adds no default for a declared name the caller wrote in
        # another case, so the caller's value is the only one.
        required, _ = self._run_step({"Require-Model-Attribution": "true"}, body="No line")
        self.assertEqual(required.returncode, 1, required.stdout + required.stderr)
        self.assertIn("::error title=PR model attribution required::", required.stdout)

    def test_a_misspelt_input_fails_before_any_git_call(self) -> None:
        result, events = self._run_step({"require-model-attributionn": "true"}, body="No line")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            "::error title=post-no-bills::unknown input(s): 'require-model-attributionn'",
            result.stdout,
        )
        self.assertEqual([record for kind, record in events if kind == "git"], [])

    def test_no_git_runs_before_the_mask_or_outside_its_environment(self) -> None:
        """The whole step, preflight included: the job token is in the step's
        environment, so a git started by the shell would see it unmasked."""
        additions = self._interpreter_additions()
        result, events = self._run_step(
            {"require-model-attribution": "false", "hidden-unicode": "error"}, body=""
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        mask = next(
            index
            for index, (kind, line) in enumerate(events)
            if kind == "out" and line.startswith("::add-mask::")
        )
        calls = [(index, record) for index, (kind, record) in enumerate(events) if kind == "git"]
        # The version check found git on PATH inside the minimal environment.
        self.assertEqual(calls[0][1]["argv"], ["--version"])
        self.assertIn("fetch", [record["argv"][0] for _, record in calls])
        self.assertLess(mask, calls[0][0])
        for _, record in calls:
            with self.subTest(command=record["argv"][0]):
                self.assertEqual(
                    set(record["env"]) - additions, git_environment(record["config_count"])
                )


class RunnerInputsTests(unittest.TestCase):
    """The stand-in for toJSON(inputs) builds what runner 2.337.0 built for
    a composite action: unknown keys and the caller's spelling kept, string
    values, and a default only for a declared name no key matches."""

    def test_defaults_fill_only_what_the_caller_left_out(self) -> None:
        inputs = runner_inputs({"Hidden-Unicode": "warn", "typo": "", "co-author-trailers": ""})
        self.assertEqual(inputs["Hidden-Unicode"], "warn")
        self.assertNotIn("hidden-unicode", inputs)
        self.assertEqual(inputs["typo"], "")
        # An explicit empty value is not replaced by the default.
        self.assertEqual(inputs["co-author-trailers"], "")
        self.assertEqual(inputs["require-model-attribution"], "true")
        self.assertEqual(len(inputs), len(event.INPUT_NAMES) + 1)
        self.assertTrue(all(isinstance(value, str) for value in inputs.values()))


class DogfoodCallerTests(unittest.TestCase):
    """Ported from test_workflow_checks_attribution_in_required_job."""

    def test_workflow_checks_attribution_in_required_job(self) -> None:
        workflow = _load(_WORKFLOWS / "post-no-bills.yml")
        job = workflow["jobs"]["check-bot-coauthors"]
        checkout, check = job["steps"]

        self.assertNotIn("pull_request", workflow["on"])
        self.assertEqual(
            workflow["on"]["pull_request_target"]["types"],
            ["opened", "synchronize", "reopened", "edited"],
        )
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            workflow["concurrency"]["group"],
            "${{ github.workflow }}-${{ github.event.pull_request.number }}",
        )
        # A skipped job satisfies a required check, so the job never skips.
        self.assertNotIn("if", job)

        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"], "${{ github.workflow_sha }}")
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertEqual(checkout["with"]["fetch-depth"], 1)

        self.assertEqual(check["uses"], "./")
        self.assertIs(check["with"]["require-model-attribution"], True)


class CentralChecksTests(unittest.TestCase):
    def test_ci_has_one_gate_job_named_test(self) -> None:
        jobs = _load(_WORKFLOWS / "ci.yml")["jobs"]
        matrix = jobs["unittest"]["strategy"]["matrix"]["python-version"]
        self.assertEqual(matrix, ["3.10", "3.13"])
        gate = jobs["test"]
        self.assertEqual(gate["needs"], "unittest")
        self.assertEqual(gate["if"], "always()")
        self.assertEqual(gate["steps"][0]["env"]["RESULT"], "${{ needs.unittest.result }}")
        self.assertEqual(gate["steps"][0]["run"], 'test "$RESULT" = success')

    def test_actionlint_fails_on_errors_for_every_pull_request(self) -> None:
        workflow = _load(_WORKFLOWS / "actionlint.yml")
        self.assertIsNone(workflow["on"]["pull_request"])
        lint = workflow["jobs"]["actionlint"]["steps"][-1]
        self.assertTrue(lint["uses"].startswith("reviewdog/action-actionlint@"))
        self.assertEqual(
            lint["with"],
            {"reporter": "github-annotations", "filter_mode": "nofilter", "fail_level": "error"},
        )

    def test_third_party_actions_are_pinned_by_sha_with_a_version(self) -> None:
        for path in sorted(_WORKFLOWS.glob("*.yml")):
            steps = [
                step
                for job in _load(path)["jobs"].values()
                for step in job.get("steps", [])
                if "uses" in step and step["uses"] != "./"
            ]
            text = path.read_text(encoding="utf-8").splitlines()
            for step in steps:
                with self.subTest(workflow=path.name, uses=step["uses"]):
                    self.assertRegex(step["uses"], _PINNED.pattern + "$")
                    for line in (line for line in text if f"uses: {step['uses']}" in line):
                        self.assertRegex(line, _VERSION_COMMENT)


if __name__ == "__main__":
    unittest.main()
