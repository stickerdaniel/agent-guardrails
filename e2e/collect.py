"""Save the raw records of E2E-3 runs, read-only, with their SHA-256.

    python3 e2e/collect.py runs --out RECORDS RUN_ID [RUN_ID ...]
    python3 e2e/collect.py tip --out RECORDS --name NAME

runs saves, for each run, the run, its jobs of every attempt, the run timing
endpoint, the run's logs.zip, and the raw log and check-run annotations of
every job that ran:

    RECORDS/runs/<run>/run.json, jobs.json, timing.json, logs.zip
    RECORDS/runs/<run>/logs/<job>.log
    RECORDS/runs/<run>/annotations/<job>.json

tip saves the current e2e/workload-base ref as RECORDS/tips/<NAME>.json, the
evidence behind a ledger's base_tip_before and base_tip_after.

Every file is listed in RECORDS/index.json with its SHA-256 and size. Only
GET requests go out, through gh api. A file already saved is never replaced
with other bytes: the first copy is the record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPOSITORY = "stickerdaniel/agent-guardrails"
BASE_BRANCH = "e2e/workload-base"


class CollectError(Exception):
    pass


def gh_get(path: str) -> bytes:
    """The body of one GET request. Job logs hold the runner's colour
    escapes, which gh prints only when allowed to."""
    proc = subprocess.run(
        [
            "gh", "api", "--method", "GET", "--allow-escape-sequences",
            "-H", "Accept: application/vnd.github+json", path,
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise CollectError(f"GET {path} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.index_path = root / "index.json"
        if self.index_path.exists():
            self.index = json.loads(self.index_path.read_text(encoding="utf-8"))
        else:
            self.index = {"repository": REPOSITORY, "files": {}}

    def save(self, name: str, data: bytes, source: str) -> None:
        entry = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "source": source}
        known = self.index["files"].get(name)
        path = self.root / name
        if known is not None:
            if known["sha256"] != entry["sha256"]:
                raise CollectError(f"{name} was saved before with other bytes; keeping the first record")
            return
        if path.exists():
            raise CollectError(f"{name} exists but is not in index.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entry["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.index["files"][name] = entry
        self.write_index()

    def write_index(self) -> None:
        self.index["files"] = dict(sorted(self.index["files"].items()))
        self.index_path.write_text(json.dumps(self.index, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect_run(store: Store, run_id: int, get=gh_get) -> list[str]:
    """Save one run's records; return the names saved."""
    base = f"repos/{REPOSITORY}/actions/runs/{run_id}"
    folder = f"runs/{run_id}"
    saved = []
    run = get(base)
    if json.loads(run).get("status") != "completed":
        raise CollectError(f"run {run_id} has not completed; collect it once it has")
    jobs_path = f"{base}/jobs?filter=all&per_page=100"
    jobs = get(jobs_path)
    listing = json.loads(jobs)
    if listing.get("total_count") != len(listing.get("jobs") or []):
        raise CollectError(f"run {run_id} lists more jobs than one page holds")
    for name, path, data in (
        ("run.json", base, run),
        ("jobs.json", jobs_path, jobs),
        ("timing.json", f"{base}/timing", get(f"{base}/timing")),
        ("logs.zip", f"{base}/logs", get(f"{base}/logs")),
    ):
        store.save(f"{folder}/{name}", data, path)
        saved.append(f"{folder}/{name}")
    for job in listing["jobs"]:
        # A skipped job never ran and has neither log nor annotations.
        if job.get("conclusion") == "skipped" or job.get("started_at") is None:
            continue
        # The annotations are the provider's own record of a lost runner,
        # which may leave no log behind, so they are kept first.
        path = f"repos/{REPOSITORY}/check-runs/{job['id']}/annotations"
        store.save(f"{folder}/annotations/{job['id']}.json", get(path), path)
        saved.append(f"{folder}/annotations/{job['id']}.json")
        path = f"repos/{REPOSITORY}/actions/jobs/{job['id']}/logs"
        try:
            log = get(path)
        except CollectError:
            # Recorded as missing; the analyzer judges a job without a log.
            print(f"collect: no log for job {job['id']}", file=sys.stderr)
            continue
        store.save(f"{folder}/logs/{job['id']}.log", log, path)
        saved.append(f"{folder}/logs/{job['id']}.log")
    return saved


def collect_tip(store: Store, name: str, get=gh_get) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise CollectError("a tip name is letters, digits, dot, dash and underscore")
    path = f"repos/{REPOSITORY}/git/ref/heads/{BASE_BRANCH}"
    data = get(path)
    store.save(f"tips/{name}.json", data, path)
    return json.loads(data)["object"]["sha"]


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    runs = commands.add_parser("runs", help="save the records of completed runs")
    runs.add_argument("--out", type=Path, required=True)
    runs.add_argument("run_ids", type=int, nargs="+")
    tip = commands.add_parser("tip", help="save the current e2e/workload-base ref")
    tip.add_argument("--out", type=Path, required=True)
    tip.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    store = Store(args.out)
    try:
        if args.command == "runs":
            for run_id in args.run_ids:
                for name in collect_run(store, run_id):
                    print(f"{store.index['files'][name]['sha256']}  {name}")
        else:
            print(collect_tip(store, args.name))
    except CollectError as error:
        print(f"collect: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
