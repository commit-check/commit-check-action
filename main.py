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

# Environment variables
MESSAGE = os.getenv("MESSAGE", "false")
BRANCH = os.getenv("BRANCH", "false")
AUTHOR_NAME = os.getenv("AUTHOR_NAME", "false")
AUTHOR_EMAIL = os.getenv("AUTHOR_EMAIL", "false")
DRY_RUN = os.getenv("DRY_RUN", "false")
JOB_SUMMARY = os.getenv("JOB_SUMMARY", "false")
PR_COMMENTS = os.getenv("PR_COMMENTS", "false")
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


def log_env_vars():
    """Logs the environment variables for debugging purposes."""
    print(f"MESSAGE = {MESSAGE}")
    print(f"BRANCH = {BRANCH}")
    print(f"AUTHOR_NAME = {AUTHOR_NAME}")
    print(f"AUTHOR_EMAIL = {AUTHOR_EMAIL}")
    print(f"DRY_RUN = {DRY_RUN}")
    print(f"JOB_SUMMARY = {JOB_SUMMARY}")
    print(f"PR_COMMENTS = {PR_COMMENTS}\n")


def is_pr_event() -> bool:
    """Return whether the workflow was triggered by a PR-style event."""
    return os.getenv("GITHUB_EVENT_NAME", "") in {"pull_request", "pull_request_target"}


def parse_commit_messages(output: str) -> list[str]:
    """Split git log output into individual commit messages."""
    return [
        message.rstrip("\n")
        for message in output.split(COMMIT_MESSAGE_DELIMITER)
        if message.rstrip("\n")
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
    args: list[str], result_file: TextIO, input_text: str | None = None
) -> int:
    """Run commit-check and write both stdout and stderr to the result file."""
    command = ["commit-check"] + args
    print(" ".join(command))
    result = subprocess.run(
        command,
        input=input_text,
        stdout=result_file,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return result.returncode


def run_pr_message_checks(pr_messages: list[str], result_file: TextIO) -> int:
    """Checks each PR commit message individually via commit-check --message.

    Returns 1 if any message fails, 0 if all pass.
    """
    has_failure = False
    total_messages = len(pr_messages)
    for index, msg in enumerate(pr_messages, start=1):
        subject = msg.splitlines()[0] if msg else "<empty commit message>"
        result_file.write(f"\n--- Commit {index}/{total_messages}: {subject}\n")
        has_failure = (
            run_check_command(["--message"], result_file, input_text=msg) != 0
            or has_failure
        )
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
    """Runs the commit-check command and logs the result."""
    args = build_check_args()
    with open("result.txt", "w") as result_file:
        if MESSAGE_ENABLED:
            pr_messages = get_pr_commit_messages()
            if pr_messages:
                # In PR context: check each commit message individually to avoid
                # only validating the synthetic merge commit at HEAD.
                message_rc = run_pr_message_checks(pr_messages, result_file)
                other_args = [a for a in args if a != "--message"]
                other_rc = run_other_checks(other_args, result_file)
                return 1 if message_rc or other_rc else 0
        # Non-PR context or message disabled: run all checks at once
        return 1 if run_check_command(args, result_file) else 0


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


def add_pr_comments() -> int:
    """Posts the commit check result as a comment on the pull request."""
    if not PR_COMMENTS_ENABLED:
        return 0

    # Fork PRs triggered by the pull_request event receive a read-only token;
    # the GitHub API will always reject comment writes with 403.
    if is_fork_pr():
        print(
            "::warning::Skipping PR comment: pull requests from forked repositories "
            "cannot write comments via the pull_request event (GITHUB_TOKEN is "
            "read-only for forks). Use the pull_request_target event or the "
            "two-workflow artifact pattern instead. "
            "See https://github.com/commit-check/commit-check-action/issues/77"
        )
        return 0

    try:
        from github import Auth, Github, GithubException  # type: ignore

        token = os.getenv("GITHUB_TOKEN")
        repo_name = os.getenv("GITHUB_REPOSITORY")
        pr_number = os.getenv("GITHUB_REF")
        if pr_number is not None:
            pr_number = pr_number.split("/")[-2]
        else:
            raise ValueError("GITHUB_REF environment variable is not set")

        if not token:
            raise ValueError("GITHUB_TOKEN is not set")

        g = Github(auth=Auth.Token(token))
        repo = g.get_repo(repo_name)
        pull_request = repo.get_issue(int(pr_number))

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
