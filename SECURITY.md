# Security policy

This action runs with a repository token on pull requests from anyone, including forks. Treat every report that lets a pull request pass a check it should fail, leak the token, or run its own code as security-relevant.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](../../security/advisories/new) for this repository. Do not open a public issue for exploitable problems.

You can expect an initial response within 7 days. Please include the release you pin, your caller workflow, and a run URL or the pull request contents that reproduce it.

## Scope

In scope:

- A pull request that passes although a commit or its body carries a bot `Co-authored-by` trailer, or a commit names a coding agent as author or committer
- A pull request that passes the model attribution check without a valid attribution line
- The job token, or its Base64 form, appearing in a run log
- Pull request text that starts a workflow command of its own
- Anything from the pull request being executed, or changing how git reads the repository

Out of scope:

- Callers that use `pull_request` instead of `pull_request_target`, check out the pull request head, or pin a branch instead of a commit SHA
- Vulnerabilities in GitHub Actions, the runner, Python, or git (report to their maintainers)
- Coding agents that sign their work with an identity the action does not list (open a regular issue)

## Supported versions

Only the latest published release receives security fixes.
