"""Unit tests for main.py."""

import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch

# GITHUB_STEP_SUMMARY is accessed via os.environ[] (not getenv) at import time,
# so we must set it before importing main.
os.environ.setdefault("GITHUB_STEP_SUMMARY", "/tmp/step_summary.txt")

import main  # noqa: E402


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


class TestRunCheckCommand(unittest.TestCase):
    def test_with_args_calls_subprocess(self):
        mock_result = MagicMock(returncode=0, stdout="")
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            rc = main.run_check_command(["--branch"], io.StringIO())
        self.assertEqual(rc, 0)
        self.assertEqual(mock_run.call_args[0][0], ["commit-check", "--branch"])

    def test_with_input_uses_text_mode(self):
        mock_result = MagicMock(returncode=0, stdout="")
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            main.run_check_command(["--message"], io.StringIO(), input_text="fix: demo")
        self.assertEqual(mock_run.call_args[1]["input"], "fix: demo")
        self.assertTrue(mock_run.call_args[1]["text"])

    def test_prints_command(self):
        mock_result = MagicMock(returncode=0, stdout="")
        with patch("main.subprocess.run", return_value=mock_result):
            with patch("builtins.print") as mock_print:
                main.run_check_command(["--branch"], io.StringIO())
        mock_print.assert_called_once_with("commit-check --branch")


class TestRunPrMessageChecks(unittest.TestCase):
    def test_single_message_pass(self):
        mock_result = MagicMock(returncode=0, stdout="")
        result_file = io.StringIO()
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            rc = main.run_pr_message_checks(["fix: something"], result_file)
        self.assertEqual(rc, 0)
        self.assertEqual(mock_run.call_args[0][0], ["commit-check", "--message"])
        self.assertEqual(mock_run.call_args[1]["input"], "fix: something")
        self.assertEqual(result_file.getvalue(), "")

    def test_failed_message_writes_header_and_output(self):
        mock_result = MagicMock(returncode=1, stdout="Commit rejected.\n")
        result_file = io.StringIO()
        with patch("main.subprocess.run", return_value=mock_result):
            rc = main.run_pr_message_checks(["fix: something"], result_file)
        self.assertEqual(rc, 1)
        self.assertIn("--- Commit 1/1: fix: something", result_file.getvalue())
        self.assertIn("Commit rejected.", result_file.getvalue())

    def test_multiple_messages_partial_failure(self):
        results = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=1, stdout="Commit rejected.\n"),
            MagicMock(returncode=0, stdout=""),
        ]
        with patch("main.subprocess.run", side_effect=results):
            rc = main.run_pr_message_checks(["ok", "bad", "ok"], io.StringIO())
        self.assertEqual(rc, 1)

    def test_empty_list(self):
        with patch("main.subprocess.run") as mock_run:
            rc = main.run_pr_message_checks([], io.StringIO())
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()

    def test_second_and_later_messages_use_no_banner(self):
        results = [
            MagicMock(returncode=1, stdout="Commit rejected.\n"),
            MagicMock(returncode=1, stdout="Type subject_imperative check failed\n"),
        ]
        with patch("main.subprocess.run", side_effect=results) as mock_run:
            main.run_pr_message_checks(["bad first", "bad second"], io.StringIO())

        self.assertEqual(
            mock_run.call_args_list[0][0][0], ["commit-check", "--message"]
        )
        self.assertEqual(
            mock_run.call_args_list[1][0][0],
            ["commit-check", "--message", "--no-banner"],
        )

    def test_second_message_prefix_uses_separator(self):
        results = [
            MagicMock(returncode=1, stdout="Commit rejected.\n"),
            MagicMock(returncode=1, stdout="Type subject_imperative check failed\n"),
        ]
        result_file = io.StringIO()
        with patch("main.subprocess.run", side_effect=results):
            main.run_pr_message_checks(["bad first", "bad second"], result_file)

        output = result_file.getvalue()
        self.assertIn("\n--- Commit 1/2: bad first\nCommit rejected.\n", output)
        self.assertIn(
            f"{main.COMMIT_SECTION_SEPARATOR}--- Commit 2/2: bad second\n",
            output,
        )
        self.assertIn("Type subject_imperative check failed\n", output)


class TestRunOtherChecks(unittest.TestCase):
    def test_empty_args_returns_zero(self):
        with patch("main.subprocess.run") as mock_run:
            rc = main.run_other_checks([], io.StringIO())
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()

    def test_with_args_returns_returncode(self):
        mock_result = MagicMock(returncode=1, stdout="branch check failed\n")
        with patch("main.subprocess.run", return_value=mock_result):
            rc = main.run_other_checks(["--branch", "--author-name"], io.StringIO())
        self.assertEqual(rc, 1)


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
    def setUp(self):
        self._orig_dir = os.getcwd()
        import tempfile

        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._orig_dir)

    def test_pr_path_calls_pr_message_checks(self):
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.get_pr_commit_messages", return_value=["fix: something"]),
            patch("main.run_pr_message_checks", return_value=0) as mock_pr,
            patch("main.run_other_checks", return_value=0),
            patch("main.run_check_command") as mock_command,
        ):
            rc = main.run_commit_check()
        self.assertEqual(rc, 0)
        mock_pr.assert_called_once()
        mock_command.assert_not_called()

    def test_pr_path_returns_nonzero_when_any_check_fails(self):
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", True),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.get_pr_commit_messages", return_value=["bad msg"]),
            patch("main.run_pr_message_checks", return_value=1),
            patch("main.run_other_checks", return_value=1),
        ):
            rc = main.run_commit_check()
        self.assertEqual(rc, 1)

    def test_non_pr_path_uses_direct_command(self):
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.get_pr_commit_messages", return_value=[]),
            patch("main.run_pr_message_checks") as mock_pr,
            patch("main.run_check_command", return_value=0) as mock_command,
        ):
            rc = main.run_commit_check()
        self.assertEqual(rc, 0)
        mock_pr.assert_not_called()
        mock_command.assert_called_once()

    def test_message_disabled_uses_direct_command(self):
        with (
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", True),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.run_pr_message_checks") as mock_pr,
            patch("main.run_check_command", return_value=0) as mock_command,
        ):
            rc = main.run_commit_check()
        self.assertEqual(rc, 0)
        mock_pr.assert_not_called()
        mock_command.assert_called_once()

    def test_result_txt_is_created(self):
        with (
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.run_check_command", return_value=0),
        ):
            main.run_commit_check()
        self.assertTrue(os.path.exists(os.path.join(self._tmpdir, "result.txt")))

    def test_other_args_excludes_message(self):
        captured_args = []

        def fake_other_checks(args, result_file):
            captured_args.extend(args)
            return 0

        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", True),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.get_pr_commit_messages", return_value=["fix: x"]),
            patch("main.run_pr_message_checks", return_value=0),
            patch("main.run_other_checks", side_effect=fake_other_checks),
        ):
            main.run_commit_check()
        self.assertNotIn("--message", captured_args)
        self.assertIn("--branch", captured_args)


class TestReadResultFile(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._orig_dir = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._orig_dir)

    def _write_result(self, content: str):
        with open("result.txt", "w", encoding="utf-8") as file_obj:
            file_obj.write(content)

    def test_empty_file_returns_none(self):
        self._write_result("")
        self.assertIsNone(main.read_result_file())

    def test_file_with_content(self):
        self._write_result("some output\n")
        self.assertEqual(main.read_result_file(), "some output")

    def test_ansi_codes_are_stripped(self):
        self._write_result("\x1b[31mError\x1b[0m: bad commit")
        self.assertEqual(main.read_result_file(), "Error: bad commit")


class TestBuildResultBody(unittest.TestCase):
    def test_success_body(self):
        self.assertEqual(main.build_result_body(None), main.SUCCESS_TITLE)

    def test_failure_body(self):
        result = main.build_result_body("bad commit")
        self.assertIn(main.FAILURE_TITLE, result)
        self.assertIn("bad commit", result)


class TestAddJobSummary(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._orig_dir = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)
        with open("result.txt", "w", encoding="utf-8"):
            pass

    def tearDown(self):
        os.chdir(self._orig_dir)

    def test_false_skips(self):
        with patch("main.JOB_SUMMARY_ENABLED", False):
            rc = main.add_job_summary()
        self.assertEqual(rc, 0)

    def test_success_writes_success_title(self):
        summary_path = os.path.join(self._tmpdir, "summary.txt")
        with (
            patch("main.JOB_SUMMARY_ENABLED", True),
            patch("main.GITHUB_STEP_SUMMARY", summary_path),
            patch("main.read_result_file", return_value=None),
        ):
            rc = main.add_job_summary()
        self.assertEqual(rc, 0)
        with open(summary_path, encoding="utf-8") as file_obj:
            content = file_obj.read()
        self.assertIn(main.SUCCESS_TITLE, content)

    def test_failure_writes_failure_title(self):
        summary_path = os.path.join(self._tmpdir, "summary.txt")
        with (
            patch("main.JOB_SUMMARY_ENABLED", True),
            patch("main.GITHUB_STEP_SUMMARY", summary_path),
            patch("main.read_result_file", return_value="bad commit message"),
        ):
            rc = main.add_job_summary()
        self.assertEqual(rc, 1)
        with open(summary_path, encoding="utf-8") as file_obj:
            content = file_obj.read()
        self.assertIn(main.FAILURE_TITLE, content)
        self.assertIn("bad commit message", content)


class TestIsForkPr(unittest.TestCase):
    def test_no_event_path(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_EVENT_PATH", None)
            result = main.is_fork_pr()
        self.assertFalse(result)

    def test_same_repo_not_fork(self):
        import tempfile

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
        import tempfile

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
            main.log_error_and_exit("# Title", None, 0)
        self.assertEqual(ctx.exception.code, 0)

    def test_with_result_text_prints_error(self):
        with (
            patch("builtins.print") as mock_print,
            self.assertRaises(SystemExit),
        ):
            main.log_error_and_exit("# Failure", "bad commit", 1)
        printed = mock_print.call_args[0][0]
        self.assertIn("::error::", printed)
        self.assertIn("bad commit", printed)


class TestMain(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._orig_dir = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)
        with open("result.txt", "w", encoding="utf-8"):
            pass

    def tearDown(self):
        os.chdir(self._orig_dir)

    def test_success_path(self):
        with (
            patch("main.log_env_vars"),
            patch("main.run_commit_check", return_value=0),
            patch("main.add_job_summary", return_value=0),
            patch("main.add_pr_comments", return_value=0),
            patch("main.DRY_RUN_ENABLED", False),
            patch("main.read_result_file", return_value=None),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        self.assertEqual(ctx.exception.code, 0)

    def test_multiple_failures_still_exit_with_one(self):
        with (
            patch("main.log_env_vars"),
            patch("main.run_commit_check", return_value=1),
            patch("main.add_job_summary", return_value=1),
            patch("main.add_pr_comments", return_value=1),
            patch("main.DRY_RUN_ENABLED", False),
            patch("main.read_result_file", return_value="bad msg"),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_dry_run_forces_zero(self):
        with (
            patch("main.log_env_vars"),
            patch("main.run_commit_check", return_value=1),
            patch("main.add_job_summary", return_value=1),
            patch("main.add_pr_comments", return_value=0),
            patch("main.DRY_RUN_ENABLED", True),
            patch("main.read_result_file", return_value=None),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
