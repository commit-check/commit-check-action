"""Unit tests for main.py."""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# GITHUB_STEP_SUMMARY is accessed via os.environ[] (not getenv) at import time,
# so we must set it before importing main.
os.environ.setdefault("GITHUB_STEP_SUMMARY", "/tmp/step_summary.txt")

import main  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_check(
    check: str,
    status: str = "pass",
    rule_id: str = "CC001",
    value: str = "",
    error: str = "",
    suggest: str = "",
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
        "docs_url": docs_url,
    }


def json_output(*checks) -> str:
    """Serialize checks to the CLI JSON output shape."""
    status = "fail" if any(c["status"] == "fail" for c in checks) else "pass"
    return json.dumps({"status": status, "checks": list(checks)})


def pass_scope(label: str = "Branch") -> main.ScopeResult:
    return main.ScopeResult(label=label, checks=[make_check("branch")])


def fail_scope(label: str = "Commit 1/1") -> main.ScopeResult:
    return main.ScopeResult(
        label=label,
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


class TestEnvFlag(unittest.TestCase):
    def test_true_value(self):
        with patch.dict(os.environ, {"FEATURE_FLAG": "true"}):
            self.assertTrue(main.env_flag("FEATURE_FLAG"))

    def test_false_value(self):
        with patch.dict(os.environ, {"FEATURE_FLAG": "false"}):
            self.assertFalse(main.env_flag("FEATURE_FLAG"))

    def test_missing_uses_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(main.env_flag("FEATURE_FLAG", default="true"))


class TestReconfigureIo(unittest.TestCase):
    def test_reconfigures_streams_to_utf8(self):
        class FakeStream:
            def __init__(self):
                self.reconfigured = None

            def reconfigure(self, **kwargs):
                self.reconfigured = kwargs

        fake_out = FakeStream()
        fake_err = FakeStream()
        with (
            patch.object(sys, "stdout", fake_out),
            patch.object(sys, "stderr", fake_err),
        ):
            main._reconfigure_io()
        self.assertEqual(
            fake_out.reconfigured, {"encoding": "utf-8", "errors": "replace"}
        )
        self.assertEqual(
            fake_err.reconfigured, {"encoding": "utf-8", "errors": "replace"}
        )

    def test_streams_without_reconfigure_are_ignored(self):
        class NoopStream:
            pass

        with (
            patch.object(sys, "stdout", NoopStream()),
            patch.object(sys, "stderr", NoopStream()),
        ):
            main._reconfigure_io()  # should not raise


class TestBuildCheckArgs(unittest.TestCase):
    def test_all_true(self):
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", True),
            patch("main.AUTHOR_NAME_ENABLED", True),
            patch("main.AUTHOR_EMAIL_ENABLED", True),
        ):
            result = main.build_check_args()
        self.assertEqual(
            result, ["--message", "--branch", "--author-name", "--author-email"]
        )

    def test_all_false(self):
        with (
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
        ):
            result = main.build_check_args()
        self.assertEqual(result, [])

    def test_message_and_branch(self):
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", True),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
        ):
            result = main.build_check_args()
        self.assertEqual(result, ["--message", "--branch"])


class TestParseCommitMessages(unittest.TestCase):
    def test_splits_messages_and_trims_surrounding_newlines(self):
        result = main.parse_commit_messages("\nfix: first\n\x00\nfeat: second\n\n\x00")
        self.assertEqual(result, ["fix: first", "feat: second"])


class TestGetPrTitle(unittest.TestCase):
    def test_non_pr_event_returns_none(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}):
            self.assertIsNone(main.get_pr_title())

    def test_pr_event_returns_title(self):
        event = {
            "pull_request": {"title": "feat: add login page"},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(event, f)
            event_path = f.name
        with (
            patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": event_path},
            ),
        ):
            self.assertEqual(main.get_pr_title(), "feat: add login page")
        os.unlink(event_path)

    def test_pull_request_target_event(self):
        event = {
            "pull_request": {"title": "fix: resolve timeout"},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(event, f)
            event_path = f.name
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_NAME": "pull_request_target",
                    "GITHUB_EVENT_PATH": event_path,
                },
            ),
        ):
            self.assertEqual(main.get_pr_title(), "fix: resolve timeout")
        os.unlink(event_path)

    def test_missing_event_path_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ["GITHUB_EVENT_NAME"] = "pull_request"
            os.environ.pop("GITHUB_EVENT_PATH", None)
            self.assertIsNone(main.get_pr_title())

    def test_invalid_json_returns_none(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json")
            event_path = f.name
        with (
            patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": event_path},
            ),
            patch("builtins.print"),
        ):
            self.assertIsNone(main.get_pr_title())
        os.unlink(event_path)


class TestRunCheckJson(unittest.TestCase):
    def test_parses_json_output(self):
        mock_result = MagicMock(returncode=0, stdout=json_output(make_check("branch")))
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            rc, data, raw = main.run_check_json(["--branch"])
        self.assertEqual(rc, 0)
        self.assertEqual(data["status"], "pass")
        self.assertEqual(len(data["checks"]), 1)
        self.assertIn("checks", raw)

    def test_command_includes_format_json(self):
        mock_result = MagicMock(returncode=0, stdout="{}")
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            main.run_check_json(["--branch"])
        self.assertEqual(
            mock_run.call_args[0][0],
            ["commit-check", "--format", "json", "--branch"],
        )

    def test_input_text_is_passed_through(self):
        mock_result = MagicMock(returncode=0, stdout="{}")
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            main.run_check_json(["--message"], input_text="fix: demo")
        self.assertEqual(mock_run.call_args[1]["input"], "fix: demo")
        self.assertTrue(mock_run.call_args[1]["text"])

    def test_invalid_json_returns_none_with_raw_output(self):
        mock_result = MagicMock(returncode=1, stdout="Commit rejected.\n")
        with patch("main.subprocess.run", return_value=mock_result):
            rc, data, raw = main.run_check_json(["--branch"])
        self.assertEqual(rc, 1)
        self.assertIsNone(data)
        self.assertEqual(raw, "Commit rejected.\n")


class TestScopeResult(unittest.TestCase):
    def test_status_pass_when_all_checks_pass(self):
        scope = main.ScopeResult(
            label="Branch", checks=[make_check("branch"), make_check("merge_base")]
        )
        self.assertEqual(scope.status, "pass")
        self.assertEqual(scope.failures, [])

    def test_status_fail_when_any_check_fails(self):
        scope = main.ScopeResult(
            label="Branch",
            checks=[
                make_check("branch", status="fail"),
                make_check("merge_base"),
            ],
        )
        self.assertEqual(scope.status, "fail")
        self.assertEqual(len(scope.failures), 1)

    def test_raw_text_fallback_is_failure(self):
        scope = main.ScopeResult(label="Branch", raw_text="unexpected output")
        self.assertEqual(scope.status, "fail")


class TestCheckScope(unittest.TestCase):
    def test_parses_checks_into_scope(self):
        mock_result = MagicMock(
            returncode=1, stdout=json_output(make_check("branch", status="fail"))
        )
        with patch("main.subprocess.run", return_value=mock_result):
            scope = main.check_scope("Branch", ["--branch"])
        self.assertEqual(scope.label, "Branch")
        self.assertEqual(scope.status, "fail")
        self.assertEqual(scope.failures[0]["rule_id"], "CC001")

    def test_invalid_json_falls_back_to_raw_text(self):
        mock_result = MagicMock(returncode=1, stdout="unexpected output")
        with patch("main.subprocess.run", return_value=mock_result):
            scope = main.check_scope("Branch", ["--branch"])
        self.assertEqual(scope.label, "Branch")
        self.assertEqual(scope.raw_text, "unexpected output")
        self.assertEqual(scope.status, "fail")


class TestRunPrMessageChecks(unittest.TestCase):
    def test_single_message_pass(self):
        mock_result = MagicMock(returncode=0, stdout=json_output(make_check("message")))
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            scopes = main.run_pr_message_checks(["fix: something"])
        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0].status, "pass")
        self.assertEqual(scopes[0].label, "Commit 1/1")
        self.assertEqual(
            mock_run.call_args[0][0],
            ["commit-check", "--format", "json", "--message"],
        )
        self.assertEqual(mock_run.call_args[1]["input"], "fix: something")

    def test_failed_message_marks_scope_failed(self):
        mock_result = MagicMock(
            returncode=1,
            stdout=json_output(make_check("message", status="fail")),
        )
        with patch("main.subprocess.run", return_value=mock_result):
            scopes = main.run_pr_message_checks(["bad commit"])
        self.assertEqual(scopes[0].status, "fail")
        self.assertEqual(len(scopes[0].failures), 1)

    def test_labels_commits_in_order(self):
        results = [
            MagicMock(returncode=0, stdout=json_output(make_check("message"))),
            MagicMock(
                returncode=1,
                stdout=json_output(make_check("message", status="fail")),
            ),
            MagicMock(returncode=0, stdout=json_output(make_check("message"))),
        ]
        with patch("main.subprocess.run", side_effect=results):
            scopes = main.run_pr_message_checks(["ok", "bad", "ok"])
        self.assertEqual(
            [s.label for s in scopes], ["Commit 1/3", "Commit 2/3", "Commit 3/3"]
        )
        self.assertEqual(scopes[1].status, "fail")

    def test_empty_list(self):
        with patch("main.subprocess.run") as mock_run:
            scopes = main.run_pr_message_checks([])
        self.assertEqual(scopes, [])
        mock_run.assert_not_called()


class TestRunOtherChecks(unittest.TestCase):
    def test_empty_args_returns_no_scopes(self):
        with patch("main.subprocess.run") as mock_run:
            scopes = main.run_other_checks([])
        self.assertEqual(scopes, [])
        mock_run.assert_not_called()

    def test_runs_each_flag_as_its_own_scope(self):
        results = [
            MagicMock(
                returncode=1, stdout=json_output(make_check("branch", status="fail"))
            ),
            MagicMock(returncode=0, stdout=json_output(make_check("author_name"))),
        ]
        with patch("main.subprocess.run", side_effect=results) as mock_run:
            scopes = main.run_other_checks(["--branch", "--author-name"])
        self.assertEqual([s.label for s in scopes], ["Branch", "Author name"])
        self.assertEqual(scopes[0].status, "fail")
        self.assertEqual(scopes[1].status, "pass")
        self.assertEqual(
            mock_run.call_args_list[0][0][0],
            ["commit-check", "--format", "json", "--branch"],
        )
        self.assertEqual(
            mock_run.call_args_list[1][0][0],
            ["commit-check", "--format", "json", "--author-name"],
        )

    def test_unknown_flag_is_skipped(self):
        with patch("main.subprocess.run") as mock_run:
            scopes = main.run_other_checks(["--unknown"])
        self.assertEqual(scopes, [])
        mock_run.assert_not_called()


class TestGetPrCommitMessages(unittest.TestCase):
    def test_non_pr_event_returns_empty(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, [])

    def test_merge_ref_is_preferred(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch(
                "main.get_messages_from_merge_ref",
                return_value=["fix: first", "feat: second"],
            ) as mock_merge,
            patch("main.get_messages_from_head_ref") as mock_head,
        ):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, ["fix: first", "feat: second"])
        mock_merge.assert_called_once()
        mock_head.assert_not_called()

    def test_pull_request_target_is_supported(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
            patch("main.get_messages_from_merge_ref", return_value=["fix: first"]),
        ):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, ["fix: first"])

    def test_falls_back_to_base_ref_when_merge_ref_is_unavailable(self):
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_BASE_REF": "main",
                },
            ),
            patch("main.get_messages_from_merge_ref", return_value=[]),
            patch(
                "main.get_messages_from_head_ref",
                return_value=["fix: first", "feat: second"],
            ) as mock_head,
        ):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, ["fix: first", "feat: second"])
        mock_head.assert_called_once_with("main")

    def test_exception_returns_empty(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch(
                "main.get_messages_from_merge_ref", side_effect=Exception("git failed")
            ),
        ):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, [])


class TestGitMessageReaders(unittest.TestCase):
    def test_get_messages_from_merge_ref(self):
        mock_result = MagicMock(
            returncode=0, stdout="fix: first\n\x00feat: second\n\x00"
        )
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            result = main.get_messages_from_merge_ref()
        self.assertEqual(result, ["fix: first", "feat: second"])
        self.assertEqual(
            mock_run.call_args[0][0],
            ["git", "log", "--pretty=format:%B%x00", "--reverse", "HEAD^1..HEAD^2"],
        )

    def test_get_messages_from_head_ref(self):
        mock_result = MagicMock(returncode=0, stdout="fix: first\n\x00")
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            result = main.get_messages_from_head_ref("main")
        self.assertEqual(result, ["fix: first"])
        self.assertEqual(
            mock_run.call_args[0][0],
            [
                "git",
                "log",
                "--pretty=format:%B%x00",
                "--reverse",
                "origin/main..HEAD",
            ],
        )


class TestRunCommitCheck(unittest.TestCase):
    def test_pr_path_checks_each_commit(self):
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.get_pr_commit_messages", return_value=["fix: something"]),
            patch("main.run_pr_message_checks", return_value=[pass_scope()]) as mock_pr,
            patch("main.run_other_checks", return_value=[]),
        ):
            rc, results = main.run_commit_check()
        self.assertEqual(rc, 0)
        mock_pr.assert_called_once_with(["fix: something"])
        self.assertEqual(len(results), 1)

    def test_pr_path_fails_when_any_scope_fails(self):
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", True),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.get_pr_commit_messages", return_value=["bad msg"]),
            patch("main.run_pr_message_checks", return_value=[fail_scope()]),
            patch("main.run_other_checks", return_value=[pass_scope()]),
        ):
            rc, results = main.run_commit_check()
        self.assertEqual(rc, 1)
        self.assertEqual(len(results), 2)

    def test_pr_title_check_runs_when_enabled(self):
        with (
            patch("main.PR_TITLE_ENABLED", True),
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.is_pr_event", return_value=True),
            patch("main.get_pr_title", return_value="feat: a feature"),
            patch(
                "main.check_scope", return_value=pass_scope("PR title")
            ) as mock_scope,
            patch("main.run_other_checks", return_value=[]),
        ):
            rc, results = main.run_commit_check()
        self.assertEqual(rc, 0)
        mock_scope.assert_called_once_with(
            "PR title", ["--message"], input_text="feat: a feature"
        )

    def test_pr_title_failure_propagates(self):
        with (
            patch("main.PR_TITLE_ENABLED", True),
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.is_pr_event", return_value=True),
            patch("main.get_pr_title", return_value="bad title"),
            patch("main.check_scope", return_value=fail_scope("PR title")),
            patch("main.run_other_checks", return_value=[]),
        ):
            rc, results = main.run_commit_check()
        self.assertEqual(rc, 1)

    def test_pr_title_skipped_outside_pr_context(self):
        with (
            patch("main.PR_TITLE_ENABLED", True),
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.is_pr_event", return_value=False),
            patch("main.get_pr_title") as mock_title,
            patch("main.run_other_checks", return_value=[]),
        ):
            rc, results = main.run_commit_check()
        self.assertEqual(rc, 0)
        mock_title.assert_not_called()

    def test_non_pr_message_check_uses_commit_message_scope(self):
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.get_pr_commit_messages", return_value=[]),
            patch("main.run_pr_message_checks") as mock_pr,
            patch(
                "main.check_scope", return_value=pass_scope("Commit message")
            ) as mock_scope,
            patch("main.run_other_checks", return_value=[]),
        ):
            rc, results = main.run_commit_check()
        self.assertEqual(rc, 0)
        mock_pr.assert_not_called()
        mock_scope.assert_called_once_with("Commit message", ["--message"])

    def test_message_flag_removed_before_other_checks_in_pr(self):
        captured_args = []

        def fake_other_checks(args):
            captured_args.extend(args)
            return []

        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", True),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.get_pr_commit_messages", return_value=["fix: x"]),
            patch("main.run_pr_message_checks", return_value=[pass_scope()]),
            patch("main.run_other_checks", side_effect=fake_other_checks),
        ):
            main.run_commit_check()
        self.assertNotIn("--message", captured_args)
        self.assertIn("--branch", captured_args)


class TestRenderStepLog(unittest.TestCase):
    def _run(self, results):
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            main.render_step_log(results)
        return buffer.getvalue()

    def test_all_pass_prints_success_line(self):
        output = self._run([pass_scope("Branch")])
        self.assertIn("✔ commit-check: all checks passed", output)

    def test_failure_prints_group_and_error_annotation(self):
        output = self._run([fail_scope("Commit 1/1")])
        self.assertIn("::group::Commit message", output)
        self.assertIn("::endgroup::", output)
        self.assertIn("✖ Commit 1/1 (1 failure)", output)
        self.assertIn(
            "::error title=CC001 message::The commit message should follow "
            "Conventional Commits.",
            output,
        )
        self.assertIn("value: bad message", output)
        self.assertIn("Suggest: Use <type>(<scope>): <description>", output)
        self.assertIn("Docs: https://commit-check.com/rules/#cc001", output)

    def test_groups_scopes_by_category(self):
        results = [
            fail_scope("PR title"),
            pass_scope("Commit 1/2"),
            fail_scope("Branch"),
        ]
        output = self._run(results)
        # One group for commit-message scopes, one for the branch scope.
        self.assertEqual(output.count("::group::"), 2)
        self.assertIn("::group::Commit message", output)
        self.assertIn("::group::Branch", output)

    def test_raw_text_fallback_is_printed(self):
        scope = main.ScopeResult(label="Branch", raw_text="unexpected output")
        output = self._run([scope])
        self.assertIn("✖ Branch (0 failures)", output)
        self.assertIn("unexpected output", output)


class TestRenderJobSummary(unittest.TestCase):
    def test_all_pass(self):
        body = main.render_job_summary([pass_scope("Branch")])
        self.assertTrue(body.startswith(main.REPORT_TITLE))
        self.assertIn("All checks passed (1 scope)", body)
        self.assertIn("<details>", body)
        self.assertIn("<summary>Show details</summary>", body)
        self.assertIn("```text", body)
        self.assertIn("Branch", body)
        self.assertIn("  ✔ Branch", body)

    def test_all_pass_groups_scopes_like_step_log(self):
        results = [
            pass_scope("PR title"),
            pass_scope("Commit 1/2"),
            pass_scope("Commit 2/2"),
            pass_scope("Branch"),
            pass_scope("Author name"),
            pass_scope("Author email"),
        ]
        body = main.render_job_summary(results)
        # Group headers in the details block mirror the step log ordering.
        self.assertLess(body.index("Commit message"), body.index("Branch"))
        self.assertLess(body.index("Branch"), body.index("Author"))
        for scope in results:
            self.assertIn(f"  ✔ {scope.label}", body)

    def test_failure_renders_table_with_rule_links(self):
        body = main.render_job_summary([fail_scope("Commit 1/1")])
        self.assertTrue(body.startswith(main.REPORT_TITLE))
        self.assertIn("**1 failure** across 1 scope", body)
        self.assertIn("| Scope | Failed checks | Result |", body)
        self.assertIn(
            "| Commit 1/1 | [CC001 message](https://commit-check.com/rules/#cc001) | ❌ |",
            body,
        )
        self.assertIn("<details>", body)
        self.assertIn("value: `bad message`", body)
        self.assertIn("suggest: Use <type>(<scope>): <description>", body)
        self.assertIn("Rules reference: https://commit-check.com/rules/", body)

    def test_pass_scope_renders_checkmark(self):
        body = main.render_job_summary([pass_scope("Branch"), fail_scope("Commit 1/1")])
        self.assertIn("| Branch | — | ✅ |", body)


class TestRenderPrComment(unittest.TestCase):
    def test_all_pass_matches_job_summary(self):
        comment = main.render_pr_comment([pass_scope("Branch")])
        summary = main.render_job_summary([pass_scope("Branch")])
        self.assertEqual(comment, summary)
        self.assertTrue(comment.startswith(main.REPORT_TITLE))
        self.assertIn("All checks passed (1 scope)", comment)

    def test_failure_matches_job_summary(self):
        comment = main.render_pr_comment([fail_scope("Commit 1/1")])
        summary = main.render_job_summary([fail_scope("Commit 1/1")])
        self.assertEqual(comment, summary)
        self.assertTrue(comment.startswith(main.REPORT_TITLE))
        self.assertIn("**1 failure** across 1 scope", comment)
        self.assertIn("| Scope | Failed checks | Result |", comment)


class TestBuildResultBody(unittest.TestCase):
    def test_success_body(self):
        self.assertEqual(main.build_result_body(None), main.REPORT_TITLE)

    def test_failure_body(self):
        result = main.build_result_body("bad commit")
        self.assertIn(main.REPORT_TITLE, result)
        self.assertIn("bad commit", result)


class TestAddJobSummary(unittest.TestCase):
    def test_false_skips(self):
        with patch("main.JOB_SUMMARY_ENABLED", False):
            rc = main.add_job_summary([pass_scope()])
        self.assertEqual(rc, 0)

    def test_success_writes_policy_report(self):
        summary_path = os.path.join(tempfile.mkdtemp(), "summary.txt")
        with (
            patch("main.JOB_SUMMARY_ENABLED", True),
            patch("main.GITHUB_STEP_SUMMARY", summary_path),
        ):
            rc = main.add_job_summary([pass_scope("Branch")])
        self.assertEqual(rc, 0)
        with open(summary_path, encoding="utf-8") as file_obj:
            content = file_obj.read()
        self.assertIn("All checks passed", content)

    def test_failure_returns_nonzero(self):
        summary_path = os.path.join(tempfile.mkdtemp(), "summary.txt")
        with (
            patch("main.JOB_SUMMARY_ENABLED", True),
            patch("main.GITHUB_STEP_SUMMARY", summary_path),
        ):
            rc = main.add_job_summary([fail_scope()])
        self.assertEqual(rc, 1)
        with open(summary_path, encoding="utf-8") as file_obj:
            content = file_obj.read()
        self.assertIn("| Scope | Failed checks | Result |", content)
        self.assertIn("❌", content)


class TestSetResultOutput(unittest.TestCase):
    def test_writes_heredoc_json(self):
        output_path = os.path.join(tempfile.mkdtemp(), "output.txt")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
            main.set_result_output([fail_scope("Commit 1/1"), pass_scope("Branch")])
        with open(output_path, encoding="utf-8") as file_obj:
            content = file_obj.read()
        self.assertIn("result<<EOF", content)
        self.assertIn('"status": "fail"', content)
        self.assertIn('"label": "Commit 1/1"', content)
        self.assertTrue(content.strip().endswith("EOF"))

    def test_all_pass_status(self):
        output_path = os.path.join(tempfile.mkdtemp(), "output.txt")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
            main.set_result_output([pass_scope()])
        with open(output_path, encoding="utf-8") as file_obj:
            content = file_obj.read()
        self.assertIn('"status": "pass"', content)

    def test_no_output_env_is_noop(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ["GITHUB_STEP_SUMMARY"] = "/tmp/step_summary.txt"
            main.set_result_output([pass_scope()])  # should not raise


class TestAddPrComments(unittest.TestCase):
    def test_disabled_returns_zero(self):
        with patch("main.PR_COMMENTS_ENABLED", False):
            rc = main.add_pr_comments([pass_scope()])
        self.assertEqual(rc, 0)

    def test_fork_pr_skips_comment_and_warns(self):
        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch("main.is_fork_pr", return_value=True),
            patch("main.JOB_SUMMARY_ENABLED", False),
            patch("builtins.print") as mock_print,
        ):
            rc = main.add_pr_comments([pass_scope()])
        self.assertEqual(rc, 0)
        printed = mock_print.call_args[0][0]
        self.assertIn("::warning::", printed)
        self.assertIn("read-only", printed)

    def test_fork_pr_writes_job_summary_hint(self):
        summary_path = os.path.join(tempfile.mkdtemp(), "summary.txt")
        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch("main.is_fork_pr", return_value=True),
            patch("main.JOB_SUMMARY_ENABLED", True),
            patch("main.GITHUB_STEP_SUMMARY", summary_path),
            patch("builtins.print"),
        ):
            rc = main.add_pr_comments([pass_scope()])
        self.assertEqual(rc, 0)
        with open(summary_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("PR Comment Skipped", content)
        self.assertIn("read-only", content)
        self.assertIn("fork-pr-comments", content)

    def test_creates_comment_with_rendered_body(self):
        mock_pull_request = MagicMock()
        mock_pull_request.get_comments.return_value = []
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_pull_request

        github_module = MagicMock()
        github_module.Github.return_value.get_repo.return_value = mock_repo

        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch("main.is_fork_pr_with_readonly_token", return_value=False),
            patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "token",
                    "GITHUB_REPOSITORY": "owner/repo",
                    "GITHUB_REF": "refs/pull/12/merge",
                },
            ),
            patch.dict(sys.modules, {"github": github_module}),
        ):
            rc = main.add_pr_comments([fail_scope()])
        self.assertEqual(rc, 1)
        self.assertEqual(mock_pull_request.create_comment.call_count, 1)
        body = mock_pull_request.create_comment.call_args[1]["body"]
        self.assertTrue(body.startswith(main.REPORT_TITLE))
        self.assertIn("| Scope | Failed checks | Result |", body)

    def test_updates_existing_comment_when_changed(self):
        old_comment = MagicMock(body="# Commit-Check ❌ 0 failures")
        mock_pull_request = MagicMock()
        mock_pull_request.get_comments.return_value = [old_comment]
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_pull_request

        github_module = MagicMock()
        github_module.Github.return_value.get_repo.return_value = mock_repo

        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch("main.is_fork_pr_with_readonly_token", return_value=False),
            patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "token",
                    "GITHUB_REPOSITORY": "owner/repo",
                    "GITHUB_REF": "refs/pull/12/merge",
                },
            ),
            patch.dict(sys.modules, {"github": github_module}),
            patch("builtins.print"),
        ):
            rc = main.add_pr_comments([fail_scope()])
        self.assertEqual(rc, 1)
        old_comment.edit.assert_called_once()
        old_comment.delete.assert_not_called()

    def test_skips_when_comment_is_up_to_date(self):
        body = main.render_pr_comment([fail_scope()])
        existing = MagicMock(body=body)
        mock_pull_request = MagicMock()
        mock_pull_request.get_comments.return_value = [existing]
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_pull_request

        github_module = MagicMock()
        github_module.Github.return_value.get_repo.return_value = mock_repo

        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch("main.is_fork_pr_with_readonly_token", return_value=False),
            patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "token",
                    "GITHUB_REPOSITORY": "owner/repo",
                    "GITHUB_REF": "refs/pull/12/merge",
                },
            ),
            patch.dict(sys.modules, {"github": github_module}),
            patch("builtins.print"),
        ):
            rc = main.add_pr_comments([fail_scope()])
        self.assertEqual(rc, 1)
        existing.edit.assert_not_called()
        mock_pull_request.create_comment.assert_not_called()


class TestIsForkPrWithReadonlyToken(unittest.TestCase):
    def test_fork_pr_with_pull_request_event(self):
        with (
            patch("main.is_fork_pr", return_value=True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
        ):
            self.assertTrue(main.is_fork_pr_with_readonly_token())

    def test_fork_pr_with_pull_request_target_event(self):
        """pull_request_target has write token — not considered read-only."""
        with (
            patch("main.is_fork_pr", return_value=True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
        ):
            self.assertFalse(main.is_fork_pr_with_readonly_token())

    def test_same_repo_not_fork(self):
        with (
            patch("main.is_fork_pr", return_value=False),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
        ):
            self.assertFalse(main.is_fork_pr_with_readonly_token())


class TestIsForkPr(unittest.TestCase):
    def test_no_event_path(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_EVENT_PATH", None)
            result = main.is_fork_pr()
        self.assertFalse(result)

    def test_same_repo_not_fork(self):
        event = {
            "pull_request": {
                "head": {"repo": {"full_name": "owner/repo"}},
                "base": {"repo": {"full_name": "owner/repo"}},
            }
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as file_obj:
            json.dump(event, file_obj)
            event_path = file_obj.name
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": event_path}):
            result = main.is_fork_pr()
        self.assertFalse(result)
        os.unlink(event_path)

    def test_different_repo_is_fork(self):
        event = {
            "pull_request": {
                "head": {"repo": {"full_name": "fork-owner/repo"}},
                "base": {"repo": {"full_name": "owner/repo"}},
            }
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as file_obj:
            json.dump(event, file_obj)
            event_path = file_obj.name
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": event_path}):
            result = main.is_fork_pr()
        self.assertTrue(result)
        os.unlink(event_path)


class TestLogErrorAndExit(unittest.TestCase):
    def test_exits_with_specified_code(self):
        with self.assertRaises(SystemExit) as ctx:
            main.log_error_and_exit(0, [pass_scope()])
        self.assertEqual(ctx.exception.code, 0)

    def test_failure_prints_error_summary(self):
        with (
            patch("builtins.print") as mock_print,
            self.assertRaises(SystemExit),
        ):
            main.log_error_and_exit(1, [fail_scope()])
        printed = mock_print.call_args[0][0]
        self.assertIn("::error::commit-check found 1 failure.", printed)


class TestMain(unittest.TestCase):
    def test_success_path(self):
        with (
            patch("main.log_env_vars"),
            patch("main.run_commit_check", return_value=(0, [pass_scope()])),
            patch("main.render_step_log"),
            patch("main.set_result_output"),
            patch("main.add_job_summary", return_value=0),
            patch("main.add_pr_comments", return_value=0),
            patch("main.DRY_RUN_ENABLED", False),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        self.assertEqual(ctx.exception.code, 0)

    def test_failure_path_exits_nonzero(self):
        with (
            patch("main.log_env_vars"),
            patch("main.run_commit_check", return_value=(1, [fail_scope()])),
            patch("main.render_step_log"),
            patch("main.set_result_output"),
            patch("main.add_job_summary", return_value=1),
            patch("main.add_pr_comments", return_value=1),
            patch("main.DRY_RUN_ENABLED", False),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_dry_run_forces_zero(self):
        with (
            patch("main.log_env_vars"),
            patch("main.run_commit_check", return_value=(1, [fail_scope()])),
            patch("main.render_step_log"),
            patch("main.set_result_output"),
            patch("main.add_job_summary", return_value=1),
            patch("main.add_pr_comments", return_value=0),
            patch("main.DRY_RUN_ENABLED", True),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
