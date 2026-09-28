"""build/reproduce.py's owned container against the real Docker daemon.

    python3 -I -B experiments/native-distribution/tests/docker_ownership.py

Uses the recipe's pinned image for this machine, pulled by digest when it is
absent, and only containers this script creates: a stalled one the helper
must stop and remove at its deadline, a completing one, and unrelated
sentinels that must survive. Every container a test made is removed again by
its ID afterwards, also when an assertion failed; nothing is removed by a
broad filter. Named so tests/run_all.py does not discover it: the unit suite
needs no Docker socket, and the reproduce job, which has Docker, runs this
file by name. Fails on any skip and when nothing ran.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))
import reproduce  # noqa: E402

TARGETS = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
SENTINEL_LABEL = "io.github.stickerdaniel.agx-ownership-test"
STALL_DEADLINE_S = 3


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


def target() -> dict:
    with open(ROOT / "build" / "recipe.json", encoding="utf-8") as handle:
        recipe = json.load(handle)
    return recipe["targets"][TARGETS[os.uname().machine]]


def all_containers() -> set[str]:
    listed = docker("ps", "--all", "--quiet", "--no-trunc")
    if listed.returncode != 0:
        raise AssertionError(f"cannot list containers: {listed.stderr.strip()}")
    return set(listed.stdout.split())


def exists(cid: str) -> bool:
    return docker("inspect", "--type", "container", cid).returncode == 0


def running(cid: str) -> bool:
    shown = docker("inspect", "--type", "container", "--format", "{{.State.Running}}", cid)
    return shown.returncode == 0 and shown.stdout.strip() == "true"


def owned_by(token: str) -> list[str]:
    listed = docker("ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={reproduce.OWNER_LABEL}={token}")
    return listed.stdout.split()


class OwnedContainerDockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.target = target()
        cls.image = cls.target["image"]
        reproduce.ensure_image(cls.image, cls.target["platform"])
        cls.options = ["--network", "none", "--platform", cls.target["platform"]]

    def setUp(self) -> None:
        self.before = all_containers()
        self.sentinels: list[str] = []
        self.tokens: list[str] = []
        self.addCleanup(self.remove_own)

    def remove_own(self) -> None:
        """This test's containers, by ID: its sentinels, and anything still
        carrying one of its tokens when the helper failed to remove it."""
        for cid in self.sentinels + [cid for token in self.tokens for cid in owned_by(token)]:
            docker("rm", "--force", cid)

    def token(self) -> str:
        token = secrets.token_hex(12)
        self.tokens.append(token)
        return token

    def sentinel(self, *, name: str | None = None, start: bool = True) -> str:
        """An unrelated container the helper must never touch."""
        naming = ["--name", name] if name else []
        created = docker(
            "create", *naming, "--label", f"{SENTINEL_LABEL}={secrets.token_hex(6)}",
            *self.options, self.image, "sleep", "600",
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        cid = created.stdout.strip()
        self.sentinels.append(cid)
        if start:
            self.assertEqual(docker("start", cid).returncode, 0)
        return cid

    def assertPreexistingUntouched(self) -> None:
        self.assertEqual(self.before - all_containers(), set(), "a container that existed before the test is gone")

    def test_a_stalled_container_is_removed_at_the_deadline(self) -> None:
        sentinel = self.sentinel()
        token = self.token()
        started = time.monotonic()
        with self.assertRaises(reproduce.OwnedRunError) as raised:
            reproduce.run_owned(self.options, self.image, ["sleep", "600"], STALL_DEADLINE_S, token=token)
        elapsed = time.monotonic() - started
        self.assertEqual(raised.exception.problems, [f"the container did not finish within {STALL_DEADLINE_S}s"])
        stalled = raised.exception.container
        self.assertRegex(stalled or "", r"^[0-9a-f]{64}$")
        self.assertFalse(exists(stalled), "the stalled container outlived the helper")
        self.assertEqual(owned_by(token), [])
        self.assertLess(elapsed, STALL_DEADLINE_S + 3 * reproduce.CLEANUP_DEADLINE_S)
        self.assertTrue(running(sentinel), "an unrelated running container was stopped")
        self.assertPreexistingUntouched()

    def test_a_completed_container_keeps_its_status_and_output_and_is_removed(self) -> None:
        sentinel = self.sentinel(start=False)
        token = self.token()
        result = reproduce.run_owned(
            self.options, self.image, ["sh", "-c", "echo built; echo note >&2; exit 3"], 120, token=token,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "built\n", "note\n"))
        self.assertEqual(owned_by(token), [])
        self.assertTrue(exists(sentinel), "an unrelated stopped container was removed")
        self.assertPreexistingUntouched()

    def test_a_name_collision_fails_and_removes_nothing(self) -> None:
        token = self.token()
        taken = self.sentinel(name=f"agx-reproduce-{token}")
        with self.assertRaises(reproduce.OwnedRunError) as raised:
            reproduce.run_owned(self.options, self.image, ["true"], 120, token=token)
        self.assertIn("docker create failed", raised.exception.problems[0])
        self.assertIsNone(raised.exception.container)
        self.assertTrue(running(taken), "the container that already had the name was touched")
        self.assertPreexistingUntouched()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(OwnedContainerDockerTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    skipped = [f"{test.id()}: {reason}" for test, reason in result.skipped]
    for line in skipped:
        print(f"docker_ownership: skipped, which counts as a failure here: {line}", file=sys.stderr)
    print(f"docker_ownership: {result.testsRun} tests ran, {len(skipped)} skipped", file=sys.stderr)
    sys.exit(0 if result.wasSuccessful() and not skipped and result.testsRun > 0 else 1)
