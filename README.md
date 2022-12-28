# Commit-Check GitHub Action

![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/commit-check/commit-check-action)
[![GitHub marketplace](https://img.shields.io/badge/marketplace-commit--check-blue?logo=github)](https://github.com/marketplace/actions/commit-check)

A Github Action for checking commit message formatting, branch naming, referencing Jira tickets, and more.

## Optional Inputs

### `message`

- **Description**: check commit message formatting convention
  - By default the rule follows [conventionalcommits](https://www.conventionalcommits.org/)
- Default: 'true'

### `branch`

- **Description**: check git branch naming convention
  - By default follow bitbucket [branching model](https://support.atlassian.com/bitbucket-cloud/docs/configure-a-projects-branching-model/)
- Default: 'true'

### `author-name`

- **Description**: check committer author name
- Default: 'true'

### `author-email`

- **Description**: check committer author email
- Default: 'true'

### `dry-run`

- **Description**: run checks without failing
- Default: 'false'

Note: to change the default rules of above inputs, just add your own [`.commit-check.yml`](https://github.com/commit-check/commit-check#usage) config file.

## Versioning

Versioning follows [Semantic Versioning](https://semver.org/).

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/H2H85WC9L)
