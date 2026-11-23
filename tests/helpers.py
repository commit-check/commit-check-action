"""Fixtures and builders the test modules share."""

import re

from action import model

HOOK_GUIDE = "https://commit-check.com/guides/integrations/#as-a-pre-commit-hook"

#: The step log's colour codes, which most assertions read past.
ANSI_CODES = re.compile(r"\x1b\[[0-9;]*m")


def make_check(
    check: str,
    status: str = "pass",
    rule_id: str = "CC001",
    value: str = "",
    error: str = "",
    suggest: str = "",
    fix: str = "",
    docs_url: str = "",
) -> dict[str, str]:
    """Build a single check outcome dict as produced by commit-check JSON."""
    return {
        "rule_id": rule_id,
        "check": check,
        "status": status,
        "value": value,
        "error": error,
        "suggest": suggest,
        "fix": fix,
        "docs_url": docs_url,
    }


#: Full hashes for scopes that carry a commit; the first seven characters
#: are what a reader sees.
SHA_A = "d87faca811e7017bbaa82f5c53eade0a97c108d8"

SHA_B = "5584f462cc3c947b2ba8d3d1a5735571803ee159"


def pass_scope(label: str = "Branch", value: str = "") -> model.ScopeResult:
    return model.ScopeResult(label=label, checks=[make_check("branch", value=value)])


def fail_scope(label: str = "Commit 1/1", sha: str = "") -> model.ScopeResult:
    return model.ScopeResult(
        label=label,
        sha=sha,
        checks=[
            make_check(
                "message",
                status="fail",
                rule_id="CC001",
                value="bad message",
                error="The commit message should follow Conventional Commits.",
                suggest="Use <type>(<scope>): <description>",
                docs_url="https://commit-check.com/rules/#cc001",
            )
        ],
    )


def fix_scope(label: str = "Commit 2/3", sha: str = SHA_B) -> model.ScopeResult:
    """A CC002 failure as commit-check 2.17 reports it: a mechanical fix, and
    a ``suggest`` the engine derived from that same fix."""
    return model.ScopeResult(
        label=label,
        sha=sha,
        checks=[
            make_check(
                "subject_capitalized",
                status="fail",
                rule_id="CC002",
                value="feat: add login page",
                error="Subject must start with a capital letter",
                suggest='Use "feat: Add login page"',
                fix="feat: Add login page",
                docs_url="https://commit-check.com/rules/#cc002",
            )
        ],
    )


def read_github_output(path: str) -> dict[str, str]:
    """Parse a ``GITHUB_OUTPUT`` file the way the runner does.

    A port of the loop in actions/runner's ``FileCommandManager``: a line is
    either ``name=value`` or ``name<<delimiter``, and a heredoc value runs up
    to the first line equal to the delimiter (CRLF counts as a newline).
    Anything else raises, as it fails the step on a real runner, so a test
    using this parser cannot pass on a file the runner would reject.
    """
    with open(path, encoding="utf-8", newline="") as file_obj:
        lines = file_obj.read().splitlines()
    outputs: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line:
            continue
        equals, heredoc = line.find("="), line.find("<<")
        if equals >= 0 and (heredoc < 0 or equals < heredoc):
            name, value = line.split("=", 1)
            outputs[name] = value
            continue
        if heredoc < 0:
            raise ValueError(f"Invalid format '{line}'")
        name, delimiter = line.split("<<", 1)
        if not delimiter:
            raise ValueError("Invalid format: empty delimiter")
        body: list[str] = []
        while True:
            if index >= len(lines):
                raise ValueError(f"Matching delimiter not found '{delimiter}'")
            current = lines[index]
            index += 1
            if current == delimiter:
                break
            body.append(current)
        outputs[name] = "\n".join(body)
    return outputs


def skip_scope(label: str = "PR title", reason: str = "") -> model.ScopeResult:
    """A scope whose every rule declined to run (e.g. author in ignore_authors)."""
    check = make_check("message", status="skip", rule_id="CC001", value="")
    if reason:
        check["reason"] = reason
    return model.ScopeResult(label=label, checks=[check])


def warn_scope(label: str = "Branch", value: str = "jsmith/fix-x") -> model.ScopeResult:
    """A scope with one rule the config lists under the top-level ``warn``."""
    return model.ScopeResult(
        label=label,
        checks=[
            make_check(
                "branch",
                status="warn",
                rule_id="CC201",
                value=value,
                error="The branch should follow Conventional Branch.",
                suggest="Use <type>/<description>",
                docs_url="https://commit-check.com/rules/#cc201",
            )
        ],
    )


def files_scope(sha: str = SHA_B, *checks) -> model.ScopeResult:
    """One commit's files scope; by default the two findings of a bad commit."""
    return model.ScopeResult(
        label="Files",
        sha=sha,
        checks=list(checks)
        or [
            make_check(
                "file_size",
                status="fail",
                rule_id="CC302",
                value="big.bin (4 KB)",
                error="File exceeds the maximum size of 1 KB",
                suggest="Keep files under 1 KB, or raise max_size in the [files] config",
                docs_url="https://commit-check.com/rules/#cc302",
            ),
            make_check(
                "file_pattern",
                status="fail",
                rule_id="CC303",
                value="key.pem (pattern *.pem)",
                error="File path matches a prohibited pattern",
                suggest="Remove the file from the commit",
                docs_url="https://commit-check.com/rules/#cc303",
            ),
        ],
    )


def files_pass(value: str = "2 file(s)", status: str = "pass") -> dict[str, str]:
    return make_check("file_pattern", status=status, rule_id="CC303", value=value)
