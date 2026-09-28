from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_guardrails import event

from .support import TOKEN

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

    def _load(self, payload: object = None, raw: str | None = None, **overrides: str) -> event.Settings:
        text = raw if raw is not None else json.dumps(_payload() if payload is None else payload)
        self.path.write_text(text, encoding="utf-8")
        environ = {
            "PATH": "/usr/bin",
            event.REQUIRE_MODEL_ATTRIBUTION: "true",
            event.HIDDEN_UNICODE: "error",
            event.TOKEN: TOKEN,
            event.SERVER_URL: "https://github.com",
            event.REPOSITORY: "owner/repo",
            event.EVENT_NAME: "pull_request_target",
            event.EVENT_PATH: str(self.path),
        }
        environ.update(overrides)
        return event.load(environ)

    def _rejects(self, message: str, payload: object = None, raw: str | None = None, **overrides: str) -> None:
        with self.assertRaises(event.InvalidEvent) as caught:
            self._load(payload, raw, **overrides)
        self.assertIn(message, str(caught.exception))

    def test_reads_body_from_event_file(self) -> None:
        settings = self._load()
        self.assertEqual(settings.title, "feat: Add x")
        self.assertEqual(settings.body, "Generated with GPT-5.6 for implementation in Claude Code.")
        self.assertEqual(settings.number, 7)
        self.assertEqual((settings.base_ref, settings.base_sha, settings.head_sha), ("main", _BASE, _HEAD))
        self.assertTrue(settings.require_model_attribution)
        self.assertEqual(settings.hidden_unicode, "error")

    def test_null_body_is_accepted_as_none(self) -> None:
        self.assertIsNone(self._load(_payload(body=None)).body)

    def test_inputs_accept_exactly_their_values(self) -> None:
        self.assertFalse(self._load(**{event.REQUIRE_MODEL_ATTRIBUTION: "false"}).require_model_attribution)
        self.assertEqual(self._load(**{event.HIDDEN_UNICODE: "warn"}).hidden_unicode, "warn")
        for value in ("yes", "True", "1", ""):
            with self.subTest(value=value):
                self._rejects("input require-model-attribution must be true or false",
                              **{event.REQUIRE_MODEL_ATTRIBUTION: value})
        for value in ("off", "Error", ""):
            with self.subTest(value=value):
                self._rejects("input hidden-unicode must be error or warn",
                              **{event.HIDDEN_UNICODE: value})

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


if __name__ == "__main__":
    unittest.main()
