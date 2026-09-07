# Fork PR Comments

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

## Option 1: Two-workflow pattern (recommended)

This is the **official GitHub-recommended best practice** for writing PR comments from
fork PRs. It uses the [`workflow_run`](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#workflow_run)
event with **no security risks**.

> 📁 Ready-to-use files: [`examples/commit-check-workflow-a.yml`](../examples/commit-check-workflow-a.yml)
> and [`examples/commit-check-workflow-b.yml`](../examples/commit-check-workflow-b.yml)

**How it works:**

Workflow A runs the checks with `pr-comments: false` and saves the action's
[`report`](../README.md#report) output — the rendered Markdown the job summary shows — plus
the [`result`](../README.md#result) JSON and the PR number, as an artifact. Workflow B,
triggered by `workflow_run` in the base repository, downloads the artifact and posts (or
updates) one comment carrying the `<!-- commit-check-action -->` marker, using `report.md`
verbatim. The fork comment is therefore the same comment the action posts on a non-fork
PR, and a later run with `pr-comments: true` finds and edits it instead of adding a
second one. The artifact contains only `report.md`, `result.json` and `pr-number` — no
code or secrets.

```
  pull_request          workflow_run
      │                      │
      ▼                      ▼
┌──────────────┐     ┌──────────────────┐
│ Workflow A   │     │ Workflow B       │
│ (checks)     │────►│ (comment writer) │
│              │     │                  │
│ Token: READ  │     │ Token: WRITE     │
│ Saves report │     │ Downloads it     │
│ as artifact  │     │ Posts PR comment │
└──────────────┘     └──────────────────┘
```

### Workflow A

`.github/workflows/commit-check.yml` (triggered by `pull_request`):

```yaml
name: Commit Check

on:
  pull_request:
    branches: ["main"]

jobs:
  check:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
      - uses: commit-check/commit-check-action@v2
        id: commit-check
        with:
          message: true
          branch: true
          pr-comments: false   # comments handled by Workflow B
          job-summary: true

      # The action exits 1 on a failure, so both steps below need `always()`
      # or the artifact is missing on exactly the runs that need a comment.
      # The outputs are written before the action exits, so they are present
      # on failing runs too.
      - name: Save the report for Workflow B
        if: always() && steps.commit-check.outputs.report != ''
        env:
          REPORT: ${{ steps.commit-check.outputs.report }}
          RESULT: ${{ steps.commit-check.outputs.result }}
          PR_NUMBER: ${{ github.event.number }}
        run: |
          mkdir -p commit-check-result
          printf '%s\n' "$REPORT"    > commit-check-result/report.md
          printf '%s\n' "$RESULT"    > commit-check-result/result.json
          printf '%s\n' "$PR_NUMBER" > commit-check-result/pr-number
      - uses: actions/upload-artifact@v4
        if: always() && steps.commit-check.outputs.report != ''
        with:
          name: commit-check-result
          path: commit-check-result/
```

> 📄 Full file: [`examples/commit-check-workflow-a.yml`](../examples/commit-check-workflow-a.yml)

### Workflow B

`.github/workflows/commit-check-comment.yml` (triggered by `workflow_run`):

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
      actions: read               # needed to download another run's artifact
    steps:
      # Download by run id: the PR number travels inside the artifact because
      # github.event.workflow_run.pull_requests is empty for fork PRs.
      - uses: actions/download-artifact@v4
        with:
          name: commit-check-result
          run-id: ${{ github.event.workflow_run.id }}
          github-token: ${{ github.token }}
      - name: Post or update the PR comment
        uses: actions/github-script@v7
        with:
          script: |
            // See examples/commit-check-workflow-b.yml for the full script
            const fs = require('fs');
            const prNumber = Number(fs.readFileSync('pr-number', 'utf8').trim());
            const body = fs.readFileSync('report.md', 'utf8');   // posted verbatim
            const MARKER = '<!-- commit-check-action -->';
            // Finds the comment that starts with MARKER and updates it,
            // or creates one when there is none yet
```

> 📄 Full file: [`examples/commit-check-workflow-b.yml`](../examples/commit-check-workflow-b.yml)

### Key security benefits

- Workflow B runs in the **base repository's context**, so `GITHUB_TOKEN` has full write
  permissions (you explicitly grant `pull-requests: write`)
- Workflow B **does not checkout the PR code**, so untrusted fork code never runs
  with elevated permissions
- The artifact only contains `report.md`, `result.json` and `pr-number` — no code or secrets

---

## Option 2: pull_request_target (advanced, use with caution)

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
      - uses: actions/checkout@v7
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
