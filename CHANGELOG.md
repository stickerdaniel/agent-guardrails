# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Composite action that fails a pull request when a commit message or the PR body carries a bot `Co-authored-by` trailer, or a commit names a coding agent as author or committer
- Input `require-model-attribution` that requires a `Generated with <model>` line as the last non-empty line of the PR body, with `renovate[bot]` and `dependabot[bot]` exempt
- Input `hidden-unicode`, validated as `error` or `warn`; no check reads it yet
- Fails closed: an invalid event or input, a head that moved since the event, a base commit missing from the fetched history, and an empty commit range all fail the run

### Provenance

- The attribution grammar and its tests come from `scripts/check_pr_model_attribution.py` and `tests/test_pr_model_attribution.py` in stickerdaniel/linkedin-mcp-server at commit `6e7344763bc3e8b99b95d5468b9edeea511be61e`, originally Apache-2.0. Daniel Sticker is their sole author and relicenses them here under MIT.
- `agent_guardrails/hidden.py` is `no_ai_marks/chars.py` from mishan/no-ai-marks at commit `747c07a76d2ad6471fa73779dc8de97fbcb4aefe`, MIT, with the Unicode License V3 for its look-alike tables. See `THIRD_PARTY_NOTICES.md`.
