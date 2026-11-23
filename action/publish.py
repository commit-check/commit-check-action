"""Where results go: the job summary, the result output, the PR comment and
the AI label."""

import json
import os
import sys
import uuid
from typing import Any

from action import cli, context, inputs, model, render

#: Prefixes of report bodies written by earlier versions, kept so the first run
#: after upgrading adopts the existing comment instead of posting a second one.
#: Drop these once a release has been out long enough.
LEGACY_TITLES = ("# Commit Check", "# Commit-Check")

#: The colour a label gets when the Action has to create it.
AI_LABEL_COLOR = "8250df"


def add_job_summary(results: list[model.ScopeResult]) -> int:
    """Adds the commit check result to the GitHub job summary."""
    if not inputs.JOB_SUMMARY_ENABLED or not inputs.GITHUB_STEP_SUMMARY:
        return 0

    with open(inputs.GITHUB_STEP_SUMMARY, "a", encoding="utf-8") as summary_file:
        summary_file.write(render.render_job_summary(results))

    return model.exit_code_for(results)


def set_result_output(results: list[model.ScopeResult]) -> None:
    """Expose the structured results as the ``result`` action output.

    Written through :func:`_write_output`, whose per-write random delimiter
    keeps a line of user text (a commit subject reading ``EOF``, say) from
    closing the heredoc early.
    """
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    payload = {
        "status": model.overall_status(results),
        "ai_tools": ai_tools_found(results),
        "scopes": [
            {
                "label": scope.label,
                "sha": scope.sha,
                "status": scope.status,
                "checks": scope.checks,
            }
            for scope in results
        ],
    }
    with open(output_path, "a", encoding="utf-8") as f:
        _write_output(f, "result", json.dumps(payload, indent=2))


def _write_output(f: Any, name: str, value: str) -> None:
    """Append one multi-line output in the heredoc form ``GITHUB_OUTPUT`` takes.

    The runner reads lines up to the first one equal to the delimiter and
    rejects the whole file if it never finds one, which fails the step and
    drops every output written after the bad one. A fixed ``EOF`` delimiter
    is therefore unsafe: the JSON quotes commit subjects and error text as
    they are, and a subject reading ``EOF`` is legal. So the delimiter is
    random per write, the shape actions/github-script uses.
    """
    delimiter = f"ghadelimiter_{uuid.uuid4()}"
    while delimiter in value:  # pragma: no cover - 122 random bits
        delimiter = f"ghadelimiter_{uuid.uuid4()}"
    f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def is_fork_pr() -> bool:
    """Returns True when the triggering PR originates from a forked repository."""
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path:
        return False
    try:
        with open(event_path, "r", encoding="utf-8") as f:
            event = json.load(f)
        pr = event.get("pull_request", {})
        head_full_name = pr.get("head", {}).get("repo", {}).get("full_name", "")
        base_full_name = pr.get("base", {}).get("repo", {}).get("full_name", "")
        return bool(
            head_full_name and base_full_name and head_full_name != base_full_name
        )
    except Exception:  # noqa: BLE001 - an unreadable payload is not a fork PR
        return False


def is_fork_pr_with_readonly_token() -> bool:
    """Returns True when the PR is from a fork AND the event has a read-only token.

    Under the pull_request event, GITHUB_TOKEN is read-only for fork PRs.
    Under pull_request_target, GITHUB_TOKEN has the workflow's configured
    permissions regardless of whether the PR is from a fork.
    """
    return is_fork_pr() and os.getenv("GITHUB_EVENT_NAME", "") != "pull_request_target"


def get_pr_number() -> int:
    """Extract the pull request number from event payload or GITHUB_REF.

    For pull_request: GITHUB_REF is refs/pull/<number>/merge
    For pull_request_target: GITHUB_REF is refs/heads/<branch> (not useful),
    so we fall back to the event payload.
    """
    ref = os.getenv("GITHUB_REF", "")
    parts = ref.split("/")
    if len(parts) >= 4 and parts[1] == "pull":
        return int(parts[2])
    # Fallback: read PR number from event payload
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if event_path:
        try:
            with open(event_path, "r", encoding="utf-8") as f:
                event = json.load(f)
            number = event.get("number") or (event.get("pull_request", {}) or {}).get(
                "number"
            )
            if number:
                return int(number)
        except Exception:  # noqa: BLE001, S110 - fall through to the error below
            pass
    raise ValueError(
        "Unable to determine PR number from GITHUB_REF or GITHUB_EVENT_PATH"
    )


def _is_bot(comment: Any) -> bool:
    """Whether a comment was posted by a bot account rather than a person."""
    try:
        return comment.user.type == "Bot"
    except Exception:  # noqa: BLE001 - an unknown author is not a bot
        return False


def _find_own_comments(comments: list[Any]) -> tuple[Any | None, list[Any]]:
    """Pick the comment to update and the ones to delete.

    Returns ``(target, stale)``. Only comments a bot posted with
    ``COMMENT_MARKER`` in them are ever edited or deleted: a person can paste
    the marker too (quoting a report, say), and their comment is theirs. A comment from an earlier
    version has no marker, so it is adopted (edited, which adds the marker)
    when there is no marked comment yet, and only if a bot posted it: the
    legacy signal is a title prefix, which a person can type by accident, and
    editing someone's comment out from under them is not recoverable.
    """
    marked = [c for c in comments if render.COMMENT_MARKER in c.body and _is_bot(c)]
    if marked:
        return marked[-1], marked[:-1]

    legacy = [c for c in comments if c.body.startswith(LEGACY_TITLES) and _is_bot(c)]
    return (legacy[-1], []) if legacy else (None, [])


def add_pr_comments(results: list[model.ScopeResult]) -> int:
    """Posts the commit check result as a comment on the pull request."""
    if not inputs.PR_COMMENTS_ENABLED:
        return 0

    # A push has no pull request to comment on. This used to fall through to
    # get_pr_number(), which raised, and every push run carried a warning.
    if not context.is_pr_event():
        print("Skipping PR comment: not a pull request event.")
        return 0

    # Fork PRs triggered by the pull_request event receive a read-only token;
    # the GitHub API will always reject comment writes with 403.
    # pull_request_target events always have the configured token permissions.
    if is_fork_pr_with_readonly_token():
        # Name only the surfaces this run actually produced: with
        # job-summary: false there is no summary to send the reader to.
        where = "in the annotations on the run page"
        if render._summary_written():
            where = f"in this job's summary and {where}"
        msg = (
            "Skipping PR comment: pull requests from forked repositories "
            "cannot write comments via the pull_request event (GITHUB_TOKEN is "
            f"read-only for forks). The findings are {where}. "
            "See https://github.com/commit-check/commit-check-action/blob/main/docs/fork-pr-comments.md"
        )
        print(f"::warning::{msg}")
        if inputs.JOB_SUMMARY_ENABLED and inputs.GITHUB_STEP_SUMMARY:
            with open(inputs.GITHUB_STEP_SUMMARY, "a", encoding="utf-8") as f:
                f.write(
                    "\n---\n"
                    "### \u2139\ufe0f PR Comment Skipped\n\n"
                    "Pull requests from forked repositories cannot write comments "
                    "using the `pull_request` event because `GITHUB_TOKEN` has "
                    "read-only permissions. The report above and the annotations "
                    "on the run page are unaffected.\n\n"
                    "> **\U0001f4a1 Tip:** see "
                    "[Fork pull requests]"
                    "(https://github.com/commit-check/commit-check-action/blob/main/docs/fork-pr-comments.md).\n"
                )
        return 0

    try:
        from github import Auth, Github, GithubException  # type: ignore
    except ImportError as e:
        # Imported here, so it has to be caught here. Leaving it inside the
        # try below would bind GithubException only on success — and an
        # ImportError would then make the `except GithubException` clause
        # itself raise NameError, which propagates past the `except Exception`
        # underneath it and kills a step that is meant to be non-fatal.
        print(f"::warning::Unable to post PR comment: {e}", file=sys.stderr)
        return 0

    try:
        token = os.getenv("GITHUB_TOKEN")
        repo_name = os.getenv("GITHUB_REPOSITORY")
        pr_number = get_pr_number()

        if not token:
            raise ValueError("GITHUB_TOKEN is not set")
        if not repo_name:
            raise ValueError("GITHUB_REPOSITORY is not set")

        api_url = os.getenv("GITHUB_API_URL", "https://api.github.com")
        g = Github(auth=Auth.Token(token), base_url=api_url)
        repo = g.get_repo(repo_name)
        pull_request = repo.get_issue(pr_number)

        pr_comment_body = render.render_pr_comment(results)

        target, stale = _find_own_comments(list(pull_request.get_comments()))

        if target is not None:
            if target.body == pr_comment_body:
                print(f"PR comment already up-to-date for PR #{pr_number}.")
                return model.exit_code_for(results)
            print(f"Updating the last comment on PR #{pr_number}.")
            target.edit(pr_comment_body)
            for comment in stale:
                print(f"Deleting an old comment on PR #{pr_number}.")
                comment.delete()
        else:
            print(f"Creating a new comment on PR #{pr_number}.")
            pull_request.create_comment(body=pr_comment_body)

        return model.exit_code_for(results)
    except GithubException as e:
        if e.status == 403:
            # GithubException.data is whatever the response decoded to, which
            # is None for an empty body and a str for a non-JSON one. Reaching
            # for .get unguarded would raise inside this handler and escape the
            # function, turning the best-effort path into a step failure.
            detail = e.data.get("message") if isinstance(e.data, dict) else None
            print(
                "::warning::Unable to post PR comment (403 Forbidden). "
                "Ensure your workflow grants 'pull-requests: write' permission. "
                f"Error: {detail or e}",
                file=sys.stderr,
            )
            return 0
        # Annotated, not just printed: posting the comment is best-effort and
        # never fails the step, so without an annotation the run is green, the
        # comment is absent, and nothing says why.
        print(f"::warning::Unable to post PR comment: {e}", file=sys.stderr)
        return 0
    except Exception as e:  # noqa: BLE001 - the comment is best-effort
        print(f"::warning::Unable to post PR comment: {e}", file=sys.stderr)
        return 0


def ai_tools_found(results: list[model.ScopeResult]) -> list[str]:
    """Every AI tool found in the pull request, sorted.

    In the checked commits (messages and authors), the title, the
    description, and the app that opened the pull request.
    """
    tools = {tool for scope in results for tool in scope.ai_tools}
    tools.update(cli.PR_BODY_AI_TOOLS)
    if context.is_pr_event():
        opener = context.get_pr_opener_tool()
        if opener:
            tools.add(opener)
    return sorted(tools)


def add_ai_label(results: list[model.ScopeResult]) -> None:
    """Label the pull request when an AI tool wrote any of it (``ai-label``).

    The label is only ever added, never removed: a maintainer may have put
    it there by hand. Best-effort, like the comment: a failure warns and
    never fails the step.
    """
    name = inputs.ai_label_name()
    if not name or not context.is_pr_event():
        return
    tools = ai_tools_found(results)
    if not tools:
        print("No AI tool found in the pull request; no label added.")
        return
    found = ", ".join(tools)
    if is_fork_pr_with_readonly_token():
        print(
            f"::warning::AI tools found ({found}), but the label '{name}' was "
            "not added: pull requests from forked repositories get a read-only "
            "GITHUB_TOKEN under the pull_request event. "
            "See https://github.com/commit-check/commit-check-action/blob/main/docs/fork-pr-comments.md"
        )
        return

    try:
        from github import Auth, Github, GithubException  # type: ignore
    except ImportError as e:
        print(f"::warning::Unable to add the AI label: {e}", file=sys.stderr)
        return

    try:
        token = os.getenv("GITHUB_TOKEN")
        repo_name = os.getenv("GITHUB_REPOSITORY")
        if not token:
            raise ValueError("GITHUB_TOKEN is not set")
        if not repo_name:
            raise ValueError("GITHUB_REPOSITORY is not set")
        pr_number = get_pr_number()
        api_url = os.getenv("GITHUB_API_URL", "https://api.github.com")
        repo = Github(auth=Auth.Token(token), base_url=api_url).get_repo(repo_name)
        issue = repo.get_issue(pr_number)
        if any(label.name == name for label in issue.labels):
            print(f"PR #{pr_number} already has the label '{name}' ({found}).")
            return
        try:
            repo.get_label(name)
        except GithubException as e:
            if e.status != 404:
                raise
            try:
                repo.create_label(
                    name, AI_LABEL_COLOR, "Written with the help of an AI tool"
                )
            except GithubException as created:
                # 422: another run created it in the meantime; use that one.
                if created.status != 422:
                    raise
        issue.add_to_labels(name)
        print(f"Added the label '{name}' to PR #{pr_number}: {found}.")
    except GithubException as e:
        hint = (
            " Ensure your workflow grants 'pull-requests: write' permission."
            if e.status == 403
            else ""
        )
        print(
            f"::warning::Unable to add the AI label.{hint} Error: {e}", file=sys.stderr
        )
    except Exception as e:  # noqa: BLE001 - the label is best-effort
        print(f"::warning::Unable to add the AI label: {e}", file=sys.stderr)
