"""README quality and consistency tests.

Testing library/framework: pytest
- These tests use only Python stdlib to avoid introducing new dependencies.
- They validate structure, key sections, badge/link syntax, and critical snippets
  from the README to catch regressions in documentation that users depend on.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import sys

README_CANDIDATES = [
    Path("README.md"),
    Path("Readme.md"),
    Path("README.rst"),
]
README_PATH = next((p for p in README_CANDIDATES if p.exists()), None)


def _require_readme() -> str:
    assert README_PATH is not None, (
        f"README file not found. Looked for: {', '.join(map(str, README_CANDIDATES))}"
    )
    return README_PATH.read_text(encoding="utf-8", errors="replace")


def test_readme_has_expected_title_and_intro():
    readme = _require_readme()
    assert "Commit-Check GitHub Action" in readme
    assert "A GitHub Action for checking commit message formatting" in readme


def test_table_of_contents_contains_expected_sections_ordered():
    readme = _require_readme()
    # Basic presence checks
    for section in [
        "Usage",
        "Optional Inputs",
        "GitHub Action Job Summary",
        "GitHub Pull Request Comments",
        "Badging Your Repository",
        "Versioning",
    ]:
        assert f"[{section}]" in readme or f"## {section}" in readme, f"Missing section: {section}"

    # Check that TOC links match headers (anchor-style)
    toc_block_match = re.search(r"## Table of Contents\s+([\s\S]+?)\n## ", readme)
    assert toc_block_match, "Table of Contents block not found"
    toc_block = toc_block_match.group(1)
    anchors = re.findall(r"\*\s+\[(.+?)\]\(#([a-z0-9\-]+)\)", toc_block, flags=re.I)
    assert anchors, "No TOC anchors found"
    # Ensure each anchor has a corresponding header
    for label, anchor in anchors:
        header_pattern = rf"^##\s+{re.escape(label)}\s*$"
        assert re.search(header_pattern, readme, flags=re.M), (
            f"Header for TOC entry '{label}' not found"
        )


def test_badges_and_links_are_well_formed_urls():
    readme = _require_readme()
    # Collect all markdown image and link URLs
    urls = []
    urls += re.findall(r"\!\[[^\]]*\]\((https?://[^)]+)\)", readme)  # images
    urls += re.findall(r"\[[^\]]*\]\((https?://[^)]+)\)", readme)  # links

    assert urls, "No URLs found in README; expected badges/links to be present"

    for u in urls:
        parsed = urlparse(u)
        assert parsed.scheme in ("http", "https"), f"Unexpected URL scheme in {u}"
        assert parsed.netloc, f"URL missing host: {u}"
        # Basic sanity: disallow spaces
        assert " " not in u, f"URL contains spaces: {u}"

    # Spot-check expected badge providers/domains
    assert any("img.shields.io" in u for u in urls), "Shields.io badges should be present"
    assert any("github.com/commit-check/commit-check-action" in u for u in urls), (
        "Repository links should be present"
    )


def test_usage_yaml_snippet_contains_expected_github_actions_fields():
    readme = _require_readme()
    # Extract the fenced yaml code block under Usage
    usage_match = re.search(r"## Usage[\s\S]+?```yaml([\s\S]+?)```", readme, flags=re.I)
    assert usage_match, "Usage YAML block not found"
    yaml_text = usage_match.group(1)

    # Validate presence of critical keys/values by regex (no external YAML dependency)
    expected_lines = [
        r"^name:\s*Commit Check\s*$",
        r"^on:\s*$",
        r"^\s+push:\s*$",
        r"^\s+pull_request:\s*$",
        r"^\s+branches:\s*'main'\s*$",
        r"^jobs:\s*$",
        r"^\s+commit-check:\s*$",
        r"^\s+runs-on:\s*ubuntu-latest\s*$",
        r"^\s+permissions:\s*#? ?use permissions because use of pr-comments\s*$",
        r"^\s+contents:\s*read\s*$",
        r"^\s+pull-requests:\s*write\s*$",
        r"^\s+steps:\s*$",
        r"^\s+- uses:\s*actions/checkout@v5\s*$",
        r"^\s+ref:\s*\$\{\{\s*github\.event\.pull_request\.head\.sha\s*\}\}\s*# checkout PR HEAD commit\s*$",
        r"^\s+fetch-depth:\s*0\s*# required for merge-base check\s*$",
        r"^\s+- uses:\s*commit-check/commit-check-action@v1\s*$",
        r"^\s+env:\s*$",
        r"^\s+GITHUB_TOKEN:\s*\$\{\{\s*secrets\.GITHUB_TOKEN\s*\}\}\s*# use GITHUB_TOKEN because use of pr-comments\s*$",
        r"^\s+with:\s*$",
        r"^\s+message:\s*true\s*$",
        r"^\s+branch:\s*true\s*$",
        r"^\s+author-name:\s*true\s*$",
        r"^\s+author-email:\s*true\s*$",
        r"^\s+commit-signoff:\s*true\s*$",
        r"^\s+merge-base:\s*false\s*$",
        r"^\s+imperative:\s*false\s*$",
        r"^\s+job-summary:\s*true\s*$",
        r"^\s+pr-comments:\s*\$\{\{\s*github\.event_name\s*==\s*'pull_request'\s*\}\}\s*$",
    ]
    for pattern in expected_lines:
        assert re.search(pattern, yaml_text, flags=re.M), (
            f"Missing or malformed line in Usage YAML matching: {pattern}"
        )


def test_optional_inputs_section_lists_all_expected_inputs_with_defaults():
    readme = _require_readme()
    # Build a map of input -> default as shown
    items = {
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

    # Ensure each input has a dedicated subsection header and default line
    for key, default in items.items():
        header_pat = rf"^###\s*`{re.escape(key)}`\s*$"
        assert re.search(header_pat, readme, flags=re.M), f"Missing Optional Inputs header for `{key}`"
        default_pat = rf"^-+\s*Default:\s*`{re.escape(default)}`"
        # Search within a bounded region (from header to either next ### or end)
        section_match = re.search(header_pat + r"([\s\S]+?)(^###\s*`|\Z)", readme, flags=re.M)
        assert section_match, f"Section body for `{key}` not found"
        section_body = section_match.group(1)
        assert re.search(default_pat, section_body, flags=re.M), (
            f"Default for `{key}` should be `{default}`"
        )


def test_merge_base_important_note_present_and_mentions_fetch_depth_zero():
    readme = _require_readme()
    note_match = re.search(
        r"###\s*`merge-base`[\s\S]+?>\s*\[\!IMPORTANT\][\s\S]+?fetch-depth:\s*0", readme, flags=re.I
    )
    assert note_match, "IMPORTANT note for `merge-base` with fetch-depth: 0 is missing"


def test_pr_comments_important_note_mentions_github_token_and_issue_77():
    readme = _require_readme()
    assert "### `pr-comments`" in readme
    assert re.search(
        r">\s*\[\!IMPORTANT\][\s\S]+GITHUB_TOKEN[\s\S]+#77", readme
    ), "IMPORTANT note for `pr-comments` should mention GITHUB_TOKEN and issue #77"


def test_used_by_section_contains_expected_orgs_and_structure():
    readme = _require_readme()
    # Simple checks for known org names and <img ... alt="...">
    expected_orgs = [
        ("Apache", "https://github.com/apache"),
        ("Texas Instruments", "https://github.com/TexasInstruments"),
        ("OpenCADC", "https://github.com/opencadc"),
        ("Extrawest", "https://github.com/extrawest"),
        ("Mila", "https://github.com/mila-iqia"),
        ("Chainlift", "https://github.com/Chainlift"),
    ]
    for alt, href in expected_orgs:
        assert alt in readme, f"Expected org '{alt}' not mentioned"
        assert href in readme, f"Expected org link '{href}' not present"
    # Check that avatars come from GitHub's avatars CDN
    assert re.search(
        r'src="https://avatars\.githubusercontent\.com/u/\d+\?s=200&v=4"', readme
    ), "Org avatar images should use githubusercontent avatars"


def test_badging_section_contains_markdown_and_rst_snippets():
    readme = _require_readme()
    # Markdown fenced snippet
    assert re.search(
        r"\[\!\[Commit Check\]\(https://github\.com/commit-check/commit-check-action/actions/workflows/commit-check\.yml/badge\.svg\)\]"
        r"\(https://github\.com/commit-check/commit-check-action/actions/workflows/commit-check\.yml\)",
        readme,
    ), "Markdown badge snippet missing or malformed"

    # reStructuredText snippet
    assert re.search(
        r"\.\. image:: https://github\.com/commit-check/commit-check-action/actions/workflows/commit-check\.yml/badge\.svg\s+"
        r":target: https://github\.com/commit-check/commit-check-action/actions/workflows/commit-check\.yml\s+"
        r":alt: Commit Check",
        readme,
    ), "reStructuredText badge snippet missing or malformed"


def test_versioning_and_feedback_sections_present_with_expected_links():
    readme = _require_readme()
    assert "Versioning follows" in readme and "Semantic Versioning" in readme
    # Feedback/issues link
    assert re.search(
        r"\[issues\]\(https://github\.com/commit-check/commit-check/issues\)", readme
    ), "Issues link in feedback section is missing"


def test_all_markdown_links_and_images_have_alt_text_or_label():
    readme = _require_readme()
    # Images must have alt text inside \![...]
    for m in re.finditer(r"\!\[(?P<alt>[^\]]*)\]\((?P<url>https?://[^)]+)\)", readme):
        alt = (m.group("alt") or "").strip()
        assert alt != "", f"Image missing alt text for URL {m.group('url')}"

    # Links should have non-empty labels
    for m in re.finditer(r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)]+)\)", readme):
        label = (m.group("label") or "").strip()
        assert label != "", f"Link missing label for URL {m.group('url')}"


def test_no_http_links_only_https():
    readme = _require_readme()
    http_links = re.findall(r"\((http://[^)]+)\)", readme)
    assert not http_links, f"Insecure http links found: {http_links}"


def test_job_summary_and_pr_comments_screenshots_referenced():
    readme = _require_readme()
    assert "screenshot/success-job-summary.png" in readme
    assert "screenshot/failure-job-summary.png" in readme
    assert "screenshot/success-pr-comments.png" in readme
    assert "screenshot/failure-pr-comments.png" in readme


# Edge cases / failure scenarios


def test_readme_is_not_empty_and_has_min_length():
    readme = _require_readme()
    assert len(readme.strip()) > 400, "README seems too short; expected a substantive document"


def test_usage_block_contains_both_checkout_and_action_steps_once_each():
    readme = _require_readme()
    usage_match = re.search(r"## Usage[\s\S]+?```yaml([\s\S]+?)```", readme, flags=re.I)
    assert usage_match, "Usage YAML block not found"
    yaml_text = usage_match.group(1)
    assert yaml_text.count("actions/checkout@v5") == 1, "Expected one checkout step"
    assert yaml_text.count("commit-check/commit-check-action@v1") == 1, "Expected one commit-check-action step"


def test_references_to_experimental_features_present():
    readme = _require_readme()
    assert re.search(r"`merge-base`.*experimental feature", readme, flags=re.I | re.S)
    assert re.search(r"`pr-comments`.*experimental feature", readme, flags=re.I | re.S)