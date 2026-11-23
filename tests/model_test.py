"""ScopeResult and the overall status of a run."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from helpers import fail_scope, make_check, pass_scope, read_github_output, skip_scope

from action import inputs, model, publish


class TestScopeResult(unittest.TestCase):
    def test_status_pass_when_all_checks_pass(self):
        scope = model.ScopeResult(
            label="Branch", checks=[make_check("branch"), make_check("merge_base")]
        )
        self.assertEqual(scope.status, "pass")
        self.assertEqual(scope.failures, [])

    def test_status_fail_when_any_check_fails(self):
        scope = model.ScopeResult(
            label="Branch",
            checks=[
                make_check("branch", status="fail"),
                make_check("merge_base"),
            ],
        )
        self.assertEqual(scope.status, "fail")
        self.assertEqual(len(scope.failures), 1)

    def test_raw_text_fallback_is_failure(self):
        scope = model.ScopeResult(label="Branch", raw_text="unexpected output")
        self.assertEqual(scope.status, "fail")


class TestSkipCompletionSemantics(unittest.TestCase):
    """A skipped run must not be treated as a failing one.

    run_commit_check has always failed only on "fail", but the completion
    paths each asked `all(status == "pass")` instead. That was equivalent
    only while pass and fail were the only statuses; with skip added, a
    skipped-only run exited 1 and reported "fail" while rendering ⊘.
    """

    def test_skipped_only_run_is_not_a_failure(self):
        self.assertEqual(model.exit_code_for([skip_scope(), skip_scope("Branch")]), 0)

    def test_partial_skip_is_not_a_failure(self):
        self.assertEqual(model.exit_code_for([pass_scope(), skip_scope()]), 0)

    def test_a_real_failure_still_fails(self):
        self.assertEqual(model.exit_code_for([fail_scope(), skip_scope()]), 1)

    def test_result_output_status_reports_skip(self):
        self.assertEqual(
            model.overall_status([skip_scope(), skip_scope("Branch")]), "skip"
        )

    def test_result_output_status_of_partial_skip_is_pass(self):
        self.assertEqual(model.overall_status([pass_scope(), skip_scope()]), "pass")

    def test_set_result_output_emits_skip(self):
        """The `result` action output must not call a skipped run failed."""
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            out_path = f.name
        try:
            with patch.dict(os.environ, {"GITHUB_OUTPUT": out_path}):
                publish.set_result_output([skip_scope(), skip_scope("Branch")])
            payload = json.loads(read_github_output(out_path)["result"])
        finally:
            os.unlink(out_path)
        self.assertEqual(payload["status"], "skip")

    def test_add_job_summary_returns_success_for_a_skipped_run(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            summary_path = f.name
        try:
            with (
                patch.object(inputs, "JOB_SUMMARY_ENABLED", True),
                patch.object(inputs, "GITHUB_STEP_SUMMARY", summary_path),
            ):
                rc = publish.add_job_summary([skip_scope(), skip_scope("Branch")])
        finally:
            os.unlink(summary_path)
        self.assertEqual(rc, 0)
