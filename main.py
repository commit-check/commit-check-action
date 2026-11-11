#!/usr/bin/env python3
"""GitHub Action that runs commit-check and renders results.

The action runs ``commit-check --format json`` to collect structured check
results (rule IDs, error messages, suggestions, docs links), then renders
them to three output surfaces:

* **step log** — grouped sections, then one ``::error`` annotation per finding
* **job summary** — a Markdown report: the verdict, then a diff of what failed
* **PR comment** — the same report (idempotently updated)

and exposes the check data as JSON in the ``result`` action output.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
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


#: Boolean inputs whose value was neither true nor false, as ``(name, value)``.
#: ``main`` fails the step on them before any check runs.
INVALID_FLAGS: list[tuple[str, str]] = []


def env_flag(name: str, default: str = "false") -> bool:
    """Read a GitHub Action boolean input: ``true`` or ``false``, any case.

    Anything else (``yes``, ``on``, a typo) used to read as false and turn
    the check off without a word. It is now recorded in ``INVALID_FLAGS``,
    and the step fails with the input's name.
    """
    value = os.getenv(name, default).strip().lower()
    if value not in ("true", "false"):
        INVALID_FLAGS.append((name, os.getenv(name, default)))
    return value == "true"


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
PR_BODY_AI_ATTRIBUTION_ENABLED = env_flag("PR_BODY_AI_ATTRIBUTION")
MESSAGE_AI_ATTRIBUTION_ENABLED = env_flag("MESSAGE_AI_ATTRIBUTION")


#: The AI attribution rules: what ``message-ai-attribution: true`` keeps.
AI_RULE_IDS = frozenset({"CC013", "CC014", "CC015", "CC016", "CC017"})


#: The label ``ai-label: true`` puts on a pull request that an AI tool wrote.
DEFAULT_AI_LABEL = "ai-assisted"
#: The colour a label gets when the Action has to create it.
AI_LABEL_COLOR = "8250df"


def ai_label_name() -> str | None:
    """The label to put on an AI-written pull request, or ``None`` for none.

    ``ai-label: true`` means the default label; any other value but
    ``false`` is the label's own name.
    """
    value = os.getenv("AI_LABEL", "false").strip()
    if not value or value.lower() == "false":
        return None
    return DEFAULT_AI_LABEL if value.lower() == "true" else value


#: AI coding agents that open pull requests from their own GitHub App, by
#: login, with the tool each one is. The same list as the GitHub App's.
AI_AGENT_BOTS = {
    "amazon-q-developer[bot]": "Amazon Q Developer",
    "chatgpt-codex-connector[bot]": "OpenAI Codex",
    "claude[bot]": "Claude Code",
    "copilot-swe-agent[bot]": "GitHub Copilot",
    "cursor[bot]": "Cursor",
    "devin-ai-integration[bot]": "Devin",
    "factory-droid[bot]": "Factory Droid",
    "google-labs-jules[bot]": "Jules",
}


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
    #: The AI tools the CLI found in what this scope checked (``ai_tools``).
    ai_tools: list[str] = field(default_factory=list)
    #: The text this scope checked (a commit message, the PR title), when the
    #: action piped it in. The report compares it with a rule's ``fix`` to name
    #: the lines to change. Never part of the ``result`` output.
    text: str = ""
    #: ``(removed, added)`` lines per rule, for a text too long to carry as a
    #: ``fix`` (the PR description). Keyed by ``_rule_key``.
    edits: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)

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
    def skip_reason(self) -> str:
        """Why a skipped scope did not run, or ``""`` when nobody said.

        Newer commit-check releases name the reason on each skipped check (e.g.
        "author dependabot[bot] is in [commit].ignore_authors"). A bare
        "skipped" left the reader unable to tell a deliberate bypass from a
        broken setup.
        """
        if self.status != "skip":
            return ""
        return next((c["reason"] for c in self.checks if c.get("reason")), "")

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
        "PR_BODY_AI_ATTRIBUTION",
        "MESSAGE_AI_ATTRIBUTION",
        "AI_LABEL",
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
    except Exception as e:  # noqa: BLE001 - a bad payload warns, never fails the step
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
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _local_author(sha: str) -> tuple[str, str] | None:
    """Author (name, email) of a commit the clone holds, or ``None``."""
    try:
        result = subprocess.run(
            ["git", "show", "-s", "--format=%an%x1f%ae", sha],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return None
    name, sep, email = result.stdout.strip("\n").partition("\x1f")
    if result.returncode != 0 or not sep:
        return None
    return name, email


def _fetch_commit(sha: str) -> bool:
    """Fetch one commit, without its history, into a clone that lacks it."""
    try:
        result = subprocess.run(
            ["git", "fetch", "--no-tags", "--quiet", "--depth=1", "origin", sha],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0 and _rev_resolves(sha)


def pr_head_rev() -> str | None:
    """The commit whose recorded author the PR's author checks read.

    First choice is ``pull_request.head.sha`` from the event payload: it
    names the branch tip for ``pull_request`` and ``pull_request_target``
    alike, whatever was checked out, as long as the clone has it. Failing
    that, ``HEAD^2`` on a ``pull_request`` checkout, where HEAD is the
    merge ref and its second parent is that same tip. Never ``HEAD^2`` on
    ``pull_request_target``: there HEAD is the base branch, so ``HEAD^2``
    is nothing, or the parent of some unrelated merge on it. A shallow clone
    that lacks the tip gets it fetched, without its history.

    ``None`` when neither can be had.
    """
    sha = get_pr_head_sha()
    if sha and (_rev_resolves(sha) or _fetch_commit(sha)):
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
                "reason": "the pull request's head commit could not be resolved",
            }
        ],
    )


#: Why an AI-only scope ran no rule: under the default policy there is none to run.
AI_POLICY_IGNORED = 'ai_attribution is "ignore": set it to "forbid" or "disclose"'


def skip_when_no_ai_rule_ran(scope: ScopeResult) -> ScopeResult:
    """Report an AI-only scope that ran no rule as skipped, not passed.

    Under the default ``ai_attribution = "ignore"`` the CLI builds none of
    the AI rules, so it returns an empty list of checks. Shown as a pass, that
    read as "checked, nothing found" to someone who turned the input on and
    forgot the policy.
    """
    if scope.checks or scope.raw_text:
        return scope
    scope.checks = [
        {
            "rule_id": "CC013",
            "check": "ai_attribution",
            "status": "skip",
            "value": "",
            "error": "",
            "suggest": "",
            "docs_url": "",
            "reason": AI_POLICY_IGNORED,
        }
    ]
    return scope


def _message_value(scope: ScopeResult) -> str:
    """The whole commit message, as the CLI's ``message`` rule reports it."""
    return next(
        (
            str(c.get("value") or "")
            for c in scope.checks
            if c.get("check") == "message"
        ),
        "",
    )


def keep_ai_rules(scope: ScopeResult) -> ScopeResult:
    """Narrow a commit message scope to the AI rules (CC013 to CC017).

    ``message-ai-attribution: true`` does this whatever ``message`` says:
    ``message`` defaults to true, so asking for the AI rules alone must not
    need it turned off as well. The CLI runs every message rule and the
    format rules' verdicts are dropped here, so the CLI needs no flag of
    its own for it. A scope left with no rule, under the default
    ``ai_attribution = "ignore"``, is reported as skipped.
    """
    if not MESSAGE_AI_ATTRIBUTION_ENABLED or scope.raw_text:
        return scope
    # The message rule's value is the whole message: keep it for the report
    # before the rule itself is dropped (outside a PR nothing was piped in).
    scope.text = scope.text or _message_value(scope)
    scope.checks = [c for c in scope.checks if c.get("rule_id") in AI_RULE_IDS]
    return skip_when_no_ai_rule_ran(scope)


#: Rules about a commit rather than about a line of text: it has a body
#: (CC011), a sign-off (CC012), an author that is no AI tool (CC017). A pull
#: request title has neither body nor trailers nor an author of its own, so
#: judged as a commit message it could never pass the first two, and the
#: third would judge whatever git identity the runner has.
COMMIT_ONLY_RULE_IDS = frozenset({"CC011", "CC012", "CC017"})


def drop_commit_only_rules(scope: ScopeResult) -> ScopeResult:
    """Narrow the PR title scope to the rules a one-line title can meet."""
    scope.checks = [
        c for c in scope.checks if c.get("rule_id") not in COMMIT_ONLY_RULE_IDS
    ]
    return scope


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
    except Exception as e:  # noqa: BLE001 - a bad payload warns, never fails the step
        print(f"::warning::Failed to read PR title from event: {e}", file=sys.stderr)
        return None


def get_pr_body() -> str | None:
    """Read the pull request description from the event payload.

    GitHub sends ``null`` for a pull request opened with no description.
    """
    body = get_pr_event().get("body")
    return body if isinstance(body, str) and body.strip() else None


def get_pr_opener_tool() -> str | None:
    """The AI tool that opened the pull request from its own app, if any."""
    login = (get_pr_event().get("user") or {}).get("login") or ""
    return AI_AGENT_BOTS.get(str(login).lower())


def _rule_key(check: dict[str, str]) -> str:
    """What names a rule within one scope: its ID, or its name without one."""
    return check.get("rule_id") or check.get("check", "")


def lines_removed(text: str, fixed: str) -> list[str]:
    """The non-blank lines of ``text`` that ``fixed`` no longer has, in order.

    A line repeated in ``text`` counts once per copy ``fixed`` dropped.
    """
    kept: dict[str, int] = {}
    for line in fixed.splitlines():
        kept[line] = kept.get(line, 0) + 1
    removed = []
    for line in text.splitlines():
        if kept.get(line, 0):
            kept[line] -= 1
        elif line.strip():
            removed.append(line.strip())
    return removed


def name_the_line_changes(scope: ScopeResult, body: str) -> None:
    """Name the lines to change instead of repeating the description.

    For a message the CLI's ``fix`` is the corrected text, short enough to
    paste. A description can run to a page, and the summary, the comment and
    the annotation would each repeat it in full; the lines to delete or add
    are what its author needs.
    """
    for check in scope.checks:
        fixed = check.get("fix") or ""
        if not fixed:
            continue
        removed = lines_removed(body, fixed)
        added = lines_removed(fixed, body)
        scope.edits[_rule_key(check)] = (removed, added)
        parts = []
        if removed:
            parts.append("remove " + "; ".join(f'"{line}"' for line in removed))
        if added:
            parts.append("add " + "; ".join(f'"{line}"' for line in added))
        if parts:
            check["suggest"] = "In the description, " + ", and ".join(parts)
            check["fix"] = ""


#: The AI tools found in the pull request description when it was read for
#: the label only (``ai-label`` without ``pr-body-ai-attribution``), so it is no scope.
PR_BODY_AI_TOOLS: list[str] = []


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
    stripped along with the message's trailing newline. A commit with an
    empty message is kept, as ``(sha, "")``: it is a commit of the pull
    request like any other, and ``allow_empty_commits`` is the rule that
    judges it.
    """
    fields = [f.strip("\n") for f in output.split(COMMIT_MESSAGE_DELIMITER)]
    return [(sha, message) for sha, message in zip(fields[0::2], fields[1::2]) if sha]


def _messages_in_range(revision_range: str) -> list[Commit]:
    """Commits in ``revision_range`` as ``(sha, message)``, oldest first, or ``[]``."""
    result = subprocess.run(
        ["git", "log", COMMIT_LOG_FORMAT, "--reverse", revision_range],
        capture_output=True,
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
            capture_output=True,
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


#: The most commits GitHub's pull request commits endpoint returns.
API_COMMIT_LIMIT = 250

#: Author (name, email) of each commit listed by the API, by hash. Those
#: commits are not in the clone, so this is the only record of who wrote them.
API_AUTHORS: dict[str, tuple[str, str]] = {}


def get_messages_from_api() -> list[Commit]:
    """The pull request's commits from the REST API, for a clone without them.

    The default ``actions/checkout`` clone holds only GitHub's merge commit.
    Deepening it cannot list a pull request that merged its base branch in;
    the API lists any pull request exactly. It stops at 250 commits, so a
    longer one is left unlisted rather than checked in part.
    """
    pr = get_pr_event()
    number, total = pr.get("number"), pr.get("commits", 0)
    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not (number and token and repo) or total > API_COMMIT_LIMIT:
        return []
    from github import Auth, Github  # type: ignore

    api_url = os.getenv("GITHUB_API_URL", "https://api.github.com")
    pull = (
        Github(auth=Auth.Token(token), base_url=api_url).get_repo(repo).get_pull(number)
    )
    listed = list(pull.get_commits())
    commits = [(c.sha, c.commit.message) for c in listed]
    for c in listed:
        author = c.commit.author
        if author is not None:
            API_AUTHORS[c.sha] = (author.name or "", author.email or "")
    # A force push after the event can leave the same count but other commits.
    head = get_pr_head_sha()
    if len(commits) != total or (head and commits[-1][0] != head):
        return []
    return commits


def get_pr_commit_messages() -> list[Commit]:
    """Get all commits, as ``(sha, message)``, for the current PR workflow.

    The event payload's ``base.sha..head.sha`` is tried first: it names the
    pull request exactly, whatever was checked out. On a ``pull_request``
    checkout HEAD is the synthetic merge commit, so ``HEAD^1..HEAD^2`` is
    the same range. If the workflow checks out the PR head SHA instead,
    diff against ``origin/<base-ref>`` when that ref is available locally.
    A clone that holds none of these, or only part of the pull request (a
    shallow boundary ends a range early), asks the API.
    """
    if not is_pr_event():
        return []

    try:
        base_ref = os.getenv("GITHUB_BASE_REF", "")
        messages = (
            get_messages_from_event_range()
            or get_messages_from_merge_ref()
            or (get_messages_from_head_ref(base_ref) if base_ref else [])
        )
        total = get_pr_event().get("commits")
        if messages and (not total or len(messages) == total):
            return messages
        from_api = get_messages_from_api()
        if from_api:
            return from_api
        if messages:
            warn_shallow_checkout(
                f"Only {len(messages)} of the pull request's {total} commits "
                "could be listed",
                "the others were not checked",
            )
        return messages
    except Exception as e:  # noqa: BLE001 - warn, then check what we have
        print(
            f"::warning::Failed to retrieve PR commit messages: {e}",
            file=sys.stderr,
        )
    return []


#: Notices already relayed, so one repeated across every scope is shown once.
_RELAYED_NOTICES: set[str] = set()


def _relay_cli_notices(text: str) -> None:
    """Put the CLI's stderr in the job log, without repeating it.

    The Action runs commit-check once per scope and once per commit in the
    pull request, and a notice about the configuration is the same every
    time; printed on each run it would bury the findings.
    """
    for line in text.splitlines():
        line = line.strip()
        if line and line not in _RELAYED_NOTICES:
            _RELAYED_NOTICES.add(line)
            print(f"commit-check: {_log_safe(line)}", file=sys.stderr)


def run_check_json(
    args: list[str],
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | None, str]:
    """Run ``commit-check --format json`` and return (exit code, parsed JSON, raw output).

    The CLI's contract is that stdout holds the JSON and nothing else, while
    stderr carries what it has to say to a person: a parent config it could
    not fetch, a flag whose every rule the config switched off, a dry run
    that softened its own verdict. Those lines are not JSON, so they are
    read separately and relayed to the job log rather than handed to the
    parser -- merged into stdout they turned a passing run into an
    unparsable one, which ScopeResult reports as a failure.

    The parsed JSON is ``None`` when the CLI did not produce valid JSON; the
    raw output is kept so callers can fall back to showing it as text, and
    in that case it carries both streams so nothing the CLI said is lost.
    """
    command = ["commit-check", "--format", "json"] + args
    result = subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, **env} if env else None,
    )
    out = result.stdout or ""
    err = result.stderr or ""
    _relay_cli_notices(err)
    try:
        return result.returncode, json.loads(out), out
    except json.JSONDecodeError:
        # Whatever went wrong, the operator needs everything the CLI said,
        # so stderr joins stdout here -- and only here, where there is no
        # JSON left to protect. It is often the whole story: a config the
        # CLI refused leaves stdout empty and prints "Error: ..." on stderr,
        # and dropping it would leave an empty raw_text that reads as a pass.
        if not err.strip():
            return result.returncode, None, out
        parts = [part for part in (out.strip(), err.strip()) if part]
        return result.returncode, None, "\n".join(parts)


def check_scope(
    label: str,
    args: list[str],
    input_text: str | None = None,
    sha: str = "",
    env: dict[str, str] | None = None,
) -> ScopeResult:
    """Run commit-check for one scope and wrap the outcome in a ScopeResult.

    ``sha`` names the commit a message scope checked; it rides along on both
    outcomes so an unparsable CLI response still says which commit it was.
    """
    _rc, data, raw = run_check_json(args, input_text=input_text, env=env)
    if isinstance(data, dict):
        tools = data.get("ai_tools")
        return ScopeResult(
            label=label,
            checks=data.get("checks", []),
            sha=sha,
            ai_tools=[str(t) for t in tools] if isinstance(tools, list) else [],
            text=input_text or "",
        )
    return ScopeResult(label=label, raw_text=raw, sha=sha)


def run_pr_message_checks(pr_commits: list[Commit]) -> list[ScopeResult]:
    """Check each PR commit individually via commit-check --message.

    The raw message is piped in, so every commit is read the same way
    whether the clone holds it or only the API listed it. Its own author
    goes with it as git's author variables, which is where the CLI reads the
    author of a piped message: the AI author rule (CC017) and
    ``ignore_authors`` then judge that commit's author, not whoever HEAD or
    the runner's git config names.
    """
    results: list[ScopeResult] = []
    total = len(pr_commits)
    for index, (sha, msg) in enumerate(pr_commits, start=1):
        label = f"Commit {index}/{total}"
        author = API_AUTHORS.get(sha) or (_local_author(sha) if sha else None)
        env = None
        if author:
            names = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL")
            env = {key: value for key, value in zip(names, author) if value} or None
        if msg.strip():
            scope = check_scope(label, ["--message"], input_text=msg, sha=sha, env=env)
        else:
            scope = check_empty_message(label, sha, env)
        results.append(keep_ai_rules(scope))
    return results


def run_pr_author_checks(
    flags: list[str], pr_commits: list[Commit]
) -> list[ScopeResult]:
    """Run the author checks on every author of the pull request's commits.

    Checking only the branch tip let an earlier commit by another author,
    say a GitHub noreply address that ``author_email_pattern`` forbids, go
    through. Each distinct author is checked once, on the newest commit
    that carries it, so a one-person pull request still reports one scope
    per check. A commit the clone lacks is fetched without its history.

    Returns ``[]`` when no author can be read, or a commit cannot be had
    even by fetching it, so the caller falls back to the branch tip as before.
    """
    newest: dict[tuple[str, str], str] = {}
    for sha, _msg in pr_commits:
        author = API_AUTHORS.get(sha) or (_local_author(sha) if sha else None)
        if author:
            newest.pop(author, None)
            newest[author] = sha
    revs = [sha for sha in newest.values() if _rev_resolves(sha) or _fetch_commit(sha)]
    if len(revs) != len(newest):
        return []
    return [
        check_scope(CHECK_LABELS[flag], [flag, "--rev", sha], sha=sha)
        for flag in flags
        for sha in revs
    ]


def check_empty_message(
    label: str, sha: str, env: dict[str, str] | None
) -> ScopeResult:
    """Check a commit whose message is empty, from an empty file.

    Piped, an empty message reads to the CLI as no input at all, and it
    checks HEAD in its place: the merge commit, which passes. A file names
    the message, so the empty one is judged (``allow_empty_commits``).
    """
    fd, path = tempfile.mkstemp(prefix="commit-msg-", dir=os.getenv("RUNNER_TEMP"))
    os.close(fd)
    try:
        return check_scope(label, ["--message", path], sha=sha, env=env)
    finally:
        os.unlink(path)


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
        ("--message", MESSAGE_ENABLED or MESSAGE_AI_ATTRIBUTION_ENABLED),
        ("--branch", BRANCH_ENABLED),
        ("--author-name", AUTHOR_NAME_ENABLED),
        ("--author-email", AUTHOR_EMAIL_ENABLED),
    ]
    return [flag for flag, enabled in flags if enabled]


def run_commit_check() -> tuple[int, list[ScopeResult]]:
    """Runs all enabled checks and returns the overall exit code and results.

    Checks are evaluated in order:
      1. PR title (when ``pr-title: true`` and in a PR event)
      2. PR description, AI rules only (when ``pr-body-ai-attribution: true`` and in a PR
         event; with only ``ai-label`` set it is read for the label, and no
         scope is reported)
      3. Individual PR commit messages (when ``message: true`` and in a PR event;
         AI rules only with ``message-ai-attribution: true``)
      4. Author checks on every PR commit's author, then the remaining checks
         (branch; outside a PR, the HEAD commit and its author)

    Outside of a PR event all enabled checks are handed to the CLI at once.
    """
    args = build_check_args()
    results: list[ScopeResult] = []

    # ---- 1. PR title check ------------------------------------------------
    if PR_TITLE_ENABLED and is_pr_event():
        pr_title = get_pr_title()
        if pr_title:
            scope = check_scope("PR title", ["--message"], input_text=pr_title)
            results.append(drop_commit_only_rules(scope))

    # ---- 2. PR description ------------------------------------------------
    if (PR_BODY_AI_ATTRIBUTION_ENABLED or ai_label_name()) and is_pr_event():
        pr_body = get_pr_body()
        if pr_body:
            scope = check_scope(
                "PR description", ["--ai-attribution-only"], input_text=pr_body
            )
            if PR_BODY_AI_ATTRIBUTION_ENABLED:
                name_the_line_changes(scope, pr_body)
                results.append(skip_when_no_ai_rule_ran(scope))
            else:
                PR_BODY_AI_TOOLS[:] = scope.ai_tools

    # ---- 3. Commit message checks -----------------------------------------
    pr_commits: list[Commit] | None = None
    if MESSAGE_ENABLED or MESSAGE_AI_ATTRIBUTION_ENABLED:
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

    # ---- 4. Remaining checks (branch, author, etc.) -----------------------
    # Outside a PR, check the HEAD commit message directly.
    if "--message" in args:
        results.append(
            keep_ai_rules(check_scope("Commit message", ["--message"], sha=head_sha()))
        )
        args = [a for a in args if a != "--message"]
    rev = None
    author_flags = [flag for flag in args if flag in AUTHOR_FLAGS]
    if is_pr_event() and author_flags:
        if pr_commits is None:
            pr_commits = get_pr_commit_messages()
        author_scopes = run_pr_author_checks(author_flags, pr_commits)
        if author_scopes:
            results.extend(author_scopes)
            args = [a for a in args if a not in AUTHOR_FLAGS]
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

#: Home of the status marks the report shows: static PNGs in the organization's
#: ``.github`` repository, so nothing has to be running for them to load.
STATUS_MARK_URL = (
    "https://raw.githubusercontent.com/commit-check/.github/main/branding/status"
)
SITE_URL = "https://commit-check.com"
#: Where a reader sets the same rules up locally, so the next push passes.
LOCAL_HOOK_URL = "https://commit-check.com/guides/integrations/#as-a-pre-commit-hook"

#: One glyph per status. The step log prints it, and the report's images carry
#: it as their alt text, so a report whose images cannot load still reads.
STATUS_GLYPHS = {"pass": "✔", "fail": "✖", "warn": "⚠", "skip": "⊘"}
#: File name of each status mark, and the word the heading uses for it.
STATUS_WORDS = {
    "pass": "passed",
    "fail": "failed",
    "warn": "warned",
    "skip": "skipped",
}
#: Step log colours. GitHub renders these; bright black is its grey.
ANSI = {"pass": "\033[32m", "fail": "\033[31m", "warn": "\033[33m", "skip": "\033[90m"}
ANSI_BOLD, ANSI_GREY, ANSI_RESET = "\033[1m", "\033[90m", "\033[0m"

#: Failing items the report shows before folding the rest away. A hunk is two
#: or three lines, and folding one or two leftovers would cost a click and
#: save nothing, so up to ``MAX_HUNKS + HUNK_SLACK`` are shown unfolded.
MAX_HUNKS = 5
HUNK_SLACK = 2


def _rule_label(check: dict[str, str]) -> str:
    """Human-readable label for a check: ``CC001 message`` (kebab-case)."""
    rule_id = check.get("rule_id", "")
    name = check.get("check", "").replace("_", "-")
    return f"{rule_id} {name}" if rule_id else name


def _rule_ref(check: dict[str, str]) -> str:
    """Short Markdown link for a rule: ``[CC001](docs_url)``."""
    label = check.get("rule_id") or check.get("check", "").replace("_", "-")
    docs_url = check.get("docs_url", "")
    return f"[{label}]({docs_url})" if docs_url else label


def _scope_group(label: str) -> str:
    """Group name for a scope label, used to fold the step log output."""
    if label in ("PR title", "PR description"):
        return "Pull request"
    if label.startswith("Commit"):
        return "Commits"
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


def _plural(count: int, word: str) -> str:
    """``1 warning``, ``2 warnings``."""
    return f"{count} {word}{'' if count == 1 else 's'}"


def _group_status(scopes: list[ScopeResult]) -> tuple[str, str]:
    """A group's worst outcome and the few words that say it: ``3 of 4 failed``.

    The same words head the group in the step log and its line in the
    report, so a folded log and a skimmed comment both say where it went
    wrong. A skipped scope never counts as passed, and a group is ``skip``
    only when nothing in it ran.
    """
    total = len(scopes)
    failed = sum(1 for scope in scopes if scope.status == "fail")
    skipped = sum(1 for scope in scopes if scope.status == "skip")
    warned = sum(1 for scope in scopes if scope.warnings)
    if failed:
        return "fail", f"{failed} of {total} failed" if total > 1 else "failed"
    if skipped == total:
        return "skip", "skipped"
    if warned:
        return "warn", _plural(warned, "warning")
    text = f"{total - skipped} passed" if total > 1 else "passed"
    return "pass", f"{text}, {skipped} skipped" if skipped else text


def _first_value(scope: ScopeResult) -> str:
    """First line of what the scope checked: a subject, a title, a branch.

    The text the action piped in when it has it, then the whole message the
    ``message`` rule reports, then whichever rule names a value first. A PR
    description has no such line: its first line does not identify it.
    """
    if scope.label == "PR description":
        return ""
    values = [scope.text, _message_value(scope)]
    values.extend(str(check.get("value") or "") for check in scope.checks)
    for value in values:
        if value.strip():
            return value.strip().splitlines()[0].strip()
    return ""


def _scope_value(scope: ScopeResult, max_len: int = 60) -> str:
    """``_first_value`` capped for a row of the listing.

    The value reads naturally next to the scope's label, and the cap keeps a
    passing row on one line of the fenced block.
    """
    value = _first_value(scope)
    return value[: max_len - 3] + "..." if len(value) > max_len else value


def _line_edits(
    scope: ScopeResult, check: dict[str, str]
) -> tuple[list[str], list[str]]:
    """The lines a rule's fix removes and adds, as ``(removed, added)``.

    The CLI's ``fix`` is corrected text, but of what differs by rule: the
    whole message for most, the subject alone for the subject rules. So the
    fix is compared with the rule's value when that is one line of the text
    and the fix is one line too, and with the whole text otherwise. An AI
    rule's value is the tool or the line it found, never the text it fixes.

    Both lists are empty when the rule offers no fix, or nothing is known to
    compare it with; the caller then falls back to the rule's advice.
    """
    recorded = scope.edits.get(_rule_key(check))
    if recorded:
        return recorded
    fix = (check.get("fix") or "").strip()
    if not fix:
        return [], []
    value = str(check.get("value") or "").strip()
    text = (scope.text or _message_value(scope)).strip()
    one_line = "\n" not in value and "\n" not in fix
    if check.get("rule_id") in AI_RULE_IDS:
        original = text
    elif one_line and value in text.splitlines():
        original = value
    else:
        original = text or value
    if not original:
        return [], []
    return lines_removed(original, fix), lines_removed(fix, original)


def _plain_error(check: dict[str, str]) -> str:
    """First line of the rule's error, without a trailing ``See <url>``."""
    error = (check.get("error") or "").splitlines()
    fallback = "check warning" if check.get("status") == "warn" else "check failed"
    first = error[0] if error else fallback
    return re.sub(r"\s*See https?://\S+\s*$", "", first)


def _advice(check: dict[str, str]) -> str:
    """What to do about a finding that has no ready-made fix, in one line.

    The CLI's suggestion, without the tails that only matter once the reader
    opens the rule (the list of allowed types, the config key that would
    exempt the branch): those are in the listing and behind the rule link.
    """
    suggest = (check.get("suggest") or "").splitlines()
    if not suggest:
        return _plain_error(check)
    advice = re.sub(r"^In the description, ", "", suggest[0])
    return re.sub(
        r"(, where <type> is one of: .*|, or add the branch to .*)$", "", advice
    )


def _quoted(lines: list[str]) -> str:
    """``"first"; "second"``."""
    return "; ".join(f'"{line}"' for line in lines)


def _instruction(scope: ScopeResult, check: dict[str, str]) -> str:
    """One finding as an instruction: ``change "a" to "b"``, ``remove "x"``."""
    removed, added = _line_edits(scope, check)
    if len(removed) == 1 and len(added) == 1:
        return f'change "{removed[0]}" to "{added[0]}"'
    parts = []
    if removed:
        parts.append(f"remove {_quoted(removed)}")
    if added:
        parts.append(f"add {_quoted(added)}")
    return ", and ".join(parts) or _advice(check)


def _target(scope: ScopeResult, instructions: list[str], label: str) -> str:
    """Name what an instruction is about: ``Commit 2/3 (5584f46) "bad msg"``.

    The quoted value is left out when an instruction already quotes it.
    """
    value = _first_value(scope)
    if value and not any(f'"{value}"' in text for text in instructions):
        return f'{label} "{value}"'
    return label


def _findings(scope: ScopeResult, status: str) -> list[dict[str, str]]:
    """A scope's failures or warnings."""
    return scope.failures if status == "fail" else scope.warnings


def _finding_lines(check: dict[str, str]) -> list[str]:
    """The detail a reader needs to act on one finding, one item per line.

    ``value:`` (what was checked), the error, ``Suggest:`` and ``Fix:`` —
    each only when the CLI filled it in.

    ``Fix:`` is the corrected text itself, ready to paste. When a rule has a
    fix but no bespoke advice the CLI sets ``suggest`` to ``Use "<fix>"``,
    so printing both would say the same thing twice in a row; in exactly
    that case only ``Fix:`` is shown. A multi-line fix (a signed-off body)
    takes one row per line so the trailer lands where it would in the
    message; a multi-line value (a whole commit message on a failing rule)
    or suggestion is split the same way, so every line of user text sits
    inside the tree rather than at column 0.
    """
    lines: list[str] = []
    if check.get("value"):
        first, *rest = str(check["value"]).splitlines()
        lines.append(f"value: {first}")
        lines.extend(rest)
    lines.extend(check.get("error", "").splitlines())
    fix = check.get("fix", "")
    suggest = check.get("suggest", "")
    if suggest and suggest != f'Use "{fix}"':
        first, *rest = suggest.splitlines()
        lines.append(f"Suggest: {first}")
        lines.extend(rest)
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
        # A scope can hold a failure and a warning at once (see below), so a
        # warning says so on its own line rather than borrow the row's mark.
        kind = " (warning)" if check.get("status") == "warn" else ""
        lines.append(f"      {_rule_label(check)}{kind}")
        lines.extend(f"        {line}" for line in _finding_lines(check))
        if include_docs and check.get("docs_url"):
            lines.append(f"        Docs: {check['docs_url']}")
    return lines


def _row_label(scope: ScopeResult) -> str:
    """A scope's name within its group: the short hash, ``title``, ``email``.

    The group header already says "Commits" or "Author", so the row only
    needs what tells it apart from its neighbours.
    """
    if scope.sha and scope.label.startswith("Commit"):
        return scope.sha[:7]
    _group, _, name = scope.label.partition(" ")
    return name or scope.label.lower()


def _row(mark: str, scope: ScopeResult, value: str) -> str:
    """One aligned row of the listing: mark, label, value."""
    return f"  {mark} {_row_label(scope):<12} {value}".rstrip()


def _render_scopes(scopes: list[ScopeResult], include_docs: bool) -> list[str]:
    """Render the indented listing for one group of scopes, without its header.

    Shared by both output surfaces so they cannot drift: the step log and the
    Markdown listing are the same rows, and the only difference is the docs
    link, which the Markdown report already carries under the diff.

    A failing or warned scope shows its value in full rather than capped. It
    is the one value the reader has to act on, and the 60-character cap can
    cut off the part that explains it.
    """
    lines: list[str] = []
    for scope in scopes:
        if scope.status == "skip":
            # Deliberately not a ✔. Nothing was validated here, and a tick
            # claiming otherwise is what made a bypassed policy look enforced.
            reason = scope.skip_reason
            lines.append(_row("⊘", scope, f"skipped{f': {reason}' if reason else ''}"))
            continue
        if scope.status == "pass":
            lines.append(_row("✔", scope, _scope_value(scope)))
            continue
        if scope.raw_text and not scope.checks:
            # Defensive fallback: commit-check produced unexpected output.
            lines.append(_row("✖", scope, ""))
            lines.extend(f"      {ln}" for ln in scope.raw_text.strip().splitlines())
            continue
        # A scope's status names its worst outcome (fail beats warn), but the
        # two are not exclusive: CC2xx covers both branch and merge_base, so
        # one can fail while the other only warns. The row carries the worst
        # mark, and both lists are rendered under it — a warning on an
        # otherwise-failing scope is still a finding to fix.
        lines.append(_row(STATUS_GLYPHS[scope.status], scope, _first_value(scope)))
        lines.extend(_render_findings(scope.failures, include_docs))
        lines.extend(_render_findings(scope.warnings, include_docs))
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


#: C0 and C1 control characters, and DEL, except the newline and tab that a
#: multi-line commit message legitimately carries.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _log_safe(text: str) -> str:
    """Make commit-controlled text inert before it is printed to the step log.

    The tree prints values from the CLI's JSON, and those are whoever wrote
    the commit, branch or pull request title: a multi-line value (a whole
    message on a failing rule, a sign-off fix) prints each further line on a
    line of its own. The runner reads any log line beginning with ``::`` as a
    workflow command, after skipping leading whitespace, so indenting it is
    not enough; and it reads the older ``##[command]`` form anywhere in a
    line. Left as they are, such lines can forge annotations or mask log
    text. So such a line gets a backslash before its ``::``, ``##[`` is broken
    up, and control characters are shown as ``\\xNN`` escapes. This is the
    CLI's own ``printable()`` (commit-check#620), which covers its text
    output but not the JSON this action renders itself.

    Only the step log needs it: the Markdown report and the annotation
    payloads are not read as commands. It runs before the log's own colours
    are added, so an escape sequence in the log is always the action's.
    """
    if text.isprintable() and "::" not in text and "##[" not in text:
        return text  # nothing to escape: the common case
    text = _CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group()):02x}", text)
    text = text.replace("##[", "#\\#[")
    return "\n".join(
        line.replace("::", "\\::", 1) if line.lstrip().startswith("::") else line
        for line in text.split("\n")
    )


#: A row of the listing: two spaces, the status glyph, a space.
_ROW_MARK = re.compile(r"^  ([✔✖⚠⊘]) ")
_GLYPH_STATUS = {glyph: status for status, glyph in STATUS_GLYPHS.items()}


def _coloured(line: str) -> str:
    """Colour a row's status glyph for the step log.

    Only rows start with a glyph at column two; the detail under a finding
    sits deeper, so user text can never be taken for a row.
    """
    match = _ROW_MARK.match(line)
    if not match:
        return line
    glyph = match.group(1)
    return f"  {ANSI[_GLYPH_STATUS[glyph]]}{glyph}{ANSI_RESET} {line[4:]}"


def _annotation_body(scope: ScopeResult, check: dict[str, str]) -> str:
    """The message of one finding's annotation, before escaping.

    The run page shows only the first line of an annotation, so that line
    says where and what to change: ``Commit 2/3 (5584f46): change "bad msg"
    to "fix: bad msg"``. The rule's own sentence follows on the second line
    for whoever expands it.
    """
    instruction = _instruction(scope, check)
    headline = f"{_target(scope, [instruction], scope.display_label)}: {instruction}"
    error = _plain_error(check)
    return headline if error == instruction else f"{headline}\n{error}"


def render_step_log(results: list[ScopeResult]) -> None:
    """Print results to the step log, then emit one annotation per finding.

    Each group is folded under a title that carries its own result
    (``✖ Commits: 3 of 4 failed``): GitHub shows a group collapsed, and a
    bare "Commits" said nothing about where to look.

    The tree and the annotations are separated deliberately. An ``::error`` or
    ``::warning`` command renders as a line of its own wherever it is printed,
    so emitting one inside the indented listing broke the tree apart, and its
    ``title=`` — which is what carries the rule ID — is only shown in the
    annotations UI, never inline. Printing the listing first and the
    annotations after all the groups keeps the log readable and still
    surfaces findings on the run page, where the annotation's first line is
    all a reader sees (``_annotation_body``).

    A failure becomes an ``::error``, a warning a ``::warning`` — GitHub
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
        status, text = _group_status(scopes)
        print(f"::group::{STATUS_GLYPHS[status]} {group_name}: {text}")
        for line in _render_scopes(scopes, include_docs=True):
            print(_coloured(_log_safe(line)))
        print("::endgroup::")

    errors: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []
    for scope in results:
        if scope.status in ("pass", "skip"):
            continue
        if scope.raw_text and not scope.checks:
            errors.append(
                (f"Commit Check: {scope.display_label}", "output could not be parsed")
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
                f"Commit Check (dry-run): {failed} of {total} checks failed; "
                "not failing the job"
            )
        else:
            verdict = f"✖ Commit Check: {failed} of {total} checks failed"
            warned = _warn_count(results)
            if warned:
                verdict += f", {_plural(warned, 'warning')}"
            print(f"{ANSI['fail']}{ANSI_BOLD}{verdict}{ANSI_RESET}")
        where = "the job summary" if _summary_written() else "the findings above"
        print(
            f"{ANSI_GREY}How to fix: see {where}. "
            f"Check before you push: {LOCAL_HOOK_URL}{ANSI_RESET}"
        )
    if not errors:
        skipped, warned, total = (
            _skip_count(results),
            _warn_count(results),
            len(results),
        )
        if total and skipped == total:
            print("⊘ Commit Check: all checks skipped, nothing was validated")
            if _skip_reasons(results):
                print(_log_safe(_skip_reasons(results)))
        elif warned or skipped:
            passed = total - skipped - warned
            tail = []
            if warned:
                tail.append(_plural(warned, "warning"))
            if skipped:
                tail.append(f"{skipped} skipped")
            print(
                f"{ANSI['pass']}✔ Commit Check: {passed} of {total} checks "
                f"passed, {', '.join(tail)}{ANSI_RESET}"
            )
            if _skip_reasons(results):
                print(_log_safe(_skip_reasons(results)))
        else:
            print(f"{ANSI['pass']}✔ Commit Check: all checks passed{ANSI_RESET}")


def _summary_written() -> bool:
    """Whether this run writes a job summary a log line can point to."""
    return bool(JOB_SUMMARY_ENABLED and GITHUB_STEP_SUMMARY)


def _check_counts(results: list[ScopeResult]) -> tuple[int, int]:
    """Return ``(failed, total)`` where one check is one thing that was checked.

    A "check" here is a scope — one commit message, the branch, the author name
    — not one rule evaluation. Counting rule evaluations produced a number that
    grew with the size of the pull request rather than with the strictness of
    the policy: sixteen commit messages against six enabled rules reported
    "1 of 100 checks failed", where 96 of the 100 were the same six rules run
    again per commit. The large denominator also made a real failure look
    negligible — one bad commit out of fifteen reads very differently from
    1 of 100.

    This number matches what the reader can count: the ✔/✖ rows of the
    listing. Which rules failed is not lost, it is just reported where it
    belongs — under the diff and in the listing.
    """
    failed = sum(1 for scope in results if scope.status == "fail")
    return failed, len(results)


def _skip_count(results: list[ScopeResult]) -> int:
    """Number of scopes that never ran.

    Reported separately from the pass count so the headline cannot claim
    that checks passed when they were skipped.
    """
    return sum(1 for scope in results if scope.status == "skip")


def _skip_reasons(results: list[ScopeResult]) -> str:
    """One sentence naming why checks were skipped, or ``""`` if unknown.

    The verdict is all most readers see, so it stays short: the config
    section is dropped ("[commit].ignore_authors" and "[branch].ignore_authors"
    both read "ignore_authors"), which folds the usual bot case into one
    reason. The per-scope lines in the collapsed block keep the section.
    """
    reasons = list(
        dict.fromkeys(
            re.sub(r"\[\w+\]\.", "", s.skip_reason) for s in results if s.skip_reason
        )
    )
    return f"Skipped because {'; '.join(reasons)}." if reasons else ""


def _warn_count(results: list[ScopeResult]) -> int:
    """Number of scopes that carry at least one warning.

    Counts by ``scope.warnings``, not ``scope.status == "warn"``: a scope
    whose overall status is "fail" (CC2xx covers both ``branch`` and
    ``merge_base``, so one can fail while the other only warns) still has
    warnings to report, and this is the count the verdict uses to decide
    whether to show them.
    """
    return sum(1 for scope in results if scope.warnings)


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


def _longest_backtick_run(lines: list[str]) -> int:
    """Length of the longest run of consecutive backticks across ``lines``."""
    return max((len(m) for line in lines for m in re.findall(r"`+", line)), default=0)


def _fenced(language: str, body: list[str]) -> list[str]:
    """A fenced code block that user text cannot close.

    The body quotes commit subjects, errors and suggestions as they are. A
    value with three backticks in it (a `docs:` commit showing a fence)
    would close a fixed ``` fence and spill the rest of the report out as
    prose, so the fence is one longer than any backtick run inside.
    """
    fence = "`" * max(3, _longest_backtick_run(body) + 1)
    return [f"{fence}{language}", *body, fence]


def _status_mark(status: str, size: int, line: bool = False) -> str:
    """The ``<img>`` of a status mark: the tile at 24 px, the line mark at 16."""
    name = f"{'line-' if line else ''}{STATUS_WORDS[status]}"
    return (
        f'<img alt="{STATUS_GLYPHS[status]}" width="{size}" height="{size}" '
        f'align="top" src="{STATUS_MARK_URL}/{name}.png">'
    )


def _group_lines(results: list[ScopeResult]) -> str:
    """One line per group, its status mark in front: the overview."""
    return "<br>\n".join(
        f"{_status_mark(status, 16, line=True)} {group_name}: {text}"
        for group_name, scopes in _grouped(results)
        for status, text in [_group_status(scopes)]
    )


def _hunk(scope: ScopeResult, status: str) -> list[str]:
    """One scope's findings as a diff hunk.

    ``@@ where @@``, then red for what was found and has to go, green for
    what to write instead, and a ``#`` comment for advice that has no
    literal text to offer. GitHub colours all three in a ``diff`` block,
    which is the only colour a comment or a job summary can carry.
    """
    is_commit = bool(scope.sha) and scope.label.startswith("Commit")
    lines = [f"@@ {scope.sha[:7] if is_commit else scope.label} @@"]
    removed: list[str] = []
    added: list[str] = []
    advice: list[str] = []
    for check in _findings(scope, status):
        minus, plus = _line_edits(scope, check)
        removed.extend(minus)
        added.extend(plus)
        if not (minus or plus):
            advice.append(_advice(check))
    if scope.raw_text and not scope.checks:
        advice.append("the output could not be parsed, see the step log")
    removed, added, advice = (list(dict.fromkeys(x)) for x in (removed, added, advice))
    value = _first_value(scope)
    if value and value not in removed:
        # Advice is about the value itself, so it is what was found; a fix
        # that only touches other lines leaves it as context.
        lines.append(f"{'-' if advice else ' '} {value}")
    lines.extend(f"- {line}" for line in removed)
    lines.extend(f"+ {line}" for line in added)
    lines.extend(f"# {line}" for line in advice)
    return lines


def _diff_block(results: list[ScopeResult], status: str = "fail") -> list[str]:
    """The findings of one kind as a ``diff`` block, with their links under it.

    A scope appears when it has an entry of the requested kind — checked via
    ``scope.failures`` / ``scope.warnings``, not ``scope.status`` — so a scope
    that both failed and warned shows up in both blocks.

    A long list is cut at ``MAX_HUNKS`` and the rest folded away, so twenty
    bad commits do not push the rest of the pull request off the screen. A
    code block cannot hold links, so the commits shown and the rules that
    spoke get one small line of them underneath.
    """
    scopes = [
        scope
        for scope in results
        if _findings(scope, status)
        or (status == "fail" and scope.raw_text and not scope.checks)
    ]
    hunks = [_hunk(scope, status) for scope in scopes]
    shown = len(hunks) if len(hunks) <= MAX_HUNKS + HUNK_SLACK else MAX_HUNKS
    lines = _fenced("diff", [line for hunk in hunks[:shown] for line in hunk])
    if len(hunks) > shown:
        rest = [line for hunk in hunks[shown:] for line in hunk]
        lines += ["", "<details>", f"<summary>and {len(hunks) - shown} more</summary>"]
        lines += ["", *_fenced("diff", rest), "", "</details>"]
    commits = [
        f"[`{scope.sha[:7]}`]({_commit_url(scope.sha)})"
        for scope in scopes[:shown]
        if scope.label.startswith("Commit") and _commit_url(scope.sha)
    ]
    rules = list(
        dict.fromkeys(
            _rule_ref(check) for scope in scopes for check in _findings(scope, status)
        )
    )
    links = []
    if commits:
        links.append(f"Commits: {' '.join(commits)}")
    if rules:
        links.append(f"Rules: {' · '.join(rules)}")
    if links:
        lines += ["", f"<sub>{' &nbsp;|&nbsp; '.join(links)}</sub>"]
    return lines


#: First line of the prompt "How to fix" offers for an AI coding agent.
AGENT_PROMPT = "Fix these Commit Check findings without changing any code:"
BRANCH_HELP = (
    "**Branch name.** Rename the branch and push it. A pull request cannot "
    "change its branch, so open a new one from the renamed branch:"
)
AUTHOR_HELP = (
    "**Author.** Set your git identity (`git config user.name`, "
    "`git config user.email`), then `git commit --amend --reset-author` "
    "and force-push."
)
HOOK_HELP = (
    f"**Next time.** The [pre-commit hook]({LOCAL_HOOK_URL}) runs the same "
    "rules before you push."
)


def _agent_prompt(failing: list[ScopeResult]) -> list[str]:
    """A prompt to paste into an AI coding agent, one line per failing scope."""
    lines = [AGENT_PROMPT]
    for scope in failing:
        todo = list(dict.fromkeys(_instruction(scope, c) for c in scope.failures))
        is_commit = bool(scope.sha) and scope.label.startswith("Commit")
        label = f"Commit {scope.sha[:7]}" if is_commit else scope.label
        lines.append(f"- {_target(scope, todo, label)}: {'; '.join(todo)}")
    return lines


def _is_last_commit(scope: ScopeResult) -> bool:
    """Whether the scope is the newest commit: ``Commit 3/3`` or the HEAD one."""
    position = re.fullmatch(r"Commit (\d+)/(\d+)", scope.label)
    return not position or position.group(1) == position.group(2)


def _reword_commands(commits: list[ScopeResult]) -> list[str]:
    """The git commands that reword the failing commits and push the result."""
    if len(commits) == 1 and _is_last_commit(commits[0]):
        reword = "git commit --amend"
    else:
        first = commits[0].sha[:7] or "<first failing commit>"
        note = '# change "pick" to "reword" on the commits above'
        reword = f"git rebase -i {first}~1   {note}"
    return [reword, "git push --force-with-lease"]


def _how_to_fix(results: list[ScopeResult]) -> list[str]:
    """The folded "How to fix" section: only what this run's failures need.

    A prompt for whoever lets an AI agent do it, then the git commands for
    doing it by hand. Most people who fail a message check have never
    reworded a pushed commit, and the commands are the same every time.
    """
    failing = [scope for scope in results if scope.failures]
    if not failing:
        return []
    commits = [scope for scope in failing if scope.label.startswith("Commit")]
    labels = {scope.label for scope in failing}
    # One list of lines per paragraph; a blank line goes between them.
    parts = [
        ["**With an AI coding agent.** Paste this prompt:"],
        _fenced("text", _agent_prompt(failing)),
    ]
    if commits:
        many = "it" if len(commits) == 1 else "them"
        parts.append([f"**Commit messages.** Reword {many}, then force-push:"])
        parts.append(_fenced("bash", _reword_commands(commits)))
    if any(label.startswith("Author") for label in labels):
        parts.append([AUTHOR_HELP])
    if "Branch" in labels:
        # The CLI's own corrected name when it has one, a pattern otherwise.
        fixes = (
            c.get("fix") for s in failing if s.label == "Branch" for c in s.failures
        )
        name = next((fix for fix in fixes if fix), "<type>/<description>")
        rename = [f"git branch -m {name}", "git push -u origin HEAD"]
        parts.extend([[BRANCH_HELP], _fenced("bash", rename)])
    fields = [word for word in ("title", "description") if f"PR {word}" in labels]
    if fields:
        many = "it" if len(fields) == 1 else "them"
        what = " and ".join(fields)
        parts.append(
            [f"**Pull request {what}.** Edit {many} on the pull request page."]
        )
    parts.append([HOOK_HELP])
    lines = ["<details>", "<summary><b>How to fix</b></summary>"]
    for part in parts:
        lines.extend(["", *part])
    return [*lines, "", "</details>"]


def _all_checks(results: list[ScopeResult]) -> list[str]:
    """The folded listing of every scope, passed ones included.

    The same rows the step log prints (group name, one ✔/✖ row per scope with
    the checked value), with the rule's reason, suggestion and fix under each
    finding, so one expand answers both "what was checked" and "what failed
    and why". The same block is used whether or not anything failed.
    """
    _failed, total = _check_counts(results)
    unit = "check" if total == 1 else "checks"
    label = f"All {total} {unit}" if total else "Details"
    body = _fenced("text", _render_tree(results, include_docs=False))
    return ["<details>", f"<summary>{label}</summary>", "", *body, "", "</details>"]


# ---------------------------------------------------------------------------
# Output specification
#
# The Markdown report shared by the job summary and the PR comment renders
# as follows (values are filled from ScopeResult data; <mark:…> stands for an
# <img> of that status mark, see _status_mark).
#
# Every body opens with COMMENT_MARKER, which is invisible when rendered and is
# how the action recognises its own PR comment on the next run.
#
# Failure:
#
#   <!-- commit-check-action -->
#   ## <mark:failed> Commit Check failed
#
#   <mark:line-passed> Pull request: passed<br>
#   <mark:line-failed> Commits: 2 of 3 failed<br>
#   <mark:line-failed> Branch: failed
#
#   ```diff
#   @@ 5584f46 @@
#   - bad msg
#   + fix: bad msg
#   @@ 37d6def @@
#     feat: add OAuth providers
#   - Co-authored-by: Claude <noreply@anthropic.com>
#   @@ Branch @@
#   - login-page
#   # Use <type>/<description> with an allowed type
#   ```
#
#   <sub>Commits: [`5584f46`](…) [`37d6def`](…) &nbsp;|&nbsp; Rules: [CC001](…) · [CC013](…) · [CC201](…)</sub>
#
#   <details>
#   <summary><b>How to fix</b></summary>
#
#   **With an AI coding agent.** Paste this prompt:
#
#   ```text
#   Fix these Commit Check findings without changing any code:
#   - Commit 5584f46: change "bad msg" to "fix: bad msg"
#   - Commit 37d6def "feat: add OAuth providers": remove "Co-authored-by: Claude <noreply@anthropic.com>"
#   - Branch "login-page": Use <type>/<description> with an allowed type
#   ```
#
#   **Commit messages.** Reword them, then force-push:
#
#   ```bash
#   git rebase -i 5584f46~1   # change "pick" to "reword" on the commits above
#   git push --force-with-lease
#   ```
#
#   **Branch name.** Rename the branch and push it. …
#
#   **Next time.** The [pre-commit hook](…) runs the same rules before you push.
#
#   </details>
#
#   <details>
#   <summary>All 5 checks</summary>
#
#   ```text
#   Pull request
#     ✔ title        feat: add login page
#   Commits
#     ✔ d87faca      feat: add login page
#     ✖ 5584f46      bad msg
#         CC001 message
#           value: bad msg
#           The commit message should follow Conventional Commits.
#           Fix: fix: bad msg
#     ✖ 37d6def      feat: add OAuth providers
#         CC013 ai-attribution
#           value: Claude Code
#           AI attribution is forbidden in this project — detected: Claude Code
#           Suggest: …
#           Fix: feat: add OAuth providers
#   Branch
#     ✖ branch       login-page
#         CC201 branch
#           value: login-page
#           The branch should follow Conventional Branch.
#           Suggest: Use <type>/<description> with an allowed type, or …
#   ```
#
#   </details>
#
#   <sub>Checked by [Commit Check](https://commit-check.com) 2.20.5 · [Rules](…) · [Check before you push](…)</sub>
#
# Success: the heading reads "<mark:passed> Commit Check passed", then the
# group lines, the folded listing and the footer. No diff, no "How to fix".
#
# Warning (the repository's config lists a rule under the top-level `warn`):
# "<mark:warned> Commit Check passed with warnings", the group lines (a warned
# group reads "<mark:line-warned> Branch: 1 warning"), then **Warnings** and
# the same diff block built from the warnings. A warned scope is reported
# exactly like a failing one except the mark is ⚠ rather than ✖, it counts
# toward "passed" rather than "failed", and it never turns the workflow red.
# A real failure elsewhere still fails the run; both diff blocks then appear.
#
# Skipped (every rule declined to run — e.g. the author is in ignore_authors):
# "<mark:skipped> Commit Check skipped", then "Nothing was validated." and the
# reason ("Skipped because author dependabot[bot] is in ignore_authors."). The
# reason comes from commit-check; an older CLI sends none. A skipped scope
# deliberately carries no ✔ and no checked value: nothing was examined, so
# there is no value to report and no pass to claim. When only some scopes
# skip, their group line says so ("Author: 1 passed, 1 skipped").
#
# The step log prints the same listing, each group folded under a title that
# carries its result, plus a `Docs:` line per finding; then one annotation per
# finding, and the verdict:
#
#   ::group::✖ Commits: 2 of 3 failed
#     ✔ d87faca      feat: add login page
#     ✖ 5584f46      bad msg
#         CC001 message
#           …
#   ::endgroup::
#   ::error title=CC001 message::Commit 2/3 (5584f46): change "bad msg" to "fix: bad msg"%0AThe commit message should follow Conventional Commits.
#   ✖ Commit Check: 2 of 5 checks failed
#   How to fix: see the job summary. Check before you push: https://commit-check.com/…
#
# The verdict is a plain line, not an `::error`, so the run's error count is
# the number of findings.
#
# Notes:
# - The status marks are the Commit Check mark in a status colour, so the mark
#   itself is the status signal: there is never a tick next to a failure.
#   Their alt text is the glyph the step log uses, so the report still reads
#   where the images cannot load.
# - A red line is text that was found and has to change, a green line is text
#   to write, taken from the CLI's `fix` (see _line_edits). A rule with no
#   mechanical fix gets a `#` line with the CLI's suggestion instead.
# - The diff lists only what failed, capped at MAX_HUNKS items with the rest
#   folded; passing scopes live in the listing.
# - A commit is named by its short hash on every surface; the label
#   ("Commit 1/11") stays bare in the `result` output.
# - One check is one thing that was checked — a commit message, the branch, the
#   author — not one rule evaluation. The totals therefore match the number of
#   ✔/✖ rows the reader can count in the listing, and do not grow with the
#   number of rules in the config.
# - Passing values are capped at 60 characters with a literal "..." suffix; a
#   failing scope prints its value in full — it is the one value the reader
#   has to act on and the cap can hide the reason.
# - Every fenced block grows its fence past any backtick run inside it, so a
#   subject that quotes code cannot close the block.
# ---------------------------------------------------------------------------


def _commit_check_version() -> str:
    """Version of the commit-check CLI that produced these results."""
    try:
        from importlib.metadata import version

        return version("commit-check")
    except Exception:  # noqa: BLE001 - the version is decoration, never fail on it
        return ""


def _report_footer() -> str:
    """Attribution line: who checked, which version, and where to go next.

    The version is the first thing worth knowing when a result looks wrong,
    and it is otherwise buried in the step log. The last link is for the
    contributor who would rather hear this before pushing.
    """
    installed = _commit_check_version()
    version = f" {installed}" if installed else ""
    return (
        f"<sub>Checked by [Commit Check]({SITE_URL}){version} · "
        f"[Rules]({RULES_URL}) · [Check before you push]({LOCAL_HOOK_URL})</sub>"
    )


def render_report(results: list[ScopeResult]) -> str:
    """Render the Markdown report shared by the job summary and PR comment.

    Opens with the hidden marker and a heading that is the verdict — the
    status mark and ``Commit Check failed`` / ``passed`` — then one line per
    group, the diff of what failed, a diff of the warnings when the config
    listed any rule under ``warn``, the folded "How to fix", and the folded
    listing of every scope.

    A warned scope counts toward "passed", never toward "failed": it ran,
    found something, and is reported in full, but the config asked for it to
    be surfaced rather than enforced.
    """
    failed, total = _check_counts(results)
    warned = _warn_count(results)
    skipped = _skip_count(results)

    if failed:
        status, verdict = "fail", "failed"
    elif total and skipped == total:
        # Nothing ran, so there is no success to announce. Saying "passed"
        # here is the defect this branch exists to prevent.
        status, verdict = "skip", "skipped"
    elif warned:
        status, verdict = "warn", "passed with warnings"
    else:
        status, verdict = "pass", "passed"

    lines = [COMMENT_MARKER, f"## {_status_mark(status, 24)} Commit Check {verdict}"]
    lines.append("")
    if status == "skip":
        lines.extend(["Nothing was validated.", ""])
    if results:
        lines.extend([_group_lines(results), ""])
    if skipped and _skip_reasons(results):
        lines.extend([_skip_reasons(results), ""])
    if failed:
        lines.extend([*_diff_block(results), ""])
    if warned:
        lines.extend(["**Warnings**", "", *_diff_block(results, "warn"), ""])
    if failed:
        how = _how_to_fix(results)
        lines.extend([*how, ""] if how else [])
    lines.extend([*_all_checks(results), "", _report_footer()])
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

    Written through :func:`_write_output`, whose per-write random delimiter
    keeps a line of user text (a commit subject reading ``EOF``, say) from
    closing the heredoc early.
    """
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    payload = {
        "status": overall_status(results),
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
    marked = [c for c in comments if COMMENT_MARKER in c.body and _is_bot(c)]
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
        # Name only the surfaces this run actually produced: with
        # job-summary: false there is no summary to send the reader to.
        where = "in the annotations on the run page"
        if _summary_written():
            where = f"in this job's summary and {where}"
        msg = (
            "Skipping PR comment: pull requests from forked repositories "
            "cannot write comments via the pull_request event (GITHUB_TOKEN is "
            f"read-only for forks). The findings are {where}. "
            "See https://github.com/commit-check/commit-check-action/blob/main/docs/fork-pr-comments.md"
        )
        print(f"::warning::{msg}")
        if JOB_SUMMARY_ENABLED and GITHUB_STEP_SUMMARY:
            with open(GITHUB_STEP_SUMMARY, "a", encoding="utf-8") as f:
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
    except Exception as e:  # noqa: BLE001 - the comment is best-effort
        print(f"::warning::Unable to post PR comment: {e}", file=sys.stderr)
        return 0


def ai_tools_found(results: list[ScopeResult]) -> list[str]:
    """Every AI tool found in the pull request, sorted.

    In the checked commits (messages and authors), the title, the
    description, and the app that opened the pull request.
    """
    tools = {tool for scope in results for tool in scope.ai_tools}
    tools.update(PR_BODY_AI_TOOLS)
    if is_pr_event():
        opener = get_pr_opener_tool()
        if opener:
            tools.add(opener)
    return sorted(tools)


def add_ai_label(results: list[ScopeResult]) -> None:
    """Label the pull request when an AI tool wrote any of it (``ai-label``).

    The label is only ever added, never removed: a maintainer may have put
    it there by hand. Best-effort, like the comment: a failure warns and
    never fails the step.
    """
    name = ai_label_name()
    if not name or not is_pr_event():
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


def fail_on_invalid_flags() -> None:
    """Stop with an error when a boolean input is neither true nor false."""
    if not INVALID_FLAGS:
        return
    for name, value in INVALID_FLAGS:
        input_name = name.lower().replace("_", "-")
        print(
            f"::error::Input {input_name} must be true or false, got "
            f"{_log_safe(repr(value))}"
        )
    sys.exit(1)


def main():
    """Main function to run commit-check and render all output surfaces."""
    _reconfigure_io()
    fail_on_invalid_flags()
    log_env_vars()

    ret_code, results = run_commit_check()

    render_step_log(results)
    set_result_output(results)

    ret_code = max(ret_code, add_job_summary(results), add_pr_comments(results))
    add_ai_label(results)

    if DRY_RUN_ENABLED:
        ret_code = 0

    log_error_and_exit(ret_code)


if __name__ == "__main__":
    main()
