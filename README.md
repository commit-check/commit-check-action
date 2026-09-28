<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/commit-check/.github/main/branding/banner-dark.png">
  <img src="https://raw.githubusercontent.com/commit-check/.github/main/branding/banner-light.png" alt="Commit Check">
</picture>

**Catch bad commits before they merge — on every pull request.**

[![Release](https://img.shields.io/github/v/release/commit-check/commit-check-action?labelColor=0b1620&color=2c9ccd&label=release)](https://github.com/commit-check/commit-check-action/releases)
[![Used by](https://img.shields.io/static/v1?label=Used%20by&message=167&color=2c9ccd&logo=github&logoColor=white&labelColor=0b1620)](https://github.com/commit-check/commit-check-action/network/dependents)<!-- used by badge -->
[![Marketplace](https://img.shields.io/badge/Marketplace-commit--check--action-2c9ccd?labelColor=0b1620&logo=githubactions&logoColor=white)](https://github.com/marketplace/actions/commit-check-action)
[![SLSA 3](https://slsa.dev/images/gh-badge-level3.svg)](https://github.com/commit-check/commit-check-action/blob/main/action.yml#L84-L94)
[![Coverage](https://img.shields.io/codecov/c/github/commit-check/commit-check-action?labelColor=0b1620&color=2c9ccd&label=coverage)](https://codecov.io/gh/commit-check/commit-check-action)

[Docs](https://commit-check.com/guides/github-actions/) ·
[Rules](https://commit-check.com/rules/) ·
[CLI](https://github.com/commit-check/commit-check) ·
[GitHub App](https://github.com/apps/commit-check) ·
[MCP server](https://github.com/commit-check/commit-check-mcp)

</div>

The GitHub Action for [Commit Check](https://github.com/commit-check/commit-check).
It checks every commit of a pull request — message, branch, author, and
optionally the PR title — against your `cchk.toml`, and reports the result in
the job summary, as annotations on the diff and, if you want, as a PR comment.

## Usage

Add a workflow, e.g. `.github/workflows/commit-check.yml`:

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
          # Recommended: the action then reads the PR's commits from the clone.
          # With the default fetch-depth: 1 it asks the GitHub API instead.
          fetch-depth: 0
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
> `fetch-depth: 0` is recommended, not required. A shallow clone holds only
> GitHub's merge commit, so the action lists the pull request's commits
> through the API — up to 250, with `pull-requests: read` — and fetches the
> head commit for the author checks. If neither works it warns and checks
> HEAD alone.

Runs on `ubuntu-latest`, `macos-latest` and `windows-latest`. Self-hosted
runners need a few tools — see [Good to know](#good-to-know).

## Inputs

| Input | Default | What it does |
|---|---|---|
| `message` | `true` | Check every commit message against [Conventional Commits](https://www.conventionalcommits.org/) |
| `branch` | `true` | Check the branch name against [Conventional Branch](https://conventionalbranch.org/) |
| `author-name` | `false` | Check each commit's author name |
| `author-email` | `false` | Check each commit's author email |
| `pr-title` | `false` | Check the pull request title against Conventional Commits — the one that matters for squash merges. Pull request events only |
| `pr-comments` | `false` | Post the report as a pull request comment, edited in place on later runs. Needs `pull-requests: write`; skipped on fork pull requests |
| `job-summary` | `true` | Write the report to the job summary |
| `dry-run` | `false` | Report failures as warnings and always exit 0 |

> [!TIP]
> `pull_request` does not fire when a title is edited. With `pr-title: true`,
> add `types: [opened, synchronize, reopened, edited]` to re-check a renamed
> pull request.

## Configuration

The action reads the repository's `cchk.toml` or `commit-check.toml` — the same
file the CLI and the pre-commit hook use. Any setting can also come from a
`CCHK_*` environment variable, no config file needed:

```yaml
- uses: commit-check/commit-check-action@v2
  env:
    CCHK_SUBJECT_CAPITALIZED: "true"
    CCHK_REQUIRE_SIGNED_OFF_BY: "true"
    CCHK_AI_ATTRIBUTION: "forbid"
    CCHK_ALLOW_COMMIT_TYPES: "feat,fix,docs,chore"
```

Priority: inputs > environment variables > config file > defaults. A rule
listed under the config's top-level `warn` is reported in full but never fails
the run. Every key: [configuration reference](https://commit-check.com/configuration/).

## What it looks like

A failing run opens with a count and a table of only what failed — each rule ID
links to its documentation, each commit to itself — with the full tree one
click away:

> **Commit Check**
>
> ❌ **2 of 4 checks failed**
>
> | Scope | Checked value | Failed checks |
> |---|---|---|
> | [Commit 2/2 (5584f46)](https://github.com/acme/widgets/commit/5584f462cc3c947b2ba8d3d1a5735571803ee159) | `bad msg` | [CC001 message](https://commit-check.com/rules/#cc001) |
> | Branch | `Feature/Add-Login` | [CC201 branch](https://commit-check.com/rules/#cc201) |
>
> <details>
> <summary>Show all 4 checks</summary>
>
> ```text
> Commit message
>   ✔ PR title (feat: add login page)
>   ✔ Commit 1/2 (d87faca) (feat: add login page)
>   ✖ Commit 2/2 (5584f46) (1 failure)
>       CC001 message
>         value: bad msg
>         The commit message should follow Conventional Commits.
>         Suggest: Use <type>(<scope>): <description>
> Branch
>   ✖ Branch (1 failure)
>       CC201 branch
>         value: Feature/Add-Login
>         The branch should follow Conventional Branch.
>         Suggest: Rename the branch to "feature/Add-Login" (git branch -m feature/Add-Login)
>         Fix: feature/Add-Login
> ```
>
> </details>
>
> _commit-check &lt;version&gt; · [Rules reference](https://commit-check.com/rules/)_

The rest follow the same layout:

- **Passed:** one line, `✅ All 3 checks passed`, with the tree folded away.
- **Warned:** a rule under `warn` gets its own ⚠ row and counts as passed.
- **Skipped:** a run where nothing was validated — typically a bot listed in
  `ignore_authors` — reads `⊘`, never `✔`.

The step log prints the same tree, plus one annotation per finding on the
Files changed tab. With `pr-comments: true` the same report is posted to the
pull request, and later runs edit that one comment instead of adding more.

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

The top-level `status` is one of:

| `status` | Meaning | Exit code |
|---|---|---|
| `pass` | every check passed | 0 |
| `warn` | nothing failed, but a rule listed under the config's `warn` found something | 0 |
| `skip` | every check declined to run (for example the author is in `ignore_authors`) | 0 |
| `fail` | at least one check failed | 1 (0 with `dry-run`) |

Only `fail` is ever non-zero; `warn` exists so a downstream step can react to a
bent-but-not-broken policy without the run turning red:

```yaml
- if: fromJSON(steps.commit-check.outputs.result).status == 'warn'
  run: echo "passed with warnings"
```

Each entry in `scopes` has a `label` (`PR title`, `Commit 2/3`, `Branch`, ...),
a `status` like the ones above, a `sha` (the full hash of the commit a
`Commit N/M` or `Commit message` scope checked; empty for the others) and the
check outcomes (`rule_id`, `check`, `status`, `value`, `error`, `suggest`,
`fix`, `docs_url`) exactly as produced by `commit-check --format json`, so
downstream jobs can build their own reports or gate on individual rules.

## Action, pre-commit hook, or GitHub App?

All three run the same `commit-check` engine against the same
`commit-check.toml` / `cchk.toml`; they differ in where they run and what they
can see.

| | GitHub Action (this repo) | [pre-commit hook](https://commit-check.com/guides/pre-commit/) | [Commit Check GitHub App](https://github.com/apps/commit-check) |
|---|---|---|---|
| **Where it runs** | In your workflow, on the runner, after the push | On the contributor's machine, at `git commit` / `git push` | Hosted by commit-check; installed on the repository, no workflow file |
| **What it checks** | Every PR commit's message, plus the PR title, branch and author checks you enable; renders a job summary, annotations, a PR comment and the `result` output | Message (`commit-msg` stage), branch, author; tag, force-push and files (`pre-push`) — one commit at a time, before it exists | Every commit of a push or pull request: message, branch, author (the PR title only in squash mode); reported as one **Commit Check** check run per commit |
| **When to pick it** | You want enforcement in CI that a contributor cannot skip, per-rule outputs for later steps, or you run on GitHub Enterprise Server / need `CCHK_*` overrides | You want the fastest feedback and to stop bad commits before they are pushed; pair it with the Action, since hooks are opt-in | You want zero YAML and no Actions minutes, or feedback on [fork pull requests](docs/fork-pr-comments.md) without the Action's read-only-token limits |

Most teams pair the pre-commit hook (fast, local) with the Action (enforced):
the hook catches a bad message before it is pushed, and the Action is why CI
fails when a contributor did not install the hook.

## Used by

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

## Good to know

<details>
<summary><b>Runner requirements</b></summary>

The action is a composite step and uses what the runner already has:

- **Python 3.10 or newer** on `PATH` (`python3`, or `python` on Windows). No
  `setup-python` step is needed on GitHub-hosted runners. Everything the action
  installs goes under `$RUNNER_TEMP`, never into your checkout.
- **`gh` CLI** — used to verify the build-provenance attestation of the
  `commit-check` wheel before installing it. Present on GitHub-hosted images;
  install it on self-hosted runners or the attestation step fails.
  Only the `commit-check` wheel is attested; PyGithub and the transitive
  dependencies are pinned by `requirements.txt` but not verified.
- **Network access to PyPI and `api.github.com`** — the pinned wheels are
  downloaded once per run and the attestation is fetched from GitHub.
- **`git`** on `PATH`.

There is currently no input to skip attestation verification.

</details>

<details>
<summary><b>Fork and Dependabot pull requests</b></summary>

A pull request from a fork gets a read-only `GITHUB_TOKEN`, so `pr-comments`
cannot post there. The action skips the comment with a `::warning::` and
notes it in the job summary; the check, the annotations and the summary still
work. For feedback on the pull request itself, use the
[Commit Check GitHub App](https://github.com/apps/commit-check) (free on public
repositories) or run the action on `pull_request_target` — both are covered in
[Fork pull requests](docs/fork-pr-comments.md).

Dependabot pull requests are not forks, but their `pull_request` runs also get
a read-only token by default. The `pull-requests: write` grant in the
[usage example](#usage) is honoured for them; without it the action logs a
`::warning::` on the 403 and leaves the report in the job summary. Adding
`dependabot[bot]` to `ignore_authors` skips those pull requests altogether.

</details>

## Show that you use it

[![commit-check](https://commit-check.com/badge.svg)](https://commit-check.com)

```text
[![commit-check](https://commit-check.com/badge.svg)](https://commit-check.com)
```

## Versioning and feedback

`@v2` follows the latest v2 release; pin a full version or a commit SHA if you
prefer. Releases follow [Semantic Versioning](https://semver.org/). Upgrading
from v1? See the [v2.0.0 release notes](https://github.com/commit-check/commit-check-action/releases/tag/v2.0.0).

Questions and ideas go to [Discussions](https://github.com/commit-check/commit-check/discussions),
bugs and feature requests to [Issues](https://github.com/commit-check/commit-check/issues).
