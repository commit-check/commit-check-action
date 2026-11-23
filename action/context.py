"""The event, the pull request and its commits, read from git or the API."""

import json
import os
import subprocess
import sys
from typing import Any

from action import escape, model

COMMIT_MESSAGE_DELIMITER = "\x00"

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


def warn_shallow_checkout(problem: str, consequence: str) -> None:
    """Annotate a PR run whose clone is too shallow to do what was asked.

    ``actions/checkout`` defaults to ``fetch-depth: 1``, which leaves the
    synthetic merge commit as the only commit in the clone. Every caller
    hits that same root cause, so they share one message shape that names
    the fix rather than only the symptom.
    """
    text = f"{problem} ({checkout_hint()}); {consequence}"
    print(f"::warning title=commit-check::{escape._annotation_escape(text)}")


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


#: ``git log`` format that yields, per commit, the full hash and the raw
#: message as two NUL-terminated fields (``%x00`` is git's spelling of
#: COMMIT_MESSAGE_DELIMITER; a literal NUL cannot be passed as an argument).
#: NUL cannot appear in a hash or a message, so the split is unambiguous
#: however many blank lines the message holds.
COMMIT_LOG_FORMAT = "--pretty=format:%H%x00%B%x00"


def parse_commit_messages(output: str) -> list[model.Commit]:
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


def _messages_in_range(revision_range: str) -> list[model.Commit]:
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


def get_messages_from_event_range() -> list[model.Commit]:
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


def get_messages_from_merge_ref() -> list[model.Commit]:
    """Read PR commit messages from GitHub's synthetic merge commit.

    Only meaningful on a ``pull_request`` checkout, where HEAD is
    ``refs/pull/N/merge``. On ``pull_request_target`` HEAD is the base
    branch: ``HEAD^2`` is then nothing, or the parent of some unrelated
    merge on it, whose commits are not the pull request's.
    """
    if os.getenv("GITHUB_EVENT_NAME") != "pull_request":
        return []
    return _messages_in_range("HEAD^1..HEAD^2")


def get_messages_from_head_ref(base_ref: str) -> list[model.Commit]:
    """Read PR commit messages when the workflow checks out the head SHA."""
    return _messages_in_range(f"origin/{base_ref}..HEAD")


#: The most commits GitHub's pull request commits endpoint returns.
API_COMMIT_LIMIT = 250

#: Author (name, email) of each commit listed by the API, by hash. Those
#: commits are not in the clone, so this is the only record of who wrote them.
API_AUTHORS: dict[str, tuple[str, str]] = {}


def get_messages_from_api() -> list[model.Commit]:
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


def get_pr_commit_messages() -> list[model.Commit]:
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
