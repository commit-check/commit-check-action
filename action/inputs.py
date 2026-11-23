"""The action's inputs, read from the environment action.yml sets."""

import os
import sys

from action import escape

GITHUB_STEP_SUMMARY = os.getenv("GITHUB_STEP_SUMMARY", "")

#: Boolean inputs whose value was neither true nor false, as ``(name, value)``.
#: ``main`` fails the step on them before any check runs.
INVALID_FLAGS: list[tuple[str, str]] = []


def env_flag(name: str, default: str = "false") -> bool:
    """Read a GitHub Action boolean input: ``true`` or ``false``, any case.

    Anything else (``yes``, ``on``, a typo) used to read as false and turn
    the check off without a word. It is now recorded in ``INVALID_FLAGS``,
    and the step fails with the input's name.
    """
    value = os.getenv(name, default).strip().lower()
    if value not in ("true", "false"):
        INVALID_FLAGS.append((name, os.getenv(name, default)))
    return value == "true"


MESSAGE_ENABLED = env_flag("MESSAGE")

BRANCH_ENABLED = env_flag("BRANCH")

AUTHOR_NAME_ENABLED = env_flag("AUTHOR_NAME")

AUTHOR_EMAIL_ENABLED = env_flag("AUTHOR_EMAIL")

FILES_ENABLED = env_flag("FILES")

DRY_RUN_ENABLED = env_flag("DRY_RUN")

JOB_SUMMARY_ENABLED = env_flag("JOB_SUMMARY")

PR_COMMENTS_ENABLED = env_flag("PR_COMMENTS")

PR_TITLE_ENABLED = env_flag("PR_TITLE")

PR_BODY_AI_ATTRIBUTION_ENABLED = env_flag("PR_BODY_AI_ATTRIBUTION")

MESSAGE_AI_ATTRIBUTION_ENABLED = env_flag("MESSAGE_AI_ATTRIBUTION")

#: Where the checks read the config: ``head``, the files the workflow
#: checked out, or ``base``, the pull request's base branch, so that a pull
#: request cannot loosen the rules it is checked against.
CONFIG_SOURCES = ("head", "base")

CONFIG_SOURCE = os.getenv("CONFIG_SOURCE", "head").strip().lower() or "head"

#: The label ``ai-label: true`` puts on a pull request that an AI tool wrote.
DEFAULT_AI_LABEL = "ai-assisted"


def ai_label_name() -> str | None:
    """The label to put on an AI-written pull request, or ``None`` for none.

    ``ai-label: true`` means the default label; any other value but
    ``false`` is the label's own name.
    """
    value = os.getenv("AI_LABEL", "false").strip()
    if not value or value.lower() == "false":
        return None
    return DEFAULT_AI_LABEL if value.lower() == "true" else value


def log_env_vars():
    """Logs the environment variables for debugging purposes.

    Uses the ``::debug::`` workflow command so these only appear in the
    action log when ``ACTIONS_STEP_DEBUG`` is set to ``true``.
    """
    for name in (
        "MESSAGE",
        "BRANCH",
        "AUTHOR_NAME",
        "AUTHOR_EMAIL",
        "FILES",
        "DRY_RUN",
        "JOB_SUMMARY",
        "PR_COMMENTS",
        "PR_TITLE",
        "PR_BODY_AI_ATTRIBUTION",
        "MESSAGE_AI_ATTRIBUTION",
        "AI_LABEL",
    ):
        value = os.getenv(name, "false")
        print(f"::debug::{name}={value}")
    print(f"::debug::CONFIG_SOURCE={CONFIG_SOURCE}")


def fail_on_invalid_flags() -> None:
    """Stop with an error when an input holds a value it does not take."""
    invalid = CONFIG_SOURCE not in CONFIG_SOURCES
    if not INVALID_FLAGS and not invalid:
        return
    for name, value in INVALID_FLAGS:
        input_name = name.lower().replace("_", "-")
        print(
            f"::error::Input {input_name} must be true or false, got "
            f"{escape._log_safe(repr(value))}"
        )
    if invalid:
        print(
            "::error::Input config-source must be head or base, got "
            f"{escape._log_safe(repr(os.getenv('CONFIG_SOURCE', '')))}"
        )
    sys.exit(1)
