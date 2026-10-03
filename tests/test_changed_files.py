"""The added-line reader and the hidden Unicode check against real git: the
records the reader returns, and the entrypoint's result on whole pull
requests."""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from agent_guardrails import event, gitdata, rules
from agent_guardrails.main import main

from .support import TOKEN, RemoteTestCase, foreign_commands

_ZWSP = "​"
_MACROSCOPE = (
    "<!-- Macroscope's pull request summary starts here -->\n"
    "Co-authored-by: Claude <noreply@anthropic.com>\n"
    f"Sum{_ZWSP}mary\n"
    "<!-- Macroscope's pull request summary ends here -->\n"
)


class _ChangedFileTestCase(RemoteTestCase):
    def new_base(self, **files: str | bytes) -> None:
        """Move main forward, so the pull request starts from these files."""
        base = self.remote.commit("Base", files=files)
        self.remote.push(base, "refs/heads/main")
        self.remote.base = base

    def symlink(self, name: str, target: str) -> None:
        path = self.remote.work / name
        if path.exists() or path.is_symlink():
            path.unlink()
        os.symlink(target, path)
        self.remote.git("add", name)

    def replace_with_file(self, name: str, content: str) -> None:
        (self.remote.work / name).unlink()
        (self.remote.work / name).write_text(content, encoding="utf-8")
        self.remote.git("add", name)

    def remove(self, name: str) -> None:
        self.remote.git("rm", "--quiet", name)


class ReaderTests(_ChangedFileTestCase):
    """What gitdata hands to the checks, for each kind of change."""

    def _files(self, head: str) -> dict[str, gitdata.ChangedFile]:
        self.remote.open_pull_request(head)
        settings = event.load(self.remote.environment(self.remote.event(head=head)))
        pull_request = gitdata.fetch_pull_request(
            settings, gitdata.encode_credential(TOKEN), lambda _: None
        )
        return {changed.path: changed for changed in pull_request.files}

    def test_records_status_modes_kind_and_added_lines(self) -> None:
        self.symlink("link", "target")
        self.new_base(**{"five.txt": "1\n2\n3\n4\n5\n", "mode.sh": "x\n", "gone.txt": "g\n"})
        self.replace_with_file("link", f"a{_ZWSP}b\n")
        (self.remote.work / "mode.sh").chmod(0o755)
        self.remote.git("add", "mode.sh")
        self.remote.git("update-index", "--add", "--cacheinfo", f"160000,{self.remote.base},sub")
        self.remove("gone.txt")
        head = self.remote.commit(
            "Change everything",
            files={
                "five.txt": "1\n2\nthree\n4\n5\nsix",
                "crlf.txt": "one\r\ntwo\r\n",
                "logo.png": b"\x89PNG\0",
                "notes.md": b"text\0more\n",
                "empty.txt": "",
            },
        )

        files = self._files(head)

        self.assertEqual(
            sorted(files),
            ["crlf.txt", "empty.txt", "five.txt", "link", "logo.png", "mode.sh", "notes.md", "sub"],
        )
        expected = {
            "link": ("T", "120000", "100644", gitdata.TEXT, ((1, f"a{_ZWSP}b"),)),
            "five.txt": ("M", "100644", "100644", gitdata.TEXT, ((3, "three"), (6, "six"))),
            "crlf.txt": ("A", "000000", "100644", gitdata.TEXT, ((1, "one"), (2, "two"))),
            "mode.sh": ("M", "100644", "100755", gitdata.TEXT, ()),
            "empty.txt": ("A", "000000", "100644", gitdata.TEXT, ()),
            "logo.png": ("A", "000000", "100644", gitdata.ALLOWED_BINARY, ()),
            "notes.md": ("A", "000000", "100644", gitdata.REJECTED_BINARY, ()),
            "sub": ("A", "000000", "160000", gitdata.SUBMODULE, ()),
        }
        for path, (status, old_mode, new_mode, kind, added) in expected.items():
            with self.subTest(path=path):
                changed = files[path]
                self.assertEqual(
                    (changed.status, changed.old_mode, changed.new_mode, changed.kind, changed.added),
                    (status, old_mode, new_mode, kind, added),
                )

    def test_hunk_lines_that_look_like_headers_are_content(self) -> None:
        self.new_base(**{"plus.md": "keep\n-- a/old\n"})
        head = self.remote.commit(
            "Edit",
            files={
                "plus.md": "keep\n++ b/new\ndiff --git a/x b/x\n",
                'we"ird name.md': "++ quoted\n",
            },
        )

        files = self._files(head)

        self.assertEqual(files["plus.md"].added, ((2, "++ b/new"), (3, "diff --git a/x b/x")))
        self.assertEqual(files['we"ird name.md'].added, ((1, "++ quoted"),))

    def test_invalid_utf8_is_undecodable(self) -> None:
        head = self.remote.commit("Add", files={"latin1.txt": b"caf\xe9\n"})
        self.assertEqual(self._files(head)["latin1.txt"].kind, gitdata.UNDECODABLE)

    def test_kind_is_that_of_the_new_side(self) -> None:
        # Git prints a binary patch when either side is binary.
        self.new_base(
            **{
                "x.png": b"PNG\0",
                "data.bin": b"\0\1",
                "notes.md": "text\n",
                "logo.png": "text\n",
                "photo.png": b"\x89PNG\0a",
            }
        )
        head = self.remote.commit(
            "Replace",
            files={
                "x.png": f"new{_ZWSP}text\nmore\n",
                "data.bin": f"a{_ZWSP}b\n",
                "notes.md": b"a\0b\n",
                "logo.png": b"\x89PNG\0",
                "photo.png": b"\x89PNG\0b",
            },
        )

        files = self._files(head)

        expected = {
            "x.png": (gitdata.TEXT, ((1, f"new{_ZWSP}text"), (2, "more"))),
            "data.bin": (gitdata.TEXT, ((1, f"a{_ZWSP}b"),)),
            "notes.md": (gitdata.REJECTED_BINARY, ()),
            "logo.png": (gitdata.ALLOWED_BINARY, ()),
            "photo.png": (gitdata.ALLOWED_BINARY, ()),
        }
        for path, (kind, added) in expected.items():
            with self.subTest(path=path):
                self.assertEqual((files[path].status, files[path].kind, files[path].added), ("M", kind, added))

    def test_mode_change_reads_the_new_file_and_adds_no_line(self) -> None:
        contents = {
            "notes.md": f"a\0b{_ZWSP}\n".encode(),
            "logo.png": b"PNG\0",
            "latin1.txt": b"caf\xe9\n",
            "plain.md": f"a{_ZWSP}b\n",
            "empty.sh": "",
        }
        self.new_base(**contents)
        for name in contents:
            (self.remote.work / name).chmod(0o755)
            self.remote.git("add", name)
        head = self.remote.commit("Make executable")

        files = self._files(head)

        expected = {
            "notes.md": gitdata.REJECTED_BINARY,
            "logo.png": gitdata.ALLOWED_BINARY,
            "latin1.txt": gitdata.UNDECODABLE,
            "plain.md": gitdata.TEXT,
            "empty.sh": gitdata.TEXT,
        }
        for path, kind in expected.items():
            with self.subTest(path=path):
                changed = files[path]
                self.assertEqual(
                    (changed.status, changed.old_mode, changed.new_mode, changed.kind, changed.added),
                    ("M", "100644", "100755", kind, ()),
                )


class DriverTests(_ChangedFileTestCase):
    """The entrypoint exactly as action.yml starts it."""

    def _run(self, head: str, **event_fields):
        mode = event_fields.pop("mode", "error")
        self.remote.open_pull_request(head)
        result = self.remote.run_action(
            self.remote.event(head=head, **event_fields), {"hidden-unicode": mode}
        )
        self.assertEqual(foreign_commands(result.stdout), [])
        return result

    def test_added_line_is_located(self) -> None:
        self.new_base(**{"docs/x.md": "1\n2\n3\n4\n5\n"})
        head = self.remote.commit("Edit", files={"docs/x.md": f"1\n2\n3\nfo{_ZWSP}ur\n5\n"})
        result = self._run(head)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            "::error file=docs/x.md,line=4,title=Invisible character::Line 4 of docs/x.md "
            "has 1 invisible character: U+200B ZERO WIDTH SPACE; the line reads: fo<U+200B>ur\n",
            result.stdout,
        )

    def test_removed_lines_are_not_reported(self) -> None:
        self.new_base(**{"y.md": f"keep\nbad{_ZWSP}\n", "gone.md": f"old{_ZWSP}\n"})
        self.remove("gone.md")
        head = self.remote.commit("Clean up", files={"y.md": "keep\n"})
        result = self._run(head)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("Invisible character", result.stdout)

    def test_symlink_replaced_by_a_file_is_scanned(self) -> None:
        self.symlink("link", "target")
        self.new_base()
        self.replace_with_file("link", f"a{_ZWSP}b\n")
        head = self.remote.commit("Replace the link")
        result = self._run(head)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("::error file=link,line=1,title=Invisible character::", result.stdout)

    def test_pull_request_gitattributes_cannot_hide_a_file(self) -> None:
        head = self.remote.commit(
            "Add docs", files={".gitattributes": "*.md binary\n", "x.md": f"a{_ZWSP}b\n"}
        )
        result = self._run(head)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("::error file=x.md,line=1,title=Invisible character::", result.stdout)

    def test_binary_text_file_fails(self) -> None:
        head = self.remote.commit("Add notes", files={"notes.md": f"a\0b{_ZWSP}\n".encode()})
        result = self._run(head)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            "::error file=notes.md,title=Cannot scan a changed file::"
            "cannot scan notes.md: binary content.",
            result.stdout,
        )

    def test_binary_format_is_logged_as_unscanned(self) -> None:
        self.remote.git("update-index", "--add", "--cacheinfo", f"160000,{self.remote.base},sub")
        head = self.remote.commit("Add a logo", files={"img/logo.png": b"\x89PNG\r\n\x1a\n\0"})
        result = self._run(head)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("agent-guardrails: not scanned (binary): img/logo.png\n", result.stdout)
        self.assertIn("agent-guardrails: not scanned (submodule): sub\n", result.stdout)
        self.assertIn("checked 1 commit, 2 changed files", result.stdout)

    def test_binary_replaced_by_text_is_scanned(self) -> None:
        self.new_base(**{"x.png": b"PNG\0", "data.bin": b"\0\1"})
        head = self.remote.commit(
            "Replace", files={"x.png": f"new{_ZWSP}text\n", "data.bin": f"a{_ZWSP}b\n"}
        )
        result = self._run(head)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("::error file=x.png,line=1,title=Invisible character::", result.stdout)
        self.assertIn("::error file=data.bin,line=1,title=Invisible character::", result.stdout)
        self.assertNotIn("not scanned", result.stdout)

    def test_mode_change_judges_the_new_file_in_warn_mode_too(self) -> None:
        contents = {
            "notes.md": f"a\0b{_ZWSP}\n".encode(),
            "logo.png": b"PNG\0",
            "latin1.txt": b"caf\xe9\n",
            "plain.md": f"a{_ZWSP}b\n",
        }
        self.new_base(**contents)
        for name in contents:
            (self.remote.work / name).chmod(0o755)
            self.remote.git("add", name)
        head = self.remote.commit("Make executable")
        result = self._run(head, mode="warn")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            "::error file=notes.md,title=Cannot scan a changed file::cannot scan notes.md: binary content.",
            result.stdout,
        )
        self.assertIn(
            "::error file=latin1.txt,title=Cannot scan a changed file::cannot decode latin1.txt",
            result.stdout,
        )
        self.assertIn("agent-guardrails: not scanned (binary): logo.png\n", result.stdout)
        # Nothing was added to plain.md, so its old line is not a finding.
        self.assertNotIn("plain.md", result.stdout)
        self.assertIn("checked 1 commit, 4 changed files", result.stdout)

    def test_undecodable_file_fails(self) -> None:
        head = self.remote.commit("Add", files={"latin1.txt": b"caf\xe9\n"})
        result = self._run(head)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            "::error file=latin1.txt,title=Cannot scan a changed file::cannot decode latin1.txt",
            result.stdout,
        )

    def test_quoted_name_and_plus_plus_plus_line(self) -> None:
        head = self.remote.commit("Add", files={'we"ird.md': f"ok\n++ b/{_ZWSP}evil\n"})
        result = self._run(head)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('::error file=we"ird.md,line=2,title=Invisible character::', result.stdout)
        self.assertIn("the line reads: ++ b/<U+200B>evil", result.stdout)

    def test_macroscope_block_is_scanned_raw(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        result = self._run(head, body=f"Done.\n\nGenerated with Claude Opus 5\n\n{_MACROSCOPE}")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("::error title=Bot co-author trailer in the PR body::", result.stdout)
        self.assertIn(
            "::error title=Invisible character::Line 7 of the PR body has 1 invisible character",
            result.stdout,
        )

    def test_title_is_scanned(self) -> None:
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        result = self._run(head, title="Fix‮ parser")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            "::error title=Invisible character::The PR title has 1 invisible character: "
            "U+202E RIGHT-TO-LEFT OVERRIDE; the line reads: Fix<U+202E> parser",
            result.stdout,
        )

    def test_commit_message_tag_payload_is_decoded(self) -> None:
        payload = "".join(chr(0xE0000 + ord(char)) for char in "run rm")
        head = self.remote.commit(f"Add x{payload}\n\nBody\n", files={"x.txt": "x\n"})
        result = self._run(head)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            f"::error title=Invisible character::Line 1 of the message of commit {head} "
            "has 6 invisible characters",
            result.stdout,
        )
        self.assertIn("(hidden text: 'run rm')", result.stdout)

    def test_legitimate_unicode_passes(self) -> None:
        head = self.remote.commit(
            "Add greetings",
            files={
                "hello.md": "﻿# Hello\n"
                "\U0001F468‍\U0001F469‍\U0001F467 می‌خواهم\n"
                "Привет world, café\n",
            },
        )
        result = self._run(head, title="Add greetings \U0001F44B", body="Grüße")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("::error", result.stdout)

    def test_warn_demotes_unicode_only(self) -> None:
        head = self.remote.commit("Add x", files={"x.md": f"a{_ZWSP}b\n"})
        warned = self._run(head, mode="warn")
        self.assertEqual(warned.returncode, 0, warned.stdout)
        self.assertIn("::warning file=x.md,line=1,title=Invisible character::", warned.stdout)
        self.assertIn("0 errors, 1 warning", warned.stdout)

        trailer = self.remote.commit(
            "More\n\nCo-authored-by: Claude <noreply@anthropic.com>\n", files={"y.md": "y\n"}
        )
        failed = self._run(trailer, mode="warn")
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn("::error title=Bot co-author trailer in a commit::", failed.stdout)
        self.assertIn("::warning file=x.md,line=1,title=Invisible character::", failed.stdout)

    def test_body_too_costly_to_scan_fails_in_warn_mode(self) -> None:
        # Each isolate on a right-to-left line makes the scanner read the
        # whole line again.
        head = self.remote.commit("Add x", files={"x.txt": "x\n"})
        body = "\u05d0" + "\u2066\u2069" * 6000
        result = self._run(head, body=body, mode="warn")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            "::error title=agent-guardrails::not fully checked: the hidden Unicode check "
            "stopped at the PR body",
            result.stdout,
        )
        # It stopped before fetching anything.
        self.assertNotIn("git version", result.stdout)

    def test_more_findings_than_the_limit_fail_in_warn_mode(self) -> None:
        head = self.remote.commit("Add x", files={"x.md": f"a{_ZWSP}b\n" * 1001})
        result = self._run(head, mode="warn")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout.count("::warning file=x.md,"), 1000)
        self.assertIn(
            "::error title=agent-guardrails::not fully checked: stopped after 1000 findings",
            result.stdout,
        )

    def test_findings_of_one_line_before_the_limit_are_reported(self) -> None:
        # One line, 1,001 words that each mix a Cyrillic letter into Latin.
        head = self.remote.commit("Add x", files={"x.md": ("p\u0430 " * 1001).rstrip() + "\n"})
        result = self._run(head, mode="warn")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(
            result.stdout.count("::warning file=x.md,line=1,title=Look-alike letter::"), 1000
        )
        self.assertEqual(result.stdout.count("agent-guardrails: warning: Look-alike letter: "), 1000)
        self.assertTrue(
            result.stdout.endswith(
                "::error title=agent-guardrails::not fully checked: stopped after 1000 findings, "
                "the most this action reports for one pull request\n"
                "agent-guardrails: error: agent-guardrails: not fully checked: stopped after "
                "1000 findings, the most this action reports for one pull request\n"
            ),
            result.stdout[-500:],
        )

    def test_findings_of_one_trailer_check_before_the_limit_are_reported(self) -> None:
        trailers = "Co-authored-by: Claude <noreply@anthropic.com>\n" * 1001
        cases = {
            "Bot co-author trailer in the PR body": ("Add x", trailers),
            "Bot co-author trailer in a commit": (f"Add x\n\n{trailers}", ""),
        }
        for title, (message, body) in cases.items():
            with self.subTest(title):
                head = self.remote.commit(message, files={"x.txt": message[-40:]})
                result = self._run(head, body=body)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout.count(f"::error title={title}::"), 1000)
                self.assertIn("not fully checked: stopped after 1000 findings", result.stdout)


class PolicyDriverTests(_ChangedFileTestCase):
    """Exclusions, added binary formats, and every Unicode rule off, through
    the entrypoint exactly as action.yml starts it."""

    def _run(self, head: str, inputs: dict[str, str], **event_fields):
        self.remote.open_pull_request(head)
        result = self.remote.run_action(self.remote.event(head=head, **event_fields), inputs)
        self.assertEqual(foreign_commands(result.stdout), [])
        return result

    def test_excluded_file_is_logged_and_not_scanned(self) -> None:
        head = self.remote.commit(
            "Add", files={"fixtures/x.md": f"a{_ZWSP}b\n", "src/y.md": "clean\n"}
        )
        result = self._run(head, {"unicode-exclude-paths": "fixtures/"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("agent-guardrails: not scanned (excluded): fixtures/x.md\n", result.stdout)
        self.assertNotIn("Invisible character", result.stdout)
        self.assertIn("checked 1 commit, 2 changed files", result.stdout)

    def test_exclusions_leave_title_body_and_messages_alone(self) -> None:
        head = self.remote.commit(f"Add{_ZWSP} x", files={"x.md": f"a{_ZWSP}b\n"})
        result = self._run(
            head, {"unicode-exclude-paths": "x.md"}, title=f"Fix{_ZWSP}", body=f"Do{_ZWSP}ne"
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout.count("::error title=Invisible character::"), 3)
        for where in ("The PR title", "The PR body", f"The message of commit {head}"):
            self.assertIn(f"::error title=Invisible character::{where} has 1", result.stdout)
        self.assertNotIn("file=x.md", result.stdout)

    def test_a_move_is_judged_at_its_new_path(self) -> None:
        self.new_base(**{"fixtures/a.md": f"a{_ZWSP}b\n", "src/b.md": f"c{_ZWSP}d\n"})
        self.remote.git("mv", "fixtures/a.md", "src/a.md")
        self.remote.git("mv", "src/b.md", "fixtures/b.md")
        head = self.remote.commit("Move")
        result = self._run(head, {"unicode-exclude-paths": "fixtures/"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("::error file=src/a.md,line=1,title=Invisible character::", result.stdout)
        self.assertIn("agent-guardrails: not scanned (excluded): fixtures/b.md\n", result.stdout)
        self.assertNotIn("file=fixtures/b.md", result.stdout)

    def test_unreadable_files_fail_inside_an_exclusion(self) -> None:
        head = self.remote.commit(
            "Add", files={"fixtures/latin1.txt": b"caf\xe9\n", "fixtures/notes.md": b"a\0b\n"}
        )
        for hidden_unicode in ("error", "off"):
            with self.subTest(hidden_unicode=hidden_unicode):
                result = self._run(
                    head, {"unicode-exclude-paths": "fixtures/", "hidden-unicode": hidden_unicode}
                )
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(
                    "::error file=fixtures/latin1.txt,title=Cannot scan a changed file::"
                    "cannot decode fixtures/latin1.txt",
                    result.stdout,
                )
                self.assertIn(
                    "::error file=fixtures/notes.md,title=Cannot scan a changed file::"
                    "cannot scan fixtures/notes.md: binary content.",
                    result.stdout,
                )
                self.assertNotIn("not scanned (excluded)", result.stdout)

    def test_an_undecodable_name_fails_inside_an_exclusion(self) -> None:
        # The invalid name decodes for display to the same U+FFFD that the
        # valid name holds; only the valid one is excluded.
        blob = self.remote.git("hash-object", "-w", "--stdin", stdin="clean\n")
        self.remote.git("update-index", "--add", "--cacheinfo", f"100644,{blob},fixtures/bad\udcff.txt")
        head = self.remote.commit("Add", files={"fixtures/ok\ufffd.txt": f"a{_ZWSP}b\n"})
        for hidden_unicode in ("error", "off"):
            with self.subTest(hidden_unicode=hidden_unicode):
                result = self._run(
                    head, {"unicode-exclude-paths": "fixtures/", "hidden-unicode": hidden_unicode}
                )
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(
                    "title=Cannot scan a changed file::cannot decode fixtures/bad\ufffd.txt",
                    result.stdout,
                )
                self.assertIn(
                    "agent-guardrails: not scanned (excluded): fixtures/ok\ufffd.txt\n", result.stdout
                )
                self.assertNotIn("Invisible character", result.stdout)

    def test_added_binary_format(self) -> None:
        wasm = b"\0asm\1\0\0\0"
        head = self.remote.commit("Add", files={"x.wasm": wasm, "y.wasm": f"a{_ZWSP}b\n"})
        allowed = self._run(head, {"additional-binary-extensions": "wasm"})
        self.assertEqual(allowed.returncode, 1, allowed.stdout)
        self.assertIn("agent-guardrails: not scanned (binary): x.wasm\n", allowed.stdout)
        self.assertNotIn("file=x.wasm", allowed.stdout)
        # Text is scanned whatever its extension.
        self.assertIn("::error file=y.wasm,line=1,title=Invisible character::", allowed.stdout)

        refused = self._run(head, {})
        self.assertIn("cannot scan x.wasm: binary content", refused.stdout)

    def test_rejection_names_the_added_formats(self) -> None:
        head = self.remote.commit("Add", files={"x.wasm": b"\0asm", "notes.md": b"a\0b\n"})
        result = self._run(head, {"additional-binary-extensions": "wasm"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("cannot scan notes.md: binary content.", result.stdout)
        self.assertIn("ttf, wasm, wav", result.stdout)

    def test_every_unicode_rule_off(self) -> None:
        line = "a\u200bb prompt\ue000 10\u00a0km p\u0430ypal"
        head = self.remote.commit(f"Add {line}", files={"x.md": f"{line}\n"})
        result = self._run(head, {"hidden-unicode": "off"}, title=line, body=line)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("0 errors, 0 warnings", result.stdout)

        unreadable = self.remote.commit("More", files={"latin1.txt": b"caf\xe9\n", "notes.md": b"a\0b"})
        result = self._run(unreadable, {"hidden-unicode": "off"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("cannot decode latin1.txt", result.stdout)
        self.assertIn("cannot scan notes.md: binary content", result.stdout)
        self.assertIn("2 errors, 0 warnings", result.stdout)

    def test_git_output_limit_holds_with_every_unicode_rule_off(self) -> None:
        head = self.remote.commit("Add data", files={"data.txt": "line\n" * 300_000})
        self.remote.open_pull_request(head)
        stdout = io.StringIO()
        environ = self.remote.environment(self.remote.event(head=head), {"hidden-unicode": "off"})
        with mock.patch.object(gitdata, "OUTPUT_LIMIT", 2**20):
            self.assertEqual(main(environ, stdout=stdout), 1, stdout.getvalue())
        self.assertIn(
            "::error title=agent-guardrails::not fully checked: git printed more than 1 MiB",
            stdout.getvalue(),
        )


class InspectionLimitTests(_ChangedFileTestCase):
    """What a line may hold in memory while it is checked is bounded before
    it is taken, and running out fails the run in warn mode too."""

    def _main(self, head: str) -> str:
        self.remote.open_pull_request(head)
        stdout = io.StringIO()
        environ = self.remote.environment(self.remote.event(head=head), {"hidden-unicode": "warn"})
        self.assertEqual(main(environ, stdout=stdout), 1, stdout.getvalue())
        self.assertEqual(foreign_commands(stdout.getvalue()), [])
        return stdout.getvalue()

    def test_line_of_dense_control_characters_stops_the_run(self) -> None:
        head = self.remote.commit("Add x", files={"x.md": "ok\n" + "\x01" * 5000 + "\n"})
        with mock.patch.object(rules, "HIT_LIMIT", 1000):
            stdout = self._main(head)
        self.assertIn(
            "::error title=agent-guardrails::not fully checked: the hidden Unicode check "
            "stopped at line 2 of x.md, which has more than the 1,000 suspicious characters",
            stdout,
        )

    def test_long_line_after_a_binary_file_stops_the_run(self) -> None:
        self.new_base(**{"x.png": b"PNG\0"})
        head = self.remote.commit("Replace", files={"x.png": "a" * 5000 + "\n"})
        with mock.patch.object(rules, "LINE_LENGTH_LIMIT", 1000):
            stdout = self._main(head)
        self.assertIn(
            "::error title=agent-guardrails::not fully checked: the hidden Unicode check "
            "stopped at line 1 of x.png, longer than the 1,000 characters",
            stdout,
        )


if __name__ == "__main__":
    unittest.main()
