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
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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

BUILD_DEADLINE_S = 1800  # an emulated build is slow; a stuck one still ends


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def docker(*args: str, deadline: float) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=deadline)


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
    command = [
        "run", "--rm", "--network", "none", "--platform", target["platform"],
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{source}:{mount_in}:ro",
        "-v", f"{output}:{mount_out}",
        "-v", f"{os.path.join(HERE, 'build-in-image.sh')}:/agx-recipe/build-in-image.sh:ro",
        target["image"],
        "env", "-i", *(f"{name}={value}" for name, value in environment.items()),
        "/bin/sh", "/agx-recipe/build-in-image.sh", mount_in, mount_out,
    ]
    started = time.monotonic()
    result = docker(*command, deadline=BUILD_DEADLINE_S)
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
