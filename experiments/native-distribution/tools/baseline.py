"""The frozen Python baseline every Gate X comparison runs against.

The baseline is the agent_guardrails package at a fixed commit, never the
checkout's own copy and never a moving branch. Callers extract it themselves
(a second checkout at BASELINE_COMMIT, or git archive of that commit) and
pass the directory; this module refuses any directory whose package files do
not hash to the pinned values.
"""

from __future__ import annotations

import hashlib
import os
import sys

BASELINE_COMMIT = "40382e2107a899e428cad2ab6b1d3132c90d3dcf"

# agent_guardrails/*.py at BASELINE_COMMIT, recorded when the benchmark
# prototype froze its archive (sha256 f7276cb2...d74a).
PACKAGE_SHA256 = {
    "__init__.py": "5297d400b3df72bf98f9823c6474110ad9ca87245d26f5bfbb7e20a231891295",
    "attribution.py": "eada4510eac1e10faeb1e9a9e08d4cee5a0145856f9256c0a0f2beb952255b18",
    "event.py": "721e5d82e4563b14cf81ae7786a840665eb3e1e10134189be73201edb9df298b",
    "gitdata.py": "07de3ad53ca4d0ea95c46a33996bee3d186449372331ce6f324e667f4c36b240",
    "hidden.py": "83666402bc31c246da507a9424f668d7dc831dee4d45e33f4bdf099c211a97cc",
    "main.py": "dd32944d3d659d07cafd2a3f81cc183341c03ebc3c575650ae95b7670b2c98bc",
    "report.py": "9b04a7b2c80f7cac3ab1b9d1912edef7fb59ba1042c21427e000cb971ee6c922",
    "rules.py": "6c68494bfb24ea0d490996435121b57bd1ee5821d92d2f9d85d2e371057e7293",
}


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(root: str) -> str:
    """The absolute baseline root, after proving its package is the pinned
    one: every pinned file with its hash, and no other module beside them."""
    root = os.path.abspath(root)
    package = os.path.join(root, "agent_guardrails")
    for name, expected in PACKAGE_SHA256.items():
        path = os.path.join(package, name)
        if not os.path.isfile(path) or sha256_file(path) != expected:
            raise SystemExit(f"baseline {name} is not the one at {BASELINE_COMMIT}")
    extra = sorted(
        name for name in os.listdir(package)
        if name.endswith(".py") and name not in PACKAGE_SHA256
    )
    if extra:
        raise SystemExit(f"baseline package has unexpected modules: {extra}")
    return root


def import_modules(root: str):
    """(hidden, rules, report, gitdata) from the verified baseline only."""
    root = verify(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    from agent_guardrails import gitdata, hidden, report, rules  # noqa: PLC0415

    imported = os.path.dirname(os.path.dirname(os.path.abspath(hidden.__file__)))
    if imported != root:
        raise SystemExit("imported a baseline other than the verified one")
    return hidden, rules, report, gitdata
