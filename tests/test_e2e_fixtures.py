"""The CI cost study's frozen workloads and the local fixture branches built
from them."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from e2e import build_fixtures, gen_workloads

from .support import ROOT

_PARENT = gen_workloads.BASE_PARENT


def _generate() -> tuple[tempfile.TemporaryDirectory, Path, dict]:
    directory = tempfile.TemporaryDirectory()
    out = Path(directory.name) / "workloads"
    return directory, out, gen_workloads.generate(out)


class GeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory, cls.out, cls.manifest = _generate()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def test_regenerated_workloads_match_the_committed_lock(self) -> None:
        self.assertEqual(gen_workloads.check(None), [])
        lock = json.loads(gen_workloads.LOCK.read_text(encoding="utf-8"))
        self.assertEqual(gen_workloads.lock_of(self.out), lock)

    def test_check_reports_a_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = json.loads(gen_workloads.LOCK.read_text(encoding="utf-8"))
            lock["files"]["w3/pr/title.txt"]["sha256"] = "0" * 64
            fake = Path(directory) / "workloads.lock.json"
            fake.write_text(json.dumps(lock), encoding="utf-8")
            original = gen_workloads.LOCK
            gen_workloads.LOCK = fake
            try:
                problems = gen_workloads.check(None)
            finally:
                gen_workloads.LOCK = original
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("w3/pr/title.txt"))

    def test_the_four_workloads_are_the_designed_ones(self) -> None:
        workloads = self.manifest["workloads"]
        self.assertEqual(list(workloads), ["W3", "W1", "W2", "W4"])
        shape = {
            key: (len(entry["commits"]), entry["pull_request_diff"]["files"], entry["expected"]["exit_code"])
            for key, entry in workloads.items()
        }
        self.assertEqual(shape, {"W3": (1, 1, 0), "W1": (3, 6, 0), "W2": (8, 40, 0), "W4": (1, 1, 1)})
        self.assertEqual(self.manifest["inputs"], {"require-model-attribution": "true", "hidden-unicode": "error"})

        idle = self.out / workloads["W3"]["commits"][0]["files"][0]["file"]
        self.assertEqual(idle.read_bytes().count(b"\n"), 40)
        self.assertTrue(idle.read_bytes().isascii())

        medium = workloads["W1"]
        self.assertEqual(medium["name"], "medium controlled")
        self.assertEqual(medium["pull_request_diff"]["added_lines"], 600)
        self.assertTrue(medium["body"]["text"].splitlines()[-1].startswith("Generated with "))
        docs = b"".join(
            (self.out / item["file"]).read_bytes()
            for commit in medium["commits"] for item in commit["files"] if item["path"].endswith(".md")
        )
        self.assertFalse(docs.isascii())

        heavy = workloads["W2"]
        self.assertEqual(heavy["name"], "heavy controlled")
        files = {item["path"]: item for commit in heavy["commits"] for item in commit["files"]}
        lock = files["workload/w2/package-lock.json"]
        self.assertEqual((lock["bytes"], lock["lines"]), (3_800_000, 1))
        markdown = [item for path, item in files.items() if path.startswith("workload/w2/docs/")]
        self.assertEqual(sum(item["bytes"] for item in markdown), 2 * 2**20)
        cjk = (self.out / files["workload/w2/i18n/cjk-notes.txt"]["file"]).read_text(encoding="utf-8")
        self.assertIn("文档", cjk)

    def test_the_failing_workload_has_its_fixed_finding_set(self) -> None:
        failing = self.manifest["workloads"]["W4"]["expected"]
        self.assertEqual((failing["exit_code"], failing["conclusion"], failing["errors"]), (1, "failure", 4))
        self.assertEqual(
            [(finding["title"], finding["line"]) for finding in failing["findings"]],
            [
                ("PR model attribution required", None),
                ("Bot co-author trailer in a commit", None),
                ("Invisible character", 12),
                ("Invisible character", 27),
            ],
        )
        head = self.manifest["workloads"]["W4"]["head_sha"]
        self.assertIn(f"Commit {head} contains a bot Co-Authored-By line", failing["log_lines"][1])
        self.assertIn("U+200D ZERO WIDTH JOINER", failing["log_lines"][2])
        self.assertIn("U+202E RIGHT-TO-LEFT OVERRIDE", failing["log_lines"][3])
        self.assertEqual(
            failing["log_lines"][-1],
            "agent-guardrails: checked 1 commit, 1 changed file, and the PR title and body: 4 errors, 0 warnings",
        )
        for key in ("W1", "W2", "W3"):
            expected = self.manifest["workloads"][key]["expected"]
            self.assertEqual((expected["errors"], expected["warnings"], expected["findings"]), (0, 0, []))

    def test_counts_are_recorded(self) -> None:
        for key, entry in self.manifest["workloads"].items():
            with self.subTest(workload=key):
                counts = entry["counts"]
                self.assertGreater(counts["code_points"], 0)
                self.assertGreaterEqual(counts["work_units"], counts["code_points"])
                self.assertGreater(counts["records"], entry["pull_request_diff"]["added_lines"])
        self.assertGreater(self.manifest["workloads"]["W2"]["counts"]["code_points"], 3_800_000)

    def test_refuses_to_write_into_the_repository(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as stderr:
            gen_workloads.main_cli([str(ROOT / "e2e" / "corpus")])
        self.assertIn("outside the repository", stderr.getvalue())
        self.assertFalse((ROOT / "e2e" / "corpus").exists())


def _git(repository: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(repository.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    return subprocess.run(
        ["git", "-C", str(repository), *args], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


def _has_parent() -> bool:
    return shutil.which("git") is not None and subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{_PARENT}^{{commit}}"], capture_output=True
    ).returncode == 0


@unittest.skipUnless(_has_parent(), f"needs git and {_PARENT} in this checkout; a shallow clone lacks it")
class FixtureBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory, cls.out, cls.manifest = _generate()
        cls.clone = Path(cls.directory.name) / "clone.git"
        subprocess.run(
            ["git", "clone", "--bare", "--quiet", "--no-local", str(ROOT), str(cls.clone)],
            check=True, capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def branches(self) -> dict:
        return {
            self.manifest["base"]["branch"]: self.manifest["base"]["commit"],
            **{entry["head_branch"]: entry["head_sha"] for entry in self.manifest["workloads"].values()},
        }

    def test_two_builds_give_the_predicted_commits(self) -> None:
        first = build_fixtures.build(self.out, self.clone)
        second = build_fixtures.build(self.out, self.clone)
        self.assertEqual(first, second)
        self.assertEqual(first, self.branches())
        for branch, sha in first.items():
            self.assertEqual(_git(self.clone, "rev-parse", f"refs/heads/{branch}"), sha)
            self.assertNotIn("gpgsig", _git(self.clone, "cat-file", "commit", sha))

    def test_the_base_only_removes_the_workflows(self) -> None:
        build_fixtures.build(self.out, self.clone)
        base = self.manifest["base"]["commit"]
        self.assertEqual(_git(self.clone, "rev-parse", f"{base}^"), _PARENT)
        changes = _git(self.clone, "diff-tree", "-r", "--name-status", "--no-renames", _PARENT, base).splitlines()
        self.assertTrue(changes)
        for change in changes:
            status, path = change.split("\t")
            self.assertEqual(status, "D")
            self.assertTrue(path.startswith(".github/workflows/"), path)

    def test_never_moves_an_existing_branch(self) -> None:
        build_fixtures.build(self.out, self.clone)
        branch = "refs/heads/e2e/workload-w1"
        _git(self.clone, "update-ref", branch, _PARENT)
        try:
            with self.assertRaises(build_fixtures.FixtureError):
                build_fixtures.build(self.out, self.clone)
            self.assertEqual(_git(self.clone, "rev-parse", branch), _PARENT)
        finally:
            _git(self.clone, "update-ref", branch, self.manifest["workloads"]["W1"]["head_sha"])

    def test_refuses_a_workload_file_that_is_not_the_locked_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "workloads"
            shutil.copytree(self.out, copy)
            (copy / "w3" / "pr" / "body.md").write_text("Edited\n", encoding="utf-8")
            with self.assertRaisesRegex(build_fixtures.FixtureError, "w3/pr/body.md"):
                build_fixtures.build(copy, self.clone)

    def test_real_git_confirms_the_expected_output_and_counts(self) -> None:
        branches = build_fixtures.build(self.out, self.clone)
        results = build_fixtures.verify(self.out, self.clone, branches)
        self.assertEqual(set(results), {"W1", "W2", "W3", "W4"})
        for key, result in results.items():
            with self.subTest(workload=key):
                self.assertEqual(result["problems"], [])
                self.assertEqual(result["records"], self.manifest["workloads"][key]["counts"]["records"])


class ScheduleTests(unittest.TestCase):
    def test_the_frozen_rotations_and_order(self) -> None:
        from e2e import analyze

        self.assertEqual({key: analyze.rotation(key) for key in analyze.CYCLE}, {"W3": 1, "W1": 0, "W2": 2, "W4": 1})
        sequence = analyze.schedule()
        self.assertEqual(len(sequence), 48)
        orders = {key: "".join(item["order"] for item in sequence if item["workload"] == key) for key in analyze.CYCLE}
        self.assertEqual(orders, {"W3": "BCA" * 4, "W1": "ABC" * 4, "W2": "CAB" * 4, "W4": "BCA" * 4})
        self.assertEqual([item["workload"] for item in sequence[:8]], ["W3", "W1", "W2", "W4"] * 2)
        contract = json.loads((ROOT / "e2e" / "contract.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["schedule"]["sequence"], sequence)
        self.assertEqual(contract["schedule"]["rotations"], analyze.FROZEN_ROTATIONS)

    def test_the_contract_binds_the_locked_manifest(self) -> None:
        contract = json.loads((ROOT / "e2e" / "contract.json").read_text(encoding="utf-8"))
        lock = json.loads(gen_workloads.LOCK.read_text(encoding="utf-8"))
        bound = contract["fixtures"]["workload_manifest"]
        self.assertEqual({"sha256": bound["sha256"], "bytes": bound["bytes"]}, lock["files"]["manifest.json"])
        self.assertEqual(contract["fixtures"]["base"]["sha"], gen_workloads.base_commit())


if __name__ == "__main__":
    unittest.main()
