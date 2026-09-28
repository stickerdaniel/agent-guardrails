#!/bin/sh
# Builds one Gate X asset inside the pinned rust image. build/reproduce.py
# starts it under `env -i` with the network disabled, so every variable the
# build sees is either passed in by name below or set here from recipe.json;
# nothing is inherited from the image or the host.
#
#   build-in-image.sh <read-only rust dir> <empty output dir>
#
# Writes agent-guardrails (the ELF), agent-guardrails.gz (its canonical gzip
# stream) and tools.txt (the versions that produced them) to the output dir.
set -eu
umask 022

input=$1
output=$2
: "${AGX_TRIPLE:?}" "${AGX_TOOLCHAIN:?}" "${AGX_RUSTC_COMMIT:?}" "${AGX_RUSTC_RELEASE:?}"
: "${AGX_EPOCH:?}" "${AGX_SOURCE_ROOT:?}" "${AGX_LINK_SELF_CONTAINED:?}"

# A fresh directory whose name differs on every build, so two builds never
# share a source, target or cargo path. Path remapping must hide it.
work=$(mktemp -d /tmp/agx-build.XXXXXXXXXX)
mkdir "$work/src" "$work/home" "$work/cargo-home"
cp -R "$input/Cargo.toml" "$input/Cargo.lock" "$input/src" "$work/src/"

export PATH="$AGX_TOOLCHAIN/bin:/usr/bin:/bin"
export RUSTC="$AGX_TOOLCHAIN/bin/rustc"
export HOME="$work/home"
export CARGO_HOME="$work/cargo-home"
export CARGO_TARGET_DIR="$work/target"
export CARGO_INCREMENTAL=0
export TZ=UTC LC_ALL=C LANG=C
export SOURCE_DATE_EPOCH="$AGX_EPOCH"
export RUSTFLAGS="--remap-path-prefix=$work/src=$AGX_SOURCE_ROOT --remap-path-prefix=$work/cargo-home=/cargo-home -C target-feature=+crt-static -C linker=cc -C link-self-contained=$AGX_LINK_SELF_CONTAINED"

# The compiler must be exactly the pinned one, not whatever answers to rustc.
version=$("$RUSTC" -vV)
case "$version" in
*"commit-hash: $AGX_RUSTC_COMMIT"*"release: $AGX_RUSTC_RELEASE"*) ;;
*)
    echo "build-in-image: unexpected rustc: $version" >&2
    exit 1
    ;;
esac

{
    echo "work-dir-name: $(basename "$work")"
    "$RUSTC" -vV
    cargo -V
    cc --version | head -n 1
    ld --version | head -n 1
    busybox 2>&1 | head -n 1
    echo "RUSTFLAGS (work path elided): -C target-feature=+crt-static -C linker=cc -C link-self-contained=$AGX_LINK_SELF_CONTAINED"
} > "$output/tools.txt"

cd "$work/src"
cargo build --frozen --offline --release --target "$AGX_TRIPLE"

elf="$CARGO_TARGET_DIR/$AGX_TRIPLE/release/agent-guardrails"
cp "$elf" "$output/agent-guardrails"
# BusyBox gzip from the same image; stdin carries no name or timestamp, so
# the header is 1f8b 08 00 00000000 02 03.
gzip -9 -c < "$elf" > "$output/agent-guardrails.gz"
sha256sum "$output/agent-guardrails" "$output/agent-guardrails.gz" | sed "s#$output/##"
rm -rf "$work"
