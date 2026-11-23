"""Rendering results: the step log, and the Markdown report the job summary
and the PR comment share."""

import os
import re

from action import cli, escape, inputs, model

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
#   <mark:line-passed> Pull request: passed<br><mark:line-failed> Commits: 2 of 3 failed<br><mark:line-failed> Branch: failed
#   (one source line: see _group_lines)
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
#   <sub>Checked by [Commit Check](https://commit-check.com) 2.22.0 · [Rules](…) · [Check before you push](…)</sub>
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
# - With `files: true` each commit also gets a row in a "Files" group, for the
#   files it changes. Its hunk reads `@@ 5584f46 files @@`, with a red line per
#   path a rule found and the rule's advice under it. The agent prompt then
#   opens with "changing only the files they name" in place of "without
#   changing any code".
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


RULES_URL = "https://commit-check.com/rules/"

#: Hidden marker identifying comments this action owns.
#
# Comment identity has to be something a human cannot type by accident. The
# previous title-prefix match meant any comment opening with "# Commit Check"
# was treated as ours — and old ones are deleted, not just skipped. An HTML
# comment is invisible in the rendered body and is what Codecov, SonarQube and
# CodSpeed all use for the same purpose.
COMMENT_MARKER = "<!-- commit-check-action -->"

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


def _names_a_commit(scope: model.ScopeResult) -> bool:
    """Whether the scope is one commit's: its message, or the files it changes."""
    return bool(scope.sha) and (
        scope.label.startswith("Commit") or scope.label == cli.FILES_LABEL
    )


def _found(check: dict[str, str]) -> str:
    """First line of what a rule reports it found."""
    value = str(check.get("value") or "").strip()
    return value.splitlines()[0].strip() if value else ""


def _scope_group(label: str) -> str:
    """Group name for a scope label, used to fold the step log output."""
    if label in ("PR title", "PR description"):
        return "Pull request"
    if label.startswith("Commit"):
        return "Commits"
    if label.startswith("Author"):
        return "Author"
    return label


def _grouped(
    results: list[model.ScopeResult],
) -> list[tuple[str, list[model.ScopeResult]]]:
    """Split results into ordered groups for step log folding."""
    groups: list[tuple[str, list[model.ScopeResult]]] = []
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


def _group_status(scopes: list[model.ScopeResult]) -> tuple[str, str]:
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


def _first_value(scope: model.ScopeResult) -> str:
    """First line of what the scope checked: a subject, a title, a branch.

    The text the action piped in when it has it, then the whole message the
    ``message`` rule reports, then whichever rule names a value first. A PR
    description has no such line: its first line does not identify it. An
    AI rule's value is the tool or the author it found, never the text, so
    it never stands in for a commit's subject.
    """
    if scope.label == "PR description":
        return ""
    if scope.label == cli.FILES_LABEL:
        # Each file rule names its own finding, a path and what is wrong
        # with it. The first finding stands for the scope; with none, the
        # count of files a passing rule reports.
        checks = [*scope.failures, *scope.warnings] or scope.checks
        return next((_found(check) for check in checks if _found(check)), "")
    values = [scope.text, cli._message_value(scope)]
    values.extend(
        str(check.get("value") or "")
        for check in scope.checks
        if check.get("rule_id") not in cli.AI_RULE_IDS
    )
    values.append(scope.subject)
    for value in values:
        if value.strip():
            return value.strip().splitlines()[0].strip()
    return ""


def _scope_value(scope: model.ScopeResult, max_len: int = 80) -> str:
    """``_first_value`` capped for a row of the listing.

    The value reads naturally next to the scope's label, and the cap keeps a
    passing row on one line of the fenced block. 80 holds a subject written
    to the usual 72-column limit; a longer one is cut at a word, with an
    ellipsis, rather than mid-word.
    """
    if scope.label == "PR description" and scope.status == "pass":
        # Its first line does not identify it; what was found does.
        if scope.ai_tools:
            return f"AI attribution: {', '.join(scope.ai_tools)}"
        return "no AI attribution"
    value = _first_value(scope)
    if len(value) <= max_len:
        return value
    cut = value[: max_len - 1]
    space = cut.rfind(" ")
    if space > max_len // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _line_edits(
    scope: model.ScopeResult, check: dict[str, str]
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
    recorded = scope.edits.get(cli._rule_key(check))
    if recorded:
        return recorded
    fix = (check.get("fix") or "").strip()
    if not fix:
        return [], []
    value = str(check.get("value") or "").strip()
    text = (scope.text or cli._message_value(scope)).strip()
    one_line = "\n" not in value and "\n" not in fix
    if check.get("rule_id") in cli.AI_RULE_IDS:
        original = text
    elif one_line and value in text.splitlines():
        original = value
    else:
        original = text or value
    if not original:
        return [], []
    return cli.lines_removed(original, fix), cli.lines_removed(fix, original)


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


def _instruction(scope: model.ScopeResult, check: dict[str, str]) -> str:
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


def _target(
    scope: model.ScopeResult,
    instructions: list[str],
    label: str,
    value: str | None = None,
) -> str:
    """Name what an instruction is about: ``Commit 2/3 (5584f46) "bad msg"``.

    The quoted value is left out when an instruction already quotes it. It
    is the scope's own unless ``value`` names another: a files scope holds
    one finding per rule, each about a path of its own.
    """
    if value is None:
        value = _first_value(scope)
    if value and not any(f'"{value}"' in text for text in instructions):
        return f'{label} "{value}"'
    return label


def _findings(scope: model.ScopeResult, status: str) -> list[dict[str, str]]:
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

    The rest of a multi-line value (a commit body, the second declaration
    box CC501 found) is aligned under its first line, so the value reads as
    one block and the error after it is not mistaken for part of it. A blank
    line stays empty, with no trailing spaces.
    """
    lines: list[str] = []
    if check.get("value"):
        first, *rest = str(check["value"]).splitlines()
        lines.append(f"value: {first}")
        lines.extend(f"{'':7}{line}" if line else "" for line in rest)
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
        lines.extend(f"        {line}" if line else "" for line in _finding_lines(check))
        if include_docs and check.get("docs_url"):
            lines.append(f"        Docs: {check['docs_url']}")
    return lines


def _row_label(scope: model.ScopeResult) -> str:
    """A scope's name within its group: the short hash, ``title``, ``email``.

    The group header already says "Commits" or "Author", so the row only
    needs what tells it apart from its neighbours.
    """
    if _names_a_commit(scope):
        return scope.sha[:7]
    _group, _, name = scope.label.partition(" ")
    return name or scope.label.lower()


def _row(mark: str, scope: model.ScopeResult, value: str) -> str:
    """One aligned row of the listing: mark, label, value."""
    return f"  {mark} {_row_label(scope):<12} {value}".rstrip()


def _one_skip_reason(results: list[model.ScopeResult]) -> bool:
    """Whether every skipped scope was skipped for the same, named reason.

    Then the one sentence under the verdict (``_skip_reasons``) explains
    them all, and each skipped row can show what it skipped instead of
    repeating why.
    """
    reasons = {s.skip_reason for s in results if s.status == "skip"}
    return len(reasons) == 1 and "" not in reasons


def _render_scopes(
    scopes: list[model.ScopeResult], include_docs: bool, reason_said: bool = False
) -> list[str]:
    """Render the indented listing for one group of scopes, without its header.

    Shared by both output surfaces so they cannot drift: the step log and the
    Markdown listing are the same rows, and the only difference is the docs
    link, which the Markdown report already carries under the diff.

    A failing or warned scope shows its value in full rather than capped. It
    is the one value the reader has to act on, and the cap can cut off the
    part that explains it.

    ``reason_said`` is for a report whose skipped scopes share one reason,
    stated once under the verdict: a skipped row then shows what it skipped
    (a commit's subject, an author) rather than that reason again.
    """
    lines: list[str] = []
    for scope in scopes:
        if scope.status == "skip":
            # Deliberately not a ✔. Nothing was validated here, and a tick
            # claiming otherwise is what made a bypassed policy look enforced.
            value = _scope_value(scope) if reason_said else ""
            if value:
                lines.append(_row("⊘", scope, value))
                continue
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


def _render_tree(results: list[model.ScopeResult], include_docs: bool) -> list[str]:
    """Render the full grouped listing: a header line per group, then its scopes."""
    lines: list[str] = []
    reason_said = _one_skip_reason(results)
    for group_name, scopes in _grouped(results):
        lines.append(group_name)
        lines.extend(_render_scopes(scopes, include_docs, reason_said))
    return lines


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


def _annotation_body(scope: model.ScopeResult, check: dict[str, str]) -> str:
    """The message of one finding's annotation, before escaping.

    The run page shows only the first line of an annotation, so that line
    says where and what to change: ``Commit 2/3 (5584f46): change "bad msg"
    to "fix: bad msg"``. The rule's own sentence follows on the second line
    for whoever expands it.
    """
    instruction = _instruction(scope, check)
    found = _found(check) if scope.label == cli.FILES_LABEL else None
    target = _target(scope, [instruction], scope.display_label, found)
    headline = f"{target}: {instruction}"
    error = _plain_error(check)
    return headline if error == instruction else f"{headline}\n{error}"


def render_step_log(results: list[model.ScopeResult]) -> None:
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
    reason_said = _one_skip_reason(results)
    for group_name, scopes in _grouped(results):
        status, text = _group_status(scopes)
        print(f"::group::{STATUS_GLYPHS[status]} {group_name}: {text}")
        for line in _render_scopes(scopes, include_docs=True, reason_said=reason_said):
            print(_coloured(escape._log_safe(line)))
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

    level = "warning" if inputs.DRY_RUN_ENABLED else "error"
    for title, message in errors:
        print(
            f"::{level} title={escape._annotation_escape(title)}"
            f"::{escape._annotation_escape(message)}"
        )
    for title, message in warnings:
        print(
            f"::warning title={escape._annotation_escape(title)}"
            f"::{escape._annotation_escape(message)}"
        )

    # The verdict is a plain line, never an ::error: the findings above are
    # already one annotation each, and a second, untitled ::error for the
    # total inflated the run's error count and told the reader nothing new.
    if errors:
        failed, total = _check_counts(results)
        if inputs.DRY_RUN_ENABLED:
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
                print(escape._log_safe(_skip_reasons(results)))
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
                print(escape._log_safe(_skip_reasons(results)))
        else:
            print(f"{ANSI['pass']}✔ Commit Check: all checks passed{ANSI_RESET}")


def _summary_written() -> bool:
    """Whether this run writes a job summary a log line can point to."""
    return bool(inputs.JOB_SUMMARY_ENABLED and inputs.GITHUB_STEP_SUMMARY)


def _check_counts(results: list[model.ScopeResult]) -> tuple[int, int]:
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


def _skip_count(results: list[model.ScopeResult]) -> int:
    """Number of scopes that never ran.

    Reported separately from the pass count so the headline cannot claim
    that checks passed when they were skipped.
    """
    return sum(1 for scope in results if scope.status == "skip")


def _skip_reasons(results: list[model.ScopeResult]) -> str:
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


def _warn_count(results: list[model.ScopeResult]) -> int:
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
    """The ``<img>`` of a status mark: the tile at 24 px, the line mark at 16.

    The tile sits at the top of the heading. A line mark is centred on its
    line of text (``absmiddle``): aligned to the top it rode about two pixels
    above the words beside it.
    """
    name = f"{'line-' if line else ''}{STATUS_WORDS[status]}"
    align = "absmiddle" if line else "top"
    return (
        f'<img alt="{STATUS_GLYPHS[status]}" width="{size}" height="{size}" '
        f'align="{align}" src="{STATUS_MARK_URL}/{name}.png">'
    )


def _group_lines(results: list[model.ScopeResult]) -> str:
    """One line per group, its status mark in front: the overview.

    The lines are joined by ``<br>`` alone, on one source line. A pull request
    comment turns every newline into a line break of its own, so ``<br>``
    followed by a newline left an empty line between the groups there.
    """
    return "<br>".join(
        f"{_status_mark(status, 16, line=True)} {group_name}: {text}"
        for group_name, scopes in _grouped(results)
        for status, text in [_group_status(scopes)]
    )


def _hunk(scope: model.ScopeResult, status: str) -> list[str]:
    """One scope's findings as a diff hunk.

    ``@@ where @@``, then red for what was found and has to go, green for
    what to write instead, and a ``#`` comment for advice that has no
    literal text to offer. GitHub colours all three in a ``diff`` block,
    which is the only colour a comment or a job summary can carry.
    """
    if scope.sha and scope.label.startswith("Commit"):
        where = scope.sha[:7]
    elif scope.sha and scope.label == cli.FILES_LABEL:
        where = f"{scope.sha[:7]} files"
    else:
        where = scope.label
    lines = [f"@@ {where} @@"]
    if scope.label == cli.FILES_LABEL and scope.checks:
        # One finding per rule, each about a path of its own: the path it
        # found, then what to do about it.
        for check in _findings(scope, status):
            if _found(check):
                lines.append(f"- {_found(check)}")
            lines.append(f"# {_advice(check)}")
        return lines
    value = _first_value(scope)
    removed: list[str] = []
    added: list[str] = []
    advice: list[str] = []
    # Whether a piece of advice is about the value itself: a subject or a
    # branch name that has to change. Advice about something else, such as
    # an AI tool as the commit's author, leaves the subject as context.
    about_value = False
    for check in _findings(scope, status):
        minus, plus = _line_edits(scope, check)
        removed.extend(minus)
        added.extend(plus)
        if not (minus or plus):
            advice.append(_advice(check))
            checked = str(check.get("value") or "").strip()
            about_value |= not checked or checked.splitlines()[0].strip() == value
    if scope.raw_text and not scope.checks:
        advice.append("the output could not be parsed, see the step log")
        about_value = True
    removed, added, advice = (list(dict.fromkeys(x)) for x in (removed, added, advice))
    if value and value not in removed:
        # Advice about the value means it is what was found; a fix that only
        # touches other lines, or advice about something else, leaves it as
        # context.
        lines.append(f"{'-' if about_value else ' '} {value}")
    lines.extend(f"- {line}" for line in removed)
    lines.extend(f"+ {line}" for line in added)
    lines.extend(f"# {line}" for line in advice)
    return lines


def _diff_block(results: list[model.ScopeResult], status: str = "fail") -> list[str]:
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
    commits = list(
        dict.fromkeys(
            f"[`{scope.sha[:7]}`]({_commit_url(scope.sha)})"
            for scope in scopes[:shown]
            if _names_a_commit(scope) and _commit_url(scope.sha)
        )
    )
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

#: The same line when a finding is about a file, which then has to change.
AGENT_PROMPT_FILES = (
    "Fix these Commit Check findings, changing only the files they name:"
)

BRANCH_HELP = (
    "**Branch name.** Rename the branch and push it. A pull request cannot "
    "change its branch, so open a new one from the renamed branch:"
)

AUTHOR_HELP = (
    "**Author.** Set your git identity (`git config user.name`, "
    "`git config user.email`), then `git commit --amend --reset-author` "
    "and force-push."
)

FILES_HELP = (
    "**Files.** Amend the commit that changed the file (`git rebase -i`, or "
    "`git commit --amend` for the last one), then force-push. A later commit "
    "that removes the file leaves it in the history."
)

HOOK_HELP = (
    f"**Next time.** The [pre-commit hook]({LOCAL_HOOK_URL}) runs the same "
    "rules before you push."
)


def _agent_prompt(failing: list[model.ScopeResult]) -> list[str]:
    """A prompt to paste into an AI coding agent, one line per failing scope.

    A files scope gets a line per finding: each is about a path of its own.
    """
    about_files = any(scope.label == cli.FILES_LABEL for scope in failing)
    lines = [AGENT_PROMPT_FILES if about_files else AGENT_PROMPT]
    for scope in failing:
        if scope.label == cli.FILES_LABEL:
            label = f"Files of commit {scope.sha[:7]}" if scope.sha else scope.label
            for check in scope.failures:
                todo = [_advice(check)]
                lines.append(
                    f"- {_target(scope, todo, label, _found(check))}: {todo[0]}"
                )
            continue
        todo = list(dict.fromkeys(_instruction(scope, c) for c in scope.failures))
        is_commit = bool(scope.sha) and scope.label.startswith("Commit")
        label = f"Commit {scope.sha[:7]}" if is_commit else scope.label
        lines.append(f"- {_target(scope, todo, label)}: {'; '.join(todo)}")
    return lines


def _is_last_commit(scope: model.ScopeResult) -> bool:
    """Whether the scope is the newest commit: ``Commit 3/3`` or the HEAD one."""
    position = re.fullmatch(r"Commit (\d+)/(\d+)", scope.label)
    return not position or position.group(1) == position.group(2)


def _reword_commands(commits: list[model.ScopeResult]) -> list[str]:
    """The git commands that reword the failing commits and push the result."""
    if len(commits) == 1 and _is_last_commit(commits[0]):
        reword = "git commit --amend"
    else:
        first = commits[0].sha[:7] or "<first failing commit>"
        note = '# change "pick" to "reword" on the commits above'
        reword = f"git rebase -i {first}~1   {note}"
    return [reword, "git push --force-with-lease"]


def _how_to_fix(results: list[model.ScopeResult]) -> list[str]:
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
    if cli.FILES_LABEL in labels:
        parts.append([FILES_HELP])
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


def _all_checks(results: list[model.ScopeResult]) -> list[str]:
    """The folded listing of every scope, passed ones included.

    The same rows the step log prints (group name, one ✔/✖ row per scope with
    the checked value), with the rule's reason, suggestion and fix under each
    finding, so one expand answers both "what was checked" and "what failed
    and why". The same block is used whether or not anything failed.
    """
    _failed, total = _check_counts(results)
    if total > 1:
        label = f"All {total} checks"
    else:
        label = "1 check" if total else "Details"
    body = _fenced("text", _render_tree(results, include_docs=False))
    return ["<details>", f"<summary>{label}</summary>", "", *body, "", "</details>"]


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


def _run_url() -> str:
    """Link to this workflow run, whose page holds the job summary."""
    repository = os.getenv("GITHUB_REPOSITORY", "")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    if not (repository and run_id):
        return ""
    server = os.getenv("GITHUB_SERVER_URL") or "https://github.com"
    return f"{server.rstrip('/')}/{repository}/actions/runs/{run_id}"


def _elsewhere(what: str) -> str:
    """Point to where the parts a shortened comment left out can be read."""
    where = "job summary" if _summary_written() else "step log"
    url = _run_url()
    place = f"[{where}]({url})" if url else where
    return f"{what} are in the {place} of this run."


def render_report(results: list[model.ScopeResult], detail: str = "full") -> str:
    """Render the Markdown report shared by the job summary and PR comment.

    Opens with the hidden marker and a heading that is the verdict — the
    status mark and ``Commit Check failed`` / ``passed`` — then one line per
    group, the diff of what failed, a diff of the warnings when the config
    listed any rule under ``warn``, the folded "How to fix", and the folded
    listing of every scope.

    ``detail`` is for a PR comment that would not fit (``render_pr_comment``):
    ``"short"`` leaves out the listing, ``"headline"`` everything below the
    group lines, and either says where to read what it left out.

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
    if detail == "headline":
        what = "The findings and how to fix them" if failed or warned else "All checks"
        lines.extend([_elsewhere(what), "", _report_footer()])
        return "\n".join(lines)
    if failed:
        lines.extend([*_diff_block(results), ""])
    if warned:
        lines.extend(["**Warnings**", "", *_diff_block(results, "warn"), ""])
    if failed:
        how = _how_to_fix(results)
        lines.extend([*how, ""] if how else [])
    if detail == "short":
        lines.extend([_elsewhere(f"All {_plural(total, 'check')}"), ""])
    else:
        lines.extend([*_all_checks(results), ""])
    lines.append(_report_footer())
    return "\n".join(lines)


def render_job_summary(results: list[model.ScopeResult]) -> str:
    """Create the Markdown body for the GitHub job summary."""
    return render_report(results)


#: The longest comment GitHub accepts, in UTF-16 code units.
COMMENT_LIMIT = 65536


def _comment_length(body: str) -> int:
    """Length of a comment as GitHub counts it: in UTF-16 code units."""
    return len(body.encode("utf-16-le")) // 2


def render_pr_comment(results: list[model.ScopeResult]) -> str:
    """Create the Markdown body for the PR comment (same report as summary).

    The report grows with the pull request: every failing commit's message
    is quoted in the diff, the prompt and the listing. A few dozen failing
    commits ran past GitHub's limit, the API refused the comment, and an
    earlier one, perhaps saying "passed", stayed on the pull request. So a
    report that does not fit drops the listing, then everything under the
    group lines, and points to the run for the rest. The job summary, with
    its far higher limit, keeps the full report.
    """
    for detail in ("full", "short"):
        body = render_report(results, detail)
        if _comment_length(body) <= COMMENT_LIMIT:
            return body
    return render_report(results, "headline")
