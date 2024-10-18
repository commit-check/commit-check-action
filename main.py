#!/usr/bin/env python3
import os
import sys
import subprocess
import re
from github import Github


# Constants for message titles
SUCCESS_TITLE = "### Commit-Check ✔️\n"
FAILURE_TITLE = "### Commit-Check ❌\n"

# Environment variables
MESSAGE = os.getenv("MESSAGE", "false")
BRANCH = os.getenv("BRANCH", "false")
AUTHOR_NAME = os.getenv("AUTHOR_NAME", "false")
AUTHOR_EMAIL = os.getenv("AUTHOR_EMAIL", "false")
COMMIT_SIGNOFF = os.getenv("COMMIT_SIGNOFF", "false")
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
    ]
    args = [
        arg
        for arg, value in zip(
            args, [MESSAGE, BRANCH, AUTHOR_NAME, AUTHOR_EMAIL, COMMIT_SIGNOFF]
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
        return result_text
    return None


def add_job_summary() -> int:
    """Adds the commit check result to the GitHub job summary."""
    if JOB_SUMMARY == "false":
        return 0

    result_text = read_result_file()

    summary_content = (
        SUCCESS_TITLE
        if result_text is None
        else f"{FAILURE_TITLE}```\n{result_text}\n```"
    )

    with open(GITHUB_STEP_SUMMARY, "a") as summary_file:
        summary_file.write(summary_content)

    return 0 if result_text is None else 1


def add_pr_comments() -> int:
    """Posts the commit check result as a comment on the pull request."""
    if (
        PR_COMMENTS == "false"
        or not GITHUB_TOKEN
        or not GITHUB_REPOSITORY
        or not GITHUB_REF
    ):
        return 0

    try:
        token = os.getenv("GITHUB_TOKEN")
        repo_name = os.getenv("GITHUB_REPOSITORY")
        pr_number = os.getenv("GITHUB_REF").split("/")[-2]

        # Initialize GitHub client
        g = Github(token)
        repo = g.get_repo(repo_name)
        issue = repo.get_issue(int(pr_number))

        # Prepare comment content
        result_text = read_result_file()
        pr_comments = (
            SUCCESS_TITLE
            if result_text is None
            else f"{FAILURE_TITLE}```\n{result_text}\n```"
        )

        issue.create_comment(body=pr_comments)
        return 0 if result_text is None else 1
    except Exception as e:
        print(f"Error posting PR comment: {e}", file=sys.stderr)
        return 1


def main():
    """Main function to run commit-check, add job summary and post PR comments."""
    log_env_vars()

    # Combine return codes
    ret_code = run_commit_check()
    ret_code += add_job_summary()
    ret_code += add_pr_comments()

    if DRY_RUN == "true":
        ret_code = 0

    sys.exit(ret_code)


if __name__ == "__main__":
    main()
