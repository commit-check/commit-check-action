# Commit-Check GitHub Action

[![Main](https://github.com/commit-check/commit-check-action/actions/workflows/main.yaml/badge.svg)](https://github.com/commit-check/commit-check-action/actions/workflows/main.yaml)
[![Commit Check](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml/badge.svg)](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml)
![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/commit-check/commit-check-action)
[![Used by](https://img.shields.io/static/v1?label=Used%20by&message=55&color=informational&logo=slickpic)](https://github.com/commit-check/commit-check-action/network/dependents)<!-- used by badge -->
[![GitHub marketplace](https://img.shields.io/badge/Marketplace-commit--check--action-blue)](https://github.com/marketplace/actions/commit-check-action)
[![slsa-badge](https://slsa.dev/images/gh-badge-level3.svg)](https://github.com/commit-check/commit-check-action/blob/a2873ca0482dd505c93fb51861c953e82fd0a186/action.yml#L59-L69)

A Github Action for checking commit message formatting, branch naming, committer name, email, commit signoff and more.

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
    permissions:  # use permissions because of use pr-comments
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}  # checkout PR HEAD commit
          fetch-depth: 0  # required for merge-base check
      - uses: commit-check/commit-check-action@v1
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }} # use GITHUB_TOKEN because of use pr-comments
        with:
          message: true
          branch: true
          author-name: true
          author-email: true
          commit-signoff: true
          merge-base: false
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
  <strong> and <a href="https://github.com/commit-check/commit-check-action/network/dependents">many more</a>.</strong>
</p>

## Optional Inputs

### `message`

- **Description**: check commit message formatting convention.
  - By default the rule follows [conventional commits](https://www.conventionalcommits.org/).
- Default: `true`

### `branch`

- **Description**: check git branch naming convention.
  - By default the rule follows [conventional branch](https://conventional-branch.github.io/).
- Default: `true`

### `author-name`

- **Description**: check committer author name.
- Default: `true`

### `author-email`

- **Description**: check committer author email.
- Default: `true`

### `commit-signoff`

- **Description**: check committer commit signature.
- Default: `true`

### `merge-base`

- **Description**: check current branch is rebased onto target branch.
- Default: `false`

> [!IMPORTANT]
> `merge-base` is an experimental feature. by default it's disable.
>
> To use this feature, you need fetch all history for all branches by setting `fetch-depth: 0` in `actions/checkout`.

### `dry-run`

- **Description**: run checks without failing. exit code is 0 otherwise is 1.
- Default: `false`

### `job-summary`

- **Description**: display job summary to the workflow run.
- Default: `true`

### `pr-comments`

- **Description**: post results to the pull request comments.
- Default: `false`

> [!IMPORTANT]
> `pr-comments` is an experimental feature. by default it's disable. To use it you need to set `GITHUB_TOKEN` in the GitHub Action.
>
> This feature currently doesn’t work with forked repositories. For more details, refer to issue [#77](https://github.com/commit-check/commit-check-action/issues/77).

Note: the default rule of above inputs is following [this configuration](https://github.com/commit-check/commit-check/blob/main/.commit-check.yml), if you want to customize just add your `.commit-check.yml` config file under your repository root directory.

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

To provide feedback (requesting a feature or reporting a bug) please post to [issues](https://github.com/commit-check/commit-check/issues).
