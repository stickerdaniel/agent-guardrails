"""bench/total_run.py in a runner-shaped work directory.

Each test lays out what a hosted runner has when the worker starts: the
delivered snapshot under _actions/<owner>/<repo>/<sha> with its .completed
watermark, a third-party action beside it, RUNNER_TEMP holding the generated
typical capture, a workspace, and HOME outside all of them. The delivered
trees hold real bytes from this checkout: the committed launcher and gzip
assets for the candidate, which the frozen snapshot shares byte for byte,
and the root agent_guardrails package for the control, whose hashes are
tools/baseline.py's. A test contract names exactly those trees in its
manifests, so the positive controls run the real native drive and the real
captured driver and must print the contract's expected output.

Linux only, like the worker. Set AGX_REQUIRE_LINUX=1 to make a non-Linux
host an error instead of a skip; CI does.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = ROOT.parent.parent
WORKER = ROOT / "bench" / "total_run.py"
sys.path.insert(0, str(ROOT / "bench"))
import gen_corpora  # noqa: E402
import total_analyze  # noqa: E402
import total_plan  # noqa: E402

if sys.platform != "linux" or shutil.which("bash") is None or platform.machine() not in ("x86_64", "aarch64"):
    if os.environ.get("AGX_REQUIRE_LINUX") == "1":
        raise RuntimeError("the worker tests need Linux x86_64 or aarch64 and bash")
    raise unittest.SkipTest("the worker tests need Linux x86_64 or aarch64 and bash")

SOURCE = "0123456789abcdef0123456789abcdef01234567"
TARGET = {"x86_64": "linux-x64", "aarch64": "linux-arm64"}[platform.machine()]
ACTION = "experiments/native-distribution"
EXPECTED_LINE = b"agent-guardrails: checked 2 commits, 4 changed files, and the PR title and body: 0 errors, 0 warnings\n"


def checkout_files(paths: list[str]) -> dict[str, bytes]:
    return {path: (REPOSITORY / path).read_bytes() for path in paths}


def candidate_tree(asset: bytes | None = None) -> dict[str, bytes]:
    files = checkout_files([
        f"{ACTION}/launch.sh", f"{ACTION}/action.yml", f"{ACTION}/bin/linux-x64/agent-guardrails.gz",
        f"{ACTION}/bin/linux-arm64/agent-guardrails.gz", f"{ACTION}/bench/py_driver.py",
        f"{ACTION}/bench/capture.py", "README.md",
    ])
    if asset is not None:
        files[f"{ACTION}/bin/{TARGET}/agent-guardrails.gz"] = asset
    return files


def control_tree() -> dict[str, bytes]:
    package = sorted(f"agent_guardrails/{p.name}" for p in (REPOSITORY / "agent_guardrails").glob("*.py"))
    return checkout_files([*package, "action.yml", "run.py", "README.md"])


def stand_in(script: str) -> bytes:
    """A canonical gzip asset, as `gzip -9 -c` writes it, whose 'binary' is
    this shell script."""
    payload = ("#!/bin/sh\n" + script).encode()
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = compressor.compress(payload) + compressor.flush()
    trailer = struct.pack("<II", zlib.crc32(payload), len(payload) & 0xFFFFFFFF)
    return bytes.fromhex("1f8b0800000000000203") + body + trailer


class Runner:
    def __init__(self, test: unittest.TestCase, treatment: str, tree: dict[str, bytes] | None = None) -> None:
        self.test = test
        self.root = Path(tempfile.mkdtemp(prefix="agx-total-run-"))
        test.addCleanup(shutil.rmtree, self.root, True)
        self.work = self.root / "work"
        self.temp = self.work / "_temp"
        self.workspace = self.work / "stickerdaniel-agent-guardrails" / "agent-guardrails"
        self.home = self.root / "home"
        self.actions = self.work / "_actions"
        for directory in (self.temp / "corpora", self.workspace, self.home):
            directory.mkdir(parents=True)
        self.capture = self.temp / "corpora" / "pr-typical.cap"
        self.capture.write_bytes(gen_corpora.generate("pr-typical"))
        third = self.actions / "actions" / "checkout" / ("3d3c42e5aac5ba805825da76410c181273ba90b1")
        third.mkdir(parents=True)
        (third / "action.yml").write_text("name: checkout\n")

        self.contract, _ = total_plan.load()
        self.trees = {"candidate": candidate_tree(), "control": control_tree()}
        if tree is not None:
            self.trees[treatment] = tree
        for name, files in self.trees.items():
            manifest = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}
            self.contract["treatments"][name]["manifest"] = manifest
            self.contract["treatments"][name]["manifest_sha256"] = total_plan.manifest_sha256(manifest)
        version = ".".join(sys.version.split()[0].split(".")[:2])
        self.contract["python"] = {"version": version, "unidata": unicodedata.unidata_version}
        self.contract_path = self.root / "contract.json"
        self.write_contract()
        machine = platform.machine()
        self.job = next(j for j in total_plan.jobs(self.contract)
                        if j["machine"] == machine and j["treatment"] == treatment and j["pair"] == 1)
        self.snapshot = self.job["snapshot"]
        self.other = next(t["snapshot"] for n, t in self.contract["treatments"].items() if n != treatment)
        self.delivered = self.deliver(self.actions)

    def write_contract(self) -> None:
        self.contract_path.write_text(json.dumps(self.contract, indent=2) + "\n")

    def deliver(self, actions: Path, snapshot: str | None = None, files: dict[str, bytes] | None = None) -> Path:
        """What the runner writes: the snapshot's files, and its watermark."""
        snapshot = snapshot or self.snapshot
        files = files if files is not None else self.trees[self.job["treatment"]]
        where = actions / "stickerdaniel" / "agent-guardrails" / snapshot
        for path, data in files.items():
            (where / path).parent.mkdir(parents=True, exist_ok=True)
            (where / path).write_bytes(data)
        Path(str(where) + ".completed").write_text("09/29/2026 10:00:00")
        return where

    def env(self, **overrides: str) -> dict:
        workflow = "stickerdaniel/agent-guardrails/.github/workflows/native-distribution-total.yml"
        env = {
            "PATH": "/usr/bin:/bin", "RUNNER_OS": "Linux", "RUNNER_TEMP": str(self.temp),
            "GITHUB_WORKSPACE": str(self.workspace), "HOME": str(self.home), "GITHUB_JOB": self.job["id"],
            "GITHUB_RUN_ID": "77", "GITHUB_RUN_ATTEMPT": "1", "AGX_SOURCE_SHA": SOURCE,
            "GITHUB_WORKFLOW_REF": f"{workflow}@refs/pull/13/merge",
        }
        env.update(overrides)
        return env

    def run(self, **overrides: str) -> tuple[subprocess.CompletedProcess, dict, dict]:
        done = subprocess.run(
            [sys.executable, "-I", "-B", str(WORKER), "--job", self.job["id"], "--capture", str(self.capture),
             "--contract", str(self.contract_path)],
            env=self.env(**overrides), capture_output=True, text=True, timeout=180,
        )
        lines = done.stdout.splitlines()
        self.test.assertEqual(len(lines), 2, done.stdout + done.stderr)
        self.test.assertTrue(lines[0].startswith("agx-total begin "), lines[0])
        self.test.assertTrue(lines[1].startswith("agx-total end "), lines[1])
        begin = json.loads(lines[0][len("agx-total begin "):])
        end = json.loads(lines[1][len("agx-total end "):])
        # The worker leaves RUNNER_TEMP as it found it, whatever happened.
        self.test.assertEqual(sorted(p.name for p in self.temp.iterdir()), ["corpora"])
        return done, begin, end


class WorkerTestCase(unittest.TestCase):
    def assertRefused(self, done: subprocess.CompletedProcess, end: dict, reason: str, *, ran: bool = False) -> None:
        self.assertEqual(done.returncode, 1, end)
        self.assertIs(end["ok"], False)
        self.assertTrue(any(reason in problem for problem in end["problems"]), (reason, end["problems"]))
        self.assertEqual("check" in end, ran, end)


class PositiveControlTests(WorkerTestCase):
    def assertExpectedCheck(self, runner: Runner, done, begin: dict, end: dict) -> None:
        self.assertEqual(done.returncode, 0, end)
        self.assertEqual(end["problems"], [])
        self.assertIs(end["ok"], True)
        self.assertEqual(end["nonce"], begin["nonce"])
        self.assertEqual({k: end["check"][k] for k in ("exit", "stdout_bytes", "stdout_sha256", "stderr_bytes", "timed_out")},
                         {"exit": 0, "stdout_bytes": 102, "stdout_sha256": hashlib.sha256(EXPECTED_LINE).hexdigest(),
                          "stderr_bytes": 0, "timed_out": False})
        self.assertEqual(end["cleanup"], {"runner_temp_empty": True, "owned_removed": True})
        self.assertEqual(end["delivered"]["relative"], f"_actions/stickerdaniel/agent-guardrails/{runner.snapshot}")
        self.assertEqual(end["delivered"]["search_root"], str(runner.work.resolve()))
        self.assertEqual(end["delivered"]["watermark"], "09/29/2026 10:00:00")
        self.assertEqual(end["delivered"]["manifest_sha256"],
                         runner.contract["treatments"][runner.job["treatment"]]["manifest_sha256"])
        self.assertEqual(set(end["ns"]), {"discover", "verify", "check", "cleanup", "worker"})
        self.assertLess(begin["wall_ns"], end["wall_ns"])
        self.assertEqual(
            {k: begin[k] for k in ("job", "treatment", "snapshot", "source", "run", "attempt", "case")},
            {"job": runner.job["id"], "treatment": runner.job["treatment"], "snapshot": runner.snapshot,
             "source": SOURCE, "run": "77", "attempt": "1", "case": "pr-typical"},
        )

    def test_the_candidate_runs_the_delivered_launcher_and_asset(self) -> None:
        runner = Runner(self, "candidate")
        done, begin, end = runner.run()
        self.assertExpectedCheck(runner, done, begin, end)
        # The real ELF ran: a finding in the capture would have changed the
        # output, and the maximum resident set is that of a native process.
        self.assertGreater(end["check"]["maxrss_kib"], 0)

    def test_the_control_runs_the_captured_driver_on_the_delivered_package(self) -> None:
        runner = Runner(self, "control")
        done, begin, end = runner.run()
        self.assertExpectedCheck(runner, done, begin, end)
        self.assertEqual(end["python"]["executable"], sys.executable)

    def test_the_markers_satisfy_the_analyzer(self) -> None:
        """The worker's real markers, placed after runner lines, pass
        total_analyze.check_job with no problem and no clock anomaly."""
        for treatment in total_plan.TREATMENTS:
            with self.subTest(treatment=treatment):
                runner = Runner(self, treatment)
                done, begin, end = runner.run()
                self.assertEqual(done.returncode, 0, end)
                marks = done.stdout.splitlines()
                contract, digest = total_plan.load(str(runner.contract_path))
                refs = [*contract["third_party"], f"stickerdaniel/agent-guardrails@{runner.snapshot}"]
                base = begin["wall_ns"] // 100 - 50_000_000
                lines = [
                    total_analyze.Line(1, base, "", "Getting action download info"),
                    *(total_analyze.Line(2 + n, base + 1 + n, "", f"Download action repository '{r}' (SHA:{r[-40:]})")
                      for n, r in enumerate(refs)),
                    total_analyze.Line(5, base + 10, "", f"Complete job name: {runner.job['name']}"),
                    total_analyze.Line(6, base + 20, "", f"  ref: {SOURCE}"),
                    total_analyze.Line(7, begin["wall_ns"] // 100 + 3_000, "", marks[0]),
                    total_analyze.Line(8, end["wall_ns"] // 100 + 3_000, "", marks[1]),
                ]
                api = {"status": "completed", "conclusion": "success", "run_attempt": 1, "head_sha": SOURCE,
                       "run_id": 77, "workflow_name": contract["workflow"]["name"]}
                row = total_analyze.check_job(contract, digest, SOURCE, 77, runner.job, lines, api)
                self.assertEqual((row["problems"], row["clock"]), ([], []))
                self.assertGreater(row["total_ns"], 0)


class DiscoveryTests(WorkerTestCase):
    def test_no_delivered_snapshot_is_refused_and_the_checkout_is_never_used(self) -> None:
        runner = Runner(self, "candidate")
        shutil.rmtree(runner.delivered)
        # A perfect copy inside the workspace, where the checkout's own copy
        # of the candidate lives. It must never be found, let alone run.
        runner.deliver(runner.workspace / "_actions")
        done, _, end = runner.run()
        self.assertRefused(done, end, "found 0 delivered copies")

    def test_a_snapshot_named_directory_in_the_workspace_is_not_a_delivery(self) -> None:
        runner = Runner(self, "candidate")
        # Within the search depth, where a second delivery would be refused.
        decoy = runner.workspace / runner.snapshot
        decoy.mkdir()
        (decoy / "launch.sh").write_text("exit 7\n")
        done, _, end = runner.run()
        self.assertEqual(done.returncode, 0, end)
        self.assertEqual(end["delivered"]["relative"], f"_actions/stickerdaniel/agent-guardrails/{runner.snapshot}")

    def test_two_delivered_copies_are_refused(self) -> None:
        runner = Runner(self, "control")
        runner.deliver(runner.work / "_actions2")
        done, _, end = runner.run()
        self.assertRefused(done, end, "found 2 delivered copies")

    def test_the_other_snapshot_delivered_too_is_refused(self) -> None:
        runner = Runner(self, "control")
        runner.deliver(runner.actions, snapshot=runner.other, files=runner.trees["candidate"])
        done, _, end = runner.run()
        self.assertRefused(done, end, "the other treatment's snapshot was delivered too")

    def test_a_symlinked_snapshot_is_refused(self) -> None:
        runner = Runner(self, "candidate")
        elsewhere = runner.root / "cache" / "copy"
        elsewhere.parent.mkdir()
        shutil.move(str(runner.delivered), str(elsewhere))
        runner.delivered.symlink_to(elsewhere, target_is_directory=True)
        done, _, end = runner.run()
        self.assertRefused(done, end, "a symlink carries a snapshot name")

    def test_a_delivery_not_under_owner_and_repo_is_refused(self) -> None:
        runner = Runner(self, "candidate")
        shutil.move(str(runner.delivered), str(runner.work / "_actions" / runner.snapshot))
        done, _, end = runner.run()
        self.assertRefused(done, end, "is not a delivery of stickerdaniel/agent-guardrails")

    def test_a_delivery_the_archive_cache_could_serve_is_refused(self) -> None:
        runner = Runner(self, "candidate")
        cache = runner.root / "actionarchivecache"
        (cache / "stickerdaniel_agent-guardrails").mkdir(parents=True)
        done, _, end = runner.run(ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE=str(cache))
        self.assertRefused(done, end, "action archive cache")

    def test_a_search_root_that_would_hold_home_is_refused(self) -> None:
        runner = Runner(self, "candidate")
        done, _, end = runner.run(HOME=str(runner.work))
        self.assertRefused(done, end, "would contain HOME")

    def test_a_search_beyond_its_directory_budget_is_refused(self) -> None:
        runner = Runner(self, "candidate")
        runner.contract["search"]["max_directories"] = 5
        runner.write_contract()
        done, _, end = runner.run()
        self.assertRefused(done, end, "more than 5 directories")


class VerificationTests(WorkerTestCase):
    def test_a_changed_byte_an_extra_or_a_missing_file_is_refused(self) -> None:
        for change in ("changed", "extra", "missing"):
            with self.subTest(change=change):
                runner = Runner(self, "control")
                if change == "changed":
                    (runner.delivered / "README.md").write_bytes(b"not the README\n")
                elif change == "extra":
                    (runner.delivered / "agent_guardrails" / "planted.txt").write_text("x\n")
                else:
                    (runner.delivered / "run.py").unlink()
                done, _, end = runner.run()
                self.assertRefused(done, end, "the delivered tree differs from the manifest")

    def test_a_changed_adapter_in_the_checkout_is_refused(self) -> None:
        runner = Runner(self, "control")
        manifest = runner.contract["treatments"]["candidate"]["manifest"]
        manifest[f"{ACTION}/bench/py_driver.py"] = "0" * 64
        runner.write_contract()
        done, _, end = runner.run()
        self.assertRefused(done, end, "bench/py_driver.py is not the candidate snapshot's")

    def test_another_job_or_capture_is_refused(self) -> None:
        runner = Runner(self, "candidate")
        done, _, end = runner.run(GITHUB_JOB="x64-pair7-control")
        self.assertRefused(done, end, "GITHUB_JOB is")
        runner.capture.write_bytes(runner.capture.read_bytes() + b"\n")
        done, _, end = runner.run()
        self.assertRefused(done, end, "the capture does not hash")


class OutcomeTests(WorkerTestCase):
    def test_a_different_output_is_refused_after_the_check_ran(self) -> None:
        runner = Runner(self, "control")
        runner.contract["expected"]["stdout_sha256"] = "0" * 64
        runner.write_contract()
        done, _, end = runner.run()
        self.assertRefused(done, end, "is not the expected one", ran=True)
        self.assertEqual(end["cleanup"], {"runner_temp_empty": True, "owned_removed": True})

    def test_a_nonzero_exit_with_the_expected_output_is_refused(self) -> None:
        line = EXPECTED_LINE.decode().rstrip("\n")
        runner = Runner(self, "candidate", candidate_tree(stand_in(f"printf '%s\\n' '{line}'\nexit 1\n")))
        done, _, end = runner.run()
        self.assertRefused(done, end, "'exit': 1", ran=True)
        self.assertEqual(end["check"]["stdout_sha256"], hashlib.sha256(EXPECTED_LINE).hexdigest())

    def test_output_on_stderr_is_refused(self) -> None:
        line = EXPECTED_LINE.decode().rstrip("\n")
        runner = Runner(self, "candidate", candidate_tree(stand_in(f"printf '%s\\n' '{line}'\necho late >&2\n")))
        done, _, end = runner.run()
        self.assertRefused(done, end, "'stderr_bytes': 5", ran=True)

    def test_a_check_that_leaves_files_is_refused_and_still_cleaned_up(self) -> None:
        line = EXPECTED_LINE.decode().rstrip("\n")
        script = f"printf '%s\\n' '{line}'\ntouch \"$RUNNER_TEMP/leftover\"\n"
        runner = Runner(self, "candidate", candidate_tree(stand_in(script)))
        done, _, end = runner.run()
        self.assertRefused(done, end, "left ['leftover']", ran=True)
        self.assertEqual(end["cleanup"], {"runner_temp_empty": False, "owned_removed": True})

    def test_a_check_past_its_deadline_is_killed_and_refused(self) -> None:
        runner = Runner(self, "candidate", candidate_tree(stand_in("sleep 30\n")))
        runner.contract["deadline_per_run_s"] = 1
        runner.write_contract()
        done, _, end = runner.run()
        self.assertRefused(done, end, "did not finish within 1s", ran=True)
        self.assertIs(end["check"]["timed_out"], True)
        self.assertLess(end["ns"]["check"], 10 * 10**9)


if __name__ == "__main__":
    unittest.main()
