#!/usr/bin/env python3
import os
import sys
import subprocess
import re
from github import Github  # type: ignore


# Constants for message titles
SUCCESS_TITLE = "# Commit-Check ✔️"
FAILURE_TITLE = "# Commit-Check ❌"

# Environment variables
MESSAGE = os.getenv("MESSAGE", "false")
BRANCH = os.getenv("BRANCH", "false")
AUTHOR_NAME = os.getenv("AUTHOR_NAME", "false")
AUTHOR_EMAIL = os.getenv("AUTHOR_EMAIL", "false")
COMMIT_SIGNOFF = os.getenv("COMMIT_SIGNOFF", "false")
MERGE_BASE = os.getenv("MERGE_BASE", "false")
IMPERATIVE = os.getenv("IMPERATIVE", "true")
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
    print(f"COMMIT_SIGNOFF = {COMMIT_SIGNOFF}")
    print(f"MERGE_BASE = {MERGE_BASE}")
    print(f"IMPERATIVE = {IMPERATIVE}")
    print(f"DRY_RUN = {DRY_RUN}")
    print(f"JOB_SUMMARY = {JOB_SUMMARY}")
    print(f"PR_COMMENTS = {PR_COMMENTS}\n")


def run_commit_check() -> int:
    """Runs the commit-check command and logs the result."""
    args = [
        "--message",
        "--branch",
        "--author-name",
        "--author-email",
        "--commit-signoff",
        "--merge-base",
        "--imperative",
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
                COMMIT_SIGNOFF,
                MERGE_BASE,
                IMPERATIVE,
            ],
        )
        if value == "true"
    ]

    command = ["commit-check"] + args
    print(" ".join(command))
    with open("result.txt", "w") as result_file:
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


def add_pr_comments() -> int:
    """Posts the commit check result as a comment on the pull request."""
    if PR_COMMENTS == "false":
        return 0

    try:
        token = os.getenv("GITHUB_TOKEN")
        repo_name = os.getenv("GITHUB_REPOSITORY")
        pr_number = os.getenv("GITHUB_REF")
        if pr_number is not None:
            pr_number = pr_number.split("/")[-2]
        else:
            # Handle the case where GITHUB_REF is not set
            raise ValueError("GITHUB_REF environment variable is not set")

        # Initialize GitHub client
        g = Github(token)
        repo = g.get_repo(repo_name)
        pull_request = repo.get_issue(int(pr_number))

        # Prepare comment content
        result_text = read_result_file()
        pr_comments = (
            SUCCESS_TITLE
            if result_text is None
            else f"{FAILURE_TITLE}\n```\n{result_text}\n```"
        )

        # Fetch all existing comments on the PR
        comments = pull_request.get_comments()

        # Track if we found a matching comment
        matching_comments = []
        last_comment = None

        for comment in comments:
            if comment.body.startswith(SUCCESS_TITLE) or comment.body.startswith(
                FAILURE_TITLE
            ):
                matching_comments.append(comment)
        if matching_comments:
            last_comment = matching_comments[-1]

            if last_comment.body == pr_comments:
                print(f"PR comment already up-to-date for PR #{pr_number}.")
                return 0
            else:
                # If the last comment doesn't match, update it
                print(f"Updating the last comment on PR #{pr_number}.")
                last_comment.edit(pr_comments)

            # Delete all older matching comments
            for comment in matching_comments[:-1]:
                print(f"Deleting an old comment on PR #{pr_number}.")
                comment.delete()
        else:
            # No matching comments, create a new one
            print(f"Creating a new comment on PR #{pr_number}.")
            pull_request.create_comment(body=pr_comments)

        return 0 if result_text is None else 1
    except Exception as e:
        print(f"Error posting PR comment: {e}", file=sys.stderr)
        return 1


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
