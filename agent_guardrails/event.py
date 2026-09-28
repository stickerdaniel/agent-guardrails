"""Validate the inputs and the event payload before anything touches git.

GitHub authenticates the event. The pull request's author still influences
the head SHA and branch by pushing and the base by retargeting, so every value
read here is validated data. None of it selects the policy, the code that
runs, or where the credential goes. The guard detects misuse; it does not make
an untrusted caller trusted.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

# action.yml maps each input and context value to one of these names.
REQUIRE_MODEL_ATTRIBUTION = "CA_REQUIRE_MODEL_ATTRIBUTION"
HIDDEN_UNICODE = "CA_HIDDEN_UNICODE"
TOKEN = "CA_TOKEN"
SERVER_URL = "CA_SERVER_URL"
REPOSITORY = "CA_REPOSITORY"
EVENT_NAME = "CA_EVENT_NAME"
# Set by the runner.
EVENT_PATH = "GITHUB_EVENT_PATH"
RUNNER_TEMP = "RUNNER_TEMP"

_BOOLEANS = {"true": True, "false": False}
_HIDDEN_UNICODE_MODES = ("error", "warn")
_SHA = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# The token and server URL end up in a header and a config key: printable
# ASCII without spaces.
_PLAIN = re.compile(r"[!-~]+")


class InvalidEvent(Exception):
    """The run cannot be checked; the message says why."""


@dataclass(frozen=True)
class Settings:
    require_model_attribution: bool
    hidden_unicode: str
    token: str
    server_url: str
    repository: str
    path: str
    runner_temp: str | None
    number: int
    base_ref: str
    base_sha: str
    head_sha: str
    body: str | None
    login: str


def _choice(environ: Mapping[str, str], name: str, input_name: str, allowed) -> str:
    value = environ.get(name, "")
    if value not in allowed:
        expected = " or ".join(allowed)
        raise InvalidEvent(f"input {input_name} must be {expected}, got {value!r}")
    return value


def _object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidEvent(f"{where} is not an object")
    return value


def _sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidEvent(f"{where} is not a 40-character hex commit SHA")
    return value


def _read_payload(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise InvalidEvent(
            f"cannot read the event payload: {type(error).__name__}"
        ) from None


def load(environ: Mapping[str, str]) -> Settings:
    """Read and validate everything the check needs, or raise InvalidEvent."""
    require = _choice(
        environ, REQUIRE_MODEL_ATTRIBUTION, "require-model-attribution", _BOOLEANS
    )
    hidden_unicode = _choice(
        environ, HIDDEN_UNICODE, "hidden-unicode", _HIDDEN_UNICODE_MODES
    )

    event_name = environ.get(EVENT_NAME, "")
    if event_name != "pull_request_target":
        raise InvalidEvent(
            f"this action runs only under pull_request_target, not {event_name!r}. "
            "Under pull_request the workflow comes from the pull request itself."
        )

    token = environ.get(TOKEN, "")
    if not _PLAIN.fullmatch(token):
        raise InvalidEvent("the job token is missing or malformed")
    server_url = environ.get(SERVER_URL, "").rstrip("/")
    if not _PLAIN.fullmatch(server_url):
        raise InvalidEvent("github.server_url is missing or malformed")
    repository = environ.get(REPOSITORY, "")
    if not _REPOSITORY.fullmatch(repository):
        raise InvalidEvent("github.repository is missing or malformed")
    event_path = environ.get(EVENT_PATH, "")
    if not event_path:
        raise InvalidEvent(f"{EVENT_PATH} is not set")

    event = _object(_read_payload(event_path), "the event payload")
    pull = _object(event.get("pull_request"), "pull_request")
    number = pull.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise InvalidEvent("pull_request.number is not a positive integer")
    base = _object(pull.get("base"), "pull_request.base")
    head = _object(pull.get("head"), "pull_request.head")
    base_ref = base.get("ref")
    # git check-ref-format --branch judges the rest before the fetch.
    if not isinstance(base_ref, str) or not base_ref:
        raise InvalidEvent("pull_request.base.ref is not a branch name")
    body = pull.get("body")
    if body is not None and not isinstance(body, str):
        raise InvalidEvent("pull_request.body must be a string or null")
    user = _object(pull.get("user"), "pull_request.user")
    login = user.get("login")
    if not isinstance(login, str):
        raise InvalidEvent("pull_request.user.login is not a string")

    return Settings(
        require_model_attribution=_BOOLEANS[require],
        hidden_unicode=hidden_unicode,
        token=token,
        server_url=server_url,
        repository=repository,
        path=environ.get("PATH") or os.defpath,
        runner_temp=environ.get(RUNNER_TEMP) or None,
        number=number,
        base_ref=base_ref,
        base_sha=_sha(base.get("sha"), "pull_request.base.sha"),
        head_sha=_sha(head.get("sha"), "pull_request.head.sha"),
        body=body,
        login=login,
    )
