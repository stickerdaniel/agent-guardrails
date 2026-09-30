"""Create the local fixture branches of the E2E-3 study from generated workloads.

    python3 e2e/build_fixtures.py WORKLOADS_DIR CLONE [--verify]

WORKLOADS_DIR is the output of gen_workloads.py, which must match
e2e/workloads.lock.json. CLONE is a local clone of agent-guardrails that
holds 3937a2d. The builder writes objects and exactly five local branches
there: e2e/workload-base, one commit on 3937a2d that removes
.github/workflows, and e2e/workload-w1 to -w4, built from the manifest with
its fixed identities and dates. It uses plumbing only, so the clone's working
tree, index and HEAD stay as they are, and it never pushes. Every blob, tree
and commit must come out as the manifest predicts; a branch that exists at
another commit is never moved. Running it again gives the same SHAs.

--verify also runs the published checks from this checkout against the
built branches through real git, served from a temporary file:// remote, and
compares their output, exit status, record and work counts with the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = Path(__file__).resolve().parent / "workloads.lock.json"
_ZERO = "0" * 40
# Fixture commits are never signed; the options make that hold whatever the
# clone's own config says.
_COMMIT_OPTIONS = ("-c", "commit.gpgSign=false", "-c", "i18n.commitEncoding=UTF-8")


class FixtureError(Exception):
    pass


class Git:
    """git in one repository, with an environment that ignores every global
    and system setting."""

    def __init__(self, repository: Path, home: Path) -> None:
        self.repository = repository
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "LANG": "C",
            "LC_ALL": "C",
        }

    def run(
        self, *args: str, stdin: bytes | None = None, env: dict | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.repository), *args],
            input=stdin,
            env={**self.env, **(env or {})},
            capture_output=True,
        )

    def __call__(self, *args: str, stdin: bytes | None = None, env: dict | None = None) -> bytes:
        proc = self.run(*args, stdin=stdin, env=env)
        if proc.returncode != 0:
            raise FixtureError(f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
        return proc.stdout

    def text(self, *args: str, **kwargs) -> str:
        return self(*args, **kwargs).decode("ascii").strip()

    def exists(self, spec: str) -> bool:
        return self.run("cat-file", "-e", spec).returncode == 0

    def ref(self, name: str) -> str | None:
        proc = self.run("rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        return proc.stdout.decode("ascii").strip() if proc.returncode == 0 else None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(workloads: Path) -> dict:
    """The manifest, after checking it is the locked one and that every file
    it names has its recorded size and hash."""
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    manifest_path = workloads / "manifest.json"
    locked = lock["files"].get("manifest.json")
    data = manifest_path.read_bytes()
    if locked != {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}:
        raise FixtureError("manifest.json does not match e2e/workloads.lock.json")
    manifest = json.loads(data)
    for name, entry in lock["files"].items():
        path = workloads / name
        if not path.is_file() or path.stat().st_size != entry["bytes"] or _sha256(path) != entry["sha256"]:
            raise FixtureError(f"{name} does not match e2e/workloads.lock.json")
    return manifest


def _entries(git: Git, tree: str) -> list[tuple[str, str, str, str]]:
    """(mode, type, sha, name) of one tree."""
    raw = git("ls-tree", "-z", tree)
    entries = []
    for item in raw.split(b"\0"):
        if item:
            meta, name = item.split(b"\t", 1)
            mode, kind, sha = meta.decode("ascii").split(" ")
            entries.append((mode, kind, sha, name.decode("utf-8")))
    return entries


def _mktree(git: Git, entries) -> str:
    data = b"".join(
        f"{mode} {kind} {sha}\t".encode("ascii") + name.encode("utf-8") + b"\0"
        for mode, kind, sha, name in entries
    )
    return git.text("mktree", "-z", stdin=data)


def _commit(git: Git, tree: str, parent: str, identity: dict, message: str) -> str:
    date = f"@{identity['epoch']} {identity['tz']}"
    env = {
        "GIT_AUTHOR_NAME": identity["name"],
        "GIT_AUTHOR_EMAIL": identity["email"],
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": identity["name"],
        "GIT_COMMITTER_EMAIL": identity["email"],
        "GIT_COMMITTER_DATE": date,
    }
    return git.text(
        *_COMMIT_OPTIONS, "commit-tree", "--no-gpg-sign", tree, "-p", parent,
        stdin=message.encode("utf-8"), env=env,
    )


def _expect(what: str, actual: str, predicted: str) -> str:
    if actual != predicted:
        raise FixtureError(f"{what} is {actual}, the manifest predicts {predicted}")
    return actual


def _nested(git: Git, blobs: dict[str, str]) -> list[tuple[str, str, str, str]]:
    """Tree entries for a mapping of relative path to blob SHA."""
    directories: dict[str, dict[str, str]] = {}
    entries = []
    for path, blob in blobs.items():
        head, _, rest = path.partition("/")
        if rest:
            directories.setdefault(head, {})[rest] = blob
        else:
            entries.append(("100644", "blob", blob, head))
    for name, content in directories.items():
        entries.append(("040000", "tree", _mktree(git, _nested(git, content)), name))
    return entries


def build(workloads: Path, clone: Path) -> dict:
    """Create or confirm the five branches. Returns their SHAs."""
    manifest = load_manifest(workloads)
    with tempfile.TemporaryDirectory(prefix="e2e-fixtures-") as home:
        git = Git(clone, Path(home))
        base = manifest["base"]
        if not git.exists(f"{base['parent']}^{{commit}}"):
            raise FixtureError(f"{clone} does not hold {base['parent']}")

        root = _entries(git, f"{base['parent']}^{{tree}}")
        (github,) = [entry for entry in root if entry[3] == ".github"]
        github_entries = _entries(git, github[2])
        kept = [entry for entry in github_entries if entry[3] != "workflows"]
        if len(kept) != len(github_entries) - 1:
            raise FixtureError(".github/workflows is not in the base parent")
        base_root = [
            entry if entry[3] != ".github" else ("040000", "tree", _mktree(git, kept), ".github")
            for entry in root
        ]
        _expect("the base tree", _mktree(git, base_root), base["tree"])
        identity = {**manifest["identity"], "epoch": base["epoch"], "tz": "+0000"}
        made = _commit(git, base["tree"], base["parent"], identity, base["message"])
        base_sha = _expect("the base commit", made, base["commit"])

        heads = {}
        for key, workload in manifest["workloads"].items():
            parent = base_sha
            blobs: dict[str, str] = {}
            for commit in workload["commits"]:
                paths = "".join(f"{workloads / item['file']}\n" for item in commit["files"])
                written = git(
                    "hash-object", "-w", "--no-filters", "--stdin-paths", stdin=paths.encode("utf-8")
                )
                for item, blob in zip(commit["files"], written.decode("ascii").split(), strict=True):
                    blobs[item["path"]] = _expect(f"the blob of {key} {item['path']}", blob, item["git_blob"])
                tree = _mktree(git, [*base_root, *_nested(git, blobs)])
                _expect(f"the tree of {key} commit {commit['index']}", tree, commit["tree"])
                message = (workloads / commit["message"]["file"]).read_bytes().decode("utf-8")
                sha = _commit(git, tree, parent, commit["author"], message)
                parent = _expect(f"{key} commit {commit['index']}", sha, commit["sha"])
            heads[workload["head_branch"]] = _expect(f"the head of {key}", parent, workload["head_sha"])

        branches = {base["branch"]: base_sha, **heads}
        for branch, sha in branches.items():
            current = git.ref(f"refs/heads/{branch}")
            if current is not None and current != sha:
                raise FixtureError(f"refusing to move {branch} from {current} to {sha}")
        for branch, sha in branches.items():
            # With the old value given, update-ref creates the branch only if
            # it is still absent, and otherwise leaves it where it is.
            git("update-ref", f"refs/heads/{branch}", sha, git.ref(f"refs/heads/{branch}") or _ZERO)
        return branches


def verify(workloads: Path, clone: Path, branches: dict) -> dict:
    """Run the published checks on each fixture pull request through real
    git and compare with the manifest. Returns the measured counts."""
    sys.path.insert(0, str(ROOT))
    from agent_guardrails import gitdata, main, rules

    manifest = load_manifest(workloads)
    token = "ghs_E2E0000000000000000000000000000000000"
    results = {}
    with tempfile.TemporaryDirectory(prefix="e2e-verify-") as directory:
        root = Path(directory).resolve()
        git = Git(clone, root)
        server = root / "server"
        bare = server / "owner" / "repo.git"
        subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], env=git.env, check=True)
        base = manifest["base"]
        refspecs = [f"{branches[base['branch']]}:refs/heads/{base['branch']}"]
        numbers = {}
        for number, (key, workload) in enumerate(manifest["workloads"].items(), 1):
            numbers[key] = number
            refspecs.append(f"{branches[workload['head_branch']]}:refs/pull/{number}/head")
        git("push", "--quiet", str(bare), *refspecs)

        counted = {"records": 0, "stdout_bytes": 0}
        original_charge = gitdata._Repository.charge
        original_run = gitdata._Repository.run
        original_spend = rules.Budget.spend
        budgets = []

        def charge(self, output):
            counted["records"] += output.count(b"\n") + output.count(b"\0") + 1
            return original_charge(self, output)

        def run(self, *args, **kwargs):
            proc = original_run(self, *args, **kwargs)
            counted["stdout_bytes"] += len(proc.stdout)
            return proc

        def spend(self, line, where):
            if self not in budgets:
                budgets.append(self)
            return original_spend(self, line, where)

        gitdata._Repository.charge = charge
        gitdata._Repository.run = run
        rules.Budget.spend = spend
        try:
            for key, workload in manifest["workloads"].items():
                counted.update(records=0, stdout_bytes=0)
                budgets.clear()
                event = root / f"event-{key}.json"
                event.write_text(
                    json.dumps({
                        "pull_request": {
                            "number": numbers[key],
                            "title": workload["title"]["text"],
                            "body": workload["body"]["text"],
                            "user": {"login": "e2e-maintainer"},
                            "base": {"ref": base["branch"], "sha": base["commit"]},
                            "head": {"sha": workload["head_sha"]},
                        }
                    }),
                    encoding="utf-8",
                )
                (root / "runner").mkdir(exist_ok=True)
                environ = {
                    "PATH": os.environ.get("PATH", os.defpath),
                    "CA_REQUIRE_MODEL_ATTRIBUTION": manifest["inputs"]["require-model-attribution"],
                    "CA_HIDDEN_UNICODE": manifest["inputs"]["hidden-unicode"],
                    "CA_TOKEN": token,
                    "CA_SERVER_URL": server.as_uri(),
                    "CA_REPOSITORY": "owner/repo",
                    "CA_EVENT_NAME": "pull_request_target",
                    "GITHUB_EVENT_PATH": str(event),
                    "RUNNER_TEMP": str(root / "runner"),
                }
                stdout = io.StringIO()
                code = main.main(environ, stdout)
                lines = stdout.getvalue().split("\n")
                stdout_file = workload["expected"]["stdout_after_git_version"]["file"]
                expected = (workloads / stdout_file).read_text(encoding="utf-8")
                problems = []
                if lines[0] != f"::add-mask::{gitdata.encode_credential(token)}":
                    problems.append("the output does not start with the mask")
                if not lines[1].startswith("agent-guardrails: git version "):
                    problems.append("the git version line is missing")
                if "".join(f"{line}\n" for line in lines[2:-1]) != expected or lines[-1] != "":
                    problems.append("the output after the git version line differs")
                if code != workload["expected"]["exit_code"]:
                    problems.append(f"exit {code}, expected {workload['expected']['exit_code']}")
                if counted["records"] != workload["counts"]["records"]:
                    predicted = workload["counts"]["records"]
                    problems.append(f"{counted['records']} records, predicted {predicted}")
                if len(budgets) != 1 or budgets[0]._work != workload["counts"]["work_units"]:
                    problems.append("work units differ from the manifest")
                results[key] = {
                    "exit_code": code,
                    "records": counted["records"],
                    "git_stdout_bytes": counted["stdout_bytes"],
                    "work_units": budgets[0]._work if budgets else None,
                    "git_version": lines[1][len("agent-guardrails: "):],
                    "problems": problems,
                }
        finally:
            gitdata._Repository.charge = original_charge
            gitdata._Repository.run = original_run
            rules.Budget.spend = original_spend
    return results


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("workloads", type=Path, help="output directory of gen_workloads.py")
    parser.add_argument("clone", type=Path, help="local clone that holds 3937a2d")
    parser.add_argument(
        "--verify", action="store_true", help="also run the published checks through real git"
    )
    args = parser.parse_args(argv)
    try:
        branches = build(args.workloads.resolve(), args.clone.resolve())
        result = {"branches": branches}
        if args.verify:
            result["verify"] = verify(args.workloads.resolve(), args.clone.resolve(), branches)
    except FixtureError as error:
        print(f"build_fixtures: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    failed = any(entry["problems"] for entry in result.get("verify", {}).values())
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
