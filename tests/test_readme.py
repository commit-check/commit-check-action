import os
import re
from pathlib import Path

README_CANDIDATES = [
    Path("README.md"),
    *Path(".").glob("README.*"),
]


def load_readme_text() -> str:
    for p in README_CANDIDATES:
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError()


def test_readme_exists_and_non_empty():
    text = load_readme_text()
    assert isinstance(text, str)
    assert len(text.strip()) > 0, "README should not be empty"


def test_title_and_intro_present():
    text = load_readme_text()
    assert re.search(
        r"^#\s+Commit-Check GitHub Action\s*$", text, re.M
    ), "Missing H1 title"
    assert (
        "A GitHub Action for checking commit message" in text
    ), "Missing introductory sentence"


def test_badges_section_contains_expected_badges():
    text = load_readme_text()
    # Key badges/links in the diff
    assert "actions/workflow/status/commit-check/commit-check-action/commit-check.yml" in text
    assert "shields.io/github/v/release/commit-check/commit-check-action" in text
    assert "Used%20by" in text and "network/dependents" in text
    assert "marketplace/actions/commit-check-action" in text
    assert "slsa-badge" in text or "slsa.dev/images/gh-badge" in text


def test_table_of_contents_includes_expected_sections():
    text = load_readme_text()
    sections = [
        "Usage",
        "Optional Inputs",
        "GitHub Action Job Summary",
        "GitHub Pull Request Comments",
        "Badging Your Repository",
        "Versioning",
    ]
    for sec in sections:
        assert re.search(rf"^\*\s+\[{re.escape(sec)}\]\(#", text, re.M), f"Missing ToC entry: {sec}"


def test_usage_block_contains_required_yaml_fields():
    text = load_readme_text()
    # Ensure the YAML block exists
    assert "```yaml" in text and "```" in text, "Usage YAML code fence missing"
    # Critical lines in example workflow
    required_lines = [
        "name: Commit Check",
        "on:",
        "push:",
        "pull_request:",
        "branches: 'main'",
        "- uses: actions/checkout@v5",
        "fetch-depth: 0",
        "- uses: commit-check/commit-check-action@v1",
        "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
        "with:",
        "message: true",
        "branch: true",
        "author-name: true",
        "author-email: true",
        "commit-signoff: true",
        "merge-base: false",
        "imperative: false",
        "job-summary: true",
        "pr-comments: ${{ github.event_name == 'pull_request' }}",
    ]
    for line in required_lines:
        assert line in text, f"Missing line in usage YAML: {line}"


def test_used_by_gallery_contains_key_orgs():
    text = load_readme_text()
    # The gallery is HTML-based; check representative orgs to avoid brittleness
    for org in ["Apache", "Texas Instruments", "Mila"]:
        assert org in text, f"Expected org '{org}' not found in Used By section"
    # Ensure the "many more" dependents link exists
    assert "network/dependents" in text, "Missing 'many more' dependents link"


def test_optional_inputs_section_and_defaults():
    text = load_readme_text()
    # Sub-headings for inputs
    inputs = [
        "message", "branch", "author-name", "author-email",
        "commit-signoff", "merge-base", "imperative",
        "dry-run", "job-summary", "pr-comments",
    ]
    for name in inputs:
        assert re.search(rf"^###\s+`{re.escape(name)}`\s*$", text, re.M), f"Missing input heading for `{name}`"

    # Representative default assertions (avoid overfitting to formatting)
    defaults = {
        "message": "Default: `true`",
        "branch": "Default: `true`",
        "author-name": "Default: `true`",
        "author-email": "Default: `true`",
        "commit-signoff": "Default: `true`",
        "merge-base": "Default: `false`",
        "imperative": "Default: `false`",
        "dry-run": "Default: `false`",
        "job-summary": "Default: `true`",
        "pr-comments": "Default: `false`",
    }
    for key, default_line in defaults.items():
        assert default_line in text, f"Missing default line for `{key}`"


def test_merge_base_and_pr_comments_important_notes_present():
    text = load_readme_text()
    # merge-base important note about fetch-depth: 0
    assert "merge-base" in text and "experimental" in text
    assert "fetch-depth: 0" in text, "merge-base note should mention fetch-depth: 0"
    # pr-comments important note and forked repos limitation with issue #77
    assert "pr-comments" in text and "experimental" in text
    assert "#77" in text, "pr-comments note should reference issue #77"


def test_job_summary_and_pr_comment_screenshots_present():
    text = load_readme_text()
    # Two sections each with an image
    assert "Success job summary" in text and "Failure job summary" in text
    assert "screenshot/success-job-summary.png" in text
    assert "screenshot/failure-job-summary.png" in text
    assert "Success pull request comment" in text and "Failure pull request comment" in text
    assert "screenshot/success-pr-comments.png" in text
    assert "screenshot/failure-pr-comments.png" in text


def test_badging_section_contains_markdown_and_rst_examples():
    text = load_readme_text()
    # Heading and primary badge
    assert "## Badging Your Repository" in text
    assert "badge.svg" in text
    # Markdown example fenced block
    md_block = re.search(r"(?s)Markdown\s+```[\s\S]*?```", text)
    assert md_block, "Missing Markdown badging example fenced block"
    assert "actions/workflows/commit-check.yml/badge.svg" in md_block.group(0)
    # reStructuredText example fenced block
    rst_block = re.search(r"(?s)reStructuredText\s+```[\s\S]*?```", text)
    assert rst_block, "Missing reStructuredText badging example fenced block"
    assert ".. image:: https://github.com/commit-check/commit-check-action/actions/workflows/commit-check.yml/badge.svg" in rst_block.group(0)


def test_versioning_and_feedback_sections_present():
    text = load_readme_text()
    assert "Versioning follows [Semantic Versioning]" in text
    assert re.search(r"\[issues\]\(https://github\.com/commit-check/commit-check/issues\)", text), "Missing issues link"


def test_top_badges_appear_near_top_of_file():
    text = load_readme_text()
    # Check first ~20 lines contain multiple badge URLs
    top = "\n".join(text.splitlines()[:25])
    urls = [
        "img.shields.io/github/actions/workflow/status/commit-check/commit-check-action/commit-check.yml",
        "img.shields.io/github/v/release/commit-check/commit-check-action",
        "img.shields.io/static/v1?label=Used%20by",
        "github.com/marketplace/actions/commit-check-action",
    ]
    missing = [u for u in urls if u not in top]
    assert not missing, f"Expected top badges missing: {missing}"


# The tests above intentionally avoid asserting volatile values (like exact counts)
# while thoroughly validating structure and key content introduced/changed in the diff.