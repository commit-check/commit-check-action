# Commit-Check GitHub Action

[![Commit Check](https://img.shields.io/github/actions/workflow/status/commit-check/commit-check-action/commit-check.yml?branch=main&label=Commit%20Check&color=blue&logo=github)](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml)
![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/commit-check/commit-check-action?color=blue)
[![Used by](https://img.shields.io/static/v1?label=Used%20by&message=133&color=informational&logo=slickpic)](https://github.com/commit-check/commit-check-action/network/dependents)<!-- used by badge -->
[![GitHub marketplace](https://img.shields.io/badge/Marketplace-commit--check--action-blue)](https://github.com/marketplace/actions/commit-check-action)
[![slsa-badge](https://slsa.dev/images/gh-badge-level3.svg?color=blue)](https://github.com/commit-check/commit-check-action/blob/a2873ca0482dd505c93fb51861c953e82fd0a186/action.yml#L59-L69)

A GitHub Action for checking commit message formatting, branch naming, committer name, email, commit signoff, and more.

## What's New in v2

> [!IMPORTANT]
> This v2 release introduces several 🚨**breaking changes**. Please review the [Breaking Changes](#breaking-changes) section carefully before upgrading.

### Breaking Changes

- Removed support for `commit-signoff`, `merge-base`, and `imperative` inputs — now configured via `commit-check.toml` or `cchk.toml`.
- Deprecated `.commit-check.yml` in favor of `commit-check.toml` or `cchk.toml`.
- Changed default values of `author-name` and `author-email` inputs to `false` to align with the default behavior in commit-check.
- Upgraded core dependency [`commit-check`](https://github.com/commit-check/commit-check) to [**v2.0.0**](https://github.com/commit-check/commit-check/releases/tag/v2.0.0).

## Table of Contents

* [Usage](#usage)
* [Optional Inputs](#optional-inputs)
* [GitHub Action Job Summary](#github-action-job-summary)
* [GitHub Pull Request Comments](#github-pull-request-comments)
* [Fork PR Comments](#fork-pr-comments)
* [Badging Your Repository](#badging-your-repository)
* [Versioning](#versioning)

## Usage

Create a new GitHub Actions workflow in your project, e.g. at [.github/workflows/commit-check.yml](.github/workflows/commit-check.yml)

```yaml
name: Commit Check

on:
  push:
  pull_request:
    branches: 'main'

jobs:
  commit-check:
    runs-on: ubuntu-latest
    permissions:  # use permissions because use of pr-comments
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0  # Required for merge-base checks
      - uses: commit-check/commit-check-action@v2
        with:
          message: true
          branch: true
          author-name: false
          author-email: false
          job-summary: true
          pr-comments: ${{ github.event_name == 'pull_request' }}
```

## Used By

<p align="center">
  <a href="https://github.com/apache"><img src="https://avatars.githubusercontent.com/u/47359?s=200&v=4" alt="Apache" width="28"/></a>
  <strong>Apache</strong>&nbsp;&nbsp;
  <a href="https://github.com/discovery-unicamp"><img src="https://avatars.githubusercontent.com/u/112810766?s=200&v=4" alt="discovery-unicamp" width="28"/></a>
  <strong>discovery-unicamp</strong>&nbsp;&nbsp;
  <a href="https://github.com/TexasInstruments"><img src="https://avatars.githubusercontent.com/u/24322022?s=200&v=4" alt="Texas Instruments" width="28"/></a>
  <strong>Texas Instruments</strong>&nbsp;&nbsp;
  <a href="https://github.com/opencadc"><img src="https://avatars.githubusercontent.com/u/13909060?s=200&v=4" alt="OpenCADC" width="28"/></a>
  <strong>OpenCADC</strong>&nbsp;&nbsp;
  <a href="https://github.com/extrawest"><img src="https://avatars.githubusercontent.com/u/39154663?s=200&v=4" alt="Extrawest" width="28"/></a>
  <strong>Extrawest</strong>
  <a href="https://github.com/Chainlift"><img src="https://avatars.githubusercontent.com/u/204404276?s=200&v=4" alt="Chainlift" width="28"/></a>
  <strong>Chainlift</strong>&nbsp;&nbsp;
  <a href="https://github.com/mila-iqia"><img src="https://avatars.githubusercontent.com/u/11724251?s=200&v=4" alt="Mila" width="28"/></a>
  <strong>Mila</strong>&nbsp;&nbsp;
  <a href="https://github.com/RLinf/RLinf"><img src="https://avatars.githubusercontent.com/u/226440105?s=200&v=4" alt="RLinf" width="28"/></a>
  <strong>RLinf</strong>&nbsp;&nbsp;
  <strong> and <a href="https://github.com/commit-check/commit-check-action/network/dependents">many more</a>.</strong>
</p>

## Optional Inputs

### `message`

- **Description**: check git commit message following [Conventional Commits](https://www.conventionalcommits.org/).
- Default: `true`

### `branch`

- **Description**: check git branch name following [Conventional Branch](https://conventional-branch.github.io/).
- Default: `true`

### `author-name`

- **Description**: check committer author name.
- Default: `false`

### `author-email`

- **Description**: check committer author email.
- Default: `false`

### `dry-run`

- **Description**: run checks without failing. exit code is 0; otherwise is 1.
- Default: `false`

### `job-summary`

- **Description**: display job summary to the workflow run.
- Default: `true`

### `pr-comments`

- **Description**: post results to the pull request comments.
- Default: `false`

> [!IMPORTANT]
> `pr-comments` is an experimental feature. By default, it's disabled.
>
> PR comments are skipped for pull requests from forked repositories. See
> [Fork PR Comments](#fork-pr-comments) for details on how to enable this feature
> for fork contributions.
>
> Note: write-access to pull-requests requires the `pull-requests: write` permission.
> See [usage example](#usage).

Note: the default rule of above inputs is following [this configuration](https://github.com/commit-check/commit-check-action/blob/main/commit-check.toml). If you want to customize, just add your [`commit-check.toml`](https://commit-check.github.io/commit-check/configuration.html) config file under your repository root directory.

## GitHub Action Job Summary

By default, commit-check-action results are shown on the job summary page of the workflow.

### Success Job Summary

![Success job summary](https://github.com/commit-check/.github/blob/main/screenshot/success-job-summary.png)

### Failure Job Summary

![Failure job summary](https://github.com/commit-check/.github/blob/main/screenshot/failure-job-summary.png)

## GitHub Pull Request Comments

### Success Pull Request Comment

![Success pull request comment](https://github.com/commit-check/.github/blob/main/screenshot/success-pr-comments.png)

### Failure Pull Request Comment

![Failure pull request comment](https://github.com/commit-check/.github/blob/main/screenshot/failure-pr-comments.png)

## Fork PR Comments

When a pull request is opened from a **forked repository**, the `GITHUB_TOKEN` used by the
`pull_request` event has **read-only** permissions by design (GitHub security policy).
This means `pr-comments: true` cannot write a comment back to the PR.

By default, commit-check-action handles this gracefully:

- PR comment writing is **skipped** with a `::warning::` message in the logs
- A **notice is added to the Job Summary** explaining why and how to fix it
- The commit checks themselves **still run normally**

> **For most projects, this is sufficient** — contributors can see check results in the
> action Job Summary. But if you *must* have PR comments on fork contributions, there
> are two recommended approaches.

---

### Option 1: Two-workflow pattern (recommended)

This is the **official GitHub-recommended best practice** for writing PR comments from
fork PRs. It uses the [`workflow_run`](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#workflow_run)
event with **no security risks**.

> 📁 Ready-to-use files: [`examples/commit-check-workflow-a.yml`](examples/commit-check-workflow-a.yml)
> and [`examples/commit-check-workflow-b.yml`](examples/commit-check-workflow-b.yml)

**How it works:**

```
  pull_request          workflow_run
      │                      │
      ▼                      ▼
┌──────────────┐     ┌──────────────────┐
│ Workflow A   │     │ Workflow B       │
│ (checks)     │────►│ (comment writer) │
│              │     │                  │
│ Token: READ  │     │ Token: WRITE     │
│ Saves result │     │ Reads artifact   │
│ as artifact  │     │ Posts PR comment │
└──────────────┘     └──────────────────┘
```

**Workflow A** — `.github/workflows/commit-check.yml` (triggered by `pull_request`):

```yaml
name: Commit Check

on:
  pull_request:
    branches: ["main"]

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - uses: commit-check/commit-check-action@v2
        with:
          message: true
          branch: true
          pr-comments: false     # comments handled by Workflow B
          job-summary: true
      - uses: actions/upload-artifact@v4
        with:
          name: commit-check-result-${{ github.event.number }}
          path: result.txt      # saved for Workflow B
```

> 📄 Full file: [`examples/commit-check-workflow-a.yml`](examples/commit-check-workflow-a.yml)

**Workflow B** — `.github/workflows/commit-check-comment.yml` (triggered by `workflow_run`):

```yaml
name: Commit Check Comment

on:
  workflow_run:
    workflows: ["Commit Check"]   # must match Workflow A's name exactly
    types: [completed]

jobs:
  comment:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      actions: read               # needed to download artifacts
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: commit-check-result-${{ github.event.workflow_run.pull_requests[0].number }}
          run-id: ${{ github.event.workflow_run.id }}
          github-token: ${{ github.token }}
      - name: Read result and post PR comment
        uses: actions/github-script@v7
        with:
          script: |
            // See examples/commit-check-workflow-b.yml for full script
            const fs = require('fs');
            const prNumber = ${{ github.event.workflow_run.pull_requests[0].number }};
            const resultText = fs.readFileSync('result.txt', 'utf8').trim();
            const body = resultText
              ? '# Commit-Check ❌\n```\n' + resultText + '\n```'
              : '# Commit-Check ✔️';
            // Creates or updates the matching PR comment
```

> 📄 Full file: [`examples/commit-check-workflow-b.yml`](examples/commit-check-workflow-b.yml)

> **Key security benefits:**
> - Workflow B runs in the **base repository's context**, so `GITHUB_TOKEN` has full write
>   permissions (you explicitly grant `pull-requests: write`)
> - Workflow B **does not checkout the PR code**, so untrusted fork code never runs
>   with elevated permissions
> - The artifact only contains `result.txt` — no code or secrets

---

### Option 2: pull_request_target (advanced, use with caution)

If you understand the security implications, you can use
[`pull_request_target`](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#pull_request_target)
which runs in the base repository's context with **write token access**.

> **⚠️ Security warning:** Never check out (`actions/checkout`) the PR's HEAD commit
> when using `pull_request_target`. Always check out the base branch or use the
> default merge commit. Otherwise, fork code could exfiltrate your repository's secrets.

```yaml
name: Commit Check

on:
  pull_request_target:
    branches: ["main"]

jobs:
  commit-check:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      # SAFE: checkout the merge commit, NOT the PR head
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - uses: commit-check/commit-check-action@v2
        with:
          message: true
          branch: true
          pr-comments: true
```

> ✅ With `pull_request_target`, `pr-comments: true` **does work** on fork PRs —
> the token has the workflow's configured permissions regardless of whether the PR
> is from a fork.
>
> **When to use this:** Only if the two-workflow pattern is too complex for your setup
> and you have thoroughly reviewed the security implications.

## Badging Your Repository

You can add a badge to your repository to show your contributors/users that you use commit-check!

[![Commit Check](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml/badge.svg)](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml)

Markdown

```
[![Commit Check](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml/badge.svg)](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml)
```

reStructuredText

```
.. image:: https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml/badge.svg
    :target: https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml
    :alt: Commit Check
```


## Versioning

Versioning follows [Semantic Versioning](https://semver.org/).

## Have questions or feedback?

To provide feedback (requesting a feature or reporting a bug), please post to [issues](https://github.com/commit-check/commit-check/issues).
