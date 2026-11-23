"""The step log and its annotations."""

import io
import unittest
from unittest.mock import patch

from helpers import (
    ANSI_CODES,
    HOOK_GUIDE,
    SHA_A,
    SHA_B,
    fail_scope,
    fix_scope,
    make_check,
    pass_scope,
    warn_scope,
)

from action import escape, model, render


class TestRenderStepLog(unittest.TestCase):
    def _run(self, results, colours=False):
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            render.render_step_log(results)
        output = buffer.getvalue()
        return output if colours else ANSI_CODES.sub("", output)

    def test_all_pass_prints_success_line(self):
        output = self._run([pass_scope("Branch")])
        self.assertIn("✔ Commit Check: all checks passed", output)

    def test_failure_prints_group_and_error_annotation(self):
        output = self._run([fail_scope("Commit 1/1")])
        self.assertIn("::group::✖ Commits: failed", output)
        self.assertIn("::endgroup::", output)
        self.assertIn("  ✖ 1/1          bad message\n", output)
        self.assertIn("      CC001 message", output)
        self.assertIn("value: bad message", output)
        self.assertIn("Suggest: Use <type>(<scope>): <description>", output)
        self.assertIn("Docs: https://commit-check.com/rules/#cc001", output)
        # The run page shows one line of an annotation, so the first line
        # names the scope, which the title alone cannot carry, and what to
        # change. The rule's own sentence follows for whoever expands it.
        self.assertIn(
            '::error title=CC001 message::Commit 1/1 "bad message": '
            "Use <type>(<scope>): <description>"
            "%0AThe commit message should follow Conventional Commits.\n",
            output,
        )

    def test_group_title_carries_the_result_of_the_group(self):
        """GitHub folds every group, so the title is all a reader sees."""
        output = self._run(
            [
                pass_scope("Commit 1/3"),
                fail_scope("Commit 2/3"),
                fix_scope("Commit 3/3"),
                pass_scope("Author name"),
                pass_scope("Author email"),
                warn_scope(),
            ]
        )
        self.assertIn("::group::✖ Commits: 2 of 3 failed\n", output)
        self.assertIn("::group::✔ Author: 2 passed\n", output)
        self.assertIn("::group::⚠ Branch: 1 warning\n", output)

    def test_only_the_status_glyph_of_a_row_is_coloured(self):
        output = self._run(
            [pass_scope("Branch", value="feature/x"), fail_scope("Commit 1/1")],
            colours=True,
        )
        self.assertIn("  \x1b[32m✔\x1b[0m branch       feature/x\n", output)
        self.assertIn("  \x1b[31m✖\x1b[0m 1/1          bad message\n", output)
        self.assertIn("\n      CC001 message\n", output)
        self.assertIn(
            "\x1b[31m\x1b[1m✖ Commit Check: 1 of 2 checks failed\x1b[0m\n", output
        )

    def test_commit_text_cannot_issue_workflow_commands(self):
        # A failing rule prints the whole message, one line of the log per
        # line of the message. The runner skips leading whitespace before
        # looking for "::", and reads "##[" anywhere in a line.
        scope = model.ScopeResult(
            label="Commit 1/1",
            checks=[
                make_check(
                    "message",
                    status="fail",
                    rule_id="CC001",
                    value=(
                        "bad subject\n\n"
                        "::warning title=Forged::text\n"
                        "  ::add-mask::secret\n"
                        "old ##[error]form\n"
                        "esc\x1b]52;c;eA==\x07"
                    ),
                    error="The commit message should follow Conventional Commits.",
                )
            ],
        )
        output = self._run([scope], colours=True)
        tree = output.split("::endgroup::")[0]
        for line in tree.splitlines()[1:]:  # past ::group::, our own command
            self.assertFalse(line.lstrip().startswith("::"), line)
            self.assertNotIn("##[", line)
            # The only escape sequences left are the action's own colours.
            self.assertNotIn("\x1b", ANSI_CODES.sub("", line))
        self.assertIn("        \\::warning title=Forged::text\n", tree)
        self.assertIn("          \\::add-mask::secret\n", tree)
        self.assertIn("        old #\\#[error]form\n", tree)
        self.assertIn("        esc\\x1b]52;c;eA==\\x07\n", tree)
        # The annotation names the subject alone, on one line.
        self.assertIn('::Commit 1/1 "bad subject": The commit message', output)

    def test_a_subject_that_starts_like_a_command_stays_text_in_its_row(self):
        scope = model.ScopeResult(
            label="Commit 1/1", checks=[make_check("message", value="::error::x")]
        )
        output = self._run([scope])
        self.assertIn("  ✔ 1/1          ::error::x\n", output)

    def test_log_safe_leaves_ordinary_text_alone(self):
        for text in [
            "feat: add login",
            "value: a::b",
            "  ✔ branch       feature/x",
            "tab\there",
        ]:
            self.assertEqual(escape._log_safe(text), text)

    def test_annotation_names_the_commit_and_carries_the_fix(self):
        output = self._run([fix_scope("Commit 2/3", sha=SHA_B)])
        self.assertIn(
            "::error title=CC002 subject-capitalized::Commit 2/3 (5584f46): "
            'change "feat: add login page" to "feat: Add login page"'
            "%0ASubject must start with a capital letter\n",
            output,
        )

    def test_finding_lines_are_the_value_the_error_and_the_fix(self):
        self.assertEqual(
            render._finding_lines(fix_scope().checks[0]),
            [
                "value: feat: add login page",
                "Subject must start with a capital letter",
                "Fix: feat: Add login page",
            ],
        )

    def test_a_list_value_lines_up_under_its_first_item(self):
        """The second declaration box would otherwise sit at the column of
        ``value:`` and read as a line of its own."""
        check = make_check(
            "ai_declaration",
            status="fail",
            rule_id="CC501",
            value="- [ ] No AI tools were used\n- [ ] AI tools were used",
            error="No AI declaration box is ticked",
        )
        self.assertEqual(
            render._finding_lines(check),
            [
                "value: - [ ] No AI tools were used",
                "       - [ ] AI tools were used",
                "No AI declaration box is ticked",
            ],
        )

    def test_a_commit_body_lines_up_under_the_subject(self):
        """The body sits under the subject, so the error after it is not
        read as a line of the message. The blank line stays empty."""
        check = make_check(
            "message",
            status="fail",
            rule_id="CC001",
            value="added login page\n\nIt also fixes the redirect.",
            error="The commit message should follow Conventional Commits.",
        )
        self.assertEqual(
            render._finding_lines(check),
            [
                "value: added login page",
                "",
                "       It also fixes the redirect.",
                "The commit message should follow Conventional Commits.",
            ],
        )

    def test_annotation_without_an_error_still_names_the_kind_of_finding(self):
        scope = model.ScopeResult(
            label="Branch",
            checks=[
                make_check("branch", status="fail", rule_id="CC201"),
                make_check("merge_base", status="warn", rule_id="CC202"),
            ],
        )
        output = self._run([scope])
        self.assertIn("::error title=CC201 branch::Branch: check failed\n", output)
        self.assertIn(
            "::warning title=CC202 merge-base::Branch: check warning\n", output
        )

    def test_fix_is_printed_once_when_suggest_only_repeats_it(self):
        """commit-check sets suggest to ``Use "<fix>"`` when a rule has a
        fix and no advice of its own; printing both says it twice."""
        output = self._run([fix_scope()])
        self.assertIn("        Fix: feat: Add login page\n", output)
        self.assertNotIn("Suggest:", output)

    def test_bespoke_suggest_and_fix_are_both_printed(self):
        scope = model.ScopeResult(
            label="Commit 1/1",
            checks=[
                make_check(
                    "allow_wip_commits",
                    status="fail",
                    rule_id="CC010",
                    value="wip: stuff",
                    error="WIP commits are not allowed",
                    suggest='Drop the WIP marker: "stuff"',
                    fix="stuff",
                )
            ],
        )
        output = self._run([scope])
        listing = output.split("::endgroup::")[0]
        self.assertIn(
            "        WIP commits are not allowed\n"
            '        Suggest: Drop the WIP marker: "stuff"\n'
            "        Fix: stuff\n",
            listing,
        )
        self.assertIn(
            '::Commit 1/1: change "wip: stuff" to "stuff"'
            "%0AWIP commits are not allowed\n",
            output,
        )

    def test_a_multi_line_fix_takes_one_row_per_line(self):
        fix = "feat: add login page\n\nSigned-off-by: Jane Doe <jane@example.com>"
        scope = model.ScopeResult(
            label="Commit 1/1",
            sha=SHA_A,
            checks=[
                make_check(
                    "require_signed_off_by",
                    status="fail",
                    rule_id="CC012",
                    value="feat: add login page",
                    error="Signed-off-by not found in latest commit",
                    suggest=f'Use "{fix}"',
                    fix=fix,
                )
            ],
        )
        output = self._run([scope])
        listing = output.split("::endgroup::")[0]
        self.assertIn(
            "        Fix: feat: add login page\n"
            "        \n"
            "        Signed-off-by: Jane Doe <jane@example.com>\n",
            listing,
        )
        # The annotation names the one line the fix adds.
        self.assertIn(
            '::Commit 1/1 (d87faca) "feat: add login page": '
            'add "Signed-off-by: Jane Doe <jane@example.com>"'
            "%0ASigned-off-by not found in latest commit\n",
            output,
        )

    def test_tree_lines_carry_the_short_sha(self):
        results = [
            pass_scope("Commit 1/3", value="fix: first"),
            fix_scope("Commit 2/3", sha=SHA_B),
            model.ScopeResult(label="Commit 3/3", sha=SHA_A, raw_text="garbage"),
        ]
        results[0].sha = "c" * 40
        output = self._run(results)
        self.assertIn("  ✔ ccccccc      fix: first\n", output)
        self.assertIn("  ✖ 5584f46      feat: add login page\n", output)
        self.assertIn("  ✖ d87faca\n", output)
        self.assertIn(
            "::error title=Commit Check: Commit 3/3 (d87faca)::output could not "
            "be parsed\n",
            output,
        )

    def test_failure_verdict_is_a_plain_line_not_an_annotation(self):
        """One ::error per finding, and a verdict in words — never a second
        annotation for the total, which GitHub counted as one more error."""
        output = self._run([fail_scope("Commit 1/2"), fix_scope("Commit 2/2")])
        self.assertEqual(output.count("::error"), 2)
        self.assertNotIn("::error::", output)
        self.assertIn("\n✖ Commit Check: 2 of 2 checks failed\n", output)
        self.assertNotIn("passed", output)

    def test_failure_verdict_says_where_the_fix_is(self):
        with patch("action.inputs.JOB_SUMMARY_ENABLED", True):
            output = self._run([fail_scope("Commit 1/1")])
        self.assertTrue(
            output.endswith(
                "How to fix: see the job summary. "
                f"Check before you push: {HOOK_GUIDE}\n"
            )
        )
        # With no summary to send the reader to, the log is the report.
        with patch("action.inputs.JOB_SUMMARY_ENABLED", False):
            output = self._run([fail_scope("Commit 1/1")])
        self.assertIn("How to fix: see the findings above. ", output)

    def test_failure_verdict_counts_scopes_and_names_the_warnings(self):
        output = self._run(
            [fail_scope("Commit 1/1"), pass_scope("PR title"), warn_scope()]
        )
        self.assertIn("\n✖ Commit Check: 1 of 3 checks failed, 1 warning\n", output)
        self.assertNotIn("dry-run", output)

    def test_failure_reason_is_printed_once(self):
        """The listing and the annotation must not both print the reason.

        They used to: the ::error command carried the first line of the error
        and the listing printed the whole error underneath it, so the same
        sentence appeared twice in a row in the job log.
        """
        output = self._run([fail_scope("Commit 1/1")])
        self.assertEqual(
            output.count("The commit message should follow Conventional Commits."),
            2,  # once in the listing, once in the annotation after the groups
        )
        listing = output.split("::endgroup::")[0]
        self.assertEqual(
            listing.count("The commit message should follow Conventional Commits."), 1
        )

    def test_annotations_come_after_every_group(self):
        """An ::error inside a group breaks the indented listing apart."""
        output = self._run([fail_scope("Commit 1/1"), pass_scope("Branch")])
        self.assertLess(output.rindex("::endgroup::"), output.index("::error "))

    def test_annotation_payload_is_escaped(self):
        scope = model.ScopeResult(
            label="Commit 1/1",
            checks=[
                make_check(
                    "message",
                    status="fail",
                    value="50% done",
                    error="first line\nsecond line with 100% certainty",
                )
            ],
        )
        output = self._run([scope])
        annotation = next(ln for ln in output.splitlines() if ln.startswith("::error"))
        self.assertNotIn("\n", annotation.removeprefix("::error "))
        self.assertIn('Commit 1/1 "50%25 done": first line', annotation)

    def test_pass_scopes_show_the_checked_value(self):
        output = self._run([pass_scope("Branch", value="feature/add-login")])
        self.assertIn("✔ branch       feature/add-login", output)

    def test_groups_scopes_by_category(self):
        results = [
            fail_scope("PR title"),
            pass_scope("Commit 1/2"),
            fail_scope("Branch"),
        ]
        output = self._run(results)
        # One group each for the pull request, the commits and the branch.
        self.assertEqual(output.count("::group::"), 3)
        self.assertIn("::group::✖ Pull request: failed", output)
        self.assertIn("::group::✔ Commits: passed", output)
        self.assertIn("::group::✖ Branch: failed", output)

    def test_title_and_description_share_one_group(self):
        """The commits come after both, so the commit group is not split."""
        results = [
            pass_scope("PR title"),
            pass_scope("PR description"),
            pass_scope("Commit 1/1"),
        ]
        groups = [name for name, _ in render._grouped(results)]
        self.assertEqual(groups, ["Pull request", "Commits"])

    def test_raw_text_fallback_is_printed(self):
        scope = model.ScopeResult(label="Branch", raw_text="unexpected output")
        output = self._run([scope])
        self.assertIn("  ✖ branch\n", output)
        self.assertIn("unexpected output", output)
        self.assertIn("::error title=Commit Check: Branch::", output)

    def test_dry_run_downgrades_annotations_and_prints_verdict(self):
        """Dry-run still reports every failure, but nothing may say "error".

        The exit code is forced to 0, so an ::error annotation would count
        toward the run's error total on a green job, and log_error_and_exit
        prints nothing for a zero exit — leaving the log with no verdict.
        """
        with patch("action.inputs.DRY_RUN_ENABLED", True):
            output = self._run([fail_scope("Commit 1/1"), pass_scope("Branch")])
        self.assertNotIn("::error", output)
        self.assertIn(
            '::warning title=CC001 message::Commit 1/1 "bad message": '
            "Use <type>(<scope>): <description>",
            output,
        )
        self.assertIn(
            "Commit Check (dry-run): 1 of 2 checks failed; not failing the job",
            output,
        )
        self.assertNotIn("all checks passed", output)
        self.assertNotIn("✖ Commit Check", output)

    def test_dry_run_without_failures_prints_the_usual_verdict(self):
        with patch("action.inputs.DRY_RUN_ENABLED", True):
            output = self._run([pass_scope("Branch")])
        self.assertIn("✔ Commit Check: all checks passed", output)
        self.assertNotIn("dry-run", output)
