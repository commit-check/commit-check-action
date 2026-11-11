"""Unit tests for main.py."""

import functools
import importlib.metadata
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("GITHUB_STEP_SUMMARY", "/tmp/step_summary.txt")

import main

#: The report footer names the installed commit-check version, which differs
#: between a contributor's machine and CI. Golden tests pin it so they assert on
#: the report layout rather than on whatever version happens to be installed.
PINNED_VERSION = "2.13.1"
HOOK_GUIDE = "https://commit-check.com/guides/integrations/#as-a-pre-commit-hook"
FOOTER = (
    f"<sub>Checked by [Commit Check](https://commit-check.com) {PINNED_VERSION} · "
    "[Rules](https://commit-check.com/rules/) · "
    f"[Check before you push]({HOOK_GUIDE})</sub>"
)
MARKS = "https://raw.githubusercontent.com/commit-check/.github/main/branding/status"


def heading(glyph: str, word: str, verdict: str) -> str:
    """The report's first visible line: the status tile and the verdict."""
    return (
        f'## <img alt="{glyph}" width="24" height="24" align="top" '
        f'src="{MARKS}/{word}.png"> Commit Check {verdict}'
    )


def group_line(glyph: str, word: str, text: str) -> str:
    """One group's line of the report: its line mark, name and result."""
    return (
        f'<img alt="{glyph}" width="16" height="16" align="top" '
        f'src="{MARKS}/line-{word}.png"> {text}'
    )


#: The step log's colour codes, which most assertions read past.
ANSI_CODES = re.compile(r"\x1b\[[0-9;]*m")
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
    fix: str = "",
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
        "fix": fix,
        "docs_url": docs_url,
    }


#: Full hashes for scopes that carry a commit; the first seven characters
#: are what a reader sees.
SHA_A = "d87faca811e7017bbaa82f5c53eade0a97c108d8"
SHA_B = "5584f462cc3c947b2ba8d3d1a5735571803ee159"

#: ``git log`` output in COMMIT_LOG_FORMAT for two commits: hash, NUL, message
#: (with its trailing newline), NUL, then git's own newline before the next.
TWO_COMMITS_LOG = f"{SHA_A}\x00fix: first\n\x00\n{SHA_B}\x00feat: second\n\x00"


def json_output(*checks) -> str:
    """Serialize checks to the CLI JSON output shape."""
    status = "fail" if any(c["status"] == "fail" for c in checks) else "pass"
    return json.dumps({"status": status, "checks": list(checks)})


def pass_scope(label: str = "Branch", value: str = "") -> main.ScopeResult:
    return main.ScopeResult(label=label, checks=[make_check("branch", value=value)])


def fail_scope(label: str = "Commit 1/1", sha: str = "") -> main.ScopeResult:
    return main.ScopeResult(
        label=label,
        sha=sha,
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


def fix_scope(label: str = "Commit 2/3", sha: str = SHA_B) -> main.ScopeResult:
    """A CC002 failure as commit-check 2.17 reports it: a mechanical fix, and
    a ``suggest`` the engine derived from that same fix."""
    return main.ScopeResult(
        label=label,
        sha=sha,
        checks=[
            make_check(
                "subject_capitalized",
                status="fail",
                rule_id="CC002",
                value="feat: add login page",
                error="Subject must start with a capital letter",
                suggest='Use "feat: Add login page"',
                fix="feat: Add login page",
                docs_url="https://commit-check.com/rules/#cc002",
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


class TestLogEnvVars(unittest.TestCase):
    def test_every_input_is_logged_as_a_debug_command(self):
        """``::debug::`` only shows with ACTIONS_STEP_DEBUG, so every input can
        be logged without cluttering the ordinary log; an unset one reads as
        its default, ``false``."""
        buffer = io.StringIO()
        with (
            patch.dict(
                os.environ, {"MESSAGE": "true", "PR_TITLE": "false"}, clear=True
            ),
            patch("sys.stdout", buffer),
        ):
            main.log_env_vars()
        self.assertEqual(
            buffer.getvalue().splitlines(),
            [
                "::debug::MESSAGE=true",
                "::debug::BRANCH=false",
                "::debug::AUTHOR_NAME=false",
                "::debug::AUTHOR_EMAIL=false",
                "::debug::DRY_RUN=false",
                "::debug::JOB_SUMMARY=false",
                "::debug::PR_COMMENTS=false",
                "::debug::PR_TITLE=false",
                "::debug::PR_BODY_AI_ATTRIBUTION=false",
                "::debug::MESSAGE_AI_ATTRIBUTION=false",
                "::debug::AI_LABEL=false",
            ],
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
        result = main.parse_commit_messages(
            f"{SHA_A}\x00\nfix: first\n\x00\n{SHA_B}\x00\nfeat: second\n\n\x00"
        )
        self.assertEqual(result, [(SHA_A, "fix: first"), (SHA_B, "feat: second")])

    def test_real_git_log_layout(self):
        self.assertEqual(
            main.parse_commit_messages(TWO_COMMITS_LOG),
            [(SHA_A, "fix: first"), (SHA_B, "feat: second")],
        )

    def test_the_format_asks_git_for_the_hash_and_the_body(self):
        """A literal NUL cannot be an argument; git spells it %x00."""
        self.assertEqual(main.COMMIT_LOG_FORMAT, "--pretty=format:%H%x00%B%x00")
        self.assertNotIn("\x00", main.COMMIT_LOG_FORMAT)

    def test_an_empty_message_is_kept_and_does_not_shift_the_hashes(self):
        output = f"{SHA_A}\x00\n\x00\n{SHA_B}\x00feat: second\n\x00"
        self.assertEqual(
            main.parse_commit_messages(output),
            [(SHA_A, ""), (SHA_B, "feat: second")],
        )

    def test_empty_output_is_no_commits(self):
        self.assertEqual(main.parse_commit_messages(""), [])


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


class TestLocalAuthor(unittest.TestCase):
    """The author of a commit the clone holds, read from git."""

    def test_reads_name_and_email(self):
        result = MagicMock(returncode=0, stdout="Jane Dev\x1fjane@example.com\n")
        with patch("main.subprocess.run", return_value=result) as mock_run:
            self.assertEqual(
                main._local_author(SHA_A), ("Jane Dev", "jane@example.com")
            )
        self.assertEqual(
            mock_run.call_args[0][0],
            ["git", "show", "-s", "--format=%an%x1f%ae", SHA_A],
        )

    def test_a_commit_git_cannot_show_has_no_author(self):
        result = MagicMock(returncode=128, stdout="")
        with patch("main.subprocess.run", return_value=result):
            self.assertIsNone(main._local_author(SHA_A))

    def test_output_without_the_separator_has_no_author(self):
        result = MagicMock(returncode=0, stdout="unexpected\n")
        with patch("main.subprocess.run", return_value=result):
            self.assertIsNone(main._local_author(SHA_A))

    def test_missing_git_has_no_author(self):
        with patch("main.subprocess.run", side_effect=OSError("no git")):
            self.assertIsNone(main._local_author(SHA_A))

    def test_reads_a_real_commit(self):
        with tempfile.TemporaryDirectory() as repo:
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Renovate Bot",
                "GIT_AUTHOR_EMAIL": "bot@renovateapp.com",
                "GIT_COMMITTER_NAME": "Renovate Bot",
                "GIT_COMMITTER_EMAIL": "bot@renovateapp.com",
            }
            run = functools.partial(
                subprocess.run, cwd=repo, env=env, check=True, capture_output=True
            )
            run(["git", "init", "-q"])
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "chore: bump",
                ]
            )
            sha = run(["git", "rev-parse", "HEAD"], text=True).stdout.strip()
            cwd = os.getcwd()
            os.chdir(repo)
            try:
                author = main._local_author(sha)
            finally:
                os.chdir(cwd)
        self.assertEqual(author, ("Renovate Bot", "bot@renovateapp.com"))


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
    def setUp(self):
        # Never the event of whatever CI run the tests happen to be in.
        patcher = patch("main.get_pr_event", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_a_range_cut_short_by_a_shallow_clone_asks_the_api(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.get_messages_from_event_range", return_value=[("b2", "fix")]),
            patch("main.get_pr_event", return_value={"commits": 2}),
            patch("main.get_messages_from_api", return_value=[]) as api,
            patch("main.warn_shallow_checkout") as warn,
        ):
            # The API cannot help either: check what the clone had, and say so.
            self.assertEqual(main.get_pr_commit_messages(), [("b2", "fix")])
        api.assert_called_once()
        self.assertIn("Only 1 of the pull request's 2 commits", warn.call_args[0][0])

    def test_asks_the_api_when_the_clone_holds_none_of_them(self):
        with (
            patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": "main"},
            ),
            patch("main.get_messages_from_event_range", return_value=[]),
            patch("main.get_messages_from_merge_ref", return_value=[]),
            patch("main.get_messages_from_head_ref", return_value=[]),
            patch("main.get_messages_from_api", return_value=[("a1", "feat: one")]),
        ):
            self.assertEqual(main.get_pr_commit_messages(), [("a1", "feat: one")])

    def test_nothing_listed_anywhere_is_empty_and_not_warned_here(self):
        """With no partial list there is no "only N of M" to report; the
        caller warns that it fell back to HEAD."""
        with (
            patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": ""},
            ),
            patch("main.get_messages_from_event_range", return_value=[]),
            patch("main.get_messages_from_merge_ref", return_value=[]),
            patch("main.get_messages_from_head_ref") as mock_head,
            patch("main.get_messages_from_api", return_value=[]) as api,
            patch("main.warn_shallow_checkout") as warn,
        ):
            self.assertEqual(main.get_pr_commit_messages(), [])
        # No base ref to diff against, so the head-ref reader is never asked.
        mock_head.assert_not_called()
        api.assert_called_once()
        warn.assert_not_called()

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
        mock_result = MagicMock(returncode=0, stdout=TWO_COMMITS_LOG)
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.subprocess.run", return_value=mock_result) as mock_run,
        ):
            result = main.get_messages_from_merge_ref()
        self.assertEqual(result, [(SHA_A, "fix: first"), (SHA_B, "feat: second")])
        self.assertEqual(
            mock_run.call_args[0][0],
            ["git", "log", main.COMMIT_LOG_FORMAT, "--reverse", "HEAD^1..HEAD^2"],
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
            return MagicMock(returncode=0, stdout=TWO_COMMITS_LOG)

        with (
            patch("main.get_pr_base_sha", return_value="base111"),
            patch("main.get_pr_head_sha", return_value="head222"),
            patch("main.subprocess.run", side_effect=run),
        ):
            result = main.get_messages_from_event_range()
        self.assertEqual(result, [(SHA_A, "fix: first"), (SHA_B, "feat: second")])
        self.assertIn(
            ["git", "log", main.COMMIT_LOG_FORMAT, "--reverse", "base111..head222"],
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
        mock_result = MagicMock(returncode=0, stdout=f"{SHA_A}\x00fix: first\n\x00")
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            result = main.get_messages_from_head_ref("main")
        self.assertEqual(result, [(SHA_A, "fix: first")])
        self.assertEqual(
            mock_run.call_args[0][0],
            [
                "git",
                "log",
                main.COMMIT_LOG_FORMAT,
                "--reverse",
                "origin/main..HEAD",
            ],
        )


class TestHeadSha(unittest.TestCase):
    def test_returns_the_full_hash(self):
        mock_result = MagicMock(returncode=0, stdout=f"{SHA_A}\n")
        with patch("main.subprocess.run", return_value=mock_result) as mock_run:
            self.assertEqual(main.head_sha(), SHA_A)
        self.assertEqual(mock_run.call_args[0][0], ["git", "rev-parse", "HEAD"])

    def test_no_head_is_empty(self):
        with patch("main.subprocess.run", return_value=MagicMock(returncode=128)):
            self.assertEqual(main.head_sha(), "")

    def test_missing_git_is_empty(self):
        with patch("main.subprocess.run", side_effect=OSError("no git")):
            self.assertEqual(main.head_sha(), "")


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
            "PR title", ["--message"], input_text="feat: a feature"
        )

    def test_pr_title_is_not_held_to_commit_only_rules(self):
        # require_body and require_signed_off_by judge a commit; a title has
        # no body or trailers, so it could never pass them.
        title = main.ScopeResult(
            label="PR title",
            checks=[
                make_check("message", "pass", "CC001", value="feat: add login"),
                make_check("require_body", "fail", "CC011", value="feat: add login"),
                make_check("require_signed_off_by", "fail", "CC012"),
                make_check("ai_author", "fail", "CC017", value="Claude <x@y.z>"),
            ],
        )
        with (
            patch("main.PR_TITLE_ENABLED", True),
            patch("main.MESSAGE_ENABLED", False),
            patch("main.BRANCH_ENABLED", False),
            patch("main.AUTHOR_NAME_ENABLED", False),
            patch("main.AUTHOR_EMAIL_ENABLED", False),
            patch("main.is_pr_event", return_value=True),
            patch("main.get_pr_title", return_value="feat: add login"),
            patch("main.check_scope", return_value=title),
            patch("main.run_other_checks", return_value=[]),
        ):
            rc, results = main.run_commit_check()
        self.assertEqual(rc, 0)
        self.assertEqual([c["rule_id"] for c in results[0].checks], ["CC001"])

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


class TestFetchPrHead(unittest.TestCase):
    def test_a_head_the_clone_lacks_is_fetched_without_history(self):
        # rev-parse fails, the fetch succeeds, rev-parse now resolves.
        run = MagicMock(
            side_effect=[
                MagicMock(returncode=1),
                MagicMock(returncode=0),
                MagicMock(returncode=0),
            ]
        )
        with (
            patch("main.get_pr_head_sha", return_value="abc123"),
            patch("main.subprocess.run", run),
        ):
            self.assertEqual(main.pr_head_rev(), "abc123")
        self.assertEqual(
            run.call_args_list[1][0][0],
            ["git", "fetch", "--no-tags", "--quiet", "--depth=1", "origin", "abc123"],
        )

    def test_a_fetch_without_git_gives_up(self):
        with patch("main.subprocess.run", side_effect=OSError("no git")):
            self.assertFalse(main._fetch_commit("abc123"))


class TestGetMessagesFromApi(unittest.TestCase):
    """A clone without the pull request's commits lists them through the API."""

    def _run(self, total, api_commits, head="b2"):
        pull = MagicMock()
        pull.get_commits.return_value = [
            MagicMock(
                sha=sha,
                commit=MagicMock(
                    message=msg,
                    author=MagicMock(email=f"{sha}@example.com"),
                ),
            )
            for sha, msg in api_commits
        ]
        for listed in pull.get_commits.return_value:
            listed.commit.author.name = f"author {listed.sha}"
        github_module = MagicMock()
        github_module.Github.return_value.get_repo.return_value.get_pull.return_value = pull
        env = {
            "GITHUB_TOKEN": "token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_API_URL": "https://ghe.example.com/api/v3",
        }
        with (
            patch.dict(os.environ, env),
            patch("main.get_pr_event", return_value={"number": 7, "commits": total}),
            patch("main.get_pr_head_sha", return_value=head),
            patch.dict(sys.modules, {"github": github_module}),
        ):
            return main.get_messages_from_api(), github_module

    def test_lists_the_commits_oldest_first(self):
        commits = [("a1", "feat: one"), ("b2", "fix: two")]
        result, github_module = self._run(2, commits)
        self.assertEqual(result, commits)
        self.assertEqual(
            github_module.Github.call_args[1]["base_url"],
            "https://ghe.example.com/api/v3",
        )

    def test_records_each_commit_author(self):
        main.API_AUTHORS.clear()
        self.addCleanup(main.API_AUTHORS.clear)
        self._run(2, [("a1", "feat: one"), ("b2", "fix: two")])
        self.assertEqual(
            main.API_AUTHORS,
            {
                "a1": ("author a1", "a1@example.com"),
                "b2": ("author b2", "b2@example.com"),
            },
        )

    def test_a_commit_without_an_author_records_nothing(self):
        main.API_AUTHORS.clear()
        self.addCleanup(main.API_AUTHORS.clear)
        pull = MagicMock()
        pull.get_commits.return_value = [
            MagicMock(sha="b2", commit=MagicMock(message="fix: two", author=None))
        ]
        github_module = MagicMock()
        github_module.Github.return_value.get_repo.return_value.get_pull.return_value = pull
        env = {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "owner/repo"}
        with (
            patch.dict(os.environ, env),
            patch("main.get_pr_event", return_value={"number": 7, "commits": 1}),
            patch("main.get_pr_head_sha", return_value="b2"),
            patch.dict(sys.modules, {"github": github_module}),
        ):
            self.assertEqual(main.get_messages_from_api(), [("b2", "fix: two")])
        self.assertEqual(main.API_AUTHORS, {})

    def test_a_list_that_ends_elsewhere_is_not_used(self):
        # A force push after the event: same count, other commits.
        result, _ = self._run(2, [("a1", "feat: one"), ("c3", "fix: other")])
        self.assertEqual(result, [])

    def test_a_partial_list_is_not_used(self):
        result, _ = self._run(3, [("a1", "feat: one"), ("b2", "fix: two")])
        self.assertEqual(result, [])

    def test_more_than_the_api_returns_is_left_unlisted(self):
        result, github_module = self._run(main.API_COMMIT_LIMIT + 1, [])
        self.assertEqual(result, [])
        github_module.Github.assert_not_called()


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
    def _run(self, results, colours=False):
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            main.render_step_log(results)
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
        scope = main.ScopeResult(
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
        scope = main.ScopeResult(
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
            self.assertEqual(main._log_safe(text), text)

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
            main._finding_lines(fix_scope().checks[0]),
            [
                "value: feat: add login page",
                "Subject must start with a capital letter",
                "Fix: feat: Add login page",
            ],
        )

    def test_annotation_without_an_error_still_names_the_kind_of_finding(self):
        scope = main.ScopeResult(
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
        scope = main.ScopeResult(
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
        scope = main.ScopeResult(
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
            main.ScopeResult(label="Commit 3/3", sha=SHA_A, raw_text="garbage"),
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
        with patch("main.JOB_SUMMARY_ENABLED", True):
            output = self._run([fail_scope("Commit 1/1")])
        self.assertTrue(
            output.endswith(
                "How to fix: see the job summary. "
                f"Check before you push: {HOOK_GUIDE}\n"
            )
        )
        # With no summary to send the reader to, the log is the report.
        with patch("main.JOB_SUMMARY_ENABLED", False):
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
        scope = main.ScopeResult(
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
        groups = [name for name, _ in main._grouped(results)]
        self.assertEqual(groups, ["Pull request", "Commits"])

    def test_raw_text_fallback_is_printed(self):
        scope = main.ScopeResult(label="Branch", raw_text="unexpected output")
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
        with patch("main.DRY_RUN_ENABLED", True):
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
        with patch("main.DRY_RUN_ENABLED", True):
            output = self._run([pass_scope("Branch")])
        self.assertIn("✔ Commit Check: all checks passed", output)
        self.assertNotIn("dry-run", output)


#: Fixed lines of the report's "How to fix" section.
AGENT_PROMPT = "Fix these Commit Check findings without changing any code:"
HOOK_HELP = (
    f"**Next time.** The [pre-commit hook]({HOOK_GUIDE}) runs the same "
    "rules before you push."
)


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
        results[1].sha, results[2].sha = SHA_A, SHA_B
        body = main.render_report(results)
        self.assertEqual(
            body,
            f"{main.COMMENT_MARKER}\n"
            f"{heading('✔', 'passed', 'passed')}\n"
            "\n"
            f"{group_line('✔', 'passed', 'Pull request: passed')}<br>\n"
            f"{group_line('✔', 'passed', 'Commits: 2 passed')}<br>\n"
            f"{group_line('✔', 'passed', 'Branch: passed')}\n"
            "\n"
            "<details>\n"
            "<summary>All 4 checks</summary>\n"
            "\n"
            "```text\n"
            "Pull request\n"
            "  ✔ title        feat: add login page\n"
            "Commits\n"
            "  ✔ d87faca      feat: add user auth\n"
            "  ✔ 5584f46      fix: resolve timeout\n"
            "Branch\n"
            "  ✔ branch       feature/add-login\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    @pin_version
    def test_failure_golden_output(self):
        """Pin the full failure report: the diff of what failed, how to fix
        it, and every scope in the listing."""
        results = [
            pass_scope("PR title", value="feat: add login page"),
            fail_scope("Commit 2/2"),
            pass_scope("Branch", value="feature/add-login"),
        ]
        body = main.render_report(results)
        self.assertEqual(
            body,
            f"{main.COMMENT_MARKER}\n"
            f"{heading('✖', 'failed', 'failed')}\n"
            "\n"
            f"{group_line('✔', 'passed', 'Pull request: passed')}<br>\n"
            f"{group_line('✖', 'failed', 'Commits: failed')}<br>\n"
            f"{group_line('✔', 'passed', 'Branch: passed')}\n"
            "\n"
            "```diff\n"
            "@@ Commit 2/2 @@\n"
            "- bad message\n"
            "# Use <type>(<scope>): <description>\n"
            "```\n"
            "\n"
            "<sub>Rules: [CC001](https://commit-check.com/rules/#cc001)</sub>\n"
            "\n"
            "<details>\n"
            "<summary><b>How to fix</b></summary>\n"
            "\n"
            "**With an AI coding agent.** Paste this prompt:\n"
            "\n"
            "```text\n"
            f"{AGENT_PROMPT}\n"
            '- Commit 2/2 "bad message": Use <type>(<scope>): <description>\n'
            "```\n"
            "\n"
            "**Commit messages.** Reword it, then force-push:\n"
            "\n"
            "```bash\n"
            "git commit --amend\n"
            "git push --force-with-lease\n"
            "```\n"
            "\n"
            f"{HOOK_HELP}\n"
            "\n"
            "</details>\n"
            "\n"
            "<details>\n"
            "<summary>All 3 checks</summary>\n"
            "\n"
            "```text\n"
            "Pull request\n"
            "  ✔ title        feat: add login page\n"
            "Commits\n"
            "  ✖ 2/2          bad message\n"
            "      CC001 message\n"
            "        value: bad message\n"
            "        The commit message should follow Conventional Commits.\n"
            "        Suggest: Use <type>(<scope>): <description>\n"
            "Branch\n"
            "  ✔ branch       feature/add-login\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    @pin_version
    def test_failure_golden_output_with_commits(self):
        """Pin the report for a pull request's commits: each is named by its
        short hash, linked under the diff, and a mechanical fix is a green
        line."""
        results = [
            pass_scope("PR title", value="feat: Add login page"),
            fail_scope("Commit 1/3", sha=SHA_A),
            fix_scope("Commit 2/3", sha=SHA_B),
            pass_scope("Commit 3/3", value="fix: Handle timeout"),
            pass_scope("Branch", value="feature/add-login"),
        ]
        results[3].sha = "37d6def82ffcc6dbfa0af2ea1ec90512d89979ae"
        env = {"GITHUB_REPOSITORY": "acme/widgets", "GITHUB_SERVER_URL": ""}
        with patch.dict(os.environ, env):
            body = main.render_report(results)
        commits = "https://github.com/acme/widgets/commit"
        self.assertEqual(
            body,
            f"{main.COMMENT_MARKER}\n"
            f"{heading('✖', 'failed', 'failed')}\n"
            "\n"
            f"{group_line('✔', 'passed', 'Pull request: passed')}<br>\n"
            f"{group_line('✖', 'failed', 'Commits: 2 of 3 failed')}<br>\n"
            f"{group_line('✔', 'passed', 'Branch: passed')}\n"
            "\n"
            "```diff\n"
            "@@ d87faca @@\n"
            "- bad message\n"
            "# Use <type>(<scope>): <description>\n"
            "@@ 5584f46 @@\n"
            "- feat: add login page\n"
            "+ feat: Add login page\n"
            "```\n"
            "\n"
            f"<sub>Commits: [`d87faca`]({commits}/{SHA_A}) "
            f"[`5584f46`]({commits}/{SHA_B}) &nbsp;|&nbsp; "
            "Rules: [CC001](https://commit-check.com/rules/#cc001) · "
            "[CC002](https://commit-check.com/rules/#cc002)</sub>\n"
            "\n"
            "<details>\n"
            "<summary><b>How to fix</b></summary>\n"
            "\n"
            "**With an AI coding agent.** Paste this prompt:\n"
            "\n"
            "```text\n"
            f"{AGENT_PROMPT}\n"
            '- Commit d87faca "bad message": Use <type>(<scope>): <description>\n'
            '- Commit 5584f46: change "feat: add login page" to '
            '"feat: Add login page"\n'
            "```\n"
            "\n"
            "**Commit messages.** Reword them, then force-push:\n"
            "\n"
            "```bash\n"
            'git rebase -i d87faca~1   # change "pick" to "reword" on the '
            "commits above\n"
            "git push --force-with-lease\n"
            "```\n"
            "\n"
            f"{HOOK_HELP}\n"
            "\n"
            "</details>\n"
            "\n"
            "<details>\n"
            "<summary>All 5 checks</summary>\n"
            "\n"
            "```text\n"
            "Pull request\n"
            "  ✔ title        feat: Add login page\n"
            "Commits\n"
            "  ✖ d87faca      bad message\n"
            "      CC001 message\n"
            "        value: bad message\n"
            "        The commit message should follow Conventional Commits.\n"
            "        Suggest: Use <type>(<scope>): <description>\n"
            "  ✖ 5584f46      feat: add login page\n"
            "      CC002 subject-capitalized\n"
            "        value: feat: add login page\n"
            "        Subject must start with a capital letter\n"
            "        Fix: feat: Add login page\n"
            "  ✔ 37d6def      fix: Handle timeout\n"
            "Branch\n"
            "  ✔ branch       feature/add-login\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    def test_commit_link_honours_the_server_url(self):
        """GitHub Enterprise Server is not github.com."""
        env = {
            "GITHUB_REPOSITORY": "acme/widgets",
            "GITHUB_SERVER_URL": "https://ghe.example.com/",
        }
        with patch.dict(os.environ, env):
            body = main.render_report([fix_scope("Commit 1/1", sha=SHA_B)])
        self.assertIn(
            f"<sub>Commits: [`5584f46`](https://ghe.example.com/acme/widgets/commit/{SHA_B}) ",
            body,
        )

    def test_no_commit_link_without_a_repository_to_link_into(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}):
            body = main.render_report([fix_scope("Commit 1/1", sha=SHA_B)])
        self.assertIn("@@ 5584f46 @@\n- feat: add login page\n", body)
        self.assertIn("\n<sub>Rules: [CC002](", body)
        self.assertNotIn("/commit/", body)

    def test_scopes_without_a_commit_are_never_linked(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/widgets"}):
            body = main.render_report([fail_scope("Branch")])
        self.assertIn("@@ Branch @@\n- bad message\n", body)
        self.assertNotIn("/commit/", body)

    def test_an_author_scope_is_named_by_its_label_not_its_commit(self):
        """An author check runs on a commit, but the finding is the author."""
        scope = fail_scope("Author email", sha=SHA_A)
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/widgets"}):
            body = main.render_report([scope])
        self.assertIn("@@ Author email @@\n", body)
        self.assertIn("  ✖ email        bad message\n", body)
        self.assertNotIn("/commit/", body)

    def test_skipped_scope_carries_the_sha_too(self):
        scope = skip_scope("Commit 1/1")
        scope.sha = SHA_A
        self.assertIn("  ⊘ d87faca      skipped\n", main.render_report([scope]))

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
        body = main.render_report(
            [pass_scope("Branch"), two_failures, pass_scope("Commit 2/2")]
        )
        self.assertIn("> Commits: 1 of 2 failed", body)
        self.assertIn("<summary>All 3 checks</summary>", body)
        # Both failing rules are still named, under the diff and in the listing.
        self.assertIn("<sub>Rules: CC001 · CC005</sub>", body)
        self.assertIn("CC001 message", body)
        self.assertIn("CC005 subject-min-length", body)

    def test_total_does_not_grow_with_the_number_of_rules(self):
        """Adding rules to one scope must not change the totals."""
        one_rule = [pass_scope("Branch")]
        many_rules = [
            main.ScopeResult(
                label="Branch",
                checks=[
                    make_check(f"rule_{i}", rule_id=f"CC{i:03d}") for i in range(9)
                ],
            )
        ]
        for results in (one_rule, many_rules):
            body = main.render_report(results)
            self.assertIn("> Branch: passed\n", body)
            self.assertIn("<summary>All 1 check</summary>", body)

    def test_body_opens_with_hidden_marker(self):
        body = main.render_job_summary([pass_scope("Branch")])
        self.assertTrue(body.startswith(main.COMMENT_MARKER))

    def test_the_heading_is_the_verdict(self):
        """The mark carries the status, so no tick ever sits next to a failure."""
        cases = [
            ([pass_scope("Branch")], heading("✔", "passed", "passed")),
            ([fail_scope("Branch")], heading("✖", "failed", "failed")),
            ([warn_scope()], heading("⚠", "warned", "passed with warnings")),
            ([skip_scope("Branch")], heading("⊘", "skipped", "skipped")),
        ]
        for results, expected in cases:
            with self.subTest(expected=expected):
                lines = main.render_report(results).splitlines()
                self.assertEqual(lines[1], expected)

    def test_all_pass(self):
        body = main.render_job_summary([pass_scope("Branch", value="main")])
        self.assertIn("Commit Check passed\n", body)
        self.assertIn("<details>", body)
        self.assertIn("<summary>All 1 check</summary>", body)
        self.assertIn("```text", body)
        self.assertIn("\nBranch\n  ✔ branch       main\n", body)
        self.assertNotIn("```diff", body)
        self.assertNotIn("How to fix", body)

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
        listing = body.split("```text\n")[1]
        # Group headers in the listing mirror the step log ordering.
        self.assertLess(listing.index("Commits\n"), listing.index("Branch\n"))
        self.assertLess(listing.index("Branch\n"), listing.index("Author\n"))
        self.assertIn("  ✔ title        feat: add login page\n", listing)
        self.assertIn("  ✔ branch       feature/pr-12\n", listing)
        self.assertIn("  ✔ email        jane@example.com\n", listing)
        self.assertIn("> Author: 2 passed", body)

    def test_all_pass_truncates_long_values(self):
        long_value = "x" * 200
        body = main.render_job_summary([pass_scope("Commit 1/1", value=long_value)])
        self.assertIn(f"  ✔ 1/1          {'x' * 57}...\n", body)

    def test_all_pass_without_value_shows_plain_label(self):
        body = main.render_job_summary([pass_scope("Branch")])
        self.assertIn("  ✔ branch\n", body)

    def test_failure_shows_the_diff_and_the_rule_links(self):
        body = main.render_job_summary([fail_scope("Commit 1/1")])
        self.assertIn("Commit Check failed\n", body)
        self.assertIn("> Commits: failed\n", body)
        self.assertIn(
            "```diff\n"
            "@@ Commit 1/1 @@\n"
            "- bad message\n"
            "# Use <type>(<scope>): <description>\n"
            "```\n",
            body,
        )
        self.assertIn(
            "<sub>Rules: [CC001](https://commit-check.com/rules/#cc001)", body
        )
        self.assertIn("<summary>All 1 check</summary>", body)
        self.assertIn("  ✖ 1/1          bad message\n", body)
        self.assertIn("      CC001 message", body)
        self.assertIn("        value: bad message", body)
        self.assertIn("        Suggest: Use <type>(<scope>): <description>", body)
        self.assertIn("[Rules](https://commit-check.com/rules/)", body)

    def test_a_failing_value_is_printed_in_full(self):
        """It is the one value the reader has to act on; the cap is for the
        rows that passed."""
        subject = "feat: " + "x" * 100
        scope = main.ScopeResult(
            label="Commit 1/1",
            checks=[
                make_check(
                    "subject_max_length", status="fail", rule_id="CC004", value=subject
                )
            ],
        )
        body = main.render_report([scope])
        self.assertIn(f"- {subject}\n", body)
        self.assertIn(f"  ✖ 1/1          {subject}\n", body)

    def test_failure_details_show_all_scopes_and_values(self):
        results = [
            fail_scope("Commit 1/2"),
            pass_scope("Commit 2/2", value="fix: resolve timeout"),
        ]
        body = main.render_job_summary(results)
        self.assertIn("  ✔ 2/2          fix: resolve timeout\n", body)
        self.assertIn("  ✖ 1/2          bad message\n", body)

    def test_passing_scopes_stay_out_of_the_diff(self):
        body = main.render_job_summary([pass_scope("Branch"), fail_scope("Commit 1/1")])
        diff = body.split("```diff\n")[1].split("```")[0]
        self.assertEqual(diff.count("@@ "), 1)
        self.assertIn("@@ Commit 1/1 @@", diff)
        self.assertIn("  ✔ branch\n", body)


def ai_scope(label: str = "Commit 1/1", sha: str = SHA_A) -> main.ScopeResult:
    """A commit that credits an AI co-author, under ``ai_attribution = "forbid"``:
    the rule's value is the tool, and its fix the message without the trailer."""
    return main.ScopeResult(
        label=label,
        sha=sha,
        text=(
            "feat: add OAuth providers\n\n"
            "Co-authored-by: Claude <noreply@anthropic.com>\n"
        ),
        checks=[
            make_check(
                "ai_attribution",
                status="fail",
                rule_id="CC013",
                value="Claude Code",
                error="AI attribution is forbidden in this project",
                suggest="Remove the AI trailer lines and re-commit.",
                fix="feat: add OAuth providers",
                docs_url="https://commit-check.com/rules/#cc013",
            )
        ],
    )


class TestLineEdits(unittest.TestCase):
    """What a rule's fix removes and adds, which the diff shows red and green."""

    def test_a_subject_rule_is_compared_with_the_subject_alone(self):
        """Its fix is the subject, so the body is not something it removed."""
        scope = fix_scope()
        scope.text = "feat: add login page\n\nThe body stays.\n"
        self.assertEqual(
            main._line_edits(scope, scope.checks[0]),
            (["feat: add login page"], ["feat: Add login page"]),
        )

    def test_a_whole_message_fix_is_compared_with_the_whole_message(self):
        message = "feat: add login page\n\nThe body stays."
        scope = main.ScopeResult(
            label="Commit 1/1",
            text=message + "\n",
            checks=[
                make_check(
                    "require_signed_off_by",
                    status="fail",
                    rule_id="CC012",
                    value=message,
                    fix=f"{message}\n\nSigned-off-by: Jane Doe <jane@example.com>",
                )
            ],
        )
        self.assertEqual(
            main._line_edits(scope, scope.checks[0]),
            ([], ["Signed-off-by: Jane Doe <jane@example.com>"]),
        )

    def test_an_ai_rule_is_compared_with_the_message_not_with_its_value(self):
        """The value is the tool's name; the fix drops the trailer."""
        scope = ai_scope()
        self.assertEqual(
            main._line_edits(scope, scope.checks[0]),
            (["Co-authored-by: Claude <noreply@anthropic.com>"], []),
        )

    def test_an_ai_rule_without_the_message_has_no_line_to_name(self):
        scope = ai_scope()
        scope.text = ""
        self.assertEqual(main._line_edits(scope, scope.checks[0]), ([], []))
        self.assertEqual(
            main._instruction(scope, scope.checks[0]),
            "Remove the AI trailer lines and re-commit.",
        )

    def test_the_message_rule_supplies_the_text_outside_a_pull_request(self):
        """Nothing is piped in for the HEAD commit, but the message rule's
        value is the whole message."""
        scope = ai_scope()
        scope.checks.insert(0, make_check("message", value=scope.text.strip()))
        scope.text = ""
        self.assertEqual(
            main._line_edits(scope, scope.checks[1]),
            (["Co-authored-by: Claude <noreply@anthropic.com>"], []),
        )
        self.assertEqual(main._first_value(scope), "feat: add OAuth providers")

    def test_a_rule_without_a_fix_has_no_edits(self):
        scope = fail_scope()
        self.assertEqual(main._line_edits(scope, scope.checks[0]), ([], []))

    def test_a_fix_with_nothing_to_compare_it_with_has_no_edits(self):
        scope = main.ScopeResult(
            label="Branch", checks=[make_check("branch", status="fail", fix="x")]
        )
        self.assertEqual(main._line_edits(scope, scope.checks[0]), ([], []))

    def test_recorded_edits_win_over_the_fix(self):
        """A description is too long to carry as a fix, so its line changes
        are recorded when it is checked."""
        scope = fail_scope("PR description")
        scope.edits["CC001"] = (["Generated with Claude Code"], [])
        self.assertEqual(
            main._line_edits(scope, scope.checks[0]),
            (["Generated with Claude Code"], []),
        )

    def test_instruction_names_each_kind_of_change(self):
        scope = fail_scope()
        check = scope.checks[0]
        cases = [
            ((["a"], ["b"]), 'change "a" to "b"'),
            ((["a", "b"], []), 'remove "a"; "b"'),
            (([], ["c"]), 'add "c"'),
            ((["a", "b"], ["c"]), 'remove "a"; "b", and add "c"'),
        ]
        for edits, expected in cases:
            with self.subTest(expected=expected):
                scope.edits["CC001"] = edits
                self.assertEqual(main._instruction(scope, check), expected)

    def test_advice_drops_what_only_the_rule_page_needs(self):
        cases = {
            "Use <type>(<scope>): <description>, where <type> is one of: feat, fix": (
                "Use <type>(<scope>): <description>"
            ),
            "Use <type>/<description> with an allowed type, or add the branch "
            "to allow_branch_names in config": (
                "Use <type>/<description> with an allowed type"
            ),
            'In the description, remove "x"': 'remove "x"',
            "Add a body\nexplaining the change": "Add a body",
        }
        for suggest, expected in cases.items():
            with self.subTest(suggest=suggest):
                check = make_check("message", status="fail", suggest=suggest)
                self.assertEqual(main._advice(check), expected)

    def test_advice_falls_back_to_the_error_without_its_link(self):
        check = make_check(
            "message",
            status="fail",
            error="Follow Conventional Commits. See https://www.conventionalcommits.org",
        )
        self.assertEqual(main._advice(check), "Follow Conventional Commits.")


class TestDiffBlock(unittest.TestCase):
    def _hunks(self, results, status="fail"):
        lines = main._diff_block(results, status)
        return lines[1 : lines.index("```")]

    def test_a_fix_on_another_line_leaves_the_subject_as_context(self):
        self.assertEqual(
            self._hunks([ai_scope()]),
            [
                "@@ d87faca @@",
                "  feat: add OAuth providers",
                "- Co-authored-by: Claude <noreply@anthropic.com>",
            ],
        )

    def test_advice_is_about_the_value_so_the_value_is_red(self):
        scope = fail_scope("Commit 1/1", sha=SHA_A)
        scope.checks.append(
            make_check(
                "subject_min_length",
                status="fail",
                rule_id="CC005",
                value="bad message",
                suggest="Write a subject of at least 20 characters",
            )
        )
        self.assertEqual(
            self._hunks([scope]),
            [
                "@@ d87faca @@",
                "- bad message",
                "# Use <type>(<scope>): <description>",
                "# Write a subject of at least 20 characters",
            ],
        )

    def test_two_rules_that_ask_for_the_same_line_show_it_once(self):
        scope = ai_scope()
        scope.checks.append({**scope.checks[0], "rule_id": "CC015"})
        self.assertEqual(len(self._hunks([scope])), 3)

    def test_a_description_is_named_by_its_label_and_shows_only_the_lines(self):
        scope = fail_scope("PR description")
        scope.text = "Adds login.\n\nGenerated with Claude Code\n"
        scope.edits["CC001"] = (["Generated with Claude Code"], [])
        self.assertEqual(
            self._hunks([scope]),
            ["@@ PR description @@", "- Generated with Claude Code"],
        )

    def test_a_long_list_is_cut_and_the_rest_folded(self):
        results = [fail_scope(f"Commit {n}/8", sha=f"{n}" * 40) for n in range(1, 9)]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/widgets"}):
            lines = main._diff_block(results)
        body = "\n".join(lines)
        shown, folded = body.split("<details>")
        self.assertEqual(shown.count("@@ "), main.MAX_HUNKS)
        self.assertIn("<summary>and 3 more</summary>\n\n```diff\n@@ 6666666 @@", folded)
        self.assertEqual(folded.count("@@ "), 3)
        # Only the commits in view are linked; the rule is named once.
        links = lines[-1]
        self.assertEqual(links.count("/commit/"), main.MAX_HUNKS)
        self.assertNotIn("6666666", links)
        self.assertEqual(links.count("[CC001]"), 1)

    def test_one_or_two_leftovers_are_not_worth_a_fold(self):
        results = [fail_scope(f"Commit {n}/7", sha=f"{n}" * 40) for n in range(1, 8)]
        body = "\n".join(main._diff_block(results))
        self.assertEqual(body.count("@@ "), 7)
        self.assertNotIn("<details>", body)

    def test_warnings_get_the_same_block(self):
        self.assertEqual(
            self._hunks([warn_scope(), fail_scope()], "warn"),
            ["@@ Branch @@", "- jsmith/fix-x", "# Use <type>/<description>"],
        )


class TestHowToFix(unittest.TestCase):
    def _body(self, results):
        return "\n".join(main._how_to_fix(results))

    def test_nothing_failed_nothing_to_fix(self):
        self.assertEqual(main._how_to_fix([pass_scope(), warn_scope()]), [])

    def test_the_prompt_lists_every_failing_scope_with_its_instruction(self):
        results = [
            fail_scope("PR title"),
            ai_scope("Commit 1/2"),
            fix_scope("Commit 2/2"),
            pass_scope("Branch"),
        ]
        self.assertIn(
            "```text\n"
            f"{AGENT_PROMPT}\n"
            '- PR title "bad message": Use <type>(<scope>): <description>\n'
            '- Commit d87faca "feat: add OAuth providers": remove '
            '"Co-authored-by: Claude <noreply@anthropic.com>"\n'
            '- Commit 5584f46: change "feat: add login page" to '
            '"feat: Add login page"\n'
            "```\n",
            self._body(results),
        )

    def test_only_the_last_commit_failing_is_an_amend(self):
        for scope in (
            fail_scope("Commit 3/3", sha=SHA_A),
            fail_scope("Commit message"),
        ):
            with self.subTest(label=scope.label):
                body = self._body([scope])
                self.assertIn("Reword it, then force-push:", body)
                self.assertIn("```bash\ngit commit --amend\ngit push --force", body)
                self.assertNotIn("rebase", body)

    def test_an_earlier_commit_needs_a_rebase_from_its_parent(self):
        body = self._body(
            [fail_scope("Commit 2/3", sha=SHA_B), pass_scope("Commit 3/3")]
        )
        self.assertIn("```bash\ngit rebase -i 5584f46~1   # change", body)

    def test_a_commit_without_a_hash_gets_a_placeholder(self):
        body = self._body([fail_scope("Commit 1/3"), fail_scope("Commit 2/3")])
        self.assertIn("Reword them, then force-push:", body)
        self.assertIn("git rebase -i <first failing commit>~1   # change", body)

    def test_each_kind_of_failure_gets_its_own_paragraph(self):
        body = self._body(
            [
                fail_scope("PR title"),
                fail_scope("PR description"),
                fail_scope("Author email", sha=SHA_A),
                fail_scope("Branch"),
            ]
        )
        self.assertNotIn("**Commit messages.**", body)
        self.assertIn("**Author.** Set your git identity", body)
        self.assertIn(
            "**Branch name.** Rename the branch and push it. A pull request cannot "
            "change its branch, so open a new one from the renamed branch:\n\n"
            "```bash\ngit branch -m <type>/<description>\ngit push -u origin HEAD\n```",
            body,
        )
        self.assertIn(
            "**Pull request title and description.** Edit them on the pull "
            "request page.",
            body,
        )
        self.assertTrue(body.endswith(f"{HOOK_HELP}\n\n</details>"))

    def test_the_branch_command_uses_the_corrected_name_when_there_is_one(self):
        scope = fail_scope("Branch")
        scope.checks[0].update(value="Feature/Add-Login", fix="feature/Add-Login")
        self.assertIn(
            "```bash\ngit branch -m feature/Add-Login\ngit push -u origin HEAD\n```",
            self._body([scope]),
        )

    def test_one_pull_request_field_is_it_not_them(self):
        body = self._body([fail_scope("PR title")])
        self.assertIn("**Pull request title.** Edit it on the pull request page.", body)
        self.assertNotIn("**Branch name.**", body)
        self.assertNotIn("**Author.**", body)


class TestRenderPrComment(unittest.TestCase):
    def test_all_pass_matches_job_summary(self):
        comment = main.render_pr_comment([pass_scope("Branch")])
        summary = main.render_job_summary([pass_scope("Branch")])
        self.assertEqual(comment, summary)
        self.assertTrue(comment.startswith(main.COMMENT_MARKER))
        self.assertIn("Commit Check passed\n", comment)

    def test_failure_matches_job_summary(self):
        comment = main.render_pr_comment([fail_scope("Commit 1/1")])
        summary = main.render_job_summary([fail_scope("Commit 1/1")])
        self.assertEqual(comment, summary)
        self.assertTrue(comment.startswith(main.COMMENT_MARKER))
        self.assertIn("Commit Check failed\n", comment)
        self.assertIn("```diff\n@@ Commit 1/1 @@\n", comment)


class TestPrCommentSizeLimit(unittest.TestCase):
    """A report too long for a comment gives up its longest parts first."""

    RUN_URL = "https://github.com/o/r/actions/runs/42"

    def setUp(self):
        self.RUN = {
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_ID": "42",
            "GITHUB_SERVER_URL": "https://github.com",
        }

    def _failing_commits(self, count: int) -> list[main.ScopeResult]:
        body = "\n".join(
            f"Line {i} of a body that explains the change." for i in range(8)
        )
        return [
            main.ScopeResult(
                label=f"Commit {i}/{count}",
                sha=f"{i:040x}",
                checks=[
                    make_check(
                        "require_signed_off_by",
                        "fail",
                        "CC012",
                        value=f"Update module {i}\n\n{body}",
                        error="Signed-off-by trailer not found in the commit message",
                        fix=f"Update module {i}\n\n{body}\n\nSigned-off-by: A <a@b.c>",
                    )
                ],
            )
            for i in range(1, count + 1)
        ]

    def test_a_report_that_fits_is_the_job_summary(self):
        results = self._failing_commits(3)
        self.assertEqual(
            main.render_pr_comment(results), main.render_job_summary(results)
        )

    def test_dozens_of_failing_commits_still_fit(self):
        results = self._failing_commits(60)
        self.assertGreater(len(main.render_job_summary(results)), main.COMMENT_LIMIT)
        with (
            patch.dict(os.environ, self.RUN),
            patch("main.JOB_SUMMARY_ENABLED", True),
            patch("main.GITHUB_STEP_SUMMARY", "/tmp/summary.md"),
        ):
            comment = main.render_pr_comment(results)
        self.assertLessEqual(main._comment_length(comment), main.COMMENT_LIMIT)
        self.assertTrue(comment.startswith(main.COMMENT_MARKER))
        self.assertIn("Commit Check failed", comment)
        self.assertNotIn("<summary>All 60 checks</summary>", comment)
        self.assertIn(
            f"All 60 checks are in the [job summary]({self.RUN_URL}) of this run.",
            comment,
        )

    def test_the_diff_goes_too_when_the_listing_was_not_enough(self):
        results = self._failing_commits(2)
        with (
            patch.dict(os.environ, self.RUN),
            patch("main.JOB_SUMMARY_ENABLED", False),
        ):
            short = main.render_report(results, "short")
            with patch("main.COMMENT_LIMIT", len(short) - 1):
                comment = main.render_pr_comment(results)
        self.assertNotIn("```diff", comment)
        self.assertNotIn("How to fix</b>", comment)
        self.assertIn(
            "The findings and how to fix them are in the "
            f"[step log]({self.RUN_URL}) of this run.",
            comment,
        )
        self.assertIn("Commits: 2 of 2 failed", comment)

    def test_a_passing_headline_names_no_findings(self):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_RUN_ID"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("main.JOB_SUMMARY_ENABLED", False),
        ):
            body = main.render_report([pass_scope("Branch")], "headline")
        self.assertIn("All checks are in the step log of this run.", body)

    def test_length_is_counted_as_github_counts_it(self):
        self.assertEqual(main._comment_length("a🤖"), 3)


class TestUnparsableScopeInTheReport(unittest.TestCase):
    """A CLI response that is not JSON is a failure with no rule to link."""

    def test_the_diff_points_at_the_step_log(self):
        scope = main.ScopeResult(label="Branch", raw_text="Error: cchk.toml: oops")
        body = main.render_report([scope])
        self.assertIn("Commit Check failed\n", body)
        self.assertIn(
            "```diff\n"
            "@@ Branch @@\n"
            "# the output could not be parsed, see the step log\n"
            "```\n",
            body,
        )
        # No rule spoke, so there is none to link and nothing to instruct.
        self.assertNotIn("<sub>Rules", body)
        self.assertNotIn("How to fix", body)
        # The listing is where the raw output went.
        self.assertIn("  ✖ branch\n      Error: cchk.toml: oops\n", body)

    def test_it_never_reaches_the_warnings(self):
        scope = main.ScopeResult(label="Branch", raw_text="garbage")
        self.assertNotIn("**Warnings**", main.render_report([scope]))
        self.assertEqual(main._diff_block([scope], "warn")[:2], ["```diff", "```"])


class TestReportFooter(unittest.TestCase):
    def test_names_the_installed_commit_check(self):
        with patch("importlib.metadata.version", return_value="9.8.7") as version:
            self.assertEqual(main._commit_check_version(), "9.8.7")
            self.assertEqual(
                main._report_footer(),
                "<sub>Checked by [Commit Check](https://commit-check.com) 9.8.7 · "
                "[Rules](https://commit-check.com/rules/) · "
                f"[Check before you push]({HOOK_GUIDE})</sub>",
            )
        version.assert_called_with("commit-check")

    def test_without_a_version_it_still_links_the_rules(self):
        """The footer is decoration: an unknown version drops out of it."""
        missing = importlib.metadata.PackageNotFoundError("commit-check")
        with patch("importlib.metadata.version", side_effect=missing):
            self.assertEqual(main._commit_check_version(), "")
            self.assertEqual(
                main._report_footer(),
                "<sub>Checked by [Commit Check](https://commit-check.com) · "
                "[Rules](https://commit-check.com/rules/) · "
                f"[Check before you push]({HOOK_GUIDE})</sub>",
            )


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
        self.assertIn("Commit Check passed\n", content)

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
        self.assertIn("Commit Check failed\n", content)
        self.assertIn("```diff\n@@ Commit 1/1 @@\n- bad message\n", content)


def read_github_output(path: str) -> dict[str, str]:
    """Parse a ``GITHUB_OUTPUT`` file the way the runner does.

    A port of the loop in actions/runner's ``FileCommandManager``: a line is
    either ``name=value`` or ``name<<delimiter``, and a heredoc value runs up
    to the first line equal to the delimiter (CRLF counts as a newline).
    Anything else raises, as it fails the step on a real runner, so a test
    using this parser cannot pass on a file the runner would reject.
    """
    with open(path, encoding="utf-8", newline="") as file_obj:
        lines = file_obj.read().splitlines()
    outputs: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line:
            continue
        equals, heredoc = line.find("="), line.find("<<")
        if equals >= 0 and (heredoc < 0 or equals < heredoc):
            name, value = line.split("=", 1)
            outputs[name] = value
            continue
        if heredoc < 0:
            raise ValueError(f"Invalid format '{line}'")
        name, delimiter = line.split("<<", 1)
        if not delimiter:
            raise ValueError("Invalid format: empty delimiter")
        body: list[str] = []
        while True:
            if index >= len(lines):
                raise ValueError(f"Matching delimiter not found '{delimiter}'")
            current = lines[index]
            index += 1
            if current == delimiter:
                break
            body.append(current)
        outputs[name] = "\n".join(body)
    return outputs


class TestSetResultOutput(unittest.TestCase):
    def test_writes_heredoc_json(self):
        output_path = os.path.join(tempfile.mkdtemp(), "output.txt")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
            main.set_result_output([fail_scope("Commit 1/1"), pass_scope("Branch")])
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
            main.ScopeResult(
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
            main.set_result_output(results)
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
            main.set_result_output([fix_scope("Commit 2/3", sha=SHA_B), pass_scope()])
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

    def test_fork_pr_warning_names_only_the_surfaces_that_exist(self):
        """With job-summary: false the warning must not send the reader to a
        summary this run never wrote; the annotations are still named."""
        summary_path = os.path.join(tempfile.mkdtemp(), "summary.txt")
        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.is_fork_pr", return_value=True),
            patch("main.JOB_SUMMARY_ENABLED", False),
            patch("main.GITHUB_STEP_SUMMARY", summary_path),
            patch("builtins.print") as mock_print,
        ):
            rc = main.add_pr_comments([pass_scope()])
        self.assertEqual(rc, 0)
        printed = mock_print.call_args[0][0]
        self.assertIn("annotations on the run page", printed)
        self.assertNotIn("job's summary", printed)
        self.assertFalse(os.path.exists(summary_path))

        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("main.is_fork_pr", return_value=True),
            patch("main.JOB_SUMMARY_ENABLED", True),
            patch("main.GITHUB_STEP_SUMMARY", summary_path),
            patch("builtins.print") as mock_print,
        ):
            main.add_pr_comments([pass_scope()])
        printed = mock_print.call_args[0][0]
        self.assertIn("in this job's summary and in the annotations", printed)

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
        self.assertIn("```diff\n@@ Commit 1/1 @@\n- bad message\n", body)

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
        existing.user.type = "Bot"
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

    def test_the_newest_report_is_updated_and_older_ones_deleted(self):
        """Duplicates (two runs racing to create the first comment, say)
        collapse into the newest one."""
        older = MagicMock(body=f"{main.COMMENT_MARKER}\nolder report")
        older.user.type = "Bot"
        newer = MagicMock(body=f"{main.COMMENT_MARKER}\nnewer report")
        newer.user.type = "Bot"
        mock_pull_request = MagicMock()
        mock_pull_request.get_comments.return_value = [older, newer]
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
        github_module.Github.return_value.get_repo.assert_called_once_with("owner/repo")
        mock_repo.get_issue.assert_called_once_with(12)
        newer.edit.assert_called_once_with(main.render_pr_comment([fail_scope()]))
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

    def test_a_missing_pygithub_is_a_warning_not_a_crash(self):
        """``None`` in sys.modules makes the import raise ImportError."""
        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch("main.is_fork_pr_with_readonly_token", return_value=False),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch.dict(sys.modules, {"github": None}),
            patch("builtins.print") as mock_print,
        ):
            rc = main.add_pr_comments([fail_scope()])
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
                    patch("main.PR_COMMENTS_ENABLED", True),
                    patch("main.is_fork_pr_with_readonly_token", return_value=False),
                    patch.dict(os.environ, env),
                    patch.dict(sys.modules, {"github": github_module}),
                    patch("builtins.print") as mock_print,
                ):
                    rc = main.add_pr_comments([fail_scope()])
                self.assertEqual(rc, 0)
                self.assertEqual(
                    mock_print.call_args[0][0],
                    f"::warning::Unable to post PR comment: {missing} is not set",
                )
                github_module.Github.assert_not_called()


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

    def test_unreadable_event_is_not_a_fork(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        event_path = os.path.join(tmp.name, "event.json")
        with open(event_path, "w", encoding="utf-8") as f:
            f.write("not json")
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": event_path}):
            self.assertFalse(main.is_fork_pr())


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
            self.assertEqual(main.get_pr_number(), 42)

    def test_pull_request_target_reads_the_event_instead(self):
        """There GITHUB_REF is the base branch, which names no pull request."""
        for payload in ({"number": 7}, {"pull_request": {"number": 7}}):
            with self.subTest(payload=payload):
                env = {
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_EVENT_PATH": self._event(payload),
                }
                with patch.dict(os.environ, env):
                    self.assertEqual(main.get_pr_number(), 7)

    def test_no_number_anywhere_raises(self):
        for payload in ({"pull_request": None}, {}, "not json"):
            with self.subTest(payload=payload):
                env = {
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_EVENT_PATH": self._event(payload),
                }
                with patch.dict(os.environ, env), self.assertRaises(ValueError):
                    main.get_pr_number()

    def test_no_event_payload_raises(self):
        with (
            patch.dict(os.environ, {"GITHUB_REF": "refs/heads/main"}),
            self.assertRaises(ValueError),
        ):
            os.environ.pop("GITHUB_EVENT_PATH", None)
            main.get_pr_number()


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

    def test_human_comment_with_the_marker_is_never_touched(self):
        """A person can paste the marker too, e.g. quoting a report."""
        quoted = self._comment(f"{main.COMMENT_MARKER}\nquoted", user_type="User")
        mine = self._comment(f"{main.COMMENT_MARKER}\nreport")
        self.assertEqual(main._find_own_comments([quoted, mine]), (mine, []))
        self.assertEqual(main._find_own_comments([mine, quoted]), (mine, []))
        self.assertEqual(main._find_own_comments([quoted]), (None, []))

    def test_no_comments_yields_nothing_to_update(self):
        target, stale = main._find_own_comments([])
        self.assertIsNone(target)
        self.assertEqual(stale, [])

    def test_a_comment_without_an_author_is_not_a_bot(self):
        """A deleted account can leave ``user`` empty; that is not a crash,
        and not a licence to adopt the comment either."""
        orphan = self._comment("# Commit Check\nold report")
        orphan.user = None
        self.assertFalse(main._is_bot(orphan))
        self.assertEqual(main._find_own_comments([orphan]), (None, []))


def skip_scope(label: str = "PR title", reason: str = "") -> main.ScopeResult:
    """A scope whose every rule declined to run (e.g. author in ignore_authors)."""
    check = make_check("message", status="skip", rule_id="CC001", value="")
    if reason:
        check["reason"] = reason
    return main.ScopeResult(label=label, checks=[check])


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
            f"{heading('⊘', 'skipped', 'skipped')}\n"
            "\n"
            "Nothing was validated.\n"
            "\n"
            f"{group_line('⊘', 'skipped', 'Pull request: skipped')}<br>\n"
            f"{group_line('⊘', 'skipped', 'Commits: skipped')}\n"
            "\n"
            "<details>\n"
            "<summary>All 2 checks</summary>\n"
            "\n"
            "```text\n"
            "Pull request\n"
            "  ⊘ title        skipped\n"
            "Commits\n"
            "  ⊘ 1/1          skipped\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    def test_partial_skip_does_not_claim_all_passed(self):
        results = [pass_scope("Branch", value="main"), skip_scope("PR title")]
        results.append(pass_scope("PR description"))
        body = main.render_report(results)
        self.assertIn("Commit Check passed\n", body)
        self.assertIn("> Branch: passed<br>\n", body)
        # Two scopes in the group, one of which ran: never "2 passed".
        self.assertIn("> Pull request: 1 passed, 1 skipped\n", body)

    def test_failure_still_wins_over_skips(self):
        results = [fail_scope("Commit 1/1"), skip_scope("PR title")]
        body = main.render_report(results)
        self.assertIn("Commit Check failed\n", body)
        self.assertIn("> Commits: failed<br>\n", body)
        self.assertIn("> Pull request: skipped\n", body)

    def test_skipped_scope_reports_no_checked_value(self):
        """The ⊘ line carries no value, because nothing was examined."""
        body = main.render_report([skip_scope("PR title")])
        self.assertIn("  ⊘ title        skipped\n", body)

    def test_skip_reason_is_shown_on_the_scope_and_under_the_verdict(self):
        """A skip says why, so a deliberate bypass reads as one."""
        commit = "author dependabot[bot] is in [commit].ignore_authors"
        branch = "author dependabot[bot] is in [branch].ignore_authors"
        results = [
            skip_scope("PR title", reason=commit),
            skip_scope("Commit 1/1", reason=commit),
            skip_scope("Branch", reason=branch),
        ]
        body = main.render_report(results)
        self.assertIn("Commit Check skipped\n\nNothing was validated.\n", body)
        self.assertIn(
            "> Branch: skipped\n\n"
            "Skipped because author dependabot[bot] is in ignore_authors.\n",
            body,
        )
        self.assertIn(f"  ⊘ title        skipped: {commit}\n", body)
        self.assertIn(f"  ⊘ branch       skipped: {branch}\n", body)

    def test_skip_reason_follows_a_partial_skip_verdict(self):
        reason = "author dependabot[bot] is in [branch].ignore_authors"
        results = [pass_scope("PR title"), skip_scope("Branch", reason=reason)]
        body = main.render_report(results)
        self.assertIn(
            "> Branch: skipped\n\n"
            "Skipped because author dependabot[bot] is in ignore_authors.\n",
            body,
        )

    def test_skip_reason_reaches_the_step_log(self):
        reason = "author dependabot[bot] is in [commit].ignore_authors"
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            main.render_step_log([skip_scope("PR title", reason=reason)])
        self.assertIn("::group::⊘ Pull request: skipped\n", out.getvalue())
        self.assertIn(
            f"  ⊘ title        skipped: {reason}\n", ANSI_CODES.sub("", out.getvalue())
        )
        self.assertIn(
            "Skipped because author dependabot[bot] is in ignore_authors.",
            out.getvalue(),
        )

    def test_skip_reason_follows_a_partial_skip_in_the_step_log(self):
        reason = "author dependabot[bot] is in [branch].ignore_authors"
        results = [pass_scope("PR title"), skip_scope("Branch", reason=reason)]
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            main.render_step_log(results)
        self.assertIn(
            "Skipped because author dependabot[bot] is in ignore_authors.",
            out.getvalue(),
        )

    def test_a_passing_scope_has_no_skip_reason(self):
        self.assertEqual(pass_scope("Branch", value="main").skip_reason, "")

    def test_older_engine_without_skip_is_unaffected(self):
        """Back-compat: engines that only emit pass/fail render as before."""
        results = [pass_scope("Branch", value="main")]
        self.assertIn("Commit Check passed\n", main.render_report(results))


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
        still belong on every surface: the count, both diffs, and the
        listing. Filtering any of those on ``scope.status == "warn"`` drops
        this scope's warning the moment its sibling rule fails.
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
        self.assertIn("Commit Check failed\n", body)
        failed, warned = body.split("**Warnings**")
        self.assertIn(
            "```diff\n"
            "@@ Branch @@\n"
            "- jsmith/fix-x\n"
            "# Current branch is not rebased onto main.\n"
            "```\n"
            "\n"
            "<sub>Rules: [CC202](https://commit-check.com/rules/#cc202)</sub>\n",
            failed,
        )
        self.assertIn(
            "```diff\n"
            "@@ Branch @@\n"
            "- jsmith/fix-x\n"
            "# Use <type>/<description>\n"
            "```\n"
            "\n"
            "<sub>Rules: [CC201](https://commit-check.com/rules/#cc201)</sub>\n",
            warned,
        )
        # One row, marked with the worse of the two, and both findings under
        # it; the warning says that it is one.
        self.assertIn("  ✖ branch       jsmith/fix-x\n      CC202 merge-base\n", body)
        self.assertIn("\n      CC201 branch (warning)\n", body)
        self.assertNotIn("⚠ branch", body)

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

    def test_overall_status_is_warn_but_the_exit_code_is_still_zero(self):
        """``warn`` is for downstream steps; it must never fail the job."""
        results = [pass_scope("PR title"), warn_scope()]
        self.assertEqual(main.overall_status(results), "warn")
        self.assertEqual(main.exit_code_for(results), 0)

    def test_a_warning_outranks_a_skip_but_not_an_all_skipped_run(self):
        self.assertEqual(main.overall_status([warn_scope(), skip_scope()]), "warn")
        self.assertEqual(main.overall_status([skip_scope()]), "skip")

    def test_result_output_reports_warn(self):
        output_path = os.path.join(tempfile.mkdtemp(), "output.txt")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
            main.set_result_output([pass_scope("PR title"), warn_scope()])
        payload = json.loads(read_github_output(output_path)["result"])
        self.assertEqual(payload["status"], "warn")
        self.assertEqual([s["status"] for s in payload["scopes"]], ["pass", "warn"])

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
            f"{heading('⚠', 'warned', 'passed with warnings')}\n"
            "\n"
            f"{group_line('✔', 'passed', 'Pull request: passed')}<br>\n"
            f"{group_line('⚠', 'warned', 'Branch: 1 warning')}\n"
            "\n"
            "**Warnings**\n"
            "\n"
            "```diff\n"
            "@@ Branch @@\n"
            "- jsmith/fix-x\n"
            "# Use <type>/<description>\n"
            "```\n"
            "\n"
            "<sub>Rules: [CC201](https://commit-check.com/rules/#cc201)</sub>\n"
            "\n"
            "<details>\n"
            "<summary>All 2 checks</summary>\n"
            "\n"
            "```text\n"
            "Pull request\n"
            "  ✔ title        feat: add login page\n"
            "Branch\n"
            "  ⚠ branch       jsmith/fix-x\n"
            "      CC201 branch (warning)\n"
            "        value: jsmith/fix-x\n"
            "        The branch should follow Conventional Branch.\n"
            "        Suggest: Use <type>/<description>\n"
            "```\n"
            "\n"
            "</details>\n"
            "\n"
            f"{FOOTER}",
        )

    def test_warning_and_skip_each_get_their_own_group_line(self):
        results = [pass_scope("PR title"), warn_scope(), skip_scope("Author name")]
        body = main.render_report(results)
        self.assertIn("Commit Check passed with warnings\n", body)
        self.assertIn(
            f"{group_line('✔', 'passed', 'Pull request: passed')}<br>\n"
            f"{group_line('⚠', 'warned', 'Branch: 1 warning')}<br>\n"
            f"{group_line('⊘', 'skipped', 'Author: skipped')}\n",
            body,
        )

    def test_a_failure_still_fails_and_names_the_warning_too(self):
        results = [fail_scope("Commit 1/1"), warn_scope()]
        body = main.render_report(results)
        self.assertIn("Commit Check failed\n", body)
        self.assertEqual(body.count("```diff\n"), 2)
        # The failures come first, the warnings under their own heading, and
        # only the failures get instructions.
        self.assertLess(body.index("@@ Commit 1/1 @@"), body.index("**Warnings**"))
        self.assertLess(body.index("**Warnings**"), body.index("@@ Branch @@"))
        self.assertLess(body.index("@@ Branch @@"), body.index("How to fix"))
        self.assertNotIn("Branch name.", body)

    def test_step_log_prints_a_warning_annotation_not_an_error(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.render_step_log([warn_scope()])
        out = buf.getvalue()
        self.assertIn(
            '::warning title=CC201 branch::Branch "jsmith/fix-x": '
            "Use <type>/<description>%0AThe branch should ",
            out,
        )
        self.assertNotIn("::error", out)
        self.assertIn("::group::⚠ Branch: 1 warning\n", out)
        self.assertIn("  \x1b[33m⚠\x1b[0m branch       jsmith/fix-x\n", out)
        self.assertNotIn("How to fix", out)

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
            payload = json.loads(read_github_output(out_path)["result"])
        finally:
            os.unlink(out_path)
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


class TestMarkdownEscaping(unittest.TestCase):
    """User text in the report must not break the Markdown that carries it.

    Commit subjects quote code, branch names and author names may contain
    pipes; every value lands in a fenced block, and the fence has to survive
    both. The checks below come from a real ``commit-check --format json``
    run on the subject ``fix: handle `None` | retry`` (all rules pass on it,
    so the CC001 outcome is flipped to make it a finding).
    """

    def _scope(self, value: str) -> main.ScopeResult:
        return main.ScopeResult(
            label="Commit 1/1",
            sha=SHA_B,
            checks=[
                make_check(
                    "message",
                    status="fail",
                    rule_id="CC001",
                    value=value,
                    error="The commit message should follow Conventional Commits.",
                    docs_url="https://commit-check.com/rules/#cc001",
                ),
                make_check("subject_imperative", rule_id="CC003", value=value),
            ],
        )

    def test_diff_fence_outgrows_a_triple_backtick_in_the_subject(self):
        diff = main._diff_block([self._scope("docs: show ``` usage")])
        self.assertEqual(diff[0], "````diff")
        self.assertEqual(diff[2], "- docs: show ``` usage")
        self.assertEqual(diff[4], "````")

    def test_prompt_fence_outgrows_a_triple_backtick_in_the_subject(self):
        how = main._how_to_fix([self._scope("docs: show ``` usage")])
        self.assertIn("````text", how)
        self.assertEqual(how.count("````"), 1)
        # The commands quote nothing of the user's, so their fence stays.
        self.assertIn("```bash", how)

    def test_a_subject_with_a_pipe_or_backticks_is_shown_as_it_is(self):
        """Every place a value lands is a code block, so nothing is escaped."""
        subject = "fix: handle `None` | retry"
        body = main.render_report([self._scope(subject)])
        self.assertIn(f"- {subject}\n", body)
        self.assertIn(f"  ✖ 5584f46      {subject}\n", body)

    def test_capped_value_with_an_unbalanced_backtick_stays_in_its_block(self):
        """The 60-char cap can cut inside a backtick run."""
        value = "fix: " + "`x`, " * 20
        scope = pass_scope("Commit 1/1", value=value)
        capped = main._scope_value(scope)
        self.assertTrue(capped.endswith("`x..."), capped)  # cut mid-span
        listing = main._all_checks([scope])
        self.assertEqual(listing[3], "```text")
        self.assertEqual(listing[-3], "```")

    def test_details_fence_outgrows_a_triple_backtick_in_the_value(self):
        details = "\n".join(main._all_checks([self._scope("docs: show ``` usage")]))
        lines = details.splitlines()
        self.assertEqual(lines[3], "````text")
        self.assertEqual(lines[-3], "````")
        self.assertIn("value: docs: show ``` usage", details)

    def test_details_fence_stays_three_backticks_for_ordinary_values(self):
        lines = main._all_checks([pass_scope(value="feat: add `login`")])
        self.assertEqual(lines[3], "```text")
        self.assertEqual(lines[-3], "```")


class TestSkipRenderingEdgeCases(unittest.TestCase):
    def test_the_diff_omits_skipped_scopes(self):
        """A skipped scope has no failed checks, so it must not get a hunk:
        a blank accusation among what failed otherwise."""
        diff = main._diff_block([fail_scope("Commit 1/1"), skip_scope("PR title")])
        self.assertEqual(
            diff[:5],
            [
                "```diff",
                "@@ Commit 1/1 @@",
                "- bad message",
                "# Use <type>(<scope>): <description>",
                "```",
            ],
        )

    def test_empty_result_set_is_not_reported_as_all_skipped(self):
        """`skipped == total` is trivially true for no scopes at all."""
        body = main.render_report([])
        self.assertNotIn("skipped", body)
        self.assertIn("Commit Check passed\n\n<details>\n", body)
        self.assertIn("<summary>Details</summary>", body)

    def test_step_log_partial_skip_does_not_claim_all_passed(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.render_step_log([pass_scope(), skip_scope()])
        out = buf.getvalue()
        self.assertIn("1 of 2 checks passed, 1 skipped", out)
        self.assertNotIn("all checks passed", out)

    def test_step_log_all_skipped_claims_no_pass(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main.render_step_log([skip_scope(), skip_scope("Branch")])
        out = buf.getvalue()
        self.assertIn("  \x1b[90m⊘\x1b[0m title        skipped\n", out)
        self.assertTrue(
            out.endswith("⊘ Commit Check: all checks skipped, nothing was validated\n"),
            out,
        )
        self.assertNotIn("passed", out)
        self.assertNotIn("::error", out)


# ---------------------------------------------------------------------------
# Integration: the real commit-check binary
# ---------------------------------------------------------------------------

#: The JSON shape ``make_check()`` hard-codes and every renderer reads.
CHECK_KEYS = {
    "rule_id",
    "check",
    "status",
    "value",
    "error",
    "suggest",
    "fix",
    "docs_url",
}
#: Keys newer CLIs add on top: ``reason`` says why a check was skipped.
OPTIONAL_CHECK_KEYS = {"reason"}
STATUSES = {"pass", "fail", "warn", "skip"}


@unittest.skipUnless(
    shutil.which("commit-check"),
    "commit-check CLI not on PATH (CI installs it from requirements.txt)",
)
class TestRealCommitCheckBinary(unittest.TestCase):
    """Run the pinned commit-check once, unmocked.

    Every other test patches ``main.subprocess.run`` and feeds back the JSON
    shape ``make_check()`` hard-codes, so a renamed key or a new status in
    the CLI would leave the whole suite green while the action rendered "—"
    for every value. This is the one place that drift can fail a build. CI
    installs requirements.txt, so the binary is always present there; the
    skip only spares a contributor running the suite without it.

    The CLI runs on its built-in defaults, with nothing ambient reaching it.
    A message piped in here describes no commit, so the verdict must not
    depend on who committed last, on what the org config says this week, or
    on a contributor's ``CCHK_*`` variables -- and unpinned, it depended on
    all three. It read this repo's ``commit-check.toml``, which inherits the
    org config over the network, and on a runner with no git identity the
    CLI weighs ``ignore_authors`` against the author of ``HEAD`` instead. On
    a Dependabot PR that author is ``dependabot[bot]``, which the org config
    ignores, so every message check skipped and ``test_passing_message``
    saw ``skip`` where it asserted ``pass`` (#284). commit-check's own suite
    pins the same two lookups for the same reason (its ``pinned_author``
    fixture); this is that pin, from outside the process.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # An empty config, named explicitly: the CLI reads this file and looks
        # for no other, so every rule is at its default and ``ignore_authors``
        # names nobody.
        self.config = os.path.join(tmp.name, "cchk.toml")
        with open(self.config, "w", encoding="utf-8"):
            pass
        # A known identity at git's global and system levels, so the CLI's
        # author lookup never falls through to ``HEAD``; and no ``CCHK_*``
        # variable, since the environment overrides the file.
        gitconfig = os.path.join(tmp.name, "gitconfig")
        with open(gitconfig, "w", encoding="utf-8") as f:
            f.write("[user]\n\tname = test-author\n\temail = test@example.com\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("CCHK_")}
        env["GIT_CONFIG_GLOBAL"] = gitconfig
        env["GIT_CONFIG_SYSTEM"] = gitconfig
        env_patch = patch.dict(os.environ, env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run(self, message: str) -> tuple[int, dict]:
        rc, data, raw = main.run_check_json(
            ["--message", "--config", self.config], input_text=message
        )
        self.assertIsInstance(data, dict, f"CLI did not emit JSON:\n{raw}")
        assert data is not None  # for the type checker; asserted above
        self.assertIn("checks", data)
        self.assertTrue(data["checks"], "CLI reported no checks")
        for check in data["checks"]:
            self.assertLessEqual(CHECK_KEYS, set(check), check)
            self.assertLessEqual(set(check), CHECK_KEYS | OPTIONAL_CHECK_KEYS, check)
            self.assertIn(check["status"], STATUSES, check)
            self.assertRegex(check["rule_id"], r"^CC\d{3}$")
            self.assertTrue(
                check["docs_url"].startswith("https://commit-check.com/rules/#")
            )
        return rc, data

    def test_passing_message(self):
        rc, data = self._run("fix: handle the empty case\n")
        self.assertEqual(rc, 0)
        self.assertEqual(data["status"], "pass")
        self.assertTrue(all(c["status"] == "pass" for c in data["checks"]))
        scope = main.ScopeResult(label="Commit 1/1", checks=data["checks"])
        self.assertEqual(scope.status, "pass")
        self.assertEqual(main.overall_status([scope]), "pass")

    def test_failing_message(self):
        rc, data = self._run("Bad subject\n")
        self.assertEqual(rc, 1)
        self.assertEqual(data["status"], "fail")
        failed = [c for c in data["checks"] if c["status"] == "fail"]
        self.assertEqual([c["rule_id"] for c in failed], ["CC001"])
        self.assertTrue(failed[0]["error"])
        scope = main.ScopeResult(label="Commit 1/1", checks=data["checks"])
        self.assertEqual(scope.status, "fail")
        self.assertEqual(main.overall_status([scope]), "fail")
        # The rendered report must carry the rule link the CLI supplied.
        self.assertIn(
            "<sub>Rules: [CC001](https://commit-check.com/rules/#cc001)</sub>",
            main.render_report([scope]),
        )

    def test_a_title_meets_only_the_rules_a_title_can(self):
        with open(self.config, "w", encoding="utf-8") as f:
            f.write("[commit]\nrequire_body = true\nrequire_signed_off_by = true\n")
        rc, data = self._run("feat: add login page")
        failed = {c["rule_id"] for c in data["checks"] if c["status"] == "fail"}
        self.assertEqual(rc, 1)
        self.assertEqual(failed, {"CC011", "CC012"})
        scope = main.drop_commit_only_rules(
            main.ScopeResult(label="PR title", checks=data["checks"])
        )
        self.assertEqual(scope.status, "pass")


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


def _pr_event(pull_request: dict) -> str:
    """Write a pull_request event payload and return its path."""
    path = os.path.join(tempfile.mkdtemp(), "event.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pull_request": pull_request}, f)
    return path


class TestAiLabelName(unittest.TestCase):
    def test_values(self):
        cases = {
            "false": None,
            "FALSE": None,
            "": None,
            "  ": None,
            "true": "ai-assisted",
            "True": "ai-assisted",
            "AI generated": "AI generated",
            " needs-ai-review ": "needs-ai-review",
        }
        for value, expected in cases.items():
            with self.subTest(value=value), patch.dict(os.environ, {"AI_LABEL": value}):
                self.assertEqual(main.ai_label_name(), expected)

    def test_unset_is_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(main.ai_label_name())


class TestPrBodyAndOpener(unittest.TestCase):
    def _env(self, pull_request: dict):
        return patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_EVENT_PATH": _pr_event(pull_request),
            },
        )

    def test_body(self):
        with self._env({"body": "Adds a parser."}):
            self.assertEqual(main.get_pr_body(), "Adds a parser.")

    def test_no_description_is_none(self):
        for body in (None, "", " \n "):
            with self.subTest(body=body), self._env({"body": body}):
                self.assertIsNone(main.get_pr_body())

    def test_an_agent_app_opened_it(self):
        with self._env({"user": {"login": "Copilot-SWE-Agent[bot]"}}):
            self.assertEqual(main.get_pr_opener_tool(), "GitHub Copilot")

    def test_a_person_or_another_bot_opened_it(self):
        for user in ({"login": "octocat"}, {"login": "dependabot[bot]"}, None, {}):
            with self.subTest(user=user), self._env({"user": user}):
                self.assertIsNone(main.get_pr_opener_tool())


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

    def test_no_description_nothing_to_check(self):
        results, mock_scope = self._run(True, "true", body=None)
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


class TestAiToolsFound(unittest.TestCase):
    def setUp(self):
        main.PR_BODY_AI_TOOLS.clear()
        self.addCleanup(main.PR_BODY_AI_TOOLS.clear)

    def test_union_of_scopes_description_and_opener(self):
        main.PR_BODY_AI_TOOLS[:] = ["OpenAI Codex"]
        results = [
            main.ScopeResult(label="Commit 1/2", ai_tools=["Claude Code"]),
            main.ScopeResult(label="Commit 2/2", ai_tools=["Claude Code", "Cursor"]),
        ]
        with (
            patch("main.is_pr_event", return_value=True),
            patch("main.get_pr_opener_tool", return_value="Devin"),
        ):
            found = main.ai_tools_found(results)
        self.assertEqual(found, ["Claude Code", "Cursor", "Devin", "OpenAI Codex"])

    def test_outside_a_pull_request_only_the_scopes(self):
        results = [main.ScopeResult(label="Commit message", ai_tools=["Aider"])]
        with (
            patch("main.is_pr_event", return_value=False),
            patch("main.get_pr_opener_tool") as opener,
        ):
            self.assertEqual(main.ai_tools_found(results), ["Aider"])
        opener.assert_not_called()

    def test_result_output_carries_them(self):
        output_path = os.path.join(tempfile.mkdtemp(), "output.txt")
        results = [main.ScopeResult(label="Commit 1/1", ai_tools=["Claude Code"])]
        with (
            patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}),
            patch("main.is_pr_event", return_value=False),
        ):
            main.set_result_output(results)
        payload = json.loads(read_github_output(output_path)["result"])
        self.assertEqual(payload["ai_tools"], ["Claude Code"])


class TestAddAiLabel(unittest.TestCase):
    """``ai-label``: added when an AI tool wrote any of the PR, never removed."""

    AI_SCOPE = main.ScopeResult(label="Commit 1/1", ai_tools=["Claude Code"])

    def setUp(self):
        main.PR_BODY_AI_TOOLS.clear()

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
            patch("main.is_fork_pr_with_readonly_token", return_value=False),
            patch("main.get_pr_opener_tool", return_value=None),
            patch.dict(sys.modules, {"github": github_module}),
            patch("builtins.print") as mock_print,
        ):
            main.add_ai_label(results)
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
            "ai-assisted", main.AI_LABEL_COLOR, "Written with the help of an AI tool"
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
            [main.ScopeResult(label="Commit 1/1")]
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
            main.add_ai_label([self.AI_SCOPE])
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
            patch("main.is_fork_pr_with_readonly_token", return_value=False),
            patch("main.get_pr_opener_tool", return_value=None),
            patch.dict(
                sys.modules,
                {"github": MagicMock(GithubException=_StubGithubException)},
            ),
            patch("builtins.print") as mock_print,
        ):
            main.add_ai_label([self.AI_SCOPE])
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
            patch("main.is_fork_pr_with_readonly_token", return_value=False),
            patch("main.get_pr_opener_tool", return_value=None),
            patch.dict(
                sys.modules,
                {"github": MagicMock(GithubException=_StubGithubException)},
            ),
            patch("builtins.print") as mock_print,
        ):
            main.add_ai_label([self.AI_SCOPE])
        self.assertIn("GITHUB_REPOSITORY is not set", mock_print.call_args[0][0])

    def test_fork_with_a_read_only_token_warns(self):
        with (
            patch.dict(
                os.environ, {"AI_LABEL": "true", "GITHUB_EVENT_NAME": "pull_request"}
            ),
            patch("main.is_fork_pr_with_readonly_token", return_value=True),
            patch("main.get_pr_opener_tool", return_value=None),
            patch("builtins.print") as mock_print,
        ):
            main.add_ai_label([self.AI_SCOPE])
        warning = mock_print.call_args[0][0]
        self.assertIn("::warning::AI tools found (Claude Code)", warning)
        self.assertIn("read-only", warning)

    def test_a_missing_pygithub_warns(self):
        with (
            patch.dict(
                os.environ, {"AI_LABEL": "true", "GITHUB_EVENT_NAME": "pull_request"}
            ),
            patch("main.is_fork_pr_with_readonly_token", return_value=False),
            patch("main.get_pr_opener_tool", return_value=None),
            patch.dict(sys.modules, {"github": None}),
            patch("builtins.print") as mock_print,
        ):
            main.add_ai_label([self.AI_SCOPE])
        self.assertIn("Unable to add the AI label", mock_print.call_args[0][0])


if __name__ == "__main__":
    unittest.main()


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


class TestBooleanInputs(unittest.TestCase):
    """A boolean input is true or false; anything else fails the step."""

    def setUp(self):
        main.INVALID_FLAGS.clear()
        self.addCleanup(main.INVALID_FLAGS.clear)

    def test_true_and_false_in_any_case_and_padding(self):
        for value, expected in (
            ("true", True),
            (" True ", True),
            ("FALSE", False),
            ("false", False),
        ):
            with patch.dict(os.environ, {"MESSAGE": value}):
                self.assertIs(main.env_flag("MESSAGE"), expected, value)
        self.assertEqual(main.INVALID_FLAGS, [])

    def test_unset_input_takes_the_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(main.env_flag("MESSAGE"))
        self.assertEqual(main.INVALID_FLAGS, [])

    def test_other_values_are_recorded(self):
        for value in ("yes", "on", "1", "maybe"):
            with patch.dict(os.environ, {"MESSAGE": value}):
                self.assertFalse(main.env_flag("MESSAGE"))
        self.assertEqual(
            main.INVALID_FLAGS,
            [
                ("MESSAGE", "yes"),
                ("MESSAGE", "on"),
                ("MESSAGE", "1"),
                ("MESSAGE", "maybe"),
            ],
        )

    def test_invalid_input_fails_before_any_check(self):
        main.INVALID_FLAGS.append(("PR_TITLE", "yes"))
        out = io.StringIO()
        with (
            patch("sys.stdout", out),
            patch("main.run_commit_check") as run,
            self.assertRaises(SystemExit) as cm,
        ):
            main.main()
        self.assertEqual(cm.exception.code, 1)
        run.assert_not_called()
        self.assertIn(
            "::error::Input pr-title must be true or false, got 'yes'", out.getvalue()
        )


class TestPrCommentApiUrl(unittest.TestCase):
    """The PR comment goes to the same API host as every other call (GHES)."""

    def test_comment_client_uses_github_api_url(self):
        github_module = MagicMock()
        pull_request = MagicMock()
        pull_request.get_comments.return_value = []
        github_module.Github.return_value.get_repo.return_value.get_issue.return_value = pull_request
        with (
            patch("main.PR_COMMENTS_ENABLED", True),
            patch("main.is_fork_pr_with_readonly_token", return_value=False),
            patch("main.get_pr_number", return_value=7),
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
            main.add_pr_comments([fail_scope()])
        _, kwargs = github_module.Github.call_args
        self.assertEqual(kwargs["base_url"], "https://ghe.example.com/api/v3")


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
