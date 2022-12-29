# Commit-Check GitHub Action

![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/commit-check/commit-check-action)
[![GitHub marketplace](https://img.shields.io/badge/Marketplace-commit--check--action-blue)](https://github.com/marketplace/actions/commit-check-action)

A Github Action for checking commit message formatting, branch naming, referencing Jira tickets, and more.

## Usage

Create a new GitHub Actions workflow in your project, e.g. at [.github/workflows/commit-check.yml](.github/workflows/commit-check.yml)

```yaml
name: Commit Check

on: pull_request

jobs:
  commit-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: commit-check/commit-check-action@v1
        with:
          message: true
          branch: true
          author-name: true
          author-email: true
          dry-run: true
```

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

## Add Commit Check Action badge in README

You can show Commit Check Action status with a badge in your repository README

Example

```
[![commit-check](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml/badge.svg)](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml)
```

[![commit-check](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml/badge.svg)](https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml)

## Versioning

Versioning follows [Semantic Versioning](https://semver.org/).

## Have question or feedback?

To provide feedback (requesting a feature or reporting a bug) please post to [issues](https://github.com/commit-check/commit-check/issues).

## License

[MIT License](LICENSE)
