# Commit-Check GitHub Action

[![Commit Check](https://img.shields.io/github/actions/workflow/status/commit-check/commit-check-action/commit-check.yml?branch=main&label=Commit%20Check&color=blue&logo=github)](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml)
![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/commit-check/commit-check-action?color=blue)
[![Used by](https://img.shields.io/static/v1?label=Used%20by&message=105&color=informational&logo=slickpic)](https://github.com/commit-check/commit-check-action/network/dependents)<!-- used by badge -->
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
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }} # Needed for PR comments
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
> `pr-comments` is an experimental feature. By default, it's disabled. To use it, you need to set `GITHUB_TOKEN` in the GitHub Action.
>
> This feature currently doesn’t work with forked repositories. For more details, refer to issue [#77](https://github.com/commit-check/commit-check-action/issues/77).

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
