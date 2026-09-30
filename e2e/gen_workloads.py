"""Write the four frozen workloads of the E2E-3 CI cost study.

    python3 e2e/gen_workloads.py OUT_DIR
    python3 e2e/gen_workloads.py --check [OUT_DIR]

OUT_DIR receives plain files and manifest.json: per workload the PR title and
body, and per commit its message, identities, fixed dates, and the full
content of every file it adds or changes. The manifest also names the git
blob, tree and commit SHAs each commit must get, the counts the study's
limits are measured against, and the output the published action prints for
the pull request, byte for byte. --check regenerates, into OUT_DIR or a
temporary directory, and compares every file with e2e/workloads.lock.json.
The corpora themselves are never committed, only that lock.

The expected output comes from agent_guardrails in this checkout, the source
of the published v1.0.0, run in-process with git replaced by the commits and
files below. build_fixtures.py --verify runs the same checks against real git
and compares. Stdlib only; deterministic for one seed on every supported
Python.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_guardrails import gitdata, main, rules  # noqa: E402
from agent_guardrails.report import Reporter  # noqa: E402

LOCK = Path(__file__).resolve().parent / "workloads.lock.json"
SEED = 20260929
VERSION = 1

# The fixture base: one commit on the real history at 3937a2d that removes
# .github/workflows and nothing else. These are 3937a2d's own entries; the
# builder checks the tree git makes from them against this prediction.
BASE_PARENT = "3937a2dc711ea26a856b2b75536cd01c80ac86b7"
BASE_BRANCH = "e2e/workload-base"
_BASE_GITHUB = (
    ("40000", "ISSUE_TEMPLATE", "e05d0a8dff7e076a0bba8f26ddf32452ad7aaf5a"),
    ("100644", "pull_request_template.md", "7305f7bd4e56602b6e7e189c580f69665581987a"),
    ("100644", "release.py", "c6867db6966a193526cb2971d26440f949819f9d"),
)
_BASE_ROOT = (
    ("100644", ".gitignore", "7a60b85e148f80966a550e5ab6a762a907c69ca6"),
    ("100644", "CHANGELOG.md", "47e4ae85d35c16c31493900cd71e20b59ac63a20"),
    ("100644", "CODE_OF_CONDUCT.md", "af7f5a65f16b5fbe186392292693ca3b403d9069"),
    ("100644", "CONTRIBUTING.md", "0492d176a4cb16285d1a69781af24ddc66f372a7"),
    ("100644", "LICENSE", "057b3d0a661a063b2ac4e4f74b29c09db9ece89f"),
    ("100644", "README.md", "c03cddffe686040b3423732c9918811fb6344496"),
    ("100644", "SECURITY.md", "f0af9a136c9e3a8984c6d6fdf752baa196d0550f"),
    ("100644", "THIRD_PARTY_NOTICES.md", "fffd4dd20559e916c4822605d44bf70442754225"),
    ("100644", "action.yml", "6869a72d0bbb29196881b4eaa8293197017b1e2a"),
    ("40000", "agent_guardrails", "2187872582e42eec8604f2713c1c8eda5f3ba74e"),
    ("100644", "renovate.json", "4617b57b08d3a29443ee0fe55263e97f15737060"),
    ("100644", "run.py", "16640d6230c5da7e35cbabe1e027c3a4ca315ee0"),
    ("40000", "tests", "10e0ca34d462076a4e55ecb00a7623e64377ee09"),
)
_BASE_MESSAGE = (
    "test(e2e): Freeze the workload base\n"
    "\n"
    "Removes .github/workflows and nothing else, so the fixture pull requests\n"
    "of the CI cost study start no ordinary workflow. Never merged.\n"
)
# 2026-09-29T00:00:00Z. Every fixture commit has fixed dates after it.
BASE_EPOCH = 1790640000
IDENTITY = ("Agent Guardrails E2E", "e2e-fixture@example.invalid")

# One fixed policy for all three variants. The design sets
# require-model-attribution: true and leaves hidden-unicode at its default.
INPUTS = {"require-model-attribution": "true", "hidden-unicode": "error"}
# Stands in for the runner's git version line, which is not part of the
# frozen output.
_GIT_VERSION = "git version <runner>"
_TOKEN = "ghs_E2E0000000000000000000000000000000000"
LOCKFILE_BYTES = 3_800_000
MARKDOWN_BYTES = 2 * 2**20
MARKDOWN_FILES = 16
CJK_BYTES = 512 * 2**10


class _Stream:
    """Uniform choices from SHA-256 of a counter, the same on every Python."""

    def __init__(self, label: str) -> None:
        self._label = label
        self._counter = 0
        self._pool = b""

    def below(self, bound: int) -> int:
        limit = (1 << 32) // bound * bound
        while True:
            if len(self._pool) < 4:
                key = f"E2E3-workload-v{VERSION}|{SEED}|{self._label}|{self._counter}"
                self._pool += hashlib.sha256(key.encode("ascii")).digest()
                self._counter += 1
            value = int.from_bytes(self._pool[:4], "big")
            self._pool = self._pool[4:]
            if value < limit:
                return value % bound

    def choice(self, items):
        return items[self.below(len(items))]

    def between(self, low: int, high: int) -> int:
        return low + self.below(high - low + 1)


# Phrase banks. Each phrase is in one script; phrases are joined by spaces
# or punctuation, so no word mixes scripts.
_ENGLISH = (
    "the review found nothing to change", "every commit is checked once",
    "this section describes the release process", "run the tests before you push",
    "the cache is rebuilt on every change", "keep the example short and exact",
    "configuration lives next to the code", "the parser reads one line at a time",
    "errors are reported with their line numbers", "the summary lists each changed file",
)
_ACCENTED = (
    "Les résultats sont vérifiés à chaque étape", "la société a déjà réécrit le système",
    "où se trouve la dernière révision", "une fenêtre légère et élégante",
    "Die Größe der Übersicht wurde geprüft", "Änderungen für Straßen und Brücken",
    "schöne Grüße aus München", "La configuración está lista para mañana",
    "el pingüino añade información útil", "revisión rápida del código",
    "A informação foi atualizada com atenção", "Zażółć gęślą jaźń",
    "Příliš žluťoučký kůň úpěl ďábelské ódy", "Tiếng Việt có nhiều dấu thanh",
    "Çalışma günü başarıyla tamamlandı", "Þetta er íslenska með ð og þ",
)
_FRENCH = (
    "Les résultats sont vérifiés à chaque étape", "la société a déjà réécrit le système",
    "où se trouve la dernière révision", "une fenêtre légère et élégante",
    "le garçon reçoit un reçu détaillé", "Noël approche à grands pas",
    "chaque modèle est décrit en détail", "la mise à jour préserve les données",
)
_GERMAN = (
    "Die Größe der Übersicht wurde geprüft", "Änderungen für Straßen und Brücken",
    "schöne Grüße aus München", "über die Fußgängerzone hinaus",
    "die Prüfung läuft täglich um fünf Uhr", "Schlüssel werden nie im Klartext gespeichert",
)
_SPANISH = (
    "La configuración está lista para mañana", "el pingüino añade información útil",
    "revisión rápida del código", "la canción suena también en el camión",
    "A informação foi atualizada com atenção", "a ação começa às nove horas",
)
_FOREIGN = (
    "Проверка документации завершена", "быстрый обзор изменений",
    "Сборка прошла успешно", "Українська мова: перевірка змін",
    "Η τεκμηρίωση ενημερώθηκε σήμερα", "γρήγορη επισκόπηση αλλαγών",
    "تم تحديث الوثائق اليوم", "مراجعة سريعة للتغييرات",
    "התיעוד עודכן היום", "סקירה מהירה של השינויים",
    "दस्तावेज़ आज अपडेट किया गया", "परिवर्तनों की त्वरित समीक्षा",
    "เอกสารได้รับการปรับปรุงแล้ว", "დოკუმენტაცია განახლდა",
    "文档已更新，构建通过了所有检查", "ドキュメントを更新しました", "문서가 업데이트되었습니다",
    "繁體中文的說明文件", "レビューをお願いします", "변경 사항을 확인했습니다",
)
_CJK = (
    "文档已更新", "构建通过了所有检查", "请在合并之前再运行一次测试", "每个提交只检查一次",
    "ドキュメントを更新しました", "レビューをお願いします", "変更点は次のとおりです",
    "문서가 업데이트되었습니다", "변경 사항을 확인했습니다", "테스트를 다시 실행하세요",
    "繁體中文的說明文件", "設定檔與程式碼放在一起", "錯誤會連同行號一起回報",
)
_IDENTIFIERS = ("load_config", "check_body", "render_table", "parse_line", "build_index", "write_report")


@dataclass
class _Commit:
    message: str
    files: dict[str, bytes]
    hour: int
    tree: str = ""
    sha: str = ""
    parent: str = ""


@dataclass
class _Workload:
    key: str
    name: str
    title: str
    body: str | None
    commits: list[_Commit]
    exit_code: int
    extra: dict = field(default_factory=dict)


def _git_object(kind: str, data: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(data)}\0".encode("ascii") + data).hexdigest()


def git_tree(entries) -> str:
    """The SHA of a tree object from (mode, name, sha) entries, in git's
    order: a directory sorts as its name followed by "/"."""

    def order(entry):
        mode, name, _ = entry
        return name.encode("utf-8") + (b"/" if mode == "40000" else b"")

    data = b"".join(
        mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(sha)
        for mode, name, sha in sorted(entries, key=order)
    )
    return _git_object("tree", data)


def git_commit(tree: str, parent: str, epoch: int, message: str) -> str:
    """The SHA of a commit that git commit-tree makes with the fixed identity,
    UTC dates and the message verbatim on stdin."""
    name, email = IDENTITY
    person = f"{name} <{email}> {epoch} +0000"
    data = f"tree {tree}\nparent {parent}\nauthor {person}\ncommitter {person}\n\n{message}"
    return _git_object("commit", data.encode("utf-8"))


def _nested_tree(files: dict[str, bytes]) -> list[tuple[str, str, str]]:
    """Root entries of the tree that holds files, a mapping of path to bytes."""
    directories: dict[str, dict[str, bytes]] = {}
    entries = []
    for path, data in files.items():
        head, _, rest = path.partition("/")
        if rest:
            directories.setdefault(head, {})[rest] = data
        else:
            entries.append(("100644", head, _git_object("blob", data)))
    for name, content in directories.items():
        entries.append(("40000", name, git_tree(_nested_tree(content))))
    return entries


def base_tree() -> str:
    github = git_tree(_BASE_GITHUB)
    return git_tree((*_BASE_ROOT, ("40000", ".github", github)))


def base_commit() -> str:
    return git_commit(base_tree(), BASE_PARENT, BASE_EPOCH, _BASE_MESSAGE)


def _head_tree(files: dict[str, bytes]) -> str:
    github = git_tree(_BASE_GITHUB)
    return git_tree((*_BASE_ROOT, ("40000", ".github", github), *_nested_tree(files)))


# --- Content ---------------------------------------------------------------


def _fill(lines: list[str], target: int, filler: str) -> bytes:
    """lines joined, plus one last line of filler characters that makes the
    file exactly target bytes. filler is ASCII."""
    data = "".join(f"{line}\n" for line in lines).encode("utf-8")
    room = target - len(data)
    if room < 1:
        raise ValueError("content longer than its target size")
    return data + (filler * (room - 1)).encode("ascii") + b"\n"


def _lines_until(make_line, target: int, reserve: int = 64) -> list[str]:
    lines: list[str] = []
    size = 0
    while True:
        line = make_line()
        cost = len(line.encode("utf-8")) + 1
        if size + cost > target - reserve:
            return lines
        lines.append(line)
        size += cost


def _markdown_line(stream: _Stream, latin, foreign) -> str:
    kind = stream.below(8)
    if kind == 0:
        return f"## {stream.choice(latin)} / {stream.choice(foreign)}"
    if kind == 1:
        return f"- {stream.choice(latin)}: {stream.choice(foreign)}"
    if kind == 2:
        return f"| {stream.choice(latin)} | {stream.choice(foreign)} | {stream.below(1000)} |"
    if kind == 3:
        return f"`{stream.choice(_IDENTIFIERS)}()` {stream.choice(foreign)}; {stream.choice(latin)}."
    if kind == 4:
        return ""
    parts = [stream.choice(latin if stream.below(2) else foreign) for _ in range(stream.between(2, 5))]
    return ". ".join(parts) + "."


def _prose(stream: _Stream, count: int, heading: str, bank) -> list[str]:
    """count lines of Markdown in the phrases of one bank and English."""
    lines = [f"# {heading}", ""]
    while len(lines) < count:
        kind = stream.below(6)
        if kind == 0:
            lines.append(f"## {stream.choice(bank)}")
        elif kind == 1:
            lines.append(f"- {stream.choice(bank)}; {stream.choice(_ENGLISH)}.")
        elif kind == 2:
            lines.append("")
        else:
            lines.append(". ".join(stream.choice(bank) for _ in range(stream.between(1, 3))) + ".")
    return lines[:count]


def _module(stream: _Stream, name: str, count: int) -> list[str]:
    """count lines of an ASCII Python module."""
    head = [
        f'"""{name}: generated module of the CI cost study workload."""',
        "",
        "from __future__ import annotations",
        "",
        "VALUES = (",
    ]
    tail = [
        ")",
        "",
        "",
        "def total(scale: int) -> int:",
        '    """The sum of VALUES, each multiplied by scale."""',
        "    return sum(value * scale for value in VALUES)",
    ]
    body = [f"    0x{stream.below(1 << 32):08x}," for _ in range(count - len(head) - len(tail))]
    return head + body + tail


def _config(stream: _Stream, count: int) -> list[str]:
    """count lines of an ASCII JSON document."""
    entries = [f'  "option_{index:02d}": {stream.below(10_000)},' for index in range(count - 3)]
    return ["{", *entries, '  "version": 1', "}"]


def _changelog(stream: _Stream, count: int) -> list[str]:
    lines = ["# Changelog", ""]
    version = 0
    while len(lines) < count:
        if stream.below(5) == 0:
            version += 1
            lines += ["", f"## 0.{version}.0"]
        else:
            lines.append(f"- {stream.choice(_ACCENTED)}.")
    return lines[:count]


def _lockfile(stream: _Stream) -> bytes:
    """One JSON line of exactly LOCKFILE_BYTES bytes, the final LF included."""
    head = '{"name":"e2e-heavy","lockfileVersion":3,"requires":true,"packages":{'
    tail = "}}\n"
    parts = [head]
    size = len(head) + len(tail)
    index = 0
    while True:
        digest = hashlib.sha512(f"E2E3-lock|{SEED}|{index}".encode("ascii")).digest()
        name = f"pkg-{index:05d}"
        entry = (
            f'"node_modules/{name}":{{"version":"{stream.between(0, 9)}.{stream.between(0, 40)}.'
            f'{stream.between(0, 99)}","resolved":"https://registry.npmjs.org/{name}/-/{name}.tgz",'
            f'"integrity":"sha512-{base64.b64encode(digest).decode("ascii")}","license":"MIT"}},'
        )
        if size + len(entry) > LOCKFILE_BYTES - 128:
            break
        parts.append(entry)
        size += len(entry)
        index += 1
    pad = '"node_modules/zz-pad":{"version":"0.0.0","description":"'
    close = '"}'
    parts.append(pad + "x" * (LOCKFILE_BYTES - size - len(pad) - len(close)) + close)
    data = ("".join(parts) + tail).encode("ascii")
    assert len(data) == LOCKFILE_BYTES and data.count(b"\n") == 1
    return data


def _cjk(stream: _Stream) -> bytes:
    def line() -> str:
        parts = [stream.choice(_CJK) for _ in range(stream.between(2, 6))]
        return f"{stream.below(10_000)}：" + "。".join(parts) + "。"

    return _fill(_lines_until(line, CJK_BYTES), CJK_BYTES, "-")


def _text(lines: list[str]) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def _w3() -> _Workload:
    lines = [
        f"Line {number:02d}: an idle workload for the CI cost study, plain ASCII only."
        for number in range(1, 41)
    ]
    return _Workload(
        key="W3",
        name="idle",
        title="E2E fixture W3: idle workload",
        body=(
            "Idle workload for the CI cost study: one commit that adds one 40-line ASCII file.\n"
            "\n"
            "Generated with Claude Opus 5"
        ),
        commits=[
            _Commit("test(e2e): Add the idle workload file\n", {"workload/w3/idle.txt": _text(lines)}, 21),
        ],
        exit_code=0,
    )


def _w1() -> _Workload:
    stream = _Stream("W1")
    french = _prose(stream, 140, "Guide de démarrage", _FRENCH)
    german = _prose(stream, 120, "Einführung", _GERMAN)
    overview = _prose(stream, 100, "Overview and configuración", _SPANISH)
    module = _module(stream, "report", 110)
    config = _config(stream, 30)
    changelog = _changelog(stream, 100)
    return _Workload(
        key="W1",
        name="medium controlled",
        title="E2E fixture W1: medium controlled workload",
        body=(
            "## Summary\n"
            "\n"
            "Adds the medium controlled workload: guides in French and German, an overview "
            "with Spanish and Portuguese examples, a small module, its configuration and a "
            "changelog.\n"
            "\n"
            "- Vérifié : les accents restent lisibles.\n"
            "- Geprüft: Umlaute wie ä, ö und ü.\n"
            "- Revisado: la configuración es rápida.\n"
            "\n"
            "Generated with Claude Opus 5 for implementation in Claude Code."
        ),
        commits=[
            _Commit(
                "docs(e2e): Add the French and German guides\n",
                {
                    "workload/w1/docs/guide.fr.md": _text(french[:100]),
                    "workload/w1/docs/guide.de.md": _text(german),
                },
                1,
            ),
            _Commit(
                "feat(e2e): Add the report module and its configuration\n"
                "\n"
                "The overview explains the options in English, Spanish and Portuguese.\n",
                {
                    "workload/w1/docs/overview.md": _text(overview),
                    "workload/w1/src/report.py": _text(module),
                    "workload/w1/src/config.json": _text(config),
                },
                2,
            ),
            _Commit(
                "docs(e2e): Finish the guide and add a changelog (première version)\n",
                {
                    "workload/w1/docs/guide.fr.md": _text(french),
                    "workload/w1/CHANGELOG.md": _text(changelog),
                },
                3,
            ),
        ],
        exit_code=0,
    )


def _w2() -> _Workload:
    stream = _Stream("W2")
    lock = _lockfile(stream)
    per_file = MARKDOWN_BYTES // MARKDOWN_FILES
    docs = {
        f"workload/w2/docs/part-{index:02d}.md": _fill(
            _lines_until(lambda: _markdown_line(stream, _ENGLISH + _ACCENTED, _FOREIGN), per_file),
            per_file,
            "-",
        )
        for index in range(1, MARKDOWN_FILES + 1)
    }
    cjk = _cjk(stream)
    modules = {
        f"workload/w2/src/module_{index:02d}.py": _text(
            _module(stream, f"module_{index:02d}", stream.between(120, 260))
        )
        for index in range(1, 23)
    }

    def pick(*names: str) -> dict[str, bytes]:
        every = {
            "workload/w2/package-lock.json": lock,
            "workload/w2/i18n/cjk-notes.txt": cjk,
            **docs,
            **modules,
        }
        return {name: every[name] for name in names}

    def doc(index: int) -> str:
        return f"workload/w2/docs/part-{index:02d}.md"

    def mod(index: int) -> str:
        return f"workload/w2/src/module_{index:02d}.py"

    layout = [
        ("chore(e2e): Regenerate the lockfile\n", ["workload/w2/package-lock.json", *map(mod, range(1, 5))]),
        ("docs(e2e): Add the first mixed-script chapters\n", [*map(doc, range(1, 5)), mod(5)]),
        ("docs(e2e): Add chapters five to eight\n", [*map(doc, range(5, 9)), mod(6)]),
        ("docs(e2e): Add the CJK notes\n", ["workload/w2/i18n/cjk-notes.txt", *map(mod, range(7, 11))]),
        ("docs(e2e): Add chapters nine to twelve\n", [*map(doc, range(9, 13)), mod(11)]),
        ("docs(e2e): Add the last chapters\n", [*map(doc, range(13, 17)), mod(12)]),
        ("feat(e2e): Add modules thirteen to seventeen\n", list(map(mod, range(13, 18)))),
        ("feat(e2e): Add the remaining modules\n", list(map(mod, range(18, 23)))),
    ]
    return _Workload(
        key="W2",
        name="heavy controlled",
        title="E2E fixture W2: heavy controlled workload",
        body=(
            "## Summary\n"
            "\n"
            "Adds the heavy controlled workload: a one-line lockfile of 3,800,000 bytes, "
            "2 MiB of mixed-script Markdown, a CJK file and 22 modules.\n"
            "\n"
            "- Русский: документация обновлена.\n"
            "- Ελληνικά: η τεκμηρίωση ενημερώθηκε.\n"
            "- 日本語: ドキュメントを更新しました。\n"
            "\n"
            "Generated with Claude Opus 5 for implementation in Claude Code."
        ),
        commits=[_Commit(message, pick(*names), 11 + index) for index, (message, names) in enumerate(layout)],
        exit_code=0,
        extra={
            "lockfile": {"path": "workload/w2/package-lock.json", "bytes": LOCKFILE_BYTES, "lines": 1},
            "markdown": {"files": MARKDOWN_FILES, "bytes": MARKDOWN_BYTES},
            "cjk": {"path": "workload/w2/i18n/cjk-notes.txt", "bytes": CJK_BYTES},
        },
    )


def _w4() -> _Workload:
    lines = [f"Line {number:02d}: a failing workload for the CI cost study." for number in range(1, 41)]
    # A zero width joiner between two Latin letters, and a right-to-left
    # override on a line without right-to-left text.
    lines[11] = "Line 12: the word jo\u200dined hides a zero width joiner."
    lines[26] = "Line 27: an override \u202ereverses what follows on this line."
    return _Workload(
        key="W4",
        name="failing",
        title="E2E fixture W4: failing workload",
        body=(
            "Failing workload for the CI cost study. Its commit carries a bot co-author "
            "trailer, and its file holds a zero width joiner and a right-to-left override.\n"
            "\n"
            "No attribution line follows."
        ),
        commits=[
            _Commit(
                "test(e2e): Add the failing workload file\n"
                "\n"
                "Co-authored-by: Claude <noreply@anthropic.com>\n",
                {"workload/w4/failing.txt": _text(lines)},
                31,
            )
        ],
        exit_code=1,
    )


# --- What the published action makes of it ----------------------------------


def _chain(workload: _Workload, base: str) -> None:
    """Give each commit its tree, parent and SHA."""
    parent = base
    files: dict[str, bytes] = {}
    for commit in workload.commits:
        files.update(commit.files)
        commit.parent = parent
        commit.tree = _head_tree(files)
        commit.sha = git_commit(commit.tree, parent, BASE_EPOCH + 3600 * commit.hour, commit.message)
        parent = commit.sha


def final_files(workload: _Workload) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for commit in workload.commits:
        files.update(commit.files)
    return files


def _sorted_paths(files) -> list[str]:
    """Paths in the order git diff lists them."""
    return sorted(files, key=lambda path: path.encode("utf-8"))


def predicted_records(workload: _Workload) -> int:
    """What gitdata charges against its record limit for this pull request:
    each LF and NUL of rev-list, log -z, diff --raw -z and the -U0 patch,
    plus one per call. Every file is new and ends with LF."""
    commits = len(workload.commits)
    records = commits + 1
    records += sum(4 + commit.message.count("\n") for commit in workload.commits) + 1
    files = final_files(workload)
    records += 2 * len(files) + 1
    records += sum(6 + data.count(b"\n") for data in files.values()) + 1
    return records


def _oracle(workload: _Workload, base: str) -> dict:
    """Run the published checks in-process on this pull request. git is
    replaced by the commits (newest first, as git log lists them) and the
    files (each new, in diff order)."""
    files = final_files(workload)
    changed = [
        gitdata.ChangedFile(
            path,
            "A",
            "000000",
            "100644",
            gitdata.TEXT,
            tuple(
                (number, line.rstrip("\r"))
                for number, line in enumerate(files[path].decode("utf-8").split("\n")[:-1], 1)
            ),
        )
        for path in _sorted_paths(files)
    ]
    email = IDENTITY[1]
    commits = [
        gitdata.Commit(commit.sha, email, email, commit.message) for commit in reversed(workload.commits)
    ]
    pull_request = gitdata.PullRequest(commits, base, changed)

    def fetch(settings, credential, log):
        log(_GIT_VERSION)
        return pull_request

    spent = {"lines": 0, "code_points": 0}
    budgets = []
    original_spend = rules.Budget.spend
    original_fetch = gitdata.fetch_pull_request
    original_finding = Reporter.finding
    findings = []

    def spend(self, line, where):
        spent["lines"] += 1
        spent["code_points"] += len(line)
        if self not in budgets:
            budgets.append(self)
        return original_spend(self, line, where)

    def finding(self, item):
        findings.append(
            {"severity": item.severity, "title": item.title, "message": item.message,
             "file": item.file, "line": item.line}
        )
        return original_finding(self, item)

    with tempfile.TemporaryDirectory() as directory:
        event = Path(directory) / "event.json"
        event.write_text(
            json.dumps({
                "pull_request": {
                    "number": 1,
                    "title": workload.title,
                    "body": workload.body,
                    "user": {"login": "e2e-maintainer"},
                    "base": {"ref": BASE_BRANCH, "sha": base},
                    "head": {"sha": workload.commits[-1].sha},
                }
            }),
            encoding="utf-8",
        )
        environ = {
            "CA_REQUIRE_MODEL_ATTRIBUTION": INPUTS["require-model-attribution"],
            "CA_HIDDEN_UNICODE": INPUTS["hidden-unicode"],
            "CA_TOKEN": _TOKEN,
            "CA_SERVER_URL": "https://github.com",
            "CA_REPOSITORY": "stickerdaniel/agent-guardrails",
            "CA_EVENT_NAME": "pull_request_target",
            "GITHUB_EVENT_PATH": str(event),
            "RUNNER_TEMP": directory,
            "PATH": os.defpath,
        }
        stdout = io.StringIO()
        rules.Budget.spend = spend
        gitdata.fetch_pull_request = fetch
        Reporter.finding = finding
        try:
            code = main.main(environ, stdout)
        finally:
            rules.Budget.spend = original_spend
            gitdata.fetch_pull_request = original_fetch
            Reporter.finding = original_finding
    lines = stdout.getvalue().split("\n")
    mask = f"::add-mask::{gitdata.encode_credential(_TOKEN)}"
    if lines[:2] != [mask, f"agent-guardrails: {_GIT_VERSION}"] or lines[-1] != "":
        raise AssertionError(f"{workload.key}: unexpected output start {lines[:2]!r}")
    if len(budgets) != 1:
        raise AssertionError(f"{workload.key}: expected one shared budget")
    tail = lines[2:-1]
    errors = sum(item["severity"] == "error" for item in findings)
    return {
        "exit_code": code,
        "conclusion": "success" if code == 0 else "failure",
        "errors": errors,
        "warnings": len(findings) - errors,
        "findings": findings,
        "stdout_after_git_version": tail,
        "log_lines": [line for line in tail if line.startswith("agent-guardrails: ")],
        "counts": {
            "scanned_lines": spent["lines"],
            "code_points": spent["code_points"],
            "work_units": budgets[0]._work,
        },
    }


# --- Writing ------------------------------------------------------------------


def _record(path: Path, data: bytes, root: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "file": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def generate(out: Path) -> dict:
    """Write every workload into out, which must be empty or absent, and
    return the manifest, also written as out/manifest.json."""
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise SystemExit(f"{out} is not empty")
    base = base_commit()
    manifest = {
        "version": VERSION,
        "seed": SEED,
        "generator": "e2e/gen_workloads.py",
        "oracle": (
            "agent_guardrails at 3937a2dc711ea26a856b2b75536cd01c80ac86b7, runtime-equivalent to "
            "the published a3508d7320e64370b878359b1f4ae541b507224f, with git replaced in-process"
        ),
        "inputs": INPUTS,
        "identity": {"name": IDENTITY[0], "email": IDENTITY[1]},
        "base": {
            "branch": BASE_BRANCH,
            "parent": BASE_PARENT,
            "tree": base_tree(),
            "commit": base,
            "date": _iso(BASE_EPOCH),
            "epoch": BASE_EPOCH,
            "message": _BASE_MESSAGE,
            "removes": [".github/workflows"],
        },
        "variant_output": {
            "note": (
                "Each variant prints its own lines, then the credential mask, then "
                "'agent-guardrails: git version <runner git>', then stdout_after_git_version "
                "exactly. The mask never reaches the job log; annotations appear there as ##[error] "
                "and ##[warning] lines, and log_lines appear verbatim."
            ),
            "PY": ["Python 3.12.<patch> (python3 --version in the composite step)"],
            "RS": [
                "agent-guardrails: agent-guardrails native study; rustc 1.97.1; CPython 3.12.3; "
                "UCD 15.0.0; tables <table id>"
            ],
            "TS-H": [
                "agent-guardrails: agent-guardrails TS-H study; node v24.<minor>.<patch>; "
                "CPython 3.12.3; UCD 15.0.0; tables <table id>; bundle <sha256>; git-guard <sha256>"
            ],
        },
        "workloads": {},
    }
    for workload in (_w3(), _w1(), _w2(), _w4()):
        _chain(workload, base)
        key = workload.key
        folder = out / key.lower()
        entry = {
            "name": workload.name,
            "head_branch": f"e2e/workload-{key.lower()}",
            "head_sha": workload.commits[-1].sha,
            "title": {
                **_record(folder / "pr" / "title.txt", workload.title.encode("utf-8"), out),
                "text": workload.title,
            },
            "body": {
                **_record(folder / "pr" / "body.md", (workload.body or "").encode("utf-8"), out),
                "text": workload.body,
            },
            "commits": [],
        }
        for index, commit in enumerate(workload.commits, 1):
            epoch = BASE_EPOCH + 3600 * commit.hour
            where = folder / "commits" / f"{index:02d}"
            person = {
                "name": IDENTITY[0], "email": IDENTITY[1], "date": _iso(epoch), "epoch": epoch, "tz": "+0000"
            }
            entry["commits"].append({
                "index": index,
                "parent": commit.parent,
                "tree": commit.tree,
                "sha": commit.sha,
                "author": person,
                "committer": person,
                "message": _record(where / "message.txt", commit.message.encode("utf-8"), out),
                "files": [
                    {
                        "path": path,
                        **_record(where / "files" / path, commit.files[path], out),
                        "git_blob": _git_object("blob", commit.files[path]),
                        "lines": commit.files[path].count(b"\n"),
                    }
                    for path in _sorted_paths(commit.files)
                ],
            })
        files = final_files(workload)
        oracle = _oracle(workload, base)
        if oracle["exit_code"] != workload.exit_code:
            raise AssertionError(f"{key}: exit {oracle['exit_code']}, designed {workload.exit_code}")
        stdout = "".join(f"{line}\n" for line in oracle.pop("stdout_after_git_version"))
        entry["pull_request_diff"] = {
            "files": len(files),
            "added_lines": sum(data.count(b"\n") for data in files.values()),
            "added_bytes": sum(len(data) for data in files.values()),
        }
        entry["counts"] = {**oracle.pop("counts"), "records": predicted_records(workload)}
        entry["expected"] = {
            **oracle,
            "stdout_after_git_version": _record(
                folder / "expected" / "stdout.txt", stdout.encode("utf-8"), out
            ),
        }
        entry.update(workload.extra)
        manifest["workloads"][key] = entry
    data = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    (out / "manifest.json").write_bytes(data)
    return manifest


def _iso(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lock_of(out: Path) -> dict:
    """The lock of a generated directory: every file's SHA-256 and size."""
    files = {}
    for path in sorted(out.rglob("*"), key=lambda item: item.relative_to(out).as_posix()):
        if path.is_file():
            data = path.read_bytes()
            files[path.relative_to(out).as_posix()] = {
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)
            }
    return {"version": VERSION, "seed": SEED, "files": files}


def dumps(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check(out: Path | None) -> list[str]:
    """Regenerate and compare with the committed lock. The differences, or
    an empty list."""
    temporary = None
    if out is None:
        temporary = tempfile.mkdtemp(prefix="e2e-workloads-")
        out = Path(temporary) / "workloads"
    try:
        generate(out)
        actual = lock_of(out)
    finally:
        if temporary:
            shutil.rmtree(temporary)
    expected = json.loads(LOCK.read_text(encoding="utf-8"))
    problems = []
    for name in sorted(set(expected["files"]) | set(actual["files"])):
        if expected["files"].get(name) != actual["files"].get(name):
            locked, generated = expected["files"].get(name), actual["files"].get(name)
            problems.append(f"{name}: locked {locked}, generated {generated}")
    if (expected["version"], expected["seed"]) != (actual["version"], actual["seed"]):
        problems.append("lock header differs")
    return problems


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", nargs="?", type=Path, help="directory to write; empty or absent")
    parser.add_argument("--check", action="store_true", help="compare with e2e/workloads.lock.json")
    parser.add_argument("--write-lock", action="store_true", help="write e2e/workloads.lock.json from OUT")
    args = parser.parse_args(argv)
    if args.check:
        problems = check(args.out)
        for problem in problems:
            print(problem)
        print("workloads match the lock" if not problems else f"{len(problems)} differences")
        return 1 if problems else 0
    if args.out is None:
        parser.error("OUT is required")
    if ROOT in args.out.resolve().parents or args.out.resolve() == ROOT:
        parser.error("write the workloads outside the repository")
    manifest = generate(args.out)
    if args.write_lock:
        LOCK.write_text(dumps(lock_of(args.out)), encoding="utf-8")
    for key, entry in manifest["workloads"].items():
        print(f"{key} {entry['name']}: head {entry['head_sha']}, exit {entry['expected']['exit_code']}")
    print(f"base {manifest['base']['commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
