#!/usr/bin/env python3
"""GitHub Action that runs commit-check and renders results.

The action runs ``commit-check --format json`` to collect structured check
results (rule IDs, error messages, suggestions, docs links), then renders
them to three output surfaces:

* **step log** — grouped sections, then one ``::error`` annotation per finding
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

#: Report heading. h2 rather than h1: this renders inside a PR comment, where an
#: h1 is louder than anything else on the page.
#
# Deliberately carries no logo. The org mark is a check inside a rounded tile,
# which is the same object GitHub's ✅ is — same shape, same silhouette, only
# the hue differs. Putting it immediately above the verdict line meant a failing
# report opened with a tick and then said "❌ 2 of 4 checks failed": the first
# symbol the eye lands on contradicted the second, and it did so precisely when
# the reader most needs to read the result quickly.
#
# The verdict line is the one status signal in this report, and one is the right
# number. "Commit Check" in words is unambiguous branding; a checkmark next to a
# failure is not.
#
# Do not "fix" this by showing the logo only on success — that makes the logo's
# presence itself a status signal, which is the same defect wearing a hat.
REPORT_TITLE = "## Commit Check"

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

    ``sha`` is the full hash of the commit a message scope checked, or ``""``
    for scopes that have no commit (PR title, branch, author). It is kept
    apart from ``label`` on purpose: ``label`` ("Commit 2/3") is what the
    ``result`` output has always carried, so downstream steps can keep
    matching on it, and the hash is appended only where a person reads it.
    """

    label: str
    checks: list[dict[str, str]] = field(default_factory=list)
    raw_text: str = ""
    sha: str = ""

    @property
    def display_label(self) -> str:
        """The label as a reader sees it: ``Commit 2/3 (5584f46)``.

        Seven characters, like ``git log --oneline``; the full hash goes
        into the link and the ``result`` output, where it is not read by eye.
        """
        return f"{self.label} ({self.sha[:7]})" if self.sha else self.label

    @property
    def status(self) -> str:
        """Overall status: ``pass``, ``fail``, ``warn``, or ``skip``.

        ``skip`` means every rule in this scope declined to run — the author
        is on an ``ignore_authors`` list, or there was nothing to check. It
        is reported separately from ``pass`` because a skipped scope
        validated nothing, and rendering the two identically let a bypassed
        policy read as an enforced one.

        ``warn`` means a rule found something but is listed under the
        config's top-level ``warn``, so commit-check reports it without
        failing the run. A real failure still outranks a warning in the same
        scope (CC202 stands in for ``branch`` when the two disagree), and a
        single real verdict — failing or warning — outranks the skips: a
        scope is ``skip`` only when *all* of its checks skipped.
        """
        if self.raw_text and not self.checks:
            return "fail"
        if any(c["status"] == "fail" for c in self.checks):
            return "fail"
        if any(c["status"] == "warn" for c in self.checks):
            return "warn"
        if self.checks and all(c["status"] == "skip" for c in self.checks):
            return "skip"
        return "pass"

    @property
    def failures(self) -> list[dict[str, str]]:
        """The checks that failed in this scope."""
        return [c for c in self.checks if c["status"] == "fail"]

    @property
    def warnings(self) -> list[dict[str, str]]:
        """The checks reported as warnings in this scope."""
        return [c for c in self.checks if c["status"] == "warn"]


def overall_status(results: list[ScopeResult]) -> str:
    """Reduce scope statuses to one of ``pass``/``fail``/``warn``/``skip``.

    One function, used by every completion path, because the alternative
    is what this replaced: four separate ``all(... == "pass")`` tests, each
    correct only while exactly two statuses existed. The moment ``skip``
    appeared they all silently reclassified a skipped run as a failure.

    Precedence is fail, then skip, then warn, then pass. ``skip`` requires
    at least one scope and all of them skipped: nothing was validated, and a
    warning cannot have been found where nothing ran. ``warn`` means at
    least one scope carries a finding the config listed under ``warn`` and
    nothing failed. It is distinct from ``pass`` so a downstream step can
    act on a bent-but-not-broken policy without walking every scope; the
    exit code does not distinguish the two (see ``exit_code_for``), because
    the whole point of ``warn`` is that it never fails the workflow.
    """
    if any(scope.status == "fail" for scope in results):
        return "fail"
    if results and all(scope.status == "skip" for scope in results):
        return "skip"
    if any(scope.status == "warn" for scope in results):
        return "warn"
    return "pass"


def exit_code_for(results: list[ScopeResult]) -> int:
    """Only a failure is an error.

    A skipped run validated nothing, but it violated no policy either, so
    it must not fail the workflow.
    """
    return 1 if overall_status(results) == "fail" else 0


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


#: The one fix for every "history is too shallow" finding below.
SHALLOW_CHECKOUT_HINT = "is actions/checkout using fetch-depth: 0?"

#: On pull_request_target the default checkout is the base branch, which
#: holds none of the pull request at any depth; the fix is to check the
#: pull request out.
TARGET_CHECKOUT_HINT = (
    "is the workflow checking out the pull request, e.g. "
    "ref: refs/pull/<number>/merge with fetch-depth: 0?"
)


def checkout_hint() -> str:
    """The fix for a checkout that does not hold the pull request."""
    if os.getenv("GITHUB_EVENT_NAME") == "pull_request_target":
        return TARGET_CHECKOUT_HINT
    return SHALLOW_CHECKOUT_HINT


#: The pull request branch tip. On ``refs/pull/N/merge`` HEAD is a merge
#: commit that GitHub authored, so its recorded author is
#: ``GitHub <noreply@github.com>`` whatever the contributor configured;
#: HEAD^2 is the commit the contributor actually made.
PR_HEAD_REV = "HEAD^2"

#: The checks whose subject is a commit's recorded author.
AUTHOR_FLAGS = ("--author-name", "--author-email")


def warn_shallow_checkout(problem: str, consequence: str) -> None:
    """Annotate a PR run whose clone is too shallow to do what was asked.

    ``actions/checkout`` defaults to ``fetch-depth: 1``, which leaves the
    synthetic merge commit as the only commit in the clone. Every caller
    hits that same root cause, so they share one message shape that names
    the fix rather than only the symptom.
    """
    text = f"{problem} ({checkout_hint()}); {consequence}"
    print(f"::warning title=commit-check::{_annotation_escape(text)}")


def get_pr_event() -> dict[str, Any]:
    """The ``pull_request`` object from the event payload, or ``{}``."""
    if not is_pr_event():
        return {}
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    try:
        with open(event_path, "r", encoding="utf-8") as f:
            event = json.load(f)
        return event.get("pull_request") or {}
    except Exception as e:
        print(f"::warning::Failed to read the PR from the event: {e}", file=sys.stderr)
        return {}


def get_pr_head_sha() -> str | None:
    """The pull request's head commit, from the event payload."""
    return get_pr_event().get("head", {}).get("sha") or None


def get_pr_base_sha() -> str | None:
    """The base branch tip the pull request targets, from the event payload."""
    return get_pr_event().get("base", {}).get("sha") or None


def _rev_resolves(rev: str) -> bool:
    """Whether ``rev`` names a commit the clone actually has."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def pr_head_rev() -> str | None:
    """The commit whose recorded author the PR's author checks read.

    First choice is ``pull_request.head.sha`` from the event payload: it
    names the branch tip for ``pull_request`` and ``pull_request_target``
    alike, whatever was checked out, as long as the clone has it. Failing
    that, ``HEAD^2`` on a ``pull_request`` checkout, where HEAD is the
    merge ref and its second parent is that same tip. Never ``HEAD^2`` on
    ``pull_request_target``: there HEAD is the base branch, so ``HEAD^2``
    is nothing, or the parent of some unrelated merge on it.

    ``None`` when the clone is too shallow to hold either.
    """
    sha = get_pr_head_sha()
    if sha and _rev_resolves(sha):
        return sha
    if os.getenv("GITHUB_EVENT_NAME") == "pull_request" and _rev_resolves(PR_HEAD_REV):
        return PR_HEAD_REV
    return None


#: The rule each author check runs, for a scope that had to be skipped.
AUTHOR_RULES = {
    "--author-name": ("CC101", "author_name"),
    "--author-email": ("CC102", "author_email"),
}


def skipped_author_scope(flag: str) -> ScopeResult:
    """A scope recording that an author check could not run at all.

    Reported as ``skip``, never as a pass: nothing was validated, and the
    one commit the clone does hold (HEAD) has the wrong author for a pull
    request, GitHub's merge commit or the base branch.
    """
    rule_id, check = AUTHOR_RULES[flag]
    return ScopeResult(
        label=CHECK_LABELS[flag],
        checks=[
            {
                "rule_id": rule_id,
                "check": check,
                "status": "skip",
                "value": "",
                "error": "",
                "suggest": "",
                "docs_url": "",
            }
        ],
    )


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


#: One commit to check: ``(full sha, message)``.
Commit = tuple[str, str]

#: ``git log`` format that yields, per commit, the full hash and the raw
#: message as two NUL-terminated fields (``%x00`` is git's spelling of
#: COMMIT_MESSAGE_DELIMITER; a literal NUL cannot be passed as an argument).
#: NUL cannot appear in a hash or a message, so the split is unambiguous
#: however many blank lines the message holds.
COMMIT_LOG_FORMAT = "--pretty=format:%H%x00%B%x00"


def parse_commit_messages(output: str) -> list[Commit]:
    """Split ``git log`` output (see ``COMMIT_LOG_FORMAT``) into commits.

    The fields alternate hash, message, hash, message; git puts a newline
    between commits, which lands on the front of the next hash and is
    stripped along with the message's trailing newline. Commits are paired
    before empty messages are dropped, so a blank message never shifts the
    hashes of the commits after it.
    """
    fields = [f.strip("\n") for f in output.split(COMMIT_MESSAGE_DELIMITER)]
    return [
        (sha, message) for sha, message in zip(fields[0::2], fields[1::2]) if message
    ]


def _messages_in_range(revision_range: str) -> list[Commit]:
    """Commits in ``revision_range`` as ``(sha, message)``, oldest first, or ``[]``."""
    result = subprocess.run(
        ["git", "log", COMMIT_LOG_FORMAT, "--reverse", revision_range],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        check=False,
    )
    if result.returncode == 0 and result.stdout:
        return parse_commit_messages(result.stdout)
    return []


def head_sha() -> str:
    """The full hash of HEAD, or ``""`` when git cannot say.

    Only decoration for the non-PR ``Commit message`` scope, so a failure
    here never fails the run: the message is still checked, just unlabelled.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def get_messages_from_event_range() -> list[Commit]:
    """Read PR commit messages between the payload's base and head commits.

    ``pull_request.base.sha`` and ``pull_request.head.sha`` name the pull
    request whatever the workflow checked out, for ``pull_request`` and
    ``pull_request_target`` alike; the range is usable whenever the clone
    holds both commits.
    """
    base_sha, head_sha = get_pr_base_sha(), get_pr_head_sha()
    if not (base_sha and head_sha):
        return []
    if not (_rev_resolves(head_sha) and _rev_resolves(base_sha)):
        return []
    return _messages_in_range(f"{base_sha}..{head_sha}")


def get_messages_from_merge_ref() -> list[Commit]:
    """Read PR commit messages from GitHub's synthetic merge commit.

    Only meaningful on a ``pull_request`` checkout, where HEAD is
    ``refs/pull/N/merge``. On ``pull_request_target`` HEAD is the base
    branch: ``HEAD^2`` is then nothing, or the parent of some unrelated
    merge on it, whose commits are not the pull request's.
    """
    if os.getenv("GITHUB_EVENT_NAME") != "pull_request":
        return []
    return _messages_in_range("HEAD^1..HEAD^2")


def get_messages_from_head_ref(base_ref: str) -> list[Commit]:
    """Read PR commit messages when the workflow checks out the head SHA."""
    return _messages_in_range(f"origin/{base_ref}..HEAD")


def get_pr_commit_messages() -> list[Commit]:
    """Get all commits, as ``(sha, message)``, for the current PR workflow.

    The event payload's ``base.sha..head.sha`` is tried first: it names the
    pull request exactly, whatever was checked out. On a ``pull_request``
    checkout HEAD is the synthetic merge commit, so ``HEAD^1..HEAD^2`` is
    the same range. If the workflow checks out the PR head SHA instead,
    diff against ``origin/<base-ref>`` when that ref is available locally.
    """
    if not is_pr_event():
        return []

    try:
        messages = get_messages_from_event_range()
        if messages:
            return messages

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
    label: str, args: list[str], input_text: str | None = None, sha: str = ""
) -> ScopeResult:
    """Run commit-check for one scope and wrap the outcome in a ScopeResult.

    ``sha`` names the commit a message scope checked; it rides along on both
    outcomes so an unparsable CLI response still says which commit it was.
    """
    _rc, data, raw = run_check_json(args, input_text=input_text)
    if isinstance(data, dict):
        return ScopeResult(label=label, checks=data.get("checks", []), sha=sha)
    return ScopeResult(label=label, raw_text=raw, sha=sha)


def run_pr_message_checks(pr_commits: list[Commit]) -> list[ScopeResult]:
    """Check each PR commit message individually via commit-check --message."""
    results: list[ScopeResult] = []
    total = len(pr_commits)
    for index, (sha, msg) in enumerate(pr_commits, start=1):
        results.append(
            check_scope(
                f"Commit {index}/{total}", ["--message"], input_text=msg, sha=sha
            )
        )
    return results


def run_other_checks(args: list[str], rev: str | None = None) -> list[ScopeResult]:
    """Run each non-message check (branch, author) once, as its own scope.

    ``rev`` goes to the author checks only: it names the commit whose
    recorded author is validated (commit-check >= 2.16.0), which in a PR is
    the branch tip rather than GitHub's merge commit. The branch check has
    no commit to point at, so it never takes it.
    """
    results: list[ScopeResult] = []
    for flag in args:
        label = CHECK_LABELS.get(flag)
        if label:
            cli_args = [flag]
            if rev and flag in AUTHOR_FLAGS:
                cli_args += ["--rev", rev]
            results.append(check_scope(label, cli_args))
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
        pr_commits = get_pr_commit_messages()
        if pr_commits:
            # In PR context: check each commit individually to avoid
            # only validating the synthetic merge commit at HEAD.
            results.extend(run_pr_message_checks(pr_commits))
            args = [a for a in args if a != "--message"]
        elif is_pr_event():
            # Falling through to HEAD validates the synthetic merge commit,
            # "Merge X into Y", which passes CC001 by default: a shallow
            # clone used to turn every pull request green without a word.
            warn_shallow_checkout(
                "Could not list the pull request's commits", "only HEAD was checked"
            )

    # ---- 3. Remaining checks (branch, author, etc.) -----------------------
    # Outside a PR, check the HEAD commit message directly.
    if "--message" in args:
        results.append(check_scope("Commit message", ["--message"], sha=head_sha()))
        args = [a for a in args if a != "--message"]
    rev = None
    if is_pr_event() and any(flag in AUTHOR_FLAGS for flag in args):
        rev = pr_head_rev()
        if rev is None:
            # HEAD's author is GitHub's merge commit on a pull_request
            # checkout and the base branch on pull_request_target: checking
            # it would grade the wrong person either way. Say so, and skip.
            warn_shallow_checkout(
                "Could not resolve the pull request's head commit for the "
                "author checks",
                "they were skipped",
            )
            for flag in args:
                if flag in AUTHOR_FLAGS:
                    results.append(skipped_author_scope(flag))
            args = [a for a in args if a not in AUTHOR_FLAGS]
    results.extend(run_other_checks(args, rev=rev))

    exit_code = exit_code_for(results)
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


def _finding_lines(check: dict[str, str], include_error: bool) -> list[str]:
    """The detail a reader needs to act on one finding, one item per line.

    ``value:`` (what was checked), the error (when ``include_error``),
    ``Suggest:`` and ``Fix:`` — each only when the CLI filled it in. This is
    the single source for both the tree and the annotation payload, so the
    two cannot drift; the error is optional because the annotation carries
    its first line in the headline instead.

    ``Fix:`` is the corrected text itself, ready to paste. When a rule has a
    fix but no bespoke advice the CLI sets ``suggest`` to ``Use "<fix>"``,
    so printing both would say the same thing twice in a row; in exactly
    that case only ``Fix:`` is shown. A multi-line fix (a signed-off body)
    takes one row per line so the trailer lands where it would in the
    message.
    """
    lines: list[str] = []
    if check.get("value"):
        lines.append(f"value: {check['value']}")
    if include_error:
        lines.extend(check.get("error", "").splitlines())
    fix = check.get("fix", "")
    suggest = check.get("suggest", "")
    if suggest and suggest != f'Use "{fix}"':
        lines.append(f"Suggest: {suggest}")
    if fix:
        first, *rest = fix.splitlines()
        lines.append(f"Fix: {first}")
        lines.extend(rest)
    return lines


def _render_findings(checks: list[dict[str, str]], include_docs: bool) -> list[str]:
    """Render the indented detail lines for a list of check entries.

    Shared by the failure and warning branches of ``_render_scopes`` — the
    same rule label / value / error / suggestion / fix / docs layout,
    whichever list it is called with.
    """
    lines: list[str] = []
    for check in checks:
        lines.append(f"      {_rule_label(check)}")
        detail = _finding_lines(check, include_error=True)
        lines.extend(f"        {line}" for line in detail)
        if include_docs and check.get("docs_url"):
            lines.append(f"        Docs: {check['docs_url']}")
    return lines


def _render_scopes(scopes: list[ScopeResult], include_docs: bool) -> list[str]:
    """Render the indented listing for one group of scopes, without its header.

    Shared by both output surfaces so they cannot drift: the step log and the
    Markdown details block are the same tree, and the only difference is the
    docs link, which the Markdown report already carries on the rule ID in the
    table above it.

    A failing or warned scope shows its value in full rather than truncated.
    It is the one value the reader has to act on, and the table's 60-character
    cap can cut off the part that explains it.
    """
    lines: list[str] = []
    for scope in scopes:
        label = scope.display_label
        if scope.status == "skip":
            # Deliberately not a ✔. Nothing was validated here, and a tick
            # claiming otherwise is what made a bypassed policy look enforced.
            lines.append(f"  ⊘ {label} (skipped)")
            continue
        if scope.status == "pass":
            value = _scope_value(scope)
            lines.append(f"  ✔ {label}{f' ({value})' if value else ''}")
            continue
        if scope.raw_text and not scope.checks:
            # Defensive fallback: commit-check produced unexpected output.
            lines.append(f"  ✖ {label}")
            lines.extend(f"      {ln}" for ln in scope.raw_text.strip().splitlines())
            continue
        # A scope's status names its worst outcome (fail beats warn), but the
        # two are not exclusive: CC2xx covers both branch and merge_base, so
        # one can fail while the other only warns. Render whichever of the
        # two lists is non-empty, rather than only the one the status names —
        # a warning on an otherwise-failing scope is still a finding to fix.
        failures = scope.failures
        if failures:
            count = f" ({len(failures)} failure{'s' if len(failures) != 1 else ''})"
            lines.append(f"  ✖ {label}{count}")
            lines.extend(_render_findings(failures, include_docs))
        warnings = scope.warnings
        if warnings:
            count = f" ({len(warnings)} warning{'s' if len(warnings) != 1 else ''})"
            lines.append(f"  ⚠ {label}{count}")
            lines.extend(_render_findings(warnings, include_docs))
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


def _annotation_body(scope: ScopeResult, check: dict[str, str]) -> str:
    """The message of one finding's annotation, before escaping.

    First line: which commit and what rule said, ``Commit 2/3 (5584f46):
    Subject must start with a capital letter``. Then the same value /
    Suggest / Fix rows the tree prints, so the annotation on the Files
    changed tab is enough to act on without opening the step log. GitHub
    renders the escaped newlines as line breaks.
    """
    error = check.get("error", "")
    fallback = "check warning" if check.get("status") == "warn" else "check failed"
    first_line = error.splitlines()[0] if error else fallback
    lines = [f"{scope.display_label}: {first_line}"]
    lines.extend(_finding_lines(check, include_error=False))
    return "\n".join(lines)


def render_step_log(results: list[ScopeResult]) -> None:
    """Print results to the step log, then emit one annotation per finding.

    The tree and the annotations are separated deliberately. An ``::error`` or
    ``::warning`` command renders as a line of its own wherever it is printed,
    so emitting one inside the indented listing broke the tree apart, and its
    ``title=`` \u2014 which is what carries the rule ID \u2014 is only shown in the
    annotations UI, never inline. Printing the listing first and the
    annotations after all the groups keeps the log readable and still
    surfaces findings in the run summary and on the Files changed tab — where
    the annotation is all a reader has, so its message repeats the value,
    suggestion and fix from the tree (``_annotation_body``), one per line.

    A failure becomes an ``::error``, a warning a ``::warning`` \u2014 GitHub
    renders the two differently, and only the errors count toward the
    friendly one-line verdict claiming nothing failed.

    Under ``dry-run`` a failure is still reported, but as a ``::warning``:
    an ``::error`` annotation on a step that then exits 0 reads as a
    contradiction, and the run's error count would claim a failure the job
    did not have. The verdict line says the same thing in words.
    """
    # The tree is grouped, so it is printed group by group rather than in one
    # block: ::group:: and ::endgroup:: have to bracket each section's lines.
    for group_name, scopes in _grouped(results):
        print(f"::group::{group_name}")
        for line in _render_scopes(scopes, include_docs=True):
            print(line)
        print("::endgroup::")

    errors: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []
    for scope in results:
        if scope.status in ("pass", "skip"):
            continue
        if scope.raw_text and not scope.checks:
            errors.append(
                (f"commit-check: {scope.display_label}", "output could not be parsed")
            )
            continue
        for check in scope.failures:
            errors.append((_rule_label(check), _annotation_body(scope, check)))
        for check in scope.warnings:
            warnings.append((_rule_label(check), _annotation_body(scope, check)))

    level = "warning" if DRY_RUN_ENABLED else "error"
    for title, message in errors:
        print(
            f"::{level} title={_annotation_escape(title)}"
            f"::{_annotation_escape(message)}"
        )
    for title, message in warnings:
        print(
            f"::warning title={_annotation_escape(title)}"
            f"::{_annotation_escape(message)}"
        )

    # The verdict is a plain line, never an ::error: the findings above are
    # already one annotation each, and a second, untitled ::error for the
    # total inflated the run's error count and told the reader nothing new.
    if errors:
        failed, total = _check_counts(results)
        if DRY_RUN_ENABLED:
            print(
                f"commit-check (dry-run): {failed} of {total} checks failed; "
                "not failing the job"
            )
        else:
            verdict = f"✖ commit-check: {failed} of {total} checks failed"
            warned = _warn_count(results)
            if warned:
                verdict += f", {warned} warning{'s' if warned != 1 else ''}"
            print(verdict)
    if not errors:
        skipped, warned, total = (
            _skip_count(results),
            _warn_count(results),
            len(results),
        )
        if total and skipped == total:
            print("\u2298 commit-check: all checks skipped, nothing was validated")
        elif warned or skipped:
            passed = total - skipped - warned
            tail = []
            if warned:
                tail.append(f"{warned} warning{'s' if warned != 1 else ''}")
            if skipped:
                tail.append(f"{skipped} skipped")
            print(
                f"\u2714 commit-check: {passed} of {total} checks passed, {', '.join(tail)}"
            )
        else:
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


def _skip_count(results: list[ScopeResult]) -> int:
    """Number of scopes that never ran.

    Reported separately from the pass count so the headline cannot claim
    that checks passed when they were skipped.
    """
    return sum(1 for scope in results if scope.status == "skip")


def _warn_count(results: list[ScopeResult]) -> int:
    """Number of scopes that carry at least one warning.

    Counts by ``scope.warnings``, not ``scope.status == "warn"``: a scope
    whose overall status is "fail" (CC2xx covers both ``branch`` and
    ``merge_base``, so one can fail while the other only warns) still has
    warnings to report, and this is the count the verdict and the table use
    to decide whether to show them.
    """
    return sum(1 for scope in results if scope.warnings)


def _markdown_table(
    results: list[ScopeResult], status: str = "fail", header: str = "Failed checks"
) -> str:
    """Render the failure or warning table shared by summary and PR comment.

    A scope appears when it has an entry of the requested kind \u2014 checked via
    ``scope.failures`` / ``scope.warnings``, not ``scope.status`` \u2014 so a scope
    that both failed and warned gets a row in both tables. Filtering on the
    scope's single overall status would silently drop its warnings once a
    failure in the same scope outranked them.
    """
    rows = [
        f"| Scope | Checked value | {header} |",
        "|---|---|---|",
    ]
    is_raw_only_failure = (
        status == "fail"
    )  # a raw-text scope has no checks to warn about
    for scope in results:
        entries = scope.failures if status == "fail" else scope.warnings
        raw_failure = is_raw_only_failure and scope.raw_text and not scope.checks
        # A skipped or clean-passing scope has no matching entries and is not
        # a raw-text failure, so it contributes no row \u2014 an empty accusation
        # in a table headed "Failed checks" or "Warnings" otherwise.
        if not entries and not raw_failure:
            continue
        value = _scope_value(scope)
        value_display = f"`{value}`" if value else "\u2014"
        if raw_failure:
            links = "_output could not be parsed \u2014 see details_"
        else:
            links = " \u00b7 ".join(_rule_markdown_link(check) for check in entries)
        rows.append(f"| {_scope_markdown_label(scope)} | {value_display} | {links} |")
    return "\n".join(rows)


def _commit_url(sha: str) -> str:
    """Link to a commit on the GitHub instance the workflow runs against.

    ``GITHUB_SERVER_URL`` is what makes this right on GitHub Enterprise
    Server; without ``GITHUB_REPOSITORY`` there is nothing to link into, so
    the caller falls back to plain text rather than guess.
    """
    repository = os.getenv("GITHUB_REPOSITORY", "")
    if not (sha and repository):
        return ""
    server = os.getenv("GITHUB_SERVER_URL") or "https://github.com"
    return f"{server.rstrip('/')}/{repository}/commit/{sha}"


def _scope_markdown_label(scope: ScopeResult) -> str:
    """The table's Scope cell: the label, linked to the commit when there is one.

    The link lives here rather than in the tree because the tree is a fenced
    code block, where Markdown does not render; the table row is the one
    place a reader can click through to the commit.
    """
    url = _commit_url(scope.sha)
    label = scope.display_label
    return f"[{label}]({url})" if url else label


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
#   ## Commit Check
#
#   ✅ **All 5 checks passed**
#
#   <details>
#   <summary>Show all 5 checks</summary>
#
#   ```text
#   Commit message
#     ✔ PR title (feat: add login page)
#     ✔ Commit 1/11 (d87faca) (feat: add user auth)
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
# A commit scope names its commit by short hash after the label, on every
# surface; the label itself ("Commit 1/11") stays bare in the `result` output.
#
# Skipped (every rule declined to run — e.g. the author is in ignore_authors):
#
#   ⊘ **All 5 checks skipped** — nothing was validated
#
#   ```text
#   Commit message
#     ⊘ PR title (skipped)
#     ⊘ Commit 1/1 (skipped)
#   Branch
#     ⊘ Branch (skipped)
#   Author
#     ⊘ Author name (skipped)
#     ⊘ Author email (skipped)
#   ```
#
# A skipped scope deliberately carries no ✔ and no checked value: nothing was
# examined, so there is no value to report and no pass to claim. When only some
# scopes skip, the verdict reads "✅ **3 of 5 checks passed**, 2 skipped".
#
# Warning (the repository's config lists a rule under the top-level `warn`):
#
#   <!-- commit-check-action -->
#   ## Commit Check
#
#   ✅ **3 of 4 checks passed**, 1 warning
#
#   | Scope | Checked value | Warnings |
#   |---|---|---|
#   | Branch | `jsmith/fix-x` | [CC201 branch](https://commit-check.com/rules/#cc201) |
#
#   <details>
#   <summary>Show all 4 checks</summary>
#
#   ```text
#   Commit message
#     ✔ PR title (feat: add login page)
#   Branch
#     ⚠ Branch (1 warning)
#         CC201 branch
#           value: jsmith/fix-x
#           The branch should follow Conventional Branch.
#           Suggest: Use <type>/<description>
#   Author
#     ✔ Author name (Jane Doe)
#     ✔ Author email (jane@example.com)
#   ```
#
#   </details>
#
#   _commit-check 2.17.0 · [Rules reference](https://commit-check.com/rules/)_
#
# A warned scope is reported exactly like a failing one — its own table row,
# its own entry in the details block — except the marker is ⚠ rather than ✖,
# it counts toward "passed" rather than "failed", and it never turns the
# workflow red. A real failure elsewhere still fails the run; the verdict
# then reads "❌ **N of M checks failed**, K warnings" and both tables appear.
#
# Failure:
#
#   <!-- commit-check-action -->
#   ## Commit Check
#
#   ❌ **2 of 5 checks failed**
#
#   | Scope | Checked value | Failed checks |
#   |---|---|---|
#   | [Commit 2/11 (5584f46)](https://github.com/acme/widgets/commit/5584f46…) | `bad msg` | [CC001 message](https://commit-check.com/rules/#cc001) |
#   | [Commit 3/11 (37d6def)](https://github.com/acme/widgets/commit/37d6def…) | `feat: add login page` | [CC002 subject-capitalized](https://commit-check.com/rules/#cc002) |
#
#   <details>
#   <summary>Show all 5 checks</summary>
#
#   ```text
#   Commit message
#     ✔ PR title (feat: add login page)
#     ✖ Commit 2/11 (5584f46) (1 failure)
#         CC001 message
#           value: bad msg
#           The commit message should follow Conventional Commits.
#           Suggest: Use <type>(<scope>): <description>
#     ✖ Commit 3/11 (37d6def) (1 failure)
#         CC002 subject-capitalized
#           value: feat: add login page
#           Subject must start with a capital letter
#           Fix: feat: Add login page
#   Branch
#     ✔ Branch (feature/add-login)
#   ```
#
#   </details>
#
#   _commit-check 2.13.1 · [Rules reference](https://commit-check.com/rules/)_
#
# The step log prints the same tree (plus a `Docs:` line per finding), then one
# annotation per finding whose message is the tree's detail joined on `%0A`:
#
#   ::error title=CC002 subject-capitalized::Commit 3/11 (37d6def): Subject must start with a capital letter%0Avalue: feat: add login page%0AFix: feat: Add login page
#   ✖ commit-check: 2 of 5 checks failed
#
# The verdict is a plain line, not an `::error`, so the run's error count is
# the number of findings.
#
# Notes:
# - The table's Scope cell links to the commit (GITHUB_SERVER_URL, so it is
#   right on GitHub Enterprise Server too); the tree cannot, being a code block.
# - `Fix:` is the corrected text the CLI proposes, when the correction is
#   mechanical (CC002 capitalisation, CC010 WIP marker, CC012 sign-off, ...).
#   When the CLI's `suggest` is just `Use "<fix>"`, only `Fix:` is printed. A
#   multi-line fix takes one row per line.
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
    failure table (failures only), a warnings table when the config listed
    any rule under ``warn``, and the collapsible per-scope details.

    A warned scope counts toward "passed", never toward "failed": it ran,
    found something, and is reported in full, but the config asked for it to
    be surfaced rather than enforced.
    """
    failed, total = _check_counts(results)
    warned = _warn_count(results)
    skipped = _skip_count(results)
    unit = "check" if total == 1 else "checks"

    lines = [COMMENT_MARKER, REPORT_TITLE, ""]
    if failed:
        verdict = f"❌ **{failed} of {total} {unit} failed**"
        if warned:
            verdict += f", {warned} warning{'s' if warned != 1 else ''}"
        lines.append(verdict)
        lines.extend(["", _markdown_table(results), ""])
        if warned:
            lines.extend([_markdown_table(results, "warn", "Warnings"), ""])
    elif total and skipped == total:
        # Nothing ran, so there is no success to announce. Saying "all
        # checks passed" here is the defect this branch exists to prevent.
        lines.append(f"⊘ **All {total} {unit} skipped** — nothing was validated")
        lines.append("")
    elif warned or skipped:
        passed = total - skipped - warned
        tail = []
        if warned:
            tail.append(f"{warned} warning{'s' if warned != 1 else ''}")
        if skipped:
            tail.append(f"{skipped} skipped")
        lines.append(f"✅ **{passed} of {total} {unit} passed**, {', '.join(tail)}")
        lines.append("")
        if warned:
            lines.extend([_markdown_table(results, "warn", "Warnings"), ""])
    else:
        lines.append(f"✅ **All {total} {unit} passed**")
        lines.append("")
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

    return exit_code_for(results)


def set_result_output(results: list[ScopeResult]) -> None:
    """Expose the structured results as the ``result`` action output.

    Uses the heredoc form of ``GITHUB_OUTPUT`` so multi-line JSON survives.
    """
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    payload = {
        "status": overall_status(results),
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

    # A push has no pull request to comment on. This used to fall through to
    # get_pr_number(), which raised, and every push run carried a warning.
    if not is_pr_event():
        print("Skipping PR comment: not a pull request event.")
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
                return exit_code_for(results)
            print(f"Updating the last comment on PR #{pr_number}.")
            target.edit(pr_comment_body)
            for comment in stale:
                print(f"Deleting an old comment on PR #{pr_number}.")
                comment.delete()
        else:
            print(f"Creating a new comment on PR #{pr_number}.")
            pull_request.create_comment(body=pr_comment_body)

        return exit_code_for(results)
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


def log_error_and_exit(ret_code: int) -> None:
    """Exit with the given code.

    This used to print ``::error::commit-check found N failures.`` first.
    That was a second ``::error`` annotation on top of the one-per-finding
    annotations ``render_step_log`` had already emitted, so GitHub counted
    one error more than there were findings and listed an untitled entry
    that only restated the titled ones. The verdict now lives in
    ``render_step_log`` as a plain line, next to the passing verdicts.
    """
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

    log_error_and_exit(ret_code)


if __name__ == "__main__":
    main()
