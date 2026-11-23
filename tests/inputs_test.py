"""Action inputs: parsing, validation and the wiring from action.yml."""

import io
import os
import re
import sys
import unittest
from unittest.mock import patch

import main


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
                "::debug::FILES=false",
                "::debug::DRY_RUN=false",
                "::debug::JOB_SUMMARY=false",
                "::debug::PR_COMMENTS=false",
                "::debug::PR_TITLE=false",
                "::debug::PR_BODY_AI_ATTRIBUTION=false",
                "::debug::MESSAGE_AI_ATTRIBUTION=false",
                "::debug::AI_LABEL=false",
                "::debug::CONFIG_SOURCE=head",
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


class TestFilesInputIsWired(unittest.TestCase):
    """The input reaches the script, and the README lists it."""

    def _read(self, name: str) -> str:
        here = os.path.dirname(os.path.abspath(main.__file__))
        with open(os.path.join(here, name), encoding="utf-8") as f:
            return f.read()

    def test_action_yml_declares_it_off_and_passes_it_on(self):
        action = self._read("action.yml")
        declared = re.search(
            r"^  files:\n    description: .+\n    required: false\n    default: false$",
            action,
            re.MULTILINE,
        )
        self.assertIsNotNone(declared)
        self.assertIn("        FILES: ${{ inputs.files }}\n", action)

    def test_the_readme_lists_it(self):
        self.assertIn("| `files` | `false` |", self._read("README.md"))
