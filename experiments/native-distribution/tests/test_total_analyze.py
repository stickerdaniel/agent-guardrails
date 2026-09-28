"""bench/total_analyze.py on synthetic runs, and the committed contract and
workflow of the total-cost experiment.

SyntheticRun writes what gh returns for a complete run of the committed
workflow: the run object, the Jobs API response and `gh run view --log`
text, with the runner lines the analyzer relies on (shaped like the
retained transfer run's), the checkout's ref line and the worker's markers.
Each test changes one thing and asserts the outcome that change must
produce. Runs anywhere Python does.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = ROOT.parent.parent
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(REPOSITORY / "tests"))
import gen_total  # noqa: E402
import total_analyze  # noqa: E402
import total_plan  # noqa: E402
import yamlsubset  # noqa: E402

SOURCE = "0123456789abcdef0123456789abcdef01234567"
RUN_ID = 4242
MS = 1_000_000
BOM = "\ufeff"


def stamp(ns: int) -> str:
    ticks = ns // 100
    seconds, fraction = divmod(ticks, 10_000_000)
    moment = datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction:07d}Z"


def iso_seconds(ns: int) -> str:
    moment = datetime.datetime.fromtimestamp(ns // 1_000_000_000, tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class SyntheticRun:
    """A valid run, one job after another per chain. totals_ms maps a job
    name to its primary total; everything else is fixed and plausible."""

    def __init__(self, totals_ms: dict[str, float] | None = None, end_lag_ms: dict[str, float] | None = None) -> None:
        self.contract, self.contract_sha256 = total_plan.load()
        self.plan = total_plan.jobs(self.contract)
        totals_ms = totals_ms or {}
        end_lag_ms = end_lag_ms or {}
        self.lines: dict[str, list[list]] = {}
        self.api: dict[str, dict] = {}
        clocks = {arch: 1_790_700_000 * 1_000_000_000 for arch in total_plan.ARCHES}
        for index, job in enumerate(self.plan):
            default = 19_950.0 if job["treatment"] == "candidate" else 20_000.0
            total = int(totals_ms.get(job["name"], default) * MS)
            lag = int(end_lag_ms.get(job["name"], 0.4) * MS)
            t0 = clocks[job["arch"]]
            self.lines[job["name"]] = self._job(job, t0, total, lag, index)
            end = t0 + 35 * MS + total
            self.api[job["name"]] = {
                "id": 9_000 + index, "run_id": RUN_ID, "run_attempt": 1, "head_sha": SOURCE,
                "workflow_name": self.contract["workflow"]["name"], "name": job["name"],
                "status": "completed", "conclusion": "success",
                "created_at": iso_seconds(t0 - 3_000 * MS), "started_at": iso_seconds(t0),
                "completed_at": iso_seconds(end + 100 * MS),
                "runner_name": f"GitHub Actions {1000 + index}", "runner_id": 1000 + index, "labels": [job["runner"]],
            }
            clocks[job["arch"]] = end + 5_000 * MS
        self.run = {
            "id": RUN_ID, "name": self.contract["workflow"]["name"], "path": self.contract["workflow"]["path"],
            "event": "pull_request", "head_sha": SOURCE, "run_attempt": 1,
            "status": "completed", "conclusion": "success",
        }

    def _job(self, job: dict, t0: int, total: int, end_lag: int, index: int) -> list[list]:
        contract = self.contract
        treatment = contract["treatments"][job["treatment"]]
        start = t0 + 35 * MS
        end_line = start + total
        worker = 60 * MS
        begin_line = end_line - worker
        downloads = [*contract["third_party"], f"{contract['delivered_as']}@{job['snapshot']}"]
        nonce = f"{index:032x}"
        begin = {
            "v": 1, "nonce": nonce, "job": job["id"], "arch": job["arch"], "pair": job["pair"],
            "position": job["position"], "treatment": job["treatment"], "snapshot": job["snapshot"],
            "source": SOURCE, "run": str(RUN_ID), "attempt": "1",
            "workflow_ref": f"{contract['delivered_as']}/{contract['workflow']['path']}@refs/pull/13/merge",
            "case": contract["case"], "contract_sha256": self.contract_sha256,
            "wall_ns": begin_line - 300_000,
        }
        end = {
            "v": 1, "nonce": nonce, "ok": True, "problems": [],
            "check": {
                "exit": 0, "timed_out": False, "stdout_bytes": contract["expected"]["stdout_bytes"],
                "stdout_sha256": contract["expected"]["stdout_sha256"], "stderr_bytes": 0, "stderr_head": "",
                "cpu_s": 0.02, "maxrss_kib": 12000,
            },
            "cleanup": {"runner_temp_empty": True, "owned_removed": True},
            "delivered": {
                "relative": f"_actions/{contract['delivered_as']}/{job['snapshot']}", "search_root": "/home/runner/work",
                "directories_seen": 12, "watermark": "09/29/2026 10:00:00", "archive_cache": "/opt/actionarchivecache",
                "archive_cache_entry": False, "files": len(treatment["manifest"]), "bytes": 1000,
                "manifest_sha256": treatment["manifest_sha256"],
            },
            "python": {"executable": "/opt/hostedtoolcache/Python/3.14.7/x64/bin/python", "version": "3.14.7",
                       "unidata": "16.0.0"},
            "ns": {"discover": 1 * MS, "verify": 3 * MS, "check": 30 * MS, "cleanup": 1 * MS,
                   "worker": worker - end_lag + 300_000},
            "wall_ns": end_line - end_lag,
        }
        checkout = "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        lines = [
            ["Set up job", t0, "Current runner version: '2.337.0'"],
            ["Set up job", t0 + 30 * MS, "Prepare all required actions"],
            ["Set up job", start, "Getting action download info"],
            *(["Set up job", start + (250 + 100 * n) * MS, f"Download action repository '{ref}' (SHA:{ref.rsplit('@', 1)[1]})"]
              for n, ref in enumerate(downloads)),
            ["Set up job", start + 700 * MS, f"Complete job name: {job['name']}"],
            [checkout, start + 750 * MS, f"##[group]{checkout}"],
            [checkout, start + 751 * MS, f"  ref: {SOURCE}"],
            ["Run actions/setup-python", start + 1_500 * MS, "##[group]Run actions/setup-python"],
            ["Generate the typical capture", start + 2_500 * MS, "##[group]Run \"$PY314\" -I -B gen_corpora.py"],
            ["Check", begin_line - 5 * MS, "##[group]Run \"$PY314\" -I -B total_run.py"],
            ["Check", begin_line, "agx-total begin " + json.dumps(begin, sort_keys=True, separators=(",", ":"))],
            ["Check", end_line, "agx-total end " + json.dumps(end, sort_keys=True, separators=(",", ":"))],
            ["Complete job", end_line + 50 * MS, "Cleaning up orphan processes"],
        ]
        return lines

    def name(self, arch: str, pair: int, treatment: str) -> str:
        return next(j["name"] for j in self.plan if (j["arch"], j["pair"], j["treatment"]) == (arch, pair, treatment))

    def marker(self, name: str, kind: str, change) -> None:
        for line in self.lines[name]:
            prefix = f"agx-total {kind} "
            if line[2].startswith(prefix):
                payload = json.loads(line[2][len(prefix):])
                change(payload)
                line[2] = prefix + json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def log(self) -> str:
        out = []
        for name, lines in self.lines.items():
            step = None
            for line in lines:
                bom = BOM if line[0] != step else ""
                step = line[0]
                out.append(f"{name}\t{line[0]}\t{bom}{stamp(line[1])} {line[2]}")
        return "\n".join(out) + "\n"

    def jobs_api(self) -> dict:
        jobs = list(self.api.values())
        return {"total_count": len(jobs), "jobs": jobs}

    def analyze(self) -> dict:
        return total_analyze.analyze(self.contract, self.contract_sha256, SOURCE, self.run, self.jobs_api(), self.log())


class AnalyzerTestCase(unittest.TestCase):
    def assertOutcome(self, result: dict, outcome: str, reason: str | None = None) -> None:
        self.assertEqual(result["outcome"], outcome, result["reasons"][:5])
        if reason is not None:
            self.assertTrue(any(reason in r for r in result["reasons"]), (reason, result["reasons"][:8]))


class DecisionTests(AnalyzerTestCase):
    def test_a_saving_in_every_pair_passes(self) -> None:
        result = SyntheticRun().analyze()
        self.assertOutcome(result, "PASS")
        for arch in total_plan.ARCHES:
            summary = result["architectures"][arch]
            self.assertEqual(summary["verdict"], "PASS")
            self.assertEqual(summary["median_difference_ms"], -50.0)
            self.assertEqual([p["first"] for p in summary["pairs"]], SyntheticRun().contract["first"][arch])

    def test_one_pair_without_a_saving_is_inconclusive(self) -> None:
        run = SyntheticRun()
        run = SyntheticRun({run.name("x64", 3, "candidate"): 20_001.0})
        result = run.analyze()
        self.assertOutcome(result, "INCONCLUSIVE", "architecture verdicts")
        self.assertEqual(result["architectures"]["x64"]["verdict"], "INCONCLUSIVE")
        self.assertEqual(result["architectures"]["x64"]["median_difference_ms"], -50.0)
        self.assertEqual(result["architectures"]["arm64"]["verdict"], "PASS")

    def test_a_zero_difference_is_not_a_saving(self) -> None:
        run = SyntheticRun()
        run = SyntheticRun({run.name("arm64", 5, "candidate"): 20_000.0})
        self.assertEqual(run.analyze()["architectures"]["arm64"]["verdict"], "INCONCLUSIVE")

    def test_no_saving_in_any_pair_fails(self) -> None:
        run = SyntheticRun()
        slower = {j["name"]: 20_030.0 for j in run.plan if j["treatment"] == "candidate"}
        slower[run.name("x64", 2, "candidate")] = 20_001.0
        result = SyntheticRun(slower).analyze()
        self.assertOutcome(result, "FAIL")
        self.assertEqual(result["architectures"]["x64"]["min_difference_ms"], 1.0)

    def test_no_saving_within_the_timestamp_uncertainty_is_not_a_failure(self) -> None:
        run = SyntheticRun()
        slower = {j["name"]: 20_030.0 for j in run.plan if j["treatment"] == "candidate"}
        slower[run.name("x64", 2, "candidate")] = 20_000.0
        result = SyntheticRun(slower).analyze()
        self.assertEqual(result["architectures"]["x64"]["min_difference_ms"], 0.0)
        self.assertEqual(result["architectures"]["x64"]["verdict"], "INCONCLUSIVE")
        self.assertEqual(result["architectures"]["arm64"]["verdict"], "FAIL")

    def test_architectures_that_disagree_are_inconclusive(self) -> None:
        run = SyntheticRun()
        slower = {j["name"]: 20_050.0 for j in run.plan if j["treatment"] == "candidate" and j["arch"] == "arm64"}
        result = SyntheticRun(slower).analyze()
        self.assertOutcome(result, "INCONCLUSIVE", "architecture verdicts")
        self.assertEqual(
            [result["architectures"][a]["verdict"] for a in total_plan.ARCHES], ["PASS", "FAIL"]
        )

    def test_a_saving_inside_the_timestamp_uncertainty_is_inconclusive(self) -> None:
        run = SyntheticRun()
        close = run.name("x64", 6, "candidate")
        run = SyntheticRun({close: 19_999.5}, end_lag_ms={run.name("x64", 6, "control"): 1.0})
        summary = run.analyze()["architectures"]["x64"]
        self.assertEqual(summary["max_difference_ms"], -0.5)
        self.assertGreater(summary["max_upper_ms"], 0)
        self.assertEqual(summary["verdict"], "INCONCLUSIVE")


class IncompleteTests(AnalyzerTestCase):
    def test_a_missing_job_is_incomplete(self) -> None:
        run = SyntheticRun()
        name = run.name("arm64", 7, "control")
        del run.lines[name], run.api[name]
        result = run.analyze()
        self.assertOutcome(result, "INCOMPLETE", f"{name}: not in the Jobs API response")
        self.assertEqual(result["architectures"]["arm64"]["verdict"], "INCOMPLETE")

    def test_a_job_only_in_the_api_is_incomplete(self) -> None:
        run = SyntheticRun()
        del run.lines[run.name("x64", 4, "candidate")]
        self.assertOutcome(run.analyze(), "INCOMPLETE", "no log lines")

    def test_a_cancelled_job_is_incomplete(self) -> None:
        run = SyntheticRun()
        run.api[run.name("x64", 1, "control")]["conclusion"] = "cancelled"
        self.assertOutcome(run.analyze(), "INCOMPLETE", "conclusion is 'cancelled'")

    def test_a_second_attempt_is_incomplete(self) -> None:
        run = SyntheticRun()
        run.run["run_attempt"] = 2
        self.assertOutcome(run.analyze(), "INCOMPLETE", "run run_attempt is 2")
        run = SyntheticRun()
        repeated = dict(run.api[run.name("x64", 2, "control")], id=1, run_attempt=2)
        run.api["repeat"] = repeated
        self.assertOutcome(run.analyze(), "INCOMPLETE", "times")

    def test_another_commit_or_workflow_is_incomplete(self) -> None:
        run = SyntheticRun()
        run.run["head_sha"] = "f" * 40
        self.assertOutcome(run.analyze(), "INCOMPLETE", "run head_sha")
        run = SyntheticRun()
        run.marker(run.name("x64", 1, "candidate"), "begin", lambda b: b.update(source="e" * 40))
        self.assertOutcome(run.analyze(), "INCOMPLETE", "begin marker source")
        run = SyntheticRun()
        run.marker(run.name("x64", 1, "candidate"), "begin",
                   lambda b: b.update(workflow_ref=b["workflow_ref"].replace("total", "transfer")))
        self.assertOutcome(run.analyze(), "INCOMPLETE", "workflow_ref")

    def test_another_contract_is_incomplete(self) -> None:
        run = SyntheticRun()
        run.marker(run.name("arm64", 2, "candidate"), "begin", lambda b: b.update(contract_sha256="0" * 64))
        self.assertOutcome(run.analyze(), "INCOMPLETE", "contract_sha256")

    def test_an_undeclared_job_is_incomplete(self) -> None:
        run = SyntheticRun()
        run.lines["total (x64, pair 8, 1st, candidate)"] = run.lines[run.name("x64", 7, "candidate")]
        self.assertOutcome(run.analyze(), "INCOMPLETE", "undeclared jobs")

    def test_a_duplicate_marker_is_incomplete(self) -> None:
        run = SyntheticRun()
        lines = run.lines[run.name("x64", 5, "control")]
        lines.insert(-1, [lines[-2][0], lines[-2][1], lines[-2][2]])
        self.assertOutcome(run.analyze(), "INCOMPLETE", "2 end marker lines, not one")

    def test_a_missing_marker_is_incomplete(self) -> None:
        run = SyntheticRun()
        lines = run.lines[run.name("arm64", 1, "candidate")]
        lines[:] = [line for line in lines if not line[2].startswith("agx-total end ")]
        self.assertOutcome(run.analyze(), "INCOMPLETE", "0 end marker lines")

    def test_the_wrong_first_party_snapshot_is_incomplete(self) -> None:
        run = SyntheticRun()
        name = run.name("x64", 3, "control")
        candidate = run.contract["treatments"]["candidate"]["snapshot"]
        for line in run.lines[name]:
            line[2] = line[2].replace(run.contract["treatments"]["control"]["snapshot"], candidate)
        self.assertOutcome(run.analyze(), "INCOMPLETE", f"{name}: downloads")

    def test_both_snapshots_delivered_is_incomplete(self) -> None:
        run = SyntheticRun()
        name = run.name("arm64", 4, "candidate")
        lines = run.lines[name]
        control = f"{run.contract['delivered_as']}@{run.contract['treatments']['control']['snapshot']}"
        at = next(i for i, line in enumerate(lines) if line[2].startswith("Complete job name"))
        lines.insert(at, ["Set up job", lines[at][1] - 1, f"Download action repository '{control}' (SHA:{control[-40:]})"])
        self.assertOutcome(run.analyze(), "INCOMPLETE", f"{name}: downloads")

    def test_a_ref_resolved_to_another_commit_is_incomplete(self) -> None:
        run = SyntheticRun()
        name = run.name("x64", 6, "candidate")
        for line in run.lines[name]:
            if "agent-guardrails@" in line[2] and line[2].startswith("Download"):
                line[2] = line[2][: -41] + "a" * 40 + ")"
        self.assertOutcome(run.analyze(), "INCOMPLETE", "not a download of a ref resolved to itself")

    def test_boundaries_out_of_order_are_incomplete(self) -> None:
        run = SyntheticRun()
        name = run.name("arm64", 3, "control")
        lines = run.lines[name]
        begin = next(i for i, line in enumerate(lines) if line[2].startswith("agx-total begin"))
        complete = next(i for i, line in enumerate(lines) if line[2].startswith("Complete job name"))
        lines[complete], lines[begin] = lines[begin], lines[complete]
        self.assertOutcome(run.analyze(), "INCOMPLETE", f"{name}: boundaries are out of order")

    def test_another_checkout_is_incomplete(self) -> None:
        run = SyntheticRun()
        name = run.name("x64", 7, "control")
        for line in run.lines[name]:
            line[2] = line[2].replace(f"  ref: {SOURCE}", "  ref: " + "b" * 40)
        self.assertOutcome(run.analyze(), "INCOMPLETE", f"{name}: 0 checkout ref lines")

    def test_wrong_output_is_incomplete(self) -> None:
        cases = [
            (lambda e: e["check"].update(stdout_sha256="0" * 64), "stdout is not the expected output"),
            (lambda e: e["check"].update(stdout_bytes=101), "stdout is not the expected output"),
            (lambda e: e["check"].update(exit=1), "exit status 1"),
            (lambda e: e["check"].update(stderr_bytes=5), "stderr is not empty"),
            (lambda e: e["check"].update(timed_out=True), "timed out"),
            (lambda e: e["cleanup"].update(owned_removed=False), "cleanup did not complete"),
            (lambda e: e["cleanup"].update(runner_temp_empty=False), "cleanup did not complete"),
            (lambda e: e.update(ok=False, problems=["x"]), "the worker reported"),
            (lambda e: e["delivered"].update(manifest_sha256="0" * 64), "not the manifest's"),
            (lambda e: e["delivered"].update(relative="agent-guardrails/agent-guardrails"), "delivered path"),
            (lambda e: e["delivered"].update(archive_cache_entry=True), "archive cache"),
            (lambda e: e["python"].update(version="3.12.3"), "not the declared Python"),
        ]
        for change, reason in cases:
            with self.subTest(reason=reason):
                run = SyntheticRun()
                run.marker(run.name("arm64", 6, "candidate"), "end", change)
                self.assertOutcome(run.analyze(), "INCOMPLETE", reason)

    def test_a_nonce_mismatch_or_reuse_is_incomplete(self) -> None:
        run = SyntheticRun()
        run.marker(run.name("x64", 2, "candidate"), "end", lambda e: e.update(nonce="f" * 32))
        self.assertOutcome(run.analyze(), "INCOMPLETE", "one fresh nonce")
        run = SyntheticRun()
        reused = "a" * 32
        for name in (run.name("x64", 2, "candidate"), run.name("arm64", 2, "candidate")):
            for kind in ("begin", "end"):
                run.marker(name, kind, lambda m: m.update(nonce=reused))
        self.assertOutcome(run.analyze(), "INCOMPLETE", "more than one job")

    def test_a_line_without_a_timestamp_is_incomplete(self) -> None:
        run = SyntheticRun()
        broken = run.log().split("\n")
        broken[1] = broken[1].split("\t")[0] + "\tSet up job\tno timestamp here"
        result = total_analyze.analyze(run.contract, run.contract_sha256, SOURCE, run.run, run.jobs_api(), "\n".join(broken))
        self.assertOutcome(result, "INCOMPLETE", "has no 7-digit UTC timestamp")


class ClockTests(AnalyzerTestCase):
    def test_a_timestamp_going_back_is_inconclusive(self) -> None:
        run = SyntheticRun()
        name = run.name("x64", 4, "control")
        lines = run.lines[name]
        at = next(i for i, line in enumerate(lines) if line[2].startswith("##[group]Run actions/setup-python"))
        lines[at][1] = lines[at - 1][1] - 1 * MS
        self.assertOutcome(run.analyze(), "INCONCLUSIVE", "log time goes back")

    def test_a_late_marker_stamp_is_inconclusive(self) -> None:
        run = SyntheticRun()
        run.marker(run.name("arm64", 5, "control"), "end", lambda e: e.update(wall_ns=e["wall_ns"] - 30 * MS))
        self.assertOutcome(run.analyze(), "INCONCLUSIVE", "stamped 30.400 ms after")

    def test_a_marker_stamped_before_the_worker_clock_is_inconclusive(self) -> None:
        run = SyntheticRun()
        run.marker(run.name("x64", 1, "candidate"), "begin", lambda b: b.update(wall_ns=b["wall_ns"] + 1 * MS))
        self.assertOutcome(run.analyze(), "INCONCLUSIVE", "stamped -0.700 ms after")

    def test_a_worker_interval_that_disagrees_is_inconclusive(self) -> None:
        run = SyntheticRun()
        run.marker(run.name("arm64", 1, "control"), "end", lambda e: e["ns"].update(worker=e["ns"]["worker"] + 40 * MS))
        self.assertOutcome(run.analyze(), "INCONCLUSIVE", "differ by")


class CommandLineTests(AnalyzerTestCase):
    def test_it_refuses_another_contract_and_judges_the_frozen_one(self) -> None:
        run = SyntheticRun()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "run.json").write_text(json.dumps(run.run))
            (directory / "jobs.json").write_text(json.dumps(run.jobs_api()))
            (directory / "run.log").write_text(run.log(), encoding="utf-8")
            base = [sys.executable, "-I", "-B", str(ROOT / "bench" / "total_analyze.py"), "--source", SOURCE,
                    "--run", str(directory / "run.json"), "--jobs", str(directory / "jobs.json"),
                    "--log", str(directory / "run.log"), "--out", str(directory / "analysis.json")]
            refused = subprocess.run([*base, "--contract-sha256", "0" * 64], capture_output=True, text=True)
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertFalse((directory / "analysis.json").exists())
            judged = subprocess.run([*base, "--contract-sha256", run.contract_sha256], capture_output=True, text=True)
            self.assertEqual(judged.returncode, 0, judged.stderr)
            self.assertIn("outcome: PASS", judged.stdout)
            self.assertEqual(json.loads((directory / "analysis.json").read_text())["outcome"], "PASS")


class ContractAndWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract, _ = total_plan.load()
        self.path = REPOSITORY / self.contract["workflow"]["path"]
        self.text = self.path.read_text(encoding="ascii")
        self.workflow = yamlsubset.load(self.text)

    def test_the_committed_workflow_and_contract_agree(self) -> None:
        self.assertEqual(gen_total.check(self.contract, self.text), [])

    def test_the_order_is_balanced_and_comes_from_the_seed(self) -> None:
        first = self.contract["first"]
        self.assertEqual(first, total_plan.first_order(self.contract["seed"], 7))
        self.assertEqual(sorted(first[a].count("candidate") for a in total_plan.ARCHES), [3, 4])
        self.assertEqual(sum(first[a].count("candidate") for a in total_plan.ARCHES), 7)

    def test_it_runs_only_on_the_label(self) -> None:
        self.assertEqual(self.workflow["on"], {"pull_request": {"types": ["labeled"]}})
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertIs(self.workflow["concurrency"]["cancel-in-progress"], False)

    def test_two_chains_of_fourteen_jobs_each_declaring_only_its_own_snapshot(self) -> None:
        jobs = self.workflow["jobs"]
        plan = total_plan.jobs(self.contract)
        self.assertEqual(list(jobs), [job["id"] for job in plan])
        own = {name: t["uses"] for name, t in self.contract["treatments"].items()}
        for job in plan:
            with self.subTest(job=job["id"]):
                declared = jobs[job["id"]]
                self.assertEqual(declared["name"], job["name"])
                self.assertEqual(declared["runs-on"], self.contract["runners"][job["arch"]])
                self.assertEqual(declared["timeout-minutes"], 10)
                if job["needs"] is None:
                    self.assertEqual(declared["if"], "github.event.label.name == 'native-total-cost'")
                    self.assertNotIn("needs", declared)
                else:
                    self.assertEqual(declared["needs"], job["needs"])
                    self.assertNotIn("if", declared)
                uses = [step["uses"] for step in declared["steps"] if "uses" in step]
                self.assertEqual(uses, [*self.contract["third_party"], own[job["treatment"]]])
                checkout = declared["steps"][0]
                self.assertIs(checkout["with"]["persist-credentials"], False)
                deliver = declared["steps"][3]
                self.assertEqual(deliver["if"], "github.run_attempt == '0'")
                self.assertIn(f"--job {job['id']} ", declared["steps"][-1]["run"])
        chains = {arch: [j for j in plan if j["arch"] == arch] for arch in total_plan.ARCHES}
        for chain in chains.values():
            self.assertEqual(len(chain), 14)
            self.assertEqual([j["needs"] for j in chain[1:]], [j["id"] for j in chain[:-1]])
            for pair in range(1, 8):
                self.assertEqual(sorted(j["treatment"] for j in chain if j["pair"] == pair), ["candidate", "control"])

    def test_the_shared_steps_are_identical_in_both_treatments(self) -> None:
        jobs = self.workflow["jobs"]
        steps = {(i, json.dumps(step, sort_keys=True).replace(job, "JOB"))
                 for job, body in jobs.items() for i, step in enumerate(body["steps"]) if i != 3}
        self.assertEqual(len(steps), 4)


if __name__ == "__main__":
    unittest.main()
