# Contributing

Thanks for helping improve Post No Bills. It runs with a token on untrusted pull requests, so a few rules are stricter than usual.

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
- Edits to `post_no_bills/hidden.py`. It is a verbatim copy of upstream no-ai-marks; update it by copying a newer upstream revision and naming that commit in its header.
- Changes that check out, import, or run anything from the pull request.
- Policy from anywhere but validated action inputs in the trusted default-branch workflow. Inputs may set check severities, literal exclusions, and identity or attribution exceptions; nothing in the pull request may choose them, and no input may make an unreadable file or a reached limit pass.

## Releasing

Releases are lightweight `vX.Y.Z` tags on the current green `main` tip with an immutable GitHub release. A published version is never moved or reused; a fix ships as a new patch release. A removed or renamed input, a stricter default, or a new check that fails by default is a major version.

Before tagging, ensure no merge is queued. Keep `main` and the new tag unchanged until the release workflow and its readback finish. Then verify that both still name the released commit and that the release is immutable. A mismatch stops delivery and requires investigation; the workflow's last ref check is not an atomic publication lock.

GitHub offers no API to list a release on GitHub Marketplace. After the readback, open the release's edit page, select **Publish this Action to the GitHub Marketplace**, and update the release.

## Pull requests

Keep PRs small and single-purpose. Describe the problem, the change, and how you verified it. A change to a check needs a failing fixture and a clean counterpart in `tests/`. The template's last line is the model attribution this repository requires of its own pull requests.
