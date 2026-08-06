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
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

COMMIT_MESSAGE_DELIMITER = "\x00"
RULES_URL = "https://commit-check.com/rules/"

#: Hidden marker identifying comments this action owns.
#
# Comment identity has to be something a human cannot type by accident. The
# previous title-prefix match meant any comment opening with "# Commit Check"
# was treated as ours — and old ones are deleted, not just skipped. An HTML
# comment is invisible in the rendered body and is what Codecov, SonarQube and
# CodSpeed all use for the same purpose.
COMMENT_MARKER = "<!-- commit-check-action -->"

#: Logo shown next to the report title.
#
# Served from this repository rather than commit-check.com so the report has no
# cross-repository dependency, and as PNG rather than SVG because GitHub proxies
# comment images through camo, which handles SVG unreliably. Point this at a
# single org-wide asset if the other tools grow the same header.
LOGO_URL = (
    "https://raw.githubusercontent.com/commit-check/commit-check-action/main/"
    "assets/logo.png"
)

#: Report heading. h2 rather than h1: this renders inside a PR comment, where an
#: h1 is louder than anything else on the page.
REPORT_TITLE = f'## <img src="{LOGO_URL}" width="20" align="top" alt=""> Commit Check'

#: Prefixes of report bodies written by earlier versions, kept so the first run
#: after upgrading adopts the existing comment instead of posting a second one.
#: Drop these once a release has been out long enough.
LEGACY_TITLES = ("# Commit Check", "# Commit-Check")

GITHUB_STEP_SUMMARY = os.getenv("GITHUB_STEP_SUMMARY", "")

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


def _render_scopes(scopes: list[ScopeResult], include_docs: bool) -> list[str]:
    """Render the indented listing for one group of scopes, without its header.

    Shared by both output surfaces so they cannot drift: the step log and the
    Markdown details block are the same tree, and the only difference is the
    docs link, which the Markdown report already carries on the rule ID in the
    table above it.

    A failing scope shows its value in full rather than truncated. It is the one
    value the reader has to act on, and the table's 60-character cap can cut off
    the part that explains the failure.
    """
    lines: list[str] = []
    for scope in scopes:
        if scope.status == "pass":
            value = _scope_value(scope)
            lines.append(f"  ✔ {scope.label}{f' ({value})' if value else ''}")
            continue
        if scope.raw_text and not scope.checks:
            # Defensive fallback: commit-check produced unexpected output.
            lines.append(f"  ✖ {scope.label}")
            lines.extend(f"      {ln}" for ln in scope.raw_text.strip().splitlines())
            continue
        failures = scope.failures
        count = f" ({len(failures)} failure{'s' if len(failures) != 1 else ''})"
        lines.append(f"  ✖ {scope.label}{count}")
        for check in failures:
            lines.append(f"      {_rule_label(check)}")
            if check.get("value"):
                lines.append(f"        value: {check['value']}")
            for line in check.get("error", "").splitlines():
                lines.append(f"        {line}")
            if check.get("suggest"):
                lines.append(f"        Suggest: {check['suggest']}")
            if include_docs and check.get("docs_url"):
                lines.append(f"        Docs: {check['docs_url']}")
    return lines


def _render_tree(results: list[ScopeResult], include_docs: bool) -> list[str]:
    """Render the full grouped listing: a header line per group, then its scopes."""
    lines: list[str] = []
    for group_name, scopes in _grouped(results):
        lines.append(group_name)
        lines.extend(_render_scopes(scopes, include_docs))
    return lines


def _annotation_escape(text: str) -> str:
    """Escape text for a workflow command payload.

    A newline would end the command and leave the rest of the message as a
    stray log line, and a bare ``%`` can be read as the start of an escape.
    """
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def render_step_log(results: list[ScopeResult]) -> None:
    """Print results to the step log, then emit one annotation per failure.

    The two are separated deliberately. An ``::error`` command renders as a
    line of its own wherever it is printed, so emitting one inside the indented
    listing broke the tree apart, and its ``title=`` \u2014 which is what carries the
    rule ID \u2014 is only shown in the annotations UI, never inline. Printing the
    detail once in the listing and the annotations after all the groups keeps
    the log readable and still surfaces failures in the run summary and on the
    Files changed tab.
    """
    # The tree is grouped, so it is printed group by group rather than in one
    # block: ::group:: and ::endgroup:: have to bracket each section's lines.
    for group_name, scopes in _grouped(results):
        print(f"::group::{group_name}")
        for line in _render_scopes(scopes, include_docs=True):
            print(line)
        print("::endgroup::")

    annotations: list[tuple[str, str]] = []
    for scope in results:
        if scope.status == "pass":
            continue
        if scope.raw_text and not scope.checks:
            annotations.append(
                (f"commit-check: {scope.label}", "output could not be parsed")
            )
            continue
        for check in scope.failures:
            error = check.get("error", "")
            first_line = error.splitlines()[0] if error else "check failed"
            annotations.append((_rule_label(check), f"{scope.label}: {first_line}"))

    for title, message in annotations:
        print(
            f"::error title={_annotation_escape(title)}"
            f"::{_annotation_escape(message)}"
        )

    if not annotations:
        print("\u2714 commit-check: all checks passed")


def _check_counts(results: list[ScopeResult]) -> tuple[int, int]:
    """Return ``(failed, total)`` where one check is one thing that was checked.

    A "check" here is a scope \u2014 one commit message, the branch, the author name
    \u2014 not one rule evaluation. Counting rule evaluations produced a number that
    grew with the size of the pull request rather than with the strictness of
    the policy: sixteen commit messages against six enabled rules reported
    "1 of 100 checks failed", where 96 of the 100 were the same six rules run
    again per commit. The large denominator also made a real failure look
    negligible \u2014 one bad commit out of fifteen reads very differently from
    1 of 100.

    This number matches what the reader can count: the rows in the table plus
    the \u2714/\u2716 lines in the details block. Which rules failed is not lost, it is
    just reported where it belongs \u2014 in the table and the details.
    """
    failed = sum(1 for scope in results if scope.status == "fail")
    return failed, len(results)


def _failure_count(results: list[ScopeResult]) -> int:
    """Number of scopes that failed."""
    return _check_counts(results)[0]


def _markdown_table(results: list[ScopeResult]) -> str:
    """Render the failure table shared by summary and PR comment.

    Only failed scopes appear, so a per-row result column would read ``\u274c`` on
    every row and carry no information; the pass/fail picture for everything
    else lives in the details block.
    """
    rows = [
        "| Scope | Checked value | Failed checks |",
        "|---|---|---|",
    ]
    for scope in results:
        if scope.status == "pass":
            continue
        value = _scope_value(scope)
        value_display = f"`{value}`" if value else "\u2014"
        if scope.raw_text and not scope.checks:
            links = "_output could not be parsed \u2014 see details_"
        else:
            links = " \u00b7 ".join(
                _rule_markdown_link(check) for check in scope.failures
            )
        rows.append(f"| {scope.label} | {value_display} | {links} |")
    return "\n".join(rows)


def _markdown_details(results: list[ScopeResult]) -> str:
    """Render the collapsible details block listing every scope.

    Mirrors the step log layout (group name, ✔/✖ scope lines with the checked
    value) and adds the failure reason and suggestion under each failing rule,
    so one expand answers both "what was checked" and "what failed and why".
    The same block is used whether or not anything failed — on a clean run the
    failure branches simply never fire.
    """
    _failed, total = _check_counts(results)
    unit = "check" if total == 1 else "checks"
    label = f"Show all {total} {unit}" if total else "Show details"
    lines = ["<details>", f"<summary>{label}</summary>", "", "```text"]
    lines.extend(_render_tree(results, include_docs=False))
    lines.extend(["```", "", "</details>"])
    return "\n".join(lines)


def _scope_value(scope: ScopeResult, max_len: int = 60) -> str:
    """First non-empty check value for a scope, trimmed to a single line.

    The value is the concrete thing that was checked (PR title, commit
    subject, branch name, author name/email) and reads naturally next to
    the scope label in the success details. The 60-character cap keeps the
    full line (prefix + value + parentheses) short enough to avoid wrapping
    in the fenced details block.
    """
    for check in scope.checks:
        value = check.get("value", "")
        if value:
            first_line = value.splitlines()[0].strip()
            if len(first_line) > max_len:
                return first_line[: max_len - 3] + "..."
            return first_line
    return ""


# ---------------------------------------------------------------------------
# Output specification
#
# The Markdown report shared by the job summary and the PR comment renders
# as follows (values are filled from ScopeResult data):
#
# Every body opens with COMMENT_MARKER, which is invisible when rendered and is
# how the action recognises its own PR comment on the next run.
#
# Success:
#
#   <!-- commit-check-action -->
#   ## <img src="..." width="20" align="top" alt=""> Commit Check
#
#   ✅ **All 5 checks passed**
#
#   <details>
#   <summary>Show all 5 checks</summary>
#
#   ```text
#   Commit message
#     ✔ PR title (feat: add login page)
#     ✔ Commit 1/11 (feat: add user auth)
#   Branch
#     ✔ Branch (feature/add-login)
#   Author
#     ✔ Author name (Jane Doe)
#     ✔ Author email (jane@example.com)
#   ```
#
#   </details>
#
#   _commit-check 2.13.1 · [Rules reference](https://commit-check.com/rules/)_
#
# Failure:
#
#   <!-- commit-check-action -->
#   ## <img src="..." width="20" align="top" alt=""> Commit Check
#
#   ❌ **1 of 5 checks failed**
#
#   | Scope | Checked value | Failed checks |
#   |---|---|---|
#   | Commit 2/11 | `bad msg` | [CC001 message](https://commit-check.com/rules/#cc001) |
#
#   <details>
#   <summary>Show all 5 checks</summary>
#
#   ```text
#   Commit message
#     ✔ PR title (feat: add login page)
#     ✖ Commit 2/11 (1 failure)
#         CC001 message
#           value: bad msg
#           The commit message should follow Conventional Commits.
#           Suggest: Use <type>(<scope>): <description>
#   Branch
#     ✔ Branch (feature/add-login)
#   ```
#
#   </details>
#
#   _commit-check 2.13.1 · [Rules reference](https://commit-check.com/rules/)_
#
# Notes:
# - One check is one thing that was checked — a commit message, the branch, the
#   author — not one rule evaluation. The total therefore matches the number of
#   ✔/✖ lines the reader can count in the details block, and does not grow with
#   the number of commits in the pull request or rules in the config.
# - The table lists only failed scopes; there is no per-row result column
#   because it would read ❌ on every row. Passing scopes live in the details.
# - Values are capped at 60 characters with a literal "..." suffix, except on a
#   failing scope, where the details block prints the value in full — it is the
#   one value the reader has to act on and the cap can hide the reason.
# - The step log renders the same tree (_render_scopes); it adds the docs URL,
#   which the Markdown report already carries on the rule ID in the table.
# ---------------------------------------------------------------------------


def _commit_check_version() -> str:
    """Version of the commit-check CLI that produced these results."""
    try:
        from importlib.metadata import version

        return version("commit-check")
    except Exception:
        return ""


def _report_footer() -> str:
    """Attribution line: which version ran, and where the rules are documented.

    The version is the first thing worth knowing when a result looks wrong, and
    it is otherwise buried in the step log.
    """
    rules = f"[Rules reference]({RULES_URL})"
    installed = _commit_check_version()
    return f"_commit-check {installed} · {rules}_" if installed else f"_{rules}_"


def render_report(results: list[ScopeResult]) -> str:
    """Render the Markdown report shared by the job summary and PR comment.

    Opens with the hidden marker and the title, then a one-line verdict —
    ``✅ **All N checks passed**`` or ``❌ **N of M checks failed**`` — then the
    failure table (failures only) and the collapsible per-scope details.
    """
    failed, total = _check_counts(results)
    unit = "check" if total == 1 else "checks"

    lines = [COMMENT_MARKER, REPORT_TITLE, ""]
    if failed == 0:
        lines.append(f"✅ **All {total} {unit} passed**")
        lines.append("")
    else:
        lines.append(f"❌ **{failed} of {total} {unit} failed**")
        lines.extend(["", _markdown_table(results), ""])
    lines.extend([_markdown_details(results), "", _report_footer()])
    return "\n".join(lines)


def render_job_summary(results: list[ScopeResult]) -> str:
    """Create the Markdown body for the GitHub job summary."""
    return render_report(results)


def render_pr_comment(results: list[ScopeResult]) -> str:
    """Create the Markdown body for the PR comment (same report as summary)."""
    return render_report(results)


# ---------------------------------------------------------------------------
# Output surfaces
# ---------------------------------------------------------------------------


def add_job_summary(results: list[ScopeResult]) -> int:
    """Adds the commit check result to the GitHub job summary."""
    if not JOB_SUMMARY_ENABLED or not GITHUB_STEP_SUMMARY:
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


def _is_bot(comment: Any) -> bool:
    """Whether a comment was posted by a bot account rather than a person."""
    try:
        return comment.user.type == "Bot"
    except Exception:
        return False


def _find_own_comments(comments: list[Any]) -> tuple[Any | None, list[Any]]:
    """Pick the comment to update and the ones to delete.

    Returns ``(target, stale)``. Only comments carrying ``COMMENT_MARKER`` are
    ever deleted — those are unambiguously ours. A comment from an earlier
    version has no marker, so it is adopted (edited, which adds the marker)
    when there is no marked comment yet, and only if a bot posted it: the
    legacy signal is a title prefix, which a person can type by accident, and
    editing someone's comment out from under them is not recoverable.
    """
    marked = [c for c in comments if COMMENT_MARKER in c.body]
    if marked:
        return marked[-1], marked[:-1]

    legacy = [c for c in comments if c.body.startswith(LEGACY_TITLES) and _is_bot(c)]
    return (legacy[-1], []) if legacy else (None, [])


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
        if JOB_SUMMARY_ENABLED and GITHUB_STEP_SUMMARY:
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

        g = Github(auth=Auth.Token(token))
        repo = g.get_repo(repo_name)
        pull_request = repo.get_issue(pr_number)

        pr_comment_body = render_pr_comment(results)

        target, stale = _find_own_comments(list(pull_request.get_comments()))

        if target is not None:
            if target.body == pr_comment_body:
                print(f"PR comment already up-to-date for PR #{pr_number}.")
                return 0 if all(scope.status == "pass" for scope in results) else 1
            print(f"Updating the last comment on PR #{pr_number}.")
            target.edit(pr_comment_body)
            for comment in stale:
                print(f"Deleting an old comment on PR #{pr_number}.")
                comment.delete()
        else:
            print(f"Creating a new comment on PR #{pr_number}.")
            pull_request.create_comment(body=pr_comment_body)

        return 0 if all(scope.status == "pass" for scope in results) else 1
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
    except Exception as e:
        print(f"::warning::Unable to post PR comment: {e}", file=sys.stderr)
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
