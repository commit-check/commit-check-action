"""Unit tests for main.py."""

import importlib.metadata
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("GITHUB_STEP_SUMMARY", "/tmp/step_summary.txt")

import main  # noqa: E402

#: The report footer names the installed commit-check version, which differs
#: between a contributor's machine and CI. Golden tests pin it so they assert on
#: the report layout rather than on whatever version happens to be installed.
PINNED_VERSION = "2.13.1"
FOOTER = (
    f"_commit-check {PINNED_VERSION} · "
    "[Rules reference](https://commit-check.com/rules/)_"
)
pin_version = patch("main._commit_check_version", new=lambda: PINNED_VERSION)

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


def pass_scope(label: str = "Branch", value: str = "") -> main.ScopeResult:
    return main.ScopeResult(label=label, checks=[make_check("branch", value=value)])


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

    def test_event_range_is_preferred(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch(
                "main.get_messages_from_event_range",
                return_value=["fix: first", "feat: second"],
            ) as mock_range,
            patch("main.get_messages_from_merge_ref") as mock_merge,
            patch("main.get_messages_from_head_ref") as mock_head,
        ):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, ["fix: first", "feat: second"])
        mock_range.assert_called_once()
        mock_merge.assert_not_called()
        mock_head.assert_not_called()

    def test_merge_ref_is_next(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.get_messages_from_event_range", return_value=[]),
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
            patch("main.get_messages_from_event_range", return_value=["fix: first"]),
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
            patch("main.get_messages_from_event_range", return_value=[]),
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
                "main.get_messages_from_event_range",
                side_effect=Exception("git failed"),
            ),
        ):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, [])


class TestGitMessageReaders(unittest.TestCase):
    def test_get_messages_from_merge_ref(self):
        mock_result = MagicMock(
            returncode=0, stdout="fix: first\n\x00feat: second\n\x00"
        )
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.subprocess.run", return_value=mock_result) as mock_run,
        ):
            result = main.get_messages_from_merge_ref()
        self.assertEqual(result, ["fix: first", "feat: second"])
        self.assertEqual(
            mock_run.call_args[0][0],
            ["git", "log", "--pretty=format:%B%x00", "--reverse", "HEAD^1..HEAD^2"],
        )

    def test_merge_ref_is_never_read_on_pull_request_target(self):
        """HEAD is the base branch there; HEAD^2 belongs to some other merge."""
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
            patch("main.subprocess.run") as mock_run,
        ):
            self.assertEqual(main.get_messages_from_merge_ref(), [])
        mock_run.assert_not_called()

    def test_get_messages_from_event_range(self):
        commands: list[list[str]] = []

        def run(command, **_kwargs):
            commands.append(command)
            if command[:2] == ["git", "rev-parse"]:
                return MagicMock(returncode=0, stdout="x\n")
            return MagicMock(returncode=0, stdout="fix: first\n\x00feat: second\n\x00")

        with (
            patch("main.get_pr_base_sha", return_value="base111"),
            patch("main.get_pr_head_sha", return_value="head222"),
            patch("main.subprocess.run", side_effect=run),
        ):
            result = main.get_messages_from_event_range()
        self.assertEqual(result, ["fix: first", "feat: second"])
        self.assertIn(
            ["git", "log", "--pretty=format:%B%x00", "--reverse", "base111..head222"],
            commands,
        )

    def test_event_range_needs_both_commits_in_the_clone(self):
        with (
            patch("main.get_pr_base_sha", return_value="base111"),
            patch("main.get_pr_head_sha", return_value="head222"),
            patch("main.subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            self.assertEqual(main.get_messages_from_event_range(), [])

    def test_event_range_without_a_payload_is_empty(self):
        with (
            patch("main.get_pr_base_sha", return_value=None),
            patch("main.get_pr_head_sha", return_value=None),
            patch("main.subprocess.run") as mock_run,
        ):
            self.assertEqual(main.get_messages_from_event_range(), [])
        mock_run.assert_not_called()

    def test_a_failing_git_log_yields_no_messages(self):
        with patch("main.subprocess.run", return_value=MagicMock(returncode=128)):
            self.assertEqual(main.get_messages_from_head_ref("main"), [])

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

        def fake_other_checks(args, rev=None):
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

    SHALLOW_PR_WARNING = (
        "::warning title=commit-check::Could not list the pull request's commits "
        "(is actions/checkout using fetch-depth: 0?); only HEAD was checked"
    )

    def _run_capturing_stdout(self):
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            rc, results = main.run_commit_check()
        return rc, results, buffer.getvalue()

    def test_pr_without_enumerable_commits_warns_and_checks_head(self):
        """A shallow clone must not turn a pull request green silently.

        With fetch-depth: 1 neither HEAD^1..HEAD^2 nor origin/<base>..HEAD
        can be listed, and the fallback validates HEAD — the synthetic
        "Merge X into Y" commit, which passes CC001 by default.
        """
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.is_pr_event", return_value=True),
            patch("main.get_pr_commit_messages", return_value=[]),
            patch(
                "main.check_scope", return_value=pass_scope("Commit message")
            ) as mock_scope,
            patch("main.run_other_checks", return_value=[]),
        ):
            rc, results, output = self._run_capturing_stdout()
        self.assertEqual(rc, 0)
        self.assertIn(self.SHALLOW_PR_WARNING, output)
        mock_scope.assert_called_once_with("Commit message", ["--message"])

    def test_push_without_pr_commits_does_not_warn(self):
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.is_pr_event", return_value=False),
            patch("main.get_pr_commit_messages", return_value=[]),
            patch("main.check_scope", return_value=pass_scope("Commit message")),
            patch("main.run_other_checks", return_value=[]),
        ):
            _rc, _results, output = self._run_capturing_stdout()
        self.assertNotIn("::warning", output)

    @staticmethod
    def _fake_git_and_cli(resolves: bool):
        """subprocess.run stand-in: answers rev-parse and the CLI alike."""
        commands: list[list[str]] = []

        def run(command, **_kwargs):
            commands.append(command)
            if command[:2] == ["git", "rev-parse"]:
                return MagicMock(
                    returncode=0 if resolves else 1,
                    stdout="abc123\n" if resolves else "",
                )
            check = command[3].lstrip("-").replace("-", "_")
            return MagicMock(returncode=0, stdout=json_output(make_check(check)))

        return run, commands

    def test_pr_author_checks_read_the_branch_tip(self):
        """On refs/pull/N/merge HEAD's author is GitHub, not the contributor."""
        run, commands = self._fake_git_and_cli(resolves=True)
        with (
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", True),
            patch("main.AUTHOR_NAME_ENABLED", True),
            patch("main.AUTHOR_EMAIL_ENABLED", True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.get_pr_head_sha", return_value=None),
            patch("main.subprocess.run", side_effect=run),
        ):
            rc, results, output = self._run_capturing_stdout()
        self.assertEqual(rc, 0)
        self.assertEqual(
            [s.label for s in results], ["Branch", "Author name", "Author email"]
        )
        self.assertIn(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD^2^{commit}"], commands
        )
        self.assertIn(
            ["commit-check", "--format", "json", "--author-name", "--rev", "HEAD^2"],
            commands,
        )
        self.assertIn(
            ["commit-check", "--format", "json", "--author-email", "--rev", "HEAD^2"],
            commands,
        )
        # The branch check has no commit to point at.
        self.assertIn(["commit-check", "--format", "json", "--branch"], commands)
        self.assertNotIn("::warning", output)

    def test_pr_author_checks_prefer_the_payload_head_sha(self):
        """pull_request.head.sha names the tip for either PR event type."""
        run, commands = self._fake_git_and_cli(resolves=True)
        with (
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", True),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
            patch("main.get_pr_head_sha", return_value="deadbeefcafe"),
            patch("main.subprocess.run", side_effect=run),
        ):
            rc, results, output = self._run_capturing_stdout()
        self.assertEqual(rc, 0)
        self.assertEqual([s.status for s in results], ["pass"])
        self.assertIn(
            ["git", "rev-parse", "--verify", "--quiet", "deadbeefcafe^{commit}"],
            commands,
        )
        self.assertIn(
            [
                "commit-check",
                "--format",
                "json",
                "--author-name",
                "--rev",
                "deadbeefcafe",
            ],
            commands,
        )
        self.assertNotIn("::warning", output)

    def test_pr_author_checks_are_skipped_on_a_shallow_clone(self):
        """HEAD's author is GitHub's merge commit: skip rather than grade it."""
        run, commands = self._fake_git_and_cli(resolves=False)
        with (
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", True),
            patch("main.AUTHOR_NAME_ENABLED", True),
            patch("main.AUTHOR_EMAIL_ENABLED", True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.get_pr_head_sha", return_value=None),
            patch("main.subprocess.run", side_effect=run),
        ):
            rc, results, output = self._run_capturing_stdout()
        self.assertEqual(rc, 0)
        self.assertEqual(
            [(s.label, s.status) for s in results],
            [("Author name", "skip"), ("Author email", "skip"), ("Branch", "pass")],
        )
        self.assertEqual(
            results[0].checks,
            [
                {
                    "rule_id": "CC101",
                    "check": "author_name",
                    "status": "skip",
                    "value": "",
                    "error": "",
                    "suggest": "",
                    "docs_url": "",
                }
            ],
        )
        self.assertEqual(results[1].checks[0]["rule_id"], "CC102")
        self.assertFalse([c for c in commands if "--author-name" in c], commands)
        self.assertFalse([c for c in commands if "--author-email" in c], commands)
        self.assertIn(["commit-check", "--format", "json", "--branch"], commands)
        warning = [ln for ln in output.splitlines() if ln.startswith("::warning")]
        self.assertEqual(len(warning), 1, output)
        self.assertTrue(warning[0].startswith("::warning title=commit-check::"))
        self.assertIn("Could not resolve the pull request's head commit", warning[0])
        self.assertIn("is actions/checkout using fetch-depth: 0?", warning[0])
        self.assertIn("they were skipped", warning[0])

    def test_pull_request_target_never_uses_head2(self):
        """On pull_request_target HEAD is the base branch; HEAD^2 is unrelated."""
        run, commands = self._fake_git_and_cli(resolves=True)
        with (
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", True),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
            patch("main.get_pr_head_sha", return_value=None),
            patch("main.subprocess.run", side_effect=run),
        ):
            rc, results, output = self._run_capturing_stdout()
        self.assertEqual(rc, 0)
        self.assertEqual(
            [(s.label, s.status) for s in results], [("Author name", "skip")]
        )
        self.assertFalse([c for c in commands if "HEAD^2^{commit}" in c], commands)
        self.assertFalse([c for c in commands if c[0] == "commit-check"], commands)
        self.assertIn("::warning title=commit-check::", output)

    def test_push_author_checks_never_pass_rev(self):
        run, commands = self._fake_git_and_cli(resolves=True)
        with (
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", True),
            patch("main.AUTHOR_EMAIL_ENABLED", True),
            patch("main.is_pr_event", return_value=False),
            patch("main.subprocess.run", side_effect=run),
        ):
            _rc, _results, output = self._run_capturing_stdout()
        self.assertFalse([c for c in commands if c[0] == "git"], commands)
        self.assertFalse([c for c in commands if "--rev" in c], commands)
        self.assertNotIn("::warning", output)


class TestPrHeadRev(unittest.TestCase):
    def test_payload_head_sha_wins_when_the_clone_has_it(self):
        with (
            patch("main.get_pr_head_sha", return_value="abc123"),
            patch(
                "main.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            self.assertEqual(main.pr_head_rev(), "abc123")
        self.assertEqual(
            mock_run.call_args[0][0],
            ["git", "rev-parse", "--verify", "--quiet", "abc123^{commit}"],
        )

    def test_pull_request_falls_back_to_head2(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.get_pr_head_sha", return_value=None),
            patch(
                "main.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            self.assertEqual(main.pr_head_rev(), "HEAD^2")
        self.assertEqual(
            mock_run.call_args[0][0],
            ["git", "rev-parse", "--verify", "--quiet", "HEAD^2^{commit}"],
        )

    def test_pull_request_target_does_not_fall_back_to_head2(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
            patch("main.get_pr_head_sha", return_value=None),
            patch(
                "main.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            self.assertIsNone(main.pr_head_rev())
        mock_run.assert_not_called()

    def test_unfetched_payload_sha_on_pull_request_target_returns_none(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
            patch("main.get_pr_head_sha", return_value="abc123"),
            patch("main.subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            self.assertIsNone(main.pr_head_rev())

    def test_shallow_clone_returns_none(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.get_pr_head_sha", return_value=None),
            patch("main.subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            self.assertIsNone(main.pr_head_rev())

    def test_missing_git_returns_none(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.get_pr_head_sha", return_value=None),
            patch("main.subprocess.run", side_effect=OSError("no git")),
        ):
            self.assertIsNone(main.pr_head_rev())


class TestCheckoutHint(unittest.TestCase):
    def test_pull_request_names_fetch_depth(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            self.assertEqual(main.checkout_hint(), main.SHALLOW_CHECKOUT_HINT)

    def test_pull_request_target_names_the_checkout(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}):
            self.assertEqual(main.checkout_hint(), main.TARGET_CHECKOUT_HINT)
            self.assertIn("refs/pull/<number>/merge", main.checkout_hint())


class TestGetPrHeadSha(unittest.TestCase):
    def test_reads_the_head_sha_from_the_event(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"pull_request": {"head": {"sha": "abc123"}}}, f)
            event_path = f.name
        try:
            with patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_NAME": "pull_request_target",
                    "GITHUB_EVENT_PATH": event_path,
                },
            ):
                self.assertEqual(main.get_pr_head_sha(), "abc123")
        finally:
            os.unlink(event_path)

    def test_reads_the_base_sha_from_the_event(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"pull_request": {"base": {"sha": "base111"}}}, f)
            event_path = f.name
        try:
            with patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": event_path},
            ):
                self.assertEqual(main.get_pr_base_sha(), "base111")
                self.assertIsNone(main.get_pr_head_sha())
        finally:
            os.unlink(event_path)

    def test_not_a_pr_event_returns_none(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}):
            self.assertIsNone(main.get_pr_head_sha())
            self.assertIsNone(main.get_pr_base_sha())

    def test_missing_event_path_returns_none(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            os.environ.pop("GITHUB_EVENT_PATH", None)
            self.assertIsNone(main.get_pr_head_sha())

    def test_unreadable_event_returns_none(self):
        with patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_EVENT_PATH": "/nonexistent.json",
            },
        ):
            self.assertIsNone(main.get_pr_head_sha())


class TestCommitCheckVersionPin(unittest.TestCase):
    """The warn rendering is inert against an engine that never emits it.

    commit-check reports ``"status": "warn"`` from 2.17.0; the action pinned
    2.16.0 for a release after shipping the rendering, so nobody could ever
    see it. Both the installed package and the pin have to keep up.
    """

    MINIMUM = (2, 17, 0)

    @staticmethod
    def _parse(version: str) -> tuple[int, ...]:
        match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
        assert match, f"unparsable commit-check version: {version!r}"
        return tuple(int(part) for part in match.groups())

    def test_installed_commit_check_can_report_warnings(self):
        try:
            version = importlib.metadata.version("commit-check")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("commit-check is not installed")
        self.assertGreaterEqual(
            self._parse(version),
            self.MINIMUM,
            f"installed commit-check {version} predates the warn status",
        )

    def test_requirements_pin_can_report_warnings(self):
        here = os.path.dirname(os.path.abspath(main.__file__))
        with open(os.path.join(here, "requirements.txt"), encoding="utf-8") as f:
            pins = dict(
                line.strip().split("==", 1)
                for line in f
                if "==" in line and not line.startswith("#")
            )
        self.assertGreaterEqual(self._parse(pins["commit-check"]), self.MINIMUM)


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
        self.assertIn("      CC001 message", output)
        self.assertIn("value: bad message", output)
        self.assertIn("Suggest: Use <type>(<scope>): <description>", output)
        self.assertIn("Docs: https://commit-check.com/rules/#cc001", output)
        # The annotation names the scope, which the title alone cannot carry.
        self.assertIn(
            "::error title=CC001 message::Commit 1/1: The commit message should "
            "follow Conventional Commits.",
            output,
        )

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
        scope = main.ScopeResult(
            label="Commit 1/1",
            checks=[
                make_check(
                    "message",
                    status="fail",
                    error="first line\nsecond line with 100% certainty",
                )
            ],
        )
        output = self._run([scope])
        annotation = [ln for ln in output.splitlines() if ln.startswith("::error")][0]
        self.assertNotIn("\n", annotation.removeprefix("::error "))
        self.assertIn("first line", annotation)

    def test_pass_scopes_show_the_checked_value(self):
        output = self._run([pass_scope("Branch", value="feature/add-login")])
        self.assertIn("✔ Branch (feature/add-login)", output)

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
        # No "(0 failures)": there is no check list to count, and claiming zero
        # next to a ✖ reads as a contradiction.
        self.assertIn("✖ Branch", output)
        self.assertNotIn("0 failures", output)
        self.assertIn("unexpected output", output)
        self.assertIn("::error title=commit-check: Branch::", output)

    def test_dry_run_downgrades_annotations_and_prints_verdict(self):
        """Dry-run still reports every failure, but nothing may say "error".

        The exit code is forced to 0, so an ::error annotation would count
        toward the run's error total on a green job, and log_error_and_exit
        prints nothing for a zero exit — leaving the log with no verdict.
        """
        with patch("main.DRY_RUN_ENABLED", True):
            output = self._run([fail_scope("Commit 1/1"), pass_scope("Branch")])
        self.assertNotIn("::error", output)
        self.assertIn(
            "::warning title=CC001 message::Commit 1/1: The commit message should "
            "follow Conventional Commits.",
            output,
        )
        self.assertIn(
            "commit-check (dry-run): 1 of 2 checks failed; not failing the job",
            output,
        )
        self.assertNotIn("all checks passed", output)

    def test_dry_run_without_failures_prints_the_usual_verdict(self):
        with patch("main.DRY_RUN_ENABLED", True):
            output = self._run([pass_scope("Branch")])
        self.assertIn("✔ commit-check: all checks passed", output)
        self.assertNotIn("dry-run", output)


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
        body = main.render_report(results)
        self.assertEqual(
            body,
            f"{main.COMMENT_MARKER}\n"
            f"{main.REPORT_TITLE}\n"
            "\n"
            "✅ **All 4 checks passed**\n"
            "\n"
            "<details>\n"
            "<summary>Show all 4 checks</summary>\n"
            "\n"
            "```text\n"
            "Commit message\n"
            "  ✔ PR title (feat: add login page)\n"
            "  ✔ Commit 1/2 (feat: add user auth)\n"
            "  ✔ Commit 2/2 (fix: resolve timeout)\n"
            "Branch\n"
            "  ✔ Branch (feature/add-login)\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    @pin_version
    def test_failure_golden_output(self):
        """Pin the full failure report: failed row in the table, all in details."""
        results = [
            pass_scope("PR title", value="feat: add login page"),
            fail_scope("Commit 2/2"),
            pass_scope("Branch", value="feature/add-login"),
        ]
        body = main.render_report(results)
        self.assertEqual(
            body,
            f"{main.COMMENT_MARKER}\n"
            f"{main.REPORT_TITLE}\n"
            "\n"
            "❌ **1 of 3 checks failed**\n"
            "\n"
            "| Scope | Checked value | Failed checks |\n"
            "|---|---|---|\n"
            "| Commit 2/2 | `bad message` | "
            "[CC001 message](https://commit-check.com/rules/#cc001) |\n"
            "\n"
            "<details>\n"
            "<summary>Show all 3 checks</summary>\n"
            "\n"
            "```text\n"
            "Commit message\n"
            "  ✔ PR title (feat: add login page)\n"
            "  ✖ Commit 2/2 (1 failure)\n"
            "      CC001 message\n"
            "        value: bad message\n"
            "        The commit message should follow Conventional Commits.\n"
            "        Suggest: Use <type>(<scope>): <description>\n"
            "Branch\n"
            "  ✔ Branch (feature/add-login)\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

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
        body = main.render_report([pass_scope("Branch"), two_failures])
        self.assertIn("❌ **1 of 2 checks failed**", body)
        self.assertIn("<summary>Show all 2 checks</summary>", body)
        # Both failing rules are still named, in the table and the details.
        self.assertIn("CC001 message", body)
        self.assertIn("CC005 subject-min-length", body)

    def test_total_does_not_grow_with_the_number_of_rules(self):
        """Adding rules to one scope must not change the headline total."""
        one_rule = [pass_scope("Branch")]
        many_rules = [
            main.ScopeResult(
                label="Branch",
                checks=[
                    make_check(f"rule_{i}", rule_id=f"CC{i:03d}") for i in range(9)
                ],
            )
        ]
        self.assertIn("✅ **All 1 check passed**", main.render_report(one_rule))
        self.assertIn("✅ **All 1 check passed**", main.render_report(many_rules))

    def test_table_has_no_constant_result_column(self):
        """Only failures reach the table, so a result column would never vary."""
        body = main.render_job_summary([fail_scope("Commit 1/1")])
        self.assertIn("| Scope | Checked value | Failed checks |", body)
        self.assertNotIn("| Result |", body)

    def test_body_opens_with_hidden_marker(self):
        body = main.render_job_summary([pass_scope("Branch")])
        self.assertTrue(body.startswith(main.COMMENT_MARKER))

    def test_all_pass(self):
        body = main.render_job_summary([pass_scope("Branch", value="main")])
        self.assertIn(main.REPORT_TITLE, body)
        self.assertIn("✅ **All 1 check passed**", body)
        self.assertIn("<details>", body)
        self.assertIn("<summary>Show all 1 check</summary>", body)
        self.assertIn("```text", body)
        self.assertIn("Branch", body)
        self.assertIn("  ✔ Branch (main)", body)

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
        # Group headers in the details block mirror the step log ordering.
        self.assertLess(body.index("Commit message"), body.index("Branch"))
        self.assertLess(body.index("Branch"), body.index("Author"))
        self.assertIn("  ✔ PR title (feat: add login page)", body)
        self.assertIn("  ✔ Branch (feature/pr-12)", body)
        self.assertIn("  ✔ Author email (jane@example.com)", body)

    def test_all_pass_truncates_long_values(self):
        long_value = "x" * 200
        body = main.render_job_summary([pass_scope("Commit 1/1", value=long_value)])
        self.assertIn(f"  ✔ Commit 1/1 ({'x' * 57}...)", body)

    def test_all_pass_without_value_shows_plain_label(self):
        body = main.render_job_summary([pass_scope("Branch")])
        self.assertIn("  ✔ Branch", body)
        self.assertNotIn("  ✔ Branch (", body)

    def test_failure_renders_table_with_rule_links(self):
        body = main.render_job_summary([fail_scope("Commit 1/1")])
        self.assertIn(main.REPORT_TITLE, body)
        self.assertIn("❌ **1 of 1 check failed**", body)
        self.assertIn("| Scope | Checked value | Failed checks |", body)
        self.assertIn(
            "| Commit 1/1 | `bad message` | "
            "[CC001 message](https://commit-check.com/rules/#cc001) |",
            body,
        )
        self.assertIn("<details>", body)
        self.assertIn("<summary>Show all 1 check</summary>", body)
        self.assertIn("```text", body)
        self.assertIn("✖ Commit 1/1 (1 failure)", body)
        self.assertIn("      CC001 message", body)
        # The failing value appears in full; the table truncates at 60.
        self.assertIn("        value: bad message", body)
        self.assertIn("        Suggest: Use <type>(<scope>): <description>", body)
        self.assertIn("[Rules reference](https://commit-check.com/rules/)", body)

    def test_failure_details_show_all_scopes_and_values(self):
        results = [
            fail_scope("Commit 1/2"),
            pass_scope("Commit 2/2", value="fix: resolve timeout"),
        ]
        body = main.render_job_summary(results)
        self.assertIn("✔ Commit 2/2 (fix: resolve timeout)", body)
        self.assertIn("✖ Commit 1/2 (1 failure)", body)

    def test_pass_scope_renders_checkmark_without_value(self):
        body = main.render_job_summary([pass_scope("Branch"), fail_scope("Commit 1/1")])
        # Pass scopes stay out of the table; the details block carries them.
        table = body.split("<details>")[0]
        self.assertNotIn("| Branch |", table)
        self.assertIn("| Commit 1/1 | `bad message` |", table)
        self.assertIn("✔ Branch", body)


class TestRenderPrComment(unittest.TestCase):
    def test_all_pass_matches_job_summary(self):
        comment = main.render_pr_comment([pass_scope("Branch")])
        summary = main.render_job_summary([pass_scope("Branch")])
        self.assertEqual(comment, summary)
        self.assertTrue(comment.startswith(main.COMMENT_MARKER))
        self.assertIn("✅ **All 1 check passed**", comment)

    def test_failure_matches_job_summary(self):
        comment = main.render_pr_comment([fail_scope("Commit 1/1")])
        summary = main.render_job_summary([fail_scope("Commit 1/1")])
        self.assertEqual(comment, summary)
        self.assertTrue(comment.startswith(main.COMMENT_MARKER))
        self.assertIn("❌ **1 of 1 check failed**", comment)
        self.assertIn("| Scope | Checked value | Failed checks |", comment)


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
        self.assertIn("✅ **All 1 check passed**", content)

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
        self.assertIn("| Scope | Checked value | Failed checks |", content)
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

    def test_push_event_skips_comment_without_warning(self):
        """A push has no PR to comment on, and that is not a problem.

        It used to reach get_pr_number(), which raised, so every push run
        with pr-comments enabled carried a "Unable to post PR comment"
        warning annotation.
        """
        event_path = os.path.join(tempfile.mkdtemp(), "event.json")
        with open(event_path, "w", encoding="utf-8") as f:
            json.dump({"ref": "refs/heads/main", "pusher": {"name": "octocat"}}, f)
        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_NAME": "push",
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_EVENT_PATH": event_path,
                    "GITHUB_TOKEN": "token",
                    "GITHUB_REPOSITORY": "owner/repo",
                },
            ),
            patch("main.get_pr_number") as mock_number,
            patch("builtins.print") as mock_print,
        ):
            rc = main.add_pr_comments([fail_scope()])
        self.assertEqual(rc, 0)
        mock_number.assert_not_called()
        printed = [
            call[0][0]
            for call in mock_print.call_args_list
            if call[0] and isinstance(call[0][0], str)
        ]
        self.assertFalse(
            [line for line in printed if line.startswith("::warning")], printed
        )

    def test_fork_pr_skips_comment_and_warns(self):
        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
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
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
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
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_REF": "refs/pull/12/merge",
                },
            ),
            patch.dict(sys.modules, {"github": github_module}),
        ):
            rc = main.add_pr_comments([fail_scope()])
        self.assertEqual(rc, 1)
        self.assertEqual(mock_pull_request.create_comment.call_count, 1)
        body = mock_pull_request.create_comment.call_args[1]["body"]
        self.assertTrue(body.startswith(main.COMMENT_MARKER))
        self.assertIn("| Scope | Checked value | Failed checks |", body)

    def test_updates_existing_comment_when_changed(self):
        # A report from an earlier version: no marker, and posted by the bot,
        # which is what makes it safe to adopt.
        old_comment = MagicMock(body="# Commit-Check ❌ 0 failures")
        old_comment.user.type = "Bot"
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
                    "GITHUB_EVENT_NAME": "pull_request",
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
                    "GITHUB_EVENT_NAME": "pull_request",
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


class _StubGithubException(Exception):
    """Stands in for github.GithubException, which is mocked away in these tests.

    The real class has to be a genuine exception type or the ``except`` clause
    in add_pr_comments raises TypeError before the handler is reached.
    """

    def __init__(self, status, data=None):
        super().__init__(f"status {status}")
        self.status = status
        # Stored as given, exactly as PyGithub does. Coercing a falsy payload
        # to {} here would hide the very shapes the handler has to survive.
        self.data = data


class TestAddPrCommentsFailures(unittest.TestCase):
    """Posting the comment is best-effort, but it must never fail silently.

    Every branch here returns 0 so the step stays green — which is the point:
    without an annotation the run is green, the comment is absent, and nothing
    on the page says why.
    """

    def _run(self, side_effect):
        mock_pull_request = MagicMock()
        mock_pull_request.get_comments.return_value = []
        mock_pull_request.create_comment.side_effect = side_effect
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_pull_request

        github_module = MagicMock()
        github_module.GithubException = _StubGithubException
        github_module.Github.return_value.get_repo.return_value = mock_repo

        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch("main.is_fork_pr_with_readonly_token", return_value=False),
            patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "token",
                    "GITHUB_REPOSITORY": "owner/repo",
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_REF": "refs/pull/12/merge",
                },
            ),
            patch.dict(sys.modules, {"github": github_module}),
            patch("builtins.print") as mock_print,
        ):
            rc = main.add_pr_comments([fail_scope()])
        printed = [
            call[0][0]
            for call in mock_print.call_args_list
            if call[0] and isinstance(call[0][0], str)
        ]
        return rc, printed

    def test_forbidden_names_the_permission_that_actually_grants_this(self):
        rc, printed = self._run(
            _StubGithubException(403, {"message": "Resource not accessible"})
        )
        self.assertEqual(rc, 0)
        warning = next(w for w in printed if "::warning::" in w)
        # pull-requests, not issues: a PR comment is written with the
        # pull-requests scope, and this hint is the only guidance a user gets.
        self.assertIn("pull-requests: write", warning)
        self.assertNotIn("issues: write", warning)
        # The API's own wording is what tells the user which resource was
        # refused, so the hint has to carry it through.
        self.assertIn("Resource not accessible", warning)

    def test_forbidden_with_a_non_mapping_payload_still_warns(self):
        # data is whatever the body decoded to: None when empty, a str when it
        # is not JSON. Reaching for .get on either raises inside the handler
        # and escapes the function, which would fail the step.
        for payload in (None, "forbidden"):
            with self.subTest(payload=payload):
                rc, printed = self._run(_StubGithubException(403, payload))
                self.assertEqual(rc, 0)
                warning = next(w for w in printed if "::warning::" in w)
                self.assertIn("pull-requests: write", warning)
                self.assertIn("status 403", warning)

    def test_other_api_errors_are_annotated(self):
        rc, printed = self._run(_StubGithubException(500, {"message": "boom"}))
        self.assertEqual(rc, 0)
        self.assertTrue(
            any("::warning::" in w for w in printed),
            f"a failed post must be annotated, got: {printed}",
        )

    def test_unexpected_errors_are_annotated(self):
        rc, printed = self._run(RuntimeError("network went away"))
        self.assertEqual(rc, 0)
        self.assertTrue(
            any("::warning::" in w and "network went away" in w for w in printed),
            f"a failed post must be annotated, got: {printed}",
        )


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


class TestFindOwnComments(unittest.TestCase):
    """Comment ownership: the action must never destroy a human's comment."""

    @staticmethod
    def _comment(body: str, user_type: str = "Bot") -> MagicMock:
        comment = MagicMock()
        comment.body = body
        comment.user.type = user_type
        return comment

    def test_marked_comment_is_updated_and_older_ones_deleted(self):
        first = self._comment(f"{main.COMMENT_MARKER}\nold")
        second = self._comment(f"{main.COMMENT_MARKER}\nnewer")
        target, stale = main._find_own_comments([first, second])
        self.assertIs(target, second)
        self.assertEqual(stale, [first])

    def test_human_comment_with_the_old_title_is_never_deleted(self):
        """A person can type '# Commit Check'; deleting on that is destructive."""
        human = self._comment("# Commit Check\nwhy is this failing?", user_type="User")
        mine = self._comment(f"{main.COMMENT_MARKER}\nreport")
        target, stale = main._find_own_comments([human, mine])
        self.assertIs(target, mine)
        self.assertEqual(stale, [])

    def test_human_comment_with_the_old_title_is_not_adopted(self):
        human = self._comment("# Commit Check\nwhy is this failing?", user_type="User")
        target, stale = main._find_own_comments([human])
        self.assertIsNone(target)
        self.assertEqual(stale, [])

    def test_bot_comment_from_an_older_version_is_adopted(self):
        legacy = self._comment("# Commit-Check\nold report")
        target, stale = main._find_own_comments([legacy])
        self.assertIs(target, legacy)
        self.assertEqual(stale, [])

    def test_no_comments_yields_nothing_to_update(self):
        target, stale = main._find_own_comments([])
        self.assertIsNone(target)
        self.assertEqual(stale, [])


def skip_scope(label: str = "PR title") -> main.ScopeResult:
    """A scope whose every rule declined to run (e.g. author in ignore_authors)."""
    return main.ScopeResult(
        label=label,
        checks=[make_check("message", status="skip", rule_id="CC001", value="")],
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
            f"{main.REPORT_TITLE}\n"
            "\n"
            "⊘ **All 2 checks skipped** — nothing was validated\n"
            "\n"
            "<details>\n"
            "<summary>Show all 2 checks</summary>\n"
            "\n"
            "```text\n"
            "Commit message\n"
            "  ⊘ PR title (skipped)\n"
            "  ⊘ Commit 1/1 (skipped)\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    def test_partial_skip_does_not_claim_all_passed(self):
        results = [pass_scope("Branch", value="main"), skip_scope("PR title")]
        body = main.render_report(results)
        self.assertIn("✅ **1 of 2 checks passed**, 1 skipped", body)
        self.assertNotIn("All 2 checks passed", body)

    def test_failure_still_wins_over_skips(self):
        results = [fail_scope("Commit 1/1"), skip_scope("PR title")]
        body = main.render_report(results)
        self.assertIn("❌ **1 of 2 checks failed**", body)

    def test_skipped_scope_reports_no_checked_value(self):
        """The ⊘ line carries no value, because nothing was examined."""
        body = main.render_report([skip_scope("PR title")])
        self.assertIn("  ⊘ PR title (skipped)", body)

    def test_older_engine_without_skip_is_unaffected(self):
        """Back-compat: engines that only emit pass/fail render as before."""
        results = [pass_scope("Branch", value="main")]
        self.assertIn("✅ **All 1 check passed**", main.render_report(results))


def warn_scope(label: str = "Branch", value: str = "jsmith/fix-x") -> main.ScopeResult:
    """A scope with one rule the config lists under the top-level ``warn``."""
    return main.ScopeResult(
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
        still belong on every surface: the count, both tables, and the
        details block. Filtering any of those on ``scope.status == "warn"``
        drops this scope's warning the moment its sibling rule fails.
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
        self.assertIn("❌ **1 of 1 check failed**, 1 warning", body)
        self.assertIn("| Scope | Checked value | Failed checks |", body)
        self.assertIn("[CC202 merge-base](https://commit-check.com/rules/#cc202)", body)
        self.assertIn("| Scope | Checked value | Warnings |", body)
        self.assertIn("[CC201 branch](https://commit-check.com/rules/#cc201)", body)
        self.assertIn("  ✖ Branch (1 failure)", body)
        self.assertIn("      CC202 merge-base", body)
        self.assertIn("  ⚠ Branch (1 warning)", body)
        self.assertIn("      CC201 branch", body)

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

    def test_overall_status_and_exit_code_treat_a_warning_as_a_pass(self):
        results = [pass_scope("PR title"), warn_scope()]
        self.assertEqual(main.overall_status(results), "pass")
        self.assertEqual(main.exit_code_for(results), 0)

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
            f"{main.REPORT_TITLE}\n"
            "\n"
            "✅ **1 of 2 checks passed**, 1 warning\n"
            "\n"
            "| Scope | Checked value | Warnings |\n"
            "|---|---|---|\n"
            "| Branch | `jsmith/fix-x` | "
            "[CC201 branch](https://commit-check.com/rules/#cc201) |\n"
            "\n"
            "<details>\n"
            "<summary>Show all 2 checks</summary>\n"
            "\n"
            "```text\n"
            "Commit message\n"
            "  ✔ PR title (feat: add login page)\n"
            "Branch\n"
            "  ⚠ Branch (1 warning)\n"
            "      CC201 branch\n"
            "        value: jsmith/fix-x\n"
            "        The branch should follow Conventional Branch.\n"
            "        Suggest: Use <type>/<description>\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    def test_warning_and_skip_both_fold_into_the_title_in_order(self):
        results = [pass_scope("PR title"), warn_scope(), skip_scope("Author")]
        body = main.render_report(results)
        self.assertIn("✅ **1 of 3 checks passed**, 1 warning, 1 skipped", body)

    def test_a_failure_still_fails_and_names_the_warning_too(self):
        results = [fail_scope("Commit 1/1"), warn_scope()]
        body = main.render_report(results)
        self.assertIn("❌ **1 of 2 checks failed**, 1 warning", body)
        self.assertIn("| Scope | Checked value | Failed checks |", body)
        self.assertIn("| Scope | Checked value | Warnings |", body)
        self.assertLess(
            body.index("Failed checks"),
            body.index("| Scope | Checked value | Warnings |"),
        )

    def test_step_log_prints_a_warning_annotation_not_an_error(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.render_step_log([warn_scope()])
        out = buf.getvalue()
        self.assertIn("::warning title=CC201 branch::Branch: The branch should ", out)
        self.assertNotIn("::error", out)
        self.assertIn("  ⚠ Branch (1 warning)", out)

    def test_step_log_friendly_line_mentions_the_warning(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.render_step_log([pass_scope("PR title"), warn_scope()])
        out = buf.getvalue()
        self.assertIn("1 of 2 checks passed, 1 warning", out)
        self.assertNotIn("all checks passed", out)


class TestSkipCompletionSemantics(unittest.TestCase):
    """A skipped run must not be treated as a failing one.

    run_commit_check has always failed only on "fail", but the completion
    paths each asked `all(status == "pass")` instead. That was equivalent
    only while pass and fail were the only statuses; with skip added, a
    skipped-only run exited 1 and reported "fail" while rendering ⊘.
    """

    def test_skipped_only_run_is_not_a_failure(self):
        self.assertEqual(main.exit_code_for([skip_scope(), skip_scope("Branch")]), 0)

    def test_partial_skip_is_not_a_failure(self):
        self.assertEqual(main.exit_code_for([pass_scope(), skip_scope()]), 0)

    def test_a_real_failure_still_fails(self):
        self.assertEqual(main.exit_code_for([fail_scope(), skip_scope()]), 1)

    def test_result_output_status_reports_skip(self):
        self.assertEqual(
            main.overall_status([skip_scope(), skip_scope("Branch")]), "skip"
        )

    def test_result_output_status_of_partial_skip_is_pass(self):
        self.assertEqual(main.overall_status([pass_scope(), skip_scope()]), "pass")

    def test_set_result_output_emits_skip(self):
        """The `result` action output must not call a skipped run failed."""
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            out_path = f.name
        try:
            with patch.dict(os.environ, {"GITHUB_OUTPUT": out_path}):
                main.set_result_output([skip_scope(), skip_scope("Branch")])
            written = open(out_path, encoding="utf-8").read()
        finally:
            os.unlink(out_path)
        payload = json.loads(written.split("result<<EOF\n", 1)[1].rsplit("\nEOF", 1)[0])
        self.assertEqual(payload["status"], "skip")

    def test_add_job_summary_returns_success_for_a_skipped_run(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            summary_path = f.name
        try:
            with (
                patch.object(main, "JOB_SUMMARY_ENABLED", True),
                patch.object(main, "GITHUB_STEP_SUMMARY", summary_path),
            ):
                rc = main.add_job_summary([skip_scope(), skip_scope("Branch")])
        finally:
            os.unlink(summary_path)
        self.assertEqual(rc, 0)


class TestSkipRenderingEdgeCases(unittest.TestCase):
    def test_failure_table_omits_skipped_scopes(self):
        """A skipped scope has no failed checks, so it must not get a row.

        It previously contributed a row with an empty value and an empty
        rule list — a blank accusation under "Failed checks".
        """
        table = main._markdown_table([fail_scope("Commit 1/1"), skip_scope("PR title")])
        self.assertIn("| Commit 1/1 |", table)
        self.assertNotIn("PR title", table)
        # header + separator + exactly one data row
        self.assertEqual(len(table.splitlines()), 3)

    def test_empty_result_set_is_not_reported_as_all_skipped(self):
        """`skipped == total` is trivially true for no scopes at all."""
        body = main.render_report([])
        self.assertNotIn("skipped", body)
        self.assertIn("✅ **All 0 checks passed**", body)

    def test_step_log_partial_skip_does_not_claim_all_passed(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.render_step_log([pass_scope(), skip_scope()])
        out = buf.getvalue()
        self.assertIn("1 of 2 checks passed, 1 skipped", out)
        self.assertNotIn("all checks passed", out)
