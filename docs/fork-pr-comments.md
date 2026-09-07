# Fork Pull Requests

When a pull request comes from a **forked repository**, the `GITHUB_TOKEN` of the
`pull_request` event is **read-only** by design (GitHub security policy). That single
restriction is worth understanding precisely, because it costs less than it sounds like.

## What a fork contributor still sees

Everything except the comment. The action does not degrade on a fork PR:

| Surface | Fork PR | Why |
|---|---|---|
| The check's pass/fail status | ✅ works | the job's own conclusion |
| `::error` annotations on the **Files changed** tab | ✅ works | workflow commands are written by the runner, not the API |
| The **job summary** — the full report table and details | ✅ works | `$GITHUB_STEP_SUMMARY` is a file on the runner |
| The `result` output for later steps | ✅ works | `$GITHUB_OUTPUT` is a file on the runner |
| A **PR comment** | ❌ skipped | writing a comment needs the API, and the token is read-only |

So a contributor pushing to a fork already gets the red check, the per-finding annotations
on their diff, and the whole report in the job summary. The action says so in the log:

```text
::warning::Skipping PR comment: pull requests from forked repositories cannot write
comments via the pull_request event (GITHUB_TOKEN is read-only for forks). The findings
are in this job's summary and in the annotations on the Files changed tab.
```

(with `job-summary: false` the message names the annotations alone, since there is no
summary to read.)

The run is **not** failed by this: `pr-comments: true` on a fork PR is a no-op, not an error.

## If you want feedback on the pull request itself

### Install the Commit Check GitHub App (recommended)

The [Commit Check GitHub App](https://github.com/marketplace/commit-check) is not bound by
the `pull_request` token at all: it receives the `pull_request` webhook on the **base**
repository and acts with its own installation token. Fork pull requests are ordinary
pull requests to it.

- **No workflow file.** Install it on the repository and it runs.
- **One check run per commit**, with the failing value, the rule and the suggested fix.
  A check run is a first-class PR surface: it shows in the merge box and links straight
  to the details.
- **Free on public repositories and personal accounts** — which is where fork pull
  requests happen. Private organization repositories need the Team plan.

It reports as a check run, not as a comment. If your goal is "the contributor sees what
failed, on the pull request, without me writing YAML", this is the shortest path.

You can run the App and this action together: the App covers fork pull requests, the
action gives you enforcement you control in CI, per-rule outputs for later steps, and
`CCHK_*` overrides.

### Or run on `pull_request_target`

If you cannot install a GitHub App — GitHub Enterprise Server, or an organization policy
that forbids it — `pull_request_target` runs in the context of the base repository, so
`GITHUB_TOKEN` has the permissions your workflow asks for and `pr-comments: true` works
on fork pull requests.

```yaml
on:
  pull_request_target:

permissions:
  contents: read
  pull-requests: write

jobs:
  commit-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          ref: refs/pull/${{ github.event.number }}/merge   # the PR's commits
          fetch-depth: 0
          # Required since checkout v7: without it the step refuses to place
          # fork code in a pull_request_target job, and this workflow never
          # reaches the action below.
          allow-unsafe-pr-checkout: true
      - uses: commit-check/commit-check-action@v2
        with:
          message: true
          branch: true
          pr-comments: true
```

> [!WARNING]
> This is the "pwn request" shape, and `allow-unsafe-pr-checkout: true` is the switch
> `actions/checkout` makes you flip to acknowledge it. The job runs with the base
> repository's `GITHUB_TOKEN`, its secrets, its cache scope and its runner, and the
> checkout puts the fork's code on that runner. commit-check only *reads* commit metadata
> and never executes the checked-out tree, but any other step you add to this job would.
> Keep the job to exactly these two steps, never build, install or cache from the tree,
> and never expose secrets to it. Read
> [Securely using `pull_request_target`](https://gh.io/securely-using-pull_request_target)
> before turning this on.

## What this page used to describe

Earlier versions documented a two-workflow pattern: workflow A runs the checks on
`pull_request` and uploads an artifact, workflow B picks it up on `workflow_run` and posts
the comment with a writable token. It worked, but it cost two workflow files, an artifact
round trip, `actions: read`, and smuggling the PR number through the artifact — all to move
information the contributor could already see into a comment. The App does the same job with
no YAML at all, so the pattern and its example workflows have been removed.
