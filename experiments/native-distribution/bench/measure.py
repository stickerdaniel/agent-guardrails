"""Gate X captured-driver measurement, as bench/contract.json declares it.

    python3 bench/measure.py --baseline DIR --corpora DIR --python314 PATH \
        [--python312 PATH] --label TEXT --out FILE [--source-sha SHA] [--emulated]

Linux only: every run is a fresh process started by bench/spawn.py, woken
through a pidfd and reaped with os.wait4, so wall time, CPU and peak RSS
belong to exactly that process and the children it waited for. Peak RSS can
never read lower than the spawner's own, which /bin/true records every round
as rss_floor_kib. Before the clock starts, this verifies the baseline's
hashes, every capture's pinned hash and the committed asset's envelope and
ELF. Writes every raw trial, the run order, the paired differences, the
decision rules' verdicts and the environment to --out, and exits 1 if parity,
cleanup or any run failed. A decision FAIL or INCONCLUSIVE is a result, not
an error, and exits 0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)
import baseline  # noqa: E402
import gen_corpora  # noqa: E402
import verify_artifacts  # noqa: E402

CONTRACT = os.path.join(HERE, "contract.json")
TARGETS = {"x86_64": ("linux-x64", "x64"), "aarch64": ("linux-arm64", "arm64")}
PARITY = ("python-tuned", "rust-packaged", "rust-extracted")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: str) -> str:
    """Hashed in chunks: the harness keeps its own peak small."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Spawner:
    """bench/spawn.py, one per measurement, started before any timing."""

    def __init__(self, python: str, scratch: str) -> None:
        self.scratch = scratch
        self.process = subprocess.Popen(
            [python, "-I", "-S", "-B", os.path.join(HERE, "spawn.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env={"PATH": "/usr/bin:/bin"},
        )

    def run(self, argv: list[str], env: dict, deadline_s: float) -> dict:
        """One fresh process: wall, CPU, peak RSS, exit status and output."""
        stdout_path = os.path.join(self.scratch, "stdout")
        stderr_path = os.path.join(self.scratch, "stderr")
        request = {"argv": argv, "env": env, "stdout": stdout_path, "stderr": stderr_path, "deadline": deadline_s}
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        reply = self.process.stdout.readline()
        if not reply:
            raise SystemExit("measure: the spawner ended")
        result = json.loads(reply)
        with open(stdout_path, "rb") as handle:
            stdout = handle.read()
        with open(stderr_path, "rb") as handle:
            stderr = handle.read()
        result.update({
            "stdout_bytes": len(stdout),
            "stdout_sha256": sha256(stdout),
            "stderr_bytes": len(stderr),
            "stderr_head": stderr[:300].decode("utf-8", "replace") if stderr else "",
            "_stdout": stdout,
        })
        return result

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=30)


def environment_record(args, pythons: dict) -> dict:
    def text(argv: list[str]) -> str:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            return (done.stdout + done.stderr).strip().splitlines()[0] if (done.stdout + done.stderr).strip() else ""
        except (OSError, subprocess.TimeoutExpired) as error:
            return f"unavailable: {type(error).__name__}"

    cpu = ""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.lower().startswith(("model name", "cpu part", "hardware")):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    record = {
        "label": args.label,
        "emulated": args.emulated,
        "source_sha": args.source_sha,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": cpu,
        "cpus_available": len(os.sched_getaffinity(0)),
        "harness_python": sys.version.split()[0],
        "bash": text([shutil.which("bash") or "bash", "--version"]),
        "gzip": text([shutil.which("gzip") or "gzip", "--version"]),
        "runner_image": {k: os.environ.get(k, "") for k in ("ImageOS", "ImageVersion", "RUNNER_ENVIRONMENT", "RUNNER_ARCH")},
        "file_cache": "warm: every file is read once in the warmup round before any timed round",
    }
    for name, path in pythons.items():
        record[f"{name}_version"] = text([path, "-I", "-c", "import sys, unicodedata; print(sys.version.split()[0], unicodedata.unidata_version)"])
        record[f"{name}_stdlib_bytecode"] = text([
            path, "-I", "-c",
            "import importlib.util, os; print(os.path.exists(importlib.util.cache_from_source(os.__file__)))",
        ])
    return record


def verdicts(summary: dict, trials: list[dict], contract: dict) -> dict:
    decision = contract["decision"]
    out = {}

    def paired(case: str) -> list[float]:
        by_round: dict[int, dict[str, float]] = {}
        for trial in trials:
            if trial["case"] == case and trial["round"] >= 0:
                by_round.setdefault(trial["round"], {})[trial["contestant"]] = trial["wall_ns"] / 1e6
        return [r["rust-packaged"] - r["python-faithful"] for _, r in sorted(by_round.items())]

    for case in decision["small_cases"]:
        base = summary[case]["python-faithful"]["median_ms"]
        cand = summary[case]["rust-packaged"]["median_ms"]
        limit = max(5.0, 0.05 * base)
        diffs = paired(case)
        median_ok = cand - base <= limit
        low_ok, high_ok = min(diffs) <= limit, max(diffs) <= limit
        out[f"small:{case}"] = {
            "rule": decision["small_rule"],
            "median_regression_ms": round(cand - base, 3),
            "limit_ms": round(limit, 3),
            "paired_median_ms": round(statistics.median(diffs), 3),
            "paired_interval_ms": [round(min(diffs), 3), round(max(diffs), 3)],
            "verdict": "PASS" if median_ok and high_ok else "FAIL" if not median_ok and not low_ok else "INCONCLUSIVE",
        }
    for case in decision["large_cases"]:
        base = summary[case]["python-faithful"]["median_ms"]
        cand = summary[case]["rust-packaged"]["median_ms"]
        need = max(100.0, 0.20 * base)
        saving = base - cand
        diffs = paired(case)  # negative: the candidate was faster
        savings = [-d for d in diffs]
        median_ok = saving >= need
        out[f"large:{case}"] = {
            "rule": decision["large_rule"],
            "median_saving_ms": round(saving, 3),
            "median_saving_fraction": round(saving / base, 4) if base else None,
            "needed_ms": round(need, 3),
            "paired_saving_median_ms": round(statistics.median(savings), 3),
            "paired_saving_interval_ms": [round(min(savings), 3), round(max(savings), 3)],
            "verdict": "PASS" if median_ok and min(savings) >= need
            else "FAIL" if not median_ok and max(savings) < need else "INCONCLUSIVE",
        }
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--corpora", required=True)
    parser.add_argument("--python314", required=True)
    parser.add_argument("--python312")
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-sha", default="")
    parser.add_argument("--emulated", action="store_true", help="label the result as emulated hardware")
    parser.add_argument("--rounds", type=int, help="override for a quick functional check; never a Gate X result")
    args = parser.parse_args(argv)

    with open(CONTRACT, "rb") as handle:
        contract_bytes = handle.read()
    contract = json.loads(contract_bytes)
    rounds = contract["rounds"] if args.rounds is None else args.rounds

    # Everything below is checked before the first timed process.
    baseline_root = baseline.verify(args.baseline)
    machine = os.uname().machine
    if machine not in TARGETS:
        raise SystemExit(f"measure: unsupported machine {machine}")
    target, arch = TARGETS[machine]
    asset_path = os.path.join(ROOT, "bin", target, "agent-guardrails.gz")
    with open(asset_path, "rb") as handle:
        asset = handle.read()
    elf = verify_artifacts.verify(asset, arch)
    corpora = {}
    for case in contract["cases"]:
        path = os.path.abspath(os.path.join(args.corpora, f"{case}.cap"))
        if sha256_path(path) != gen_corpora.EXPECTED[case]:
            raise SystemExit(f"measure: {case}.cap does not hash to its pinned value")
        corpora[case] = path

    owned = tempfile.mkdtemp(prefix="agx-measure-")
    runner_temp = os.path.join(owned, "runner-temp")
    scratch = os.path.join(owned, "scratch")
    home = os.path.join(owned, "home")
    for directory in (runner_temp, scratch, home):
        os.mkdir(directory, 0o700)
    extracted = os.path.join(owned, "agent-guardrails")
    with open(extracted, "wb") as handle:
        handle.write(elf)
    os.chmod(extracted, 0o700)

    bash = shutil.which("bash")
    gzip = shutil.which("gzip")
    if not bash or not gzip:
        raise SystemExit("measure: bash and gzip are required")
    base_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": home}
    driver = os.path.join(HERE, "py_driver.py")
    pythons = {"python314": os.path.abspath(args.python314)}
    if args.python312:
        pythons["python312"] = os.path.abspath(args.python312)

    def command(contestant: str, case: str) -> tuple[list[str], dict]:
        cap = corpora[case]
        if contestant == "python-faithful":
            return [pythons["python314"], "-I", "-B", driver, baseline_root, "faithful", "drive", cap], base_env
        if contestant == "python-tuned":
            return [pythons["python314"], "-I", "-B", driver, baseline_root, "tuned", "drive", cap], base_env
        if contestant == "python312-control":
            return [pythons["python312"], "-I", "-B", driver, baseline_root, "faithful", "drive", cap], base_env
        if contestant == "rust-packaged":
            env = dict(base_env, RUNNER_OS="Linux", RUNNER_TEMP=runner_temp,
                       GITHUB_ACTION_PATH=ROOT, AGX_CAPTURE=cap)
            return [bash, "--noprofile", "--norc", "--", os.path.join(ROOT, "launch.sh")], env
        if contestant == "rust-extracted":
            return [extracted, "drive", cap], base_env
        if contestant == "gunzip-only":
            return [gzip, "-d", "-c", "--", asset_path], base_env
        raise KeyError(contestant)

    contestants = ["python-faithful", "python-tuned", "rust-packaged", "rust-extracted", "gunzip-only"]
    if "python312" in pythons:
        contestants.append("python312-control")
    pairs = sorted((case, contestant) for case in contract["cases"] for contestant in contestants)

    trials: list[dict] = []
    order: dict[int, list[list[str]]] = {}
    reference: dict[str, tuple[int, str]] = {}
    control_parity: dict[str, set] = {}
    floors: dict[int, int] = {}
    problems: list[str] = []
    spawner = Spawner(pythons["python314"], scratch)
    started = time.monotonic()
    for round_index in range(-contract["warmup_rounds"], rounds):
        floors[round_index] = spawner.run(["/bin/true"], base_env, 10)["maxrss_kib"]
        shuffled = list(pairs)
        random.Random(contract["seed"] * 100 + round_index).shuffle(shuffled)
        order[round_index] = [list(p) for p in shuffled]
        for case, contestant in shuffled:
            argv_, env = command(contestant, case)
            trial = spawner.run(argv_, env, contract["deadline_per_run_s"])
            trial.update({"round": round_index, "case": case, "contestant": contestant})
            stdout = trial.pop("_stdout")
            where = f"round {round_index} {case} {contestant}"
            if trial["timed_out"]:
                problems.append(f"{where}: timed out")
            if contestant == "gunzip-only":
                if trial["code"] != 0 or stdout != elf:
                    problems.append(f"{where}: gzip did not reproduce the verified ELF")
            elif trial["code"] not in (0, 1):
                problems.append(f"{where}: exit status {trial['code']}")
            if contestant == "rust-packaged" and os.listdir(runner_temp):
                problems.append(f"{where}: RUNNER_TEMP not empty after the run")
                for leftover in os.listdir(runner_temp):
                    shutil.rmtree(os.path.join(runner_temp, leftover), ignore_errors=True)
            observed = (trial["code"], trial["stdout_sha256"])
            if contestant == "python-faithful":
                if reference.setdefault(case, observed) != observed or trial["stderr_bytes"]:
                    problems.append(f"{where}: the oracle itself is not stable")
            trials.append(trial)
        # Parity is judged after the round, against the oracle's answer.
        for trial in trials:
            if trial["round"] != round_index:
                continue
            observed = (trial["code"], trial["stdout_sha256"])
            if trial["contestant"] in PARITY and (observed != reference[trial["case"]] or trial["stderr_bytes"]):
                problems.append(f"round {round_index} {trial['case']} {trial['contestant']}: output differs from python-faithful")
            if trial["contestant"] == "python312-control":
                control_parity.setdefault(trial["case"], set()).add(observed == reference[trial["case"]])
        print(f"measure: round {round_index} done at {time.monotonic() - started:.1f}s", file=sys.stderr, flush=True)
    spawner.close()
    shutil.rmtree(owned, ignore_errors=True)

    summary: dict[str, dict[str, dict]] = {}
    for case in contract["cases"]:
        summary[case] = {}
        for contestant in contestants:
            rows = [t for t in trials if t["case"] == case and t["contestant"] == contestant and t["round"] >= 0]
            walls = [t["wall_ns"] / 1e6 for t in rows]
            summary[case][contestant] = {
                "n": len(rows),
                "median_ms": round(statistics.median(walls), 3),
                "min_ms": round(min(walls), 3),
                "max_ms": round(max(walls), 3),
                "cpu_median_s": round(statistics.median(t["cpu_s"] for t in rows), 6),
                "maxrss_median_kib": statistics.median(t["maxrss_kib"] for t in rows),
                "stdout_bytes": rows[0]["stdout_bytes"],
                "exit": rows[0]["code"],
            }
    decisions = verdicts(summary, trials, contract)
    parity_ok = not any("differs" in p or "oracle" in p for p in problems)
    gate = (
        "NOT ELIGIBLE (emulated hardware)" if args.emulated
        else "NOT ELIGIBLE (rounds override)" if args.rounds is not None
        else "PASS" if parity_ok and not problems and all(d["verdict"] == "PASS" for d in decisions.values())
        else "FAIL" if problems or any(d["verdict"] == "FAIL" for d in decisions.values())
        else "INCONCLUSIVE"
    )
    result = {
        "contract_sha256": sha256(contract_bytes),
        "rounds": rounds,
        "gate_captured_driver": gate,
        "problems": problems,
        "decisions": decisions,
        "summary": summary,
        "python312_control_parity": {case: sorted(v) for case, v in control_parity.items()},
        "rss_floor_kib": floors,
        "rss_floor_meaning": "maxrss of /bin/true started by the same spawner: no maxrss_kib can read lower, so a value at this floor means at most this much",
        "environment": environment_record(args, pythons),
        "asset": {"path": os.path.relpath(asset_path, ROOT), "gz_bytes": len(asset), "gz_sha256": sha256(asset),
                  "elf_bytes": len(elf), "elf_sha256": sha256(elf)},
        "corpora": {case: gen_corpora.EXPECTED[case] for case in contract["cases"]},
        "baseline_commit": baseline.BASELINE_COMMIT,
        "elapsed_s": round(time.monotonic() - started, 1),
        "order": order,
        "trials": trials,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=1)
        handle.write("\n")
    print(json.dumps({k: result[k] for k in ("gate_captured_driver", "problems", "decisions")}, indent=2))
    for case in contract["cases"]:
        cells = "  ".join(f"{c}={summary[case][c]['median_ms']:.2f}" for c in contestants)
        print(f"{case:18} {cells}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
