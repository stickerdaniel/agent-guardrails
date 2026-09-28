"""Starts one measured process per request and reports how it ran.

    python3 -I -S -B bench/spawn.py      (driven by bench/measure.py)

One JSON request per stdin line: argv (argv[0] absolute), env, stdout and
stderr file paths, deadline in seconds. One JSON reply per line: wall time,
CPU, peak RSS, exit status, whether the deadline killed it.

A separate process on purpose. On Linux a child's ru_maxrss also counts the
peak resident set of the process that spawned it, carried across exec, so
spawning from the harness itself would report the harness's size for every
small contestant. This process holds no data and stays small; measure.py
records its floor with /bin/true every round. It runs each child in a new
session, wakes on its pidfd, and signals the group only before reaping it.
"""

import json
import os
import select
import signal
import sys
import time


def main() -> None:
    for line in sys.stdin:
        request = json.loads(line)
        actions = [
            (os.POSIX_SPAWN_OPEN, 0, "/dev/null", os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, request["stdout"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
            (os.POSIX_SPAWN_OPEN, 2, request["stderr"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
        ]
        argv = request["argv"]
        start = time.perf_counter_ns()
        pid = os.posix_spawn(argv[0], argv, request["env"], file_actions=actions, setsid=True)
        pidfd = os.pidfd_open(pid)
        ready, _, _ = select.select([pidfd], [], [], request["deadline"])
        if not ready:
            os.killpg(pid, signal.SIGKILL)  # not yet reaped: the group is still ours
        _, status, usage = os.wait4(pid, 0)
        wall = time.perf_counter_ns() - start
        os.close(pidfd)
        sys.stdout.write(json.dumps({
            "wall_ns": wall,
            "cpu_s": round(usage.ru_utime + usage.ru_stime, 6),
            "maxrss_kib": usage.ru_maxrss,
            "code": os.waitstatus_to_exitcode(status),
            "timed_out": not ready,
        }) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
