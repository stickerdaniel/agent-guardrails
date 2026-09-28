# agent-guardrails

[![CI](https://github.com/stickerdaniel/agent-guardrails/actions/workflows/ci.yml/badge.svg)](https://github.com/stickerdaniel/agent-guardrails/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A GitHub Action that fails a pull request when a coding agent signs it. It looks for a `Co-authored-by` trailer naming an agent in any commit message or in the PR body, and for an agent's address as a commit's author or committer. Squash merging can carry these trailers into the default branch. Optionally, it also requires the PR body to end with a line such as `Generated with Claude Opus 5`.

## Usage

```yaml
name: Agent Guardrails
on:
  pull_request_target:
    types: [opened, synchronize, reopened, edited]
permissions:
  contents: read
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  check-bot-coauthors:
    runs-on: ubuntu-latest
    steps:
      - uses: stickerdaniel/agent-guardrails@<sha> # v1.0.0
        with:
          require-model-attribution: false
```

Pin the commit SHA of a release. Make `check-bot-coauthors` a required status check, and keep the job free of an `if:`, because GitHub counts a skipped job as passing.

| Input | Default | Values | Effect |
| --- | --- | --- | --- |
| `require-model-attribution` | `false` | `true`, `false` | With `true`, the last non-empty line of the PR body must read `Generated with <model>`, or the detailed `Generated with <model> for <job> in <harness>.` Pull requests opened by `renovate[bot]` and `dependabot[bot]` are exempt. |
| `hidden-unicode` | `error` | `error`, `warn` | Reserved for the hidden Unicode check. The action validates the value but no check reads it yet. |

Any other value fails the run.

## Trust model

The workflow and the pinned action come from the base repository's default branch, never from the pull request. The action fetches the base branch and `refs/pull/<n>/head` from the base repository into a temporary bare repository and reads commits and the PR body as data, without checking out or running anything from the pull request. The job token goes to git through the environment and is masked in the log, and every doubt fails the run, including a head that moved since the event and a range with no commits.

## Requirements

The runner needs Python 3.10 or newer and git 2.31 or newer on `PATH`. GitHub-hosted Ubuntu runners have both.

## License

MIT. `agent_guardrails/hidden.py` comes from [no-ai-marks](https://github.com/mishan/no-ai-marks); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
