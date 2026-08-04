#!/usr/bin/env python3
"""GitHub Action that runs commit-check and renders results.

The action runs ``commit-check --format json`` to collect structured check
results (rule IDs, error messages, suggestions, docs links), then renders
them to three output surfaces:

* **step log** — grouped sections with ``::error`` annotations per rule
* **job summary** — a Markdown policy report table
* **PR comment** — a compact Markdown summary (idempotently updated)
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

# Constant for the report title
REPORT_TITLE = "# Commit Check"
COMMIT_MESSAGE_DELIMITER = "\x00"
RULES_URL = "https://commit-check.com/rules/"

GITHUB_STEP_SUMMARY = os.environ["GITHUB_STEP_SUMMARY"]

#: Human-readable labels for the non-message CLI flags.
CHECK_LABELS = {
    "--branch": "Branch",
    "--author-name": "Author name",
    "--author-email": "Author email",
}


def env_flag(name: str, default: str = "false") -> bool:
    """Read a GitHub Action boolean-style environment variable."""
    return os.getenv(name, default).lower() == "true"


def _reconfigure_io() -> None:
    """Reconfigure stdout/stderr to UTF-8 so emoji and check marks never
    crash on runners with legacy encodings (e.g. cp1252 on Windows)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


MESSAGE_ENABLED = env_flag("MESSAGE")
BRANCH_ENABLED = env_flag("BRANCH")
AUTHOR_NAME_ENABLED = env_flag("AUTHOR_NAME")
AUTHOR_EMAIL_ENABLED = env_flag("AUTHOR_EMAIL")
DRY_RUN_ENABLED = env_flag("DRY_RUN")
JOB_SUMMARY_ENABLED = env_flag("JOB_SUMMARY")
PR_COMMENTS_ENABLED = env_flag("PR_COMMENTS")
PR_TITLE_ENABLED = env_flag("PR_TITLE")


@dataclass
class ScopeResult:
    """Result of running commit-check against one scope (PR title, one commit,
    branch, author, ...).

    ``checks`` holds the parsed JSON check outcomes (only set when the CLI
    produced valid JSON); ``raw_text`` holds the raw CLI output when parsing
    failed (a defensive fallback so unexpected output is never swallowed).
    """

    label: str
    checks: list[dict[str, str]] = field(default_factory=list)
    raw_text: str = ""

    @property
    def status(self) -> str:
        """Overall status: ``pass`` when every check passed."""
        if self.raw_text and not self.checks:
            return "fail"
        return "fail" if any(c["status"] == "fail" for c in self.checks) else "pass"

    @property
    def failures(self) -> list[dict[str, str]]:
        """The checks that failed in this scope."""
        return [c for c in self.checks if c["status"] == "fail"]


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
        with open(event_path, "r", encoding="utf-8") as f:
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


def run_check_json(
    args: list[str], input_text: str | None = None
) -> tuple[int, dict[str, Any] | None, str]:
    """Run ``commit-check --format json`` and return (exit code, parsed JSON, raw output).

    The parsed JSON is ``None`` when the CLI did not produce valid JSON; the
    raw output is kept so callers can fall back to showing it as text.
    """
    command = ["commit-check", "--format", "json"] + args
    result = subprocess.run(
        command,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        check=False,
    )
    raw = result.stdout or ""
    try:
        return result.returncode, json.loads(raw), raw
    except json.JSONDecodeError:
        return result.returncode, None, raw


def check_scope(
    label: str, args: list[str], input_text: str | None = None
) -> ScopeResult:
    """Run commit-check for one scope and wrap the outcome in a ScopeResult."""
    _rc, data, raw = run_check_json(args, input_text=input_text)
    if isinstance(data, dict):
        return ScopeResult(label=label, checks=data.get("checks", []))
    return ScopeResult(label=label, raw_text=raw)


def run_pr_message_checks(pr_messages: list[str]) -> list[ScopeResult]:
    """Check each PR commit message individually via commit-check --message."""
    results: list[ScopeResult] = []
    total = len(pr_messages)
    for index, msg in enumerate(pr_messages, start=1):
        results.append(
            check_scope(f"Commit {index}/{total}", ["--message"], input_text=msg)
        )
    return results


def run_other_checks(args: list[str]) -> list[ScopeResult]:
    """Run each non-message check (branch, author) once, as its own scope."""
    results: list[ScopeResult] = []
    for flag in args:
        label = CHECK_LABELS.get(flag)
        if label:
            results.append(check_scope(label, [flag]))
    return results


def build_check_args() -> list[str]:
    """Map enabled validation switches to commit-check CLI arguments."""
    flags = [
        ("--message", MESSAGE_ENABLED),
        ("--branch", BRANCH_ENABLED),
        ("--author-name", AUTHOR_NAME_ENABLED),
        ("--author-email", AUTHOR_EMAIL_ENABLED),
    ]
    return [flag for flag, enabled in flags if enabled]


def run_commit_check() -> tuple[int, list[ScopeResult]]:
    """Runs all enabled checks and returns the overall exit code and results.

    Checks are evaluated in order:
      1. PR title (when ``pr-title: true`` and in a PR event)
      2. Individual PR commit messages (when ``message: true`` and in a PR event)
      3. All remaining checks (branch, author name/email, etc.)

    Outside of a PR event all enabled checks are handed to the CLI at once.
    """
    args = build_check_args()
    results: list[ScopeResult] = []

    # ---- 1. PR title check ------------------------------------------------
    if PR_TITLE_ENABLED and is_pr_event():
        pr_title = get_pr_title()
        if pr_title:
            results.append(check_scope("PR title", ["--message"], input_text=pr_title))

    # ---- 2. Commit message checks -----------------------------------------
    if MESSAGE_ENABLED:
        pr_messages = get_pr_commit_messages()
        if pr_messages:
            # In PR context: check each commit individually to avoid
            # only validating the synthetic merge commit at HEAD.
            results.extend(run_pr_message_checks(pr_messages))
            args = [a for a in args if a != "--message"]

    # ---- 3. Remaining checks (branch, author, etc.) -----------------------
    # Outside a PR, check the HEAD commit message directly.
    if "--message" in args:
        results.append(check_scope("Commit message", ["--message"]))
        args = [a for a in args if a != "--message"]
    results.extend(run_other_checks(args))

    exit_code = 1 if any(scope.status == "fail" for scope in results) else 0
    return exit_code, results


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _rule_label(check: dict[str, str]) -> str:
    """Human-readable label for a check: ``CC001 message`` (kebab-case)."""
    rule_id = check.get("rule_id", "")
    name = check.get("check", "").replace("_", "-")
    return f"{rule_id} {name}" if rule_id else name


def _rule_markdown_link(check: dict[str, str]) -> str:
    """Markdown link for a check: ``[CC001 message](docs_url)``."""
    label = _rule_label(check)
    docs_url = check.get("docs_url", "")
    return f"[{label}]({docs_url})" if docs_url else label


def _scope_group(label: str) -> str:
    """Group name for a scope label, used to fold the step log output."""
    if label == "PR title" or label.startswith("Commit"):
        return "Commit message"
    if label.startswith("Author"):
        return "Author"
    return label


def _grouped(results: list[ScopeResult]) -> list[tuple[str, list[ScopeResult]]]:
    """Split results into ordered groups for step log folding."""
    groups: list[tuple[str, list[ScopeResult]]] = []
    for scope in results:
        group_name = _scope_group(scope.label)
        if groups and groups[-1][0] == group_name:
            groups[-1][1].append(scope)
        else:
            groups.append((group_name, [scope]))
    return groups


def render_step_log(results: list[ScopeResult]) -> None:
    """Print results to the step log with folded groups and error annotations."""
    for group_name, scopes in _grouped(results):
        print(f"::group::{group_name}")
        for scope in scopes:
            if scope.status == "pass":
                print(f"  \u2714 {scope.label}")
                continue
            failures = scope.failures
            count = f" ({len(failures)} failure{'s' if len(failures) != 1 else ''})"
            print(f"  \u2716 {scope.label}{count}")
            if scope.raw_text and not scope.checks:
                # Defensive fallback: commit-check produced unexpected output.
                for line in scope.raw_text.strip().splitlines():
                    print(f"    {line}")
                continue
            for check in failures:
                title = _rule_label(check)
                error = check.get("error", "")
                first_line = error.splitlines()[0] if error else "check failed"
                print(f"::error title={title}::{first_line}")
                if check.get("value"):
                    print(f"    value: {check['value']}")
                if error:
                    print(f"    {error}")
                if check.get("suggest"):
                    print(f"    Suggest: {check['suggest']}")
                if check.get("docs_url"):
                    print(f"    Docs: {check['docs_url']}")
        print("::endgroup::")

    if all(scope.status == "pass" for scope in results):
        print("\u2714 commit-check: all checks passed")


def _failure_count(results: list[ScopeResult]) -> int:
    return sum(len(scope.failures) for scope in results)


def _markdown_table(results: list[ScopeResult]) -> str:
    """Render the scope/result table shared by summary and PR comment."""
    rows = ["| Scope | Failed checks | Result |", "|---|---|---|"]
    for scope in results:
        if scope.status == "pass":
            rows.append(f"| {scope.label} | \u2014 | \u2705 |")
        else:
            links = " \u00b7 ".join(
                _rule_markdown_link(check) for check in scope.failures
            )
            rows.append(f"| {scope.label} | {links} | \u274c |")
    return "\n".join(rows)


def _markdown_details(results: list[ScopeResult]) -> str:
    """Render the collapsible failure details section."""
    sections: list[str] = ["<details>", "<summary>Failure details</summary>", ""]
    for scope in results:
        if scope.status == "pass":
            continue
        sections.append(f"**{scope.label}**")
        sections.append("")
        for check in scope.failures:
            error = check.get("error", "")
            sections.append(f"- **{_rule_markdown_link(check)}** \u2014 {error}")
            if check.get("value"):
                sections.append(f"  - value: `{check['value']}`")
            if check.get("suggest"):
                sections.append(f"  - suggest: {check['suggest']}")
        sections.append("")
    sections.append("</details>")
    return "\n".join(sections)


def _markdown_passed_details(results: list[ScopeResult]) -> str:
    """Render the collapsible section listing which checks passed per scope."""
    rows = ["| Scope | Passed checks |", "|---|---|"]
    for scope in results:
        passed = [c for c in scope.checks if c["status"] == "pass"]
        if passed:
            links = ", ".join(_rule_markdown_link(c) for c in passed)
        else:
            links = "\u2014"
        rows.append(f"| {scope.label} | {links} |")
    return "\n".join(
        ["<details>", "<summary>Show details</summary>", "", *rows, "", "</details>"]
    )


def render_report(results: list[ScopeResult], include_footer: bool = True) -> str:
    """Render the Markdown report shared by the job summary and PR comment.

    The report opens with the plain title line followed by the status line:
    ``✅ All checks passed (N scopes)`` on success, or the failure count on
    failure, followed by a scope table with rule links and collapsible
    failure details.
    """
    if all(scope.status == "pass" for scope in results):
        scopes = "scope" if len(results) == 1 else "scopes"
        lines = [
            REPORT_TITLE,
            "",
            f"✅ All checks passed ({len(results)} {scopes})",
            "",
            _markdown_passed_details(results),
        ]
        return "\n".join(lines)

    failures = _failure_count(results)
    unit = "failure" if failures == 1 else "failures"
    scopes = "scope" if len(results) == 1 else "scopes"
    lines = [
        REPORT_TITLE,
        "",
        f"❌ **{failures} {unit}** across {len(results)} {scopes}",
        "",
        _markdown_table(results),
        "",
        _markdown_details(results),
    ]
    if include_footer:
        lines.extend(["", f"_Rules reference: {RULES_URL}_"])
    return "\n".join(lines)


def render_job_summary(results: list[ScopeResult]) -> str:
    """Create the Markdown body for the GitHub job summary."""
    return render_report(results, include_footer=True)


def render_pr_comment(results: list[ScopeResult]) -> str:
    """Create the Markdown body for the PR comment (same report as summary)."""
    return render_report(results, include_footer=True)


def build_result_body(result_text: str | None) -> str:
    """Legacy helper kept for backward compatibility with existing callers."""
    if result_text is None:
        return REPORT_TITLE
    return f"{REPORT_TITLE}\n```\n{result_text}\n```"


# ---------------------------------------------------------------------------
# Output surfaces
# ---------------------------------------------------------------------------


def add_job_summary(results: list[ScopeResult]) -> int:
    """Adds the commit check result to the GitHub job summary."""
    if not JOB_SUMMARY_ENABLED:
        return 0

    with open(GITHUB_STEP_SUMMARY, "a", encoding="utf-8") as summary_file:
        summary_file.write(render_job_summary(results))

    return 0 if all(scope.status == "pass" for scope in results) else 1


def set_result_output(results: list[ScopeResult]) -> None:
    """Expose the structured results as the ``result`` action output.

    Uses the heredoc form of ``GITHUB_OUTPUT`` so multi-line JSON survives.
    """
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    payload = {
        "status": "pass" if all(s.status == "pass" for s in results) else "fail",
        "scopes": [
            {"label": scope.label, "status": scope.status, "checks": scope.checks}
            for scope in results
        ],
    }
    with open(output_path, "a", encoding="utf-8") as f:
        f.write("result<<EOF\n")
        f.write(json.dumps(payload, indent=2))
        f.write("\nEOF\n")


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
            with open(event_path, "r", encoding="utf-8") as f:
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


def add_pr_comments(results: list[ScopeResult]) -> int:
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
            with open(GITHUB_STEP_SUMMARY, "a", encoding="utf-8") as f:
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

        pr_comment_body = render_pr_comment(results)

        comments = pull_request.get_comments()
        matching_comments = [
            c
            for c in comments
            if c.body.startswith(REPORT_TITLE)
            # Match comments from older versions that used a hyphenated title.
            or c.body.startswith("# Commit-Check")
        ]

        if matching_comments:
            last_comment = matching_comments[-1]
            if last_comment.body == pr_comment_body:
                print(f"PR comment already up-to-date for PR #{pr_number}.")
                return 0 if all(scope.status == "pass" for scope in results) else 1
            print(f"Updating the last comment on PR #{pr_number}.")
            last_comment.edit(pr_comment_body)
            for comment in matching_comments[:-1]:
                print(f"Deleting an old comment on PR #{pr_number}.")
                comment.delete()
        else:
            print(f"Creating a new comment on PR #{pr_number}.")
            pull_request.create_comment(body=pr_comment_body)

        return 0 if all(scope.status == "pass" for scope in results) else 1
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


def log_error_and_exit(ret_code: int, results: list[ScopeResult]) -> None:
    """Logs a summary error to GitHub Actions and exits with the given code."""
    if ret_code != 0 and results:
        failures = _failure_count(results)
        unit = "failure" if failures == 1 else "failures"
        print(f"::error::commit-check found {failures} {unit}.")
    sys.exit(ret_code)


def main():
    """Main function to run commit-check and render all output surfaces."""
    _reconfigure_io()
    log_env_vars()

    ret_code, results = run_commit_check()

    render_step_log(results)
    set_result_output(results)

    ret_code = max(ret_code, add_job_summary(results), add_pr_comments(results))

    if DRY_RUN_ENABLED:
        ret_code = 0

    log_error_and_exit(ret_code, results)


if __name__ == "__main__":
    main()
