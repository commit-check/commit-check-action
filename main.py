#!/usr/bin/env python3
"""GitHub Action that runs commit-check and renders results.

The action runs ``commit-check --format json`` to collect structured check
results (rule IDs, error messages, suggestions, docs links), then renders
them to three output surfaces:

* **step log** — grouped sections, then one ``::error`` annotation per finding
* **job summary** — a Markdown report: the verdict, then a diff of what failed
* **PR comment** — the same report (idempotently updated)

and exposes the check data as JSON in the ``result`` action output.

This file is the entrypoint action.yml runs; the code lives in the ``action``
package next to it, one module per part of the work.
"""

import sys

from action import base_config, cli, inputs, publish, render


def _reconfigure_io() -> None:
    """Reconfigure stdout/stderr to UTF-8 so emoji and check marks never
    crash on runners with legacy encodings (e.g. cp1252 on Windows)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def log_error_and_exit(ret_code: int) -> None:
    """Exit with the given code.

    This used to print ``::error::commit-check found N failures.`` first.
    That was a second ``::error`` annotation on top of the one-per-finding
    annotations ``render_step_log`` had already emitted, so GitHub counted
    one error more than there were findings and listed an untitled entry
    that only restated the titled ones. The verdict now lives in
    ``render_step_log`` as a plain line, next to the passing verdicts.
    """
    sys.exit(ret_code)


def main():
    """Main function to run commit-check and render all output surfaces."""
    _reconfigure_io()
    inputs.fail_on_invalid_flags()
    inputs.log_env_vars()
    if not base_config.use_base_config():
        sys.exit(1)

    ret_code, results = cli.run_commit_check()

    render.render_step_log(results)
    publish.set_result_output(results)

    ret_code = max(
        ret_code, publish.add_job_summary(results), publish.add_pr_comments(results)
    )
    publish.add_ai_label(results)

    if inputs.DRY_RUN_ENABLED:
        ret_code = 0

    log_error_and_exit(ret_code)


if __name__ == "__main__":
    main()
