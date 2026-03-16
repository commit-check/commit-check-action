#!/usr/bin/env python3
import json
import os
import sys
import subprocess
import re
from github import Github, Auth, GithubException  # type: ignore


# Constants for message titles
SUCCESS_TITLE = "# Commit-Check ✔️"
FAILURE_TITLE = "# Commit-Check ❌"

# Environment variables
MESSAGE = os.getenv("MESSAGE", "false")
BRANCH = os.getenv("BRANCH", "false")
AUTHOR_NAME = os.getenv("AUTHOR_NAME", "false")
AUTHOR_EMAIL = os.getenv("AUTHOR_EMAIL", "false")
DRY_RUN = os.getenv("DRY_RUN", "false")
JOB_SUMMARY = os.getenv("JOB_SUMMARY", "false")
PR_COMMENTS = os.getenv("PR_COMMENTS", "false")
GITHUB_STEP_SUMMARY = os.environ["GITHUB_STEP_SUMMARY"]
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")
GITHUB_REF = os.getenv("GITHUB_REF")


def log_env_vars():
    """Logs the environment variables for debugging purposes."""
    print(f"MESSAGE = {MESSAGE}")
    print(f"BRANCH = {BRANCH}")
    print(f"AUTHOR_NAME = {AUTHOR_NAME}")
    print(f"AUTHOR_EMAIL = {AUTHOR_EMAIL}")
    print(f"DRY_RUN = {DRY_RUN}")
    print(f"JOB_SUMMARY = {JOB_SUMMARY}")
    print(f"PR_COMMENTS = {PR_COMMENTS}\n")


def get_pr_commit_messages() -> list[str]:
    """Get all commit messages for the current PR (pull_request event only).

    In a pull_request event, actions/checkout checks out a synthetic merge
    commit (HEAD = merge of PR branch into base). HEAD^1 is the base branch
    tip, HEAD^2 is the PR branch tip. So HEAD^1..HEAD^2 gives all PR commits.
    """
    if os.getenv("GITHUB_EVENT_NAME", "") != "pull_request":
        return []
    try:
        result = subprocess.run(
            ["git", "log", "--pretty=format:%B%x00", "HEAD^1..HEAD^2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            check=False,
        )
        if result.returncode == 0 and result.stdout:
            return [m.strip() for m in result.stdout.split("\x00") if m.strip()]
    except Exception:
        pass
    return []


def run_commit_check() -> int:
    """Runs the commit-check command and logs the result."""
    args = [
        "--message",
        "--branch",
        "--author-name",
        "--author-email",
    ]
    args = [
        arg
        for arg, value in zip(
            args,
            [
                MESSAGE,
                BRANCH,
                AUTHOR_NAME,
                AUTHOR_EMAIL,
            ],
        )
        if value == "true"
    ]

    total_rc = 0
    with open("result.txt", "w") as result_file:
        if MESSAGE == "true":
            pr_messages = get_pr_commit_messages()
            if pr_messages:
                # In PR context: check each commit message individually to avoid
                # only validating the synthetic merge commit at HEAD.
                for msg in pr_messages:
                    result = subprocess.run(
                        ["commit-check", "--message"],
                        input=msg,
                        stdout=result_file,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                    total_rc += result.returncode

                # Run non-message checks (branch, author) once
                other_args = [a for a in args if a != "--message"]
                if other_args:
                    command = ["commit-check"] + other_args
                    print(" ".join(command))
                    result = subprocess.run(
                        command, stdout=result_file, stderr=subprocess.PIPE, check=False
                    )
                    total_rc += result.returncode

                return total_rc

        # Non-PR context or message disabled: run all checks at once
        command = ["commit-check"] + args
        print(" ".join(command))
        result = subprocess.run(
            command, stdout=result_file, stderr=subprocess.PIPE, check=False
        )
        return result.returncode


def read_result_file() -> str | None:
    """Reads the result.txt file and removes ANSI color codes."""
    if os.path.getsize("result.txt") > 0:
        with open("result.txt", "r") as result_file:
            result_text = re.sub(
                r"\x1B\[[0-9;]*[a-zA-Z]", "", result_file.read()
            )  # Remove ANSI colors
        return result_text.rstrip()
    return None


def add_job_summary() -> int:
    """Adds the commit check result to the GitHub job summary."""
    if JOB_SUMMARY == "false":
        return 0

    result_text = read_result_file()

    summary_content = (
        SUCCESS_TITLE
        if result_text is None
        else f"{FAILURE_TITLE}\n```\n{result_text}\n```"
    )

    with open(GITHUB_STEP_SUMMARY, "a") as summary_file:
        summary_file.write(summary_content)

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
    if PR_COMMENTS == "false":
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

        # Prepare comment content
        result_text = read_result_file()
        pr_comment_body = (
            SUCCESS_TITLE
            if result_text is None
            else f"{FAILURE_TITLE}\n```\n{result_text}\n```"
        )

        # Fetch all existing comments on the PR
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

    # Combine return codes
    ret_code = run_commit_check()
    ret_code += add_job_summary()
    ret_code += add_pr_comments()

    if DRY_RUN == "true":
        ret_code = 0

    result_text = read_result_file()
    log_error_and_exit(FAILURE_TITLE, result_text, ret_code)


if __name__ == "__main__":
    main()
