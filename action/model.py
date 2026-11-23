"""ScopeResult, the one shape every check result takes, and the run's overall status."""

from dataclasses import dataclass, field


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
    #: What an author check looked at (the name or the email), which the CLI
    #: does not report for a skipped one; the listing shows it in that row.
    subject: str = ""

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


#: One commit to check: ``(full sha, message)``.
Commit = tuple[str, str]
