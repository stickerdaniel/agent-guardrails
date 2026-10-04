# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html), with one exception: 2.1.0 changes a default.

## [Unreleased]

## [2.1.0] - 2026-10-04

This minor release changes a default, which Semantic Versioning reserves for a major release. A PR that passed under 2.0.0 can fail under 2.1.0.

### Changed

- `require-model-attribution` defaults to `true`. Every PR body then needs a final `Generated with <model>` line, unless the PR author is `renovate[bot]`, `dependabot[bot]`, or listed in `additional-attribution-exemptions`
- The attribution error message's examples name Claude Opus 5.5. The accepted attribution forms are unchanged

### Migration

- A workflow that set `require-model-attribution` keeps its pass or fail result
- A workflow that relied on the default and does not want the check sets `require-model-attribution: false`

## [2.0.0] - 2026-10-03

### Added

- Inputs `co-author-trailers` and `agent-identities`, each `error`, `warn` or `off`
- Inputs `unicode-homoglyphs` and `unicode-unusual-spaces`, each `inherit`, `error`, `warn` or `off`; an explicit value wins over `hidden-unicode`
- Input `unicode-exclude-paths`: literal file and directory paths whose added lines skip the hidden Unicode check. An unreadable file under them still fails
- Inputs `allowed-identities` and `additional-identities`: `email:<address>` and `github:<handle>` selectors that exempt addresses from, or add them to, the trailer and identity checks
- Input `additional-binary-extensions`: further formats that may be binary
- Input `additional-attribution-exemptions`: further PR author logins exempt from the attribution check

### Changed

- `hidden-unicode` accepts `off`

### Breaking

- An input name that matches no declared input, ignoring case, fails the run. v1 ignored it with a runner warning

### Migration

- A v1 workflow with valid input names works unchanged

## [1.0.0] - 2026-09-29

### Added

- Composite action that fails a pull request when a commit message or the PR body carries a bot `Co-authored-by` trailer, or a commit names a coding agent as author or committer
- Input `require-model-attribution` that requires a `Generated with <model>` line as the last non-empty line of the PR body, with `renovate[bot]` and `dependabot[bot]` exempt
- Hidden Unicode check on the PR title and raw body, every commit message, and every line the pull request adds: invisible characters, private-use characters, and letters that pass for Latin fail, and unusual spaces warn. Text hidden in tag characters, variation selectors, or zero-width characters is decoded into the message
- Input `hidden-unicode`: with `warn`, hidden Unicode findings are warnings; every other finding still fails
- Fails closed: an invalid event or input, a head that moved since the event, a base commit missing from the fetched history, and an empty commit range all fail the run
- A new-side binary file fails unless its extension is a listed binary format; invalid UTF-8 added lines and paths fail, as do undecodable mode-only destinations
- Inspection limits: 64 MiB of Git standard output, two million output records, 600 seconds for Git calls, 100 million scan-work units, 1,000 findings, four million characters per line, and 100,000 suspicious characters per line; exceeding any limit fails even in `warn` mode

### Fixed

- Binary-to-text changes are scanned on the new side, and mode-only changes retain their true binary or text classification
- Incomplete final diff hunks fail instead of accepting a partial added-line set

### Provenance

- The attribution grammar and its tests come from `scripts/check_pr_model_attribution.py` and `tests/test_pr_model_attribution.py` in stickerdaniel/linkedin-mcp-server at commit `6e7344763bc3e8b99b95d5468b9edeea511be61e`, originally Apache-2.0. Daniel Sticker is their sole author and relicenses them here under MIT.
- `agent_guardrails/hidden.py` is `no_ai_marks/chars.py` from mishan/no-ai-marks at commit `747c07a76d2ad6471fa73779dc8de97fbcb4aefe`, MIT, with the Unicode License V3 for its look-alike tables. See `THIRD_PARTY_NOTICES.md`.
