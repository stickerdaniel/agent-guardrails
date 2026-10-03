# agent-guardrails

[![CI](https://github.com/stickerdaniel/agent-guardrails/actions/workflows/ci.yml/badge.svg)](https://github.com/stickerdaniel/agent-guardrails/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A GitHub Action that fails a pull request when a coding agent signs it. It looks for a `Co-authored-by` trailer naming an agent in any commit message or in the PR body, and for an agent's address as a commit's author or committer. Squash merging can carry these trailers into the default branch. It checks the PR title and body, commit messages, and added text lines for hidden Unicode: invisible characters such as zero-width spaces, bidi controls and tag characters, private-use characters, and letters from another script that pass for Latin. Unusual spaces only warn. Optionally, it also requires the PR body to end with a line such as `Generated with Claude Opus 5`.

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

Pin the commit SHA of a release. Make `check-bot-coauthors` a required status check, and keep the job free of an `if:`, because GitHub counts a skipped job as passing.

| Input | Default | Values | Effect |
| --- | --- | --- | --- |
| `require-model-attribution` | `false` | `true`, `false` | With `true`, the last non-empty line of the PR body must read `Generated with <model>`, or the detailed `Generated with <model> for <job> in <harness>.` Pull requests opened by `renovate[bot]` and `dependabot[bot]` are exempt. |
| `hidden-unicode` | `error` | `error`, `warn` | With `warn`, hidden Unicode findings are warnings and do not fail the run. Every other finding still does, including a file that cannot be scanned. |

Any other value fails the run.

## Changed files

Text is checked on added lines compared with the merge base; a moved file counts as added. Git judges binary content on the **new side**, independently of the old file, when the patch is binary or has no text hunk. A binary file fails unless its extension is `png`, `jpg`, `jpeg`, `gif`, `webp`, `ico`, `pdf`, `zip`, `gz`, `woff`, `woff2`, `ttf`, `otf`, `mp4`, `mov`, `mp3` or `wav`; those and submodules are logged as unscanned. Git's binary classification includes NUL-containing files and files above its big-file threshold. An invalid UTF-8 path or added line fails; a mode-only change also checks the full new file for binary content and valid UTF-8, without treating unchanged lines as additions. Otherwise, unchanged text is not validated. PR `.gitattributes` cannot suppress this check.

A run stops with an error, even with `hidden-unicode: warn`, if Git exceeds 64 MiB of standard output, two million output records or 600 seconds; scanning also stops past 100 million work units, 1,000 findings, four million characters on one line or 100,000 suspicious characters on one line. GitHub displays at most ten error and ten warning annotations per step; findings beyond that display limit still affect the exit status and remain in the log.

## Trust model

The workflow and the pinned action come from the base repository's default branch, never from the pull request. The action fetches the base branch and `refs/pull/<n>/head` from the base repository into a temporary bare repository and reads commits, the diff, and the PR title and body as data, without checking out or running anything from the pull request. The job token goes to git through the environment and is masked in the log, and every doubt fails the run, including a head that moved since the event and a range with no commits.

## Requirements

Use a Linux or macOS runner with Python 3.10 or newer and git 2.31 or newer on `PATH`. GitHub-hosted Ubuntu runners have both. Windows runners are not supported.

## License

MIT. `agent_guardrails/hidden.py` comes from [no-ai-marks](https://github.com/mishan/no-ai-marks); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
