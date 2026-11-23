"""Running the commit-check CLI and turning its JSON into scopes."""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from helpers import (
    SHA_A,
    SHA_B,
    fail_scope,
    files_pass,
    files_scope,
    make_check,
    pass_scope,
)

import main


def json_output(*checks) -> str:
    """Serialize checks to the CLI JSON output shape."""
    status = "fail" if any(c["status"] == "fail" for c in checks) else "pass"
    return json.dumps({"status": status, "checks": list(checks)})


class TestRunCheckJson(unittest.TestCase):
    def test_parses_json_output(self):
        mock_result = MagicMock(returncode=0, stdout=json_output(make_check("branch")))
        with patch("main.subprocess.run", return_value=mock_result):
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
        mock_result = MagicMock(returncode=1, stdout="Commit rejected.\n", stderr="")
        with patch("main.subprocess.run", return_value=mock_result):
            rc, data, raw = main.run_check_json(["--branch"])
        self.assertEqual(rc, 1)
        self.assertIsNone(data)
        self.assertEqual(raw, "Commit rejected.\n")


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
        mock_result = MagicMock(returncode=1, stdout="unexpected output", stderr="")
        with patch("main.subprocess.run", return_value=mock_result):
            scope = main.check_scope("Branch", ["--branch"])
        self.assertEqual(scope.label, "Branch")
        self.assertEqual(scope.raw_text, "unexpected output")
        self.assertEqual(scope.status, "fail")

    def test_keeps_the_text_it_piped_in(self):
        """The report compares it with a rule's fix to name the lines to change."""
        mock_result = MagicMock(returncode=0, stdout=json_output(make_check("message")))
        with patch("main.subprocess.run", return_value=mock_result):
            piped = main.check_scope("PR title", ["--message"], input_text="feat: x")
            read_by_the_cli = main.check_scope("Branch", ["--branch"])
        self.assertEqual(piped.text, "feat: x")
        self.assertEqual(read_by_the_cli.text, "")


class TestRunPrMessageChecks(unittest.TestCase):
    """Each commit's raw message is piped in, with that commit's own author."""

    def setUp(self):
        local = patch("main._local_author", return_value=None)
        self.local_author = local.start()
        self.addCleanup(local.stop)
        main.API_AUTHORS.clear()
        self.addCleanup(main.API_AUTHORS.clear)

    def test_a_commit_in_the_clone_carries_its_own_author(self):
        """The raw message is still piped, so it reads as before (no --rev)."""
        self.local_author.return_value = ("Renovate Bot", "bot@renovateapp.com")
        mock_result = MagicMock(returncode=0, stdout=json_output(make_check("message")))
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            scopes = main.run_pr_message_checks([(SHA_A, "fix: something")])
        self.assertEqual(scopes[0].status, "pass")
        self.assertEqual(scopes[0].sha, SHA_A)
        self.local_author.assert_called_once_with(SHA_A)
        self.assertEqual(
            mock_run.call_args[0][0],
            ["commit-check", "--format", "json", "--message"],
        )
        self.assertEqual(mock_run.call_args[1]["input"], "fix: something")
        env = mock_run.call_args[1]["env"]
        self.assertEqual(env["GIT_AUTHOR_NAME"], "Renovate Bot")
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], "bot@renovateapp.com")

    def test_message_ai_attribution_checks_only_the_ai_rules(self):
        """The format rules' verdicts are dropped; the commit still goes in
        with its author, so CC017 judges it."""
        self.local_author.return_value = ("Jane Dev", "jane@example.com")
        output = json_output(
            make_check("message", status="fail"),
            make_check("ai_author", status="fail", rule_id="CC017"),
            make_check("ai_co_author", rule_id="CC015"),
        )
        mock_result = MagicMock(returncode=1, stdout=output)
        with (
            patch("main.MESSAGE_AI_ATTRIBUTION_ENABLED", True),
            patch("main.subprocess.run", return_value=mock_result) as mock_run,
        ):
            [scope] = main.run_pr_message_checks([(SHA_A, "WIP")])
        self.assertEqual(
            mock_run.call_args[0][0], ["commit-check", "--format", "json", "--message"]
        )
        self.assertEqual(mock_run.call_args[1]["env"]["GIT_AUTHOR_NAME"], "Jane Dev")
        self.assertEqual([c["rule_id"] for c in scope.checks], ["CC017", "CC015"])
        self.assertEqual(scope.status, "fail")

    def test_message_ai_attribution_passes_a_badly_formatted_message(self):
        mock_result = MagicMock(
            returncode=1,
            stdout=json_output(
                make_check("message", status="fail"),
                make_check("subject_min_length", status="fail", rule_id="CC005"),
                make_check("ai_attribution", rule_id="CC013"),
            ),
        )
        with (
            patch("main.MESSAGE_AI_ATTRIBUTION_ENABLED", True),
            patch("main.subprocess.run", return_value=mock_result),
        ):
            [scope] = main.run_pr_message_checks([(SHA_A, "WIP")])
        self.assertEqual(scope.status, "pass")
        self.assertEqual(scope.text, "WIP")

    def test_message_ai_attribution_keeps_the_head_message_for_the_report(self):
        """Outside a PR nothing is piped in, and the message rule, whose
        value is the whole message, is one of the rules dropped."""
        message = "feat: x\n\nCo-authored-by: Claude <noreply@anthropic.com>"
        scope = main.ScopeResult(
            label="Commit message",
            checks=[
                make_check("message", value=message),
                make_check(
                    "ai_attribution",
                    status="fail",
                    rule_id="CC013",
                    value="Claude Code",
                    fix="feat: x",
                ),
            ],
        )
        with patch("main.MESSAGE_AI_ATTRIBUTION_ENABLED", True):
            main.keep_ai_rules(scope)
        self.assertEqual([c["rule_id"] for c in scope.checks], ["CC013"])
        self.assertEqual(scope.text, message)
        self.assertEqual(
            main._line_edits(scope, scope.checks[0]),
            (["Co-authored-by: Claude <noreply@anthropic.com>"], []),
        )

    def test_message_ai_attribution_under_ignore_is_skipped(self):
        """No AI rule ran: say so rather than report a pass."""
        mock_result = MagicMock(returncode=0, stdout=json_output(make_check("message")))
        with (
            patch("main.MESSAGE_AI_ATTRIBUTION_ENABLED", True),
            patch("main.subprocess.run", return_value=mock_result),
        ):
            [scope] = main.run_pr_message_checks([(SHA_A, "feat: x")])
        self.assertEqual(scope.status, "skip")
        self.assertEqual(scope.checks[0]["reason"], main.AI_POLICY_IGNORED)

    def test_a_blank_author_field_is_left_to_git(self):
        self.local_author.return_value = ("Renovate Bot", "")
        mock_result = MagicMock(returncode=0, stdout=json_output(make_check("message")))
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            main.run_pr_message_checks([(SHA_A, "fix: something")])
        env = mock_run.call_args[1]["env"]
        self.assertEqual(env["GIT_AUTHOR_NAME"], "Renovate Bot")
        self.assertNotEqual(env.get("GIT_AUTHOR_EMAIL"), "")

    def test_no_author_at_all_passes_no_environment(self):
        self.local_author.return_value = ("", "")
        mock_result = MagicMock(returncode=0, stdout=json_output(make_check("message")))
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            main.run_pr_message_checks([(SHA_A, "fix: something")])
        self.assertIsNone(mock_run.call_args[1]["env"])

    def test_a_commit_without_a_hash_is_not_looked_up(self):
        mock_result = MagicMock(returncode=0, stdout=json_output(make_check("message")))
        with patch("main.subprocess.run", return_value=mock_result):
            main.run_pr_message_checks([("", "fix: something")])
        self.local_author.assert_not_called()

    def test_an_api_listed_commit_carries_its_author(self):
        """No commit to read, so the API's author goes in git's variables."""
        main.API_AUTHORS[SHA_A] = ("google-labs-jules[bot]", "jules@example.com")
        mock_result = MagicMock(returncode=0, stdout=json_output(make_check("message")))
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            main.run_pr_message_checks([(SHA_A, "fix: something")])
        self.assertEqual(mock_run.call_args[1]["input"], "fix: something")
        env = mock_run.call_args[1]["env"]
        self.assertEqual(env["GIT_AUTHOR_NAME"], "google-labs-jules[bot]")
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], "jules@example.com")
        # The rest of the environment is kept: PATH finds the CLI.
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))

    def test_single_message_pass(self):
        mock_result = MagicMock(returncode=0, stdout=json_output(make_check("message")))
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            scopes = main.run_pr_message_checks([(SHA_A, "fix: something")])
        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0].status, "pass")
        self.assertEqual(scopes[0].label, "Commit 1/1")
        self.assertEqual(scopes[0].sha, SHA_A)
        self.assertEqual(scopes[0].display_label, "Commit 1/1 (d87faca)")
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
            scopes = main.run_pr_message_checks([(SHA_A, "bad commit")])
        self.assertEqual(scopes[0].status, "fail")
        self.assertEqual(len(scopes[0].failures), 1)
        self.assertEqual(scopes[0].sha, SHA_A)

    def test_unparsable_output_still_names_the_commit(self):
        mock_result = MagicMock(returncode=1, stdout="unexpected output", stderr="")
        with patch("main.subprocess.run", return_value=mock_result):
            scopes = main.run_pr_message_checks([(SHA_A, "bad commit")])
        self.assertEqual(scopes[0].raw_text, "unexpected output")
        self.assertEqual(scopes[0].sha, SHA_A)

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
            scopes = main.run_pr_message_checks(
                [("a" * 40, "ok"), ("b" * 40, "bad"), ("c" * 40, "ok")]
            )
        # The label stays the bare index for the ``result`` output; the hash
        # is appended only where a person reads it.
        self.assertEqual(
            [s.label for s in scopes], ["Commit 1/3", "Commit 2/3", "Commit 3/3"]
        )
        self.assertEqual([s.sha for s in scopes], ["a" * 40, "b" * 40, "c" * 40])
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
            rc, _results = main.run_commit_check()
        self.assertEqual(rc, 0)
        mock_scope.assert_called_once_with(
            "PR title", ["--pr-title"], input_text="feat: a feature"
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
            rc, _results = main.run_commit_check()
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
            rc, _results = main.run_commit_check()
        self.assertEqual(rc, 0)
        mock_title.assert_not_called()

    def test_pr_title_check_needs_a_title_to_check(self):
        """An event that carries no title adds no scope, rather than checking
        an empty message."""
        with (
            patch("main.PR_TITLE_ENABLED", True),
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.is_pr_event", return_value=True),
            patch("main.get_pr_title", return_value=None),
            patch("main.check_scope") as mock_scope,
            patch("main.run_other_checks", return_value=[]),
        ):
            rc, results = main.run_commit_check()
        self.assertEqual((rc, results), (0, []))
        mock_scope.assert_not_called()

    def test_non_pr_message_check_uses_commit_message_scope(self):
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.get_pr_commit_messages", return_value=[]),
            patch("main.run_pr_message_checks") as mock_pr,
            patch("main.head_sha", return_value=SHA_A),
            patch(
                "main.check_scope", return_value=pass_scope("Commit message")
            ) as mock_scope,
            patch("main.run_other_checks", return_value=[]),
        ):
            rc, _results = main.run_commit_check()
        self.assertEqual(rc, 0)
        mock_pr.assert_not_called()
        # HEAD is the commit that was checked, so the scope names it too.
        mock_scope.assert_called_once_with("Commit message", ["--message"], sha=SHA_A)

    def test_message_ai_attribution_alone_checks_each_commit(self):
        """With message: false the commits are still checked, for AI only."""
        with (
            patch("main.MESSAGE_ENABLED", False),
            patch("main.MESSAGE_AI_ATTRIBUTION_ENABLED", True),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.get_pr_commit_messages", return_value=[(SHA_A, "WIP")]),
            patch("main.run_pr_message_checks", return_value=[pass_scope()]) as mock_pr,
            patch("main.run_other_checks", return_value=[]) as mock_other,
        ):
            rc, _results = main.run_commit_check()
        self.assertEqual(rc, 0)
        mock_pr.assert_called_once_with([(SHA_A, "WIP")])
        mock_other.assert_called_once_with([], rev=None)

    def test_message_ai_attribution_narrows_the_head_check(self):
        """Outside a PR, HEAD is checked for AI only, even with message: true."""
        with (
            patch("main.MESSAGE_ENABLED", True),
            patch("main.MESSAGE_AI_ATTRIBUTION_ENABLED", True),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.get_pr_commit_messages", return_value=[]),
            patch("main.head_sha", return_value=SHA_A),
            patch(
                "main.check_scope", return_value=pass_scope("Commit message")
            ) as mock_scope,
            patch("main.run_other_checks", return_value=[]),
        ):
            main.run_commit_check()
        mock_scope.assert_called_once_with("Commit message", ["--message"], sha=SHA_A)

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
            patch("main.head_sha", return_value=""),
            patch(
                "main.check_scope", return_value=pass_scope("Commit message")
            ) as mock_scope,
            patch("main.run_other_checks", return_value=[]),
        ):
            rc, _results, output = self._run_capturing_stdout()
        self.assertEqual(rc, 0)
        self.assertIn(self.SHALLOW_PR_WARNING, output)
        mock_scope.assert_called_once_with("Commit message", ["--message"], sha="")

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
                    "reason": "the pull request's head commit could not be resolved",
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


class TestStderrIsNotPartOfTheJson(unittest.TestCase):
    """stdout is the JSON; stderr is what the CLI has to say to a person.

    Merging the two fed notices to json.loads, and an unparsable response is
    a failure as far as ScopeResult is concerned -- so a passing run went
    red the moment the CLI gained something to say. It has several such
    lines, printed under --format json as well: a parent config it could not
    fetch, a flag whose every rule the config switched off, a dry run that
    softened its own verdict.
    """

    #: What the CLI prints when --branch is asked for and the repository's
    #: config has switched every branch rule off.
    NOTICE = (
        "⊘ --branch requested but no branch rules are configured "
        "(conventional_branch = false and no require_rebase_target)\n"
    )

    def setUp(self):
        main._RELAYED_NOTICES.clear()

    def test_the_two_streams_are_read_separately(self):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            main.run_check_json(["--branch"])
        self.assertIs(mock_run.call_args[1]["capture_output"], True)

    def test_a_notice_does_not_break_the_json(self):
        mock_result = MagicMock(
            returncode=0,
            stdout=json_output(make_check("branch")),
            stderr=self.NOTICE,
        )
        with (
            patch("main.subprocess.run", return_value=mock_result),
            patch.object(sys, "stderr", io.StringIO()),
        ):
            rc, data, _raw = main.run_check_json(["--branch"])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(data)
        self.assertEqual(data["status"], "pass")
        # And the scope built from it is a pass, not the "unparsable" fail.
        self.assertEqual(main.check_scope("Branch", ["--branch"]).label, "Branch")

    def test_a_notice_alone_does_not_fail_the_scope(self):
        mock_result = MagicMock(returncode=0, stdout=json_output(), stderr=self.NOTICE)
        with (
            patch("main.subprocess.run", return_value=mock_result),
            patch.object(sys, "stderr", io.StringIO()),
        ):
            scope = main.check_scope("Branch", ["--branch"])
        self.assertEqual(scope.status, "pass")
        self.assertEqual(scope.raw_text, "")

    def test_notices_reach_the_job_log_once(self):
        mock_result = MagicMock(
            returncode=0, stdout=json_output(make_check("branch")), stderr=self.NOTICE
        )
        fake_err = io.StringIO()
        with (
            patch("main.subprocess.run", return_value=mock_result),
            patch.object(sys, "stderr", fake_err),
        ):
            for _ in range(3):
                main.run_check_json(["--branch"])
        printed = fake_err.getvalue()
        self.assertIn("--branch requested but no branch rules are configured", printed)
        # Once, not once per scope and per commit in the pull request.
        self.assertEqual(printed.count("commit-check: ⊘"), 1)

    def test_unparsable_output_keeps_both_streams(self):
        mock_result = MagicMock(
            returncode=2, stdout="", stderr="Error: cchk.toml: Expected ']'\n"
        )
        with (
            patch("main.subprocess.run", return_value=mock_result),
            patch.object(sys, "stderr", io.StringIO()),
        ):
            scope = main.check_scope("Branch", ["--branch"])
        self.assertEqual(scope.status, "fail")
        self.assertIn("Expected ']'", scope.raw_text)


class TestCheckScopeAiTools(unittest.TestCase):
    def _scope(self, payload):
        result = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("main.subprocess.run", return_value=result):
            return main.check_scope("Commit 1/1", ["--message"])

    def test_reads_the_tools(self):
        scope = self._scope({"checks": [], "ai_tools": ["Claude Code"]})
        self.assertEqual(scope.ai_tools, ["Claude Code"])

    def test_an_older_cli_reports_none(self):
        self.assertEqual(self._scope({"checks": []}).ai_tools, [])
        self.assertEqual(self._scope({"checks": [], "ai_tools": "x"}).ai_tools, [])


class TestRunCommitCheckPrBody(unittest.TestCase):
    def setUp(self):
        main.PR_BODY_AI_TOOLS.clear()
        self.addCleanup(main.PR_BODY_AI_TOOLS.clear)

    def _run(self, pr_body_enabled, ai_label, body="Adds a parser."):
        body_scope = main.ScopeResult(
            label="PR description", checks=[], ai_tools=["OpenAI Codex"]
        )
        with (
            patch("main.PR_TITLE_ENABLED", False),
            patch("main.PR_BODY_AI_ATTRIBUTION_ENABLED", pr_body_enabled),
            patch.dict(os.environ, {"AI_LABEL": ai_label}),
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.is_pr_event", return_value=True),
            patch("main.get_pr_body", return_value=body),
            patch("main.check_scope", return_value=body_scope) as mock_scope,
            patch("main.run_other_checks", return_value=[]),
        ):
            _rc, results = main.run_commit_check()
        return results, mock_scope

    def test_pr_body_is_a_scope_checked_with_the_ai_rules_only(self):
        results, mock_scope = self._run(True, "false")
        mock_scope.assert_called_once_with(
            "PR description", ["--ai-attribution-only"], input_text="Adds a parser."
        )
        self.assertEqual([r.label for r in results], ["PR description"])

    def test_no_ai_rule_ran_reads_as_skipped_not_passed(self):
        """Under ai_attribution = "ignore" the CLI runs no rule on the description."""
        results, _ = self._run(True, "false")
        self.assertEqual(results[0].status, "skip")
        self.assertEqual(results[0].skip_reason, main.AI_POLICY_IGNORED)

    def test_a_rule_that_ran_keeps_its_verdict(self):
        checks = [{"rule_id": "CC013", "check": "ai_attribution", "status": "pass"}]
        scope = main.ScopeResult(label="PR description", checks=list(checks))
        self.assertEqual(main.skip_when_no_ai_rule_ran(scope).checks, checks)

    def test_unparsable_output_still_fails(self):
        scope = main.ScopeResult(label="PR description", raw_text="Error: bad config")
        self.assertEqual(main.skip_when_no_ai_rule_ran(scope).status, "fail")

    def test_ai_label_alone_reads_it_but_reports_no_scope(self):
        results, mock_scope = self._run(False, "true")
        mock_scope.assert_called_once()
        self.assertEqual(results, [])
        self.assertEqual(main.PR_BODY_AI_TOOLS, ["OpenAI Codex"])

    def test_off_by_default(self):
        results, mock_scope = self._run(False, "false")
        mock_scope.assert_not_called()
        self.assertEqual(results, [])

    def test_no_description_is_checked_as_empty_text(self):
        """A description left empty has no AI declaration for CC501 to find."""
        results, mock_scope = self._run(True, "true", body=None)
        mock_scope.assert_called_once_with(
            "PR description", ["--ai-attribution-only"], input_text=""
        )
        self.assertEqual([r.label for r in results], ["PR description"])

    def test_no_description_nothing_to_label(self):
        results, mock_scope = self._run(False, "true", body=None)
        mock_scope.assert_not_called()
        self.assertEqual(results, [])


class TestDescriptionLineChanges(unittest.TestCase):
    BODY = "## Summary\nAdds a parser.\n\nLink to Devin run: https://app.devin.ai/sessions/1\n"

    def _scope(self, *checks):
        return main.ScopeResult(label="PR description", checks=list(checks))

    def test_lines_removed_keeps_order_and_counts_copies(self):
        self.assertEqual(main.lines_removed("a\nb\na\n\nc", "a\nc"), ["b", "a"])

    def test_forbid_names_the_line_to_delete(self):
        scope = self._scope(
            {
                "rule_id": "CC013",
                "suggest": "Remove the AI lines.",
                "fix": "## Summary\nAdds a parser.",
            }
        )
        main.name_the_line_changes(scope, self.BODY)
        self.assertEqual(
            scope.checks[0]["suggest"],
            'In the description, remove "Link to Devin run: https://app.devin.ai/sessions/1"',
        )
        self.assertEqual(scope.checks[0]["fix"], "")
        # The fix is gone from the check, so the lines are kept for the diff.
        self.assertEqual(
            scope.edits,
            {"CC013": (["Link to Devin run: https://app.devin.ai/sessions/1"], [])},
        )

    def test_disclose_names_the_line_to_add_and_the_one_it_replaces(self):
        body = "Adds a parser.\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
        scope = self._scope(
            {
                "rule_id": "CC015",
                "suggest": "x",
                "fix": "Adds a parser.\n\nAssisted-by: Claude",
            },
            {"rule_id": "CC014", "suggest": "x", "fix": body + "\nAssisted-by: Claude"},
        )
        main.name_the_line_changes(scope, body)
        self.assertEqual(
            [c["suggest"] for c in scope.checks],
            [
                (
                    'In the description, remove "Co-Authored-By: Claude'
                    ' <noreply@anthropic.com>", and add "Assisted-by: Claude"'
                ),
                'In the description, add "Assisted-by: Claude"',
            ],
        )

    def test_a_check_without_a_fix_or_a_change_is_left_alone(self):
        scope = self._scope(
            {"rule_id": "CC013", "suggest": "s", "fix": ""},
            {"rule_id": "CC013", "suggest": "s", "fix": "same"},
        )
        main.name_the_line_changes(scope, "same")
        self.assertEqual(
            scope.checks,
            [
                {"rule_id": "CC013", "suggest": "s", "fix": ""},
                {"rule_id": "CC013", "suggest": "s", "fix": "same"},
            ],
        )

    def test_run_names_the_lines_for_a_reported_description(self):
        scope = self._scope(
            {
                "rule_id": "CC013",
                "status": "fail",
                "suggest": "s",
                "fix": "## Summary\nAdds a parser.",
            }
        )
        with (
            patch("main.PR_TITLE_ENABLED", False),
            patch("main.PR_BODY_AI_ATTRIBUTION_ENABLED", True),
            patch.dict(os.environ, {"AI_LABEL": "false"}),
            patch("main.MESSAGE_ENABLED", False),
            patch("main.is_pr_event", return_value=True),
            patch("main.get_pr_body", return_value=self.BODY),
            patch("main.check_scope", return_value=scope),
            patch("main.run_other_checks", return_value=[]),
        ):
            _rc, results = main.run_commit_check()
        self.assertEqual(results[0].checks[0]["fix"], "")


class TestPrAuthorChecksEveryCommit(unittest.TestCase):
    """The author checks read every PR commit's author, not only the tip."""

    def setUp(self):
        main.API_AUTHORS.clear()
        self.addCleanup(main.API_AUTHORS.clear)

    def _run(self, commits, authors, resolves=True, message=False):
        commands: list[list[str]] = []

        def run(command, **_kwargs):
            commands.append(command)
            if command[:2] == ["git", "rev-parse"]:
                return MagicMock(returncode=0 if resolves else 1, stdout="")
            if command[:2] == ["git", "fetch"]:
                return MagicMock(returncode=1, stdout="")
            if "--message" in command:
                return MagicMock(
                    returncode=0, stdout=json_output(make_check("message"))
                )
            check = command[3].lstrip("-").replace("-", "_")
            sha = command[-1]
            status = "fail" if "noreply" in authors[sha][1] else "pass"
            return MagicMock(
                returncode=0,
                stdout=json_output(make_check(check, status=status, value=sha)),
            )

        with (
            patch("main.MESSAGE_ENABLED", message),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.get_pr_commit_messages", return_value=commits) as listed,
            patch("main._local_author", side_effect=lambda sha: authors.get(sha)),
            patch("main.get_pr_head_sha", return_value=commits[-1][0]),
            patch("main.subprocess.run", side_effect=run),
            patch("sys.stdout", io.StringIO()),
        ):
            rc, results = main.run_commit_check()
        self.listed_calls = listed.call_count
        return rc, results, commands

    def test_the_commits_are_listed_once_for_messages_and_authors(self):
        commits = [("a1", "feat: one")]
        _rc, results, _commands = self._run(
            commits, {"a1": ("Dev", "dev@example.org")}, message=True
        )
        self.assertEqual(self.listed_calls, 1)
        self.assertEqual([s.label for s in results], ["Commit 1/1", "Author email"])

    def test_a_commit_whose_author_cannot_be_read_is_passed_over(self):
        commits = [("a1", "feat: one"), ("b2", "feat: two")]
        _rc, results, _commands = self._run(commits, {"b2": ("Dev", "dev@example.org")})
        self.assertEqual([(s.label, s.sha) for s in results], [("Author email", "b2")])

    def test_an_earlier_commit_by_another_author_is_checked(self):
        """FreeBSD atf forbids noreply emails; the tip alone hid this one."""
        commits = [("a1", "feat: one"), ("b2", "feat: two")]
        authors = {
            "a1": ("Dev", "dev@users.noreply.github.com"),
            "b2": ("Dev", "dev@example.org"),
        }
        rc, results, commands = self._run(commits, authors)
        self.assertEqual(rc, 1)
        self.assertEqual(
            [(s.label, s.sha, s.status) for s in results],
            [("Author email", "a1", "fail"), ("Author email", "b2", "pass")],
        )
        self.assertIn(
            ["commit-check", "--format", "json", "--author-email", "--rev", "a1"],
            commands,
        )

    def test_one_author_is_checked_once_on_the_newest_commit(self):
        commits = [("a1", "feat: one"), ("b2", "feat: two"), ("c3", "feat: three")]
        same = ("Dev", "dev@example.org")
        rc, results, _commands = self._run(
            commits, dict.fromkeys(("a1", "b2", "c3"), same)
        )
        self.assertEqual(rc, 0)
        self.assertEqual([(s.label, s.sha) for s in results], [("Author email", "c3")])

    def test_an_api_listed_author_is_used(self):
        main.API_AUTHORS["a1"] = ("Dev", "dev@users.noreply.github.com")
        _rc, results, _ = self._run(
            [("a1", "feat: one")], {"a1": ("Dev", "dev@users.noreply.github.com")}
        )
        self.assertEqual([(s.sha, s.status) for s in results], [("a1", "fail")])

    def test_a_commit_that_cannot_be_had_falls_back_to_the_tip(self):
        """Neither in the clone nor fetchable: the tip path decides (here: skip)."""
        commits = [("a1", "feat: one")]
        _rc, results, commands = self._run(
            commits, {"a1": ("Dev", "dev@example.org")}, resolves=False
        )
        self.assertEqual(
            [(s.label, s.status) for s in results], [("Author email", "skip")]
        )
        self.assertFalse([c for c in commands if c[0] == "commit-check"], commands)


class TestEmptyCommitMessage(unittest.TestCase):
    """An empty message is checked as empty, never as HEAD's message."""

    def test_an_empty_message_goes_to_the_cli_as_an_empty_file(self):
        seen: list[tuple[list[str], str | None, str]] = []

        def run(command, **kwargs):
            path = command[-1]
            with open(path, encoding="utf-8") as f:
                seen.append((command, kwargs.get("input"), f.read()))
            return MagicMock(
                returncode=1,
                stdout=json_output(make_check("allow_empty_commits", status="fail")),
                stderr="",
            )

        with (
            patch("main.subprocess.run", side_effect=run),
            patch("main._local_author", return_value=None),
        ):
            results = main.run_pr_message_checks([("a1", "")])
        command, piped, content = seen[0]
        self.assertEqual(command[:4], ["commit-check", "--format", "json", "--message"])
        self.assertIsNone(piped)
        self.assertEqual(content, "")
        self.assertFalse(os.path.exists(command[-1]))
        self.assertEqual(
            [(s.label, s.sha, s.status) for s in results],
            [("Commit 1/1", "a1", "fail")],
        )


class TestShallowBoundary(unittest.TestCase):
    """The commits a shallow clone holds without their parents."""

    def test_a_shallow_clone_lists_them_in_its_shallow_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shallow")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{SHA_A}\n{SHA_B}\n")
            located = MagicMock(returncode=0, stdout=f"{path}\n")
            with patch("main.subprocess.run", return_value=located) as run:
                self.assertEqual(main._shallow_boundary(), {SHA_A, SHA_B})
        self.assertEqual(
            run.call_args.args[0], ["git", "rev-parse", "--git-path", "shallow"]
        )

    def test_a_complete_clone_has_no_such_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            located = MagicMock(returncode=0, stdout=os.path.join(tmp, "shallow"))
            with patch("main.subprocess.run", return_value=located):
                self.assertEqual(main._shallow_boundary(), set())

    def test_outside_a_repository_there_is_none(self):
        with patch("main.subprocess.run", return_value=MagicMock(returncode=128)):
            self.assertEqual(main._shallow_boundary(), set())

    def test_without_git_there_is_none(self):
        with patch("main.subprocess.run", side_effect=OSError("no git")):
            self.assertEqual(main._shallow_boundary(), set())


class TestRunFilesChecks(unittest.TestCase):
    """``files: true`` checks the files of each commit, one scope per commit."""

    def _run(self, shas, answers, boundary=(), missing=()):
        """Run the files checks; ``answers`` maps a rev to the CLI's checks."""
        calls: list[list[str]] = []

        def check_scope(label, args, sha=""):
            calls.append(args)
            answer = answers.get(args[-1], [])
            if isinstance(answer, str):
                return main.ScopeResult(label=label, raw_text=answer, sha=sha)
            return main.ScopeResult(label=label, checks=list(answer), sha=sha)

        out = io.StringIO()
        with (
            patch("main._shallow_boundary", return_value=set(boundary)),
            patch("main._rev_resolves", side_effect=lambda sha: sha not in missing),
            patch("main.check_scope", side_effect=check_scope),
            patch("sys.stdout", out),
        ):
            results = main.run_files_checks(shas)
        return results, calls, out.getvalue()

    def test_every_commit_is_checked_at_its_own_revision(self):
        results, calls, out = self._run(
            ["a1", "b2"], {"a1": [files_pass()], "b2": files_scope().checks}
        )
        self.assertEqual(
            calls, [["--files", "--rev", "a1"], ["--files", "--rev", "b2"]]
        )
        self.assertEqual(
            [(s.label, s.sha, s.status) for s in results],
            [("Files", "a1", "pass"), ("Files", "b2", "fail")],
        )
        self.assertEqual(out, "")

    def test_a_commit_without_its_parent_is_skipped_not_guessed_at(self):
        """At the edge of a shallow clone every file would look newly added."""
        results, calls, out = self._run(
            ["a1", "b2", "c3"],
            {"c3": [files_pass()]},
            boundary=["b2"],
            missing=["a1"],
        )
        self.assertEqual(calls, [["--files", "--rev", "c3"]])
        self.assertEqual(
            [(s.sha, s.status, s.skip_reason) for s in results],
            [
                ("a1", "skip", main.FILES_NOT_IN_CLONE),
                ("b2", "skip", main.FILES_NOT_IN_CLONE),
                ("c3", "pass", ""),
            ],
        )
        self.assertIn(
            "::warning title=commit-check::2 of 3 commits are not in the checkout "
            "together with their parent (is actions/checkout using fetch-depth: 0?); "
            "their files were not checked",
            out,
        )

    def test_a_checkout_with_none_of_them_gets_one_skipped_scope(self):
        results, calls, out = self._run(
            ["a1", "b2"], {"--files": [files_pass()]}, missing=["a1", "b2"]
        )
        # HEAD is only asked whether any rule is set; its verdict is not used.
        self.assertEqual(calls, [["--files"]])
        self.assertEqual(
            [(s.label, s.sha, s.status, s.skip_reason) for s in results],
            [("Files", "", "skip", main.FILES_NOT_IN_CLONE)],
        )
        self.assertIn("the files check was skipped", out)

    def test_an_unknown_head_is_not_checked(self):
        """Outside a PR the one commit is HEAD; git may fail to name it."""
        results, calls, _out = self._run([""], {"--files": "boom"})
        self.assertEqual(calls, [["--files"]])
        self.assertEqual(results[0].skip_reason, main.FILES_NOT_IN_CLONE)

    def test_no_rule_set_is_one_skipped_scope_and_no_shallow_warning(self):
        for missing in ([], ["a1", "b2"]):
            results, _calls, out = self._run(["a1", "b2"], {}, missing=missing)
            self.assertEqual(
                [(s.label, s.sha, s.status, s.skip_reason) for s in results],
                [("Files", "", "skip", main.FILES_UNCONFIGURED)],
            )
            self.assertEqual(out, "")

    def test_a_commit_that_changes_no_file_says_so(self):
        skipped = files_pass(value="", status="skip")
        skipped["reason"] = ""
        results, _calls, _out = self._run(["a1"], {"a1": [skipped]})
        self.assertEqual(results[0].status, "skip")
        self.assertEqual(results[0].skip_reason, main.FILES_NOTHING_CHANGED)

    def test_the_clis_own_skip_reason_is_kept(self):
        skipped = files_pass(value="", status="skip")
        skipped["reason"] = "what the commit changed could not be read"
        results, _calls, _out = self._run(["a1"], {"a1": [skipped]})
        self.assertEqual(
            results[0].skip_reason, "what the commit changed could not be read"
        )

    def test_unparsable_output_is_a_failure_of_that_commit(self):
        results, _calls, _out = self._run(["a1"], {"a1": "Traceback ..."})
        self.assertEqual([(s.sha, s.status) for s in results], [("a1", "fail")])

    def test_a_pull_request_whose_commits_cannot_be_listed_is_skipped(self):
        out = io.StringIO()
        with patch("sys.stdout", out), patch("main.check_scope") as check:
            results = main.run_pr_files_checks([])
        check.assert_not_called()
        self.assertEqual(
            [(s.label, s.status, s.skip_reason) for s in results],
            [("Files", "skip", main.FILES_UNLISTED)],
        )
        self.assertIn("Could not list the pull request's commits", out.getvalue())

    def test_a_pull_request_hands_over_its_commits(self):
        with patch("main.run_files_checks", return_value=[]) as run:
            main.run_pr_files_checks([("a1", "feat: one"), ("b2", "feat: two")])
        run.assert_called_once_with(["a1", "b2"])


class TestRunCommitCheckFiles(unittest.TestCase):
    def _run(self, event="pull_request", message=True, files=True):
        # Only a pull request has commits to list.
        listable = event == "pull_request"
        commits = [("a1", "feat: one"), ("b2", "feat: two")] if listable else []

        def check_scope(label, args, **kwargs):
            return main.ScopeResult(
                label=label, checks=[files_pass()], sha=kwargs.get("sha", "")
            )

        with (
            patch("main.MESSAGE_ENABLED", message),
            patch("main.BRANCH_ENABLED", True),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.FILES_ENABLED", files),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": event}),
            patch("main.get_pr_commit_messages", return_value=commits) as listed,
            patch("main._local_author", return_value=None),
            patch("main._shallow_boundary", return_value=set()),
            patch("main._rev_resolves", return_value=True),
            patch("main.head_sha", return_value="h0"),
            patch("main.check_scope", side_effect=check_scope),
        ):
            _rc, results = main.run_commit_check()
        self.listed_calls = listed.call_count
        return [(s.label, s.sha) for s in results]

    def test_off_by_default(self):
        self.assertFalse(main.FILES_ENABLED)
        self.assertNotIn("Files", [label for label, _ in self._run(files=False)])

    def test_a_pull_request_gets_one_scope_per_commit_after_the_messages(self):
        self.assertEqual(
            self._run(),
            [
                ("Commit 1/2", "a1"),
                ("Commit 2/2", "b2"),
                ("Files", "a1"),
                ("Files", "b2"),
                ("Branch", ""),
            ],
        )
        self.assertEqual(self.listed_calls, 1)

    def test_the_commits_are_listed_for_the_files_alone(self):
        self.assertEqual(
            self._run(message=False),
            [("Files", "a1"), ("Files", "b2"), ("Branch", "")],
        )
        self.assertEqual(self.listed_calls, 1)

    def test_outside_a_pull_request_the_head_commit_is_checked(self):
        self.assertEqual(
            self._run(event="push"),
            [("Commit message", "h0"), ("Files", "h0"), ("Branch", "")],
        )
