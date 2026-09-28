"""Judges one run of the total-cost workflow by bench/total-contract.json.

    python3 -I -B bench/total_analyze.py --contract-sha256 HEX --source SHA \\
        --run run.json --jobs jobs.json --log run.log --out analysis.json

Inputs, all downloaded after the run:
  run.json   gh api repos/OWNER/REPO/actions/runs/RUN
  jobs.json  gh api "repos/OWNER/REPO/actions/runs/RUN/jobs?filter=all&per_page=100"
  run.log    gh run view RUN --repo OWNER/REPO --log   (job TAB step TAB line)

The outcome is INCOMPLETE unless the run, every one of the 28 declared jobs
and nothing else is there, all successful at attempt 1 of the source commit,
and each job's log proves the contract: one 'Getting action download info'
line, then exactly the declared downloads (the two third-party actions and
only this job's first-party snapshot, resolved to itself), then 'Complete
job name' with the declared name, the checkout of the source, then one begin
and one end marker of the worker with matching fresh nonces, the declared
job identity, the frozen contract hash, the expected exit status, stdout
digest and empty stderr, the verified delivery and completed cleanup. Step
names are not used: gh may not know them.

With every job complete, a clock anomaly (a log timestamp going back, a
marker stamped outside 0 to max_marker_lag_ms of the worker's own clock, or
the markers' log interval disagreeing with the worker's monotonic interval)
makes it INCONCLUSIVE. Otherwise each architecture gets the contract's
verdict on its seven paired differences of total time, from 'Getting action
download info' to the end marker, and the outcome is PASS only when both
PASS. Timestamps stay exact integers of 100 ns. Exit status 0 once judged,
2 when the inputs cannot be judged at all (another contract, unreadable).
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import json
import os
import re
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import total_plan  # noqa: E402

STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{7})Z(?: |$)")
DOWNLOAD = re.compile(r"Download action repository '([^'@]+)@([0-9a-f]{40})' \(SHA:([0-9a-f]{40})\)")
NONCE = re.compile(r"[0-9a-f]{32}")
TICK_NS = 100
BEGIN_KEYS = {
    "v", "nonce", "job", "arch", "pair", "position", "treatment", "snapshot", "source", "run",
    "attempt", "workflow_ref", "case", "contract_sha256", "wall_ns",
}
WORKER_STAGES = ("discover", "verify", "check", "cleanup", "worker")


class Line:
    __slots__ = ("index", "ticks", "stamp", "message")

    def __init__(self, index: int, ticks: int, stamp: str, message: str) -> None:
        self.index, self.ticks, self.stamp, self.message = index, ticks, stamp, message


def parse_log(text: str) -> tuple[dict[str, list[Line]], list[str]]:
    """Every line of gh's run log, by job name, with its exact timestamp."""
    jobs: dict[str, list[Line]] = {}
    problems = []
    for index, raw in enumerate(text.split("\n"), start=1):
        if not raw:
            continue
        fields = raw.split("\t", 2)
        if len(fields) != 3:
            problems.append(f"log line {index} is not job, step and text")
            continue
        content = fields[2].removeprefix("\ufeff")
        match = STAMP.match(content)
        if match is None:
            problems.append(f"log line {index} of {fields[0]!r} has no 7-digit UTC timestamp")
            continue
        y, mo, d, h, mi, s, fraction = (int(match.group(i)) for i in range(1, 8))
        ticks = calendar.timegm((y, mo, d, h, mi, s)) * 10_000_000 + fraction
        stamp = content[: match.end()].rstrip()
        jobs.setdefault(fields[0], []).append(Line(index, ticks, stamp, content[match.end():]))
    return jobs, problems


def api_seconds(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def ms(ns: int | None) -> float | None:
    return None if ns is None else round(ns / 1e6, 4)


def check_job(contract: dict, contract_sha256: str, source: str, run_id: int, job: dict,
              lines: list[Line], api: dict | None) -> dict:
    """One job's evidence. problems make the run INCOMPLETE; clock entries
    make its timing uncertain."""
    problems: list[str] = []
    clock: list[str] = []
    row = {k: job[k] for k in ("id", "name", "arch", "pair", "position", "treatment", "snapshot")}
    row.update(problems=problems, clock=clock)
    if api is None:
        problems.append("not in the Jobs API response")
    else:
        wanted = {"status": "completed", "conclusion": "success", "run_attempt": 1, "head_sha": source,
                  "run_id": run_id, "workflow_name": contract["workflow"]["name"]}
        for key, value in wanted.items():
            if api.get(key) != value:
                problems.append(f"Jobs API {key} is {api.get(key)!r}, not {value!r}")
        created, started, completed = (api_seconds(api.get(k)) for k in ("created_at", "started_at", "completed_at"))
        row["api"] = {
            "queue_s": None if created is None or started is None else started - created,
            "job_s": None if started is None or completed is None else completed - started,
            "started_at": api.get("started_at"), "completed_at": api.get("completed_at"),
            "runner_name": api.get("runner_name"), "runner_id": api.get("runner_id"), "labels": api.get("labels"),
        }
    if not lines:
        problems.append("no log lines")
        return row
    for before, after in zip(lines, lines[1:], strict=False):
        if after.ticks < before.ticks:
            clock.append(f"log time goes back from {before.stamp} to {after.stamp} at line {after.index}")

    def single(what: str, test) -> Line | None:
        matches = [line for line in lines if test(line.message)]
        if len(matches) != 1:
            problems.append(f"{len(matches)} {what} lines, not one")
            return None
        return matches[0]

    info = single("'Getting action download info'", lambda m: m == "Getting action download info")
    complete = single("'Complete job name'", lambda m: m.startswith("Complete job name: "))
    if complete is not None and complete.message != f"Complete job name: {job['name']}":
        problems.append(f"{complete.message!r} does not name this job")
    checkout = single("checkout ref", lambda m: m == f"  ref: {source}")
    begin_line = single("begin marker", lambda m: m.startswith("agx-total begin "))
    end_line = single("end marker", lambda m: m.startswith("agx-total end "))

    downloads = [line for line in lines if line.message.startswith("Download action repository ")]
    refs = []
    for line in downloads:
        match = DOWNLOAD.fullmatch(line.message)
        if match is None or match.group(2) != match.group(3):
            problems.append(f"download line {line.index} is not a download of a ref resolved to itself")
            continue
        refs.append(f"{match.group(1)}@{match.group(2)}")
    first_party = f"{contract['delivered_as']}@{job['snapshot']}"
    if sorted(refs) != sorted([*contract["third_party"], first_party]):
        problems.append(f"downloads {sorted(refs)} are not the two third-party actions and {first_party}")

    anchors = (info, complete, checkout, begin_line, end_line)
    if downloads and all(anchor is not None for anchor in anchors):
        sequence = [info.index, *sorted(line.index for line in downloads), complete.index,
                    checkout.index, begin_line.index, end_line.index]
        if sequence != sorted(sequence):
            problems.append("boundaries are out of order")
    if begin_line is None or end_line is None or info is None:
        return row

    try:
        begin = json.loads(begin_line.message[len("agx-total begin "):])
        end = json.loads(end_line.message[len("agx-total end "):])
    except ValueError:
        problems.append("a marker is not one JSON object")
        return row
    if not isinstance(begin, dict) or not isinstance(end, dict):
        problems.append("a marker is not one JSON object")
        return row
    if set(begin) != BEGIN_KEYS:
        problems.append(f"begin marker keys {sorted(begin)} are not the protocol's")
        return row
    declared = {
        "v": 1, "job": job["id"], "arch": job["arch"], "pair": job["pair"], "position": job["position"],
        "treatment": job["treatment"], "snapshot": job["snapshot"], "source": source, "run": str(run_id),
        "attempt": "1", "case": contract["case"], "contract_sha256": contract_sha256,
    }
    for key, value in declared.items():
        if begin.get(key) != value:
            problems.append(f"begin marker {key} is {begin.get(key)!r}, not {value!r}")
    workflow = f"{contract['delivered_as']}/{contract['workflow']['path']}"
    if str(begin["workflow_ref"]).split("@", 1)[0] != workflow:
        problems.append(f"begin marker workflow_ref {begin['workflow_ref']!r} is not {workflow}")
    if not NONCE.fullmatch(str(begin["nonce"])) or end.get("nonce") != begin["nonce"]:
        problems.append("the markers do not share one fresh nonce")
    row["nonce"] = begin["nonce"]

    expected = contract["expected"]
    check = end.get("check") or {}
    delivered = end.get("delivered") or {}
    cleanup = end.get("cleanup") or {}
    python = end.get("python") or {}
    treatment = contract["treatments"][job["treatment"]]
    facts = [
        (end.get("v") == 1, "end marker protocol"),
        (end.get("ok") is True and end.get("problems") == [], f"the worker reported {end.get('problems')!r}"),
        (check.get("timed_out") is False, "the check timed out or did not run"),
        (check.get("exit") == expected["exit"], f"exit status {check.get('exit')!r}"),
        (check.get("stdout_bytes") == expected["stdout_bytes"]
         and check.get("stdout_sha256") == expected["stdout_sha256"], "stdout is not the expected output"),
        (check.get("stderr_bytes") == expected["stderr_bytes"], "stderr is not empty"),
        (cleanup.get("runner_temp_empty") is True and cleanup.get("owned_removed") is True, "cleanup did not complete"),
        (str(delivered.get("relative", "")).split("/")[-3:] == [*contract["delivered_as"].split("/"), job["snapshot"]],
         f"delivered path {delivered.get('relative')!r} is not this snapshot's"),
        (delivered.get("manifest_sha256") == treatment["manifest_sha256"]
         and delivered.get("files") == len(treatment["manifest"]), "the delivered tree is not the manifest's"),
        (delivered.get("archive_cache_entry") is False, "the archive cache could have served the delivery"),
        (str(python.get("version", "")).startswith(contract["python"]["version"] + ".")
         and python.get("unidata") == contract["python"]["unidata"], "not the declared Python"),
    ]
    problems += [what for held, what in facts if not held]
    stages = end.get("ns") or {}
    if not all(isinstance(stages.get(k), int) and stages[k] >= 0 for k in WORKER_STAGES):
        problems.append("the end marker lacks the worker's durations")
        return row
    if not isinstance(begin["wall_ns"], int) or not isinstance(end.get("wall_ns"), int):
        problems.append("a marker lacks its wall clock")
        return row

    limits = contract["clock"]
    begin_lag = begin_line.ticks * TICK_NS - begin["wall_ns"]
    end_lag = end_line.ticks * TICK_NS - end["wall_ns"]
    for name, lag in (("begin", begin_lag), ("end", end_lag)):
        if not 0 <= lag <= limits["max_marker_lag_ms"] * 1_000_000:
            clock.append(f"the {name} marker is stamped {lag / 1e6:.3f} ms after the worker's clock")
    disagreement = (end_line.ticks - begin_line.ticks) * TICK_NS - stages["worker"]
    if abs(disagreement) > limits["max_worker_log_disagreement_ms"] * 1_000_000:
        clock.append(f"the markers' log interval and the worker's differ by {disagreement / 1e6:.3f} ms")

    after = {line.index: line for line in lines}
    download = next((line for line in downloads if line.message.startswith(f"Download action repository '{first_party}'")), None)
    following = after.get(download.index + 1) if download else None
    row.update(
        total_ns=(end_line.ticks - info.ticks) * TICK_NS,
        end_lag_ns=end_lag,
        begin_lag_ns=begin_lag,
        stamps={"start": info.stamp, "end": end_line.stamp},
        diagnostics_ms={
            "before_download_info": ms((info.ticks - lines[0].ticks) * TICK_NS),
            "preparation": ms((complete.ticks - info.ticks) * TICK_NS) if complete else None,
            "first_party_download_line": ms((following.ticks - download.ticks) * TICK_NS) if following else None,
            "preparation_to_worker": ms((begin_line.ticks - complete.ticks) * TICK_NS) if complete else None,
            **{f"worker_{k}": ms(stages[k]) for k in WORKER_STAGES},
            "whole_job_log": ms((lines[-1].ticks - lines[0].ticks) * TICK_NS),
            "worker_log_disagreement": ms(disagreement),
        },
        delivered={k: delivered.get(k) for k in ("relative", "watermark", "archive_cache", "directories_seen", "files", "bytes")},
    )
    return row


def verdict(differences: list[int], uppers: list[int], lowers: list[int]) -> str:
    if statistics.median(differences) < 0 and max(differences) < 0 and max(uppers) < 0:
        return "PASS"
    if min(differences) >= 0 and min(lowers) >= 0:
        return "FAIL"
    return "INCONCLUSIVE"


def analyze(contract: dict, contract_sha256: str, source: str, run: dict, jobs_api: dict, log_text: str) -> dict:
    problems: list[str] = []
    wanted = {"event": "pull_request", "head_sha": source, "run_attempt": 1, "status": "completed",
              "conclusion": "success", "name": contract["workflow"]["name"]}
    for key, value in wanted.items():
        if run.get(key) != value:
            problems.append(f"run {key} is {run.get(key)!r}, not {value!r}")
    path = str(run.get("path", ""))
    if path != contract["workflow"]["path"] and not path.startswith(contract["workflow"]["path"] + "@"):
        problems.append(f"run path {path!r} is not {contract['workflow']['path']}")
    run_id = run.get("id")
    if not isinstance(run_id, int):
        problems.append("the run has no numeric id")

    plan = total_plan.jobs(contract)
    declared = [job["name"] for job in plan]
    api_jobs = jobs_api.get("jobs", [])
    if jobs_api.get("total_count") != len(api_jobs):
        problems.append("the Jobs API response is not complete in one page")
    api_names = [job.get("name") for job in api_jobs]
    for name in sorted(set(api_names)):
        if api_names.count(name) > 1:
            problems.append(f"the Jobs API lists {name!r} {api_names.count(name)} times")
    if sorted(set(api_names) - set(declared)):
        problems.append(f"the Jobs API lists undeclared jobs {sorted(set(api_names) - set(declared))}")
    by_api = {job.get("name"): job for job in api_jobs}

    logs, log_problems = parse_log(log_text)
    problems += log_problems
    if sorted(set(logs) - set(declared)):
        problems.append(f"the log has undeclared jobs {sorted(set(logs) - set(declared))}")

    rows = {job["name"]: check_job(contract, contract_sha256, source, run_id, job, logs.get(job["name"], []),
                                   by_api.get(job["name"])) for job in plan}
    nonces = [row["nonce"] for row in rows.values() if "nonce" in row]
    if len(set(nonces)) != len(nonces):
        problems.append("a nonce appears in more than one job")
    incomplete = problems + [f"{name}: {p}" for name, row in rows.items() for p in row["problems"]]
    anomalies = [f"{name}: {c}" for name, row in rows.items() for c in row["clock"]]

    arches = {}
    for arch in total_plan.ARCHES:
        pairs = []
        for pair in range(1, contract["pairs"] + 1):
            candidate, control = (rows[j["name"]] for t in total_plan.TREATMENTS for j in plan
                                  if j["arch"] == arch and j["pair"] == pair and j["treatment"] == t)
            if "total_ns" not in candidate or "total_ns" not in control:
                pairs.append({"pair": pair, "complete": False})
                continue
            difference = candidate["total_ns"] - control["total_ns"]
            resolution = 4 * TICK_NS
            pairs.append({
                "pair": pair, "complete": True, "first": contract["first"][arch][pair - 1],
                "candidate_ms": ms(candidate["total_ns"]), "control_ms": ms(control["total_ns"]),
                "difference_ns": difference, "difference_ms": ms(difference),
                "upper_ns": difference + control["end_lag_ns"] + resolution,
                "lower_ns": difference - candidate["end_lag_ns"] - resolution,
            })
        summary = {"pairs": pairs}
        if all(p["complete"] for p in pairs) and len(pairs) == contract["pairs"]:
            differences = [p["difference_ns"] for p in pairs]
            summary.update(
                median_difference_ms=ms(statistics.median(differences)),
                min_difference_ms=ms(min(differences)),
                max_difference_ms=ms(max(differences)),
                max_upper_ms=ms(max(p["upper_ns"] for p in pairs)),
                min_lower_ms=ms(min(p["lower_ns"] for p in pairs)),
                verdict=verdict(differences, [p["upper_ns"] for p in pairs], [p["lower_ns"] for p in pairs]),
            )
        else:
            summary["verdict"] = "INCOMPLETE"
        # Beside the verdict only: never subtracted from a total or summed.
        diagnostics: dict[str, dict] = {}
        for treatment in total_plan.TREATMENTS:
            own = [row["diagnostics_ms"] for row in rows.values()
                   if row["arch"] == arch and row["treatment"] == treatment and "diagnostics_ms" in row]
            medians = diagnostics.setdefault(treatment, {})
            for key in own[0] if own else ():
                values = [d[key] for d in own if d[key] is not None]
                if values:
                    medians[key] = statistics.median(values)
        summary["diagnostic_medians_ms"] = diagnostics
        arches[arch] = summary

    verdicts = [arches[arch]["verdict"] for arch in total_plan.ARCHES]
    if incomplete or "INCOMPLETE" in verdicts:
        outcome, reasons = "INCOMPLETE", incomplete or ["a pair has no total"]
    elif anomalies:
        outcome, reasons = "INCONCLUSIVE", ["clock anomaly, timestamps uncertain", *anomalies]
    elif verdicts == ["PASS", "PASS"]:
        outcome, reasons = "PASS", []
    elif verdicts == ["FAIL", "FAIL"]:
        outcome, reasons = "FAIL", []
    else:
        outcome, reasons = "INCONCLUSIVE", [f"architecture verdicts {dict(zip(total_plan.ARCHES, verdicts, strict=True))}"]
    return {
        "outcome": outcome,
        "reasons": reasons,
        "contract_sha256": contract_sha256,
        "source": source,
        "run": run_id,
        "architectures": arches,
        "sign_test": contract["decision"]["sign_test"],
        "scope": contract["scope"],
        "jobs": list(rows.values()),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--contract-sha256", required=True, help="the frozen contract's sha256")
    parser.add_argument("--source", required=True, help="the commit the run was for")
    parser.add_argument("--run", required=True)
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    contract, contract_sha256 = total_plan.load()
    if contract_sha256 != args.contract_sha256:
        print(f"total_analyze: bench/total-contract.json is {contract_sha256}, not the frozen one", file=sys.stderr)
        return 2
    try:
        with open(args.run, encoding="utf-8") as handle:
            run = json.load(handle)
        with open(args.jobs, encoding="utf-8") as handle:
            jobs_api = json.load(handle)
        with open(args.log, encoding="utf-8", newline="") as handle:
            log_text = handle.read()
    except (OSError, ValueError) as error:
        print(f"total_analyze: cannot read the inputs: {error}", file=sys.stderr)
        return 2
    result = analyze(contract, contract_sha256, args.source, run, jobs_api, log_text)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=1)
        handle.write("\n")
    print(f"outcome: {result['outcome']}")
    for reason in result["reasons"][:20]:
        print(f"  {reason}")
    for arch, summary in result["architectures"].items():
        cells = " ".join(f"{p['difference_ms']:+.3f}" for p in summary["pairs"] if p["complete"])
        print(f"{arch}: {summary['verdict']} median {summary.get('median_difference_ms')} ms "
              f"range [{summary.get('min_difference_ms')}, {summary.get('max_difference_ms')}] ms; pairs {cells}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
