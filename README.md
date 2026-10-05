# Post No Bills

[![CI](https://github.com/stickerdaniel/post-no-bills/actions/workflows/ci.yml/badge.svg)](https://github.com/stickerdaniel/post-no-bills/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Agents write the code, people sign it. Models are disclosed in the PR body.

<br>

<p>
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/hero-dark.png">
  <img alt="Left, agent trailers merged, meh: the Contributors list shows 4, stickerdaniel, claude, cursoragent and codex, after commit messages with Co-authored-by trailers for Claude, Cursor Agent and Codex. Right, show your contributors instead: the list shows stickerdaniel, open, vip and perf, and the PR body ends with Generated with Claude Opus 5.5 for implementation and GPT-6 Pro for review in Claude Code via T3 Code." src="docs/hero-light.png" width="800">
</picture>
</p>

Coding agents love to add `Co-authored-by:` trailers to your commit messages. Once one reaches your default branch, your Contributors list shows the agent's logo, free advertising for its vendor. Put your contributors first. Post No Bills is a GitHub Action that fails such pull requests and asks for a one-line model disclosure in the PR body instead, so you still see which models and tools your community uses.

## What it catches

- **Agent trailers.** A `Co-authored-by` line that names a coding agent.
- **Agent identities.** A commit authored or committed by an agent's address.
- **Hidden text.** Invisible characters and look-alike letters.
- **Model disclosure.** The last line of the PR body names the model, the job, the tool and the host.

<details>
<summary>Details</summary>

- A trailer in a commit message or the PR body counts. Squash merging can carry it into the default branch.
- An agent's address, such as `noreply@anthropic.com`, counts as author or committer.
- Hidden text covers zero-width spaces, bidi controls, tag characters, private-use characters, and letters from another script that pass for Latin, in the title, body, commit messages, and added lines. Unusual spaces warn by default.
- `renovate[bot]` and `dependabot[bot]` skip the disclosure check. Every other PR needs the line, including an empty body.

</details>

## Usage

```yaml
name: Post No Bills
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
      - uses: stickerdaniel/post-no-bills@3a0295140a40edc1ded01766ed9d0ac42cd148a5 # v3.0.0
```

Pin a release SHA. Make `check-bot-coauthors` a required check and give the job no `if:`, because GitHub counts a skipped job as passing.

## Configuration

Defaults fail the pull request. Set an input to `warn` or `off` to loosen one check.

<details>
<summary>Inputs</summary>

| Input | Default | Effect |
| --- | --- | --- |
| `model-attribution` | `host` | `off`, `model`, `job`, `tool` or `host`. |
| `co-author-trailers` | `error` | `error`, `warn` or `off` for agent trailers in commit messages and the PR body. |
| `agent-identities` | `error` | `error`, `warn` or `off` for commits authored or committed by an agent's address. |
| `hidden-unicode` | `error` | `error`, `warn` or `off` for invisible and private-use characters. |
| `unicode-homoglyphs` | `inherit` | `inherit`, `error`, `warn` or `off` for look-alike letters. `inherit` follows `hidden-unicode`. |
| `unicode-unusual-spaces` | `inherit` | `inherit`, `error`, `warn` or `off` for unusual spaces. `inherit` warns unless `hidden-unicode` is `off`. |
| `unicode-exclude-paths` | empty | Paths whose added lines skip the hidden Unicode check. A trailing `/` marks a directory. |
| `allowed-identities` | empty | Addresses that are not agents, as `email:<address>` or `github:<handle>`. |
| `additional-identities` | empty | Addresses that are agents besides the built-in list, in the same form. |
| `additional-binary-extensions` | empty | Extensions that may be binary besides the built-in ones, lowercase without a dot. |
| `additional-attribution-exemptions` | empty | PR author logins exempt from the disclosure check. |

Lists take one entry per line:

```yaml
        with:
          unicode-homoglyphs: warn
          unicode-exclude-paths: |
            tests/fixtures/unicode/
            docs/ja.md
          allowed-identities: |
            github:Copilot
          additional-binary-extensions: |
            wasm
```

Set inputs as literals in the workflow on the default branch, never from pull request content.

- An `email:` selector matches that address, ignoring ASCII case. A `github:` selector matches the handle's noreply address, with or without its numeric ID.
- An unknown input name, an invalid value, a repeated entry, or an address both lists can match fails the run.

</details>

<details>
<summary>Model attribution</summary>

`host` is the default: model, job, tool and host. `off` turns the check off. A line that passes a stricter level also passes the ones below it.

| Last line of the PR body | `model` | `job` | `tool` | `host` |
| --- | :-: | :-: | :-: | :-: |
| `Generated with Claude Opus 5.5` | ✓ | ✗ | ✗ | ✗ |
| `Generated with Claude Opus 5.5 and GPT-6 Pro` | ✓ | ✗ | ✗ | ✗ |
| `Generated with Claude Opus 5.5 in Claude Code.` | ✓ | ✗ | ✗ | ✗ |
| `Generated with Claude Opus 5.5 for implementation.` | ✓ | ✓ | ✗ | ✗ |
| `Generated with Claude Opus 5.5 for implementation and GPT-6 Pro for review.` | ✓ | ✓ | ✗ | ✗ |
| `Generated with Claude Opus 5.5 for implementation, tests in Claude Code.` | ✓ | ✓ | ✓ | ✗ |
| `Generated with Claude Opus 5.5 for implementation and GPT-6 Pro for review in Claude Code.` | ✓ | ✓ | ✓ | ✗ |
| `Generated with Claude Opus 5.5 for implementation in Claude Code via T3 Code.` | ✓ | ✓ | ✓ | ✓ |
| `Generated with Claude Opus 5.5 for implementation and GPT-6 Pro for review in Claude Code via T3 Code.` | ✓ | ✓ | ✓ | ✓ |
| `Generated with Claude Opus 5.5 for implementation and GPT-6 Pro in Claude Code.` | ✗ | ✗ | ✗ | ✗ |
| `Generated with Claude Opus 5.5 for implementation in Claude Code and GPT-6 Pro.` | ✗ | ✗ | ✗ | ✗ |

</details>

<details>
<summary>How it works</summary>

The workflow and the pinned action come from the default branch. The action fetches the PR and reads its commits, diff, title, and body as data, without checking out or running anything from the pull request. Every doubt fails the run.

A Linux or macOS runner needs Python 3.10+ and git 2.31+ on `PATH`. GitHub-hosted Ubuntu runners have both. Windows is not supported.

</details>

## License

MIT. `post_no_bills/hidden.py` comes from [no-ai-marks](https://github.com/mishan/no-ai-marks); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
