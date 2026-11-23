"""Output surfaces: job summary, result output, PR comment and AI label."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from helpers import (
    SHA_B,
    fail_scope,
    fix_scope,
    make_check,
    pass_scope,
    read_github_output,
)

from action import cli, model, publish, render


class TestAddJobSummary(unittest.TestCase):
    def test_false_skips(self):
        with patch("action.inputs.JOB_SUMMARY_ENABLED", False):
            rc = publish.add_job_summary([pass_scope()])
        self.assertEqual(rc, 0)

    def test_success_writes_policy_report(self):
        summary_path = os.path.join(tempfile.mkdtemp(), "summary.txt")
        with (
            patch("action.inputs.JOB_SUMMARY_ENABLED", True),
            patch("action.inputs.GITHUB_STEP_SUMMARY", summary_path),
        ):
            rc = publish.add_job_summary([pass_scope("Branch")])
        self.assertEqual(rc, 0)
        with open(summary_path, encoding="utf-8") as file_obj:
            content = file_obj.read()
        self.assertIn("Commit Check passed\n", content)

    def test_failure_returns_nonzero(self):
        summary_path = os.path.join(tempfile.mkdtemp(), "summary.txt")
        with (
            patch("action.inputs.JOB_SUMMARY_ENABLED", True),
            patch("action.inputs.GITHUB_STEP_SUMMARY", summary_path),
        ):
            rc = publish.add_job_summary([fail_scope()])
        self.assertEqual(rc, 1)
        with open(summary_path, encoding="utf-8") as file_obj:
            content = file_obj.read()
        self.assertIn("Commit Check failed\n", content)
        self.assertIn("```diff\n@@ Commit 1/1 @@\n- bad message\n", content)


class TestSetResultOutput(unittest.TestCase):
    def test_writes_heredoc_json(self):
        output_path = os.path.join(tempfile.mkdtemp(), "output.txt")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
            publish.set_result_output([fail_scope("Commit 1/1"), pass_scope("Branch")])
        with open(output_path, encoding="utf-8") as file_obj:
            content = file_obj.read()
        self.assertRegex(content, r"(?m)^result<<ghadelimiter_[0-9a-f-]{36}$")
        self.assertIn('"status": "fail"', content)
        self.assertIn('"label": "Commit 1/1"', content)
        outputs = read_github_output(output_path)
        self.assertEqual(set(outputs), {"result"})
        self.assertEqual(json.loads(outputs["result"])["status"], "fail")

    def test_output_survives_an_eof_line_in_the_commit_body(self):
        """A commit body line reading ``EOF`` must not end the heredoc.

        The JSON quotes the checked value and the error text as they are, so
        such a line lands in the output verbatim; with a fixed ``EOF``
        delimiter the runner rejected the whole file and failed the step.
        The runner-faithful parser raises on such a file.
        """
        results = [
            model.ScopeResult(
                label="Commit 1/1",
                checks=[
                    make_check(
                        "message",
                        status="fail",
                        value="bad Subject\n\nEOF\r\nrest of body",
                        error="Subject must be Conventional Commits.",
                        suggest="Use\nEOF\nnow",
                    )
                ],
            )
        ]
        output_path = os.path.join(tempfile.mkdtemp(), "output.txt")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
            publish.set_result_output(results)
        outputs = read_github_output(output_path)
        payload = json.loads(outputs["result"])
        self.assertEqual(payload["status"], "fail")
        check = payload["scopes"][0]["checks"][0]
        self.assertEqual(check["value"], "bad Subject\n\nEOF\r\nrest of body")
        self.assertEqual(check["suggest"], "Use\nEOF\nnow")

    def test_scopes_carry_the_full_sha_and_an_unchanged_label(self):
        """Downstream steps keep matching on ``label``; the hash is a new
        field beside it, full length so it can be passed back to the API."""
        output_path = os.path.join(tempfile.mkdtemp(), "output.txt")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
            publish.set_result_output(
                [fix_scope("Commit 2/3", sha=SHA_B), pass_scope()]
            )
        payload = json.loads(read_github_output(output_path)["result"])
        self.assertEqual(payload["scopes"][0]["label"], "Commit 2/3")
        self.assertEqual(payload["scopes"][0]["sha"], SHA_B)
        self.assertEqual(
            payload["scopes"][0]["checks"][0]["fix"], "feat: Add login page"
        )
        self.assertEqual(payload["scopes"][1]["sha"], "")

    def test_all_pass_status(self):
        output_path = os.path.join(tempfile.mkdtemp(), "output.txt")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
            publish.set_result_output([pass_scope()])
        with open(output_path, encoding="utf-8") as file_obj:
            content = file_obj.read()
        self.assertIn('"status": "pass"', content)

    def test_no_output_env_is_noop(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ["GITHUB_STEP_SUMMARY"] = "/tmp/step_summary.txt"
            publish.set_result_output([pass_scope()])  # should not raise


class TestAddPrComments(unittest.TestCase):
    def test_disabled_returns_zero(self):
        with patch("action.inputs.PR_COMMENTS_ENABLED", False):
            rc = publish.add_pr_comments([pass_scope()])
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
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
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
            patch("action.publish.get_pr_number") as mock_number,
            patch("builtins.print") as mock_print,
        ):
            rc = publish.add_pr_comments([fail_scope()])
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
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("action.publish.is_fork_pr", return_value=True),
            patch("action.inputs.JOB_SUMMARY_ENABLED", False),
            patch("builtins.print") as mock_print,
        ):
            rc = publish.add_pr_comments([pass_scope()])
        self.assertEqual(rc, 0)
        printed = mock_print.call_args[0][0]
        self.assertIn("::warning::", printed)
        self.assertIn("read-only", printed)

    def test_fork_pr_warning_names_only_the_surfaces_that_exist(self):
        """With job-summary: false the warning must not send the reader to a
        summary this run never wrote; the annotations are still named."""
        summary_path = os.path.join(tempfile.mkdtemp(), "summary.txt")
        with (
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("action.publish.is_fork_pr", return_value=True),
            patch("action.inputs.JOB_SUMMARY_ENABLED", False),
            patch("action.inputs.GITHUB_STEP_SUMMARY", summary_path),
            patch("builtins.print") as mock_print,
        ):
            rc = publish.add_pr_comments([pass_scope()])
        self.assertEqual(rc, 0)
        printed = mock_print.call_args[0][0]
        self.assertIn("annotations on the run page", printed)
        self.assertNotIn("job's summary", printed)
        self.assertFalse(os.path.exists(summary_path))

        with (
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("action.publish.is_fork_pr", return_value=True),
            patch("action.inputs.JOB_SUMMARY_ENABLED", True),
            patch("action.inputs.GITHUB_STEP_SUMMARY", summary_path),
            patch("builtins.print") as mock_print,
        ):
            publish.add_pr_comments([pass_scope()])
        printed = mock_print.call_args[0][0]
        self.assertIn("in this job's summary and in the annotations", printed)

    def test_fork_pr_writes_job_summary_hint(self):
        summary_path = os.path.join(tempfile.mkdtemp(), "summary.txt")
        with (
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("action.publish.is_fork_pr", return_value=True),
            patch("action.inputs.JOB_SUMMARY_ENABLED", True),
            patch("action.inputs.GITHUB_STEP_SUMMARY", summary_path),
            patch("builtins.print"),
        ):
            rc = publish.add_pr_comments([pass_scope()])
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
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=False),
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
            rc = publish.add_pr_comments([fail_scope()])
        self.assertEqual(rc, 1)
        self.assertEqual(mock_pull_request.create_comment.call_count, 1)
        body = mock_pull_request.create_comment.call_args[1]["body"]
        self.assertTrue(body.startswith(render.COMMENT_MARKER))
        self.assertIn("```diff\n@@ Commit 1/1 @@\n- bad message\n", body)

    def test_updates_existing_comment_when_changed(self):
        # The report an earlier run posted: the marker, and posted by the bot,
        # which is what makes it safe to edit.
        old_comment = MagicMock(body=f"{render.COMMENT_MARKER}\nold report")
        old_comment.user.type = "Bot"
        mock_pull_request = MagicMock()
        mock_pull_request.get_comments.return_value = [old_comment]
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_pull_request

        github_module = MagicMock()
        github_module.Github.return_value.get_repo.return_value = mock_repo

        with (
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=False),
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
            rc = publish.add_pr_comments([fail_scope()])
        self.assertEqual(rc, 1)
        old_comment.edit.assert_called_once()
        old_comment.delete.assert_not_called()

    def test_skips_when_comment_is_up_to_date(self):
        body = render.render_pr_comment([fail_scope()])
        existing = MagicMock(body=body)
        existing.user.type = "Bot"
        mock_pull_request = MagicMock()
        mock_pull_request.get_comments.return_value = [existing]
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_pull_request

        github_module = MagicMock()
        github_module.Github.return_value.get_repo.return_value = mock_repo

        with (
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=False),
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
            rc = publish.add_pr_comments([fail_scope()])
        self.assertEqual(rc, 1)
        existing.edit.assert_not_called()
        mock_pull_request.create_comment.assert_not_called()

    def test_the_newest_report_is_updated_and_older_ones_deleted(self):
        """Duplicates (two runs racing to create the first comment, say)
        collapse into the newest one."""
        older = MagicMock(body=f"{render.COMMENT_MARKER}\nolder report")
        older.user.type = "Bot"
        newer = MagicMock(body=f"{render.COMMENT_MARKER}\nnewer report")
        newer.user.type = "Bot"
        mock_pull_request = MagicMock()
        mock_pull_request.get_comments.return_value = [older, newer]
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_pull_request
        github_module = MagicMock()
        github_module.Github.return_value.get_repo.return_value = mock_repo

        with (
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=False),
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
            rc = publish.add_pr_comments([fail_scope()])
        self.assertEqual(rc, 1)
        github_module.Github.return_value.get_repo.assert_called_once_with("owner/repo")
        mock_repo.get_issue.assert_called_once_with(12)
        newer.edit.assert_called_once_with(render.render_pr_comment([fail_scope()]))
        newer.delete.assert_not_called()
        older.delete.assert_called_once_with()
        older.edit.assert_not_called()
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
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=False),
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
            rc = publish.add_pr_comments([fail_scope()])
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

    def test_a_missing_pygithub_is_a_warning_not_a_crash(self):
        """``None`` in sys.modules makes the import raise ImportError."""
        with (
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=False),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch.dict(sys.modules, {"github": None}),
            patch("builtins.print") as mock_print,
        ):
            rc = publish.add_pr_comments([fail_scope()])
        self.assertEqual(rc, 0)
        warning = mock_print.call_args[0][0]
        self.assertTrue(warning.startswith("::warning::Unable to post PR comment: "))
        self.assertIs(mock_print.call_args[1]["file"], sys.stderr)

    def test_a_missing_token_or_repository_is_named(self):
        for missing in ("GITHUB_TOKEN", "GITHUB_REPOSITORY"):
            with self.subTest(missing=missing):
                github_module = MagicMock()
                github_module.GithubException = _StubGithubException
                env = {
                    "GITHUB_TOKEN": "token",
                    "GITHUB_REPOSITORY": "owner/repo",
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_REF": "refs/pull/12/merge",
                    missing: "",
                }
                with (
                    patch("action.inputs.PR_COMMENTS_ENABLED", True),
                    patch(
                        "action.publish.is_fork_pr_with_readonly_token",
                        return_value=False,
                    ),
                    patch.dict(os.environ, env),
                    patch.dict(sys.modules, {"github": github_module}),
                    patch("builtins.print") as mock_print,
                ):
                    rc = publish.add_pr_comments([fail_scope()])
                self.assertEqual(rc, 0)
                self.assertEqual(
                    mock_print.call_args[0][0],
                    f"::warning::Unable to post PR comment: {missing} is not set",
                )
                github_module.Github.assert_not_called()


class TestIsForkPrWithReadonlyToken(unittest.TestCase):
    def test_fork_pr_with_pull_request_event(self):
        with (
            patch("action.publish.is_fork_pr", return_value=True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
        ):
            self.assertTrue(publish.is_fork_pr_with_readonly_token())

    def test_fork_pr_with_pull_request_target_event(self):
        """pull_request_target has write token — not considered read-only."""
        with (
            patch("action.publish.is_fork_pr", return_value=True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
        ):
            self.assertFalse(publish.is_fork_pr_with_readonly_token())

    def test_same_repo_not_fork(self):
        with (
            patch("action.publish.is_fork_pr", return_value=False),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
        ):
            self.assertFalse(publish.is_fork_pr_with_readonly_token())


class TestIsForkPr(unittest.TestCase):
    def test_no_event_path(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_EVENT_PATH", None)
            result = publish.is_fork_pr()
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
            result = publish.is_fork_pr()
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
            result = publish.is_fork_pr()
        self.assertTrue(result)
        os.unlink(event_path)

    def test_unreadable_event_is_not_a_fork(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        event_path = os.path.join(tmp.name, "event.json")
        with open(event_path, "w", encoding="utf-8") as f:
            f.write("not json")
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": event_path}):
            self.assertFalse(publish.is_fork_pr())


class TestGetPrNumber(unittest.TestCase):
    def _event(self, payload) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "event.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def test_reads_the_number_from_a_pull_request_ref(self):
        with patch.dict(
            os.environ, {"GITHUB_REF": "refs/pull/42/merge", "GITHUB_EVENT_PATH": ""}
        ):
            self.assertEqual(publish.get_pr_number(), 42)

    def test_pull_request_target_reads_the_event_instead(self):
        """There GITHUB_REF is the base branch, which names no pull request."""
        for payload in ({"number": 7}, {"pull_request": {"number": 7}}):
            with self.subTest(payload=payload):
                env = {
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_EVENT_PATH": self._event(payload),
                }
                with patch.dict(os.environ, env):
                    self.assertEqual(publish.get_pr_number(), 7)

    def test_no_number_anywhere_raises(self):
        for payload in ({"pull_request": None}, {}, "not json"):
            with self.subTest(payload=payload):
                env = {
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_EVENT_PATH": self._event(payload),
                }
                with patch.dict(os.environ, env), self.assertRaises(ValueError):
                    publish.get_pr_number()

    def test_no_event_payload_raises(self):
        with (
            patch.dict(os.environ, {"GITHUB_REF": "refs/heads/main"}),
            self.assertRaises(ValueError),
        ):
            os.environ.pop("GITHUB_EVENT_PATH", None)
            publish.get_pr_number()


class TestFindOwnComments(unittest.TestCase):
    """Comment ownership: the action must never destroy a human's comment."""

    @staticmethod
    def _comment(body: str, user_type: str = "Bot") -> MagicMock:
        comment = MagicMock()
        comment.body = body
        comment.user.type = user_type
        return comment

    def test_marked_comment_is_updated_and_older_ones_deleted(self):
        first = self._comment(f"{render.COMMENT_MARKER}\nold")
        second = self._comment(f"{render.COMMENT_MARKER}\nnewer")
        target, stale = publish._find_own_comments([first, second])
        self.assertIs(target, second)
        self.assertEqual(stale, [first])

    def test_human_comment_with_the_old_title_is_never_deleted(self):
        """A person can type '# Commit Check'; deleting on that is destructive."""
        human = self._comment("# Commit Check\nwhy is this failing?", user_type="User")
        mine = self._comment(f"{render.COMMENT_MARKER}\nreport")
        target, stale = publish._find_own_comments([human, mine])
        self.assertIs(target, mine)
        self.assertEqual(stale, [])

    def test_human_comment_with_the_old_title_is_not_adopted(self):
        human = self._comment("# Commit Check\nwhy is this failing?", user_type="User")
        target, stale = publish._find_own_comments([human])
        self.assertIsNone(target)
        self.assertEqual(stale, [])

    def test_bot_comment_without_the_marker_is_left_alone(self):
        """A report from before v2.13.0 has no marker: it is neither edited
        nor deleted, and the run posts a new comment."""
        legacy = self._comment("# Commit-Check\nold report")
        target, stale = publish._find_own_comments([legacy])
        self.assertIsNone(target)
        self.assertEqual(stale, [])

    def test_human_comment_with_the_marker_is_never_touched(self):
        """A person can paste the marker too, e.g. quoting a report."""
        quoted = self._comment(f"{render.COMMENT_MARKER}\nquoted", user_type="User")
        mine = self._comment(f"{render.COMMENT_MARKER}\nreport")
        self.assertEqual(publish._find_own_comments([quoted, mine]), (mine, []))
        self.assertEqual(publish._find_own_comments([mine, quoted]), (mine, []))
        self.assertEqual(publish._find_own_comments([quoted]), (None, []))

    def test_no_comments_yields_nothing_to_update(self):
        target, stale = publish._find_own_comments([])
        self.assertIsNone(target)
        self.assertEqual(stale, [])

    def test_a_comment_without_an_author_is_not_a_bot(self):
        """A deleted account can leave ``user`` empty; that is not a crash,
        and not a licence to edit the comment either."""
        orphan = self._comment(f"{render.COMMENT_MARKER}\nold report")
        orphan.user = None
        self.assertFalse(publish._is_bot(orphan))
        self.assertEqual(publish._find_own_comments([orphan]), (None, []))


class TestAiToolsFound(unittest.TestCase):
    def setUp(self):
        cli.PR_BODY_AI_TOOLS.clear()
        self.addCleanup(cli.PR_BODY_AI_TOOLS.clear)

    def test_union_of_scopes_description_and_opener(self):
        cli.PR_BODY_AI_TOOLS[:] = ["OpenAI Codex"]
        results = [
            model.ScopeResult(label="Commit 1/2", ai_tools=["Claude Code"]),
            model.ScopeResult(label="Commit 2/2", ai_tools=["Claude Code", "Cursor"]),
        ]
        with (
            patch("action.context.is_pr_event", return_value=True),
            patch("action.context.get_pr_opener_tool", return_value="Devin"),
        ):
            found = publish.ai_tools_found(results)
        self.assertEqual(found, ["Claude Code", "Cursor", "Devin", "OpenAI Codex"])

    def test_outside_a_pull_request_only_the_scopes(self):
        results = [model.ScopeResult(label="Commit message", ai_tools=["Aider"])]
        with (
            patch("action.context.is_pr_event", return_value=False),
            patch("action.context.get_pr_opener_tool") as opener,
        ):
            self.assertEqual(publish.ai_tools_found(results), ["Aider"])
        opener.assert_not_called()

    def test_result_output_carries_them(self):
        output_path = os.path.join(tempfile.mkdtemp(), "output.txt")
        results = [model.ScopeResult(label="Commit 1/1", ai_tools=["Claude Code"])]
        with (
            patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}),
            patch("action.context.is_pr_event", return_value=False),
        ):
            publish.set_result_output(results)
        payload = json.loads(read_github_output(output_path)["result"])
        self.assertEqual(payload["ai_tools"], ["Claude Code"])


class TestAddAiLabel(unittest.TestCase):
    """``ai-label``: added when an AI tool wrote any of the PR, never removed."""

    AI_SCOPE = model.ScopeResult(label="Commit 1/1", ai_tools=["Claude Code"])

    def setUp(self):
        cli.PR_BODY_AI_TOOLS.clear()

    def _run(
        self, results, label="true", labels=(), get_label=None, add=None, create=None
    ):
        issue = MagicMock()
        issue.labels = [MagicMock() for _ in labels]
        for mock_label, name in zip(issue.labels, labels):
            mock_label.name = name
        if add is not None:
            issue.add_to_labels.side_effect = add
        repo = MagicMock()
        repo.get_issue.return_value = issue
        if get_label is not None:
            repo.get_label.side_effect = get_label
        if create is not None:
            repo.create_label.side_effect = create
        github_module = MagicMock()
        github_module.GithubException = _StubGithubException
        github_module.Github.return_value.get_repo.return_value = repo
        with (
            patch.dict(
                os.environ,
                {
                    "AI_LABEL": label,
                    "GITHUB_TOKEN": "token",
                    "GITHUB_REPOSITORY": "owner/repo",
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_REF": "refs/pull/12/merge",
                    "GITHUB_API_URL": "https://ghe.example.com/api/v3",
                },
            ),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=False),
            patch("action.context.get_pr_opener_tool", return_value=None),
            patch.dict(sys.modules, {"github": github_module}),
            patch("builtins.print") as mock_print,
        ):
            publish.add_ai_label(results)
        printed = [
            c[0][0]
            for c in mock_print.call_args_list
            if c[0] and isinstance(c[0][0], str)
        ]
        return repo, issue, printed, github_module

    def test_adds_the_default_label(self):
        repo, issue, printed, _ = self._run([self.AI_SCOPE])
        issue.add_to_labels.assert_called_once_with("ai-assisted")
        repo.create_label.assert_not_called()
        self.assertIn("Added the label 'ai-assisted' to PR #12: Claude Code.", printed)

    def test_a_custom_label(self):
        _repo, issue, _printed, _ = self._run([self.AI_SCOPE], label="AI generated")
        issue.add_to_labels.assert_called_once_with("AI generated")

    def test_creates_a_missing_label(self):
        repo, issue, _printed, _ = self._run(
            [self.AI_SCOPE], get_label=_StubGithubException(404)
        )
        repo.create_label.assert_called_once_with(
            "ai-assisted", publish.AI_LABEL_COLOR, "Written with the help of an AI tool"
        )
        issue.add_to_labels.assert_called_once_with("ai-assisted")

    def test_uses_the_api_url_of_the_server(self):
        _repo, _issue, _printed, github_module = self._run([self.AI_SCOPE])
        self.assertEqual(
            github_module.Github.call_args.kwargs["base_url"],
            "https://ghe.example.com/api/v3",
        )

    def test_a_label_created_meanwhile_is_still_added(self):
        _repo, issue, printed, _ = self._run(
            [self.AI_SCOPE],
            get_label=_StubGithubException(404),
            create=_StubGithubException(422),
        )
        issue.add_to_labels.assert_called_once_with("ai-assisted")
        self.assertIn("Added the label 'ai-assisted' to PR #12: Claude Code.", printed)

    def test_a_label_that_cannot_be_created_is_not_added(self):
        _repo, issue, _printed, _ = self._run(
            [self.AI_SCOPE],
            get_label=_StubGithubException(404),
            create=_StubGithubException(403),
        )
        issue.add_to_labels.assert_not_called()

    def test_already_labelled(self):
        _repo, issue, printed, _ = self._run([self.AI_SCOPE], labels=("ai-assisted",))
        issue.add_to_labels.assert_not_called()
        self.assertTrue(any("already has the label" in p for p in printed))

    def test_no_ai_no_label_and_no_api_call(self):
        _repo, _issue, printed, github_module = self._run(
            [model.ScopeResult(label="Commit 1/1")]
        )
        github_module.Github.assert_not_called()
        self.assertIn("No AI tool found in the pull request; no label added.", printed)

    def test_off_or_outside_a_pull_request_does_nothing(self):
        _repo, _issue, printed, github_module = self._run(
            [self.AI_SCOPE], label="false"
        )
        github_module.Github.assert_not_called()
        self.assertEqual(printed, [])
        with (
            patch.dict(os.environ, {"AI_LABEL": "true", "GITHUB_EVENT_NAME": "push"}),
            patch("builtins.print") as mock_print,
        ):
            publish.add_ai_label([self.AI_SCOPE])
        mock_print.assert_not_called()

    def test_forbidden_names_the_permission(self):
        _repo, _issue, printed, _ = self._run(
            [self.AI_SCOPE], add=_StubGithubException(403)
        )
        warning = next(p for p in printed if "::warning::" in p)
        self.assertIn("pull-requests: write", warning)

    def test_other_api_errors_warn(self):
        _repo, _issue, printed, _ = self._run(
            [self.AI_SCOPE], get_label=_StubGithubException(500)
        )
        warning = next(p for p in printed if "::warning::" in p)
        self.assertNotIn("pull-requests: write", warning)

    def test_any_other_error_warns(self):
        _repo, _issue, printed, _ = self._run([self.AI_SCOPE], add=RuntimeError("boom"))
        self.assertIn("::warning::Unable to add the AI label: boom", printed)

    def test_missing_token_warns(self):
        with (
            patch.dict(
                os.environ,
                {"AI_LABEL": "true", "GITHUB_EVENT_NAME": "pull_request"},
                clear=True,
            ),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=False),
            patch("action.context.get_pr_opener_tool", return_value=None),
            patch.dict(
                sys.modules,
                {"github": MagicMock(GithubException=_StubGithubException)},
            ),
            patch("builtins.print") as mock_print,
        ):
            publish.add_ai_label([self.AI_SCOPE])
        self.assertIn("GITHUB_TOKEN is not set", mock_print.call_args[0][0])

    def test_missing_repository_warns(self):
        with (
            patch.dict(
                os.environ,
                {
                    "AI_LABEL": "true",
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_TOKEN": "t",
                },
                clear=True,
            ),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=False),
            patch("action.context.get_pr_opener_tool", return_value=None),
            patch.dict(
                sys.modules,
                {"github": MagicMock(GithubException=_StubGithubException)},
            ),
            patch("builtins.print") as mock_print,
        ):
            publish.add_ai_label([self.AI_SCOPE])
        self.assertIn("GITHUB_REPOSITORY is not set", mock_print.call_args[0][0])

    def test_fork_with_a_read_only_token_warns(self):
        with (
            patch.dict(
                os.environ, {"AI_LABEL": "true", "GITHUB_EVENT_NAME": "pull_request"}
            ),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=True),
            patch("action.context.get_pr_opener_tool", return_value=None),
            patch("builtins.print") as mock_print,
        ):
            publish.add_ai_label([self.AI_SCOPE])
        warning = mock_print.call_args[0][0]
        self.assertIn("::warning::AI tools found (Claude Code)", warning)
        self.assertIn("read-only", warning)

    def test_a_missing_pygithub_warns(self):
        with (
            patch.dict(
                os.environ, {"AI_LABEL": "true", "GITHUB_EVENT_NAME": "pull_request"}
            ),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=False),
            patch("action.context.get_pr_opener_tool", return_value=None),
            patch.dict(sys.modules, {"github": None}),
            patch("builtins.print") as mock_print,
        ):
            publish.add_ai_label([self.AI_SCOPE])
        self.assertIn("Unable to add the AI label", mock_print.call_args[0][0])


class TestPrCommentApiUrl(unittest.TestCase):
    """The PR comment goes to the same API host as every other call (GHES)."""

    def test_comment_client_uses_github_api_url(self):
        github_module = MagicMock()
        pull_request = MagicMock()
        pull_request.get_comments.return_value = []
        github_module.Github.return_value.get_repo.return_value.get_issue.return_value = pull_request
        with (
            patch("action.inputs.PR_COMMENTS_ENABLED", True),
            patch("action.publish.is_fork_pr_with_readonly_token", return_value=False),
            patch("action.publish.get_pr_number", return_value=7),
            patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_TOKEN": "t",
                    "GITHUB_REPOSITORY": "o/r",
                    "GITHUB_API_URL": "https://ghe.example.com/api/v3",
                },
            ),
            patch.dict(sys.modules, {"github": github_module}),
        ):
            publish.add_pr_comments([fail_scope()])
        _, kwargs = github_module.Github.call_args
        self.assertEqual(kwargs["base_url"], "https://ghe.example.com/api/v3")
