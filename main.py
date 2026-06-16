#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
from typing import TextIO

# Constants for message titles
SUCCESS_TITLE = "# Commit-Check ✔️"
FAILURE_TITLE = "# Commit-Check ❌"
COMMIT_MESSAGE_DELIMITER = "\x00"
COMMIT_SECTION_SEPARATOR = "\n---\n"

# Environment variables
MESSAGE = os.getenv("MESSAGE", "false")
BRANCH = os.getenv("BRANCH", "false")
AUTHOR_NAME = os.getenv("AUTHOR_NAME", "false")
AUTHOR_EMAIL = os.getenv("AUTHOR_EMAIL", "false")
DRY_RUN = os.getenv("DRY_RUN", "false")
JOB_SUMMARY = os.getenv("JOB_SUMMARY", "false")
PR_COMMENTS = os.getenv("PR_COMMENTS", "false")
PR_TITLE = os.getenv("PR_TITLE", "false")
GITHUB_STEP_SUMMARY = os.environ["GITHUB_STEP_SUMMARY"]


def env_flag(name: str, default: str = "false") -> bool:
    """Read a GitHub Action boolean-style environment variable."""
    return os.getenv(name, default).lower() == "true"


MESSAGE_ENABLED = env_flag("MESSAGE")
BRANCH_ENABLED = env_flag("BRANCH")
AUTHOR_NAME_ENABLED = env_flag("AUTHOR_NAME")
AUTHOR_EMAIL_ENABLED = env_flag("AUTHOR_EMAIL")
DRY_RUN_ENABLED = env_flag("DRY_RUN")
JOB_SUMMARY_ENABLED = env_flag("JOB_SUMMARY")
PR_COMMENTS_ENABLED = env_flag("PR_COMMENTS")
PR_TITLE_ENABLED = env_flag("PR_TITLE")


def log_env_vars():
    """Logs the environment variables for debugging purposes.

    Uses the ``::debug::`` workflow command so these only appear in the
    action log when ``ACTIONS_STEP_DEBUG`` is set to ``true``.
    """
    for name in (
        "MESSAGE",
        "BRANCH",
        "AUTHOR_NAME",
        "AUTHOR_EMAIL",
        "DRY_RUN",
        "JOB_SUMMARY",
        "PR_COMMENTS",
        "PR_TITLE",
    ):
        value = os.getenv(name, "false")
        print(f"::debug::{name}={value}")


def is_pr_event() -> bool:
    """Return whether the workflow was triggered by a PR-style event."""
    return os.getenv("GITHUB_EVENT_NAME", "") in {"pull_request", "pull_request_target"}


def get_pr_title() -> str | None:
    """Read PR title from GitHub event payload."""
    if not is_pr_event():
        return None
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path:
        return None
    try:
        with open(event_path, "r") as f:
            event = json.load(f)
        return event.get("pull_request", {}).get("title")
    except Exception as e:
        print(f"::warning::Failed to read PR title from event: {e}", file=sys.stderr)
        return None


def parse_commit_messages(output: str) -> list[str]:
    """Split git log output into individual commit messages."""
    return [
        message.strip("\n")
        for message in output.split(COMMIT_MESSAGE_DELIMITER)
        if message.strip("\n")
    ]


def get_messages_from_merge_ref() -> list[str]:
    """Read PR commit messages from GitHub's synthetic merge commit."""
    result = subprocess.run(
        ["git", "log", "--pretty=format:%B%x00", "--reverse", "HEAD^1..HEAD^2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        check=False,
    )
    if result.returncode == 0 and result.stdout:
        return parse_commit_messages(result.stdout)
    return []


def get_messages_from_head_ref(base_ref: str) -> list[str]:
    """Read PR commit messages when the workflow checks out the head SHA."""
    result = subprocess.run(
        [
            "git",
            "log",
            "--pretty=format:%B%x00",
            "--reverse",
            f"origin/{base_ref}..HEAD",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        check=False,
    )
    if result.returncode == 0 and result.stdout:
        return parse_commit_messages(result.stdout)
    return []


def get_pr_commit_messages() -> list[str]:
    """Get all commit messages for the current PR workflow.

    In pull_request-style workflows, actions/checkout checks out a synthetic merge
    commit (HEAD = merge of PR branch into base). HEAD^1 is the base branch
    tip, HEAD^2 is the PR branch tip. So HEAD^1..HEAD^2 gives all PR commits.
    If the workflow explicitly checks out the PR head SHA instead, fall back to
    diffing against origin/<base-ref> when that ref is available locally.
    """
    if not is_pr_event():
        return []

    try:
        messages = get_messages_from_merge_ref()
        if messages:
            return messages

        base_ref = os.getenv("GITHUB_BASE_REF", "")
        if base_ref:
            return get_messages_from_head_ref(base_ref)
    except Exception as e:
        print(
            f"::warning::Failed to retrieve PR commit messages: {e}",
            file=sys.stderr,
        )
    return []


def run_check_command(
    args: list[str],
    result_file: TextIO,
    input_text: str | None = None,
    output_prefix: str | None = None,
) -> int:
    """Run commit-check and write both stdout and stderr to the result file."""
    command = ["commit-check"] + args
    result = subprocess.run(
        command,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if result.stdout:
        if output_prefix:
            result_file.write(output_prefix)
        result_file.write(result.stdout.rstrip("\n"))
        result_file.write("\n")
    return result.returncode


def run_pr_message_checks(
    pr_messages: list[str],
    result_file: TextIO,
    initial_emitted: bool = False,
) -> int:
    """Checks each PR commit message individually via commit-check --message.

    Parameters
    ----------
    initial_emitted : bool
        Whether another check (e.g. PR title) has already produced banner output,
        so the first failing commit should use --no-banner.

    Returns 1 if any message fails, 0 if all pass.
    """
    has_failure = False
    emitted_failure_output = initial_emitted
    total = len(pr_messages)
    for index, msg in enumerate(pr_messages, start=1):
        command_args = ["--message"]
        if emitted_failure_output:
            command_args.append("--no-banner")

        if emitted_failure_output:
            output_prefix = f"\n--- Commit {index}/{total}:\n"
        else:
            output_prefix = None

        return_code = run_check_command(
            command_args,
            result_file,
            input_text=msg,
            output_prefix=output_prefix,
        )
        if return_code != 0:
            has_failure = True
            emitted_failure_output = True
    return 1 if has_failure else 0


def run_other_checks(args: list[str], result_file: TextIO) -> int:
    """Runs non-message checks (branch, author) once. Returns 0 if args is empty."""
    if not args:
        return 0
    return run_check_command(args, result_file)


def build_check_args() -> list[str]:
    """Map enabled validation switches to commit-check CLI arguments."""
    flags = [
        ("--message", MESSAGE_ENABLED),
        ("--branch", BRANCH_ENABLED),
        ("--author-name", AUTHOR_NAME_ENABLED),
        ("--author-email", AUTHOR_EMAIL_ENABLED),
    ]
    return [flag for flag, enabled in flags if enabled]


def run_commit_check() -> int:
    """Runs all enabled checks and returns the overall exit code.

    Checks are evaluated in order:
      1. PR title (when ``pr-title: true`` and in a PR event)
      2. Individual PR commit messages (when ``message: true`` and in a PR event)
      3. All remaining checks (branch, author name/email, etc.)

    Outside of a PR event all enabled checks are handed to the CLI at once.
    """
    args = build_check_args()
    exit_code = 0
    emitted_failure_output = False

    with open("result.txt", "w") as result_file:
        # ---- 1. PR title check ------------------------------------------------
        if PR_TITLE_ENABLED and is_pr_event():
            pr_title = get_pr_title()
            if pr_title:
                rc = run_check_command(
                    ["--message"], result_file, input_text=pr_title,
                )
                if rc != 0:
                    exit_code = max(exit_code, rc)
                    emitted_failure_output = True

        # ---- 2. Commit message checks -----------------------------------------
        if MESSAGE_ENABLED:
            pr_messages = get_pr_commit_messages()
            if pr_messages:
                # In PR context: check each commit individually to avoid
                # only validating the synthetic merge commit at HEAD.
                rc = run_pr_message_checks(
                    pr_messages, result_file, initial_emitted=emitted_failure_output
                )
                if rc != 0:
                    exit_code = max(exit_code, rc)
                args = [a for a in args if a != "--message"]

        # ---- 3. Remaining checks (branch, author, etc.) -----------------------
        if args:
            rc = run_other_checks(args, result_file)
            if rc != 0:
                exit_code = max(exit_code, rc)

    return 1 if exit_code else 0


def read_result_file() -> str | None:
    """Reads the result.txt file and removes ANSI color codes."""
    if os.path.getsize("result.txt") > 0:
        with open("result.txt", "r") as result_file:
            result_text = re.sub(
                r"\x1B\[[0-9;]*[a-zA-Z]", "", result_file.read()
            )  # Remove ANSI colors
        return result_text.rstrip()
    return None


def build_result_body(result_text: str | None) -> str:
    """Create the human-readable result body used in summaries and PR comments."""
    if result_text is None:
        return SUCCESS_TITLE
    return f"{FAILURE_TITLE}\n```\n{result_text}\n```"


def add_job_summary() -> int:
    """Adds the commit check result to the GitHub job summary."""
    if not JOB_SUMMARY_ENABLED:
        return 0

    result_text = read_result_file()

    with open(GITHUB_STEP_SUMMARY, "a") as summary_file:
        summary_file.write(build_result_body(result_text))

    return 0 if result_text is None else 1


def is_fork_pr() -> bool:
    """Returns True when the triggering PR originates from a forked repository."""
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path:
        return False
    try:
        with open(event_path, "r") as f:
            event = json.load(f)
        pr = event.get("pull_request", {})
        head_full_name = pr.get("head", {}).get("repo", {}).get("full_name", "")
        base_full_name = pr.get("base", {}).get("repo", {}).get("full_name", "")
        return bool(
            head_full_name and base_full_name and head_full_name != base_full_name
        )
    except Exception:
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
            with open(event_path, "r") as f:
                event = json.load(f)
            number = event.get("number") or (event.get("pull_request", {}) or {}).get(
                "number"
            )
            if number:
                return int(number)
        except Exception:
            pass
    raise ValueError(
        "Unable to determine PR number from GITHUB_REF or GITHUB_EVENT_PATH"
    )


def add_pr_comments() -> int:
    """Posts the commit check result as a comment on the pull request."""
    if not PR_COMMENTS_ENABLED:
        return 0

    # Fork PRs triggered by the pull_request event receive a read-only token;
    # the GitHub API will always reject comment writes with 403.
    # pull_request_target events always have the configured token permissions.
    if is_fork_pr_with_readonly_token():
        msg = (
            "Skipping PR comment: pull requests from forked repositories "
            "cannot write comments via the pull_request event (GITHUB_TOKEN is "
            "read-only for forks). "
            "See https://github.com/commit-check/commit-check-action/blob/main/docs/fork-pr-comments.md "
            "for how to enable PR comments on fork PRs."
        )
        print(f"::warning::{msg}")
        if JOB_SUMMARY_ENABLED:
            with open(GITHUB_STEP_SUMMARY, "a") as f:
                f.write(
                    "\n---\n"
                    "### \u2139\ufe0f PR Comment Skipped\n\n"
                    "Pull requests from forked repositories cannot write comments "
                    "using the `pull_request` event because `GITHUB_TOKEN` has "
                    "read-only permissions.\n\n"
                    "> **\U0001f4a1 Tip:** To enable PR comments on fork PRs, see "
                    "[Enabling PR Comments on Fork Pull Requests]"
                    "(https://github.com/commit-check/commit-check-action/blob/main/docs/fork-pr-comments.md).\n"
                )
        return 0

    try:
        from github import Auth, Github, GithubException  # type: ignore

        token = os.getenv("GITHUB_TOKEN")
        repo_name = os.getenv("GITHUB_REPOSITORY")
        pr_number = get_pr_number()

        if not token:
            raise ValueError("GITHUB_TOKEN is not set")

        g = Github(auth=Auth.Token(token))
        repo = g.get_repo(repo_name)
        pull_request = repo.get_issue(pr_number)

        result_text = read_result_file()
        pr_comment_body = build_result_body(result_text)

        comments = pull_request.get_comments()
        matching_comments = [
            c
            for c in comments
            if c.body.startswith(SUCCESS_TITLE) or c.body.startswith(FAILURE_TITLE)
        ]

        if matching_comments:
            last_comment = matching_comments[-1]
            if last_comment.body == pr_comment_body:
                print(f"PR comment already up-to-date for PR #{pr_number}.")
                return 0
            print(f"Updating the last comment on PR #{pr_number}.")
            last_comment.edit(pr_comment_body)
            for comment in matching_comments[:-1]:
                print(f"Deleting an old comment on PR #{pr_number}.")
                comment.delete()
        else:
            print(f"Creating a new comment on PR #{pr_number}.")
            pull_request.create_comment(body=pr_comment_body)

        return 0 if result_text is None else 1
    except GithubException as e:
        if e.status == 403:
            print(
                "::warning::Unable to post PR comment (403 Forbidden). "
                "Ensure your workflow grants 'issues: write' permission. "
                f"Error: {e.data.get('message', str(e))}",
                file=sys.stderr,
            )
            return 0
        print(f"Error posting PR comment: {e}", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"Error posting PR comment: {e}", file=sys.stderr)
        return 0


def log_error_and_exit(
    failure_title: str, result_text: str | None, ret_code: int
) -> None:
    """
    Logs an error message to GitHub Actions and exits with the specified return code.

    Args:
        failure_title (str): The title of the failure message.
        result_text (str): The detailed result text to include in the error message.
        ret_code (int): The return code to exit with.
    """
    if result_text:
        error_message = f"{failure_title}\n```\n{result_text}\n```"
        print(f"::error::{error_message}")
    sys.exit(ret_code)


def main():
    """Main function to run commit-check, add job summary and post PR comments."""
    log_env_vars()

    ret_code = max(run_commit_check(), add_job_summary(), add_pr_comments())

    if DRY_RUN_ENABLED:
        ret_code = 0

    result_text = read_result_file()
    log_error_and_exit(FAILURE_TITLE, result_text, ret_code)


if __name__ == "__main__":
    main()
