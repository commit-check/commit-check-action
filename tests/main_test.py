"""main(): the order the action runs its steps in."""

import io
import os
import runpy
import sys
import unittest
from unittest.mock import patch

from helpers import ANSI_CODES, fail_scope, pass_scope

import main


class TestMain(unittest.TestCase):
    def test_success_path(self):
        with (
            patch("action.inputs.log_env_vars"),
            patch("action.cli.run_commit_check", return_value=(0, [pass_scope()])),
            patch("action.render.render_step_log"),
            patch("action.publish.set_result_output"),
            patch("action.publish.add_job_summary", return_value=0),
            patch("action.publish.add_pr_comments", return_value=0),
            patch("action.inputs.DRY_RUN_ENABLED", False),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        self.assertEqual(ctx.exception.code, 0)

    def test_failure_path_exits_nonzero(self):
        with (
            patch("action.inputs.log_env_vars"),
            patch("action.cli.run_commit_check", return_value=(1, [fail_scope()])),
            patch("action.render.render_step_log"),
            patch("action.publish.set_result_output"),
            patch("action.publish.add_job_summary", return_value=1),
            patch("action.publish.add_pr_comments", return_value=1),
            patch("action.inputs.DRY_RUN_ENABLED", False),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_dry_run_forces_zero(self):
        with (
            patch("action.inputs.log_env_vars"),
            patch("action.cli.run_commit_check", return_value=(1, [fail_scope()])),
            patch("action.render.render_step_log"),
            patch("action.publish.set_result_output"),
            patch("action.publish.add_job_summary", return_value=1),
            patch("action.publish.add_pr_comments", return_value=0),
            patch("action.inputs.DRY_RUN_ENABLED", True),
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        self.assertEqual(ctx.exception.code, 0)

    def test_running_the_script_runs_main(self):
        """action.yml runs ``python main.py``; with every input unset no
        check is enabled, so it reports and exits without calling anything."""
        out = io.StringIO()
        with (
            patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True),
            patch("sys.stdout", out),
            patch("sys.stderr", io.StringIO()),
            patch("subprocess.run") as mock_run,
            self.assertRaises(SystemExit) as ctx,
        ):
            runpy.run_path(main.__file__, run_name="__main__")
        self.assertEqual(ctx.exception.code, 0)
        mock_run.assert_not_called()
        self.assertIn("::debug::MESSAGE=false\n", out.getvalue())
        self.assertTrue(
            ANSI_CODES.sub("", out.getvalue()).endswith(
                "✔ Commit Check: all checks passed\n"
            ),
            out.getvalue(),
        )


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


class TestLogErrorAndExit(unittest.TestCase):
    def test_exits_with_specified_code(self):
        with self.assertRaises(SystemExit) as ctx:
            main.log_error_and_exit(0)
        self.assertEqual(ctx.exception.code, 0)

    def test_failure_exits_without_a_second_annotation(self):
        """The findings are already one ::error each; a summary ::error on
        top made GitHub count one more error than there were findings."""
        with (
            patch("builtins.print") as mock_print,
            self.assertRaises(SystemExit) as ctx,
        ):
            main.log_error_and_exit(1)
        self.assertEqual(ctx.exception.code, 1)
        mock_print.assert_not_called()
