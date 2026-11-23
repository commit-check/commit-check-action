"""Escaping text the action prints into the step log or a workflow command."""

import re


def _annotation_escape(text: str) -> str:
    """Escape text for a workflow command payload.

    A newline would end the command and leave the rest of the message as a
    stray log line, and a bare ``%`` can be read as the start of an escape.
    """
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


#: C0 and C1 control characters, and DEL, except the newline and tab that a
#: multi-line commit message legitimately carries.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _log_safe(text: str) -> str:
    """Make commit-controlled text inert before it is printed to the step log.

    The tree prints values from the CLI's JSON, and those are whoever wrote
    the commit, branch or pull request title: a multi-line value (a whole
    message on a failing rule, a sign-off fix) prints each further line on a
    line of its own. The runner reads any log line beginning with ``::`` as a
    workflow command, after skipping leading whitespace, so indenting it is
    not enough; and it reads the older ``##[command]`` form anywhere in a
    line. Left as they are, such lines can forge annotations or mask log
    text. So such a line gets a backslash before its ``::``, ``##[`` is broken
    up, and control characters are shown as ``\\xNN`` escapes. This is the
    CLI's own ``printable()`` (commit-check#620), which covers its text
    output but not the JSON this action renders itself.

    Only the step log needs it: the Markdown report and the annotation
    payloads are not read as commands. It runs before the log's own colours
    are added, so an escape sequence in the log is always the action's.
    """
    if text.isprintable() and "::" not in text and "##[" not in text:
        return text  # nothing to escape: the common case
    text = _CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group()):02x}", text)
    text = text.replace("##[", "#\\#[")
    return "\n".join(
        line.replace("::", "\\::", 1) if line.lstrip().startswith("::") else line
        for line in text.split("\n")
    )
