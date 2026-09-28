"""build/reproduce.py's owned container, against a stand-in Docker client.

The stand-in keeps the daemon's containers in a state file, so a container
outlives a client that was killed, as it does with the real daemon. Every
call it receives is recorded; the tests assert which containers survive and
which IDs cleanup removed. Runs anywhere Python does; tests/docker_ownership.py
repeats the timeout and collision cases against a real daemon.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))
import reproduce  # noqa: E402

FAKE_DOCKER = """#!{python} -I
import hashlib, json, os, sys, time
STATE = {state!r}
OWNER = {owner!r}

def load():
    with open(STATE, encoding="utf-8") as handle:
        return json.load(handle)

def save(state):
    with open(STATE + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(STATE + ".tmp", STATE)

args = sys.argv[1:]
state = load()
state["calls"].append({{"argv": args, "pid": os.getpid()}})
save(state)
command = args[0]
if command == "create":
    name = args[args.index("--name") + 1]
    key, _, value = args[args.index("--label") + 1].partition("=")
    if any(c["name"] == name for c in state["containers"].values()):
        print(f'Conflict. The container name "/{{name}}" is already in use', file=sys.stderr)
        sys.exit(125)
    cid = hashlib.sha256(name.encode()).hexdigest()
    state["containers"][cid] = {{"name": name, "labels": {{key: value}}}}
    save(state)
    if state["create"] == "hang":
        time.sleep(30)
    print(cid)
    sys.exit(0)
if command == "start":
    if args[-1] not in state["containers"]:
        sys.exit(1)
    if state["start"] == "stall":
        time.sleep(30)
    print("built")
    print("note", file=sys.stderr)
    sys.exit(state["exit"])
if command == "ps":
    key, _, value = args[args.index("--filter") + 1].removeprefix("label=").partition("=")
    for cid, container in state["containers"].items():
        if container["labels"].get(key) == value:
            print(cid)
    sys.exit(0)
if command == "inspect":
    container = state["containers"].get(args[-1])
    if container is None:
        print(f"Error: No such container: {{args[-1]}}", file=sys.stderr)
        sys.exit(1)
    if "--format" in args:
        print("/" + container["name"] + "\\t" + container["labels"].get(OWNER, ""))
    sys.exit(0)
if command == "rm":
    if state["rm"] == "fail":
        print("Error: cannot remove container", file=sys.stderr)
        sys.exit(1)
    state["containers"].pop(args[-1], None)
    save(state)
    sys.exit(0)
sys.exit(2)
"""


def container_id(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


class OwnedContainerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="agx-reproduce-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state_path = self.root / "state.json"
        fake = self.root / "docker"
        fake.write_text(
            FAKE_DOCKER.format(python=sys.executable, state=str(self.state_path), owner=reproduce.OWNER_LABEL),
            encoding="utf-8",
        )
        fake.chmod(0o755)
        # Unrelated containers: one carrying the owner label with another
        # token, one with nothing. Neither may ever be removed.
        self.unrelated = {
            container_id("other-token"): {
                "name": "agx-reproduce-other", "labels": {reproduce.OWNER_LABEL: "other"},
            },
            container_id("foreign"): {"name": "foreign", "labels": {}},
        }
        self.write_state(containers=dict(self.unrelated))
        for name, value in (("DOCKER", str(fake)), ("CREATE_DEADLINE_S", 10), ("CLEANUP_DEADLINE_S", 10)):
            self.addCleanup(setattr, reproduce, name, getattr(reproduce, name))
            setattr(reproduce, name, value)

    def write_state(self, **settings) -> None:
        state = {"containers": {}, "calls": [], "create": "ok", "start": "ok", "exit": 0, "rm": "ok"}
        state.update(settings)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def set_state(self, **settings) -> None:
        state = self.state()
        state.update(settings)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def assertRemovedOnly(self, owned: set[str]) -> None:
        """Cleanup removed exactly these IDs, by ID, and nothing else, with
        no broad Docker command anywhere."""
        calls = [call["argv"] for call in self.state()["calls"]]
        self.assertEqual({argv[0] for argv in calls} - {"create", "start", "ps", "inspect", "rm"}, set())
        removed = [argv for argv in calls if argv[0] == "rm"]
        self.assertEqual({tuple(argv) for argv in removed}, {("rm", "--force", cid) for cid in owned})
        listings = [argv for argv in calls if argv[0] == "ps"]
        for argv in listings:
            self.assertRegex(argv[-1], rf"^label={reproduce.OWNER_LABEL}=[0-9a-z-]+$")

    def test_a_completed_run_keeps_its_status_and_removes_only_its_container(self) -> None:
        self.set_state(exit=3)
        result = reproduce.run_owned(["--network", "none"], "image", ["true"], 10, token="done")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "built\n", "note\n"))
        self.assertEqual(self.state()["containers"], self.unrelated)
        self.assertRemovedOnly({container_id("agx-reproduce-done")})

    def test_a_stalled_run_is_removed_at_its_deadline(self) -> None:
        self.set_state(start="stall")
        started = time.monotonic()
        with self.assertRaises(reproduce.OwnedRunError) as raised:
            reproduce.run_owned([], "image", ["sleep", "600"], 1, token="stall")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(raised.exception.problems, ["the container did not finish within 1s"])
        self.assertEqual(raised.exception.container, container_id("agx-reproduce-stall"))
        self.assertEqual(self.state()["containers"], self.unrelated)
        self.assertRemovedOnly({container_id("agx-reproduce-stall")})
        # The killed client itself was reaped, not left running.
        (client,) = [call["pid"] for call in self.state()["calls"] if call["argv"][0] == "start"]
        with self.assertRaises(ProcessLookupError):
            os.kill(client, 0)

    def test_a_name_collision_fails_and_removes_nothing(self) -> None:
        collision = {"name": "agx-reproduce-taken", "labels": {}}
        self.set_state(containers={**self.unrelated, container_id("taken"): collision})
        with self.assertRaises(reproduce.OwnedRunError) as raised:
            reproduce.run_owned([], "image", ["true"], 10, token="taken")
        self.assertIn("docker create failed: Conflict", raised.exception.problems[0])
        self.assertIsNone(raised.exception.container)
        self.assertEqual(self.state()["containers"], {**self.unrelated, container_id("taken"): collision})
        self.assertRemovedOnly(set())

    def test_a_create_that_times_out_still_removes_what_it_made(self) -> None:
        self.set_state(create="hang")
        reproduce.CREATE_DEADLINE_S = 1
        with self.assertRaises(reproduce.OwnedRunError) as raised:
            reproduce.run_owned([], "image", ["true"], 10, token="slow")
        self.assertEqual(raised.exception.problems, ["docker create did not finish within 1s"])
        self.assertEqual(self.state()["containers"], self.unrelated)
        self.assertRemovedOnly({container_id("agx-reproduce-slow")})

    def test_a_failed_removal_is_reported_after_the_original_failure(self) -> None:
        self.set_state(start="stall", rm="fail")
        with self.assertRaises(reproduce.OwnedRunError) as raised:
            reproduce.run_owned([], "image", ["sleep", "600"], 1, token="stuck")
        cid = container_id("agx-reproduce-stuck")
        self.assertEqual(raised.exception.problems[0], "the container did not finish within 1s")
        self.assertIn(f"cannot remove {cid}: Error: cannot remove container", raised.exception.problems)
        self.assertIn(f"{cid} still exists after removal", raised.exception.problems)


if __name__ == "__main__":
    unittest.main()
