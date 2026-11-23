"""config-source: base, which checks a pull request against its base branch's config."""

import os
import subprocess
import tempfile
import tomllib

from action import context, escape, inputs

#: The config files commit-check looks for, in its order.
CONFIG_PATHS = (
    "cchk.toml",
    "commit-check.toml",
    ".github/cchk.toml",
    ".github/commit-check.toml",
)

#: Passed to every commit-check run: ``--config`` and the base branch's
#: config, when ``config-source`` is ``base``; empty otherwise.
CONFIG_ARGS: list[str] = []


class BaseConfigError(Exception):
    """The base branch's config is there but cannot be used as it is."""


def _base_blob(sha: str, path: str) -> bytes | None:
    """The file at ``path`` in commit ``sha``, or ``None`` when it has none.

    Absence is read from the commit's tree, so a file that is there but
    cannot be read -- a blob a partial clone could not fetch -- is an error,
    never taken for no config. A symbolic link is refused: its blob is the
    target's path, not the config.

    :raises BaseConfigError: If the file is there but cannot be read.
    """
    try:
        listed = subprocess.run(
            ["git", "ls-tree", "--full-tree", sha, "--", path],
            capture_output=True,
            check=False,
        )
        if listed.returncode != 0:
            raise BaseConfigError(f"git could not list {path} in the base commit")
        if not listed.stdout:
            return None
        mode, kind, _rest = listed.stdout.split(b" ", 2)
        if kind != b"blob":
            return None  # a folder of that name is no config file
        if mode == b"120000":
            raise BaseConfigError(
                f"{path} is a symbolic link on the base branch, which "
                "config-source: base does not follow"
            )
        result = subprocess.run(
            ["git", "cat-file", "blob", f"{sha}:{path}"],
            capture_output=True,
            check=False,
        )
    except OSError as e:
        raise BaseConfigError(f"git could not run: {e}") from e
    if result.returncode != 0:
        raise BaseConfigError(f"{path} could not be read from the base commit")
    return result.stdout


def _checked_out_config() -> tuple[str, bytes] | None:
    """The config file the checkout holds, as (path, contents), or ``None``."""
    for path in CONFIG_PATHS:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return path, f.read()
    return None


def _local_parent(data: bytes) -> str | None:
    """A config's ``inherit_from`` when it names a local file.

    A ``github:`` or HTTPS parent is fetched from where it names, whichever
    branch the config came from; only a local file could be read from the
    pull request.
    """
    try:
        parent = tomllib.loads(data.decode("utf-8")).get("inherit_from")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None  # commit-check reports the broken file itself
    if not isinstance(parent, str) or not parent:
        return None
    if parent.startswith(("github:", "https://", "http://")):
        return None
    return parent


def _repo_path(work: str, path: str) -> str | None:
    """``path`` under ``work`` as a repository path, or ``None`` outside it."""
    path = os.path.normpath(path)
    if os.path.commonpath([work, path]) != work:
        return None
    return os.path.relpath(path, work).replace(os.sep, "/")


def _config_error(text: str) -> bool:
    print(f"::error title=commit-check::{escape._annotation_escape(text)}")
    return False


def use_base_config() -> bool:
    """With ``config-source: base``, point every check at the base config.

    The base branch's config file is written under ``RUNNER_TEMP`` at its
    path in the repository, and ``CONFIG_ARGS`` names it. A parent it names
    by local path (``inherit_from``) is copied from the base branch too, to
    the path commit-check tries first, next to the config; otherwise
    commit-check would go on to find the pull request's copy. Without a
    config on the base branch the checks run on the defaults.

    Fails closed: if the base branch cannot be read, the step fails rather
    than check against a config the pull request may have changed.

    Returns whether the checks can run.
    """
    if inputs.CONFIG_SOURCE != "base":
        return True
    if not context.is_pr_event():
        print(
            "::notice title=commit-check::config-source: base applies to pull "
            "requests; this run reads the checked-out config"
        )
        return True
    sha = context.get_pr_base_sha()
    if not sha or not (context._rev_resolves(sha) or context._fetch_commit(sha)):
        return _config_error(
            "config-source is base, but the base branch commit could not be "
            f"read ({context.checkout_hint()}); nothing was checked, so that a config "
            "the pull request changes cannot apply"
        )
    try:
        return _stage_base_config(sha)
    except BaseConfigError as e:
        return _config_error(f"config-source is base, but {e}; nothing was checked")


def _stage_base_config(sha: str) -> bool:
    """Write the base commit's config under ``RUNNER_TEMP``; see above."""
    found = None
    for path in CONFIG_PATHS:
        data = _base_blob(sha, path)
        if data is not None:
            found = (path, data)
            break
    path, data = found or ("cchk.toml", b"")
    work = os.path.realpath(
        tempfile.mkdtemp(prefix="commit-check-config-", dir=os.getenv("RUNNER_TEMP"))
    )
    target = os.path.join(work, *path.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(data)

    parent = _local_parent(data)
    if parent is not None:
        if os.path.isabs(parent) or parent.startswith(("/", "\\")):
            return _config_error(
                f"config-source is base, but the base branch config {path} "
                f'inherits from "{parent}", an absolute path the base branch '
                "cannot vouch for; nothing was checked"
            )
        # The path as commit-check builds it, next to the config. Checked
        # with this platform's separators, so "..\\x" cannot climb out on
        # Windows.
        dest = os.path.join(os.path.dirname(target), parent)
        beside = _repo_path(work, dest)
        if beside is None:
            return _config_error(
                f"config-source is base, but the base branch config {path} "
                f'inherits from "{parent}", outside the repository; nothing '
                "was checked"
            )
        # commit-check looks next to the config first, then from the top of
        # the repository; whichever the base branch holds is copied to the
        # first place, so the second is never reached.
        top = _repo_path(work, os.path.join(work, parent))
        parent_data = None
        for candidate in [beside] + ([top] if top else []):
            parent_data = _base_blob(sha, candidate)
            if parent_data is not None:
                break
        if parent_data is None:
            # As on the base branch itself, a parent that is missing leaves
            # the config in force; the empty copy keeps the pull request's
            # file, if it adds one, from being read in its place.
            print(
                "::notice title=commit-check::"
                + escape._annotation_escape(
                    f'inherit_from "{parent}" in {path} is not on the base '
                    "branch; checking with that config alone"
                )
            )
            parent_data = b""
        # A parent that names a folder ("." or the config's own folder) is
        # no file: commit-check says so and keeps the config, as it would on
        # the base branch.
        if not os.path.isdir(os.path.normpath(dest)):
            # The folders of the path as written, so that "policies/../x"
            # resolves here too, not from the checkout.
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(os.path.normpath(dest), "wb") as f:
                f.write(parent_data)

    CONFIG_ARGS[:] = ["--config", target]
    if _checked_out_config() != found:
        if found is None:
            text = (
                "This pull request adds a commit-check config. The checks "
                "use the defaults, as the base branch has no config; the new "
                "config takes effect after it merges"
            )
        else:
            text = (
                f"This pull request changes the commit-check config. The "
                f"checks use {path} from the base branch; the change takes "
                "effect after it merges"
            )
        print(f"::notice title=commit-check::{escape._annotation_escape(text)}")
    return True
