# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.1.0] - 2026-10-07

### Added

- GitHub Marketplace metadata: the listing name `Post No Bills Check` and a white `slash` icon on dark gray

### Changed

- The action's display name is `Post No Bills Check`, because a Marketplace name cannot match an existing GitHub organization. The repository, the `post-no-bills` log prefix, inputs, defaults, and rules are unchanged
- The README gives an `AGENTS.md` snippet so that agents write the model disclosure before CI runs

## [3.0.0] - 2026-10-05

### Changed

- `require-model-attribution` is replaced by `model-attribution`: `off`, `model`, `job`, `tool` or `host`, default `host`. The final attribution line uses a stricter grammar, where `for`, `in`, `and` and `via` separate the fields and cannot appear inside a name, and every model has a job or none does

## Before 3.0.0

Versions 1.0.0 to 2.2.0 were early releases and are no longer published. Their commits stay in the history of `main`. Before 2.2.0 the repository was `stickerdaniel/agent-guardrails`; `uses:` references to that name do not resolve.
