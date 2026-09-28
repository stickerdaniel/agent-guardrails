# Contributing

Thanks for helping improve agent-guardrails. It runs with a token on untrusted pull requests, so a few rules are stricter than usual.

## Development

```bash
python3 -m unittest discover -v
```

The tests need Python 3.10 or newer and git 2.31 or newer, and nothing else. They build real git repositories in a temporary directory and start `run.py` the way `action.yml` does. CI runs them on Python 3.10 and 3.13.

To lint the workflows locally:

```bash
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest -no-color
```

## What we do not accept

- Runtime or test dependencies. The action is standard-library Python and git.
- Edits to `agent_guardrails/hidden.py`. It is a verbatim copy of upstream no-ai-marks; update it by copying a newer upstream revision and naming that commit in its header.
- Changes that check out, import, or run anything from the pull request.
- Inputs that widen or narrow the identity list per repository, or that turn a check off.
- Exemptions from the model attribution check beyond `renovate[bot]` and `dependabot[bot]`.

## Releasing

Releases are `vX.Y.Z` tags on a green `main` commit with a GitHub release. A published version is never moved or reused; a fix ships as a new patch release. A removed or renamed input, a stricter default, or a new check that fails by default is a major version.

## Pull requests

Keep PRs small and single-purpose. Describe the problem, the change, and how you verified it. A change to a check needs a failing fixture and a clean counterpart in `tests/`. The template's last line is the model attribution this repository requires of its own pull requests.
