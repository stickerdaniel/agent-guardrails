"""The CI cost study's analyzer, on synthetic records shaped like the Jobs
API and the raw job logs GitHub returns."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from e2e import analyze, gen_workloads

from .support import ROOT

_CONTRACT = json.loads((ROOT / "e2e" / "contract.json").read_text(encoding="utf-8"))
_BASE_SECONDS = {"W3": 10, "W1": 14, "W2": 40, "W4": 11}
_OFFSET = {"PY": 0, "TS-H": -2, "RS": -3}
_NUMBERS = {"W3": 101, "W1": 102, "W2": 103, "W4": 104}
_RS_IDENTITY = "agent-guardrails native study; rustc 1.97.1; CPython 3.12.3; UCD 15.0.0; tables " + "d" * 64
_TS_IDENTITY = (
    "agent-guardrails TS-H study; node v24.19.0; CPython 3.12.3; UCD 15.0.0; tables " + "e" * 64
    + "; bundle " + "f" * 64 + "; git-guard " + "9" * 64
)
_IMAGES = {"x64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
_IMAGE_VERSION = "20260920.314.1"
# What actions/runner prints when GitHub's tarball service fails a download
# (ActionManager.cs) and what it throws when an action has no metadata file.
_DOWNLOAD_FAILED = (
    "##[warning]Failed to download action 'https://api.github.com/repos/stickerdaniel/agent-guardrails/"
    "tarball/82427d58bc35a2146e3ce6209bbff8be7e2c6f3f'. Error: Response status code does not indicate "
    "success: 503 (Service Unavailable). (0400:1B2C:3D4E)"
)
_TARBALL = "https://api.github.com/repos/stickerdaniel/agent-guardrails/tarball/82427d58bc35a2146e3ce6209bbff8be7e2c6f3f"
_BACK_OFF = "##[warning]Back off 12.5 seconds before retry."
# Runner v2.337.0 (397b032c) ActionManager.cs:1723, the inner message its
# terminal InfrastructureError prints, and :1769, the generic wrapper that
# also wraps a 404.
_TIMED_OUT = f"##[error]Action '{_TARBALL}' download has timed out. Error: The operation was canceled. 0400:1B2C"
_WRAPPER = "##[error]Failed to download archive '" + _TARBALL + "' after {attempts} attempts."
_NO_MANIFEST ="##[error]Cannot find action.yml, action.yaml or Dockerfile in the downloaded action directory."
_RUNNER_LOST = (
    "The hosted runner: GitHub Actions 1000123 lost communication with the server. Anything in your "
    "workflow that terminates the runner process, starves it for CPU/Memory, or blocks its network "
    "access can cause this error."
)
# SHA-256 of "E2E3-bootstrap-v1|20260929|W1|1|<draw>" mod 12, draws 1 to 12.
_W1_REPLICATE_1 = [4, 4, 6, 9, 10, 4, 6, 8, 9, 1, 0, 2]


def _iso(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _log_time(ticks: int) -> str:
    return f"{_iso(ticks // 10**7)[:-1]}.{ticks % 10**7:07d}Z"


def seconds_of(workload: str, arch: str, variant: str, round_number: int, position: int) -> int:
    """A job's synthetic elapsed seconds, varied per round and position."""
    jitter = hashlib.sha256(f"{workload}|{arch}|{variant}|{round_number}|{position}".encode()).digest()[0] % 6
    return _BASE_SECONDS[workload] + (2 if arch == "arm64" else 0) + _OFFSET[variant] + jitter


def filled_contract(manifest: dict) -> dict:
    """The contract template with every value main fills later."""
    contract = copy.deepcopy(_CONTRACT)
    contract["controller"]["workflow_sha"] = "c" * 40
    for key, fixture in contract["fixtures"]["workloads"].items():
        workload = manifest["workloads"][key]
        fixture["pr_number"] = _NUMBERS[key]
        fixture["author"] = "maintainer"
        fixture["expected_event_digest"] = analyze.event_digest(
            _NUMBERS[key], fixture["head_sha"], contract["fixtures"]["base"]["sha"],
            contract["fixtures"]["base"]["branch"], workload["title"]["text"], workload["body"]["text"], "maintainer",
        )
    runtime = contract["runtime"]
    for arch in analyze.ARCHES:
        runtime["python"][arch] = "Python 3.12.3"
        runtime["git"][arch] = "git version 2.55.0"
        runtime["identity"]["RS"][arch] = _RS_IDENTITY
        runtime["identity"]["TS-H"][arch] = _TS_IDENTITY.replace("node v24.19.0;", "node <node>;")
    runtime["image"] = dict(_IMAGES)
    runtime["image_version"] = {arch: _IMAGE_VERSION for arch in analyze.ARCHES}
    runtime["ts_h_node"] = {arch: "v24.19.0" for arch in analyze.ARCHES}
    return contract


class Study:
    """A ledger and the records of its runs, written on demand."""

    def __init__(self, root: Path, manifest_path: Path, manifest: dict) -> None:
        self.root = root
        self.manifest_path = manifest_path
        self.manifest = manifest
        self.contract = filled_contract(manifest)
        self.dispatches: list[dict] = []
        self.runs: dict[int, dict] = {}

    def dispatch(self, workload: str, order: str, *, phase: str = "measured", replaces=None, round_number: int = 1) -> int:
        seq = len(self.dispatches) + 1
        run_id = 900_000 + seq
        created = 1_791_000_000 + 900 * seq
        contract = self.contract
        fixture = contract["fixtures"]["workloads"][workload]
        expected = self.manifest["workloads"][workload]["expected"]
        jobs, logs = [], {}
        for index, (name, spec) in enumerate(contract["jobs"].items()):
            job_id = run_id * 100 + index
            job = {
                "id": job_id, "run_id": run_id, "run_attempt": 1, "name": name, "status": "completed",
                "html_url": f"https://github.com/stickerdaniel/agent-guardrails/actions/runs/{run_id}/job/{job_id}",
                "labels": [contract["architectures"][spec["arch"]]], "steps": [],
            }
            if spec["order"] != order:
                job.update(conclusion="skipped", started_at=_iso(created), completed_at=_iso(created))
                jobs.append(job)
                continue
            seconds = seconds_of(workload, spec["arch"], spec["variant"], round_number, spec["position"])
            start = created + 5 + 60 * spec["position"]
            end = start + seconds
            job.update(
                conclusion=expected["conclusion"], started_at=_iso(start), completed_at=_iso(end),
                steps=[
                    {"name": "Set up job", "number": 1, "status": "completed", "conclusion": "success",
                     "started_at": _iso(start + 1), "completed_at": _iso(start + 2)},
                    {"name": "E2E trusted event metadata", "number": 2, "status": "completed", "conclusion": "success",
                     "started_at": _iso(start + 2), "completed_at": _iso(start + 2)},
                    {"name": f"Variant {spec['variant']}", "number": 3, "status": "completed",
                     "conclusion": expected["conclusion"], "started_at": _iso(start + 2), "completed_at": _iso(end - 1)},
                    {"name": "Complete job", "number": 4, "status": "completed", "conclusion": "success",
                     "started_at": _iso(end - 1), "completed_at": _iso(end - 1)},
                ],
            )
            jobs.append(job)
            meta = {
                "digest": fixture["expected_event_digest"], "event": "pull_request_target", "action": "labeled",
                "label": f"e2e:order-{order}", "number": str(fixture["pr_number"]),
                "base_ref": "e2e/workload-base", "base_sha": contract["fixtures"]["base"]["sha"],
                "head_sha": fixture["head_sha"], "repository": "stickerdaniel/agent-guardrails",
                "server_url": "https://github.com", "require_model_attribution": "true", "hidden_unicode": "error",
                "workflow_sha": "c" * 40, "workflow_ref": contract["controller"]["workflow_ref"],
                "run_id": str(run_id), "run_attempt": "1", "jq": "jq-1.7.1",
                "sha256sum": "sha256sum (GNU coreutils) 9.4",
            }
            logs[job_id] = {
                "start": start, "name": name, "spec": spec, "meta": meta, "report": list(expected["log_lines"]),
                "sha": contract["variants"][spec["variant"]]["sha"], "git": "git version 2.55.0", "cleanup": True,
                "image": _IMAGES[spec["arch"]], "image_version": _IMAGE_VERSION, "node": "v24.19.0",
                "setup_extra": [], "setup_only": False,
            }
        self.runs[run_id] = {
            "run": {"id": run_id, "run_attempt": 1, "event": "pull_request_target", "status": "completed",
                    "conclusion": expected["conclusion"], "path": ".github/workflows/e2e-benchmark.yml",
                    "created_at": _iso(created)},
            "jobs": jobs,
            "logs": logs,
            "annotations": {},
        }
        base = contract["fixtures"]["base"]["sha"]
        self.dispatches.append({
            "seq": seq, "phase": phase, "workload": workload, "order": order, "run_id": run_id,
            "base_tip_before": base, "base_tip_after": base, "replaces": replaces,
        })
        return run_id

    def measured_series(self) -> None:
        for slot in analyze.schedule():
            self.dispatch(slot["workload"], slot["order"], round_number=slot["round"])

    @staticmethod
    def render(log: dict) -> str:
        """A raw job log as GET .../actions/jobs/<id>/logs returns it."""
        ticks = log["start"] * 10**7 + 1_234_567
        image = (
            ["##[group]Runner Image", f"Image: {log['image']}", f"Version: {log['image_version']}", "##[endgroup]"]
            if log["image"] is not None else []
        )
        lines = [
            "Current runner version: '2.337.0'",
            "##[group]Runner Image Provisioner", "Version: 20260828.587", "##[endgroup]",
            *image,
            "Getting action download info",
            f"Download action repository 'stickerdaniel/agent-guardrails@{log['sha']}' (SHA:{log['sha']})",
            *log["setup_extra"],
        ]
        if log["setup_only"]:
            return "\ufeff" + "".join(f"{_log_time(ticks + index * 1_000_000)} {line}\n" for index, line in enumerate(lines))
        lines += [
            f"Complete job name: {log['name']}",
            "##[group]Run # e2e-metadata v1: a digest of this job's own event; no PR text is printed",
            "\x1b[36;1memit() { printf 'e2e-meta v1 %s=%s\\n' \"$1\" \"$2\"; }\x1b[0m",
            "shell: /usr/bin/bash --noprofile --norc -e -o pipefail {0}",
            "##[endgroup]",
            *(f"e2e-meta v1 {key}={value}" for key, value in log["meta"].items()),
            f"##[group]Run stickerdaniel/agent-guardrails@{log['sha']}",
            "with:", "  require-model-attribution: true", "  hidden-unicode: error", "##[endgroup]",
            {"PY": "Python 3.12.3", "RS": f"agent-guardrails: {_RS_IDENTITY}",
             "TS-H": "agent-guardrails: " + _TS_IDENTITY.replace("node v24.19.0;", "node " + log["node"] + ";")}[
                log["spec"]["variant"]],
            f"agent-guardrails: {log['git']}",
        ]
        for line in log["report"]:
            if line.startswith("agent-guardrails: error: "):
                lines.append("##[error]" + line.split(": ", 3)[-1])
            lines.append(line)
        if log["cleanup"]:
            lines.append("Cleaning up orphan processes")
        return "\ufeff" + "".join(f"{_log_time(ticks + index * 1_000_000)} {line}\n" for index, line in enumerate(lines))

    def write(self) -> dict:
        records = self.root / "records"
        records.mkdir(parents=True, exist_ok=True)
        files = {}

        def save(name: str, data: bytes) -> None:
            path = records / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

        for run_id, run in self.runs.items():
            folder = f"runs/{run_id}"
            save(f"{folder}/run.json", json.dumps(run["run"]).encode())
            save(f"{folder}/jobs.json", json.dumps({"total_count": len(run["jobs"]), "jobs": run["jobs"]}).encode())
            save(f"{folder}/timing.json", json.dumps({"billable": {}, "run_duration_ms": 8000}).encode())
            for job_id, log in run["logs"].items():
                if log is not None:
                    save(f"{folder}/logs/{job_id}.log", self.render(log).encode("utf-8"))
            ran = [job["id"] for job in run["jobs"] if job["conclusion"] != "skipped"]
            for job_id in ran:
                if job_id in run.get("drop_annotations", ()):
                    continue
                messages = run["annotations"].get(job_id, [])
                # As collect.py keeps them for every job that ran: every page
                # gh api --paginate --slurp returned, an empty page when there
                # are none, and the check run that counts them.
                annotations = [
                    {"path": ".github", "start_line": 1, "annotation_level": "failure", "title": "", "message": message}
                    for message in messages
                ]
                pages = [annotations[index:index + 30] for index in range(0, len(annotations), 30)] or [[]]
                count = run.get("annotation_counts", {}).get(job_id, len(annotations))
                if run.get("truncate_annotations"):
                    pages = pages[:1]
                save(f"{folder}/annotations/{job_id}.json", json.dumps(pages).encode())
                check_run = {"id": job_id, "output": {"annotations_count": count}}
                save(f"{folder}/check-runs/{job_id}.json", json.dumps(check_run).encode())
        (records / "index.json").write_text(json.dumps({"files": files}), encoding="utf-8")
        contract = self.root / "contract.json"
        contract.write_text(json.dumps(self.contract), encoding="utf-8")
        ledger = self.root / "ledger.json"
        ledger.write_text(json.dumps({"dispatches": self.dispatches}), encoding="utf-8")
        return {"contract": contract, "ledger": ledger, "records": records}

    def analyze(self) -> dict:
        paths = self.write()
        return analyze.analyze(paths["contract"], self.manifest_path, paths["ledger"], paths["records"])


class AnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workloads = tempfile.TemporaryDirectory()
        out = Path(cls.workloads.name) / "workloads"
        cls.manifest = gen_workloads.generate(out)
        cls.manifest_path = out / "manifest.json"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workloads.cleanup()

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.study = Study(Path(directory.name), self.manifest_path, self.manifest)

    def first(self, workload: str) -> int:
        """The run of the workload's first measured round."""
        return next(
            entry["run_id"] for entry in self.study.dispatches
            if entry["workload"] == workload and entry["phase"] == "measured"
        )

    def active(self, run_id: int, variant: str = "PY", arch: str = "x64") -> dict:
        order = next(entry["order"] for entry in self.study.dispatches if entry["run_id"] == run_id)
        spec_name = next(
            name for name, spec in self.study.contract["jobs"].items()
            if spec["order"] == order and spec["variant"] == variant and spec["arch"] == arch
        )
        return next(job for job in self.study.runs[run_id]["jobs"] if job["name"] == spec_name)

    def log(self, run_id: int, variant: str = "PY", arch: str = "x64") -> dict:
        return self.study.runs[run_id]["logs"][self.active(run_id, variant, arch)["id"]]

    def assert_closed(self, result: dict, judgement: str, text: str) -> None:
        self.assertEqual(result["status"], "closed", result["reasons"])
        self.assertIsNone(result["results"])
        self.assertTrue(
            any(judgement in reason and text in reason for reason in result["reasons"]["close"]),
            result["reasons"]["close"],
        )

    def test_a_complete_attempt_gives_paired_statistics(self) -> None:
        self.study.dispatch("W3", "B", phase="shakedown")
        self.study.measured_series()
        result = self.study.analyze()
        self.assertEqual(result["status"], "complete", result["reasons"])
        self.assertEqual(result["ledger"][0]["judgement"], "excluded (shakedown)")
        self.assertEqual(set(result["results"]), {"W1", "W2", "W3", "W4"})
        expected = []
        for slot in (item for item in analyze.schedule() if item["workload"] == "W1"):
            variants = self.study.contract["orders"][slot["order"]]["variants"]
            expected.append(
                seconds_of("W1", "x64", "TS-H", slot["round"], variants.index("TS-H") + 1)
                - seconds_of("W1", "x64", "PY", slot["round"], variants.index("PY") + 1)
            )
        w1 = result["results"]["W1"]["x64"]
        self.assertEqual(set(w1), {"variants", "contrasts"})
        contrast = w1["contrasts"]["TS-H - PY"]["api_seconds"]
        self.assertEqual(contrast["values"], expected)
        self.assertEqual(contrast["mean"], sum(expected) / 12)
        ordered = sorted(expected)
        self.assertGreater(len(set(expected)), 3)
        self.assertEqual(contrast["median_interval_3rd_10th"], [ordered[2], ordered[9]])
        indices = analyze.bootstrap_indices("W1")
        means = sorted(sum(expected[index] for index in draw) / 12 for draw in indices)
        self.assertEqual(contrast["bootstrap_95"], [means[499], means[19_499]])
        low, high = contrast["bootstrap_95"]
        self.assertTrue(min(expected) < low < contrast["mean"] < high < max(expected))
        precision = contrast["precision_interval_of_mean"]
        self.assertAlmostEqual(precision[0], contrast["mean"] - 2)
        self.assertAlmostEqual(precision[1], contrast["mean"] + 2)
        self.assertEqual(w1["contrasts"]["TS-H - PY"]["public_hosted_billed_difference"], 0)
        self.assertIn("## W1 on x64", analyze.markdown(result))

    def test_only_attempt_one_counts(self) -> None:
        self.study.measured_series()
        self.active(self.first("W3"))["run_attempt"] = 2
        self.assert_closed(self.study.analyze(), analyze.PROTOCOL, "0 jobs named")

    def test_rerun_attempts_are_listed_and_excluded(self) -> None:
        self.study.measured_series()
        run_id = self.first("W1")
        rerun = dict(self.active(run_id), id=1, run_attempt=2, conclusion="failure")
        self.study.runs[run_id]["jobs"].append(rerun)
        result = self.study.analyze()
        self.assertEqual(result["status"], "complete", result["reasons"])
        row = next(row for row in result["ledger"] if row["entry"]["run_id"] == run_id)
        self.assertEqual(row["reruns_excluded"], [{"name": rerun["name"], "id": 1, "attempt": 2, "conclusion": "failure"}])

    def test_a_download_of_another_sha_closes_the_attempt(self) -> None:
        self.study.measured_series()
        self.log(self.first("W2"))["sha"] = self.study.contract["variants"]["RS"]["sha"]
        self.assert_closed(self.study.analyze(), analyze.PROTOCOL, "downloaded")

    def test_a_wrong_event_digest_closes_the_attempt(self) -> None:
        self.study.measured_series()
        self.log(self.first("W1"), "RS", "arm64")["meta"]["digest"] = "0" * 64
        self.assert_closed(self.study.analyze(), analyze.PROTOCOL, "the event's digest")

    def test_only_the_metadata_step_counts_as_metadata(self) -> None:
        self.study.measured_series()
        log = self.log(self.first("W3"))
        # The variant prints what looks like metadata; the step itself printed none.
        log["report"] = [f"e2e-meta v1 {key}={value}" for key, value in log["meta"].items()] + log["report"]
        log["meta"] = {}
        self.assert_closed(self.study.analyze(), analyze.PROTOCOL, "printed no digest")

    def test_a_missing_log_boundary_is_unavailable_never_zero(self) -> None:
        self.study.measured_series()
        run_id = self.first("W3")
        self.log(run_id)["cleanup"] = False
        result = self.study.analyze()
        self.assertEqual(result["status"], "complete", result["reasons"])
        row = next(row for row in result["ledger"] if row["entry"]["run_id"] == run_id)
        spans = next(item for item in row["jobs"] if item["id"] == self.active(run_id)["id"])["spans"]
        self.assertEqual(spans["T_action"], analyze.UNAVAILABLE)
        self.assertEqual(spans["T_post"], analyze.UNAVAILABLE)
        self.assertGreater(spans["T_span"], 0)
        contrast = result["results"]["W3"]["x64"]["contrasts"]["TS-H - PY"]["log_spans_seconds"]
        self.assertEqual(contrast["T_action"]["n"], 11)
        self.assertNotIn("bootstrap_95", contrast["T_action"])
        self.assertIn("bootstrap_95", contrast["T_span"])

    def test_a_duplicate_job_closes_the_attempt(self) -> None:
        self.study.measured_series()
        run_id = self.first("W4")
        self.study.runs[run_id]["jobs"].append(dict(self.active(run_id), id=2))
        self.assert_closed(self.study.analyze(), analyze.PROTOCOL, "2 jobs named")

    def test_a_skipped_active_job_closes_the_attempt(self) -> None:
        self.study.measured_series()
        self.active(self.first("W1"), "TS-H")["conclusion"] = "skipped"
        self.assert_closed(self.study.analyze(), analyze.PROTOCOL, "did not complete")

    def test_an_inactive_job_that_ran_closes_the_attempt(self) -> None:
        self.study.measured_series()
        run_id = self.first("W1")
        next(job for job in self.study.runs[run_id]["jobs"] if job["conclusion"] == "skipped")["conclusion"] = "success"
        self.assert_closed(self.study.analyze(), analyze.PROTOCOL, "is not of order")

    def test_an_unexpected_failure_is_a_candidate_defect(self) -> None:
        self.study.measured_series()
        self.active(self.first("W2"), "RS")["conclusion"] = "failure"
        self.assert_closed(self.study.analyze(), analyze.DEFECT, "expects success")

    def test_a_w4_success_is_a_candidate_defect(self) -> None:
        self.study.measured_series()
        self.active(self.first("W4"), "TS-H", "arm64")["conclusion"] = "success"
        self.assert_closed(self.study.analyze(), analyze.DEFECT, "W4 must fail with exit 1")

    def test_other_report_lines_are_a_candidate_defect(self) -> None:
        self.study.measured_series()
        self.log(self.first("W4"), "RS")["report"].pop(1)
        self.assert_closed(self.study.analyze(), analyze.DEFECT, "report lines differ")

    def test_a_changed_git_version_is_drift(self) -> None:
        self.study.measured_series()
        self.log(self.first("W1"), "RS")["git"] = "git version 2.56.0"
        self.assert_closed(self.study.analyze(), analyze.DRIFT, "git version 2.56.0")

    def test_an_operator_cancel_closes_the_attempt(self) -> None:
        self.study.measured_series()
        run_id = self.first("W2")
        self.study.runs[run_id]["run"]["conclusion"] = "cancelled"
        self.active(run_id, "RS")["conclusion"] = "cancelled"
        self.assert_closed(self.study.analyze(), analyze.CANCELLED, "cancelled")

    def test_a_dispatch_out_of_the_frozen_order_closes_the_attempt(self) -> None:
        self.study.dispatch("W1", "A")
        self.assert_closed(self.study.analyze(), "", "the schedule has W3 order B")

    def test_a_moved_base_closes_the_attempt(self) -> None:
        self.study.measured_series()
        self.study.dispatches[5]["base_tip_after"] = "1" * 40
        self.assert_closed(self.study.analyze(), analyze.PROTOCOL, "base_tip_after")

    def test_a_missing_log_pauses_for_review(self) -> None:
        self.study.measured_series()
        run_id = self.first("W3")
        self.study.runs[run_id]["logs"][self.active(run_id)["id"]] = None
        result = self.study.analyze()
        self.assertEqual(result["status"], "paused", result["reasons"])
        self.assertIn("log is unavailable", result["reasons"]["pause"][0])

    def infrastructure_failure(self, run_id: int, evidence: str = "download") -> dict:
        """One job failed while it was set up, so no step after set-up ran.
        evidence is what the records show: the tarball service failing the
        download, the runner losing its server, nothing at all, or the
        candidate's own package lacking its metadata file."""
        job = self.active(run_id, "TS-H")
        job["conclusion"] = "failure"
        job["steps"][0]["conclusion"] = "failure"
        for step in job["steps"][1:]:
            step.update(conclusion="skipped", started_at=None, completed_at=None)
        log = self.study.runs[run_id]["logs"][job["id"]]
        if evidence == "download":
            final = "##[error]Response status code does not indicate success: 503 (Service Unavailable)."
            retries = [_DOWNLOAD_FAILED, _BACK_OFF] * 2
            log.update(setup_only=True, setup_extra=[*retries, final])
        elif evidence == "timeout":
            log.update(setup_only=True, setup_extra=[_TIMED_OUT])
        elif evidence == "wrapper":
            log.update(setup_only=True, setup_extra=[_WRAPPER.format(attempts=1)])
        elif evidence == "retry then wrapper":
            log.update(setup_only=True, setup_extra=[_DOWNLOAD_FAILED, _BACK_OFF, _WRAPPER.format(attempts=2)])
        elif evidence == "manifest":
            log.update(setup_only=True, setup_extra=[_NO_MANIFEST])
        else:
            self.study.runs[run_id]["logs"][job["id"]] = None
            if evidence == "runner lost":
                self.study.runs[run_id]["annotations"][job["id"]] = [_RUNNER_LOST]
        return job

    def test_an_infrastructure_round_is_replaced_whole(self) -> None:
        self.study.measured_series()
        original = self.study.dispatches[2]
        self.infrastructure_failure(original["run_id"])
        waiting = self.study.analyze()
        self.assertEqual(waiting["status"], "in_progress", waiting["reasons"])
        self.assertEqual(waiting["replacements"]["awaiting"], [original["seq"]])
        self.study.dispatch(original["workload"], original["order"], replaces=original["seq"])
        result = self.study.analyze()
        self.assertEqual(result["status"], "complete", result["reasons"])
        self.assertEqual(result["replacements"]["used"], 1)

    def test_a_w4_job_failed_before_its_variant_is_infrastructure(self) -> None:
        self.study.measured_series()
        original = next(entry for entry in self.study.dispatches if entry["workload"] == "W4")
        self.infrastructure_failure(original["run_id"])
        result = self.study.analyze()
        self.assertEqual(result["status"], "in_progress", result["reasons"])
        self.assertEqual(result["ledger"][original["seq"] - 1]["judgement"], analyze.INFRASTRUCTURE)

    def test_a_set_up_failure_without_provider_evidence_is_ambiguous(self) -> None:
        self.study.measured_series()
        original = self.study.dispatches[0]
        self.infrastructure_failure(original["run_id"], evidence="none")
        result = self.study.analyze()
        self.assertEqual(result["status"], "paused", result["reasons"])
        self.assertEqual(result["ledger"][0]["judgement"], analyze.AMBIGUOUS)
        self.assertEqual(result["replacements"]["awaiting"], [])
        # Nor does a replacement make it one.
        self.study.dispatch(original["workload"], original["order"], replaces=original["seq"])
        self.assert_closed(self.study.analyze(), "", "no unreplaced infrastructure round")

    def test_a_package_without_its_metadata_file_is_a_candidate_defect(self) -> None:
        self.study.measured_series()
        self.infrastructure_failure(self.study.dispatches[0]["run_id"], evidence="manifest")
        result = self.study.analyze()
        self.assert_closed(result, analyze.DEFECT, "Cannot find action.yml")
        self.assertEqual(result["replacements"]["awaiting"], [])

    def test_a_runner_the_server_lost_is_infrastructure(self) -> None:
        self.study.measured_series()
        original = self.study.dispatches[1]
        self.infrastructure_failure(original["run_id"], evidence="runner lost")
        result = self.study.analyze()
        self.assertEqual(result["status"], "in_progress", result["reasons"])
        self.assertEqual(result["replacements"]["awaiting"], [original["seq"]])

    def test_the_generic_download_wrapper_is_not_provider_evidence(self) -> None:
        # Runner v2.337.0 wraps a 404 as well; only debug output would say which.
        for evidence in ("wrapper", "retry then wrapper"):
            with self.subTest(evidence=evidence):
                self.setUp()
                self.study.measured_series()
                original = self.study.dispatches[0]
                self.infrastructure_failure(original["run_id"], evidence=evidence)
                result = self.study.analyze()
                self.assertEqual(result["status"], "paused", result["reasons"])
                self.assertEqual(result["ledger"][0]["judgement"], analyze.AMBIGUOUS)
                self.study.dispatch(original["workload"], original["order"], replaces=original["seq"])
                self.assert_closed(self.study.analyze(), "", "no unreplaced infrastructure round")

    def test_a_terminal_download_timeout_is_infrastructure(self) -> None:
        self.study.measured_series()
        original = self.study.dispatches[0]
        self.infrastructure_failure(original["run_id"], evidence="timeout")
        self.study.dispatch(original["workload"], original["order"], replaces=original["seq"])
        result = self.study.analyze()
        self.assertEqual(result["status"], "complete", result["reasons"])
        self.assertEqual(result["ledger"][0]["judgement"], analyze.INFRASTRUCTURE)

    def test_a_changed_image_on_an_infrastructure_failure_closes(self) -> None:
        self.study.measured_series()
        original = self.study.dispatches[0]
        job = self.infrastructure_failure(original["run_id"])
        self.study.runs[original["run_id"]]["logs"][job["id"]]["image_version"] = "20261001.999.1"
        self.study.dispatch(original["workload"], original["order"], replaces=original["seq"])
        self.assert_closed(self.study.analyze(), analyze.DRIFT, "20261001.999.1")

    def test_a_log_without_its_image_invents_no_drift(self) -> None:
        self.study.measured_series()
        original = self.study.dispatches[0]
        job = self.infrastructure_failure(original["run_id"])
        self.study.runs[original["run_id"]]["logs"][job["id"]]["image"] = None
        result = self.study.analyze()
        self.assertEqual(result["status"], "in_progress", result["reasons"])
        self.assertEqual(result["ledger"][0]["judgement"], analyze.INFRASTRUCTURE)

    def test_a_lost_runner_on_the_31st_annotation_counts(self) -> None:
        self.study.measured_series()
        original = self.study.dispatches[1]
        job = self.infrastructure_failure(original["run_id"], evidence="none")
        noise = ["Process completed with exit code 1."] * 30
        self.study.runs[original["run_id"]]["annotations"][job["id"]] = noise + [_RUNNER_LOST]
        result = self.study.analyze()
        self.assertEqual(result["status"], "in_progress", result["reasons"])
        self.assertEqual(result["replacements"]["awaiting"], [original["seq"]])

    def test_truncated_annotations_are_not_evidence(self) -> None:
        self.study.measured_series()
        original = self.study.dispatches[1]
        job = self.infrastructure_failure(original["run_id"], evidence="none")
        run = self.study.runs[original["run_id"]]
        run["annotations"][job["id"]] = [_RUNNER_LOST] + ["Process completed with exit code 1."] * 30
        run["truncate_annotations"] = True
        result = self.study.analyze()
        self.assertEqual(result["status"], "paused", result["reasons"])
        self.assertIn("annotations", " ".join(result["reasons"]["pause"]))

    def test_missing_annotation_records_are_not_an_empty_set(self) -> None:
        # The log attests a terminal 503, but neither the annotation listing
        # nor its check run was kept: the annotations are unknown, not none.
        self.study.measured_series()
        original = self.study.dispatches[0]
        job = self.infrastructure_failure(original["run_id"])
        self.study.runs[original["run_id"]]["drop_annotations"] = {job["id"]}
        paused = self.study.analyze()
        self.assertEqual(paused["status"], "paused", paused["reasons"])
        self.assertEqual(paused["replacements"]["awaiting"], [])
        self.assertIn("annotations are incomplete", " ".join(paused["reasons"]["pause"]))
        # Nor does a replacement complete the series.
        self.study.dispatch(original["workload"], original["order"], replaces=original["seq"])
        self.assert_closed(self.study.analyze(), "", "no unreplaced infrastructure round")

    def test_a_started_variant_is_never_infrastructure(self) -> None:
        self.study.measured_series()
        job = self.infrastructure_failure(self.study.dispatches[0]["run_id"])
        job["steps"][2].update(conclusion="failure", started_at=job["started_at"], completed_at=job["completed_at"])
        result = self.study.analyze()
        self.assertEqual(result["ledger"][0]["judgement"], analyze.AMBIGUOUS, result["reasons"])
        self.assertEqual(result["replacements"]["awaiting"], [])

    def test_a_reused_run_closes_the_attempt(self) -> None:
        # Twelve real runs, each listed again as rounds 4, 7 and 10 of the
        # same workload and order: without a check, a complete series.
        for slot in analyze.schedule()[:12]:
            self.study.dispatch(slot["workload"], slot["order"], round_number=slot["round"])
        first = {(entry["workload"], entry["order"]): entry for entry in self.study.dispatches}
        self.study.dispatches = [
            dict(first[(slot["workload"], slot["order"])], seq=seq) for seq, slot in enumerate(analyze.schedule(), 1)
        ]
        self.assert_closed(self.study.analyze(), "", "already used by dispatch 1")

    def test_a_shakedown_run_cannot_count_as_measured(self) -> None:
        self.study.dispatch("W3", "B", phase="shakedown")
        self.study.measured_series()
        self.study.dispatches[1]["run_id"] = self.study.dispatches[0]["run_id"]
        self.assert_closed(self.study.analyze(), "", "already used by dispatch 1")

    def test_a_ledger_out_of_run_chronology_closes_the_attempt(self) -> None:
        self.study.measured_series()
        first = self.study.runs[self.study.dispatches[0]["run_id"]]["run"]
        last = self.study.runs[self.study.dispatches[-1]["run_id"]]["run"]
        first["created_at"], last["created_at"] = last["created_at"], first["created_at"]
        self.assert_closed(self.study.analyze(), "", "the ledger order is not the order of the runs")

    def test_overlapping_rounds_close_the_attempt(self) -> None:
        self.study.measured_series()
        earlier = self.study.runs[self.study.dispatches[0]["run_id"]]
        later = self.study.runs[self.study.dispatches[1]["run_id"]]["run"]
        # After the first run began, but before its last job ended.
        later["created_at"] = _iso(analyze.parse_time(earlier["run"]["created_at"]) + 100)
        self.assert_closed(self.study.analyze(), "", "before the jobs of dispatch 1 ended")

    def test_another_image_version_is_drift(self) -> None:
        self.study.measured_series()
        self.log(self.first("W3"), "TS-H")["image_version"] = "20261001.999.1"
        self.assert_closed(self.study.analyze(), analyze.DRIFT, "20261001.999.1")

    def test_another_image_is_drift(self) -> None:
        self.study.measured_series()
        self.log(self.first("W2"), "PY", "arm64")["image"] = "ubuntu-26.04-arm"
        self.assert_closed(self.study.analyze(), analyze.DRIFT, "ubuntu-26.04-arm")

    def test_another_ts_h_node_is_drift(self) -> None:
        self.study.measured_series()
        self.log(self.first("W3"), "TS-H")["node"] = "v24.20.0"
        self.assert_closed(self.study.analyze(), analyze.DRIFT, "v24.20.0")

    def test_an_unobserved_image_closes_the_attempt(self) -> None:
        self.study.measured_series()
        log = self.log(self.first("W1"), "RS")
        log["image_version"] = ""
        self.assert_closed(self.study.analyze(), analyze.PROTOCOL, "no runner image version")

    def test_more_than_three_replacements_close_the_attempt(self) -> None:
        self.study.measured_series()
        originals = list(self.study.dispatches[:4])
        for entry in originals:
            self.infrastructure_failure(entry["run_id"])
        for entry in originals:
            self.study.dispatch(entry["workload"], entry["order"], replaces=entry["seq"])
        result = self.study.analyze()
        self.assertEqual(result["status"], "closed", result["reasons"])
        self.assertIn(f"more than {analyze.MAX_REPLACEMENTS} replacements", " ".join(result["reasons"]["close"]))

    def test_a_replacement_of_a_valid_round_closes_the_attempt(self) -> None:
        self.study.measured_series()
        entry = self.study.dispatches[0]
        self.study.dispatch(entry["workload"], entry["order"], replaces=entry["seq"])
        self.assert_closed(self.study.analyze(), "", "no unreplaced infrastructure round")

    def test_a_record_that_changed_after_collection_is_refused(self) -> None:
        self.study.measured_series()
        paths = self.study.write()
        run_json = next(paths["records"].glob("runs/*/run.json"))
        run_json.write_text(run_json.read_text(encoding="utf-8").replace("completed", "Completed"), encoding="utf-8")
        with self.assertRaisesRegex(analyze.ContractError, "SHA-256"):
            analyze.analyze(paths["contract"], self.manifest_path, paths["ledger"], paths["records"])

    def test_the_contract_must_state_the_frozen_estimators(self) -> None:
        self.study.contract["estimators"]["bootstrap_draws"] = 10_000
        with self.assertRaisesRegex(analyze.ContractError, "bootstrap_draws"):
            self.study.analyze()

    def test_an_unfilled_contract_is_refused(self) -> None:
        self.study.contract["fixtures"]["workloads"]["W1"]["expected_event_digest"] = None
        with self.assertRaisesRegex(analyze.ContractError, "W1.expected_event_digest"):
            self.study.analyze()


class CollectorTests(unittest.TestCase):
    """collect.py against a fake gh api that serves the recorded shapes."""

    def run_collector(self, annotation_pages: list) -> tuple[list, dict, str]:
        from e2e import collect

        base = f"repos/{collect.REPOSITORY}"
        jobs = [
            {"id": 1, "conclusion": "success", "started_at": "2026-10-01T00:00:00Z"},
            {"id": 2, "conclusion": "skipped", "started_at": "2026-10-01T00:00:00Z"},
            {"id": 3, "conclusion": "failure", "started_at": "2026-10-01T00:00:00Z"},
        ]
        lost = [
            {"annotation_level": "failure", "title": "", "message": message}
            for message in ["Process completed with exit code 1."] * 30 + [_RUNNER_LOST]
        ]
        responses = {
            f"{base}/actions/runs/7": b'{"id": 7, "status": "completed"}',
            f"{base}/actions/runs/7/jobs?filter=all&per_page=100": json.dumps({"total_count": 3, "jobs": jobs}).encode(),
            f"{base}/actions/runs/7/timing": b"{}",
            f"{base}/actions/runs/7/logs": b"PK",
            f"{base}/check-runs/1": b'{"id": 1, "output": {"annotations_count": 0}}',
            f"{base}/check-runs/3": b'{"id": 3, "output": {"annotations_count": 31}}',
            # One page of the default 30: what a request without pagination gets.
            f"{base}/check-runs/1/annotations": b"[]",
            f"{base}/check-runs/3/annotations": json.dumps(lost[:30]).encode(),
            f"{base}/actions/jobs/1/logs": b"\xef\xbb\xbf2026-10-01T00:00:00.0000000Z hello\n",
        }
        pages = {
            f"{base}/check-runs/1/annotations?per_page=100": [[]],
            f"{base}/check-runs/3/annotations?per_page=100": [
                [item for item in lost[start:start + 30]] for start in annotation_pages
            ],
        }

        def get(path: str) -> bytes:
            if path not in responses:
                raise collect.CollectError(f"GET {path} failed: HTTP 404")
            return responses[path]

        def get_pages(path: str) -> bytes:
            return json.dumps(pages[path]).encode()

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()) as stderr, \
                mock.patch.object(collect, "gh_get_pages", get_pages, create=True):
            store = collect.Store(Path(directory))
            saved = collect.collect_run(store, 7, get)
            again = collect.collect_run(collect.Store(Path(directory)), 7, get)
            self.assertEqual(saved, again)
            index = json.loads((Path(directory) / "index.json").read_text(encoding="utf-8"))["files"]
            files = {}
            for name in saved:
                data = (Path(directory) / name).read_bytes()
                self.assertEqual(index[name]["sha256"], hashlib.sha256(data).hexdigest())
                files[name] = json.loads(data) if name.endswith(".json") else data
        return saved, files, stderr.getvalue()

    def test_keeps_every_annotation_page_and_the_logs_of_jobs_that_ran(self) -> None:
        saved, files, stderr = self.run_collector([0, 30])
        self.assertEqual(
            [name for name in saved if "/logs/" in name or "/annotations/" in name],
            ["runs/7/annotations/1.json", "runs/7/logs/1.log", "runs/7/annotations/3.json"],
        )
        kept = files["runs/7/annotations/3.json"]
        flat = [item for page in kept for item in (page if isinstance(page, list) else [page])]
        self.assertEqual(len(flat), 31)
        self.assertEqual(flat[-1]["message"], _RUNNER_LOST)
        self.assertEqual(files["runs/7/check-runs/3.json"]["output"]["annotations_count"], 31)
        self.assertIn("no log for job 3", stderr)

    def test_refuses_annotations_fewer_than_the_check_run_counts(self) -> None:
        from e2e import collect

        with self.assertRaisesRegex(collect.CollectError, "31"):
            self.run_collector([0])


class EstimatorTests(unittest.TestCase):
    def test_bootstrap_draws_are_sha256_mod_12(self) -> None:
        for workload, replicate, draw in (("W1", 1, 1), ("W3", 20_000, 12), ("W2", 777, 5)):
            key = f"E2E3-bootstrap-v1|20260929|{workload}|{replicate}|{draw}"
            digest = int.from_bytes(hashlib.sha256(key.encode("ascii")).digest(), "big")
            self.assertEqual(analyze.uniform_round(workload, replicate, draw), digest % 12)
        self.assertEqual([analyze.uniform_round("W1", 1, draw) for draw in range(1, 13)], _W1_REPLICATE_1)
        self.assertEqual(analyze.bootstrap_indices("W1")[0], tuple(_W1_REPLICATE_1))

    def test_a_rejected_digest_is_rehashed_with_a_counter(self) -> None:
        limit = (2**256 // 12) * 12
        seen = []

        def digest(text: str) -> int:
            seen.append(text)
            return limit + 3 if len(seen) < 3 else 12 * 1000 + 7

        self.assertEqual(analyze.uniform_round("W4", 3, 9, digest), 7)
        key = "E2E3-bootstrap-v1|20260929|W4|3|9"
        self.assertEqual(seen, [key, f"{key}|1", f"{key}|2"])

    def test_the_interval_takes_the_500th_and_19500th_replicate_means(self) -> None:
        indices = analyze.bootstrap_indices("W1")
        self.assertEqual(len(indices), 20_000)
        # Distinct powers make adjacent ranks differ, so an off-by-one rank
        # cannot pass. Values computed from hashlib directly.
        values = [13**power for power in range(12)]
        self.assertEqual(analyze.bootstrap_interval(values, indices), [146884477.0, 461368993592.0])

    def test_median_coverage_of_the_3rd_and_10th_order_statistics(self) -> None:
        self.assertEqual(analyze.median_coverage(), 0.96142578125)

    def test_private_minutes_are_ambiguous_across_a_minute_boundary(self) -> None:
        self.assertEqual(analyze.billed_minutes(0), {"minutes": 1, "range": [1, 1]})
        self.assertEqual(analyze.billed_minutes(59), {"minutes": 1, "range": [1, 1]})
        self.assertEqual(analyze.billed_minutes(60), {"minutes": "AMBIGUOUS", "range": [1, 2]})
        self.assertEqual(analyze.billed_minutes(61), {"minutes": "AMBIGUOUS", "range": [1, 2]})
        self.assertEqual(analyze.billed_minutes(62), {"minutes": 2, "range": [2, 2]})
        self.assertEqual(analyze.billed_minutes(120), {"minutes": "AMBIGUOUS", "range": [2, 3]})

    def test_the_event_digest_hashes_the_sorted_projection_and_one_lf(self) -> None:
        text = (
            '{"author":"m","base_ref":"e2e/workload-base","base_sha":"' + "a" * 40 + '","body":null,'
            '"head_sha":"' + "b" * 40 + '","number":3,"title":"t \\"q\\" \\u0001","v":1}\n'
        )
        self.assertEqual(
            analyze.event_digest(3, "b" * 40, "a" * 40, "e2e/workload-base", 't "q" \u0001', None, "m"),
            hashlib.sha256(text.encode()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
