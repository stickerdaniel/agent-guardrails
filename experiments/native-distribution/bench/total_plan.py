"""The job plan of the Gate X total-cost refinement, derived from
bench/total-contract.json alone.

Per architecture one chain of fourteen jobs: pair 1's first treatment, then
its second, then pair 2's first, and so on, each job needing the one before.
Which treatment goes first in each pair is fixed in the contract's "first"
table, which first_order() derives from the contract's seed. The workflow
generator, the job worker and the analyzer all read the plan from here, so
the jobs that ran, the jobs that were declared and the jobs that are judged
are the same list.
"""

from __future__ import annotations

import hashlib
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACT = os.path.join(HERE, "total-contract.json")
ARCHES = ("x64", "arm64")
TREATMENTS = ("candidate", "control")


def load(path: str = CONTRACT) -> tuple[dict, str]:
    """The contract and the sha256 of its exact bytes."""
    with open(path, "rb") as handle:
        data = handle.read()
    return json.loads(data), hashlib.sha256(data).hexdigest()


def manifest_sha256(manifest: dict) -> str:
    """The digest of a {path: sha256} file manifest, independent of order."""
    text = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def first_order(seed: int, pairs: int) -> dict[str, list[str]]:
    """Which treatment runs first in each pair, balanced as far as an odd
    count allows: one architecture has candidate first in (pairs + 1) // 2
    pairs, the other in pairs // 2, so both treatments go first equally
    often overall. The seed picks which architecture gets which, then
    shuffles each architecture's pairs."""
    rng = random.Random(seed)
    more = rng.choice(TREATMENTS)
    less = TREATMENTS[1 - TREATMENTS.index(more)]
    order = {
        "x64": [more] * ((pairs + 1) // 2) + [less] * (pairs // 2),
        "arm64": [less] * ((pairs + 1) // 2) + [more] * (pairs // 2),
    }
    for arch in ARCHES:
        rng.shuffle(order[arch])
    return order


def job_id(arch: str, pair: int, treatment: str) -> str:
    return f"{arch}-pair{pair}-{treatment}"


def jobs(contract: dict) -> list[dict]:
    """Every declared job, x64's chain first, each in the order it runs."""
    out = []
    for arch in ARCHES:
        previous = None
        for pair, first in enumerate(contract["first"][arch], start=1):
            second = TREATMENTS[1 - TREATMENTS.index(first)]
            for position, treatment in ((1, first), (2, second)):
                ident = job_id(arch, pair, treatment)
                out.append({
                    "id": ident,
                    "name": f"total ({arch}, pair {pair}, {'1st' if position == 1 else '2nd'}, {treatment})",
                    "arch": arch,
                    "runner": contract["runners"][arch],
                    "machine": contract["machines"][arch],
                    "pair": pair,
                    "position": position,
                    "treatment": treatment,
                    "snapshot": contract["treatments"][treatment]["snapshot"],
                    "needs": previous,
                })
                previous = ident
    return out
