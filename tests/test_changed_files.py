"""The added-line reader and the hidden Unicode check against real git: the
records the reader returns, and the entrypoint's result on whole pull
requests."""

from __future__ import annotations

import os
import unittest

from agent_guardrails import event, gitdata

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


class DriverTests(_ChangedFileTestCase):
    """The entrypoint exactly as action.yml starts it."""

    def _run(self, head: str, **event_fields):
        mode = event_fields.pop("mode", "error")
        self.remote.open_pull_request(head)
        result = self.remote.run_action(
            self.remote.event(head=head, **event_fields), CA_HIDDEN_UNICODE=mode
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


if __name__ == "__main__":
    unittest.main()
