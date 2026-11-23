"""Running commit-check and turning its JSON into scopes."""

import json
import os
import subprocess
import sys
import tempfile
from typing import Any

from action import base_config, context, escape, inputs, model

#: Human-readable labels for the non-message CLI flags.
CHECK_LABELS = {
    "--branch": "Branch",
    "--author-name": "Author name",
    "--author-email": "Author email",
}

#: Label of the scopes that check the files a commit changes (``files: true``).
FILES_LABEL = "Files"

#: The AI attribution rules: what ``message-ai-attribution: true`` keeps.
AI_RULE_IDS = frozenset({"CC013", "CC014", "CC015", "CC016", "CC017"})

#: The checks whose subject is a commit's recorded author.
AUTHOR_FLAGS = ("--author-name", "--author-email")

#: The rule each author check runs, for a scope that had to be skipped.
AUTHOR_RULES = {
    "--author-name": ("CC101", "author_name"),
    "--author-email": ("CC102", "author_email"),
}


def skipped_author_scope(flag: str) -> model.ScopeResult:
    """A scope recording that an author check could not run at all.

    Reported as ``skip``, never as a pass: nothing was validated, and the
    one commit the clone does hold (HEAD) has the wrong author for a pull
    request, GitHub's merge commit or the base branch.
    """
    rule_id, check = AUTHOR_RULES[flag]
    return model.ScopeResult(
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


#: Why a files scope checked nothing.
FILES_UNCONFIGURED = "no rule is set in the [files] section of the config"

FILES_NOT_IN_CLONE = "the checkout lacks the commit or its parent"

FILES_UNLISTED = "the pull request's commits could not be listed"

FILES_NOTHING_CHANGED = "the commit adds or changes no file"


def skipped_files_scope(reason: str, sha: str = "") -> model.ScopeResult:
    """A scope recording that the files of a commit, or of all, went unchecked.

    Reported as ``skip``, never as a pass: nothing was validated.
    """
    return model.ScopeResult(
        label=FILES_LABEL,
        sha=sha,
        checks=[
            {
                "rule_id": "",
                "check": "files",
                "status": "skip",
                "value": "",
                "error": "",
                "suggest": "",
                "docs_url": "",
                "reason": reason,
            }
        ],
    )


#: Why an AI-only scope ran no rule: under the default policy there is none to run.
AI_POLICY_IGNORED = 'ai_attribution is "ignore": set it to "forbid" or "disclose"'


def skip_when_no_ai_rule_ran(scope: model.ScopeResult) -> model.ScopeResult:
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


def _message_value(scope: model.ScopeResult) -> str:
    """The whole commit message, as the CLI's ``message`` rule reports it."""
    return next(
        (
            str(c.get("value") or "")
            for c in scope.checks
            if c.get("check") == "message"
        ),
        "",
    )


def keep_ai_rules(scope: model.ScopeResult) -> model.ScopeResult:
    """Narrow a commit message scope to the AI rules (CC013 to CC017).

    ``message-ai-attribution: true`` does this whatever ``message`` says:
    ``message`` defaults to true, so asking for the AI rules alone must not
    need it turned off as well. The CLI runs every message rule and the
    format rules' verdicts are dropped here, so the CLI needs no flag of
    its own for it. A scope left with no rule, under the default
    ``ai_attribution = "ignore"``, is reported as skipped.
    """
    if not inputs.MESSAGE_AI_ATTRIBUTION_ENABLED or scope.raw_text:
        return scope
    # The message rule's value is the whole message: keep it for the report
    # before the rule itself is dropped (outside a PR nothing was piped in).
    scope.text = scope.text or _message_value(scope)
    scope.checks = [c for c in scope.checks if c.get("rule_id") in AI_RULE_IDS]
    return skip_when_no_ai_rule_ran(scope)


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


def name_the_line_changes(scope: model.ScopeResult, body: str) -> None:
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
            print(f"commit-check: {escape._log_safe(line)}", file=sys.stderr)


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
    command = ["commit-check", "--format", "json", *base_config.CONFIG_ARGS] + args
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
) -> model.ScopeResult:
    """Run commit-check for one scope and wrap the outcome in a ScopeResult.

    ``sha`` names the commit a message scope checked; it rides along on both
    outcomes so an unparsable CLI response still says which commit it was.
    """
    _rc, data, raw = run_check_json(args, input_text=input_text, env=env)
    if isinstance(data, dict):
        tools = data.get("ai_tools")
        return model.ScopeResult(
            label=label,
            checks=data.get("checks", []),
            sha=sha,
            ai_tools=[str(t) for t in tools] if isinstance(tools, list) else [],
            text=input_text or "",
        )
    return model.ScopeResult(label=label, raw_text=raw, sha=sha)


def run_pr_message_checks(pr_commits: list[model.Commit]) -> list[model.ScopeResult]:
    """Check each PR commit individually via commit-check --message.

    The raw message is piped in, so every commit is read the same way
    whether the clone holds it or only the API listed it. Its own author
    goes with it as git's author variables, which is where the CLI reads the
    author of a piped message: the AI author rule (CC017) and
    ``ignore_authors`` then judge that commit's author, not whoever HEAD or
    the runner's git config names.
    """
    results: list[model.ScopeResult] = []
    total = len(pr_commits)
    for index, (sha, msg) in enumerate(pr_commits, start=1):
        label = f"Commit {index}/{total}"
        author = context.API_AUTHORS.get(sha) or (
            context._local_author(sha) if sha else None
        )
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
    flags: list[str], pr_commits: list[model.Commit]
) -> list[model.ScopeResult]:
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
        author = context.API_AUTHORS.get(sha) or (
            context._local_author(sha) if sha else None
        )
        if author:
            newest.pop(author, None)
            newest[author] = sha
    revs = [
        sha
        for sha in newest.values()
        if context._rev_resolves(sha) or context._fetch_commit(sha)
    ]
    if len(revs) != len(newest):
        return []
    authors = {sha: author for author, sha in newest.items()}
    scopes: list[model.ScopeResult] = []
    for flag in flags:
        for sha in revs:
            scope = check_scope(CHECK_LABELS[flag], [flag, "--rev", sha], sha=sha)
            name, email = authors[sha]
            scope.subject = name if flag == "--author-name" else email
            scopes.append(scope)
    return scopes


def _shallow_boundary() -> set[str]:
    """The commits a shallow clone holds without their parents.

    Git lists them, one hash per line, in the repository's ``shallow`` file;
    a complete clone has none. Such a commit looks like a root commit, so a
    diff of it names every file in the repository as changed.
    """
    try:
        located = subprocess.run(
            ["git", "rev-parse", "--git-path", "shallow"],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        if located.returncode != 0:
            return set()
        with open(located.stdout.strip(), encoding="utf-8") as f:
            return set(f.read().split())
    except OSError:
        return set()


def run_files_checks(shas: list[str]) -> list[model.ScopeResult]:
    """Check the files each commit changes, via ``commit-check --files --rev``.

    One scope per commit, so a finding names the commit that has to change:
    a file added in one commit and removed in the next is still in the
    history. A commit is only checked when the checkout holds it together
    with its parent, since the files it changes are a diff against that
    parent. The others are reported as skipped, never guessed at.

    With no rule set in ``[files]`` the CLI runs none, for any commit, and
    one skipped scope says so in place of a row of empty passes.
    """
    boundary = _shallow_boundary()
    readable = {
        sha
        for sha in shas
        if sha and sha not in boundary and context._rev_resolves(sha)
    }
    if not readable:
        # Whether a rule is set at all, asked of HEAD: only the list of
        # checks is read, never a verdict on HEAD itself.
        probe = check_scope(FILES_LABEL, ["--files"])
        if not (probe.checks or probe.raw_text):
            return [skipped_files_scope(FILES_UNCONFIGURED)]
        context.warn_shallow_checkout(
            "The checkout holds no commit to check together with its parent",
            "the files check was skipped",
        )
        return [skipped_files_scope(FILES_NOT_IN_CLONE)]

    results: list[model.ScopeResult] = []
    for sha in shas:
        if sha not in readable:
            results.append(skipped_files_scope(FILES_NOT_IN_CLONE, sha))
            continue
        scope = check_scope(FILES_LABEL, ["--files", "--rev", sha], sha=sha)
        if not (scope.checks or scope.raw_text):
            return [skipped_files_scope(FILES_UNCONFIGURED)]
        if scope.status == "skip" and not scope.skip_reason:
            # The file rules skip a commit that only deletes, or merges
            # without bringing a file along, and do not say why.
            for check in scope.checks:
                check["reason"] = FILES_NOTHING_CHANGED
        results.append(scope)
    missing = sum(1 for sha in shas if sha not in readable)
    if missing:
        context.warn_shallow_checkout(
            f"{missing} of {len(shas)} commits are not in the checkout "
            "together with their parent",
            "their files were not checked",
        )
    return results


def run_pr_files_checks(pr_commits: list[model.Commit]) -> list[model.ScopeResult]:
    """Check the files of every commit of the pull request."""
    if not pr_commits:
        context.warn_shallow_checkout(
            "Could not list the pull request's commits",
            "the files check was skipped",
        )
        return [skipped_files_scope(FILES_UNLISTED)]
    return run_files_checks([sha for sha, _msg in pr_commits])


def check_empty_message(
    label: str, sha: str, env: dict[str, str] | None
) -> model.ScopeResult:
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


def run_other_checks(
    args: list[str], rev: str | None = None
) -> list[model.ScopeResult]:
    """Run each non-message check (branch, author) once, as its own scope.

    ``rev`` goes to the author checks only: it names the commit whose
    recorded author is validated (commit-check >= 2.16.0), which in a PR is
    the branch tip rather than GitHub's merge commit. The branch check has
    no commit to point at, so it never takes it.
    """
    results: list[model.ScopeResult] = []
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
        ("--message", inputs.MESSAGE_ENABLED or inputs.MESSAGE_AI_ATTRIBUTION_ENABLED),
        ("--branch", inputs.BRANCH_ENABLED),
        ("--author-name", inputs.AUTHOR_NAME_ENABLED),
        ("--author-email", inputs.AUTHOR_EMAIL_ENABLED),
    ]
    return [flag for flag, enabled in flags if enabled]


def run_commit_check() -> tuple[int, list[model.ScopeResult]]:
    """Runs all enabled checks and returns the overall exit code and results.

    Checks are evaluated in order:
      1. PR title (when ``pr-title: true`` and in a PR event)
      2. PR description, AI rules only (when ``pr-body-ai-attribution: true`` and in a PR
         event; with only ``ai-label`` set it is read for the label, and no
         scope is reported)
      3. Individual PR commit messages (when ``message: true`` and in a PR event;
         AI rules only with ``message-ai-attribution: true``)
      4. The files each PR commit changes (when ``files: true``; outside a PR,
         the files of the HEAD commit)
      5. Author checks on every PR commit's author, then the remaining checks
         (branch; outside a PR, the HEAD commit and its author)

    Outside of a PR event all enabled checks are handed to the CLI at once.
    """
    args = build_check_args()
    results: list[model.ScopeResult] = []

    # ---- 1. PR title check ------------------------------------------------
    if inputs.PR_TITLE_ENABLED and context.is_pr_event():
        pr_title = context.get_pr_title()
        if pr_title:
            # --pr-title runs the --message rules a one-line title can meet:
            # none needing a body (CC011), a sign-off (CC012) or an author
            # (CC017), and its advice does not say to re-commit.
            results.append(check_scope("PR title", ["--pr-title"], input_text=pr_title))

    # ---- 2. PR description ------------------------------------------------
    if (
        inputs.PR_BODY_AI_ATTRIBUTION_ENABLED or inputs.ai_label_name()
    ) and context.is_pr_event():
        pr_body = context.get_pr_body()
        if inputs.PR_BODY_AI_ATTRIBUTION_ENABLED:
            # An empty description is checked too: it has no AI declaration,
            # which CC501 fails when [pull_request] ai_declaration_checkboxes
            # names the boxes. The attribution rules pass empty text.
            scope = check_scope(
                "PR description", ["--ai-attribution-only"], input_text=pr_body or ""
            )
            name_the_line_changes(scope, pr_body or "")
            results.append(skip_when_no_ai_rule_ran(scope))
        elif pr_body:
            scope = check_scope(
                "PR description", ["--ai-attribution-only"], input_text=pr_body
            )
            PR_BODY_AI_TOOLS[:] = scope.ai_tools

    # ---- 3. Commit message checks -----------------------------------------
    pr_commits: list[model.Commit] | None = None
    if inputs.MESSAGE_ENABLED or inputs.MESSAGE_AI_ATTRIBUTION_ENABLED:
        pr_commits = context.get_pr_commit_messages()
        if pr_commits:
            # In PR context: check each commit individually to avoid
            # only validating the synthetic merge commit at HEAD.
            results.extend(run_pr_message_checks(pr_commits))
            args = [a for a in args if a != "--message"]
        elif context.is_pr_event():
            # Falling through to HEAD validates the synthetic merge commit,
            # "Merge X into Y", which passes CC001 by default: a shallow
            # clone used to turn every pull request green without a word.
            context.warn_shallow_checkout(
                "Could not list the pull request's commits", "only HEAD was checked"
            )

    # Outside a PR, check the HEAD commit message directly.
    if "--message" in args:
        results.append(
            keep_ai_rules(
                check_scope("Commit message", ["--message"], sha=context.head_sha())
            )
        )
        args = [a for a in args if a != "--message"]

    # ---- 4. Files of each commit ------------------------------------------
    if inputs.FILES_ENABLED:
        if context.is_pr_event():
            if pr_commits is None:
                pr_commits = context.get_pr_commit_messages()
            results.extend(run_pr_files_checks(pr_commits))
        else:
            results.extend(run_files_checks([context.head_sha()]))

    # ---- 5. Remaining checks (branch, author, etc.) -----------------------
    rev = None
    author_flags = [flag for flag in args if flag in AUTHOR_FLAGS]
    if context.is_pr_event() and author_flags:
        if pr_commits is None:
            pr_commits = context.get_pr_commit_messages()
        author_scopes = run_pr_author_checks(author_flags, pr_commits)
        if author_scopes:
            results.extend(author_scopes)
            args = [a for a in args if a not in AUTHOR_FLAGS]
    if context.is_pr_event() and any(flag in AUTHOR_FLAGS for flag in args):
        rev = context.pr_head_rev()
        if rev is None:
            # HEAD's author is GitHub's merge commit on a pull_request
            # checkout and the base branch on pull_request_target: checking
            # it would grade the wrong person either way. Say so, and skip.
            context.warn_shallow_checkout(
                "Could not resolve the pull request's head commit for the "
                "author checks",
                "they were skipped",
            )
            for flag in args:
                if flag in AUTHOR_FLAGS:
                    results.append(skipped_author_scope(flag))
            args = [a for a in args if a not in AUTHOR_FLAGS]
    results.extend(run_other_checks(args, rev=rev))

    exit_code = model.exit_code_for(results)
    return exit_code, results
