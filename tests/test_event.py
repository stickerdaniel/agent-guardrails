from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_guardrails import event
from agent_guardrails.main import main

from .support import TOKEN, foreign_commands, runner_inputs

_BASE = "b" * 40
_HEAD = "c" * 40


def _payload(**pull_request: object) -> dict:
    pull = {
        "number": 7,
        "title": "feat: Add x",
        "body":"Generated with GPT-5.6 for implementation in Claude Code.",
        "user": {"login": "jane"},
        "base": {"ref": "main", "sha": _BASE},
        "head": {"sha": _HEAD},
    }
    pull.update(pull_request)
    return {"pull_request": pull}


class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "event.json"

    def _load(
        self,
        payload: object = None,
        raw: str | None = None,
        inputs: dict[str, str] | None = None,
        **overrides: str,
    ) -> event.Settings:
        text = raw if raw is not None else json.dumps(_payload() if payload is None else payload)
        self.path.write_text(text, encoding="utf-8")
        given = {"require-model-attribution": "true"} if inputs is None else inputs
        environ = {
            "PATH": "/usr/bin",
            event.INPUTS: json.dumps(runner_inputs(given)),
            event.TOKEN: TOKEN,
            event.SERVER_URL: "https://github.com",
            event.REPOSITORY: "owner/repo",
            event.EVENT_NAME: "pull_request_target",
            event.EVENT_PATH: str(self.path),
        }
        environ.update(overrides)
        return event.load(environ)

    def _rejects(
        self,
        message: str,
        payload: object = None,
        raw: str | None = None,
        inputs: dict[str, str] | None = None,
        **overrides: str,
    ) -> str:
        with self.assertRaises(event.InvalidEvent) as caught:
            self._load(payload, raw, inputs, **overrides)
        self.assertIn(message, str(caught.exception))
        return str(caught.exception)

    def test_reads_body_from_event_file(self) -> None:
        settings = self._load()
        self.assertEqual(settings.title, "feat: Add x")
        self.assertEqual(settings.body, "Generated with GPT-5.6 for implementation in Claude Code.")
        self.assertEqual(settings.number, 7)
        self.assertEqual((settings.base_ref, settings.base_sha, settings.head_sha), ("main", _BASE, _HEAD))
        self.assertTrue(settings.policy.require_model_attribution)
        self.assertEqual(settings.policy.hidden_unicode, "error")

    def test_null_body_is_accepted_as_none(self) -> None:
        self.assertIsNone(self._load(_payload(body=None)).body)

    def test_inputs_accept_exactly_their_values(self) -> None:
        self.assertFalse(
            self._load(inputs={"require-model-attribution": "false"}).policy.require_model_attribution
        )
        self.assertEqual(self._load(inputs={"hidden-unicode": "warn"}).policy.hidden_unicode, "warn")
        self.assertEqual(self._load(inputs={"hidden-unicode": "off"}).policy.hidden_unicode, "off")
        for value in ("yes", "True", "1", ""):
            with self.subTest(value=value):
                self._rejects("input require-model-attribution must be true or false",
                              inputs={"require-model-attribution": value})
        for value in ("Error", ""):
            with self.subTest(value=value):
                self._rejects("input hidden-unicode must be error, warn or off",
                              inputs={"hidden-unicode": value})

    def test_rejects_other_events(self) -> None:
        self._rejects("runs only under pull_request_target", **{event.EVENT_NAME: "pull_request"})

    def test_rejects_malformed_payloads(self) -> None:
        cases = {
            "cannot read the event payload": (None, "{not json"),
            "the event payload is not an object": ([], None),
            "pull_request is not an object": ({"issue": {}}, None),
            "pull_request.number is not a positive integer": (_payload(number="7"), None),
            "pull_request.base.sha is not a 40-character hex commit SHA": (
                _payload(base={"ref": "main", "sha": "B" * 40}), None),
            "pull_request.head.sha is not a 40-character hex commit SHA": (
                _payload(head={"sha": _HEAD[:39]}), None),
            "pull_request.body must be a string or null": (_payload(body=["x"]), None),
            "pull_request.title is not a string": (_payload(title=None), None),
            "pull_request.base.ref is not a branch name": (
                _payload(base={"ref": "", "sha": _BASE}), None),
            "pull_request.user.login is not a string": (_payload(user={"login": None}), None),
        }
        for message, (payload, raw) in cases.items():
            with self.subTest(message=message):
                self._rejects(message, payload, raw)

    def test_rejects_a_boolean_pull_request_number(self) -> None:
        self._rejects("pull_request.number", _payload(number=True))

    def test_rejects_a_missing_token_and_a_malformed_repository(self) -> None:
        self._rejects("job token", **{event.TOKEN: ""})
        self._rejects("job token", **{event.TOKEN: "a b"})
        self._rejects("github.repository", **{event.REPOSITORY: "owner/repo/../x y"})

    def test_inputs_are_judged_before_the_event(self) -> None:
        self._rejects(
            "unknown input(s): 'typo'", inputs={"typo": "x"}, **{event.EVENT_NAME: "pull_request"}
        )
        self._rejects(
            "input co-author-trailers must be",
            inputs={"co-author-trailers": "of"},
            **{event.EVENT_NAME: "pull_request"},
        )


def _policy(given: dict[str, str] | None = None, raw: str | None = None) -> event.Policy:
    text = json.dumps(runner_inputs(given)) if raw is None else raw
    return event._policy(event._inputs({event.INPUTS: text}))


def _distinct(count: int, size: int, fill: str = "a") -> list[str]:
    """count different entries of size ASCII characters each."""
    return [f"{index:03d}".ljust(size, fill) for index in range(count)]


class InputTests(unittest.TestCase):
    """The inputs as the runner hands them over, and every rule they obey."""

    def _refuses(self, message: str, given: dict[str, str] | None = None, raw: str | None = None) -> str:
        with self.assertRaises(event.InvalidEvent) as caught:
            _policy(given, raw)
        self.assertIn(message, str(caught.exception))
        return str(caught.exception)

    def test_defaults_turn_every_check_on(self) -> None:
        self.assertEqual(
            _policy(),
            event.Policy(
                require_model_attribution=True,
                co_author_trailers="error",
                agent_identities="error",
                hidden_unicode="error",
                unicode_homoglyphs="error",
                unicode_unusual_spaces="warn",
                exclude_paths=(),
                allowed_identities=(),
                additional_identities=(),
                additional_binary_extensions=frozenset(),
                additional_attribution_exemptions=frozenset(),
            ),
        )

    def test_every_input_is_read(self) -> None:
        policy = _policy(
            {
                "require-model-attribution": "true",
                "co-author-trailers": "warn",
                "agent-identities": "off",
                "hidden-unicode": "warn",
                "unicode-homoglyphs": "off",
                "unicode-unusual-spaces": "error",
                "unicode-exclude-paths": "tests/fixtures/\ndocs/odd.md\n",
                "allowed-identities": "github:Copilot\nemail:Bot@Example.com",
                "additional-identities": "email:agent@example.com",
                "additional-binary-extensions": "wasm\navif",
                "additional-attribution-exemptions": "release-bot[bot]",
            }
        )
        self.assertEqual(
            policy,
            event.Policy(
                require_model_attribution=True,
                co_author_trailers="warn",
                agent_identities="off",
                hidden_unicode="warn",
                unicode_homoglyphs="off",
                unicode_unusual_spaces="error",
                exclude_paths=("tests/fixtures/", "docs/odd.md"),
                allowed_identities=(("github", "copilot"), ("email", "bot@example.com")),
                additional_identities=(("email", "agent@example.com"),),
                additional_binary_extensions=frozenset({"wasm", "avif"}),
                additional_attribution_exemptions=frozenset({"release-bot[bot]"}),
            ),
        )

    def test_inherit_follows_hidden_unicode(self) -> None:
        # hidden-unicode: (homoglyphs, unusual spaces) when both inherit.
        inherited = {"error": ("error", "warn"), "warn": ("warn", "warn"), "off": ("off", "off")}
        for hidden, (homoglyphs, spaces) in inherited.items():
            with self.subTest(hidden=hidden):
                policy = _policy({"hidden-unicode": hidden})
                self.assertEqual(
                    (policy.unicode_homoglyphs, policy.unicode_unusual_spaces), (homoglyphs, spaces)
                )
            for override in ("error", "warn", "off"):
                with self.subTest(hidden=hidden, override=override):
                    policy = _policy(
                        {
                            "hidden-unicode": hidden,
                            "unicode-homoglyphs": override,
                            "unicode-unusual-spaces": override,
                        }
                    )
                    self.assertEqual(
                        (policy.unicode_homoglyphs, policy.unicode_unusual_spaces),
                        (override, override),
                    )

    def test_modes_accept_exactly_their_values(self) -> None:
        for name in ("co-author-trailers", "agent-identities", "hidden-unicode"):
            for value in ("inherit", "Error", "WARN", "", " off"):
                with self.subTest(name=name, value=value):
                    self._refuses(f"input {name} must be error, warn or off, got {value!r}", {name: value})
        for name in ("unicode-homoglyphs", "unicode-unusual-spaces"):
            for value in ("Inherit", "", "on"):
                with self.subTest(name=name, value=value):
                    self._refuses(f"input {name} must be inherit, error, warn or off", {name: value})
        self._refuses(
            "input require-model-attribution must be true or false",
            {"require-model-attribution": "inherit"},
        )

    def test_names_ignore_case(self) -> None:
        policy = _policy({"HIDDEN-UNICODE": "warn", "Unicode-Homoglyphs": "error"})
        self.assertEqual((policy.hidden_unicode, policy.unicode_homoglyphs), ("warn", "error"))
        self._refuses("input hidden-unicode must be", {"Hidden-Unicode": "Warn"})

    def test_unknown_names_fail(self) -> None:
        self.assertEqual(
            self._refuses("unknown input", {"require-model-attributionn": "true"}),
            "unknown input(s): 'require-model-attributionn'",
        )
        # A known name with an empty value of another input is still unknown.
        self._refuses("unknown input(s): 'hidden_unicode'", {"hidden_unicode": ""})

    def test_more_than_ten_unknown_names_are_counted(self) -> None:
        names = [f"typo{index:02d}" for index in reversed(range(12))]
        message = self._refuses("unknown input(s): ", {name: "" for name in names})
        shown = ", ".join(repr(f"typo{index:02d}") for index in range(10))
        self.assertEqual(message, f"unknown input(s): {shown} and 2 more")

    def test_messages_quote_at_most_80_characters(self) -> None:
        message = self._refuses("input hidden-unicode must be", {"hidden-unicode": "x" * 1000})
        self.assertTrue(message.endswith("got '" + "x" * 80 + "'..."), message)
        message = self._refuses("unknown input(s): ", {"y" * 1000: ""})
        self.assertEqual(message, "unknown input(s): '" + "y" * 80 + "'...")

    def test_workflow_command_syntax_in_a_value_stays_inert(self) -> None:
        stdout = io.StringIO()
        value = "warn\n::warning::spoofed\r\n##[error]spoofed" + "z" * 200
        environ = {event.INPUTS: json.dumps(runner_inputs({"hidden-unicode": value}))}
        self.assertEqual(main(environ, stdout=stdout), 1)
        output = stdout.getvalue()
        self.assertEqual(foreign_commands(output), [])
        self.assertIn("::error title=agent-guardrails::input hidden-unicode must be", output)
        self.assertNotIn("z" * 81, output)

    def test_malformed_transport_fails(self) -> None:
        good = runner_inputs()
        cases = {
            "internal error: CA_INPUTS is not JSON": "{",
            "internal error: CA_INPUTS is not an object of strings": "[]",
            "internal error: CA_INPUTS has no input agent-identities": json.dumps(
                {name: value for name, value in good.items() if name != "agent-identities"}
            ),
        }
        for message, raw in cases.items():
            with self.subTest(message):
                self._refuses(message, raw=raw)
        for value in (True, None, 1, ["error"], {"a": "b"}):
            with self.subTest(value=value):
                self._refuses(
                    "internal error: CA_INPUTS is not an object of strings",
                    raw=json.dumps({**good, "hidden-unicode": value}),
                )
        with self.assertRaisesRegex(event.InvalidEvent, "^internal error: CA_INPUTS is not set$"):
            event._inputs({})

    def test_a_name_given_twice_fails(self) -> None:
        text = json.dumps(runner_inputs())
        duplicate_key = text[:-1] + ', "hidden-unicode": "warn"}'
        self.assertEqual(
            self._refuses("given more than once", raw=duplicate_key),
            "input hidden-unicode is given more than once",
        )
        collision = json.dumps({**runner_inputs(), "Hidden-Unicode": "warn"})
        self.assertEqual(
            self._refuses("given more than once", raw=collision),
            "input hidden-unicode is given more than once",
        )

    def test_list_lines(self) -> None:
        policy = _policy(
            {
                "unicode-exclude-paths": " \t a/ \t\r\n\r\n \t \nb\r\n",
                "additional-binary-extensions": "\n\nwasm\n\n",
            }
        )
        self.assertEqual(policy.exclude_paths, ("a/", "b"))
        self.assertEqual(policy.additional_binary_extensions, frozenset({"wasm"}))
        for value in ("a\rb", "a\r\r\n", "\r\ra"):
            with self.subTest(value=value):
                self._refuses(
                    "input unicode-exclude-paths line 1: a carriage return inside the line",
                    {"unicode-exclude-paths": value},
                )
        # Only ASCII spaces and tabs are trimmed.
        self._refuses(
            "input additional-binary-extensions line 1: expected a lowercase extension",
            {"additional-binary-extensions": "wasm\u00a0"},
        )

    def test_exclude_paths(self) -> None:
        valid = ["a", "a/b.txt", "a/", ".github/x/", ".hidden", "with space/x", "caf\u00e9/", "a..b"]
        policy = _policy({"unicode-exclude-paths": "\n".join(valid)})
        self.assertEqual(policy.exclude_paths, tuple(valid))
        rejected = {
            "/abs": "an absolute path",
            "/": "an absolute path",
            "a\\b": "a backslash",
            "a\x01b": "a control character",
            "a\x7fb": "a control character",
            "a\x85b": "a control character",
            "a//b": "an empty path component",
            "a//": "an empty path component",
            "./a": "a . or .. component",
            "a/./b": "a . or .. component",
            "a/..": "a . or .. component",
            "../a": "a . or .. component",
            "a/../": "a . or .. component",
            "*.md": "glob syntax",
            "a?": "glob syntax",
            "[a]": "glob syntax",
            "{a,b}": "glob syntax",
            "!a": "glob syntax",
            ":(top)a": "pathspec magic",
        }
        for entry, problem in rejected.items():
            with self.subTest(entry=entry):
                self._refuses(
                    f"input unicode-exclude-paths line 2: {problem} is not supported",
                    {"unicode-exclude-paths": f"ok\n{entry}"},
                )

    def test_selectors(self) -> None:
        valid = [
            "email:a.b+c%d_e-f@sub.example-host.org",
            "github:Copilot",
            "github:some-app[bot]",
            "email:" + "l" * 64 + "@" + "d" * 190,
            "github:" + "h" * 39,
            "github:" + "g" * 39 + "[bot]",
        ]
        policy = _policy({"allowed-identities": "\n".join(valid)})
        self.assertEqual(len(policy.allowed_identities), len(valid))
        rejected = [
            "Email:a@b.org",
            "EMAIL:a@b.org",
            "email:a",
            "email:@b.org",
            "email:a@",
            "email:a@b@c",
            "email:a b@c.org",
            "email:" + "l" * 65 + "@b.org",
            "email:a@" + "d" * 191,
            "github:",
            "github:a_b",
            "github:" + "h" * 40,
            "github:bot[BOT]",
            "github:caf\u00e9",
            "copilot@github.com",
            "Copilot",
        ]
        for name in ("allowed-identities", "additional-identities"):
            for entry in rejected:
                with self.subTest(name=name, entry=entry):
                    self._refuses(
                        f"input {name} line 1: expected email:<address> or github:<handle>",
                        {name: entry},
                    )

    def test_other_list_grammars(self) -> None:
        policy = _policy(
            {
                "additional-binary-extensions": "x\n" + "a" * 16 + "\nmp4a",
                "additional-attribution-exemptions": "a\n" + "b" * 39 + "\n" + "c" * 39 + "[bot]\nBot-1",
            }
        )
        self.assertEqual(policy.additional_binary_extensions, frozenset({"x", "a" * 16, "mp4a"}))
        self.assertEqual(
            policy.additional_attribution_exemptions,
            frozenset({"a", "b" * 39, "c" * 39 + "[bot]", "Bot-1"}),
        )
        for entry in ("a" * 17, ".wasm", "WASM", "tar.gz", "w-a"):
            with self.subTest(entry=entry):
                self._refuses(
                    "input additional-binary-extensions line 1: expected a lowercase extension without a dot",
                    {"additional-binary-extensions": entry},
                )
        for entry in ("b" * 40, "a_b", "a b", "bot[BOT]", "@bot", "b" * 40 + "[bot]"):
            with self.subTest(entry=entry):
                self._refuses(
                    "input additional-attribution-exemptions line 1: expected a GitHub login",
                    {"additional-attribution-exemptions": entry},
                )

    def test_duplicate_entries_fail_in_every_list(self) -> None:
        cases = {
            "unicode-exclude-paths": "docs/\nsrc/\ndocs/",
            "allowed-identities": "github:Copilot\nemail:x@y.org\ngithub:copilot",
            "additional-identities": "email:A@x.org\ngithub:z\nemail:a@X.ORG",
            "additional-binary-extensions": "wasm\navif\nwasm",
            "additional-attribution-exemptions": "bot-a\nbot-b\nbot-a",
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    self._refuses("repeats", {name: value}), f"input {name} line 3 repeats line 1"
                )
        # Logins compare exactly; paths compare literally.
        self.assertEqual(
            _policy({"additional-attribution-exemptions": "Bot-a\nbot-a"}).additional_attribution_exemptions,
            frozenset({"Bot-a", "bot-a"}),
        )
        self.assertEqual(_policy({"unicode-exclude-paths": "docs/\ndocs"}).exclude_paths, ("docs/", "docs"))

    def test_identity_lists_may_not_overlap(self) -> None:
        overlapping = [
            ("github:Copilot", "email:123+copilot@users.noreply.github.com"),
            ("email:foo@users.noreply.github.com", "github:Foo"),
            ("email:A@x.org", "email:a@X.org"),
            ("github:some-app[bot]", "github:Some-App[bot]"),
        ]
        for allowed, additional in overlapping:
            for first, second in ((allowed, additional), (additional, allowed)):
                with self.subTest(allowed=first, additional=second):
                    self.assertEqual(
                        self._refuses(
                            "match the same address",
                            {
                                "allowed-identities": f"github:other\n{first}",
                                "additional-identities": second,
                            },
                        ),
                        "input allowed-identities line 2 and input additional-identities "
                        "line 1 match the same address",
                    )
        apart = [
            ("github:foo", "email:x123+foo@users.noreply.github.com"),
            ("github:foo", "email:123+foo@users.noreply.github.com.evil"),
            ("github:foo", "email:foo@github.com"),
            ("github:foo", "github:foo[bot]"),
            ("github:foo", "email:1+2+foo@users.noreply.github.com"),
            ("email:a@x.org", "email:a@x.org.evil"),
        ]
        for allowed, additional in apart:
            with self.subTest(allowed=allowed, additional=additional):
                _policy({"allowed-identities": allowed, "additional-identities": additional})

    def test_settings_of_a_check_that_is_off_are_still_checked(self) -> None:
        cases = [
            ({"co-author-trailers": "off", "agent-identities": "off", "allowed-identities": "bad"},
             "input allowed-identities line 1"),
            ({"agent-identities": "off", "additional-identities": "x"}, "input additional-identities line 1"),
            ({"hidden-unicode": "off", "unicode-exclude-paths": "/abs"}, "input unicode-exclude-paths line 1"),
            ({"require-model-attribution": "false", "additional-attribution-exemptions": "a b"},
             "input additional-attribution-exemptions line 1"),
        ]
        for given, message in cases:
            with self.subTest(message):
                self._refuses(message, given)

    def test_bounds(self) -> None:
        total = "the list inputs hold more than 16,384 bytes together"
        # Exactly 16,384 bytes: 31 entries of 511 bytes, one of 512, and 31 LFs.
        full = "\n".join(_distinct(31, 511) + ["999".ljust(512, "b")])
        self.assertEqual(len(full.encode()), 16_384)
        self.assertEqual(len(_policy({"unicode-exclude-paths": full}).exclude_paths), 32)
        self._refuses(total, {"unicode-exclude-paths": full + "\n"})
        # The bound is on the five lists together.
        paths = "\n".join(_distinct(31, 512))
        extensions = "\n".join(_distinct(28, 16, "x") + ["zzzzzz"])
        self.assertEqual((len(paths), len(paths) + len(extensions)), (15_902, 16_384))
        _policy({"unicode-exclude-paths": paths, "additional-binary-extensions": extensions})
        self._refuses(
            total, {"unicode-exclude-paths": paths, "additional-binary-extensions": extensions + "\n"}
        )

        many = "more than 64 entries"
        self.assertEqual(
            len(_policy({"additional-binary-extensions": "\n\n".join(_distinct(64, 3))})
                .additional_binary_extensions),
            64,
        )
        self._refuses(
            f"input additional-binary-extensions has {many}",
            {"additional-binary-extensions": "\n".join(_distinct(65, 3))},
        )
        for name in ("unicode-exclude-paths", "allowed-identities", "additional-identities",
                     "additional-attribution-exemptions"):
            entries = {
                "unicode-exclude-paths": _distinct(65, 4),
                "allowed-identities": [f"github:h{index}" for index in range(65)],
                "additional-identities": [f"email:e{index}@x.org" for index in range(65)],
                "additional-attribution-exemptions": [f"l{index}" for index in range(65)],
            }[name]
            with self.subTest(name=name):
                _policy({name: "\n".join(entries[:64])})
                self._refuses(f"input {name} has {many}", {name: "\n".join(entries)})

        long = "input unicode-exclude-paths line 1: longer than 512 bytes"
        self.assertEqual(_policy({"unicode-exclude-paths": "a" * 512}).exclude_paths, ("a" * 512,))
        self._refuses(long, {"unicode-exclude-paths": "a" * 513})
        # Bytes, not characters.
        self.assertEqual(
            _policy({"unicode-exclude-paths": "\u00e9" * 256}).exclude_paths, ("\u00e9" * 256,)
        )
        self._refuses(long, {"unicode-exclude-paths": "\u00e9" * 256 + "a"})

    def test_bytes_that_are_not_utf8_fail(self) -> None:
        raw = json.dumps(runner_inputs()).replace(
            '"unicode-exclude-paths": ""', '"unicode-exclude-paths": "a\\ud800"'
        )
        self.assertEqual(
            self._refuses("not valid UTF-8", raw=raw), "input unicode-exclude-paths is not valid UTF-8"
        )


if __name__ == "__main__":
    unittest.main()
