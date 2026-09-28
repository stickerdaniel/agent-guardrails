"""The contracts this repository's YAML makes: the action's inputs and their
wiring, the dogfood caller, the required check names, and the pins."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest

from agent_guardrails import event

from . import yamlsubset
from .support import ROOT, TOKEN, RemoteTestCase

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

    def test_declares_exactly_two_inputs_with_safe_defaults(self) -> None:
        inputs = self.action["inputs"]
        self.assertEqual(set(inputs), {"require-model-attribution", "hidden-unicode"})
        self.assertEqual(inputs["require-model-attribution"]["default"], "false")
        self.assertEqual(inputs["hidden-unicode"]["default"], "error")

    def test_names_every_value_the_entrypoint_reads(self) -> None:
        step = _step(self.action)
        self.assertEqual(self.action["runs"]["using"], "composite")
        self.assertEqual(
            step["env"],
            {
                event.REQUIRE_MODEL_ATTRIBUTION: "${{ inputs.require-model-attribution }}",
                event.HIDDEN_UNICODE: "${{ inputs.hidden-unicode }}",
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


@unittest.skipUnless(shutil.which("bash") and shutil.which("python3"), "needs bash and python3")
class CompositeStepTests(RemoteTestCase):
    """Runs the composite step's script the way the runner would, with the
    expressions in its env block replaced by the caller's values."""

    def _run_step(self, inputs: dict[str, str], body: str) -> subprocess.CompletedProcess:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        self.remote.open_pull_request(head)
        context = {
            **{f"inputs.{name}": value for name, value in inputs.items()},
            "github.token": TOKEN,
            "github.server_url": self.remote.server_url,
            "github.repository": self.remote.repository,
            "github.event_name": "pull_request_target",
        }
        step = _step(_load(ROOT / "action.yml"))
        env = {
            "PATH": self.remote.environment(self.remote.root)["PATH"],
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_EVENT_PATH": str(self.remote.event(head=head, body=body)),
            "RUNNER_TEMP": str(self.remote.runner_temp),
        }
        for name, expression in step["env"].items():
            key = re.fullmatch(r"\$\{\{ (\S+) \}\}", expression).group(1)
            env[name] = context[key]
        return subprocess.run(
            ["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=120
        )

    def test_inputs_reach_the_entrypoint(self) -> None:
        required = self._run_step(
            {"require-model-attribution": "true", "hidden-unicode": "error"}, body="No line"
        )
        self.assertEqual(required.returncode, 1, required.stdout + required.stderr)
        self.assertIn("::error title=PR model attribution required::", required.stdout)

        optional = self._run_step(
            {"require-model-attribution": "false", "hidden-unicode": "warn"}, body="No line"
        )
        self.assertEqual(optional.returncode, 0, optional.stdout + optional.stderr)

        invalid = self._run_step(
            {"require-model-attribution": "false", "hidden-unicode": "off"}, body="No line"
        )
        self.assertIn("input hidden-unicode must be error or warn", invalid.stdout)


class DogfoodCallerTests(unittest.TestCase):
    """Ported from test_workflow_checks_attribution_in_required_job."""

    def test_workflow_checks_attribution_in_required_job(self) -> None:
        workflow = _load(_WORKFLOWS / "agent-guardrails.yml")
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
                    line = next(line for line in text if f"uses: {step['uses']}" in line)
                    self.assertRegex(line, _VERSION_COMMENT)


if __name__ == "__main__":
    unittest.main()
