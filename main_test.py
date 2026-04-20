"""Unit tests for main.py"""

import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# GITHUB_STEP_SUMMARY is accessed via os.environ[] (not getenv) at import time,
# so we must set it before importing main.
os.environ.setdefault("GITHUB_STEP_SUMMARY", "/tmp/step_summary.txt")

import main  # noqa: E402


class TestBuildCheckArgs(unittest.TestCase):
    def test_all_true(self):
        result = main.build_check_args("true", "true", "true", "true")
        self.assertEqual(
            result, ["--message", "--branch", "--author-name", "--author-email"]
        )

    def test_all_false(self):
        result = main.build_check_args("false", "false", "false", "false")
        self.assertEqual(result, [])

    def test_message_only(self):
        result = main.build_check_args("true", "false", "false", "false")
        self.assertEqual(result, ["--message"])

    def test_branch_only(self):
        result = main.build_check_args("false", "true", "false", "false")
        self.assertEqual(result, ["--branch"])

    def test_author_name_and_email(self):
        result = main.build_check_args("false", "false", "true", "true")
        self.assertEqual(result, ["--author-name", "--author-email"])

    def test_message_and_branch(self):
        result = main.build_check_args("true", "true", "false", "false")
        self.assertEqual(result, ["--message", "--branch"])


class TestRunPrMessageChecks(unittest.TestCase):
    def _make_file(self):
        return io.StringIO()

    def test_single_message_pass(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            rc = main.run_pr_message_checks(["fix: something"], self._make_file())
        self.assertEqual(rc, 0)
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        self.assertIn("--message", call_kwargs[0][0])
        self.assertEqual(call_kwargs[1]["input"], "fix: something")

    def test_single_message_fail(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("main.subprocess.run", return_value=mock_result):
            rc = main.run_pr_message_checks(["bad commit"], self._make_file())
        self.assertEqual(rc, 1)

    def test_multiple_messages_partial_failure(self):
        results = [
            MagicMock(returncode=0),
            MagicMock(returncode=1),
            MagicMock(returncode=0),
        ]
        with patch("main.subprocess.run", side_effect=results):
            rc = main.run_pr_message_checks(["ok", "bad", "ok"], self._make_file())
        self.assertEqual(rc, 1)

    def test_multiple_messages_all_fail(self):
        results = [MagicMock(returncode=1), MagicMock(returncode=1)]
        with patch("main.subprocess.run", side_effect=results):
            rc = main.run_pr_message_checks(["bad1", "bad2"], self._make_file())
        self.assertEqual(rc, 1)

    def test_empty_list(self):
        with patch("main.subprocess.run") as mock_run:
            rc = main.run_pr_message_checks([], self._make_file())
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()


class TestRunOtherChecks(unittest.TestCase):
    def test_empty_args_returns_zero(self):
        with patch("main.subprocess.run") as mock_run:
            rc = main.run_other_checks([], io.StringIO())
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()

    def test_with_args_calls_subprocess(self):
        mock_result = MagicMock(returncode=0)
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            rc = main.run_other_checks(["--branch"], io.StringIO())
        self.assertEqual(rc, 0)
        called_cmd = mock_run.call_args[0][0]
        self.assertEqual(called_cmd, ["commit-check", "--branch"])

    def test_with_args_returns_returncode(self):
        mock_result = MagicMock(returncode=1)
        with patch("main.subprocess.run", return_value=mock_result):
            rc = main.run_other_checks(["--branch", "--author-name"], io.StringIO())
        self.assertEqual(rc, 1)

    def test_prints_command(self):
        mock_result = MagicMock(returncode=0)
        with patch("main.subprocess.run", return_value=mock_result):
            with patch("builtins.print") as mock_print:
                main.run_other_checks(["--branch"], io.StringIO())
        mock_print.assert_called_once_with("commit-check --branch")


class TestRunDefaultChecks(unittest.TestCase):
    def test_rc_zero(self):
        mock_result = MagicMock(returncode=0)
        with patch("main.subprocess.run", return_value=mock_result):
            rc = main.run_default_checks(["--message", "--branch"], io.StringIO())
        self.assertEqual(rc, 0)

    def test_rc_one(self):
        mock_result = MagicMock(returncode=1)
        with patch("main.subprocess.run", return_value=mock_result):
            rc = main.run_default_checks(["--message"], io.StringIO())
        self.assertEqual(rc, 1)

    def test_command_contains_all_args(self):
        mock_result = MagicMock(returncode=0)
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            main.run_default_checks(
                ["--message", "--branch", "--author-name"], io.StringIO()
            )
        called_cmd = mock_run.call_args[0][0]
        self.assertEqual(
            called_cmd,
            ["commit-check", "--message", "--branch", "--author-name"],
        )

    def test_prints_command(self):
        mock_result = MagicMock(returncode=0)
        with patch("main.subprocess.run", return_value=mock_result):
            with patch("builtins.print") as mock_print:
                main.run_default_checks(["--branch"], io.StringIO())
        mock_print.assert_called_once_with("commit-check --branch")

    def test_empty_args(self):
        mock_result = MagicMock(returncode=0)
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            main.run_default_checks([], io.StringIO())
        called_cmd = mock_run.call_args[0][0]
        self.assertEqual(called_cmd, ["commit-check"])


class TestRunCommitCheck(unittest.TestCase):
    def setUp(self):
        # Ensure result.txt is written to a temp location
        self._orig_dir = os.getcwd()
        import tempfile

        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._orig_dir)

    def test_pr_path_calls_pr_message_checks(self):
        with (
            patch("main.MESSAGE", "true"),
            patch("main.BRANCH", "false"),
            patch("main.AUTHOR_NAME", "false"),
            patch("main.AUTHOR_EMAIL", "false"),
            patch("main.get_pr_commit_messages", return_value=["fix: something"]),
            patch("main.run_pr_message_checks", return_value=0) as mock_pr,
            patch("main.run_other_checks", return_value=0),
            patch("main.run_default_checks") as mock_default,
        ):
            rc = main.run_commit_check()
        mock_pr.assert_called_once()
        mock_default.assert_not_called()
        self.assertEqual(rc, 0)

    def test_pr_path_rc_accumulation(self):
        with (
            patch("main.MESSAGE", "true"),
            patch("main.BRANCH", "true"),
            patch("main.AUTHOR_NAME", "false"),
            patch("main.AUTHOR_EMAIL", "false"),
            patch("main.get_pr_commit_messages", return_value=["bad msg"]),
            patch("main.run_pr_message_checks", return_value=1),
            patch("main.run_other_checks", return_value=1),
        ):
            rc = main.run_commit_check()
        self.assertEqual(rc, 2)

    def test_non_pr_path_uses_default_checks(self):
        with (
            patch("main.MESSAGE", "true"),
            patch("main.BRANCH", "false"),
            patch("main.AUTHOR_NAME", "false"),
            patch("main.AUTHOR_EMAIL", "false"),
            patch("main.get_pr_commit_messages", return_value=[]),
            patch("main.run_pr_message_checks") as mock_pr,
            patch("main.run_default_checks", return_value=0) as mock_default,
        ):
            rc = main.run_commit_check()
        mock_pr.assert_not_called()
        mock_default.assert_called_once()
        self.assertEqual(rc, 0)

    def test_message_false_uses_default_checks(self):
        with (
            patch("main.MESSAGE", "false"),
            patch("main.BRANCH", "true"),
            patch("main.AUTHOR_NAME", "false"),
            patch("main.AUTHOR_EMAIL", "false"),
            patch("main.run_pr_message_checks") as mock_pr,
            patch("main.run_default_checks", return_value=0) as mock_default,
        ):
            rc = main.run_commit_check()
        mock_pr.assert_not_called()
        mock_default.assert_called_once()
        self.assertEqual(rc, 0)

    def test_result_txt_is_created(self):
        with (
            patch("main.MESSAGE", "false"),
            patch("main.BRANCH", "false"),
            patch("main.AUTHOR_NAME", "false"),
            patch("main.AUTHOR_EMAIL", "false"),
            patch("main.run_default_checks", return_value=0),
        ):
            main.run_commit_check()
        self.assertTrue(os.path.exists(os.path.join(self._tmpdir, "result.txt")))

    def test_other_args_excludes_message(self):
        """When in PR path, run_other_checks must not receive --message."""
        captured_args = []

        def fake_other_checks(args, result_file):
            captured_args.extend(args)
            return 0

        with (
            patch("main.MESSAGE", "true"),
            patch("main.BRANCH", "true"),
            patch("main.AUTHOR_NAME", "false"),
            patch("main.AUTHOR_EMAIL", "false"),
            patch("main.get_pr_commit_messages", return_value=["fix: x"]),
            patch("main.run_pr_message_checks", return_value=0),
            patch("main.run_other_checks", side_effect=fake_other_checks),
        ):
            main.run_commit_check()
        self.assertNotIn("--message", captured_args)
        self.assertIn("--branch", captured_args)


class TestGetPrCommitMessages(unittest.TestCase):
    def test_non_pr_event_returns_empty(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, [])

    def test_pr_event_with_commits(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "fix: first\n\x00feat: second\n\x00"
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.subprocess.run", return_value=mock_result),
        ):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, ["fix: first", "feat: second"])

    def test_pr_event_empty_output(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.subprocess.run", return_value=mock_result),
        ):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, [])

    def test_git_failure_returns_empty(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.subprocess.run", return_value=mock_result),
        ):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, [])

    def test_exception_returns_empty(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.subprocess.run", side_effect=Exception("git not found")),
        ):
            result = main.get_pr_commit_messages()
        self.assertEqual(result, [])


class TestReadResultFile(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._orig_dir = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._orig_dir)

    def _write_result(self, content: str):
        with open("result.txt", "w", encoding="utf-8") as f:
            f.write(content)

    def test_empty_file_returns_none(self):
        self._write_result("")
        result = main.read_result_file()
        self.assertIsNone(result)

    def test_file_with_content(self):
        self._write_result("some output\n")
        result = main.read_result_file()
        self.assertEqual(result, "some output")

    def test_ansi_codes_are_stripped(self):
        self._write_result("\x1b[31mError\x1b[0m: bad commit")
        result = main.read_result_file()
        self.assertEqual(result, "Error: bad commit")

    def test_trailing_whitespace_stripped(self):
        self._write_result("output\n\n")
        result = main.read_result_file()
        self.assertEqual(result, "output")


class TestAddJobSummary(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._orig_dir = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)
        # Create an empty result.txt
        with open("result.txt", "w", encoding="utf-8"):
            pass

    def tearDown(self):
        os.chdir(self._orig_dir)

    def test_false_skips(self):
        with patch("main.JOB_SUMMARY", "false"):
            rc = main.add_job_summary()
        self.assertEqual(rc, 0)

    def test_success_writes_success_title(self):
        summary_path = os.path.join(self._tmpdir, "summary.txt")
        with (
            patch("main.JOB_SUMMARY", "true"),
            patch("main.GITHUB_STEP_SUMMARY", summary_path),
            patch("main.read_result_file", return_value=None),
        ):
            rc = main.add_job_summary()
        self.assertEqual(rc, 0)
        with open(summary_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn(main.SUCCESS_TITLE, content)

    def test_failure_writes_failure_title(self):
        summary_path = os.path.join(self._tmpdir, "summary.txt")
        with (
            patch("main.JOB_SUMMARY", "true"),
            patch("main.GITHUB_STEP_SUMMARY", summary_path),
            patch("main.read_result_file", return_value="bad commit message"),
        ):
            rc = main.add_job_summary()
        self.assertEqual(rc, 1)
        with open(summary_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn(main.FAILURE_TITLE, content)
        self.assertIn("bad commit message", content)


class TestIsForkPr(unittest.TestCase):
    def test_no_event_path(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove GITHUB_EVENT_PATH if present
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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(event, f)
            event_path = f.name
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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(event, f)
            event_path = f.name
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": event_path}):
            result = main.is_fork_pr()
        self.assertTrue(result)
        os.unlink(event_path)

    def test_json_parse_failure_returns_false(self):
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json{{{")
            event_path = f.name
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": event_path}):
            result = main.is_fork_pr()
        self.assertFalse(result)
        os.unlink(event_path)


class TestLogErrorAndExit(unittest.TestCase):
    def test_exits_with_specified_code(self):
        with self.assertRaises(SystemExit) as ctx:
            main.log_error_and_exit("# Title", None, 0)
        self.assertEqual(ctx.exception.code, 0)

    def test_exits_with_nonzero_code(self):
        with self.assertRaises(SystemExit) as ctx:
            main.log_error_and_exit("# Title", None, 2)
        self.assertEqual(ctx.exception.code, 2)

    def test_with_result_text_prints_error(self):
        with (
            patch("builtins.print") as mock_print,
            self.assertRaises(SystemExit),
        ):
            main.log_error_and_exit("# Failure", "bad commit", 1)
        mock_print.assert_called_once()
        printed = mock_print.call_args[0][0]
        self.assertIn("::error::", printed)
        self.assertIn("bad commit", printed)

    def test_without_result_text_no_print(self):
        with (
            patch("builtins.print") as mock_print,
            self.assertRaises(SystemExit),
        ):
            main.log_error_and_exit("# Failure", None, 1)
        mock_print.assert_not_called()

    def test_empty_string_result_text_no_print(self):
        with (
            patch("builtins.print") as mock_print,
            self.assertRaises(SystemExit),
        ):
            main.log_error_and_exit("# Failure", "", 1)
        mock_print.assert_not_called()


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
            patch("main.DRY_RUN", "false"),
            patch("main.read_result_file", return_value=None),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        self.assertEqual(ctx.exception.code, 0)

    def test_failure_path(self):
        with (
            patch("main.log_env_vars"),
            patch("main.run_commit_check", return_value=1),
            patch("main.add_job_summary", return_value=0),
            patch("main.add_pr_comments", return_value=0),
            patch("main.DRY_RUN", "false"),
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
            patch("main.DRY_RUN", "true"),
            patch("main.read_result_file", return_value=None),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        self.assertEqual(ctx.exception.code, 0)

    def test_all_subfunctions_called(self):
        with (
            patch("main.log_env_vars") as mock_log,
            patch("main.run_commit_check", return_value=0) as mock_run,
            patch("main.add_job_summary", return_value=0) as mock_summary,
            patch("main.add_pr_comments", return_value=0) as mock_comments,
            patch("main.DRY_RUN", "false"),
            patch("main.read_result_file", return_value=None),
            self.assertRaises(SystemExit),
        ):
            main.main()
        mock_log.assert_called_once()
        mock_run.assert_called_once()
        mock_summary.assert_called_once()
        mock_comments.assert_called_once()


if __name__ == "__main__":
    unittest.main()
