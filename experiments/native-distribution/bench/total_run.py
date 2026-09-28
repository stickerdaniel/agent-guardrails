"""One job of the Gate X total-cost experiment (bench/total-contract.json).

    python -I -B bench/total_run.py --job <id> --capture <file> [--contract <file>]

Runs in a job of .github/workflows/native-distribution-total.yml, after the
runner delivered that job's one first-party snapshot during Set up job. It
prints a begin marker, finds the delivered snapshot, proves every file of it
against the contract's manifest, runs one captured check of the capture with
those delivered bytes, compares exit status, stdout and stderr with the
contract, removes the one directory it created, and prints its end marker
last. bench/total_analyze.py times each job from the runner's "Getting action
download info" line to that end marker.

  candidate  bash --noprofile --norc -- <delivered>/experiments/native-
             distribution/launch.sh, the composite step's own command, with
             the capture, RUNNER_OS and a RUNNER_TEMP this worker owns.
  control    bench/py_driver.py faithful from this checkout, the adapter of
             the accepted captured-driver measurement, first proven equal to
             its bytes in the candidate snapshot, driving the delivered
             agent_guardrails package, which tools/baseline.py also checks.

The delivered snapshot is searched for, never guessed or substituted: below
the common ancestor of RUNNER_TEMP and GITHUB_WORKSPACE, to a fixed depth and
directory count, never following a symlink, never entering the workspace,
which holds this checkout and so a copy of the candidate, or RUNNER_TEMP.
Exactly one directory named by the job's full snapshot SHA must be found,
directly under <owner>/<repo>, and none named by the other treatment's SHA.
Anything else, and any file that differs from the manifest, fails the job;
nothing falls back to the checkout. The check gets an explicit environment
without any token. The capture must hash to the contract's value: both
capture loaders handle the fixed benchmark captures, they do not validate
arbitrary AGCAP1 input.

Linux only (pidfd). Exits 0 only when every check held; the end marker names
every problem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import select
import shutil
import signal
import stat
import sys
import tempfile
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
REPOSITORY = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
import baseline  # noqa: E402
import total_plan  # noqa: E402

MARKER = "agx-total"
PROTOCOL = 1
BASE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
ARCHIVE_CACHE = "ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE"


class Refused(Exception):
    """A check that did not hold; the job is incomplete evidence."""


def emit(kind: str, payload: dict) -> None:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(f"{MARKER} {kind} {text}\n")
    sys.stdout.flush()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def search_root(runner_temp: str, workspace: str, home: str) -> str:
    """The common ancestor of RUNNER_TEMP and the workspace, refused when it
    is the filesystem root or would contain HOME."""
    root = os.path.commonpath([runner_temp, workspace])
    if root == os.sep or root.count(os.sep) < 2:
        raise Refused(f"the search root {root} is too broad")
    if os.path.commonpath([root, home]) == root:
        raise Refused(f"the search root {root} would contain HOME")
    return root


def discover(contract: dict, job: dict, env: dict) -> dict:
    """The one delivered snapshot of this job, or Refused."""
    runner_temp = os.path.realpath(env["RUNNER_TEMP"])
    workspace = os.path.realpath(env["GITHUB_WORKSPACE"])
    home = os.path.realpath(env.get("HOME") or "/")
    root = search_root(runner_temp, workspace, home)
    own = job["snapshot"]
    other = next(t["snapshot"] for name, t in contract["treatments"].items() if name != job["treatment"])
    limits = contract["search"]
    found: list[str] = []
    others: list[str] = []
    links: list[str] = []
    seen = 0
    level = [root]
    for depth in range(1, limits["max_depth"] + 1):
        following = []
        for directory in level:
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError as error:
                raise Refused(f"cannot read {directory}: {error.strerror}") from None
            for entry in entries:
                if entry.is_symlink():
                    if entry.name in (own, other):
                        links.append(entry.path)
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                seen += 1
                if seen > limits["max_directories"]:
                    raise Refused(f"more than {limits['max_directories']} directories below {root}")
                if entry.path in (runner_temp, workspace):
                    continue
                if entry.name == own:
                    found.append(entry.path)
                elif entry.name == other:
                    others.append(entry.path)
                elif depth < limits["max_depth"]:
                    following.append(entry.path)
        level = following
    if links:
        raise Refused(f"a symlink carries a snapshot name: {links}")
    if others:
        raise Refused(f"the other treatment's snapshot was delivered too: {others}")
    if len(found) != 1:
        raise Refused(f"found {len(found)} delivered copies of {own} below {root}, not exactly one: {found}")
    delivered = found[0]
    relative = os.path.relpath(delivered, root)
    if relative.split(os.sep)[-3:] != [*contract["delivered_as"].split("/"), own]:
        raise Refused(f"{relative} is not a delivery of {contract['delivered_as']}")
    watermark = None
    completed = delivered + ".completed"
    if os.path.isfile(completed) and not os.path.islink(completed):
        with open(completed, "rb") as handle:
            watermark = handle.read(200).decode("ascii", "replace").strip()
    cache = env.get(ARCHIVE_CACHE, "")
    cached = bool(cache) and os.path.lexists(os.path.join(cache, contract["delivered_as"].replace("/", "_")))
    if cached:
        raise Refused(f"the runner's action archive cache {cache} holds {contract['delivered_as']}")
    return {
        "path": delivered, "relative": relative, "search_root": root, "directories_seen": seen,
        "watermark": watermark, "archive_cache": cache, "archive_cache_entry": cached,
    }


def verify_tree(root: str, manifest: dict) -> dict:
    """Every regular file below root, hashed; refused unless the set of paths
    and every hash equal the manifest and nothing else is there."""
    actual = {}
    total = 0
    strange = []
    for directory, subdirectories, files in os.walk(root):
        for name in subdirectories:
            if os.path.islink(os.path.join(directory, name)):
                strange.append(os.path.join(directory, name))
        for name in files:
            path = os.path.join(directory, name)
            mode = os.lstat(path)
            if not stat.S_ISREG(mode.st_mode):
                strange.append(path)
                continue
            actual[os.path.relpath(path, root).replace(os.sep, "/")] = sha256_file(path)
            total += mode.st_size
    missing = sorted(set(manifest) - set(actual))
    extra = sorted(set(actual) - set(manifest))
    different = sorted(p for p in manifest if p in actual and actual[p] != manifest[p])
    if strange or missing or extra or different:
        raise Refused(
            f"the delivered tree differs from the manifest: not regular {strange[:5]}, missing {missing[:5]}, "
            f"extra {extra[:5]}, different {different[:5]}"
        )
    return {"files": len(actual), "bytes": total, "manifest_sha256": total_plan.manifest_sha256(actual)}


def run_check(argv: list[str], env: dict, owned: str, deadline_s: float) -> dict:
    """argv in a new session with stdout and stderr in owned; woken by its
    pidfd, killed as a group at the deadline before it is reaped."""
    stdout_path = os.path.join(owned, "stdout")
    stderr_path = os.path.join(owned, "stderr")
    create = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    actions = [
        (os.POSIX_SPAWN_OPEN, 0, "/dev/null", os.O_RDONLY, 0),
        (os.POSIX_SPAWN_OPEN, 1, stdout_path, create, 0o600),
        (os.POSIX_SPAWN_OPEN, 2, stderr_path, create, 0o600),
    ]
    started = time.perf_counter_ns()
    pid = os.posix_spawn(argv[0], argv, env, file_actions=actions, setsid=True)
    pidfd = os.pidfd_open(pid)
    try:
        ready, _, _ = select.select([pidfd], [], [], deadline_s)
        if not ready:
            os.killpg(pid, signal.SIGKILL)  # not yet reaped: the group is still ours
        _, status, usage = os.wait4(pid, 0)
    finally:
        os.close(pidfd)
    elapsed = time.perf_counter_ns() - started
    with open(stdout_path, "rb") as handle:
        stdout = handle.read()
    with open(stderr_path, "rb") as handle:
        stderr = handle.read()
    return {
        "exit": os.waitstatus_to_exitcode(status),
        "timed_out": not ready,
        "stdout_bytes": len(stdout),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_bytes": len(stderr),
        "stderr_head": stderr[:300].decode("utf-8", "replace"),
        "cpu_s": round(usage.ru_utime + usage.ru_stime, 6),
        "maxrss_kib": usage.ru_maxrss,
        "ns": elapsed,
    }


def command(contract: dict, job: dict, delivered: str, capture: str, owned: str) -> tuple[list[str], dict]:
    home = os.path.join(owned, "home")
    runner_temp = os.path.join(owned, "runner-temp")
    if job["treatment"] == "candidate":
        action = os.path.join(delivered, contract["treatments"]["candidate"]["action_dir"])
        bash = shutil.which("bash", path=BASE_ENV["PATH"])
        if bash is None:
            raise Refused("bash is not on /usr/bin:/bin")
        env = dict(BASE_ENV, HOME=home, RUNNER_OS="Linux", RUNNER_TEMP=runner_temp,
                   GITHUB_ACTION_PATH=action, AGX_CAPTURE=capture)
        return [bash, "--noprofile", "--norc", "--", os.path.join(action, "launch.sh")], env
    driver = os.path.join(HERE, "py_driver.py")
    env = dict(BASE_ENV, HOME=home, RUNNER_TEMP=runner_temp)
    return [sys.executable, "-I", "-B", driver, delivered, "faithful", "drive", capture], env


def work(contract: dict, job: dict, capture: str, env: dict, ns: dict, end: dict) -> None:
    """Every stage of the job; raises Refused at the first check that fails.
    Fills end as it goes, so the end marker shows how far it got."""
    if env.get("RUNNER_OS") != "Linux" or os.uname().machine != job["machine"]:
        raise Refused(f"this is {env.get('RUNNER_OS')} {os.uname().machine}, not Linux {job['machine']}")
    if env.get("GITHUB_JOB") != job["id"]:
        raise Refused(f"GITHUB_JOB is {env.get('GITHUB_JOB')!r}, not {job['id']!r}")
    version = sys.version.split()[0]
    end["python"] = {"executable": sys.executable, "version": version, "unidata": unicodedata.unidata_version}
    wanted = contract["python"]
    if not version.startswith(wanted["version"] + ".") or unicodedata.unidata_version != wanted["unidata"]:
        raise Refused(f"Python {version} with Unicode {unicodedata.unidata_version} is not the declared one")
    if sha256_file(capture) != contract["capture_sha256"]:
        raise Refused(f"the capture does not hash to the {contract['case']} value")

    mark = time.perf_counter_ns()
    delivered = discover(contract, job, env)
    end["delivered"] = {k: v for k, v in delivered.items() if k != "path"}
    ns["discover"] = time.perf_counter_ns() - mark

    mark = time.perf_counter_ns()
    treatment = contract["treatments"][job["treatment"]]
    tree = verify_tree(delivered["path"], treatment["manifest"])
    end["delivered"].update(tree)
    if tree["manifest_sha256"] != treatment["manifest_sha256"]:
        raise Refused("the delivered tree's digest is not the contract's")
    candidate = contract["treatments"]["candidate"]["manifest"]
    for relative in contract["adapter"]:
        if sha256_file(os.path.join(REPOSITORY, relative)) != candidate[relative]:
            raise Refused(f"this checkout's {relative} is not the candidate snapshot's")
    if job["treatment"] == "control":
        try:
            baseline.verify(delivered["path"])
        except SystemExit as error:
            raise Refused(f"tools/baseline.py refuses the delivered package: {error}") from None
    ns["verify"] = time.perf_counter_ns() - mark

    owned = tempfile.mkdtemp(prefix="agx-total-", dir=env["RUNNER_TEMP"])
    end["_owned"] = owned
    for name in ("home", "runner-temp"):
        os.mkdir(os.path.join(owned, name), 0o700)
    argv, child_env = command(contract, job, delivered["path"], capture, owned)
    result = run_check(argv, child_env, owned, contract["deadline_per_run_s"])
    ns["check"] = result.pop("ns")
    end["check"] = result
    expected = contract["expected"]
    observed = {k: result[k] for k in ("exit", "stdout_bytes", "stdout_sha256", "stderr_bytes")}
    if result["timed_out"]:
        raise Refused(f"the check did not finish within {contract['deadline_per_run_s']}s")
    if observed != {k: expected[k] for k in observed}:
        raise Refused(f"the check's result {observed} is not the expected one")


def cleanup(end: dict, ns: dict) -> list[str]:
    """Removes the worker's own directory, after proving the check left
    nothing in the RUNNER_TEMP it was given."""
    owned = end.pop("_owned", None)
    if owned is None:
        return []
    problems = []
    mark = time.perf_counter_ns()
    leftover = os.listdir(os.path.join(owned, "runner-temp"))
    if leftover:
        problems.append(f"the check left {sorted(leftover)} in its RUNNER_TEMP")
    shutil.rmtree(owned, ignore_errors=True)
    removed = not os.path.lexists(owned)
    if not removed:
        problems.append(f"cannot remove {owned}")
    ns["cleanup"] = time.perf_counter_ns() - mark
    end["cleanup"] = {"runner_temp_empty": not leftover, "owned_removed": removed}
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--job", required=True)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--contract", default=total_plan.CONTRACT)
    args = parser.parse_args(argv)
    contract, contract_sha256 = total_plan.load(args.contract)
    job = next((j for j in total_plan.jobs(contract) if j["id"] == args.job), None)
    if job is None:
        raise SystemExit(f"total_run: {args.job} is not a declared job")
    env = dict(os.environ)
    nonce = secrets.token_hex(16)
    begin = {
        "v": PROTOCOL, "nonce": nonce, "job": job["id"], "arch": job["arch"], "pair": job["pair"],
        "position": job["position"], "treatment": job["treatment"], "snapshot": job["snapshot"],
        "source": env.get("AGX_SOURCE_SHA", ""), "run": env.get("GITHUB_RUN_ID", ""),
        "attempt": env.get("GITHUB_RUN_ATTEMPT", ""), "workflow_ref": env.get("GITHUB_WORKFLOW_REF", ""),
        "case": contract["case"], "contract_sha256": contract_sha256,
    }
    begin["wall_ns"] = time.time_ns()
    started = time.perf_counter_ns()
    emit("begin", begin)

    ns: dict[str, int] = {}
    end: dict = {"v": PROTOCOL, "nonce": nonce}
    problems: list[str] = []
    try:
        work(contract, job, os.path.abspath(args.capture), env, ns, end)
    except Refused as refused:
        problems.append(str(refused))
    except Exception as error:  # noqa: BLE001 - any failure is recorded, then cleanup runs
        problems.append(f"{type(error).__name__}: {error}")
    problems += cleanup(end, ns)
    end["problems"] = problems
    end["ok"] = not problems
    ns["worker"] = time.perf_counter_ns() - started
    end["ns"] = ns
    end["wall_ns"] = time.time_ns()
    emit("end", end)
    return 0 if end["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
