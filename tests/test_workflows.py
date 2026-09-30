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

from agent_guardrails import event

from . import yamlsubset
from .support import ROOT, TOKEN, RemoteTestCase, git_environment

_WORKFLOWS = ROOT / ".github" / "workflows"
_PINNED = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}")
_VERSION_COMMENT = re.compile(r" # v[0-9]+\.[0-9]+\.[0-9]+$")
# The one exception to a version comment: the CI cost study's controller runs
# two unreleased snapshots, which have no version to name.
_STUDY = "e2e-benchmark.yml"
_STUDY_SNAPSHOTS = {
    "stickerdaniel/agent-guardrails@a7e46651837895aa10006c79a58a6a502b968806",
    "stickerdaniel/agent-guardrails@82427d58bc35a2146e3ce6209bbff8be7e2c6f3f",
}
_SNAPSHOT_COMMENT = " # e2e snapshot, unmerged"


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
    expressions in its env block replaced by the caller's values."""

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
            **{f"inputs.{name}": value for name, value in inputs.items()},
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
            {"require-model-attribution": "false", "hidden-unicode": "off"}, body="No line"
        )
        self.assertIn("input hidden-unicode must be error or warn", invalid.stdout)

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
                    for line in (line for line in text if f"uses: {step['uses']}" in line):
                        if path.name == _STUDY and step["uses"] in _STUDY_SNAPSHOTS:
                            self.assertTrue(line.endswith(f"uses: {step['uses']}{_SNAPSHOT_COMMENT}"), line)
                        else:
                            self.assertRegex(line, _VERSION_COMMENT)


class StudyControllerTests(unittest.TestCase):
    """The CI cost study's controller: 18 fixed jobs, each reading its own
    event into a digest and then running exactly one pinned variant."""

    ORDERS = {"A": ("PY", "TS-H", "RS"), "B": ("TS-H", "RS", "PY"), "C": ("RS", "PY", "TS-H")}
    ARCHES = {"x64": "ubuntu-latest", "arm64": "ubuntu-24.04-arm"}
    REFS = {
        "PY": "stickerdaniel/agent-guardrails@a3508d7320e64370b878359b1f4ae541b507224f",
        "RS": "stickerdaniel/agent-guardrails@a7e46651837895aa10006c79a58a6a502b968806",
        "TS-H": "stickerdaniel/agent-guardrails@82427d58bc35a2146e3ce6209bbff8be7e2c6f3f",
    }
    COMMENTS = {"PY": " # v1.0.0", "RS": _SNAPSHOT_COMMENT, "TS-H": _SNAPSHOT_COMMENT}
    HEADS = ("e2e/workload-w1", "e2e/workload-w2", "e2e/workload-w3", "e2e/workload-w4")

    def setUp(self) -> None:
        self.path = _WORKFLOWS / _STUDY
        self.workflow = _load(self.path)
        self.contract = json.loads((ROOT / "e2e" / "contract.json").read_text(encoding="utf-8"))

    def condition(self, order: str) -> str:
        heads = " || ".join(f"github.event.pull_request.head.ref == '{head}'" for head in self.HEADS)
        return (
            "${{ always() && !cancelled()"
            " && github.event_name == 'pull_request_target' && github.event.action == 'labeled'"
            f" && github.event.label.name == 'e2e:order-{order}'"
            " && github.event.pull_request.base.ref == 'e2e/workload-base'"
            " && github.event.pull_request.head.repo.full_name == github.repository"
            f" && ({heads}) }}}}"
        )

    def test_runs_only_on_a_label_with_a_read_only_token(self) -> None:
        self.assertEqual(self.workflow["on"], {"pull_request_target": {"types": ["labeled"]}})
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            self.workflow["concurrency"],
            {"group": "e2e-${{ github.event.pull_request.number }}", "cancel-in-progress": False},
        )
        for job in self.workflow["jobs"].values():
            self.assertNotIn("permissions", job)

    def test_eighteen_jobs_in_three_orders_on_two_architectures(self) -> None:
        jobs = self.workflow["jobs"]
        expected = {}
        for arch, runner in self.ARCHES.items():
            for order, variants in self.ORDERS.items():
                for position, variant in enumerate(variants, 1):
                    expected[f"{arch}-{order.lower()}{position}"] = (arch, runner, order, position, variant)
        self.assertEqual(set(jobs), set(expected))
        for key, (arch, runner, order, position, variant) in expected.items():
            job = jobs[key]
            with self.subTest(job=key):
                self.assertEqual(job["name"], f"{arch} {order}{position} {variant}")
                self.assertEqual(job["runs-on"], runner)
                self.assertEqual(job["if"], self.condition(order))
                if position == 1:
                    self.assertNotIn("needs", job)
                else:
                    self.assertEqual(job["needs"], f"{arch}-{order.lower()}{position - 1}")
                self.assertEqual(set(job), {"name", "if", "runs-on", "steps"} | ({"needs"} if position > 1 else set()))

    def test_each_job_is_the_metadata_step_then_its_own_variant(self) -> None:
        scripts = set()
        for key, job in self.workflow["jobs"].items():
            variant = job["name"].rsplit(" ", 1)[1]
            with self.subTest(job=key):
                metadata, action = job["steps"]
                self.assertEqual(set(metadata), {"name", "shell", "env", "run"})
                self.assertEqual(metadata["name"], "E2E trusted event metadata")
                self.assertEqual(metadata["shell"], "bash")
                # No expression reaches the script, so no event text can.
                self.assertNotIn("${{", metadata["run"])
                self.assertTrue(metadata["run"].startswith("# e2e-metadata v1"))
                scripts.add(metadata["run"])

                self.assertEqual(set(action), {"name", "uses", "with"})
                self.assertEqual(action["name"], f"Variant {variant}")
                self.assertEqual(action["uses"], self.REFS[variant])
                self.assertEqual(action["with"], {"require-model-attribution": True, "hidden-unicode": "error"})
                self.assertEqual(
                    metadata["env"],
                    {
                        "E2E_REQUIRE_MODEL_ATTRIBUTION": str(action["with"]["require-model-attribution"]).lower(),
                        "E2E_HIDDEN_UNICODE": action["with"]["hidden-unicode"],
                    },
                )
        self.assertEqual(len(scripts), 1)

    def test_no_other_action_anywhere_in_the_file(self) -> None:
        uses = [line.strip() for line in self.path.read_text(encoding="utf-8").splitlines() if "uses:" in line]
        allowed = {f"uses: {ref}{self.COMMENTS[variant]}" for variant, ref in self.REFS.items()}
        self.assertEqual(len(uses), 18)
        self.assertEqual(set(uses), allowed)
        for forbidden in ("actions/checkout", "actions/cache", "actions/setup-", "./"):
            self.assertNotIn(f"uses: {forbidden}", self.path.read_text(encoding="utf-8"))

    def test_the_contract_describes_this_controller(self) -> None:
        contract = self.contract
        self.assertEqual(contract["controller"]["workflow_path"], f".github/workflows/{_STUDY}")
        self.assertEqual(contract["inputs"], {"require-model-attribution": "true", "hidden-unicode": "error"})
        self.assertEqual(contract["architectures"], self.ARCHES)
        for variant, ref in self.REFS.items():
            self.assertEqual(f"stickerdaniel/agent-guardrails@{contract['variants'][variant]['sha']}", ref)
            self.assertEqual(contract["variants"][variant]["step"], f"Variant {variant}")
        for order, variants in self.ORDERS.items():
            self.assertEqual(contract["orders"][order], {"label": f"e2e:order-{order}", "variants": list(variants)})
        by_name = {job["name"]: key for key, job in self.workflow["jobs"].items()}
        self.assertEqual(set(contract["jobs"]), set(by_name))
        for name, spec in contract["jobs"].items():
            self.assertEqual(spec["key"], by_name[name])
            self.assertEqual(f"{spec['arch']} {spec['order']}{spec['position']} {spec['variant']}", name)


# A synthetic pull_request_target event of a fixture pull request.
def _study_event(**pull: object) -> dict:
    pull_request = {
        "number": 7,
        "title": 'Title with "quotes", a \\, é and controls \u0001\u001f\u007f',
        "body": "Body line\r\nwith a tab\there and 日本",
        "user": {"login": "maintainer"},
        "base": {"ref": "e2e/workload-base", "sha": "a" * 40},
        "head": {"sha": "b" * 40},
    }
    pull_request.update(pull)
    return {"action": "labeled", "label": {"name": "e2e:order-B"}, "pull_request": pull_request}


@unittest.skipUnless(
    shutil.which("bash") and shutil.which("jq") and shutil.which("sha256sum"), "needs bash, jq and sha256sum"
)
class StudyMetadataStepTests(unittest.TestCase):
    """Runs the controller's metadata script as the runner would."""

    def run_script(self, event: dict) -> subprocess.CompletedProcess:
        job = _load(_WORKFLOWS / _STUDY)["jobs"]["x64-a1"]
        step = job["steps"][0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(event), encoding="utf-8")
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "GITHUB_EVENT_PATH": str(path),
                "GITHUB_EVENT_NAME": "pull_request_target",
                "GITHUB_REPOSITORY": "stickerdaniel/agent-guardrails",
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_WORKFLOW_SHA": "c" * 40,
                "GITHUB_WORKFLOW_REF": "stickerdaniel/agent-guardrails/.github/workflows/e2e-benchmark.yml@refs/heads/main",
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1",
                **step["env"],
            }
            return subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
                env=env, capture_output=True, text=True, timeout=60,
            )

    def metadata(self, result: subprocess.CompletedProcess) -> dict:
        lines = result.stdout.splitlines()
        self.assertTrue(all(line.startswith("e2e-meta v1 ") for line in lines), lines)
        return dict(line[len("e2e-meta v1 "):].split("=", 1) for line in lines)

    def test_prints_the_digest_and_named_fields_only(self) -> None:
        from e2e import analyze

        event = _study_event()
        result = self.run_script(event)
        self.assertEqual(result.returncode, 0, result.stderr)
        meta = self.metadata(result)
        self.assertEqual(list(meta), list(analyze.METADATA_KEYS))
        pull = event["pull_request"]
        self.assertEqual(
            meta["digest"],
            analyze.event_digest(7, "b" * 40, "a" * 40, "e2e/workload-base", pull["title"], pull["body"], "maintainer"),
        )
        self.assertEqual((meta["label"], meta["number"], meta["run_id"]), ("e2e:order-B", "7", "123"))
        self.assertEqual((meta["require_model_attribution"], meta["hidden_unicode"]), ("true", "error"))
        for text in ("Title with", "Body line", "maintainer"):
            self.assertNotIn(text, result.stdout + result.stderr)

    def test_a_null_body_and_an_empty_body_differ(self) -> None:
        null = self.metadata(self.run_script(_study_event(body=None)))["digest"]
        empty = self.metadata(self.run_script(_study_event(body="")))["digest"]
        self.assertNotEqual(null, empty)

    def test_fails_closed_on_an_unexpected_event(self) -> None:
        for event in (
            _study_event(body=3),
            _study_event(number="7"),
            {**_study_event(), "label": {"name": "e2e:order-D"}},
            _study_event(base={"ref": "main", "sha": "a" * 40}),
            _study_event(head={"sha": "B" * 40}),
        ):
            with self.subTest(event=event):
                result = self.run_script(event)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("digest=", result.stdout)
                self.assertIn("e2e-metadata: this is not a labeled event", result.stderr)


if __name__ == "__main__":
    unittest.main()
