"""Validate the inputs and the event payload before anything touches git.

GitHub authenticates the event. The pull request's author still influences
the head SHA and branch by pushing and the base by retargeting, so every value
read here is validated data. None of it selects the policy, the code that
runs, or where the credential goes. The guard detects misuse; it does not make
an untrusted caller trusted.

The policy comes only from the inputs, which the caller's workflow sets. The
action trusts them as it trusts that workflow, and checks their form, never
their wisdom.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

# action.yml maps the whole inputs context and each context value to one of
# these names.
INPUTS = "CA_INPUTS"
TOKEN = "CA_TOKEN"
SERVER_URL = "CA_SERVER_URL"
REPOSITORY = "CA_REPOSITORY"
EVENT_NAME = "CA_EVENT_NAME"
# Set by the runner.
EVENT_PATH = "GITHUB_EVENT_PATH"
RUNNER_TEMP = "RUNNER_TEMP"

# Every input action.yml declares. The runner matches a caller's name to a
# declared one ignoring case, keeps the caller's spelling, and hands on a
# name that matches none with only a warning, so CA_INPUTS holds whatever the
# caller wrote and the declared defaults it did not override.
INPUT_NAMES = (
    "require-model-attribution",
    "co-author-trailers",
    "agent-identities",
    "hidden-unicode",
    "unicode-homoglyphs",
    "unicode-unusual-spaces",
    "unicode-exclude-paths",
    "allowed-identities",
    "additional-identities",
    "additional-binary-extensions",
    "additional-attribution-exemptions",
)
_LIST_INPUTS = INPUT_NAMES[6:]

_BOOLEANS = {"true": True, "false": False}
_MODES = ("error", "warn", "off")
_OVERRIDES = ("inherit", *_MODES)
_SHA = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# The token and server URL end up in a header and a config key: printable
# ASCII without spaces.
_PLAIN = re.compile(r"[!-~]+")

# How much configuration one run reads, and how much of a value a message
# quotes. The total is measured on the raw values of the five list inputs.
_LIST_BYTES = 16_384
_LIST_ENTRIES = 64
_ENTRY_BYTES = 512
_QUOTE_LIMIT = 80
_UNKNOWN_SHOWN = 10

_EXTENSION = re.compile(r"[a-z0-9]{1,16}")
_LOGIN = re.compile(r"[A-Za-z0-9-]{1,39}(?:\[bot\])?")
_EMAIL = re.compile(r"email:([A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190})")
_GITHUB = re.compile(r"github:([A-Za-z0-9-]{1,39}(?:\[bot\])?)")
_NOREPLY = "@users.noreply.github.com"
_DIGITS = re.compile(r"[0-9]+")
# C0, DEL and C1.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_GLOB = re.compile(r"[*?\[\]{}!]")


class InvalidEvent(Exception):
    """The run cannot be checked; the message says why."""


@dataclass(frozen=True)
class Policy:
    """What the caller asked the checks to do. Each mode is error, warn or
    off, with inherit already resolved. The two sets hold only what the
    caller adds to the built-in ones, which stay in gitdata and rules."""

    require_model_attribution: bool
    co_author_trailers: str
    agent_identities: str
    hidden_unicode: str
    unicode_homoglyphs: str
    unicode_unusual_spaces: str
    exclude_paths: tuple[str, ...]
    allowed_identities: tuple[tuple[str, str], ...]
    additional_identities: tuple[tuple[str, str], ...]
    additional_binary_extensions: frozenset[str]
    additional_attribution_exemptions: frozenset[str]


@dataclass(frozen=True)
class Settings:
    policy: Policy
    token: str
    server_url: str
    repository: str
    path: str
    runner_temp: str | None
    number: int
    base_ref: str
    base_sha: str
    head_sha: str
    title: str
    body: str | None
    login: str


def selects(selector: tuple[str, str], address: str) -> bool:
    """Whether an identity selector matches an address. Only an ASCII address
    can match, and case is ignored only for ASCII letters. A github selector
    matches the handle's noreply address, bare or with a numeric id."""
    if not address.isascii():
        return False
    address = address.lower()
    kind, value = selector
    if kind == "email":
        return address == value
    if address == value + _NOREPLY:
        return True
    prefix, plus, rest = address.partition("+")
    return bool(plus) and _DIGITS.fullmatch(prefix) is not None and rest == value + _NOREPLY


def _shown(value: str) -> str:
    """value quoted, and cut to _QUOTE_LIMIT characters, for a message."""
    if len(value) <= _QUOTE_LIMIT:
        return repr(value)
    return repr(value[:_QUOTE_LIMIT]) + "..."


def _canonical(key: str) -> str | None:
    """The declared input a key names, ignoring ASCII case as the runner
    does, or None."""
    name = key.lower() if key.isascii() else None
    return name if name in INPUT_NAMES else None


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            name = _canonical(key)
            raise InvalidEvent(f"input {name or _shown(key)} is given more than once")
        result[key] = value
    return result


def _inputs(environ: Mapping[str, str]) -> dict[str, str]:
    """Every declared input under its declared name. A name that matches
    none fails the run: the runner would only warn, and a misspelt setting
    would leave its check at the default in silence."""
    raw = environ.get(INPUTS)
    if raw is None:
        raise InvalidEvent(f"internal error: {INPUTS} is not set")
    try:
        given = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except ValueError:
        raise InvalidEvent(f"internal error: {INPUTS} is not JSON") from None
    if not isinstance(given, dict) or not all(isinstance(value, str) for value in given.values()):
        raise InvalidEvent(f"internal error: {INPUTS} is not an object of strings")
    unknown = sorted(key for key in given if _canonical(key) is None)
    if unknown:
        shown = ", ".join(_shown(key) for key in unknown[:_UNKNOWN_SHOWN])
        more = len(unknown) - _UNKNOWN_SHOWN
        raise InvalidEvent(
            f"unknown input(s): {shown}" + (f" and {more} more" if more > 0 else "")
        )
    inputs: dict[str, str] = {}
    for key, value in given.items():
        name = _canonical(key)
        if name in inputs:
            raise InvalidEvent(f"input {name} is given more than once")
        inputs[name] = value
    for name in INPUT_NAMES:
        if name not in inputs:
            raise InvalidEvent(f"internal error: {INPUTS} has no input {name}")
    return inputs


def _choice(inputs: Mapping[str, str], name: str, allowed: tuple[str, ...]) -> str:
    value = inputs[name]
    if value not in allowed:
        expected = ", ".join(allowed[:-1]) + " or " + allowed[-1]
        raise InvalidEvent(f"input {name} must be {expected}, got {_shown(value)}")
    return value


def _size(value: str, name: str) -> int:
    """UTF-8 bytes of value. A lone surrogate is refused, not replaced."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise InvalidEvent(f"input {name} is not valid UTF-8") from None


def _entries(inputs: Mapping[str, str], name: str) -> list[tuple[int, str]]:
    """(line number, entry) for each line of a list input that is not
    blank: split on LF, one trailing CR dropped, ASCII spaces and tabs
    trimmed."""
    entries = []
    for number, line in enumerate(inputs[name].split("\n"), 1):
        line = line[:-1] if line.endswith("\r") else line
        if "\r" in line:
            raise InvalidEvent(f"input {name} line {number}: a carriage return inside the line")
        entry = line.strip(" \t")
        if not entry:
            continue
        if _size(entry, name) > _ENTRY_BYTES:
            raise InvalidEvent(f"input {name} line {number}: longer than {_ENTRY_BYTES} bytes")
        entries.append((number, entry))
    if len(entries) > _LIST_ENTRIES:
        raise InvalidEvent(f"input {name} has more than {_LIST_ENTRIES} entries")
    return entries


def _first(seen: dict, key: object, name: str, number: int) -> None:
    """Note where key first appears in a list input; a repeat fails."""
    if key in seen:
        raise InvalidEvent(f"input {name} line {number} repeats line {seen[key]}")
    seen[key] = number


def _path_problem(entry: str) -> str | None:
    """What keeps entry from being a literal repository path, or None. A
    trailing slash marks a directory."""
    if entry.startswith("/"):
        return "an absolute path"
    if "\\" in entry:
        return "a backslash"
    if _CONTROL.search(entry):
        return "a control character"
    if entry.startswith(":"):
        return "pathspec magic"
    if _GLOB.search(entry):
        return "glob syntax"
    parts = (entry[:-1] if entry.endswith("/") else entry).split("/")
    if "" in parts:
        return "an empty path component"
    if "." in parts or ".." in parts:
        return "a . or .. component"
    return None


def _paths(inputs: Mapping[str, str]) -> tuple[str, ...]:
    name = "unicode-exclude-paths"
    seen: dict[str, int] = {}
    for number, entry in _entries(inputs, name):
        problem = _path_problem(entry)
        if problem:
            raise InvalidEvent(f"input {name} line {number}: {problem} is not supported")
        _first(seen, entry, name, number)
    return tuple(seen)


def _selectors(inputs: Mapping[str, str], name: str) -> list[tuple[int, tuple[str, str]]]:
    seen: dict[tuple[str, str], int] = {}
    for number, entry in _entries(inputs, name):
        for kind, grammar in (("email", _EMAIL), ("github", _GITHUB)):
            match = grammar.fullmatch(entry)
            if match:
                break
        else:
            raise InvalidEvent(
                f"input {name} line {number}: expected email:<address> or github:<handle>"
            )
        _first(seen, (kind, match[1].lower()), name, number)
    return [(number, selector) for selector, number in seen.items()]


def _overlap(one: tuple[str, str], other: tuple[str, str]) -> bool:
    """Whether two selectors can match the same address."""
    if one[0] == other[0]:
        return one[1] == other[1]
    (_, email), github = sorted((one, other))
    return selects(github, email)


def _literals(
    inputs: Mapping[str, str], name: str, grammar: re.Pattern, expected: str
) -> frozenset[str]:
    seen: dict[str, int] = {}
    for number, entry in _entries(inputs, name):
        if not grammar.fullmatch(entry):
            raise InvalidEvent(f"input {name} line {number}: expected {expected}")
        _first(seen, entry, name, number)
    return frozenset(seen)


def _policy(inputs: Mapping[str, str]) -> Policy:
    """Validate every input, those of a check that is off too, and resolve
    inherit."""
    require = _choice(inputs, "require-model-attribution", tuple(_BOOLEANS))
    trailers = _choice(inputs, "co-author-trailers", _MODES)
    identities = _choice(inputs, "agent-identities", _MODES)
    hidden = _choice(inputs, "hidden-unicode", _MODES)
    homoglyphs = _choice(inputs, "unicode-homoglyphs", _OVERRIDES)
    spaces = _choice(inputs, "unicode-unusual-spaces", _OVERRIDES)

    if sum(_size(inputs[name], name) for name in _LIST_INPUTS) > _LIST_BYTES:
        raise InvalidEvent(f"the list inputs hold more than {_LIST_BYTES:,} bytes together")
    paths = _paths(inputs)
    allowed = _selectors(inputs, "allowed-identities")
    additional = _selectors(inputs, "additional-identities")
    extensions = _literals(
        inputs, "additional-binary-extensions", _EXTENSION, "a lowercase extension without a dot"
    )
    logins = _literals(
        inputs, "additional-attribution-exemptions", _LOGIN, "a GitHub login"
    )
    # Either list winning would ignore an entry in silence.
    for allowed_line, allowed_selector in allowed:
        for additional_line, additional_selector in additional:
            if _overlap(allowed_selector, additional_selector):
                raise InvalidEvent(
                    f"input allowed-identities line {allowed_line} and input "
                    f"additional-identities line {additional_line} match the same address"
                )

    if homoglyphs == "inherit":
        homoglyphs = hidden
    # Unusual spaces are common in pasted prose, so inherited they only warn.
    if spaces == "inherit":
        spaces = "off" if hidden == "off" else "warn"
    return Policy(
        require_model_attribution=_BOOLEANS[require],
        co_author_trailers=trailers,
        agent_identities=identities,
        hidden_unicode=hidden,
        unicode_homoglyphs=homoglyphs,
        unicode_unusual_spaces=spaces,
        exclude_paths=paths,
        allowed_identities=tuple(selector for _, selector in allowed),
        additional_identities=tuple(selector for _, selector in additional),
        additional_binary_extensions=extensions,
        additional_attribution_exemptions=logins,
    )


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
    policy = _policy(_inputs(environ))

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
    title = pull.get("title")
    if not isinstance(title, str):
        raise InvalidEvent("pull_request.title is not a string")
    body = pull.get("body")
    if body is not None and not isinstance(body, str):
        raise InvalidEvent("pull_request.body must be a string or null")
    user = _object(pull.get("user"), "pull_request.user")
    login = user.get("login")
    if not isinstance(login, str):
        raise InvalidEvent("pull_request.user.login is not a string")

    return Settings(
        policy=policy,
        token=token,
        server_url=server_url,
        repository=repository,
        path=environ.get("PATH") or os.defpath,
        runner_temp=environ.get(RUNNER_TEMP) or None,
        number=number,
        base_ref=base_ref,
        base_sha=_sha(base.get("sha"), "pull_request.base.sha"),
        head_sha=_sha(head.get("sha"), "pull_request.head.sha"),
        title=title,
        body=body,
        login=login,
    )
