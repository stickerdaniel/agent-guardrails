# agent-guardrails

[![CI](https://github.com/stickerdaniel/agent-guardrails/actions/workflows/ci.yml/badge.svg)](https://github.com/stickerdaniel/agent-guardrails/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A GitHub Action that fails pull requests signed by coding agents or carrying hidden Unicode.

## What it catches

- **Agents signing themselves into your history.** A `Co-authored-by` trailer naming a coding agent, in a commit message or the PR body. Squash merging copies it into your default branch.
- **Commits made under an agent's identity.** A commit authored or committed by a coding agent's address, such as `noreply@anthropic.com`.
- **Text reviewers cannot see.** Zero-width spaces, bidi controls, tag characters, private-use characters, and letters from another script that pass for Latin, in the title, body, commit messages, and added lines. Unusual spaces only warn.
- **Undisclosed model use (opt-in).** A PR body whose last line is not `Generated with <model>`, such as `Generated with Claude Opus 5`.

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
      - uses: stickerdaniel/agent-guardrails@a3508d7320e64370b878359b1f4ae541b507224f # v1.0.0
        with:
          require-model-attribution: false
```

Pin a release SHA. Make `check-bot-coauthors` a required check and give the job no `if:`, because GitHub counts a skipped job as passing.

## Configuration

| Input | Default | Effect |
| --- | --- | --- |
| `require-model-attribution` | `false` | `true` turns on the attribution check. PRs opened by `renovate[bot]` and `dependabot[bot]` are exempt. |
| `hidden-unicode` | `error` | `warn` reports hidden Unicode as warnings instead of failures. |

The trailer and identity checks always run. Any other input value fails the run.

## How it works

The workflow and the pinned action come from your default branch. The action fetches the PR into a temporary repository and reads its commits, diff, title, and body as data, without checking out or running anything from the pull request. Every doubt fails the run, including a head that moved since the event.

<details>
<summary>Limits and edge cases</summary>

- Added lines are compared with the merge base. A moved file counts as added.
- A binary file fails unless its extension is `png`, `jpg`, `jpeg`, `gif`, `webp`, `ico`, `pdf`, `zip`, `gz`, `woff`, `woff2`, `ttf`, `otf`, `mp4`, `mov`, `mp3` or `wav`. Those and submodules are logged as unscanned. The PR's `.gitattributes` cannot change this.
- An invalid UTF-8 path or added line fails.
- The run fails, even with `hidden-unicode: warn`, past 64 MiB of Git output, two million output records, 600 seconds of Git, 100 million scan steps, 1,000 findings, or four million characters or 100,000 suspicious characters on one line.

</details>

## Requirements

A Linux or macOS runner with Python 3.10+ and git 2.31+ on `PATH`. GitHub-hosted Ubuntu runners have both. Windows is not supported.

## License

MIT. `agent_guardrails/hidden.py` comes from [no-ai-marks](https://github.com/mishan/no-ai-marks); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
