# Commit-Check GitHub Action

![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/commit-check/commit-check-action?color=blue)
[![Used by](https://img.shields.io/static/v1?label=Used%20by&message=165&color=informational&logo=slickpic)](https://github.com/commit-check/commit-check-action/network/dependents)<!-- used by badge -->
[![GitHub marketplace](https://img.shields.io/badge/Marketplace-commit--check--action-blue)](https://github.com/marketplace/actions/commit-check-action)
[![commit-check](https://img.shields.io/badge/commit--check-enabled-brightgreen?logo=Git&logoColor=white&color=%232c9ccd)](https://github.com/commit-check/commit-check)
[![slsa-badge](https://slsa.dev/images/gh-badge-level3.svg?color=blue)](https://github.com/commit-check/commit-check-action/blob/a2873ca0482dd505c93fb51861c953e82fd0a186/action.yml#L59-L69)
[![codecov](https://codecov.io/gh/commit-check/commit-check-action/graph/badge.svg?token=QHUDSMJGS7)](https://codecov.io/gh/commit-check/commit-check-action)

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
* [Advanced Configuration](#advanced-configuration)
* [Fork PR Comments](docs/fork-pr-comments.md)
* [Badging Your Repository](#badging-your-repository)
* [Versioning](#versioning)

## Usage

Create a new GitHub Actions workflow in your project, e.g. at [.github/workflows/commit-check.yml](.github/workflows/commit-check.yml)

```yaml
name: Commit Check

on:
  pull_request:
    branches: 'main'

jobs:
  commit-check:
    runs-on: ubuntu-latest
    permissions:  # use permissions because use of pr-comments
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0  # Required for merge-base checks
      - uses: commit-check/commit-check-action@v2
        with:
          message: true
          branch: true
          author-name: false
          author-email: false
          job-summary: true
          pr-comments: true
```

> [!NOTE]
> This action supports running on Linux, macOS, and Windows (`ubuntu-latest`, `macos-latest`, `windows-latest`).

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
  <strong>Extrawest</strong>&nbsp;&nbsp;
  <a href="https://github.com/Chainlift"><img src="https://avatars.githubusercontent.com/u/204404276?s=200&v=4" alt="Chainlift" width="28"/></a>
  <strong>Chainlift</strong>&nbsp;&nbsp;
  <a href="https://github.com/mila-iqia"><img src="https://avatars.githubusercontent.com/u/11724251?s=200&v=4" alt="Mila" width="28"/></a>
  <strong>Mila</strong>&nbsp;&nbsp;
  <a href="https://github.com/RLinf/RLinf"><img src="https://avatars.githubusercontent.com/u/226440105?s=200&v=4" alt="RLinf" width="28"/></a>
  <strong>RLinf</strong>&nbsp;&nbsp;
  <a href="https://github.com/collective"><img src="https://avatars.githubusercontent.com/u/362867?s=200&v=4" alt="Collective" width="28"/></a>
  <strong>Collective</strong>&nbsp;&nbsp;
  <a href="https://github.com/cpp-linter"><img src="https://avatars.githubusercontent.com/cpp-linter?s=200&v=4" alt="cpp-linter" width="28"/></a>
  <strong>cpp-linter</strong>&nbsp;&nbsp;
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

> [!NOTE]
> `pr-comments` is disabled by default.
>
> PR comments are skipped for pull requests from forked repositories. See
> [docs/fork-pr-comments.md](docs/fork-pr-comments.md) for details on how to enable
> this feature for fork contributions.
>
> Note: write-access to pull-requests requires the `pull-requests: write` permission.
> See [usage example](#usage).

### `pr-title`

- **Description**: check pull request title following [Conventional Commits](https://www.conventionalcommits.org/).
- Default: `false`

> [!TIP]
> This is especially useful for teams using **Squash & Merge**, where the PR title
> becomes the final commit message in the main branch. When enabled, the action
> validates the PR title against your Conventional Commits configuration, giving
> early feedback at PR time rather than after merge.
>
> `pr-title` works alongside `message` — you can enable both to validate the PR
> title and individual commits, or just one depending on your workflow.
>
> This setting only applies to `pull_request` and `pull_request_target` events;
> it is silently ignored on `push` events.

> [!IMPORTANT]
> By default, `pull_request` does **not** trigger on title changes.
> To validate the PR title immediately when updated, add `edited` to your
> workflow's event types:
> ```yaml
> on:
>   pull_request:
>     types: [opened, synchronize, reopened, edited]
> ```
> Without `edited`, only the initial title (at PR creation) is validated.

## Advanced Configuration

The [Optional Inputs](#optional-inputs) above cover the most common settings.
For everything else (e.g., `subject-capitalized`, `require-signed-off-by`,
`ai-attribution`, custom `allow-commit-types`, etc.), you have two approaches:

### Via Environment Variables

Set any `CCHK_*` environment variable in your workflow step — no config file required:

```yaml
- uses: commit-check/commit-check-action@v2
  env:
    CCHK_SUBJECT_CAPITALIZED: "true"
    CCHK_REQUIRE_SIGNED_OFF_BY: "true"
    CCHK_AI_ATTRIBUTION: "forbid"
    CCHK_ALLOW_COMMIT_TYPES: "feat,fix,docs,chore"
```

All available environment variables follow the naming convention:
`CCHK_` + uppercase option name with underscores instead of hyphens. See the
[full mapping](https://commit-check.com/configuration/#environment-variables)
in the commit-check documentation.

### Via Configuration File

Add a `commit-check.toml` or `cchk.toml` to the root of your repository.
Refer to the [configuration guide](https://commit-check.com/configuration/)
for all available options.

> [!NOTE]
> Configuration priority: **CLI args > environment variables > config file > defaults**.
> The action itself doesn't set any CLI flags beyond those in
> [Optional Inputs](#optional-inputs), so env vars and config files are the
> recommended way to customize.

## Outputs

### `result`

Structured check results as JSON, available to downstream steps via
[`fromJSON`](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/accessing-contextual-information-about-workflow-runs#fromjson):

```yaml
- uses: commit-check/commit-check-action@v2
  id: commit-check
  with:
    dry-run: true # (1)

- name: Inspect results
  run: |
    echo "Status: ${{ fromJSON(steps.commit-check.outputs.result).status }}"
    echo "Scopes: ${{ toJSON(fromJSON(steps.commit-check.outputs.result).scopes) }}"
```

1. Without `dry-run`, a failing check ends the job before any later step runs.
   Use `dry-run` (or `continue-on-error`) when a downstream step is meant to
   read the result and decide for itself.

Each scope carries the check outcomes (`rule_id`, `check`, `status`, `value`,
`error`, `suggest`, `docs_url`) exactly as produced by
`commit-check --format json`, so downstream jobs can build their own reports
or gate on individual rules.

## GitHub Action Job Summary

By default, commit-check-action results are shown on the job summary page of the
workflow. The report below is reproduced as the action renders it, except that
its title is a heading in the real thing — it is bold here so it stays out of
this page's table of contents — and the footer names the version that actually
ran.

### Success Job Summary

Passing runs stay to one line, with the detail folded away:

> **Commit Check**
>
> ✅ **All 3 checks passed**
>
> <details>
> <summary>Show all 3 checks</summary>
>
> ```text
> Commit message
>   ✔ PR title (feat: add login page)
>   ✔ Commit 1/2 (feat: add login page)
> Branch
>   ✔ Branch (feature/add-login)
> ```
>
> </details>
>
> _commit-check &lt;version&gt; · [Rules reference](https://commit-check.com/rules/)_

### Failure Job Summary

Failures open with a count, then a table of only the scopes that failed — every
rule ID links to its documentation — with the full tree still one click away:

> **Commit Check**
>
> ❌ **2 of 4 checks failed**
>
> | Scope | Checked value | Failed checks |
> |---|---|---|
> | Commit 2/2 | `bad msg` | [CC001 message](https://commit-check.com/rules/#cc001) |
> | Branch | `my-changes` | [CC201 branch](https://commit-check.com/rules/#cc201) |
>
> <details>
> <summary>Show all 4 checks</summary>
>
> ```text
> Commit message
>   ✔ PR title (feat: add login page)
>   ✔ Commit 1/2 (feat: add login page)
>   ✖ Commit 2/2 (1 failure)
>       CC001 message
>         value: bad msg
>         The commit message should follow Conventional Commits.
>         Suggest: Use <type>(<scope>): <description>
> Branch
>   ✖ Branch (1 failure)
>       CC201 branch
>         value: my-changes
>         The branch should follow Conventional Branch.
>         Suggest: Use <type>/<description> with allowed types
> ```
>
> </details>
>
> _commit-check &lt;version&gt; · [Rules reference](https://commit-check.com/rules/)_

A scope is one thing that was checked — a commit message, the branch, the author
— not one rule evaluation, so the total matches the ✔/✖ lines you can count and
does not grow with the number of rules in your config.

### Skipped Job Summary

Some runs validate nothing at all — most commonly when the commit author is
listed in `ignore_authors`, which is how Dependabot and other bots are usually
exempted. Those runs report `⊘`, never `✔`:

> <img src="https://raw.githubusercontent.com/commit-check/commit-check-action/main/assets/logo.png" width="20" align="top" alt=""> **Commit Check**
>
> ⊘ **All 3 checks skipped** — nothing was validated
>
> <details>
> <summary>Show all 3 checks</summary>
>
> ```text
> Commit message
>   ⊘ PR title (skipped)
>   ⊘ Commit 1/1 (skipped)
> Branch
>   ⊘ Branch (skipped)
> ```
>
> </details>
>
> _commit-check &lt;version&gt; · [Rules reference](https://commit-check.com/rules/)_

A skipped scope carries no checked value, because nothing was examined. When
only some scopes skip, the verdict counts them separately —
`✅ **3 of 5 checks passed**, 2 skipped` — so the headline never claims a pass
that did not happen. Failures still take precedence over skips.

This needs commit-check 2.13.4 or newer, which reports `"status": "skip"` in
its JSON. Against an older engine every check is `pass` or `fail` as before,
and the report is unchanged.

## GitHub Pull Request Comments

With `pr-comments: true` the same report is posted as a pull request comment.
It is the same Markdown: the job summary and the comment are both rendered by
`render_report`, so the two surfaces cannot disagree. See
[Success Job Summary](#success-job-summary) and
[Failure Job Summary](#failure-job-summary) above for what it looks like.

What differs is the lifecycle rather than the content:

- The comment is **edited in place** on later runs rather than added to, so a
  pull request carries one Commit Check comment however many times CI runs. It
  stays after the checks pass, showing the ✅ report rather than disappearing.
- Comments are identified by a hidden `<!-- commit-check-action -->` marker, so
  reformatting the visible text does not orphan the previous one. If several
  marked comments somehow exist, the newest is kept and the rest deleted.
- A comment from a version predating the marker is adopted rather than
  duplicated — but only when a bot posted it, since the older signal was just a
  title prefix that a person could type by hand.

## Fork PR Comments

When a pull request is opened from a **forked repository**, the `GITHUB_TOKEN` used by the
`pull_request` event has **read-only** permissions by design (GitHub security policy).
This means `pr-comments: true` cannot write a comment back to the PR.

By default, commit-check-action handles this gracefully:

- PR comment writing is **skipped** with a `::warning::` message in the logs
- A **notice is added to the Job Summary** explaining why and how to fix it
- The commit checks themselves **still run normally**

> **For most projects, this is sufficient** — contributors can see check results in the
> action Job Summary. But if you *must* have PR comments on fork contributions, see
> the **[Fork PR Comments](docs/fork-pr-comments.md)** documentation for
> two recommended approaches with ready-to-use workflow examples.

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

To provide feedback (requesting a feature or reporting a bug), please post to [issues](https://github.com/commit-check/commit-check/issues) or start a [discussion](https://github.com/commit-check/commit-check/discussions).
