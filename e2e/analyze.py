"""Analyze the E2E-3 CI cost study under its frozen contract.

    python3 e2e/analyze.py schedule
    python3 e2e/analyze.py digest --contract CONTRACT --manifest MANIFEST
    python3 e2e/analyze.py analyze --contract CONTRACT --manifest MANIFEST \\
        --ledger LEDGER --records RECORDS --json RESULT.json --markdown REPORT.md

schedule prints the frozen dispatch order. digest prints the event digest the
trusted metadata step must print for each fixture pull request, once the
contract names its number and author; main compares it with the shakedown.

analyze reads the records collect.py saved (run, jobs and timing JSON and
each job's raw log, all listed with SHA-256 in RECORDS/index.json) and the
ledger main keeps while dispatching. The ledger is a JSON object:

    {"dispatches": [
      {"seq": 1, "phase": "shakedown" | "measured", "workload": "W3",
       "order": "B", "run_id": 123, "base_tip_before": SHA, "base_tip_after": SHA,
       "replaces": null or the seq of the round it replaces, "note": "..."}]}

in dispatch order. Measured dispatches that replace nothing must follow the
frozen schedule exactly. Every job of attempt 1 is validated against the
contract; reruns are listed and excluded. A round counts only when all six of
its jobs are valid. The result says whether the attempt is complete, still
in progress, paused for review, or closed, and why. Statistics are computed
only for a complete attempt: twelve matched rounds per workload and
architecture. Nothing is pooled across workloads and no overall winner is
named. Stdlib only; the same input gives the same output.
"""

from __future__ import annotations

import argparse
import calendar
import functools
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SEED = 20260929
CYCLE = ("W3", "W1", "W2", "W4")
ORDER_NAMES = ("A", "B", "C")
FROZEN_ROTATIONS = {"W3": 1, "W1": 0, "W2": 2, "W4": 1}
ROUNDS = 12
VARIANTS = ("PY", "TS-H", "RS")
CHALLENGERS = ("TS-H", "RS")
ARCHES = ("x64", "arm64")
BOOTSTRAP_DRAWS = 20_000
LOWER_RANK = 500
UPPER_RANK = 19_500
MEDIAN_ORDER = (3, 10)
MEDIAN_COVERAGE = 0.96142578125
JOB_PRECISION = 1
MAX_REPLACEMENTS = 3
UNAVAILABLE = "unavailable"
SPANS = ("T_span", "T_download", "T_metadata", "T_action", "T_post")
METADATA_KEYS = (
    "digest", "event", "action", "label", "number", "base_ref", "base_sha", "head_sha",
    "repository", "server_url", "require_model_attribution", "hidden_unicode",
    "workflow_sha", "workflow_ref", "run_id", "run_attempt", "jq", "sha256sum",
)

# How a job or a round is judged, from harmless to final. A round takes the
# worst judgement of its jobs.
VALID = "valid"
INFRASTRUCTURE = "infrastructure"
AMBIGUOUS = "ambiguous"
DEFECT = "candidate_defect"
PROTOCOL = "protocol_invalid"
DRIFT = "drift"
CANCELLED = "operator_cancel"
_RANK = {VALID: 0, INFRASTRUCTURE: 1, AMBIGUOUS: 2, DEFECT: 3, PROTOCOL: 4, DRIFT: 5, CANCELLED: 6}
CLOSING = frozenset({DEFECT, PROTOCOL, DRIFT, CANCELLED})


class ContractError(Exception):
    pass


# --- Schedule ----------------------------------------------------------------


def rotation(workload: str) -> int:
    digest = hashlib.sha256(f"E2E3-v1|{SEED}|{workload}".encode("ascii")).digest()
    return int.from_bytes(digest, "big") % 3


def schedule() -> list[dict]:
    """The frozen measured dispatch order: rounds 1 to 12, each a cycle
    W3, W1, W2, W4, each workload's labels rotated by its seeded offset."""
    rotations = {workload: rotation(workload) for workload in CYCLE}
    if rotations != FROZEN_ROTATIONS:
        raise AssertionError(f"rotations {rotations} differ from the frozen {FROZEN_ROTATIONS}")
    sequence = []
    for round_number in range(1, ROUNDS + 1):
        for workload in CYCLE:
            order = ORDER_NAMES[(rotations[workload] + round_number - 1) % 3]
            sequence.append(
                {"dispatch": len(sequence) + 1, "round": round_number, "workload": workload, "order": order}
            )
    return sequence


# --- Bootstrap ---------------------------------------------------------------


def _sha256_int(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("ascii")).digest(), "big")


def uniform_round(workload: str, replicate: int, draw: int, digest=_sha256_int) -> int:
    """One resampled round index in 0..11. A digest at or above the largest
    multiple of 12 below 2^256 is rejected and the key rehashed with a
    counter suffix, so every index is exactly equally likely."""
    key = f"E2E3-bootstrap-v1|{SEED}|{workload}|{replicate}|{draw}"
    limit = (2**256 // ROUNDS) * ROUNDS
    counter = 0
    candidate = key
    while True:
        value = digest(candidate)
        if value < limit:
            return value % ROUNDS
        counter += 1
        candidate = f"{key}|{counter}"


@functools.lru_cache(maxsize=None)
def bootstrap_indices(workload: str) -> tuple[tuple[int, ...], ...]:
    """Replicates 1..20,000, each twelve draws 1..12 of whole rounds."""
    return tuple(
        tuple(uniform_round(workload, replicate, draw) for draw in range(1, ROUNDS + 1))
        for replicate in range(1, BOOTSTRAP_DRAWS + 1)
    )


def bootstrap_interval(values: list, indices: list[tuple[int, ...]]) -> list[float]:
    """Nearest-rank 500th and 19,500th of the sorted replicate means."""
    means = sorted(sum(values[index] for index in draw) / ROUNDS for draw in indices)
    return [means[LOWER_RANK - 1], means[UPPER_RANK - 1]]


def median_coverage() -> float:
    low, high = MEDIAN_ORDER
    return sum(math.comb(ROUNDS, k) for k in range(low, high)) / 2**ROUNDS


# --- Event digest --------------------------------------------------------------


_JQ_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _jq_string(text: str) -> str:
    out = []
    for char in text:
        if char in _JQ_ESCAPES:
            out.append(_JQ_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def event_digest(
    number: int, head_sha: str, base_sha: str, base_ref: str, title: str, body, author: str
) -> str:
    """SHA-256 of what the metadata step hashes: jq -cS of the projection,
    keys sorted, then jq's LF. A null body differs from an empty one."""
    fields = {
        "author": _jq_string(author),
        "base_ref": _jq_string(base_ref),
        "base_sha": _jq_string(base_sha),
        "body": "null" if body is None else _jq_string(body),
        "head_sha": _jq_string(head_sha),
        "number": str(int(number)),
        "title": _jq_string(title),
        "v": "1",
    }
    text = "{" + ",".join(f'"{key}":{value}' for key, value in fields.items()) + "}\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Records -----------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Records:
    """The collected records, each read only after its hash is checked."""

    def __init__(self, root: Path) -> None:
        self.root = root
        index_bytes = (root / "index.json").read_bytes()
        self.index_sha256 = _sha256_bytes(index_bytes)
        self.index = json.loads(index_bytes)["files"]

    def read(self, name: str) -> bytes | None:
        entry = self.index.get(name)
        if entry is None:
            return None
        data = (self.root / name).read_bytes()
        if _sha256_bytes(data) != entry["sha256"] or len(data) != entry["bytes"]:
            raise ContractError(f"{name} does not match its recorded SHA-256")
        return data

    def json(self, name: str):
        data = self.read(name)
        return None if data is None else json.loads(data)


def parse_time(text) -> int | None:
    """Seconds since the epoch of a Jobs API timestamp such as
    2026-09-29T05:03:07Z."""
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)Z", text)
    if match is None:
        return None
    return calendar.timegm(tuple(int(part) for part in match.groups()) + (0, 0, 0))


_LOG_LINE = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,7}))?Z (.*)", re.DOTALL)
_METADATA_GROUP = "##[group]Run # e2e-metadata v1"
_ACTION_GROUP = re.compile(r"##\[group\]Run stickerdaniel/agent-guardrails@([0-9a-f]{40})")
_DOWNLOAD = re.compile(r"Download action repository '([^']*)' \(SHA:([0-9a-f]{40})\)")
_META = re.compile(r"e2e-meta v1 ([a-z0-9_]+)=(.*)")
_IDENTITY = re.compile(r"agent-guardrails: (agent-guardrails (?:native|TS-H) study; .*)")
_NODE = re.compile(r"node (v\d+\.\d+\.\d+);")
_GIT_VERSION = "agent-guardrails: git version "
_STEP_END = ("Post job cleanup.", "Cleaning up orphan processes")


@dataclass
class JobLog:
    lines: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    metadata_problems: list = field(default_factory=list)
    downloads: list = field(default_factory=list)
    action_sha: str | None = None
    action_started: bool = False
    python: str | None = None
    git: str | None = None
    identity: str | None = None
    node: str | None = None
    report: list = field(default_factory=list)
    runner_version: str | None = None
    image: str | None = None
    image_version: str | None = None
    spans: dict = field(default_factory=dict)


def _ticks(match) -> int:
    year, month, day, hour, minute, second, fraction = match.groups()[:7]
    seconds = calendar.timegm((int(year), int(month), int(day), int(hour), int(minute), int(second), 0, 0, 0))
    return seconds * 10**7 + int((fraction or "0").ljust(7, "0"))


def parse_log(text: str) -> JobLog:
    """What one raw job log says, by position: the metadata step's section,
    the action's section, and the lines that bound each span. A boundary
    that is not there leaves its span unavailable, never zero."""
    log = JobLog()
    for raw in text.split("\n"):
        raw = raw.rstrip("\r").lstrip("﻿")
        if not raw:
            continue
        match = _LOG_LINE.fullmatch(raw)
        log.lines.append((_ticks(match), match.group(8)) if match else (None, raw))
    texts = [line for _, line in log.lines]

    def first(predicate, start: int = 0) -> int | None:
        return next((index for index in range(start, len(texts)) if predicate(texts[index])), None)

    meta_start = first(lambda line: line.startswith(_METADATA_GROUP))
    action_start = first(lambda line: _ACTION_GROUP.fullmatch(line) is not None, meta_start or 0)
    end = first(lambda line: line in _STEP_END, action_start) if action_start is not None else None
    if meta_start is not None:
        for line in texts[meta_start:action_start if action_start is not None else len(texts)]:
            match = _META.fullmatch(line)
            if match:
                key, value = match.groups()
                if key in log.metadata:
                    log.metadata_problems.append(f"metadata key {key} appears twice")
                log.metadata[key] = value
    if action_start is not None:
        log.action_started = True
        log.action_sha = _ACTION_GROUP.fullmatch(texts[action_start]).group(1)
        for line in texts[action_start:end]:
            if line.startswith(_GIT_VERSION) and log.git is None:
                log.git = line[len("agent-guardrails: "):]
            elif _IDENTITY.fullmatch(line) and log.identity is None:
                log.identity = _IDENTITY.fullmatch(line).group(1)
                node = _NODE.search(log.identity)
                log.node = node.group(1) if node else None
            elif re.fullmatch(r"Python \d+\.\d+\.\d+", line) and log.python is None:
                log.python = line
            elif line.startswith("agent-guardrails: "):
                log.report.append(line)
    for index, line in enumerate(texts):
        download = _DOWNLOAD.fullmatch(line)
        if download:
            log.downloads.append((download.group(1), download.group(2)))
        if line.startswith("Current runner version: "):
            log.runner_version = line[len("Current runner version: "):].strip("'")
        if line == "##[group]Runner Image":
            for inner in texts[index + 1:]:
                if inner == "##[endgroup]":
                    break
                if inner.startswith("Image: "):
                    log.image = inner[len("Image: "):]
                if inner.startswith("Version: "):
                    log.image_version = inner[len("Version: "):]

    def at(index) -> int | None:
        return None if index is None else log.lines[index][0]

    timed = [ticks for ticks, _ in log.lines if ticks is not None]
    download_start = first(lambda line: line == "Getting action download info")
    download_end = first(lambda line: line.startswith("Complete job name: "), download_start or 0)

    def span(start, stop):
        if start is None or stop is None or stop < start:
            return UNAVAILABLE
        return (stop - start) / 10**7

    log.spans = {
        "T_span": span(timed[0], timed[-1]) if timed else UNAVAILABLE,
        "T_download": span(at(download_start), at(download_end)),
        "T_metadata": span(at(meta_start), at(action_start)),
        "T_action": span(at(action_start), at(end)),
        "T_post": span(at(end), timed[-1] if timed else None),
    }
    return log


# --- Contract ----------------------------------------------------------------


def load_contract(path: Path, manifest_path: Path) -> tuple[dict, dict, str, str]:
    """The contract and the workload manifest it binds, with their SHA-256s.
    Every value main fills before the measured series must be present."""
    contract_bytes = path.read_bytes()
    contract = json.loads(contract_bytes)
    manifest_bytes = manifest_path.read_bytes()
    bound = contract["fixtures"]["workload_manifest"]
    if _sha256_bytes(manifest_bytes) != bound["sha256"] or len(manifest_bytes) != bound["bytes"]:
        raise ContractError("the workload manifest is not the one the contract binds")
    manifest = json.loads(manifest_bytes)
    missing = [name for name, value in _walk(contract) if value is None]
    if missing:
        raise ContractError(f"the contract still has unfilled values: {', '.join(missing)}")
    _check_frozen(contract, manifest)
    return contract, manifest, _sha256_bytes(contract_bytes), _sha256_bytes(manifest_bytes)


def _walk(value, prefix: str = ""):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _walk(inner, f"{prefix}.{key}" if prefix else key)
    else:
        yield prefix, value


def _check_frozen(contract: dict, manifest: dict) -> None:
    """The contract may not state another estimator than this code runs."""
    estimators = contract["estimators"]
    expected = {
        "rounds_per_workload": ROUNDS,
        "bootstrap_draws": BOOTSTRAP_DRAWS,
        "bootstrap_ranks": [LOWER_RANK, UPPER_RANK],
        "median_order_statistics": list(MEDIAN_ORDER),
        "median_coverage": MEDIAN_COVERAGE,
        "job_precision_seconds": JOB_PRECISION,
        "difference_precision_seconds": 2 * JOB_PRECISION,
        "max_replacements": MAX_REPLACEMENTS,
    }
    for key, value in expected.items():
        if estimators.get(key) != value:
            raise ContractError(
                f"contract estimator {key} is {estimators.get(key)!r}, the analyzer runs {value!r}"
            )
    if contract["schedule"]["sequence"] != schedule():
        raise ContractError("the contract's schedule is not the frozen one")
    for kind in ("terminal_error", "annotation", "candidate"):
        patterns = contract["infrastructure_evidence"].get(kind)
        if not isinstance(patterns, list) or not patterns:
            raise ContractError(f"the contract lists no {kind} evidence patterns")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error:
                raise ContractError(
                    f"the {kind} evidence pattern {pattern!r} is not a regular expression"
                ) from None
    if contract["fixtures"]["base"]["sha"] != manifest["base"]["commit"]:
        raise ContractError("the contract's base differs from the manifest's")
    for key, workload in contract["fixtures"]["workloads"].items():
        if workload["head_sha"] != manifest["workloads"][key]["head_sha"]:
            raise ContractError(f"the contract's {key} head differs from the manifest's")


# --- Validation ----------------------------------------------------------------


@dataclass
class JobResult:
    name: str
    job_id: int | None
    arch: str
    order: str
    position: int
    variant: str
    judgement: str = VALID
    problems: list = field(default_factory=list)
    seconds: int | None = None
    steps: dict = field(default_factory=dict)
    spans: dict = field(default_factory=dict)
    runtime: dict = field(default_factory=dict)
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    url: str | None = None

    def flag(self, judgement: str, problem: str) -> None:
        self.problems.append(f"{judgement}: {problem}")
        if _RANK[judgement] > _RANK[self.judgement]:
            self.judgement = judgement


def _step(job: dict, name: str) -> dict | None:
    return next((step for step in job.get("steps") or [] if step.get("name") == name), None)


def _variant_started(job: dict, step_name: str, log: JobLog | None) -> bool:
    """Whether either record shows the variant's step beginning: the Jobs
    API step, or the step's group line in the log."""
    step = _step(job, step_name)
    if step is not None and (step.get("started_at") or step.get("conclusion") not in (None, "skipped")):
        return True
    return log is not None and log.action_started


def _evidence(log: JobLog | None, annotations: list, patterns: dict) -> tuple[str | None, str | None]:
    """What the records say about a job that failed before its variant ran,
    by the contract's frozen patterns. First, any log line or annotation
    that shows the candidate's own package failing to resolve or load.
    Second, a provider failure named by the job's TERMINAL error, the last
    ##[error] line of the log, or by a failure-level annotation. Retry
    warnings before the terminal error are never provider evidence: the
    runner retries a 503 and then reports whatever ended the job."""
    lines = [line for _, line in log.lines] if log is not None else []
    texts = lines + [annotation["text"] for annotation in annotations]

    def first(candidates, kind):
        matching = (
            text for text in candidates if any(re.search(pattern, text) for pattern in patterns[kind])
        )
        return next((text[:240] for text in matching), None)

    owned = first(texts, "candidate")
    terminal = [line for line in lines if line.startswith("##[error]")][-1:]
    failures = [annotation["text"] for annotation in annotations if annotation["level"] == "failure"]
    return owned, first(terminal, "terminal_error") or first(failures, "annotation")


def validate_job(
    job: dict, log_text: str | None, spec: dict, context: dict, annotations: list | None = None
) -> JobResult:
    """Everything one attempt-1 job must show to count as a sample.
    annotations are the job's check-run annotations, each {level, text}."""
    contract, manifest = context["contract"], context["manifest"]
    workload, run_id = context["workload"], context["run_id"]
    result = JobResult(
        name=job.get("name"), job_id=job.get("id"), arch=spec["arch"], order=spec["order"],
        position=spec["position"], variant=spec["variant"], conclusion=job.get("conclusion"),
        started_at=job.get("started_at"), completed_at=job.get("completed_at"), url=job.get("html_url"),
    )
    fixture = contract["fixtures"]["workloads"][workload]
    variant = contract["variants"][spec["variant"]]
    if job.get("run_id") != run_id or job.get("run_attempt") != 1:
        result.flag(PROTOCOL, "the job is not of this run's attempt 1")
    if job.get("conclusion") == "cancelled":
        result.flag(CANCELLED, "the job was cancelled")
        return result
    if job.get("conclusion") == "skipped" or job.get("status") != "completed":
        result.flag(PROTOCOL, f"an active job did not complete: {job.get('status')}/{job.get('conclusion')}")
        return result
    if contract["architectures"][spec["arch"]] not in (job.get("labels") or []):
        runner = contract["architectures"][spec["arch"]]
        result.flag(PROTOCOL, f"the job ran on {job.get('labels')}, not {runner}")

    started, completed = parse_time(job.get("started_at")), parse_time(job.get("completed_at"))
    if started is None or completed is None or completed < started:
        result.flag(PROTOCOL, "the Jobs API has no usable started_at and completed_at")
    else:
        result.seconds = completed - started
    for label, name in (("metadata", contract["controller"]["metadata_step"]), ("action", variant["step"])):
        step = _step(job, name)
        step_start = parse_time(step.get("started_at")) if step else None
        step_end = parse_time(step.get("completed_at")) if step else None
        result.steps[label] = {
            "conclusion": step.get("conclusion") if step else None,
            "seconds": (
                step_end - step_start if step_start is not None and step_end is not None else UNAVAILABLE
            ),
        }

    log = parse_log(log_text) if log_text is not None else None
    result.spans = dict(log.spans) if log else {key: UNAVAILABLE for key in SPANS}
    expected_conclusion = fixture["expected_conclusion"]
    runtime, arch = contract["runtime"], spec["arch"]
    # The image a job ran on, whenever its log shows it, and before anything
    # decides whether the job can be replaced: a replacement cannot hide
    # that the image moved. A log that is not there shows no image, and
    # that is not drift.
    images = (
        ("image", log.image if log else None, "runner image"),
        ("image_version", log.image_version if log else None, "runner image version"),
    )
    for key, observed, what in images:
        if observed and observed != runtime[key][arch]:
            result.flag(DRIFT, f"the {what} {observed!r} is not the contract's {runtime[key][arch]!r}")
    # A failure before the variant ran is judged by what the records show,
    # also for W4, whose designed failure comes from the variant. Only a
    # positively attested provider failure can be replaced; the candidate's
    # own package failing to resolve or load is its defect; anything else is
    # unexplained and goes to review.
    if job.get("conclusion") == "failure":
        if not _variant_started(job, variant["step"], log):
            owned, provider = _evidence(log, annotations or [], contract["infrastructure_evidence"])
            if owned:
                result.flag(DEFECT, f"the candidate's own package failed before it ran: {owned}")
            elif provider:
                result.flag(
                    INFRASTRUCTURE, f"the provider failed the job before the candidate started: {provider}"
                )
            else:
                result.flag(
                    AMBIGUOUS,
                    "the job failed before the variant started, and no record shows a provider failure",
                )
            return result
        if log is None or not log.action_started:
            result.flag(AMBIGUOUS, "the Jobs API shows the variant step started, but its log does not")
            return result
    if log is None:
        result.flag(AMBIGUOUS, "the job log is unavailable, so its identity cannot be checked")
        return result
    result.runtime = {
        "runner_version": log.runner_version, "image": log.image, "image_version": log.image_version,
        "python": log.python, "git": log.git, "identity": log.identity, "node": log.node,
        "jq": log.metadata.get("jq"), "sha256sum": log.metadata.get("sha256sum"),
    }

    meta = log.metadata
    for problem in log.metadata_problems:
        result.flag(PROTOCOL, problem)
    missing = [key for key in METADATA_KEYS if key not in meta]
    if missing:
        step = result.steps["metadata"]["conclusion"]
        judgement = AMBIGUOUS if step == "failure" else PROTOCOL
        result.flag(judgement, f"the trusted metadata step printed no {', '.join(missing)} (step {step})")
    else:
        expected = {
            "digest": fixture["expected_event_digest"],
            "event": contract["controller"]["event"],
            "action": contract["controller"]["action"],
            "label": contract["orders"][spec["order"]]["label"],
            "number": str(fixture["pr_number"]),
            "base_ref": contract["fixtures"]["base"]["branch"],
            "base_sha": contract["fixtures"]["base"]["sha"],
            "head_sha": fixture["head_sha"],
            "repository": contract["repository"],
            "server_url": contract["server_url"],
            "require_model_attribution": contract["inputs"]["require-model-attribution"],
            "hidden_unicode": contract["inputs"]["hidden-unicode"],
            "workflow_sha": contract["controller"]["workflow_sha"],
            "workflow_ref": contract["controller"]["workflow_ref"],
            "run_id": str(run_id),
            "run_attempt": "1",
        }
        for key, value in expected.items():
            if meta[key] != value:
                result.flag(PROTOCOL, f"the event's {key} is {meta[key]!r}, the contract expects {value!r}")

    own = f"stickerdaniel/agent-guardrails@{variant['sha']}"
    if log.downloads != [(own, variant["sha"])]:
        result.flag(PROTOCOL, f"the job downloaded {log.downloads}, not exactly {own}")
    if log.action_started and log.action_sha != variant["sha"]:
        result.flag(PROTOCOL, f"the job ran {log.action_sha}, not {variant['sha']}")

    # A job that got this far without showing its image cannot show it ran
    # on the frozen one.
    for _, observed, what in images:
        if not observed:
            result.flag(PROTOCOL, f"the log shows no {what}")
    if log.action_started:
        if log.git != runtime["git"][arch]:
            result.flag(DRIFT, f"{log.git!r} is not the contract's {runtime['git'][arch]!r}")
        if spec["variant"] == "PY" and log.python != runtime["python"][arch]:
            result.flag(DRIFT, f"{log.python!r} is not the contract's {runtime['python'][arch]!r}")
        if spec["variant"] in ("RS", "TS-H"):
            identity = _NODE.sub("node <node>;", log.identity) if log.identity else None
            if identity != runtime["identity"][spec["variant"]][arch]:
                result.flag(DRIFT, f"the identity line {log.identity!r} is not the contract's")
        # The identity comparison leaves the Node version out; it is frozen
        # on its own, since the runner, not the package, supplies it.
        if spec["variant"] == "TS-H":
            if not log.node:
                result.flag(PROTOCOL, "the TS-H identity line names no Node version")
            elif log.node != runtime["ts_h_node"][arch]:
                frozen = runtime["ts_h_node"][arch]
                result.flag(DRIFT, f"TS-H ran Node {log.node}, not the contract's {frozen}")

    conclusion = job.get("conclusion")
    if not log.action_started:
        result.flag(AMBIGUOUS, f"the job concluded {conclusion} before the variant started")
    elif conclusion != expected_conclusion:
        if expected_conclusion == "failure" and conclusion == "success":
            result.flag(DEFECT, f"{workload} must fail with exit 1, but the job succeeded")
        else:
            result.flag(DEFECT, f"the job concluded {conclusion}, the workload expects {expected_conclusion}")
    elif result.steps["action"]["conclusion"] != expected_conclusion:
        result.flag(DEFECT, f"the variant step concluded {result.steps['action']['conclusion']}")
    elif log.report != manifest["workloads"][workload]["expected"]["log_lines"]:
        result.flag(DEFECT, "the variant's report lines differ from the frozen expected output")
    return result


@dataclass
class Dispatch:
    entry: dict
    judgement: str = VALID
    problems: list = field(default_factory=list)
    jobs: list = field(default_factory=list)
    reruns: list = field(default_factory=list)
    timing: dict | None = None
    created_at: str | None = None

    def flag(self, judgement: str, problem: str) -> None:
        self.problems.append(f"{judgement}: {problem}")
        if _RANK[judgement] > _RANK[self.judgement]:
            self.judgement = judgement


def _annotations(records: Records, folder: str, job_id) -> tuple[list, str | None]:
    """A job's check-run annotations as {level, text}, flattened from the
    pages collect.py kept, and why they cannot be used, if they cannot. A
    listing shorter than the check run's own count is not evidence: a
    truncated page can drop exactly the annotation that matters. Nor is a
    listing that was never kept: collect.py keeps both records for every
    job that ran, an empty page and a count of 0 included, so their absence
    means the annotations are unknown, not that there are none."""
    kept = records.json(f"{folder}/annotations/{job_id}.json")
    check_run = records.json(f"{folder}/check-runs/{job_id}.json")
    items = [item for page in kept or [] for item in (page if isinstance(page, list) else [page])]
    count = ((check_run or {}).get("output") or {}).get("annotations_count")
    if kept is None or not isinstance(count, int) or len(items) != count:
        return [], f"its annotations are incomplete: {len(items)} kept, the check run counts {count}"
    return [
        {
            "level": item.get("annotation_level"),
            "text": f"{item.get('title') or ''} {item.get('message') or ''}".strip(),
        }
        for item in items
    ], None


def validate_dispatch(entry: dict, records: Records, contract: dict, manifest: dict) -> Dispatch:
    """One labeled event: its run, all 18 jobs of attempt 1, and the six of
    its order."""
    dispatch = Dispatch(entry)
    run_id = entry.get("run_id")
    base = contract["fixtures"]["base"]["sha"]
    for key in ("base_tip_before", "base_tip_after"):
        if entry.get(key) != base:
            dispatch.flag(PROTOCOL, f"{key} is {entry.get(key)!r}, not the frozen base {base}")
    folder = f"runs/{run_id}"
    run = records.json(f"{folder}/run.json")
    jobs = records.json(f"{folder}/jobs.json")
    dispatch.timing = records.json(f"{folder}/timing.json")
    if run is None or jobs is None:
        dispatch.flag(AMBIGUOUS, f"the records of run {run_id} are missing")
        return dispatch
    dispatch.created_at = run.get("created_at")
    if run.get("id") != run_id or run.get("event") != contract["controller"]["event"]:
        dispatch.flag(PROTOCOL, "the run is not this dispatch's pull_request_target run")
    if run.get("path") != contract["controller"]["workflow_path"]:
        dispatch.flag(PROTOCOL, f"the run came from {run.get('path')!r}")
    if run.get("conclusion") == "cancelled":
        dispatch.flag(CANCELLED, "the run was cancelled")
    listed = jobs.get("jobs") or []
    if jobs.get("total_count") != len(listed):
        dispatch.flag(AMBIGUOUS, "the jobs listing is incomplete")
    first = [job for job in listed if job.get("run_attempt") == 1]
    dispatch.reruns = [
        {
            "name": job.get("name"), "id": job.get("id"),
            "attempt": job.get("run_attempt"), "conclusion": job.get("conclusion"),
        }
        for job in listed if job.get("run_attempt") != 1
    ]
    specs = contract["jobs"]
    by_name: dict[str, list] = {}
    for job in first:
        by_name.setdefault(job.get("name"), []).append(job)
    for name in sorted(set(by_name) - set(specs)):
        dispatch.flag(PROTOCOL, f"attempt 1 has an unknown job {name!r}")
    workload, order = entry.get("workload"), entry.get("order")
    context = {"contract": contract, "manifest": manifest, "workload": workload, "run_id": run_id}
    for name, spec in specs.items():
        found = by_name.get(name, [])
        if len(found) != 1:
            dispatch.flag(PROTOCOL, f"attempt 1 has {len(found)} jobs named {name!r}")
            continue
        (job,) = found
        if spec["order"] != order:
            if job.get("conclusion") != "skipped":
                dispatch.flag(
                    PROTOCOL, f"{name} is not of order {order} but concluded {job.get('conclusion')}"
                )
            continue
        log = records.read(f"{folder}/logs/{job.get('id')}.log")
        annotations, incomplete = _annotations(records, folder, job.get("id"))
        if incomplete:
            dispatch.flag(AMBIGUOUS, f"{name}: {incomplete}")
        text = None if log is None else log.decode("utf-8", "replace")
        result = validate_job(job, text, spec, context, annotations)
        dispatch.jobs.append(result)
        for problem in result.problems:
            judgement = problem.split(":", 1)[0]
            dispatch.flag(judgement, f"{name}: {problem.split(': ', 1)[1]}")
    if len(dispatch.jobs) != 6 and not any(problem.startswith(PROTOCOL) for problem in dispatch.problems):
        dispatch.flag(PROTOCOL, f"{len(dispatch.jobs)} active jobs, not 6")
    return dispatch


# --- The attempt -----------------------------------------------------------------


def _run_window(records: Records, run_id) -> tuple[int | None, int | None] | None:
    """When a run was created and when its last attempt-1 job that ran
    ended, from the retained API records; None without a run record."""
    run = records.json(f"runs/{run_id}/run.json")
    if run is None:
        return None
    jobs = records.json(f"runs/{run_id}/jobs.json") or {}
    ends = [
        parse_time(job.get("completed_at")) for job in jobs.get("jobs") or []
        if job.get("run_attempt") == 1 and job.get("conclusion") != "skipped"
    ]
    ends = [end for end in ends if end is not None]
    return parse_time(run.get("created_at")), max(ends) if ends else None


def account(ledger: dict, records: Records, contract: dict, manifest: dict) -> dict:
    """Validate every dispatch, apply the failure rules, and fill the twelve
    round slots of each workload."""
    frozen = schedule()
    reasons = {"close": [], "pause": []}
    rows = []
    measured_seen = False
    plain = 0
    slots: dict[tuple[str, int], Dispatch] = {}
    originals: dict[int, dict] = {}
    replaced: set[int] = set()
    replacements = 0
    used: dict = {}
    previous = None
    for position, entry in enumerate(ledger["dispatches"], 1):
        row = {"entry": entry, "slot": None, "dispatch": None, "judgement": None, "problems": []}
        rows.append(row)

        def close(problem: str) -> None:
            row["problems"].append(problem)
            reasons["close"].append(f"dispatch {position}: {problem}")

        if entry.get("seq") != position:
            close(f"the ledger entry has seq {entry.get('seq')}")
        # Every dispatch, shakedown, measured or replacement, is its own
        # labeled event, and the ledger lists them in the order they ran:
        # one round at a time.
        run_id = entry.get("run_id")
        if run_id in used:
            close(f"run {run_id} was already used by dispatch {used[run_id]}")
        else:
            used[run_id] = position
        window = _run_window(records, run_id)
        if window is not None:
            created, ended = window
            if created is None:
                close(f"run {run_id} has no creation time")
            elif previous is not None:
                before, (earlier_created, earlier_ended) = previous
                if earlier_created is not None and created <= earlier_created:
                    close(
                        f"run {run_id} was created before dispatch {before}'s run, or with it; "
                        "the ledger order is not the order of the runs"
                    )
                elif earlier_ended is not None and created < earlier_ended:
                    close(f"run {run_id} was created before the jobs of dispatch {before} ended")
            previous = (position, window)
        phase = entry.get("phase")
        if phase == "shakedown":
            row["judgement"] = "excluded (shakedown)"
            if measured_seen:
                close("a shakedown after measuring began")
            continue
        if phase != "measured":
            close(f"unknown phase {phase!r}")
            continue
        measured_seen = True
        dispatch = validate_dispatch(entry, records, contract, manifest)
        row["dispatch"] = dispatch
        row["judgement"] = dispatch.judgement
        target = entry.get("replaces")
        if target is None:
            if plain >= len(frozen):
                close("more measured dispatches than the schedule has")
                continue
            slot = frozen[plain]
            plain += 1
            if (entry.get("workload"), entry.get("order")) != (slot["workload"], slot["order"]):
                close(
                    f"{entry.get('workload')} order {entry.get('order')}, "
                    f"the schedule has {slot['workload']} order {slot['order']}"
                )
                continue
            key = (slot["workload"], slot["round"])
        else:
            original = originals.get(target)
            if original is None or original["dispatch"].judgement != INFRASTRUCTURE or target in replaced:
                close(f"it replaces {target}, which is no unreplaced infrastructure round")
                continue
            first = original["entry"]
            if (entry.get("workload"), entry.get("order")) != (first["workload"], first["order"]):
                close(f"a replacement must repeat the workload and order of {target}")
                continue
            replacements += 1
            replaced.add(target)
            if replacements > MAX_REPLACEMENTS:
                close(f"more than {MAX_REPLACEMENTS} replacements")
                continue
            key = original["key"]
        row["slot"] = list(key)
        originals[position] = {"entry": entry, "dispatch": dispatch, "key": key}
        if dispatch.judgement in CLOSING:
            problems = "; ".join(dispatch.problems)
            reasons["close"].append(f"dispatch {position} ({dispatch.judgement}): {problems}")
        elif dispatch.judgement == AMBIGUOUS:
            reasons["pause"].append(f"dispatch {position}: {'; '.join(dispatch.problems)}")
        elif dispatch.judgement == VALID:
            slots[key] = dispatch
    failed = [seq for seq, item in originals.items() if item["dispatch"].judgement == INFRASTRUCTURE]
    infrastructure = [seq for seq in failed if seq not in replaced]
    if len(failed) > MAX_REPLACEMENTS:
        reasons["close"].append(
            f"{len(failed)} infrastructure failures, more than the {MAX_REPLACEMENTS} replacements allowed"
        )
    if reasons["close"]:
        status = "closed"
    elif reasons["pause"]:
        status = "paused"
    elif len(slots) == len(frozen) and not infrastructure:
        status = "complete"
    else:
        status = "in_progress"
    return {
        "status": status,
        "reasons": reasons,
        "dispatches": rows,
        "slots": slots,
        "replacements": replacements,
        "awaiting_replacement": infrastructure,
    }


# --- Statistics -----------------------------------------------------------------


def _mean(values) -> float:
    return sum(values) / len(values)


def _median(values) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _interval(seconds: int) -> tuple[int, int]:
    """Where a job's true elapsed time lies, given second-resolution
    timestamps: within a second either way, and never below zero."""
    return max(0, seconds - JOB_PRECISION), seconds + JOB_PRECISION


def billed_minutes(seconds: int) -> dict:
    """The private-hosted minute model of one job: CEIL(duration / 60) over
    the job's precision interval. A job that ran has a positive duration,
    so it bills at least one minute. AMBIGUOUS when the interval crosses a
    60-second boundary."""
    low, high = _interval(seconds)
    least = max(1, math.ceil(low / 60))
    most = max(1, math.ceil(high / 60))
    return {"minutes": least if least == most else "AMBIGUOUS", "range": [least, most]}


def _describe(values: list, indices) -> dict:
    ordered = sorted(values)
    low, high = MEDIAN_ORDER
    result = {
        "n": len(values),
        "values": values,
        "mean": _mean(values),
        "median": _median(values),
        "min": ordered[0],
        "max": ordered[-1],
    }
    if len(values) == ROUNDS:
        result["median_interval_3rd_10th"] = [ordered[low - 1], ordered[high - 1]]
        result["bootstrap_95"] = bootstrap_interval(values, indices)
        result["bootstrap_excludes_zero"] = result["bootstrap_95"][0] > 0 or result["bootstrap_95"][1] < 0
    return result


def statistics(slots: dict, workload: str, arch: str, indices) -> dict:
    """Per-variant distributions and the paired contrasts of one workload
    and architecture, rounds in slot order."""
    rounds = [slots[(workload, number)] for number in range(1, ROUNDS + 1)]
    by_variant = {
        variant: [
            next(job for job in dispatch.jobs if job.arch == arch and job.variant == variant)
            for dispatch in rounds
        ]
        for variant in VARIANTS
    }
    variants = {}
    for variant, jobs in by_variant.items():
        seconds = [job.seconds for job in jobs]
        positions = {}
        for position in (1, 2, 3):
            chosen = [job.seconds for job in jobs if job.position == position]
            positions[str(position)] = {"n": len(chosen), "mean": _mean(chosen) if chosen else UNAVAILABLE}
        variants[variant] = {
            "api_seconds": {
                key: value for key, value in _describe(seconds, indices).items()
                if not key.startswith("bootstrap")
            },
            "by_position": positions,
            "billing": [billed_minutes(value) for value in seconds],
            "spans": {name: [job.spans.get(name, UNAVAILABLE) for job in jobs] for name in SPANS},
            "action_step_api_seconds": [job.steps["action"]["seconds"] for job in jobs],
        }
    contrasts = {}
    py = by_variant["PY"]
    py_mean = _mean([job.seconds for job in py])
    for challenger in CHALLENGERS:
        other = by_variant[challenger]
        differences = [job.seconds - base.seconds for job, base in zip(other, py)]
        api = _describe(differences, indices)
        lows = [_interval(job.seconds)[0] - _interval(base.seconds)[1] for job, base in zip(other, py)]
        highs = [_interval(job.seconds)[1] - _interval(base.seconds)[0] for job, base in zip(other, py)]
        api["precision_interval_of_mean"] = [_mean(lows), _mean(highs)]
        api["resolved_at_api_precision"] = _mean(lows) > 0 or _mean(highs) < 0
        api["percent_of_py_mean"] = 100 * api["mean"] / py_mean if py_mean else UNAVAILABLE
        api["per_1000_jobs_seconds"] = 1000 * api["mean"]
        spans = {}
        for name in ("T_span", "T_action"):
            pairs = [
                (job.spans.get(name), base.spans.get(name)) for job, base in zip(other, py)
            ]
            if any(UNAVAILABLE in pair or None in pair for pair in pairs):
                available = [a - b for a, b in pairs if UNAVAILABLE not in (a, b) and None not in (a, b)]
                spans[name] = {
                    "n": len(available),
                    "mean": _mean(available) if available else UNAVAILABLE,
                    "note": "some rounds lack a log boundary; no interval is computed",
                }
            else:
                spans[name] = _describe([a - b for a, b in pairs], indices)
        if (
            not api["resolved_at_api_precision"]
            and isinstance(spans["T_span"].get("bootstrap_95"), list)
            and spans["T_span"]["bootstrap_excludes_zero"]
        ):
            api["note"] = "below API resolution: visible only in log spans"
        billing = []
        for job, base in zip(other, py):
            mine, theirs = billed_minutes(job.seconds), billed_minutes(base.seconds)
            ambiguous = "AMBIGUOUS" in (mine["minutes"], theirs["minutes"])
            billing.append({
                "difference": "AMBIGUOUS" if ambiguous else mine["minutes"] - theirs["minutes"],
                "range": [mine["range"][0] - theirs["range"][1], mine["range"][1] - theirs["range"][0]],
            })
        total_low = sum(item["range"][0] for item in billing)
        total_high = sum(item["range"][1] for item in billing)
        contrasts[f"{challenger} - PY"] = {
            "api_seconds": api,
            "log_spans_seconds": spans,
            "private_billing_model": {
                "label": "modelled from public runs, not charged",
                "per_round": billing,
                "total_over_12_rounds": "AMBIGUOUS" if total_low != total_high else total_low,
                "total_range": [total_low, total_high],
            },
            "public_hosted_billed_difference": 0,
        }
    return {"variants": variants, "contrasts": contrasts}


def analyze(contract_path: Path, manifest_path: Path, ledger_path: Path, records_path: Path) -> dict:
    contract, manifest, contract_sha, manifest_sha = load_contract(contract_path, manifest_path)
    ledger_bytes = ledger_path.read_bytes()
    records = Records(records_path)
    accounting = account(json.loads(ledger_bytes), records, contract, manifest)
    result = {
        "study": "E2E-3",
        "inputs": {
            "contract_sha256": contract_sha,
            "manifest_sha256": manifest_sha,
            "ledger_sha256": _sha256_bytes(ledger_bytes),
            "records_index_sha256": records.index_sha256,
        },
        "status": accounting["status"],
        "reasons": accounting["reasons"],
        "replacements": {
            "used": accounting["replacements"],
            "limit": MAX_REPLACEMENTS,
            "awaiting": accounting["awaiting_replacement"],
        },
        "ledger": [_ledger_row(item) for item in accounting["dispatches"]],
        "results": None,
        "notes": NOTES,
    }
    if accounting["status"] == "complete":
        results = {}
        for workload in CYCLE:
            indices = bootstrap_indices(workload)
            results[workload] = {
                arch: statistics(accounting["slots"], workload, arch, indices) for arch in ARCHES
            }
        result["results"] = results
    return result


def _ledger_row(item: dict) -> dict:
    dispatch = item["dispatch"]
    if dispatch is None:
        return {
            "entry": item["entry"], "slot": None, "judgement": item["judgement"],
            "problems": item["problems"], "jobs": [],
        }
    return {
        "entry": dispatch.entry,
        "slot": item["slot"],
        "judgement": dispatch.judgement,
        "problems": item["problems"] + dispatch.problems,
        "created_at": dispatch.created_at,
        "timing_endpoint": dispatch.timing,
        "reruns_excluded": dispatch.reruns,
        "jobs": [
            {
                "name": job.name, "id": job.job_id, "url": job.url, "arch": job.arch, "order": job.order,
                "position": job.position, "variant": job.variant, "judgement": job.judgement,
                "problems": job.problems, "conclusion": job.conclusion, "started_at": job.started_at,
                "completed_at": job.completed_at, "api_seconds": job.seconds, "steps": job.steps,
                "spans": job.spans, "runtime": job.runtime,
            }
            for job in dispatch.jobs
        ],
    }


NOTES = [
    "Primary measure: Jobs API completed_at - started_at of attempt 1, in whole seconds. The timing "
    "endpoint's run_duration_ms and billable duration_ms are kept as billing evidence, never as job time.",
    "Per job the true elapsed time lies within one second of the observed; a paired difference within two. "
    "Timestamp precision and sampling intervals are reported separately.",
    "The bootstrap interval resamples whole rounds (six jobs, both architectures) and is "
    "descriptive under its assumptions, not a guarantee of long-term savings.",
    "The [3rd, 10th] order-statistic interval covers the median with 96.142578125% probability only for 12 "
    "independent continuous observations; one-second-quantized, possibly drifting records do not inherit it.",
    "The sixteen contrasts (4 workloads x 2 architectures x 2 challengers) are pointwise, not familywise.",
    "Workloads are never pooled and no overall winner is named. W1 is medium controlled and "
    "W2 heavy controlled; neither estimates a real workload mix.",
    "Public standard hosted runners bill nothing, so the billed difference there is zero. The private-hosted "
    "minute model is modelled from public runs, not charged. Larger runners and Ubicloud were not measured.",
    "TS-H is a Node 24 action with a native Git supervisor, not a pure TypeScript action.",
    "Log spans are diagnostics, never billable duration. A missing log boundary is reported as unavailable.",
    "A round is replaced only on a retained provider record, before the variant started, with no sign of "
    "the candidate's own package failing; an unexplained failure pauses for review. Each dispatch is a "
    "distinct run, in run order, one round at a time. The runner image, its version, git, Python and the "
    "TS-H Node version are frozen per architecture; any change closes the attempt.",
]


# --- Report -------------------------------------------------------------------


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(item) for item in value) + "]"
    return str(value)


_VARIANT_HEADER = ["Variant", "Mean s", "Median s", "Min", "Max"]
_CONTRAST_HEADER = [
    "Contrast", "Mean s", "Bootstrap 95%", "Median [3rd, 10th]", "API precision of mean",
    "% of PY", "Private minutes over 12 rounds",
]


def _row(cells) -> str:
    return "| " + " | ".join(_fmt(cell) for cell in cells) + " |"


def _rule(cells) -> str:
    return "|" + " --- |" * len(cells)


def markdown(result: dict) -> str:
    lines = [
        "# E2E-3 CI cost study: analysis",
        "",
        f"Status: **{result['status']}**.",
        "",
        "| Input | SHA-256 |",
        "| --- | --- |",
        *(f"| {name} | `{value}` |" for name, value in result["inputs"].items()),
        "",
    ]
    for kind in ("close", "pause"):
        if result["reasons"][kind]:
            lines += [f"## Reasons to {kind}", "", *(f"- {reason}" for reason in result["reasons"][kind]), ""]
    replacements = result["replacements"]
    lines += [
        f"Replacements used: {replacements['used']} of {replacements['limit']}.",
        "",
        "## Ledger",
        "",
        "| Seq | Phase | Workload | Order | Run | Judgement |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in result["ledger"]:
        entry = row["entry"]
        lines.append(
            f"| {entry.get('seq')} | {entry.get('phase')} | {entry.get('workload')} | {entry.get('order')} | "
            f"{entry.get('run_id')} | {row['judgement']} |"
        )
    lines.append("")
    if result["results"] is None:
        lines += ["No statistics: the attempt is not complete.", ""]
    else:
        for workload, arches in result["results"].items():
            for arch, data in arches.items():
                lines += [f"## {workload} on {arch}", "", _row(_VARIANT_HEADER), _rule(_VARIANT_HEADER)]
                for variant, info in data["variants"].items():
                    api = info["api_seconds"]
                    lines.append(_row([variant, api["mean"], api["median"], api["min"], api["max"]]))
                lines += ["", _row(_CONTRAST_HEADER), _rule(_CONTRAST_HEADER)]
                for name, contrast in data["contrasts"].items():
                    api = contrast["api_seconds"]
                    lines.append(_row([
                        name, api["mean"], api["bootstrap_95"],
                        f"{_fmt(api['median'])} {_fmt(api['median_interval_3rd_10th'])}",
                        api["precision_interval_of_mean"], api["percent_of_py_mean"],
                        contrast["private_billing_model"]["total_over_12_rounds"],
                    ]))
                lines.append("")
    lines += ["## Notes", "", *(f"- {note}" for note in result["notes"]), ""]
    return "\n".join(lines)


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schedule", help="print the frozen dispatch order")
    digest = commands.add_parser("digest", help="print each fixture's expected event digest")
    digest.add_argument("--contract", type=Path, required=True)
    digest.add_argument("--manifest", type=Path, required=True)
    run = commands.add_parser("analyze", help="analyze collected records")
    for name in ("contract", "manifest", "ledger", "records", "json", "markdown"):
        run.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "schedule":
        print(json.dumps(schedule(), indent=2))
        return 0
    if args.command == "digest":
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        digests = {}
        for key, fixture in contract["fixtures"]["workloads"].items():
            if fixture["pr_number"] is None or fixture["author"] is None:
                digests[key] = None
                continue
            workload = manifest["workloads"][key]
            digests[key] = event_digest(
                fixture["pr_number"], fixture["head_sha"], contract["fixtures"]["base"]["sha"],
                contract["fixtures"]["base"]["branch"], workload["title"]["text"], workload["body"]["text"],
                fixture["author"],
            )
        print(json.dumps(digests, indent=2, sort_keys=True))
        return 0
    try:
        result = analyze(args.contract, args.manifest, args.ledger, args.records)
    except ContractError as error:
        print(f"analyze: {error}", file=sys.stderr)
        return 1
    text = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    args.json.write_text(text, encoding="utf-8")
    args.markdown.write_text(markdown(result), encoding="utf-8")
    print(f"status {result['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
