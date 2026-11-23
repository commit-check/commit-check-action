"""main(): the order the action runs its steps in."""

import io
import os
import runpy
import unittest
from unittest.mock import patch

from helpers import ANSI_CODES, fail_scope, pass_scope

import main


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
