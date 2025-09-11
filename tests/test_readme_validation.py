# Test framework: pytest (assumed/preferred based on common Python projects)
# These tests validate README content added/modified in the PR, focusing on
# workflow badges, Usage snippet, Optional Inputs, admonitions, and key sections.

from pathlib import Path
import re
from typing import List, Optional

import pytest

README_CANDIDATES = [
    Path("README.md"),
    Path("Readme.md"),
    Path("README.MD"),
    Path("README.rst"),
]


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


@pytest.fixture(scope="module")
def readme_text() -> str:
    for p in README_CANDIDATES:
        if p.exists():
            return _normalize(p.read_text(encoding="utf-8"))
    pytest.skip("README not found at repository root")


def _extract_code_blocks(md: str, lang: Optional[str] = None) -> List[str]:
    # Matches fenced code blocks, optionally filtered by language tag.
    tag = re.escape(lang) if lang else r"[a-zA-Z0-9_\-]*"
    pattern = rf"```{tag}\s*\n(.*?)\n```"
    return re.findall(pattern, md, flags=re.DOTALL)


def _extract_subsection(md: str, heading: str) -> Optional[str]:
    # Extracts the body under a '### <heading>' until next ###/## or EOF.
    pat = rf"(?ms)^###\s+`?{re.escape(heading)}`?\s*$\n(.*?)(?=^\s*###\s+`?.*?`?\s*$|^\s*##\s+.*$|\Z)"
    m = re.search(pat, md)
    return m.group(1) if m else None


def test_readme_exists(readme_text: str) -> None:
    assert isinstance(readme_text, str) and len(readme_text) > 50, "README should exist and be non-trivial"
    return


def test_top_badges_present(readme_text: str) -> None:
    t = readme_text
    assert re.search(
        r"\[\!\[Commit Check\]\(https://img\.shields\.io/github/actions/workflow/status/commit-check/commit-check-action/commit-check\.yml\?branch=main&label=Commit%20Check&color=blue&logo=github\)\]\(https://github\.com/commit-check/commit-check-action/actions/workflows/commit-check\.yml\)",
        t,
    ), "Workflow status badge with branch=main should be present"
    assert re.search(
        r"https://img\.shields\.io/github/v/release/commit-check/commit-check-action\?color=blue",
        t,
    ), "GitHub release (latest SemVer) badge should be present"
    assert re.search(
        r"https://img\.shields\.io/static/v1\?label=Used%20by&message=\d+&color=informational", t
    ), "'Used by' shields.io badge should be present (do not assert specific count)"
    assert "https://img.shields.io/badge/Marketplace-commit--check--action-blue" in t, "Marketplace badge should be present"
    assert re.search(r"slsa\.dev/images/gh-badge-level3\.svg\?color=blue", t), "SLSA level 3 badge should be present"


def test_table_of_contents_has_expected_links(readme_text: str) -> None:
    anchors = [
        "Usage",
        "Optional Inputs",
        "GitHub Action Job Summary",
        "GitHub Pull Request Comments",
        "Badging Your Repository",
        "Versioning",
    ]
    for anchor in anchors:
        slug = anchor.lower().replace(" ", "-")
        assert re.search(
            rf"^\* \[{re.escape(anchor)}\]\(#{re.escape(slug)}\)\s*$", readme_text, flags=re.MULTILINE
        ), f"TOC entry for {anchor} should exist"


def test_usage_yaml_block_contains_required_items(readme_text: str) -> None:
    blocks = _extract_code_blocks(readme_text, "yaml")
    assert blocks, "A YAML Usage code block is expected"
    y = blocks[0]
    required_snippets = [
        "name: Commit Check",
        "\non:\n",
        "\npush:\n",
        "\npull_request:\n",
        "branches: 'main'",
        "jobs:",
        "commit-check:",
        "runs-on: ubuntu-latest",
        "uses: actions/checkout@v5",
        "with:",
        "ref: ${{ github.event.pull_request.head.sha }}",
        "fetch-depth: 0",
        "uses: commit-check/commit-check-action@v1",
        "env:",
        "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
    ]
    for snippet in required_snippets:
        assert snippet in y, f"Usage YAML should include: {snippet!r}"

    # Validate GitHub expressions patterns
    assert re.search(r"\$\{\{\s*github\.event\.pull_request\.head\.sha\s*\}\}", y), "PR head SHA expression must be present"
    assert re.search(
        r"\$\{\{\s*github\.event_name\s*==\s*['\"]pull_request['\"]\s*\}\}", y
    ), "Conditional pr-comments expression must be present"


def test_commit_check_action_inputs_in_usage_block(readme_text: str) -> None:
    y = _extract_code_blocks(readme_text, "yaml")[0]
    # Inputs expected in Usage 'with:' configuration
    inputs = [
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
    for i in inputs:
        assert i in y, f"Usage YAML should configure input: {i}"


def test_optional_inputs_sections_and_defaults(readme_text: str) -> None:
    t = readme_text
    expected_defaults = {
        "message": "true",
        "branch": "true",
        "author-name": "true",
        "author-email": "true",
        "commit-signoff": "true",
        "merge-base": "false",
        "imperative": "false",
        "dry-run": "false",
        "job-summary": "true",
        "pr-comments": "false",
    }
    for name, default in expected_defaults.items():
        sec = _extract_subsection(t, name)
        assert sec is not None, f"Missing Optional Input subsection: {name}"
        assert re.search(rf"Default:\s*`{re.escape(default)}`", sec), f"Default for `{name}` should be `{default}`"
        # Each section should have a brief description
        assert re.search(r"- \*\*Description\*\*:", sec), f"`{name}` subsection should include a Description bullet"


def test_important_admonitions_present(readme_text: str) -> None:
    t = readme_text
    # Expect two IMPORTANT blocks: merge-base and pr-comments
    important_blocks = re.findall(r"^> \[\!IMPORTANT\]\n>(?:.*\n?)+?(?=\n^[^>]|$)", t, flags=re.MULTILINE)
    assert len(important_blocks) >= 2, "At least two IMPORTANT admonitions should be present"

    assert re.search(r"`merge-base` is an experimental feature", t), "merge-base IMPORTANT note should be present"
    assert re.search(r"`pr-comments` is an experimental feature", t), "pr-comments IMPORTANT note should be present"
    assert re.search(r"\(#77\)", t) and "commit-check-action/issues/77" in t, "pr-comments note should reference issue #77"


def test_github_action_job_summary_section_images(readme_text: str) -> None:
    t = readme_text
    assert "## GitHub Action Job Summary" in t, "Job Summary section heading missing"
    assert re.search(r"\!\[Success job summary\]\(", t), "Success job summary image should be present"
    assert re.search(r"\!\[Failure job summary\]\(", t), "Failure job summary image should be present"


def test_pull_request_comments_section_images(readme_text: str) -> None:
    t = readme_text
    assert "## GitHub Pull Request Comments" in t, "PR Comments section heading missing"
    assert re.search(r"\!\[Success pull request comment\]\(", t), "Success PR comment image should be present"
    assert re.search(r"\!\[Failure pull request comment\]\(", t), "Failure PR comment image should be present"


def test_used_by_section_and_links(readme_text: str) -> None:
    t = readme_text
    assert "## Used By" in t, "Used By section heading missing"
    # Ensure at least one org avatar and the dependents link exist
    assert "avatars.githubusercontent.com" in t, "Expected org avatars in Used By section"
    assert "/commit-check/commit-check-action/network/dependents" in t, "Dependents 'many more' link should be present"


def test_badging_section_contains_md_and_rst_snippets(readme_text: str) -> None:
    t = readme_text
    assert "## Badging Your Repository" in t, "Badging section missing"
    assert "Markdown" in t and "reStructuredText" in t, "Both Markdown and reStructuredText subsections should be present"

    md_blocks = _extract_code_blocks(t, None)
    joined = "\n\n".join(md_blocks)
    # Badge URL should appear in the code examples
    assert "actions/workflows/commit-check.yml/badge.svg" in joined, "Badge SVG URL should be in code snippets"
    assert "actions/workflows/commit-check.yml" in joined, "Workflow link should be in code snippets"


def test_versioning_and_feedback_links(readme_text: str) -> None:
    t = readme_text
    assert "## Versioning" in t, "Versioning section missing"
    assert re.search(r"\(https?://semver\.org/?\)", t), "Semantic Versioning link should be present"
    assert "## Have questions or feedback?" in t, "Feedback section missing"
    assert "https://github.com/commit-check/commit-check/issues" in t, "Issues link should be present"


def test_all_images_have_alt_attributes(readme_text: str) -> None:
    # Basic check for HTML <img ... alt="..."> tags in README
    t = readme_text
    imgs = re.findall(r"<img\s+[^>]*>", t)
    for tag in imgs:
        assert re.search(r'alt="[^"]+"', tag), f"Image tag missing alt attribute: {tag}"


def test_workflow_badge_consistency(readme_text: str) -> None:
    # Top badge and badging section should reference the same workflow path
    t = readme_text
    # Extract all commit-check workflow badge URLs
    urls = re.findall(
        r"https://github\.com/commit-check/commit-check-action/actions/workflows/commit-check\.yml(?:/badge\.svg)?",
        t,
    )
    assert urls, "Expected workflow badge/link URLs"
    # Ensure both base workflow URL and badge.svg appear
    assert any(u.endswith("badge.svg") for u in urls) or "actions/workflows/commit-check.yml/badge.svg" in t, "badge.svg URL should be present"
    assert any(u.endswith("commit-check.yml") for u in urls), "Base workflow URL should be present"