#!/usr/bin/env python3
import os
import sys
import subprocess
import re


def run_commit_check() -> int:
    args = ["--message", "--branch", "--author-name", "--author-email", "--commit-signoff"]
    args = [arg for arg, value in zip(args, [MESSAGE, BRANCH, AUTHOR_NAME, AUTHOR_EMAIL, COMMIT_SIGNOFF]) if value == "true"]

    command = ["commit-check"] + args
    print(" ".join(command))
    with open("result.txt", "w") as result_file:
        result = subprocess.run(command, stdout=result_file, stderr=subprocess.PIPE, check=False)
        return result.returncode


def add_job_summary() -> int:
    if JOB_SUMMARY == "false":
        sys.exit()

    if os.path.getsize("result.txt") > 0:
        with open("result.txt", "r") as result_file:
            result_text = re.sub(r'\x1B\[[0-9;]*[a-zA-Z]', '', result_file.read())  # Remove ANSI colors

        with open(GITHUB_STEP_SUMMARY, "a") as summary_file:
            summary_file.write("### Commit-Check ❌\n```\n")
            summary_file.write(result_text)
            summary_file.write("\n```")
        return 1
    else:
        with open(GITHUB_STEP_SUMMARY, "a") as summary_file:
            summary_file.write("### Commit-Check ✔️\n")
        return 0


MESSAGE = os.getenv("MESSAGE", "false")
BRANCH = os.getenv("BRANCH", "false")
AUTHOR_NAME = os.getenv("AUTHOR_NAME", "false")
AUTHOR_EMAIL = os.getenv("AUTHOR_EMAIL", "false")
COMMIT_SIGNOFF = os.getenv("COMMIT_SIGNOFF", "false")
DRY_RUN = os.getenv("DRY_RUN", "false")
JOB_SUMMARY = os.getenv("JOB_SUMMARY", "false")
GITHUB_STEP_SUMMARY = os.environ["GITHUB_STEP_SUMMARY"]

print(f"MESSAGE = {MESSAGE}")
print(f"BRANCH = {BRANCH}")
print(f"AUTHOR_NAME = {AUTHOR_NAME}")
print(f"AUTHOR_EMAIL = {AUTHOR_EMAIL}")
print(f"COMMIT_SIGNOFF = {COMMIT_SIGNOFF}")
print(f"DRY_RUN = {DRY_RUN}")
print(f"JOB_SUMMARY = {JOB_SUMMARY}\n")

ret_code = run_commit_check()
ret_code += add_job_summary()  # Combine return codes

if DRY_RUN == "true":
    ret_code = 0

sys.exit(ret_code)
