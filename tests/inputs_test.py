"""Action inputs: parsing, validation and the wiring from action.yml."""

import io
import os
import re
import unittest
from unittest.mock import patch

import main
from action import cli, inputs


class TestEnvFlag(unittest.TestCase):
    def test_true_value(self):
        with patch.dict(os.environ, {"FEATURE_FLAG": "true"}):
            self.assertTrue(inputs.env_flag("FEATURE_FLAG"))

    def test_false_value(self):
        with patch.dict(os.environ, {"FEATURE_FLAG": "false"}):
            self.assertFalse(inputs.env_flag("FEATURE_FLAG"))

    def test_missing_uses_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(inputs.env_flag("FEATURE_FLAG", default="true"))


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
            inputs.log_env_vars()
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


class TestBuildCheckArgs(unittest.TestCase):
    def test_all_true(self):
        with (
            patch("action.inputs.MESSAGE_ENABLED", True),
            patch("action.inputs.BRANCH_ENABLED", True),
            patch("action.inputs.AUTHOR_NAME_ENABLED", True),
            patch("action.inputs.AUTHOR_EMAIL_ENABLED", True),
        ):
            result = cli.build_check_args()
        self.assertEqual(
            result, ["--message", "--branch", "--author-name", "--author-email"]
        )

    def test_all_false(self):
        with (
            patch("action.inputs.MESSAGE_ENABLED", False),
            patch("action.inputs.BRANCH_ENABLED", False),
            patch("action.inputs.AUTHOR_NAME_ENABLED", False),
            patch("action.inputs.AUTHOR_EMAIL_ENABLED", False),
        ):
            result = cli.build_check_args()
        self.assertEqual(result, [])

    def test_message_and_branch(self):
        with (
            patch("action.inputs.MESSAGE_ENABLED", True),
            patch("action.inputs.BRANCH_ENABLED", True),
            patch("action.inputs.AUTHOR_NAME_ENABLED", False),
            patch("action.inputs.AUTHOR_EMAIL_ENABLED", False),
        ):
            result = cli.build_check_args()
        self.assertEqual(result, ["--message", "--branch"])


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
                self.assertEqual(inputs.ai_label_name(), expected)

    def test_unset_is_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(inputs.ai_label_name())


class TestBooleanInputs(unittest.TestCase):
    """A boolean input is true or false; anything else fails the step."""

    def setUp(self):
        inputs.INVALID_FLAGS.clear()
        self.addCleanup(inputs.INVALID_FLAGS.clear)

    def test_true_and_false_in_any_case_and_padding(self):
        for value, expected in (
            ("true", True),
            (" True ", True),
            ("FALSE", False),
            ("false", False),
        ):
            with patch.dict(os.environ, {"MESSAGE": value}):
                self.assertIs(inputs.env_flag("MESSAGE"), expected, value)
        self.assertEqual(inputs.INVALID_FLAGS, [])

    def test_unset_input_takes_the_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(inputs.env_flag("MESSAGE"))
        self.assertEqual(inputs.INVALID_FLAGS, [])

    def test_other_values_are_recorded(self):
        for value in ("yes", "on", "1", "maybe"):
            with patch.dict(os.environ, {"MESSAGE": value}):
                self.assertFalse(inputs.env_flag("MESSAGE"))
        self.assertEqual(
            inputs.INVALID_FLAGS,
            [
                ("MESSAGE", "yes"),
                ("MESSAGE", "on"),
                ("MESSAGE", "1"),
                ("MESSAGE", "maybe"),
            ],
        )

    def test_invalid_input_fails_before_any_check(self):
        inputs.INVALID_FLAGS.append(("PR_TITLE", "yes"))
        out = io.StringIO()
        with (
            patch("sys.stdout", out),
            patch("action.cli.run_commit_check") as run,
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
