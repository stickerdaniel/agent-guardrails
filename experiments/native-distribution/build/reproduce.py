"""Build one Gate X target twice from scratch and compare every byte.

    python3 build/reproduce.py --arch x64 --work DIR [--record FILE] [--write-asset]

Each build gets its own unrelated host directory and its own container paths,
runs the pinned image by digest with the network disabled, and produces the
ELF and its gzip stream (build/build-in-image.sh). The two builds must be
byte-identical, both artifacts must pass tools/verify_artifacts.py, and the
committed asset must equal the fresh gzip byte for byte and decompress to the
fresh ELF byte for byte. --write-asset replaces the committed asset with the
agreed build instead of comparing, for preparing a commit locally.

Exit status 1 on any difference or failure; nothing is skipped. The image is
pulled by digest before the network-less builds when it is not present.

A build runs in a container this invocation owns (run_owned): created with a
fresh name and label, started by the ID docker create returned, and removed
by that ID once it ends, fails or overruns its deadline. A deadline kills the
Docker client, never by itself the container, so removal is what ends a
stuck build. Nothing is removed by name or by a broad filter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import verify_artifacts  # noqa: E402

BUILD_DEADLINE_S = 1800  # an emulated build is slow; a stuck one is removed here
CREATE_DEADLINE_S = 120
CLEANUP_DEADLINE_S = 60  # for each docker call cleanup makes
OWNER_LABEL = "io.github.stickerdaniel.agx-reproduce"
DOCKER = "docker"
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


class OwnedRunError(Exception):
    """A run in an owned container failed. problems lists the failure first,
    then anything cleanup could not do; container is the ID, if one was
    created."""

    def __init__(self, problems: list[str], container: str | None) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems
        self.container = container


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def docker(*args: str, deadline: float) -> subprocess.CompletedProcess:
    """One Docker client call. On timeout, subprocess.run kills and reaps the
    client; whatever the daemon runs for it is the caller's to remove."""
    return subprocess.run([DOCKER, *args], capture_output=True, text=True, timeout=deadline)


def _bounded(problems: list[str], what: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return docker(*args, deadline=CLEANUP_DEADLINE_S)
    except subprocess.TimeoutExpired:
        problems.append(f"{what} did not finish within {CLEANUP_DEADLINE_S}s")
        return None


def remove_owned(token: str, name: str, container: str | None) -> list[str]:
    """Removes the containers this token owns and returns what went wrong.

    The ID docker create returned, plus any container carrying exactly this
    token's label (a create that timed out may still have made one). Each is
    removed only after its name and label prove it ours, by ID, and then
    checked to be gone."""
    problems: list[str] = []
    owned = [container] if container else []
    listed = _bounded(
        problems, "listing this token's containers",
        "ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={OWNER_LABEL}={token}",
    )
    if listed is not None and listed.returncode == 0:
        owned += [line for line in listed.stdout.split() if line not in owned]
    elif listed is not None:
        problems.append(f"cannot list this token's containers: {listed.stderr.strip()}")
    for candidate in owned:
        if not _CONTAINER_ID.fullmatch(candidate):
            problems.append(f"not removing {candidate!r}: not a full container ID")
            continue
        shown = _bounded(
            problems, f"inspecting {candidate}",
            "inspect", "--type", "container", "--format",
            f'{{{{.Name}}}}\t{{{{index .Config.Labels "{OWNER_LABEL}"}}}}', candidate,
        )
        if shown is None:
            continue
        if shown.returncode != 0:
            problems.append(f"cannot inspect {candidate}: {shown.stderr.strip()}")
            continue
        if shown.stdout.strip() != f"/{name}\t{token}":
            problems.append(f"not removing {candidate}: its name and label are not this invocation's")
            continue
        removed = _bounded(problems, f"removing {candidate}", "rm", "--force", candidate)
        if removed is not None and removed.returncode != 0:
            problems.append(f"cannot remove {candidate}: {removed.stderr.strip()}")
        still = _bounded(problems, f"checking that {candidate} is gone", "inspect", "--type", "container", candidate)
        if still is not None and still.returncode == 0:
            problems.append(f"{candidate} still exists after removal")
    return problems


def run_owned(
    options: list[str], image: str, argv: list[str], deadline: float, token: str | None = None
) -> subprocess.CompletedProcess:
    """argv in a new container of image, which this call creates, waits for
    at most deadline seconds and always removes. Returns the attached
    `docker start` result, whose status is the container's. Raises
    OwnedRunError when the run could not complete or cleanup failed."""
    token = token or secrets.token_hex(12)
    name = f"agx-reproduce-{token}"
    container = None
    problems: list[str] = []
    result = None
    try:
        created = docker(
            "create", "--name", name, "--label", f"{OWNER_LABEL}={token}", *options, image, *argv,
            deadline=CREATE_DEADLINE_S,
        )
        if created.returncode != 0:
            problems.append(f"docker create failed: {created.stderr.strip()}")
        elif not _CONTAINER_ID.fullmatch(created.stdout.strip()):
            problems.append(f"docker create printed no container ID: {created.stdout.strip()!r}")
        else:
            container = created.stdout.strip()
            result = docker("start", "--attach", container, deadline=deadline)
    except subprocess.TimeoutExpired as expired:
        what = "docker create" if container is None else "the container"
        problems.append(f"{what} did not finish within {expired.timeout:g}s")
    finally:
        problems += remove_owned(token, name, container)
    if problems:
        raise OwnedRunError(problems, container)
    return result


def ensure_image(image: str, platform: str) -> str:
    """The image's own content digest, pulling it by digest when absent."""
    present = docker("image", "inspect", "--format", "{{.Id}}", image, deadline=60)
    if present.returncode != 0:
        pulled = docker("pull", "--platform", platform, image, deadline=900)
        if pulled.returncode != 0:
            raise SystemExit(f"reproduce: cannot pull {image}: {pulled.stderr.strip()}")
        present = docker("image", "inspect", "--format", "{{.Id}}", image, deadline=60)
    return present.stdout.strip()


def build(recipe: dict, target: dict, work_root: str) -> dict:
    """One build in a fresh, unrelated directory. Returns its record."""
    base = tempfile.mkdtemp(prefix=f"agx-{secrets.token_hex(6)}-", dir=work_root)
    source = os.path.join(base, f"in-{secrets.token_hex(4)}")
    output = os.path.join(base, f"out-{secrets.token_hex(4)}")
    os.makedirs(os.path.join(source, "src"))
    os.makedirs(output)
    for relative in recipe["source"]:
        shutil.copyfile(os.path.join(ROOT, relative), os.path.join(source, relative.split("/", 1)[1]))
    mount_in = f"/agx-in-{secrets.token_hex(6)}"
    mount_out = f"/agx-out-{secrets.token_hex(6)}"
    settings = recipe["build"]
    environment = {
        "AGX_TRIPLE": target["triple"],
        "AGX_TOOLCHAIN": target["toolchain_dir"],
        "AGX_RUSTC_COMMIT": recipe["toolchain"]["rustc_commit"],
        "AGX_RUSTC_RELEASE": recipe["toolchain"]["rustc_release"],
        "AGX_EPOCH": settings["source_date_epoch"],
        "AGX_SOURCE_ROOT": settings["source_root"],
        "AGX_LINK_SELF_CONTAINED": settings["link_self_contained"],
    }
    options = [
        "--network", "none", "--platform", target["platform"],
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{source}:{mount_in}:ro",
        "-v", f"{output}:{mount_out}",
        "-v", f"{os.path.join(HERE, 'build-in-image.sh')}:/agx-recipe/build-in-image.sh:ro",
    ]
    argv = [
        "env", "-i", *(f"{name}={value}" for name, value in environment.items()),
        "/bin/sh", "/agx-recipe/build-in-image.sh", mount_in, mount_out,
    ]
    started = time.monotonic()
    try:
        result = run_owned(options, target["image"], argv, BUILD_DEADLINE_S)
    except OwnedRunError as error:
        raise SystemExit(f"reproduce: build in {base} failed: {error}") from None
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        raise SystemExit(f"reproduce: build in {base} failed:\n{result.stdout}\n{result.stderr}")
    with open(os.path.join(output, "agent-guardrails"), "rb") as handle:
        elf = handle.read()
    with open(os.path.join(output, "agent-guardrails.gz"), "rb") as handle:
        gz = handle.read()
    with open(os.path.join(output, "tools.txt"), encoding="utf-8") as handle:
        tools = handle.read()
    return {
        "host_dir": base,
        "container_input": mount_in,
        "container_output": mount_out,
        "seconds": round(elapsed, 1),
        "elf_bytes": len(elf),
        "elf_sha256": sha256(elf),
        "gz_bytes": len(gz),
        "gz_sha256": sha256(gz),
        "tools": tools,
        "_elf": elf,
        "_gz": gz,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--arch", required=True, choices=["x64", "arm64"])
    parser.add_argument("--work", required=True, help="an existing directory for the build trees")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--record", help="write the comparison record here as JSON")
    parser.add_argument("--write-asset", action="store_true", help="store the agreed build as the asset")
    args = parser.parse_args(argv)
    if args.runs < 2:
        parser.error("reproducibility needs at least two independent builds")

    with open(os.path.join(HERE, "recipe.json"), encoding="utf-8") as handle:
        recipe = json.load(handle)
    target = recipe["targets"][args.arch]
    image_id = ensure_image(target["image"], target["platform"])
    work_root = os.path.abspath(args.work)
    builds = [build(recipe, target, work_root) for _ in range(args.runs)]

    problems = []
    first = builds[0]
    for other in builds[1:]:
        if other["_elf"] != first["_elf"]:
            problems.append(f"ELF of {other['host_dir']} differs from {first['host_dir']}")
        if other["_gz"] != first["_gz"]:
            problems.append(f"gzip of {other['host_dir']} differs from {first['host_dir']}")
    for record in builds:
        try:
            verify_artifacts.verify(record["_gz"], args.arch, record["_elf"])
        except verify_artifacts.ArtifactError as error:
            problems.append(f"{record['host_dir']}: {error}")

    asset_path = os.path.join(ROOT, target["asset"])
    committed = {}
    if args.write_asset and not problems:
        os.makedirs(os.path.dirname(asset_path), exist_ok=True)
        with open(asset_path, "wb") as handle:
            handle.write(first["_gz"])
    if os.path.exists(asset_path):
        with open(asset_path, "rb") as handle:
            asset = handle.read()
        committed = {"path": target["asset"], "bytes": len(asset), "sha256": sha256(asset)}
        try:
            verify_artifacts.verify(asset, args.arch, first["_elf"], first["_gz"])
            committed["equals_fresh_builds"] = True
        except verify_artifacts.ArtifactError as error:
            committed["equals_fresh_builds"] = False
            problems.append(f"committed {target['asset']}: {error}")
    else:
        problems.append(f"committed {target['asset']} is missing")

    record = {
        "arch": args.arch,
        "platform": target["platform"],
        "image": target["image"],
        "image_id": image_id,
        "triple": target["triple"],
        "host_uname_m": os.uname().machine,
        "builds": [{k: v for k, v in b.items() if not k.startswith("_")} for b in builds],
        "builds_identical": not any("differs from" in p for p in problems),
        "committed": committed,
        "problems": problems,
    }
    text = json.dumps(record, indent=2)
    if args.record:
        with open(args.record, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
