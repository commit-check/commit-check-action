"""The Markdown report the job summary and the PR comment share."""

import importlib.metadata
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from helpers import (
    ANSI_CODES,
    HOOK_GUIDE,
    SHA_A,
    SHA_B,
    fail_scope,
    files_pass,
    files_scope,
    fix_scope,
    make_check,
    pass_scope,
    read_github_output,
    skip_scope,
    warn_scope,
)

import main

#: The report footer names the installed commit-check version, which differs
#: between a contributor's machine and CI. Golden tests pin it so they assert on
#: the report layout rather than on whatever version happens to be installed.
PINNED_VERSION = "2.13.1"

FOOTER = (
    f"<sub>Checked by [Commit Check](https://commit-check.com) {PINNED_VERSION} · "
    "[Rules](https://commit-check.com/rules/) · "
    f"[Check before you push]({HOOK_GUIDE})</sub>"
)

MARKS = "https://raw.githubusercontent.com/commit-check/.github/main/branding/status"


def heading(glyph: str, word: str, verdict: str) -> str:
    """The report's first visible line: the status tile and the verdict."""
    return (
        f'## <img alt="{glyph}" width="24" height="24" align="top" '
        f'src="{MARKS}/{word}.png"> Commit Check {verdict}'
    )


def group_line(glyph: str, word: str, text: str) -> str:
    """One group's line of the report: its line mark, name and result."""
    return (
        f'<img alt="{glyph}" width="16" height="16" align="absmiddle" '
        f'src="{MARKS}/line-{word}.png"> {text}'
    )


pin_version = patch("main._commit_check_version", new=lambda: PINNED_VERSION)

#: Fixed lines of the report's "How to fix" section.
AGENT_PROMPT = "Fix these Commit Check findings without changing any code:"

HOOK_HELP = (
    f"**Next time.** The [pre-commit hook]({HOOK_GUIDE}) runs the same "
    "rules before you push."
)


class TestRenderJobSummary(unittest.TestCase):
    @pin_version
    def test_success_golden_output(self):
        """Pin the full success report so the spec stays visible and exact."""
        results = [
            pass_scope("PR title", value="feat: add login page"),
            pass_scope("Commit 1/2", value="feat: add user auth"),
            pass_scope("Commit 2/2", value="fix: resolve timeout"),
            pass_scope("Branch", value="feature/add-login"),
        ]
        results[1].sha, results[2].sha = SHA_A, SHA_B
        body = main.render_report(results)
        self.assertEqual(
            body,
            f"{main.COMMENT_MARKER}\n"
            f"{heading('✔', 'passed', 'passed')}\n"
            "\n"
            f"{group_line('✔', 'passed', 'Pull request: passed')}<br>"
            f"{group_line('✔', 'passed', 'Commits: 2 passed')}<br>"
            f"{group_line('✔', 'passed', 'Branch: passed')}\n"
            "\n"
            "<details>\n"
            "<summary>All 4 checks</summary>\n"
            "\n"
            "```text\n"
            "Pull request\n"
            "  ✔ title        feat: add login page\n"
            "Commits\n"
            "  ✔ d87faca      feat: add user auth\n"
            "  ✔ 5584f46      fix: resolve timeout\n"
            "Branch\n"
            "  ✔ branch       feature/add-login\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    @pin_version
    def test_failure_golden_output(self):
        """Pin the full failure report: the diff of what failed, how to fix
        it, and every scope in the listing."""
        results = [
            pass_scope("PR title", value="feat: add login page"),
            fail_scope("Commit 2/2"),
            pass_scope("Branch", value="feature/add-login"),
        ]
        body = main.render_report(results)
        self.assertEqual(
            body,
            f"{main.COMMENT_MARKER}\n"
            f"{heading('✖', 'failed', 'failed')}\n"
            "\n"
            f"{group_line('✔', 'passed', 'Pull request: passed')}<br>"
            f"{group_line('✖', 'failed', 'Commits: failed')}<br>"
            f"{group_line('✔', 'passed', 'Branch: passed')}\n"
            "\n"
            "```diff\n"
            "@@ Commit 2/2 @@\n"
            "- bad message\n"
            "# Use <type>(<scope>): <description>\n"
            "```\n"
            "\n"
            "<sub>Rules: [CC001](https://commit-check.com/rules/#cc001)</sub>\n"
            "\n"
            "<details>\n"
            "<summary><b>How to fix</b></summary>\n"
            "\n"
            "**With an AI coding agent.** Paste this prompt:\n"
            "\n"
            "```text\n"
            f"{AGENT_PROMPT}\n"
            '- Commit 2/2 "bad message": Use <type>(<scope>): <description>\n'
            "```\n"
            "\n"
            "**Commit messages.** Reword it, then force-push:\n"
            "\n"
            "```bash\n"
            "git commit --amend\n"
            "git push --force-with-lease\n"
            "```\n"
            "\n"
            f"{HOOK_HELP}\n"
            "\n"
            "</details>\n"
            "\n"
            "<details>\n"
            "<summary>All 3 checks</summary>\n"
            "\n"
            "```text\n"
            "Pull request\n"
            "  ✔ title        feat: add login page\n"
            "Commits\n"
            "  ✖ 2/2          bad message\n"
            "      CC001 message\n"
            "        value: bad message\n"
            "        The commit message should follow Conventional Commits.\n"
            "        Suggest: Use <type>(<scope>): <description>\n"
            "Branch\n"
            "  ✔ branch       feature/add-login\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    @pin_version
    def test_failure_golden_output_with_commits(self):
        """Pin the report for a pull request's commits: each is named by its
        short hash, linked under the diff, and a mechanical fix is a green
        line."""
        results = [
            pass_scope("PR title", value="feat: Add login page"),
            fail_scope("Commit 1/3", sha=SHA_A),
            fix_scope("Commit 2/3", sha=SHA_B),
            pass_scope("Commit 3/3", value="fix: Handle timeout"),
            pass_scope("Branch", value="feature/add-login"),
        ]
        results[3].sha = "37d6def82ffcc6dbfa0af2ea1ec90512d89979ae"
        env = {"GITHUB_REPOSITORY": "acme/widgets", "GITHUB_SERVER_URL": ""}
        with patch.dict(os.environ, env):
            body = main.render_report(results)
        commits = "https://github.com/acme/widgets/commit"
        self.assertEqual(
            body,
            f"{main.COMMENT_MARKER}\n"
            f"{heading('✖', 'failed', 'failed')}\n"
            "\n"
            f"{group_line('✔', 'passed', 'Pull request: passed')}<br>"
            f"{group_line('✖', 'failed', 'Commits: 2 of 3 failed')}<br>"
            f"{group_line('✔', 'passed', 'Branch: passed')}\n"
            "\n"
            "```diff\n"
            "@@ d87faca @@\n"
            "- bad message\n"
            "# Use <type>(<scope>): <description>\n"
            "@@ 5584f46 @@\n"
            "- feat: add login page\n"
            "+ feat: Add login page\n"
            "```\n"
            "\n"
            f"<sub>Commits: [`d87faca`]({commits}/{SHA_A}) "
            f"[`5584f46`]({commits}/{SHA_B}) &nbsp;|&nbsp; "
            "Rules: [CC001](https://commit-check.com/rules/#cc001) · "
            "[CC002](https://commit-check.com/rules/#cc002)</sub>\n"
            "\n"
            "<details>\n"
            "<summary><b>How to fix</b></summary>\n"
            "\n"
            "**With an AI coding agent.** Paste this prompt:\n"
            "\n"
            "```text\n"
            f"{AGENT_PROMPT}\n"
            '- Commit d87faca "bad message": Use <type>(<scope>): <description>\n'
            '- Commit 5584f46: change "feat: add login page" to '
            '"feat: Add login page"\n'
            "```\n"
            "\n"
            "**Commit messages.** Reword them, then force-push:\n"
            "\n"
            "```bash\n"
            'git rebase -i d87faca~1   # change "pick" to "reword" on the '
            "commits above\n"
            "git push --force-with-lease\n"
            "```\n"
            "\n"
            f"{HOOK_HELP}\n"
            "\n"
            "</details>\n"
            "\n"
            "<details>\n"
            "<summary>All 5 checks</summary>\n"
            "\n"
            "```text\n"
            "Pull request\n"
            "  ✔ title        feat: Add login page\n"
            "Commits\n"
            "  ✖ d87faca      bad message\n"
            "      CC001 message\n"
            "        value: bad message\n"
            "        The commit message should follow Conventional Commits.\n"
            "        Suggest: Use <type>(<scope>): <description>\n"
            "  ✖ 5584f46      feat: add login page\n"
            "      CC002 subject-capitalized\n"
            "        value: feat: add login page\n"
            "        Subject must start with a capital letter\n"
            "        Fix: feat: Add login page\n"
            "  ✔ 37d6def      fix: Handle timeout\n"
            "Branch\n"
            "  ✔ branch       feature/add-login\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    def test_commit_link_honours_the_server_url(self):
        """GitHub Enterprise Server is not github.com."""
        env = {
            "GITHUB_REPOSITORY": "acme/widgets",
            "GITHUB_SERVER_URL": "https://ghe.example.com/",
        }
        with patch.dict(os.environ, env):
            body = main.render_report([fix_scope("Commit 1/1", sha=SHA_B)])
        self.assertIn(
            f"<sub>Commits: [`5584f46`](https://ghe.example.com/acme/widgets/commit/{SHA_B}) ",
            body,
        )

    def test_no_commit_link_without_a_repository_to_link_into(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}):
            body = main.render_report([fix_scope("Commit 1/1", sha=SHA_B)])
        self.assertIn("@@ 5584f46 @@\n- feat: add login page\n", body)
        self.assertIn("\n<sub>Rules: [CC002](", body)
        self.assertNotIn("/commit/", body)

    def test_scopes_without_a_commit_are_never_linked(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/widgets"}):
            body = main.render_report([fail_scope("Branch")])
        self.assertIn("@@ Branch @@\n- bad message\n", body)
        self.assertNotIn("/commit/", body)

    def test_an_author_scope_is_named_by_its_label_not_its_commit(self):
        """An author check runs on a commit, but the finding is the author."""
        scope = fail_scope("Author email", sha=SHA_A)
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/widgets"}):
            body = main.render_report([scope])
        self.assertIn("@@ Author email @@\n", body)
        self.assertIn("  ✖ email        bad message\n", body)
        self.assertNotIn("/commit/", body)

    def test_skipped_scope_carries_the_sha_too(self):
        scope = skip_scope("Commit 1/1")
        scope.sha = SHA_A
        self.assertIn("  ⊘ d87faca      skipped\n", main.render_report([scope]))

    def test_a_check_is_a_thing_checked_not_a_rule_evaluation(self):
        """The total counts scopes, so it tracks the policy, not the PR size.

        Counting rule evaluations made the denominator grow with the number of
        commits: sixteen messages against six enabled rules reported "1 of 100
        checks failed", which both overstated the work done and made one bad
        commit out of fifteen look negligible. Two rules failing on one commit
        is still one thing to go and fix.
        """
        two_failures = main.ScopeResult(
            label="Commit 1/2",
            checks=[
                make_check("message", status="fail", rule_id="CC001"),
                make_check("subject_min_length", status="fail", rule_id="CC005"),
            ],
        )
        body = main.render_report(
            [pass_scope("Branch"), two_failures, pass_scope("Commit 2/2")]
        )
        self.assertIn("> Commits: 1 of 2 failed", body)
        self.assertIn("<summary>All 3 checks</summary>", body)
        # Both failing rules are still named, under the diff and in the listing.
        self.assertIn("<sub>Rules: CC001 · CC005</sub>", body)
        self.assertIn("CC001 message", body)
        self.assertIn("CC005 subject-min-length", body)

    def test_total_does_not_grow_with_the_number_of_rules(self):
        """Adding rules to one scope must not change the totals."""
        one_rule = [pass_scope("Branch")]
        many_rules = [
            main.ScopeResult(
                label="Branch",
                checks=[
                    make_check(f"rule_{i}", rule_id=f"CC{i:03d}") for i in range(9)
                ],
            )
        ]
        for results in (one_rule, many_rules):
            body = main.render_report(results)
            self.assertIn("> Branch: passed\n", body)
            self.assertIn("<summary>All 1 check</summary>", body)

    def test_body_opens_with_hidden_marker(self):
        body = main.render_job_summary([pass_scope("Branch")])
        self.assertTrue(body.startswith(main.COMMENT_MARKER))

    def test_the_heading_is_the_verdict(self):
        """The mark carries the status, so no tick ever sits next to a failure."""
        cases = [
            ([pass_scope("Branch")], heading("✔", "passed", "passed")),
            ([fail_scope("Branch")], heading("✖", "failed", "failed")),
            ([warn_scope()], heading("⚠", "warned", "passed with warnings")),
            ([skip_scope("Branch")], heading("⊘", "skipped", "skipped")),
        ]
        for results, expected in cases:
            with self.subTest(expected=expected):
                lines = main.render_report(results).splitlines()
                self.assertEqual(lines[1], expected)

    def test_all_pass(self):
        body = main.render_job_summary([pass_scope("Branch", value="main")])
        self.assertIn("Commit Check passed\n", body)
        self.assertIn("<details>", body)
        self.assertIn("<summary>All 1 check</summary>", body)
        self.assertIn("```text", body)
        self.assertIn("\nBranch\n  ✔ branch       main\n", body)
        self.assertNotIn("```diff", body)
        self.assertNotIn("How to fix", body)

    def test_all_pass_groups_scopes_like_step_log(self):
        results = [
            pass_scope("PR title", value="feat: add login page"),
            pass_scope("Commit 1/2", value="feat: add user auth"),
            pass_scope("Commit 2/2", value="fix: resolve timeout"),
            pass_scope("Branch", value="feature/pr-12"),
            pass_scope("Author name", value="Jane Doe"),
            pass_scope("Author email", value="jane@example.com"),
        ]
        body = main.render_job_summary(results)
        listing = body.split("```text\n")[1]
        # Group headers in the listing mirror the step log ordering.
        self.assertLess(listing.index("Commits\n"), listing.index("Branch\n"))
        self.assertLess(listing.index("Branch\n"), listing.index("Author\n"))
        self.assertIn("  ✔ title        feat: add login page\n", listing)
        self.assertIn("  ✔ branch       feature/pr-12\n", listing)
        self.assertIn("  ✔ email        jane@example.com\n", listing)
        self.assertIn("> Author: 2 passed", body)

    def test_all_pass_truncates_long_values(self):
        long_value = "x" * 200
        body = main.render_job_summary([pass_scope("Commit 1/1", value=long_value)])
        self.assertIn(f"  ✔ 1/1          {'x' * 79}…\n", body)

    def test_all_pass_without_value_shows_plain_label(self):
        body = main.render_job_summary([pass_scope("Branch")])
        self.assertIn("  ✔ branch\n", body)

    def test_failure_shows_the_diff_and_the_rule_links(self):
        body = main.render_job_summary([fail_scope("Commit 1/1")])
        self.assertIn("Commit Check failed\n", body)
        self.assertIn("> Commits: failed\n", body)
        self.assertIn(
            "```diff\n"
            "@@ Commit 1/1 @@\n"
            "- bad message\n"
            "# Use <type>(<scope>): <description>\n"
            "```\n",
            body,
        )
        self.assertIn(
            "<sub>Rules: [CC001](https://commit-check.com/rules/#cc001)", body
        )
        self.assertIn("<summary>All 1 check</summary>", body)
        self.assertIn("  ✖ 1/1          bad message\n", body)
        self.assertIn("      CC001 message", body)
        self.assertIn("        value: bad message", body)
        self.assertIn("        Suggest: Use <type>(<scope>): <description>", body)
        self.assertIn("[Rules](https://commit-check.com/rules/)", body)

    def test_a_failing_value_is_printed_in_full(self):
        """It is the one value the reader has to act on; the cap is for the
        rows that passed."""
        subject = "feat: " + "x" * 100
        scope = main.ScopeResult(
            label="Commit 1/1",
            checks=[
                make_check(
                    "subject_max_length", status="fail", rule_id="CC004", value=subject
                )
            ],
        )
        body = main.render_report([scope])
        self.assertIn(f"- {subject}\n", body)
        self.assertIn(f"  ✖ 1/1          {subject}\n", body)

    def test_failure_details_show_all_scopes_and_values(self):
        results = [
            fail_scope("Commit 1/2"),
            pass_scope("Commit 2/2", value="fix: resolve timeout"),
        ]
        body = main.render_job_summary(results)
        self.assertIn("  ✔ 2/2          fix: resolve timeout\n", body)
        self.assertIn("  ✖ 1/2          bad message\n", body)

    def test_passing_scopes_stay_out_of_the_diff(self):
        body = main.render_job_summary([pass_scope("Branch"), fail_scope("Commit 1/1")])
        diff = body.split("```diff\n")[1].split("```")[0]
        self.assertEqual(diff.count("@@ "), 1)
        self.assertIn("@@ Commit 1/1 @@", diff)
        self.assertIn("  ✔ branch\n", body)


def ai_scope(label: str = "Commit 1/1", sha: str = SHA_A) -> main.ScopeResult:
    """A commit that credits an AI co-author, under ``ai_attribution = "forbid"``:
    the rule's value is the tool, and its fix the message without the trailer."""
    return main.ScopeResult(
        label=label,
        sha=sha,
        text=(
            "feat: add OAuth providers\n\n"
            "Co-authored-by: Claude <noreply@anthropic.com>\n"
        ),
        checks=[
            make_check(
                "ai_attribution",
                status="fail",
                rule_id="CC013",
                value="Claude Code",
                error="AI attribution is forbidden in this project",
                suggest="Remove the AI trailer lines and re-commit.",
                fix="feat: add OAuth providers",
                docs_url="https://commit-check.com/rules/#cc013",
            )
        ],
    )


class TestLineEdits(unittest.TestCase):
    """What a rule's fix removes and adds, which the diff shows red and green."""

    def test_a_subject_rule_is_compared_with_the_subject_alone(self):
        """Its fix is the subject, so the body is not something it removed."""
        scope = fix_scope()
        scope.text = "feat: add login page\n\nThe body stays.\n"
        self.assertEqual(
            main._line_edits(scope, scope.checks[0]),
            (["feat: add login page"], ["feat: Add login page"]),
        )

    def test_a_whole_message_fix_is_compared_with_the_whole_message(self):
        message = "feat: add login page\n\nThe body stays."
        scope = main.ScopeResult(
            label="Commit 1/1",
            text=message + "\n",
            checks=[
                make_check(
                    "require_signed_off_by",
                    status="fail",
                    rule_id="CC012",
                    value=message,
                    fix=f"{message}\n\nSigned-off-by: Jane Doe <jane@example.com>",
                )
            ],
        )
        self.assertEqual(
            main._line_edits(scope, scope.checks[0]),
            ([], ["Signed-off-by: Jane Doe <jane@example.com>"]),
        )

    def test_an_ai_rule_is_compared_with_the_message_not_with_its_value(self):
        """The value is the tool's name; the fix drops the trailer."""
        scope = ai_scope()
        self.assertEqual(
            main._line_edits(scope, scope.checks[0]),
            (["Co-authored-by: Claude <noreply@anthropic.com>"], []),
        )

    def test_an_ai_rule_without_the_message_has_no_line_to_name(self):
        scope = ai_scope()
        scope.text = ""
        self.assertEqual(main._line_edits(scope, scope.checks[0]), ([], []))
        self.assertEqual(
            main._instruction(scope, scope.checks[0]),
            "Remove the AI trailer lines and re-commit.",
        )

    def test_the_message_rule_supplies_the_text_outside_a_pull_request(self):
        """Nothing is piped in for the HEAD commit, but the message rule's
        value is the whole message."""
        scope = ai_scope()
        scope.checks.insert(0, make_check("message", value=scope.text.strip()))
        scope.text = ""
        self.assertEqual(
            main._line_edits(scope, scope.checks[1]),
            (["Co-authored-by: Claude <noreply@anthropic.com>"], []),
        )
        self.assertEqual(main._first_value(scope), "feat: add OAuth providers")

    def test_a_rule_without_a_fix_has_no_edits(self):
        scope = fail_scope()
        self.assertEqual(main._line_edits(scope, scope.checks[0]), ([], []))

    def test_a_fix_with_nothing_to_compare_it_with_has_no_edits(self):
        scope = main.ScopeResult(
            label="Branch", checks=[make_check("branch", status="fail", fix="x")]
        )
        self.assertEqual(main._line_edits(scope, scope.checks[0]), ([], []))

    def test_recorded_edits_win_over_the_fix(self):
        """A description is too long to carry as a fix, so its line changes
        are recorded when it is checked."""
        scope = fail_scope("PR description")
        scope.edits["CC001"] = (["Generated with Claude Code"], [])
        self.assertEqual(
            main._line_edits(scope, scope.checks[0]),
            (["Generated with Claude Code"], []),
        )

    def test_instruction_names_each_kind_of_change(self):
        scope = fail_scope()
        check = scope.checks[0]
        cases = [
            ((["a"], ["b"]), 'change "a" to "b"'),
            ((["a", "b"], []), 'remove "a"; "b"'),
            (([], ["c"]), 'add "c"'),
            ((["a", "b"], ["c"]), 'remove "a"; "b", and add "c"'),
        ]
        for edits, expected in cases:
            with self.subTest(expected=expected):
                scope.edits["CC001"] = edits
                self.assertEqual(main._instruction(scope, check), expected)

    def test_advice_drops_what_only_the_rule_page_needs(self):
        cases = {
            "Use <type>(<scope>): <description>, where <type> is one of: feat, fix": (
                "Use <type>(<scope>): <description>"
            ),
            "Use <type>/<description> with an allowed type, or add the branch "
            "to allow_branch_names in config": (
                "Use <type>/<description> with an allowed type"
            ),
            'In the description, remove "x"': 'remove "x"',
            "Add a body\nexplaining the change": "Add a body",
        }
        for suggest, expected in cases.items():
            with self.subTest(suggest=suggest):
                check = make_check("message", status="fail", suggest=suggest)
                self.assertEqual(main._advice(check), expected)

    def test_advice_falls_back_to_the_error_without_its_link(self):
        check = make_check(
            "message",
            status="fail",
            error="Follow Conventional Commits. See https://www.conventionalcommits.org",
        )
        self.assertEqual(main._advice(check), "Follow Conventional Commits.")


class TestDiffBlock(unittest.TestCase):
    def _hunks(self, results, status="fail"):
        lines = main._diff_block(results, status)
        return lines[1 : lines.index("```")]

    def test_a_fix_on_another_line_leaves_the_subject_as_context(self):
        self.assertEqual(
            self._hunks([ai_scope()]),
            [
                "@@ d87faca @@",
                "  feat: add OAuth providers",
                "- Co-authored-by: Claude <noreply@anthropic.com>",
            ],
        )

    def test_advice_is_about_the_value_so_the_value_is_red(self):
        scope = fail_scope("Commit 1/1", sha=SHA_A)
        scope.checks.append(
            make_check(
                "subject_min_length",
                status="fail",
                rule_id="CC005",
                value="bad message",
                suggest="Write a subject of at least 20 characters",
            )
        )
        self.assertEqual(
            self._hunks([scope]),
            [
                "@@ d87faca @@",
                "- bad message",
                "# Use <type>(<scope>): <description>",
                "# Write a subject of at least 20 characters",
            ],
        )

    def test_an_ai_author_leaves_the_subject_as_context(self):
        # CC017 is about who made the commit, not what its subject says.
        scope = main.ScopeResult(
            label="Commit 1/1",
            sha=SHA_A,
            text="fix: handle the empty case\n",
            checks=[
                make_check(
                    "message", "pass", "CC001", value="fix: handle the empty case"
                ),
                make_check(
                    "ai_author",
                    "fail",
                    "CC017",
                    value="Claude <noreply@anthropic.com>",
                    suggest="Commit as yourself",
                ),
            ],
        )
        self.assertEqual(
            self._hunks([scope]),
            ["@@ d87faca @@", "  fix: handle the empty case", "# Commit as yourself"],
        )

    def test_an_ai_rules_value_never_stands_in_for_the_subject(self):
        # An empty message has no subject; the author CC017 reports is not one.
        scope = main.ScopeResult(
            label="Commit 1/1",
            sha=SHA_A,
            checks=[
                make_check("message", "pass", "CC001"),
                make_check(
                    "ai_author", "pass", "CC017", value="Jane <jane@example.com>"
                ),
            ],
        )
        self.assertEqual(main._first_value(scope), "")
        self.assertEqual(main._render_scopes([scope], False), ["  ✔ d87faca"])

    def test_two_rules_that_ask_for_the_same_line_show_it_once(self):
        scope = ai_scope()
        scope.checks.append({**scope.checks[0], "rule_id": "CC015"})
        self.assertEqual(len(self._hunks([scope])), 3)

    def test_a_description_is_named_by_its_label_and_shows_only_the_lines(self):
        scope = fail_scope("PR description")
        scope.text = "Adds login.\n\nGenerated with Claude Code\n"
        scope.edits["CC001"] = (["Generated with Claude Code"], [])
        self.assertEqual(
            self._hunks([scope]),
            ["@@ PR description @@", "- Generated with Claude Code"],
        )

    def test_a_long_list_is_cut_and_the_rest_folded(self):
        results = [fail_scope(f"Commit {n}/8", sha=f"{n}" * 40) for n in range(1, 9)]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/widgets"}):
            lines = main._diff_block(results)
        body = "\n".join(lines)
        shown, folded = body.split("<details>")
        self.assertEqual(shown.count("@@ "), main.MAX_HUNKS)
        self.assertIn("<summary>and 3 more</summary>\n\n```diff\n@@ 6666666 @@", folded)
        self.assertEqual(folded.count("@@ "), 3)
        # Only the commits in view are linked; the rule is named once.
        links = lines[-1]
        self.assertEqual(links.count("/commit/"), main.MAX_HUNKS)
        self.assertNotIn("6666666", links)
        self.assertEqual(links.count("[CC001]"), 1)

    def test_one_or_two_leftovers_are_not_worth_a_fold(self):
        results = [fail_scope(f"Commit {n}/7", sha=f"{n}" * 40) for n in range(1, 8)]
        body = "\n".join(main._diff_block(results))
        self.assertEqual(body.count("@@ "), 7)
        self.assertNotIn("<details>", body)

    def test_warnings_get_the_same_block(self):
        self.assertEqual(
            self._hunks([warn_scope(), fail_scope()], "warn"),
            ["@@ Branch @@", "- jsmith/fix-x", "# Use <type>/<description>"],
        )


class TestHowToFix(unittest.TestCase):
    def _body(self, results):
        return "\n".join(main._how_to_fix(results))

    def test_nothing_failed_nothing_to_fix(self):
        self.assertEqual(main._how_to_fix([pass_scope(), warn_scope()]), [])

    def test_the_prompt_lists_every_failing_scope_with_its_instruction(self):
        results = [
            fail_scope("PR title"),
            ai_scope("Commit 1/2"),
            fix_scope("Commit 2/2"),
            pass_scope("Branch"),
        ]
        self.assertIn(
            "```text\n"
            f"{AGENT_PROMPT}\n"
            '- PR title "bad message": Use <type>(<scope>): <description>\n'
            '- Commit d87faca "feat: add OAuth providers": remove '
            '"Co-authored-by: Claude <noreply@anthropic.com>"\n'
            '- Commit 5584f46: change "feat: add login page" to '
            '"feat: Add login page"\n'
            "```\n",
            self._body(results),
        )

    def test_only_the_last_commit_failing_is_an_amend(self):
        for scope in (
            fail_scope("Commit 3/3", sha=SHA_A),
            fail_scope("Commit message"),
        ):
            with self.subTest(label=scope.label):
                body = self._body([scope])
                self.assertIn("Reword it, then force-push:", body)
                self.assertIn("```bash\ngit commit --amend\ngit push --force", body)
                self.assertNotIn("rebase", body)

    def test_an_earlier_commit_needs_a_rebase_from_its_parent(self):
        body = self._body(
            [fail_scope("Commit 2/3", sha=SHA_B), pass_scope("Commit 3/3")]
        )
        self.assertIn("```bash\ngit rebase -i 5584f46~1   # change", body)

    def test_a_commit_without_a_hash_gets_a_placeholder(self):
        body = self._body([fail_scope("Commit 1/3"), fail_scope("Commit 2/3")])
        self.assertIn("Reword them, then force-push:", body)
        self.assertIn("git rebase -i <first failing commit>~1   # change", body)

    def test_each_kind_of_failure_gets_its_own_paragraph(self):
        body = self._body(
            [
                fail_scope("PR title"),
                fail_scope("PR description"),
                fail_scope("Author email", sha=SHA_A),
                fail_scope("Branch"),
            ]
        )
        self.assertNotIn("**Commit messages.**", body)
        self.assertIn("**Author.** Set your git identity", body)
        self.assertIn(
            "**Branch name.** Rename the branch and push it. A pull request cannot "
            "change its branch, so open a new one from the renamed branch:\n\n"
            "```bash\ngit branch -m <type>/<description>\ngit push -u origin HEAD\n```",
            body,
        )
        self.assertIn(
            "**Pull request title and description.** Edit them on the pull "
            "request page.",
            body,
        )
        self.assertTrue(body.endswith(f"{HOOK_HELP}\n\n</details>"))

    def test_the_branch_command_uses_the_corrected_name_when_there_is_one(self):
        scope = fail_scope("Branch")
        scope.checks[0].update(value="Feature/Add-Login", fix="feature/Add-Login")
        self.assertIn(
            "```bash\ngit branch -m feature/Add-Login\ngit push -u origin HEAD\n```",
            self._body([scope]),
        )

    def test_one_pull_request_field_is_it_not_them(self):
        body = self._body([fail_scope("PR title")])
        self.assertIn("**Pull request title.** Edit it on the pull request page.", body)
        self.assertNotIn("**Branch name.**", body)
        self.assertNotIn("**Author.**", body)


class TestRenderPrComment(unittest.TestCase):
    def test_all_pass_matches_job_summary(self):
        comment = main.render_pr_comment([pass_scope("Branch")])
        summary = main.render_job_summary([pass_scope("Branch")])
        self.assertEqual(comment, summary)
        self.assertTrue(comment.startswith(main.COMMENT_MARKER))
        self.assertIn("Commit Check passed\n", comment)

    def test_failure_matches_job_summary(self):
        comment = main.render_pr_comment([fail_scope("Commit 1/1")])
        summary = main.render_job_summary([fail_scope("Commit 1/1")])
        self.assertEqual(comment, summary)
        self.assertTrue(comment.startswith(main.COMMENT_MARKER))
        self.assertIn("Commit Check failed\n", comment)
        self.assertIn("```diff\n@@ Commit 1/1 @@\n", comment)


class TestPrCommentSizeLimit(unittest.TestCase):
    """A report too long for a comment gives up its longest parts first."""

    RUN_URL = "https://github.com/o/r/actions/runs/42"

    def setUp(self):
        self.RUN = {
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_ID": "42",
            "GITHUB_SERVER_URL": "https://github.com",
        }

    def _failing_commits(self, count: int) -> list[main.ScopeResult]:
        body = "\n".join(
            f"Line {i} of a body that explains the change." for i in range(8)
        )
        return [
            main.ScopeResult(
                label=f"Commit {i}/{count}",
                sha=f"{i:040x}",
                checks=[
                    make_check(
                        "require_signed_off_by",
                        "fail",
                        "CC012",
                        value=f"Update module {i}\n\n{body}",
                        error="Signed-off-by trailer not found in the commit message",
                        fix=f"Update module {i}\n\n{body}\n\nSigned-off-by: A <a@b.c>",
                    )
                ],
            )
            for i in range(1, count + 1)
        ]

    def test_a_report_that_fits_is_the_job_summary(self):
        results = self._failing_commits(3)
        self.assertEqual(
            main.render_pr_comment(results), main.render_job_summary(results)
        )

    def test_dozens_of_failing_commits_still_fit(self):
        results = self._failing_commits(60)
        self.assertGreater(len(main.render_job_summary(results)), main.COMMENT_LIMIT)
        with (
            patch.dict(os.environ, self.RUN),
            patch("main.JOB_SUMMARY_ENABLED", True),
            patch("main.GITHUB_STEP_SUMMARY", "/tmp/summary.md"),
        ):
            comment = main.render_pr_comment(results)
        self.assertLessEqual(main._comment_length(comment), main.COMMENT_LIMIT)
        self.assertTrue(comment.startswith(main.COMMENT_MARKER))
        self.assertIn("Commit Check failed", comment)
        self.assertNotIn("<summary>All 60 checks</summary>", comment)
        self.assertIn(
            f"All 60 checks are in the [job summary]({self.RUN_URL}) of this run.",
            comment,
        )

    def test_the_diff_goes_too_when_the_listing_was_not_enough(self):
        results = self._failing_commits(2)
        with (
            patch.dict(os.environ, self.RUN),
            patch("main.JOB_SUMMARY_ENABLED", False),
        ):
            short = main.render_report(results, "short")
            with patch("main.COMMENT_LIMIT", len(short) - 1):
                comment = main.render_pr_comment(results)
        self.assertNotIn("```diff", comment)
        self.assertNotIn("How to fix</b>", comment)
        self.assertIn(
            "The findings and how to fix them are in the "
            f"[step log]({self.RUN_URL}) of this run.",
            comment,
        )
        self.assertIn("Commits: 2 of 2 failed", comment)

    def test_a_passing_headline_names_no_findings(self):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_RUN_ID"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("main.JOB_SUMMARY_ENABLED", False),
        ):
            body = main.render_report([pass_scope("Branch")], "headline")
        self.assertIn("All checks are in the step log of this run.", body)

    def test_length_is_counted_as_github_counts_it(self):
        self.assertEqual(main._comment_length("a🤖"), 3)


class TestUnparsableScopeInTheReport(unittest.TestCase):
    """A CLI response that is not JSON is a failure with no rule to link."""

    def test_the_diff_points_at_the_step_log(self):
        scope = main.ScopeResult(label="Branch", raw_text="Error: cchk.toml: oops")
        body = main.render_report([scope])
        self.assertIn("Commit Check failed\n", body)
        self.assertIn(
            "```diff\n"
            "@@ Branch @@\n"
            "# the output could not be parsed, see the step log\n"
            "```\n",
            body,
        )
        # No rule spoke, so there is none to link and nothing to instruct.
        self.assertNotIn("<sub>Rules", body)
        self.assertNotIn("How to fix", body)
        # The listing is where the raw output went.
        self.assertIn("  ✖ branch\n      Error: cchk.toml: oops\n", body)

    def test_it_never_reaches_the_warnings(self):
        scope = main.ScopeResult(label="Branch", raw_text="garbage")
        self.assertNotIn("**Warnings**", main.render_report([scope]))
        self.assertEqual(main._diff_block([scope], "warn")[:2], ["```diff", "```"])


class TestReportFooter(unittest.TestCase):
    def test_names_the_installed_commit_check(self):
        with patch("importlib.metadata.version", return_value="9.8.7") as version:
            self.assertEqual(main._commit_check_version(), "9.8.7")
            self.assertEqual(
                main._report_footer(),
                "<sub>Checked by [Commit Check](https://commit-check.com) 9.8.7 · "
                "[Rules](https://commit-check.com/rules/) · "
                f"[Check before you push]({HOOK_GUIDE})</sub>",
            )
        version.assert_called_with("commit-check")

    def test_without_a_version_it_still_links_the_rules(self):
        """The footer is decoration: an unknown version drops out of it."""
        missing = importlib.metadata.PackageNotFoundError("commit-check")
        with patch("importlib.metadata.version", side_effect=missing):
            self.assertEqual(main._commit_check_version(), "")
            self.assertEqual(
                main._report_footer(),
                "<sub>Checked by [Commit Check](https://commit-check.com) · "
                "[Rules](https://commit-check.com/rules/) · "
                f"[Check before you push]({HOOK_GUIDE})</sub>",
            )


class TestSkippedScopes(unittest.TestCase):
    """A skipped scope must never render as a passing one.

    commit-check-action#258 is the case this guards: every rule was skipped
    because the author is dependabot[bot], and the report announced
    "All 5 checks passed" over five green ticks.
    """

    def test_scope_status_is_skip_not_pass(self):
        self.assertEqual(skip_scope().status, "skip")

    def test_one_real_verdict_outranks_the_skips(self):
        """A scope only counts as skipped when nothing in it ran."""
        mixed = main.ScopeResult(
            label="PR title",
            checks=[
                make_check("message", status="skip", value=""),
                make_check("subject_max_length", status="pass", value="feat: x"),
            ],
        )
        self.assertEqual(mixed.status, "pass")

    @pin_version
    def test_all_skipped_golden_output(self):
        """Pin the fully skipped report: no ✔, no pass claim."""
        results = [skip_scope("PR title"), skip_scope("Commit 1/1")]
        body = main.render_report(results)
        self.assertEqual(
            body,
            f"{main.COMMENT_MARKER}\n"
            f"{heading('⊘', 'skipped', 'skipped')}\n"
            "\n"
            "Nothing was validated.\n"
            "\n"
            f"{group_line('⊘', 'skipped', 'Pull request: skipped')}<br>"
            f"{group_line('⊘', 'skipped', 'Commits: skipped')}\n"
            "\n"
            "<details>\n"
            "<summary>All 2 checks</summary>\n"
            "\n"
            "```text\n"
            "Pull request\n"
            "  ⊘ title        skipped\n"
            "Commits\n"
            "  ⊘ 1/1          skipped\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    def test_partial_skip_does_not_claim_all_passed(self):
        results = [pass_scope("Branch", value="main"), skip_scope("PR title")]
        results.append(pass_scope("PR description"))
        body = main.render_report(results)
        self.assertIn("Commit Check passed\n", body)
        self.assertIn("> Branch: passed<br>", body)
        # Two scopes in the group, one of which ran: never "2 passed".
        self.assertIn("> Pull request: 1 passed, 1 skipped\n", body)

    def test_failure_still_wins_over_skips(self):
        results = [fail_scope("Commit 1/1"), skip_scope("PR title")]
        body = main.render_report(results)
        self.assertIn("Commit Check failed\n", body)
        self.assertIn("> Commits: failed<br>", body)
        self.assertIn("> Pull request: skipped\n", body)

    def test_skipped_scope_reports_no_checked_value(self):
        """The ⊘ line carries no value, because nothing was examined."""
        body = main.render_report([skip_scope("PR title")])
        self.assertIn("  ⊘ title        skipped\n", body)

    def test_skip_reason_is_shown_on_the_scope_and_under_the_verdict(self):
        """A skip says why, so a deliberate bypass reads as one."""
        commit = "author dependabot[bot] is in [commit].ignore_authors"
        branch = "author dependabot[bot] is in [branch].ignore_authors"
        results = [
            skip_scope("PR title", reason=commit),
            skip_scope("Commit 1/1", reason=commit),
            skip_scope("Branch", reason=branch),
        ]
        body = main.render_report(results)
        self.assertIn("Commit Check skipped\n\nNothing was validated.\n", body)
        self.assertIn(
            "> Branch: skipped\n\n"
            "Skipped because author dependabot[bot] is in ignore_authors.\n",
            body,
        )
        self.assertIn(f"  ⊘ title        skipped: {commit}\n", body)
        self.assertIn(f"  ⊘ branch       skipped: {branch}\n", body)

    def test_skip_reason_follows_a_partial_skip_verdict(self):
        reason = "author dependabot[bot] is in [branch].ignore_authors"
        results = [pass_scope("PR title"), skip_scope("Branch", reason=reason)]
        body = main.render_report(results)
        self.assertIn(
            "> Branch: skipped\n\n"
            "Skipped because author dependabot[bot] is in ignore_authors.\n",
            body,
        )

    def test_skip_reason_reaches_the_step_log(self):
        reason = "author dependabot[bot] is in [commit].ignore_authors"
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            main.render_step_log([skip_scope("PR title", reason=reason)])
        self.assertIn("::group::⊘ Pull request: skipped\n", out.getvalue())
        self.assertIn(
            f"  ⊘ title        skipped: {reason}\n", ANSI_CODES.sub("", out.getvalue())
        )
        self.assertIn(
            "Skipped because author dependabot[bot] is in ignore_authors.",
            out.getvalue(),
        )

    def test_skip_reason_follows_a_partial_skip_in_the_step_log(self):
        reason = "author dependabot[bot] is in [branch].ignore_authors"
        results = [pass_scope("PR title"), skip_scope("Branch", reason=reason)]
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            main.render_step_log(results)
        self.assertIn(
            "Skipped because author dependabot[bot] is in ignore_authors.",
            out.getvalue(),
        )

    def test_a_passing_scope_has_no_skip_reason(self):
        self.assertEqual(pass_scope("Branch", value="main").skip_reason, "")

    def test_older_engine_without_skip_is_unaffected(self):
        """Back-compat: engines that only emit pass/fail render as before."""
        results = [pass_scope("Branch", value="main")]
        self.assertIn("Commit Check passed\n", main.render_report(results))


class TestWarnedScopes(unittest.TestCase):
    """A rule listed under the config's top-level ``warn`` is reported in
    full, but it must never read as a failure — or disappear like a pass."""

    def test_scope_status_is_warn_not_fail_or_pass(self):
        self.assertEqual(warn_scope().status, "warn")

    def test_a_real_failure_outranks_a_warning_in_the_same_scope(self):
        """CC2xx groups both branch and merge_base; the two can disagree."""
        mixed = main.ScopeResult(
            label="Branch",
            checks=[
                make_check("branch", status="warn", rule_id="CC201"),
                make_check("merge_base", status="fail", rule_id="CC202"),
            ],
        )
        self.assertEqual(mixed.status, "fail")

    def test_a_warning_survives_reporting_on_a_scope_that_also_fails(self):
        """The scope's status names its worst outcome, but both findings
        still belong on every surface: the count, both diffs, and the
        listing. Filtering any of those on ``scope.status == "warn"`` drops
        this scope's warning the moment its sibling rule fails.
        """
        mixed = main.ScopeResult(
            label="Branch",
            checks=[
                make_check(
                    "branch",
                    status="warn",
                    rule_id="CC201",
                    value="jsmith/fix-x",
                    error="The branch should follow Conventional Branch.",
                    suggest="Use <type>/<description>",
                    docs_url="https://commit-check.com/rules/#cc201",
                ),
                make_check(
                    "merge_base",
                    status="fail",
                    rule_id="CC202",
                    value="jsmith/fix-x",
                    error="Current branch is not rebased onto main.",
                    docs_url="https://commit-check.com/rules/#cc202",
                ),
            ],
        )
        self.assertEqual(main._warn_count([mixed]), 1)

        body = main.render_report([mixed])
        self.assertIn("Commit Check failed\n", body)
        failed, warned = body.split("**Warnings**")
        self.assertIn(
            "```diff\n"
            "@@ Branch @@\n"
            "- jsmith/fix-x\n"
            "# Current branch is not rebased onto main.\n"
            "```\n"
            "\n"
            "<sub>Rules: [CC202](https://commit-check.com/rules/#cc202)</sub>\n",
            failed,
        )
        self.assertIn(
            "```diff\n"
            "@@ Branch @@\n"
            "- jsmith/fix-x\n"
            "# Use <type>/<description>\n"
            "```\n"
            "\n"
            "<sub>Rules: [CC201](https://commit-check.com/rules/#cc201)</sub>\n",
            warned,
        )
        # One row, marked with the worse of the two, and both findings under
        # it; the warning says that it is one.
        self.assertIn("  ✖ branch       jsmith/fix-x\n      CC202 merge-base\n", body)
        self.assertIn("\n      CC201 branch (warning)\n", body)
        self.assertNotIn("⚠ branch", body)

    def test_step_log_and_report_agree_on_a_mixed_scope(self):
        """The step log's ::warning annotation must not be the only surface
        that shows this scope's warning — the report has to as well."""
        mixed = main.ScopeResult(
            label="Branch",
            checks=[
                make_check("branch", status="warn", rule_id="CC201", error="e1"),
                make_check("merge_base", status="fail", rule_id="CC202", error="e2"),
            ],
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.render_step_log([mixed])
        step_log = buf.getvalue()
        self.assertIn("::warning title=CC201 branch::", step_log)
        self.assertIn("::error title=CC202 merge-base::", step_log)

        body = main.render_report([mixed])
        self.assertIn("CC201 branch", body)
        self.assertIn("CC202 merge-base", body)

    def test_a_warning_outranks_a_skip_in_the_same_scope(self):
        mixed = main.ScopeResult(
            label="Author",
            checks=[
                make_check("author_name", status="skip"),
                make_check("author_email", status="warn", rule_id="CC102"),
            ],
        )
        self.assertEqual(mixed.status, "warn")

    def test_warnings_property_lists_only_the_warned_checks(self):
        mixed = main.ScopeResult(
            label="Branch",
            checks=[
                make_check("branch", status="warn", rule_id="CC201"),
                make_check("merge_base", status="pass", rule_id="CC202"),
            ],
        )
        self.assertEqual([c["rule_id"] for c in mixed.warnings], ["CC201"])

    def test_overall_status_is_warn_but_the_exit_code_is_still_zero(self):
        """``warn`` is for downstream steps; it must never fail the job."""
        results = [pass_scope("PR title"), warn_scope()]
        self.assertEqual(main.overall_status(results), "warn")
        self.assertEqual(main.exit_code_for(results), 0)

    def test_a_warning_outranks_a_skip_but_not_an_all_skipped_run(self):
        self.assertEqual(main.overall_status([warn_scope(), skip_scope()]), "warn")
        self.assertEqual(main.overall_status([skip_scope()]), "skip")

    def test_result_output_reports_warn(self):
        output_path = os.path.join(tempfile.mkdtemp(), "output.txt")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
            main.set_result_output([pass_scope("PR title"), warn_scope()])
        payload = json.loads(read_github_output(output_path)["result"])
        self.assertEqual(payload["status"], "warn")
        self.assertEqual([s["status"] for s in payload["scopes"]], ["pass", "warn"])

    def test_a_real_failure_still_fails_the_run_alongside_a_warning(self):
        results = [fail_scope("Commit 1/1"), warn_scope()]
        self.assertEqual(main.overall_status(results), "fail")
        self.assertEqual(main.exit_code_for(results), 1)

    @pin_version
    def test_warning_only_golden_output(self):
        """Pin the warning report: passes, but the finding is fully visible."""
        results = [
            pass_scope("PR title", value="feat: add login page"),
            warn_scope("Branch"),
        ]
        body = main.render_report(results)
        self.assertEqual(
            body,
            f"{main.COMMENT_MARKER}\n"
            f"{heading('⚠', 'warned', 'passed with warnings')}\n"
            "\n"
            f"{group_line('✔', 'passed', 'Pull request: passed')}<br>"
            f"{group_line('⚠', 'warned', 'Branch: 1 warning')}\n"
            "\n"
            "**Warnings**\n"
            "\n"
            "```diff\n"
            "@@ Branch @@\n"
            "- jsmith/fix-x\n"
            "# Use <type>/<description>\n"
            "```\n"
            "\n"
            "<sub>Rules: [CC201](https://commit-check.com/rules/#cc201)</sub>\n"
            "\n"
            "<details>\n"
            "<summary>All 2 checks</summary>\n"
            "\n"
            "```text\n"
            "Pull request\n"
            "  ✔ title        feat: add login page\n"
            "Branch\n"
            "  ⚠ branch       jsmith/fix-x\n"
            "      CC201 branch (warning)\n"
            "        value: jsmith/fix-x\n"
            "        The branch should follow Conventional Branch.\n"
            "        Suggest: Use <type>/<description>\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    def test_warning_and_skip_each_get_their_own_group_line(self):
        results = [pass_scope("PR title"), warn_scope(), skip_scope("Author name")]
        body = main.render_report(results)
        self.assertIn("Commit Check passed with warnings\n", body)
        self.assertIn(
            f"{group_line('✔', 'passed', 'Pull request: passed')}<br>"
            f"{group_line('⚠', 'warned', 'Branch: 1 warning')}<br>"
            f"{group_line('⊘', 'skipped', 'Author: skipped')}\n",
            body,
        )

    def test_a_failure_still_fails_and_names_the_warning_too(self):
        results = [fail_scope("Commit 1/1"), warn_scope()]
        body = main.render_report(results)
        self.assertIn("Commit Check failed\n", body)
        self.assertEqual(body.count("```diff\n"), 2)
        # The failures come first, the warnings under their own heading, and
        # only the failures get instructions.
        self.assertLess(body.index("@@ Commit 1/1 @@"), body.index("**Warnings**"))
        self.assertLess(body.index("**Warnings**"), body.index("@@ Branch @@"))
        self.assertLess(body.index("@@ Branch @@"), body.index("How to fix"))
        self.assertNotIn("Branch name.", body)

    def test_step_log_prints_a_warning_annotation_not_an_error(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.render_step_log([warn_scope()])
        out = buf.getvalue()
        self.assertIn(
            '::warning title=CC201 branch::Branch "jsmith/fix-x": '
            "Use <type>/<description>%0AThe branch should ",
            out,
        )
        self.assertNotIn("::error", out)
        self.assertIn("::group::⚠ Branch: 1 warning\n", out)
        self.assertIn("  \x1b[33m⚠\x1b[0m branch       jsmith/fix-x\n", out)
        self.assertNotIn("How to fix", out)

    def test_step_log_friendly_line_mentions_the_warning(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.render_step_log([pass_scope("PR title"), warn_scope()])
        out = buf.getvalue()
        self.assertIn("1 of 2 checks passed, 1 warning", out)
        self.assertNotIn("all checks passed", out)


class TestMarkdownEscaping(unittest.TestCase):
    """User text in the report must not break the Markdown that carries it.

    Commit subjects quote code, branch names and author names may contain
    pipes; every value lands in a fenced block, and the fence has to survive
    both. The checks below come from a real ``commit-check --format json``
    run on the subject ``fix: handle `None` | retry`` (all rules pass on it,
    so the CC001 outcome is flipped to make it a finding).
    """

    def _scope(self, value: str) -> main.ScopeResult:
        return main.ScopeResult(
            label="Commit 1/1",
            sha=SHA_B,
            checks=[
                make_check(
                    "message",
                    status="fail",
                    rule_id="CC001",
                    value=value,
                    error="The commit message should follow Conventional Commits.",
                    docs_url="https://commit-check.com/rules/#cc001",
                ),
                make_check("subject_imperative", rule_id="CC003", value=value),
            ],
        )

    def test_diff_fence_outgrows_a_triple_backtick_in_the_subject(self):
        diff = main._diff_block([self._scope("docs: show ``` usage")])
        self.assertEqual(diff[0], "````diff")
        self.assertEqual(diff[2], "- docs: show ``` usage")
        self.assertEqual(diff[4], "````")

    def test_prompt_fence_outgrows_a_triple_backtick_in_the_subject(self):
        how = main._how_to_fix([self._scope("docs: show ``` usage")])
        self.assertIn("````text", how)
        self.assertEqual(how.count("````"), 1)
        # The commands quote nothing of the user's, so their fence stays.
        self.assertIn("```bash", how)

    def test_a_subject_with_a_pipe_or_backticks_is_shown_as_it_is(self):
        """Every place a value lands is a code block, so nothing is escaped."""
        subject = "fix: handle `None` | retry"
        body = main.render_report([self._scope(subject)])
        self.assertIn(f"- {subject}\n", body)
        self.assertIn(f"  ✖ 5584f46      {subject}\n", body)

    def test_capped_value_with_an_unbalanced_backtick_stays_in_its_block(self):
        """The cap can cut inside a backtick run."""
        value = "fix: `" + "x " * 60 + "`"
        scope = pass_scope("Commit 1/1", value=value)
        capped = main._scope_value(scope)
        self.assertTrue(capped.endswith("x…"), capped)  # cut mid-span
        listing = main._all_checks([scope])
        self.assertEqual(listing[3], "```text")
        self.assertEqual(listing[-3], "```")

    def test_details_fence_outgrows_a_triple_backtick_in_the_value(self):
        details = "\n".join(main._all_checks([self._scope("docs: show ``` usage")]))
        lines = details.splitlines()
        self.assertEqual(lines[3], "````text")
        self.assertEqual(lines[-3], "````")
        self.assertIn("value: docs: show ``` usage", details)

    def test_details_fence_stays_three_backticks_for_ordinary_values(self):
        lines = main._all_checks([pass_scope(value="feat: add `login`")])
        self.assertEqual(lines[3], "```text")
        self.assertEqual(lines[-3], "```")


class TestSkipRenderingEdgeCases(unittest.TestCase):
    def test_the_diff_omits_skipped_scopes(self):
        """A skipped scope has no failed checks, so it must not get a hunk:
        a blank accusation among what failed otherwise."""
        diff = main._diff_block([fail_scope("Commit 1/1"), skip_scope("PR title")])
        self.assertEqual(
            diff[:5],
            [
                "```diff",
                "@@ Commit 1/1 @@",
                "- bad message",
                "# Use <type>(<scope>): <description>",
                "```",
            ],
        )

    def test_empty_result_set_is_not_reported_as_all_skipped(self):
        """`skipped == total` is trivially true for no scopes at all."""
        body = main.render_report([])
        self.assertNotIn("skipped", body)
        self.assertIn("Commit Check passed\n\n<details>\n", body)
        self.assertIn("<summary>Details</summary>", body)

    def test_step_log_partial_skip_does_not_claim_all_passed(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.render_step_log([pass_scope(), skip_scope()])
        out = buf.getvalue()
        self.assertIn("1 of 2 checks passed, 1 skipped", out)
        self.assertNotIn("all checks passed", out)

    def test_step_log_all_skipped_claims_no_pass(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.render_step_log([skip_scope(), skip_scope("Branch")])
        out = buf.getvalue()
        self.assertIn("  \x1b[90m⊘\x1b[0m title        skipped\n", out)
        self.assertTrue(
            out.endswith("⊘ Commit Check: all checks skipped, nothing was validated\n"),
            out,
        )
        self.assertNotIn("passed", out)
        self.assertNotIn("::error", out)


class TestFilesInTheReport(unittest.TestCase):
    """A files scope holds one finding per rule, each about its own path."""

    def test_the_first_finding_stands_for_the_scope(self):
        scope = files_scope(SHA_B, files_pass("3 file(s)"), *files_scope().checks)
        self.assertEqual(main._first_value(scope), "big.bin (4 KB)")
        self.assertEqual(
            main._first_value(files_scope(SHA_A, files_pass())), "2 file(s)"
        )
        warned = files_scope(
            SHA_A, files_pass("3 file(s)"), files_pass("a.pem (pattern *.pem)", "warn")
        )
        self.assertEqual(main._first_value(warned), "a.pem (pattern *.pem)")
        self.assertEqual(main._first_value(main.skipped_files_scope("why")), "")

    def test_the_listing_names_each_commit_by_its_hash(self):
        results = [files_scope(SHA_A, files_pass()), files_scope()]
        self.assertEqual(
            main._render_tree(results, include_docs=False),
            [
                "Files",
                "  ✔ d87faca      2 file(s)",
                "  ✖ 5584f46      big.bin (4 KB)",
                "      CC302 file-size",
                "        value: big.bin (4 KB)",
                "        File exceeds the maximum size of 1 KB",
                (
                    "        Suggest: Keep files under 1 KB, or raise max_size in "
                    "the [files] config"
                ),
                "      CC303 file-pattern",
                "        value: key.pem (pattern *.pem)",
                "        File path matches a prohibited pattern",
                "        Suggest: Remove the file from the commit",
            ],
        )
        self.assertEqual(
            main._render_tree([main.skipped_files_scope("why")], include_docs=False),
            ["Files", "  ⊘ files        skipped: why"],
        )

    def test_the_diff_shows_each_path_with_its_advice(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r"}):
            lines = main._diff_block(
                [fail_scope("Commit 1/1", sha=SHA_B), files_scope()]
            )
        self.assertEqual(
            lines[: lines.index("```") + 1],
            [
                "```diff",
                "@@ 5584f46 @@",
                "- bad message",
                "# Use <type>(<scope>): <description>",
                "@@ 5584f46 files @@",
                "- big.bin (4 KB)",
                "# Keep files under 1 KB, or raise max_size in the [files] config",
                "- key.pem (pattern *.pem)",
                "# Remove the file from the commit",
                "```",
            ],
        )
        # One link for the commit, though its message and its files both failed.
        self.assertEqual(
            lines[-1],
            f"<sub>Commits: [`5584f46`](https://github.com/o/r/commit/{SHA_B})"
            " &nbsp;|&nbsp; Rules: [CC001](https://commit-check.com/rules/#cc001)"
            " · [CC302](https://commit-check.com/rules/#cc302)"
            " · [CC303](https://commit-check.com/rules/#cc303)</sub>",
        )

    def test_a_warned_finding_goes_in_the_warning_diff(self):
        warned = files_scope(SHA_A, files_pass("a.pem (pattern *.pem)", "warn"))
        warned.checks[0]["suggest"] = "Remove the file from the commit"
        lines = main._diff_block([warned], "warn")
        self.assertEqual(
            lines[1:4],
            [
                "@@ d87faca files @@",
                "- a.pem (pattern *.pem)",
                "# Remove the file from the commit",
            ],
        )

    def test_a_finding_that_names_nothing_still_gives_its_advice(self):
        scope = files_scope(SHA_A, make_check("file_size", "fail", "CC302"))
        self.assertEqual(
            main._hunk(scope, "fail"), ["@@ d87faca files @@", "# check failed"]
        )

    def test_without_a_commit_the_scope_goes_by_its_label(self):
        scope = files_scope("")
        self.assertEqual(main._hunk(scope, "fail")[0], "@@ Files @@")
        self.assertEqual(
            main._agent_prompt([scope])[1],
            '- Files "big.bin (4 KB)": Keep files under 1 KB, or raise max_size '
            "in the [files] config",
        )
        self.assertEqual(main._row_label(scope), "files")

    def test_unparsable_output_points_to_the_step_log(self):
        scope = main.ScopeResult(label="Files", sha=SHA_A, raw_text="Traceback ...")
        self.assertEqual(
            main._hunk(scope, "fail"),
            [
                "@@ d87faca files @@",
                "# the output could not be parsed, see the step log",
            ],
        )

    def test_how_to_fix_gives_each_finding_a_line_and_no_reword_commands(self):
        how = "\n".join(main._how_to_fix([files_scope()]))
        self.assertIn(
            '- Files of commit 5584f46 "big.bin (4 KB)": Keep files under 1 KB, '
            "or raise max_size in the [files] config\n"
            '- Files of commit 5584f46 "key.pem (pattern *.pem)": Remove the file '
            "from the commit",
            how,
        )
        self.assertIn(main.FILES_HELP, how)
        self.assertNotIn("Reword", how)
        self.assertNotIn(main.FILES_HELP, "\n".join(main._how_to_fix([fail_scope()])))

    def test_the_prompt_lets_the_named_files_change(self):
        """Taking a file out of a commit cannot be done "without changing any code"."""
        self.assertEqual(main._agent_prompt([fail_scope()])[0], AGENT_PROMPT)
        for failing in ([files_scope()], [fail_scope(), files_scope()]):
            self.assertEqual(
                main._agent_prompt(failing)[0],
                "Fix these Commit Check findings, changing only the files they name:",
            )

    def test_each_finding_is_an_annotation_about_its_own_path(self):
        out = io.StringIO()
        with patch("sys.stdout", out), patch("main.DRY_RUN_ENABLED", False):
            main.render_step_log([files_scope()])
        log = ANSI_CODES.sub("", out.getvalue())
        self.assertIn("::group::✖ Files: failed", log)
        self.assertIn(
            '::error title=CC302 file-size::Files (5584f46) "big.bin (4 KB)": Keep '
            "files under 1 KB, or raise max_size in the [files] config"
            "%0AFile exceeds the maximum size of 1 KB",
            log,
        )
        self.assertIn(
            '::error title=CC303 file-pattern::Files (5584f46) "key.pem (pattern '
            '*.pem)": Remove the file from the commit'
            "%0AFile path matches a prohibited pattern",
            log,
        )

    def test_the_result_output_carries_the_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "output")
            with patch.dict(os.environ, {"GITHUB_OUTPUT": path}):
                main.set_result_output([files_scope()])
            with open(path, encoding="utf-8") as f:
                payload = json.loads(f.read().split("\n", 1)[1].rsplit("\n", 2)[0])
        self.assertEqual(
            [(s["label"], s["sha"], s["status"]) for s in payload["scopes"]],
            [("Files", SHA_B, "fail")],
        )


class TestListingRows(unittest.TestCase):
    """What each row of the listing shows: a value a reader recognises."""

    REASON = "author pre-commit-ci[bot] is in [commit].ignore_authors"

    def _skipped_commit(self, reason: str = REASON) -> main.ScopeResult:
        scope = skip_scope("Commit 2/2", reason=reason)
        scope.sha = SHA_B
        scope.text = "chore: auto fixes from pre-commit.com hooks\n"
        return scope

    def _skipped_author(self, label: str, subject: str) -> main.ScopeResult:
        scope = skip_scope(label, reason=self.REASON)
        scope.sha = SHA_B
        scope.subject = subject
        return scope

    def test_one_reason_is_said_once_and_rows_show_what_was_skipped(self):
        results = [
            pass_scope("Commit 1/2", value="feat: add a thing"),
            self._skipped_commit(),
            self._skipped_author("Author name", "pre-commit-ci[bot]"),
            self._skipped_author("Author email", "bot@users.noreply.github.com"),
        ]
        body = main.render_report(results)
        self.assertEqual(body.count("ignore_authors"), 1, body)
        self.assertIn(
            "Skipped because author pre-commit-ci[bot] is in ignore_authors.", body
        )
        self.assertIn(
            f"  ⊘ {SHA_B[:7]}      chore: auto fixes from pre-commit.com hooks\n",
            body,
        )
        self.assertIn("  ⊘ name         pre-commit-ci[bot]\n", body)
        self.assertIn("  ⊘ email        bot@users.noreply.github.com\n", body)
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            main.render_step_log(results)
        self.assertIn(
            "  ⊘ name         pre-commit-ci[bot]\n", ANSI_CODES.sub("", out.getvalue())
        )

    def test_rows_keep_their_reason_when_the_reasons_differ(self):
        other = "author dependabot[bot] is in [commit].ignore_authors"
        results = [self._skipped_commit(), self._skipped_commit(other)]
        body = main.render_report(results)
        self.assertIn(f"skipped: {self.REASON}\n", body)
        self.assertIn(f"skipped: {other}\n", body)

    def test_a_skip_nobody_explained_still_says_skipped(self):
        scope = self._skipped_commit(reason="")
        self.assertFalse(main._one_skip_reason([scope]))
        self.assertIn("  ⊘ ", main.render_report([scope]))
        self.assertIn("skipped\n", main.render_report([scope]))

    def test_a_long_subject_is_cut_at_a_word(self):
        subject = (
            "feat: add a config-source input that reads the config from the base branch"
        )
        scope = pass_scope("Commit 1/1", value=subject)
        self.assertEqual(main._scope_value(scope), subject)  # 75 characters fit
        scope = pass_scope("Commit 1/1", value=subject + " for every run")
        self.assertEqual(
            main._scope_value(scope),
            "feat: add a config-source input that reads the config from the base "
            "branch for…",
        )

    def test_a_passing_description_says_what_was_found(self):
        scope = main.ScopeResult(
            label="PR description", checks=[make_check("ai_attribution", value="")]
        )
        self.assertEqual(main._scope_value(scope), "no AI attribution")
        scope.ai_tools = ["Claude Code"]
        self.assertEqual(main._scope_value(scope), "AI attribution: Claude Code")

    def test_each_author_scope_carries_the_author_it_checked(self):
        with (
            patch("main._local_author", return_value=("Dev", "dev@example.org")),
            patch("main._rev_resolves", return_value=True),
            patch(
                "main.check_scope",
                side_effect=lambda label, args, sha: main.ScopeResult(
                    label=label, sha=sha
                ),
            ),
        ):
            results = main.run_pr_author_checks(
                ["--author-name", "--author-email"], [("a1", "feat: one")]
            )
        self.assertEqual(
            [(s.label, s.subject) for s in results],
            [("Author name", "Dev"), ("Author email", "dev@example.org")],
        )
