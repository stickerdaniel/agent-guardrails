"""launch.sh, run the way the experimental action runs it.

Each test builds a throwaway action directory whose gzip asset holds a small
shell script standing in for the native binary, a RUNNER_TEMP holding an
unrelated sibling directory and file that must survive, and, where a test
needs one, a directory of synthetic tools placed first on PATH. Every wait is
bounded; a launcher that hangs fails its test instead of the suite.

Linux only, like the launcher. Set AGX_REQUIRE_LINUX=1 to make a non-Linux
host an error instead of a skip, and AGX_NOEXEC_DIR to a directory on a
noexec mount to run the noexec case (AGX_REQUIRE_NOEXEC=1 makes its absence
an error). CI sets all three.
"""

from __future__ import annotations

import gzip
import os
import platform
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "launch.sh"
BASH = shutil.which("bash")
TARGETS = {"x86_64": "linux-x64", "aarch64": "linux-arm64"}
DEADLINE_S = 15

if sys.platform != "linux" or BASH is None:
    if os.environ.get("AGX_REQUIRE_LINUX") == "1":
        raise RuntimeError("the launcher tests need Linux and bash")
    raise unittest.SkipTest("the launcher tests need Linux and bash")


def canonical_gzip(payload: bytes) -> bytes:
    """What `gzip -9 -c` writes for stdin, built by hand."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = compressor.compress(payload) + compressor.flush()
    return (
        bytes.fromhex("1f8b0800000000000203")
        + body
        + struct.pack("<II", zlib.crc32(payload), len(payload) & 0xFFFFFFFF)
    )


def alive(pid: int) -> bool:
    """Whether pid is a running (not zombie) process."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            state = handle.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state != "Z"


def wait_for(path: Path, deadline_s: float = DEADLINE_S) -> str:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.02)
    raise AssertionError(f"{path.name} never appeared")


# The stand-in binary for most tests: records that it ran, its arguments and
# the mode of its directory and file, prints a line, exits with EXIT_WITH.
RECORDER = """#!/bin/sh
here=$(dirname "$0")
echo "ran $*" > "$AGX_TEST_OUT/ran"
stat -c '%a' "$here" "$0" > "$AGX_TEST_OUT/modes"
echo "$here" > "$AGX_TEST_OUT/where"
echo "stand-in $*"
exit "${EXIT_WITH:-0}"
"""


class Sandbox:
    def __init__(self, test: unittest.TestCase) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="agx-launcher-"))
        test.addCleanup(self.remove)
        self.action = self.root / "action"
        self.runner_temp = self.root / "runner-temp"
        self.out = self.root / "out"
        self.tools = self.root / "tools"
        for directory in (self.action, self.runner_temp, self.out, self.tools):
            directory.mkdir()
        # Unrelated neighbours in RUNNER_TEMP that cleanup must never touch.
        (self.runner_temp / "sibling").mkdir()
        (self.runner_temp / "sibling" / "keep.txt").write_text("keep\n")
        (self.runner_temp / "keep.txt").write_text("keep\n")
        self.capture = self.root / "input.cap"
        self.capture.write_bytes(b"AGCAP1\nend\n")
        self.target = TARGETS.get(platform.machine(), "linux-x64")

    def remove(self) -> None:
        for path in self.root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        shutil.rmtree(self.root, ignore_errors=True)

    def asset(self, data: bytes, target: str | None = None) -> Path:
        path = self.action / "bin" / (target or self.target) / "agent-guardrails.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def script_asset(self, script: str, target: str | None = None) -> bytes:
        data = canonical_gzip(script.encode())
        self.asset(data, target)
        return data

    def tool(self, name: str, script: str) -> None:
        path = self.tools / name
        path.write_text(script)
        path.chmod(0o755)

    def fake_uname(self, kernel: str = "Linux", machine: str = "x86_64") -> None:
        self.tool("uname", f"#!/bin/sh\ncase \"$1\" in -s) echo '{kernel}' ;; -m) echo '{machine}' ;; *) exit 2 ;; esac\n")

    def env(self, **overrides: str) -> dict:
        env = {
            "PATH": f"{self.tools}:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(self.root),
            "RUNNER_OS": "Linux",
            "RUNNER_TEMP": str(self.runner_temp),
            "GITHUB_ACTION_PATH": str(self.action),
            "AGX_CAPTURE": str(self.capture),
            "AGX_TEST_OUT": str(self.out),
        }
        env.update(overrides)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, **overrides: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, "--noprofile", "--norc", "--", str(LAUNCHER)],
            env=self.env(**overrides),
            capture_output=True,
            text=True,
            timeout=DEADLINE_S,
            stdin=subprocess.DEVNULL,
        )

    def start(self, **overrides: str) -> subprocess.Popen:
        def default_signals() -> None:
            # A runner that cancels a job delivers INT; model one that has not
            # inherited an ignored INT.
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, signal.SIG_DFL)

        return subprocess.Popen(
            [BASH, "--noprofile", "--norc", "--", str(LAUNCHER)],
            env=self.env(**overrides),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            preexec_fn=default_signals,
        )

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.runner_temp.iterdir() if p.name not in ("sibling", "keep.txt"))

    def ran(self) -> bool:
        return (self.out / "ran").exists()


class LauncherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.box = Sandbox(self)

    def assertCleanedUp(self) -> None:
        """Only the launcher's own directory is gone; its neighbours stay."""
        self.assertEqual(self.box.leftovers(), [])
        self.assertEqual((self.box.runner_temp / "sibling" / "keep.txt").read_text(), "keep\n")
        self.assertEqual((self.box.runner_temp / "keep.txt").read_text(), "keep\n")

    def assertRefused(self, result: subprocess.CompletedProcess, message: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error title=agent-guardrails native spike::", result.stdout)
        self.assertIn(message, result.stdout)
        self.assertFalse(self.box.ran(), "the binary ran although the launcher refused")
        self.assertCleanedUp()


class RunTests(LauncherTestCase):
    def test_runs_the_binary_privately_and_removes_its_directory(self) -> None:
        self.box.script_asset(RECORDER)
        result = self.box.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, f"stand-in drive {self.box.capture}\n")
        self.assertEqual((self.box.out / "modes").read_text().split(), ["700", "700"])
        where = Path((self.box.out / "where").read_text().strip())
        self.assertEqual(where.parent, self.box.runner_temp)
        self.assertFalse(where.exists())
        self.assertCleanedUp()

    def test_keeps_the_binary_exit_status(self) -> None:
        self.box.script_asset(RECORDER)
        for status in (1, 7):
            with self.subTest(status=status):
                result = self.box.run(EXIT_WITH=str(status))
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertTrue(self.box.ran())
                self.assertCleanedUp()

    def test_selects_the_asset_by_machine(self) -> None:
        self.box.script_asset("#!/bin/sh\necho x64\n", "linux-x64")
        self.box.script_asset("#!/bin/sh\necho arm64\n", "linux-arm64")
        for machine, expected in (("x86_64", "x64"), ("aarch64", "arm64")):
            with self.subTest(machine=machine):
                self.box.fake_uname(machine=machine)
                result = self.box.run()
                self.assertEqual((result.returncode, result.stdout), (0, f"{expected}\n"), result.stderr)
                self.assertCleanedUp()

    def test_the_real_machine_maps_to_its_asset(self) -> None:
        self.assertIn(platform.machine(), TARGETS, "an unsupported test host")
        self.box.script_asset(RECORDER)
        result = self.box.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.box.ran())

    def test_inherited_xtrace_is_switched_off(self) -> None:
        self.box.script_asset(RECORDER)
        result = self.box.run(SHELLOPTS="xtrace", CA_TOKEN="not-a-real-token-value")
        self.assertEqual(result.returncode, 0, result.stderr)
        traced = [line for line in result.stderr.splitlines() if line.startswith("+")]
        self.assertLessEqual(len(traced), 2, traced)
        self.assertNotIn("not-a-real-token-value", result.stdout + result.stderr)

    def test_a_failed_cleanup_warns_and_keeps_the_status(self) -> None:
        self.box.script_asset(RECORDER)
        self.box.tool("rm", "#!/bin/sh\nexit 1\n")
        result = self.box.run(EXIT_WITH="5")
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertIn("::warning title=agent-guardrails native spike::", result.stdout)
        self.assertEqual(len(self.box.leftovers()), 1)


class GzipTests(LauncherTestCase):
    """The binary runs only after gzip exits 0, whatever gzip wrote first."""

    def setUp(self) -> None:
        super().setUp()
        self.stream = canonical_gzip(RECORDER.encode())

    def test_a_bad_crc_is_never_executed_although_gzip_wrote_it_all(self) -> None:
        crc = struct.unpack("<I", self.stream[-8:-4])[0] ^ 0xFFFFFFFF
        self.box.asset(self.stream[:-8] + struct.pack("<I", crc) + self.stream[-4:])
        result = self.box.run()
        self.assertRefused(result, "could not decompress the")

    def test_a_truncated_stream_is_never_executed(self) -> None:
        self.box.asset(self.stream[:-6])
        self.assertRefused(self.box.run(), "could not decompress the")

    def test_trailing_garbage_is_never_executed(self) -> None:
        self.box.asset(self.stream + b"garbage")
        self.assertRefused(self.box.run(), "could not decompress the")

    def test_a_raw_file_named_gz_is_never_executed(self) -> None:
        self.box.asset(RECORDER.encode())
        self.assertRefused(self.box.run(), "could not decompress the")

    def test_the_real_gzip_accepts_the_canonical_stream(self) -> None:
        self.assertEqual(gzip.decompress(self.stream), RECORDER.encode())


class RefusalTests(LauncherTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.box.script_asset(RECORDER)

    def test_refuses_a_runner_that_is_not_linux(self) -> None:
        self.assertRefused(self.box.run(RUNNER_OS="macOS"), "RUNNER_OS is not Linux")
        self.box.fake_uname(kernel="Darwin")
        self.assertRefused(self.box.run(), "the kernel is not Linux")

    def test_refuses_an_unsupported_architecture(self) -> None:
        self.box.fake_uname(machine="riscv64")
        self.assertRefused(self.box.run(), "unsupported architecture riscv64")
        self.box.fake_uname(machine="bad name!")
        self.assertRefused(self.box.run(), "unsupported architecture an unprintable value")

    def test_refuses_when_a_tool_is_missing(self) -> None:
        for missing in ("gzip", "mktemp", "chmod"):
            with self.subTest(missing=missing):
                only = self.box.root / f"only-{missing}"
                only.mkdir()
                for tool in ("uname", "mktemp", "gzip", "chmod", "rm", "dirname", "stat", "sh"):
                    if tool != missing and shutil.which(tool):
                        (only / tool).symlink_to(shutil.which(tool))
                self.assertRefused(self.box.run(PATH=str(only)), f"{missing} is not on PATH")

    def test_refuses_a_missing_asset_or_capture(self) -> None:
        self.assertRefused(self.box.run(GITHUB_ACTION_PATH=str(self.box.root / "elsewhere")), "asset is missing")
        self.assertRefused(self.box.run(AGX_CAPTURE=""), "the capture input is empty")
        self.assertRefused(self.box.run(AGX_CAPTURE=str(self.box.root / "none.cap")), "not a readable regular file")

    def test_refuses_an_unusable_runner_temp(self) -> None:
        self.assertRefused(self.box.run(RUNNER_TEMP="relative/temp"), "RUNNER_TEMP is not an absolute path")
        self.assertRefused(self.box.run(RUNNER_TEMP=str(self.box.root / "absent")), "RUNNER_TEMP is not a directory")
        self.box.tool("mktemp", "#!/bin/sh\nexit 1\n")
        self.assertRefused(self.box.run(), "cannot create a private temporary directory")

    @unittest.skipIf(os.geteuid() == 0, "root can write into a read-only directory")
    def test_refuses_a_read_only_runner_temp(self) -> None:
        self.box.runner_temp.chmod(0o500)
        try:
            self.assertRefused(self.box.run(), "cannot create a private temporary directory")
        finally:
            self.box.runner_temp.chmod(0o700)

    def test_never_writes_into_or_runs_a_file_already_there(self) -> None:
        # A mktemp whose directory already holds the output name: the
        # redirection must fail rather than reuse the file, and the planted
        # file must never run.
        planted = self.box.root / "planted"
        planted.write_text('#!/bin/sh\necho planted > "$AGX_TEST_OUT/ran"\n')
        planted.chmod(0o700)
        self.box.tool(
            "mktemp",
            f'#!/bin/sh\nd=$({shutil.which("mktemp")} "$@") || exit 1\n'
            f'cp -p "{planted}" "$d/agent-guardrails"\necho "$d"\n',
        )
        self.assertRefused(self.box.run(), "could not decompress the")

    def test_refuses_when_chmod_fails(self) -> None:
        self.box.tool("chmod", "#!/bin/sh\nexit 1\n")
        self.assertRefused(self.box.run(), "cannot mark the extracted binary executable")

    def test_refuses_a_noexec_runner_temp(self) -> None:
        noexec = os.environ.get("AGX_NOEXEC_DIR")
        if not noexec:
            if os.environ.get("AGX_REQUIRE_NOEXEC") == "1":
                self.fail("AGX_NOEXEC_DIR is not set")
            self.skipTest("no noexec mount given in AGX_NOEXEC_DIR")
        temp = Path(tempfile.mkdtemp(dir=noexec))
        self.addCleanup(shutil.rmtree, temp, ignore_errors=True)
        (temp / "keep.txt").write_text("keep\n")
        result = self.box.run(RUNNER_TEMP=str(temp))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("RUNNER_TEMP may be mounted noexec", result.stdout)
        self.assertFalse(self.box.ran())
        self.assertEqual(sorted(p.name for p in temp.iterdir()), ["keep.txt"])


class CommittedAssetTests(LauncherTestCase):
    """The real committed asset for this machine, through the real launcher:
    the one place these tests execute the native drive."""

    def run_committed(self, title: bytes) -> subprocess.CompletedProcess:
        self.box.capture.write_bytes(b"AGCAP1\nmode error\ntitle %d\n%s\nbody 0\n\nend\n" % (len(title), title))
        return self.box.run(GITHUB_ACTION_PATH=str(ROOT))

    def test_a_clean_capture_passes(self) -> None:
        result = self.run_committed(b"Add a parser")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            result.stdout,
            "agent-guardrails: checked 0 commits, 0 changed files, and the PR title and body: 0 errors, 0 warnings\n",
        )
        self.assertCleanedUp()

    def test_a_finding_fails_with_the_binary_status(self) -> None:
        result = self.run_committed("Add a\u200bparser".encode())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("::error title=Invisible character::The PR title has 1 invisible"))
        self.assertCleanedUp()

    def test_a_malformed_capture_is_a_controlled_failure(self) -> None:
        self.box.capture.write_bytes(b"AGCAP1\ntitle 100\nshort\n")
        result = self.box.run(GITHUB_ACTION_PATH=str(ROOT))
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertEqual(result.stderr, "agent-guardrails: the capture is truncated\n")
        self.assertCleanedUp()


class CancellationTests(LauncherTestCase):
    """A catchable signal while gzip or the binary runs: the child is stopped
    and reaped before the directory goes, then the launcher dies of the same
    signal. Sent to the launcher alone, as a runner may."""

    def finish(self, process: subprocess.Popen) -> tuple[int, str, str]:
        try:
            stdout, stderr = process.communicate(timeout=DEADLINE_S)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            self.fail("the launcher did not end after the signal")
        return process.returncode, stdout, stderr

    def test_a_signal_during_extraction(self) -> None:
        self.box.script_asset(RECORDER)
        pidfile = self.box.out / "gzip.pid"
        self.box.tool("gzip", f'#!/bin/sh\necho $$ > "{pidfile}"\nexec sleep 30\n')
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig.name):
                pidfile.unlink(missing_ok=True)
                process = self.box.start()
                child = int(wait_for(pidfile))
                self.assertTrue(alive(child))
                process.send_signal(sig)
                code, stdout, stderr = self.finish(process)
                self.assertEqual(code, -sig, stdout + stderr)
                self.assertFalse(alive(child), "gzip outlived the launcher")
                self.assertFalse(self.box.ran())
                self.assertCleanedUp()

    def test_a_signal_while_the_binary_runs(self) -> None:
        pidfile = self.box.out / "binary.pid"
        probe = self.box.out / "probe"
        # On TERM the stand-in waits, then reports whether its directory
        # still exists: it must, since the launcher may only remove it after
        # the binary has ended.
        self.box.script_asset(
            "#!/bin/sh\n"
            f'trap \'sleep 0.3; if [ -d "$(dirname "$0")" ]; then echo present; else echo gone; fi > "{probe}"; exit 143\' TERM\n'
            f'echo $$ > "{pidfile}"\n'
            "while :; do sleep 0.05; done\n"
        )
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig.name):
                pidfile.unlink(missing_ok=True)
                probe.unlink(missing_ok=True)
                process = self.box.start()
                child = int(wait_for(pidfile))
                process.send_signal(sig)
                code, stdout, stderr = self.finish(process)
                self.assertEqual(code, -sig, stdout + stderr)
                self.assertEqual(wait_for(probe), "present")
                self.assertFalse(alive(child), "the binary outlived the launcher")
                self.assertCleanedUp()


if __name__ == "__main__":
    unittest.main()
