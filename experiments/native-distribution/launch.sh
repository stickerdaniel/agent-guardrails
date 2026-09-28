#!/usr/bin/env bash
# EXPERIMENTAL Gate X launcher. Runs the packaged native drive benchmark on
# one capture; it is not the required policy check and enforces nothing.
#
# The committed assets are gzip streams, so the ELF exists only here: in a
# private directory this invocation creates under RUNNER_TEMP, removed again
# before the launcher exits. The binary runs only after gzip exited 0; a
# stream gzip rejects is never executed, even when gzip already wrote part or
# all of it. The launcher never execs the binary, so its own cleanup always
# runs, and the binary's exit status is the launcher's.
#
# INT, TERM and HUP: the running child (gzip or the binary) gets TERM, since
# a background child starts with INT ignored; the launcher waits for it to
# end, removes the directory, and then dies of the signal it caught. SIGKILL
# cannot be caught: after it the directory stays until the runner clears
# RUNNER_TEMP.
#
# Reads: AGX_CAPTURE, RUNNER_OS, RUNNER_TEMP, GITHUB_ACTION_PATH. No token.
set -u
set +x
umask 077

workdir=""
child=""
caught=""
signals=0

error() {
    printf '::error title=agent-guardrails native spike::%s\n' "$1"
}

cleanup() {
    if [ -n "$workdir" ]; then
        if ! rm -rf -- "$workdir"; then
            printf '::warning title=agent-guardrails native spike::%s\n' \
                "could not remove the private temporary directory"
        fi
        workdir=""
    fi
}

signal_number() {
    case "$1" in
    HUP) echo 1 ;;
    INT) echo 2 ;;
    *) echo 15 ;;
    esac
}

# Exit with $1, after cleanup. A caught signal wins over any status: the
# launcher then dies of that signal, as an uncaught one would have killed it.
finish() {
    local status=$1
    trap - EXIT
    cleanup
    if [ -n "$caught" ]; then
        trap - "$caught"
        kill -s "$caught" "$$"
        status=$((128 + $(signal_number "$caught")))
    fi
    exit "$status"
}

fail() {
    error "$1"
    finish 1
}

# shellcheck disable=SC2329 # called from the traps below
on_signal() {
    signals=$((signals + 1))
    if [ -z "$caught" ]; then
        caught=$1
    fi
    if [ -n "$child" ]; then
        kill -s TERM "$child" 2>/dev/null || :
    fi
}

# Waits for the one running child and sets status to its exit status. A
# trapped signal ends a wait early with 128+n; the loop then waits again, so
# the directory is never removed while the child still runs.
await_child() {
    local seen
    # A signal that arrived before $child was recorded found nothing to stop.
    if [ -n "$caught" ]; then
        kill -s TERM "$child" 2>/dev/null || :
    fi
    while :; do
        seen=$signals
        wait "$child"
        status=$?
        if [ "$signals" = "$seen" ] || [ "$status" -le 128 ]; then
            break
        fi
    done
    child=""
}

trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP

capture=${AGX_CAPTURE:-}
if [ -z "$capture" ]; then
    fail "the capture input is empty"
fi
if [ ! -f "$capture" ] || [ ! -r "$capture" ]; then
    fail "the capture is not a readable regular file"
fi

if [ "${RUNNER_OS:-}" != Linux ]; then
    fail "RUNNER_OS is not Linux; this experiment supports Linux x64 and arm64 runners only"
fi
for tool in uname mktemp gzip chmod rm; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        fail "$tool is not on PATH"
    fi
done
kernel=$(uname -s) || fail "uname -s failed"
if [ "$kernel" != Linux ]; then
    fail "the kernel is not Linux; this experiment supports Linux x64 and arm64 runners only"
fi
machine=$(uname -m) || fail "uname -m failed"
case "$machine" in
x86_64) target=linux-x64 ;;
aarch64) target=linux-arm64 ;;
*)
    case "$machine" in
    *[!A-Za-z0-9_.-]* | "") machine="an unprintable value" ;;
    esac
    fail "unsupported architecture $machine; this experiment supports x86_64 and aarch64 only"
    ;;
esac

action_path=${GITHUB_ACTION_PATH:-}
case "$action_path" in
/*) ;;
*) fail "GITHUB_ACTION_PATH is not an absolute path" ;;
esac
asset="$action_path/bin/$target/agent-guardrails.gz"
if [ ! -f "$asset" ]; then
    fail "the $target asset is missing from the action"
fi

runner_temp=${RUNNER_TEMP:-}
case "$runner_temp" in
/*) ;;
*) fail "RUNNER_TEMP is not an absolute path" ;;
esac
if [ ! -d "$runner_temp" ]; then
    fail "RUNNER_TEMP is not a directory"
fi

# The directory is ours only once mktemp has created it, and only then does
# cleanup know its name. A signal before this point leaves nothing to remove,
# or at most this one empty directory if it lands inside mktemp.
created=$(mktemp -d "$runner_temp/agent-guardrails-native.XXXXXXXXXX") ||
    fail "cannot create a private temporary directory in RUNNER_TEMP"
if [ ! -d "$created" ] || [ -L "$created" ] || [ ! -O "$created" ]; then
    fail "the private temporary directory is not a directory this launcher owns"
fi
workdir=$created
trap cleanup EXIT
if [ -n "$caught" ]; then
    finish 1
fi

binary="$workdir/agent-guardrails"
set -C # the redirection below must create a new file, never reuse one
gzip -d -c -- "$asset" >"$binary" &
child=$!
await_child
set +C
if [ -n "$caught" ]; then
    finish 1
fi
if [ "$status" -ne 0 ]; then
    fail "could not decompress the $target asset into a new file (status $status); nothing was run"
fi

if ! chmod 0700 -- "$binary"; then
    fail "cannot mark the extracted binary executable"
fi
if [ -n "$caught" ]; then
    finish 1
fi
if [ ! -x "$binary" ]; then
    fail "the extracted binary cannot be executed; RUNNER_TEMP may be mounted noexec"
fi

"$binary" drive "$capture" &
child=$!
await_child
if [ -z "$caught" ] && { [ "$status" -eq 126 ] || [ "$status" -eq 127 ]; }; then
    error "the extracted binary could not be started (status $status)"
fi
finish "$status"
