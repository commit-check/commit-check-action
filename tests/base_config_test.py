"""config-source: base, which checks a pull request against its base branch's config."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import main
from action import base_config, cli


class TestConfigSource(unittest.TestCase):
    """config-source: base checks a pull request against its base branch's config."""

    def setUp(self):
        base_config.CONFIG_ARGS.clear()
        self.addCleanup(base_config.CONFIG_ARGS.clear)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.repo = os.path.join(self.tmp, "repo")
        os.mkdir(self.repo)
        self.runner_temp = os.path.join(self.tmp, "runner")
        os.mkdir(self.runner_temp)
        gitconfig = os.path.join(self.tmp, "gitconfig")
        with open(gitconfig, "w", encoding="utf-8") as f:
            f.write(
                "[user]\n\tname = test-author\n\temail = test@example.com\n"
                "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n"
            )
        self.event = os.path.join(self.tmp, "event.json")
        env = {k: v for k, v in os.environ.items() if not k.startswith("CCHK_")}
        env.update(
            GIT_CONFIG_GLOBAL=gitconfig,
            GIT_CONFIG_SYSTEM=gitconfig,
            GITHUB_EVENT_NAME="pull_request",
            GITHUB_EVENT_PATH=self.event,
            RUNNER_TEMP=self.runner_temp,
        )
        for target in (
            patch.dict(os.environ, env, clear=True),
            patch("action.inputs.CONFIG_SOURCE", "base"),
        ):
            target.start()
            self.addCleanup(target.stop)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.repo)
        self._git("init", "-q")

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        return result.stdout.strip()

    def _commit(self, files: dict[str, str | None]) -> str:
        for name, content in files.items():
            path = os.path.join(self.repo, *name.split("/"))
            if content is None:
                os.unlink(path)
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", "chore: change")
        return self._git("rev-parse", "HEAD")

    def _base(self, sha: str | None) -> None:
        with open(self.event, "w", encoding="utf-8") as f:
            json.dump({"pull_request": {"base": {"sha": sha}}}, f)

    def _use_base(self) -> tuple[bool, str]:
        out = io.StringIO()
        with patch("sys.stdout", out):
            ok = base_config.use_base_config()
        return ok, out.getvalue()

    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _config_file(self) -> str:
        self.assertEqual(base_config.CONFIG_ARGS[0], "--config")
        return base_config.CONFIG_ARGS[1]

    def test_head_reads_the_checkout_and_runs_nothing(self):
        with (
            patch("action.inputs.CONFIG_SOURCE", "head"),
            patch("subprocess.run") as run,
        ):
            ok, out = self._use_base()
        self.assertTrue(ok)
        self.assertEqual((base_config.CONFIG_ARGS, out), ([], ""))
        run.assert_not_called()

    def test_outside_a_pull_request_the_checkout_is_read(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}):
            ok, out = self._use_base()
        self.assertTrue(ok)
        self.assertEqual(base_config.CONFIG_ARGS, [])
        self.assertIn("config-source: base applies to pull requests", out)

    def test_the_pull_request_cannot_loosen_its_own_rules(self):
        base = self._commit(
            {
                ".github/cchk.toml": 'inherit_from = "../shared.toml"\n',
                "shared.toml": '[commit]\nallow_commit_types = ["feat"]\n',
            }
        )
        self._commit(
            {
                ".github/cchk.toml": "",
                "shared.toml": '[commit]\nallow_commit_types = ["chore"]\n',
            }
        )
        self._base(base)
        ok, out = self._use_base()
        self.assertTrue(ok)
        config = self._config_file()
        self.assertTrue(config.startswith(os.path.realpath(self.runner_temp)), config)
        self.assertTrue(config.endswith(os.path.join(".github", "cchk.toml")))
        self.assertEqual(self._read(config), 'inherit_from = "../shared.toml"\n')
        parent = os.path.join(os.path.dirname(os.path.dirname(config)), "shared.toml")
        self.assertEqual(
            self._read(parent), '[commit]\nallow_commit_types = ["feat"]\n'
        )
        self.assertIn(
            "::notice title=commit-check::This pull request changes the "
            "commit-check config. The checks use .github/cchk.toml from the base "
            "branch; the change takes effect after it merges",
            out,
        )

    def test_a_parent_named_from_the_top_is_copied_where_it_is_looked_for_first(self):
        base = self._commit(
            {
                ".github/cchk.toml": 'inherit_from = "org/base.toml"\n',
                "org/base.toml": "[commit]\nsubject_max_length = 20\n",
            }
        )
        self._commit({"org/base.toml": "[commit]\nsubject_max_length = 200\n"})
        self._base(base)
        ok, out = self._use_base()
        self.assertTrue(ok)
        config_dir = os.path.dirname(self._config_file())
        self.assertEqual(
            self._read(os.path.join(config_dir, "org", "base.toml")),
            "[commit]\nsubject_max_length = 20\n",
        )
        # Only the parent changed: the config file itself is the same.
        self.assertEqual(out, "")

    def test_a_parent_missing_from_the_base_is_read_as_empty(self):
        base = self._commit({"cchk.toml": 'inherit_from = "shared.toml"\n'})
        self._commit({"shared.toml": "[commit]\nsubject_max_length = 200\n"})
        self._base(base)
        ok, out = self._use_base()
        self.assertTrue(ok)
        config_dir = os.path.dirname(self._config_file())
        self.assertEqual(self._read(os.path.join(config_dir, "shared.toml")), "")
        self.assertIn(
            'inherit_from "shared.toml" in cchk.toml is not on the base branch; '
            "checking with that config alone",
            out,
        )

    def test_a_parent_that_names_a_folder_is_left_to_commit_check(self):
        base = self._commit({".github/cchk.toml": 'inherit_from = "."\n'})
        self._base(base)
        ok, _out = self._use_base()
        self.assertTrue(ok)
        self.assertEqual(
            os.listdir(os.path.dirname(self._config_file())), ["cchk.toml"]
        )

    def test_a_parent_outside_the_repository_fails_closed(self):
        base = self._commit({"cchk.toml": 'inherit_from = "../org.toml"\n'})
        self._base(base)
        ok, out = self._use_base()
        self.assertFalse(ok)
        self.assertEqual(base_config.CONFIG_ARGS, [])
        self.assertIn(
            "::error title=commit-check::config-source is base, but the base "
            'branch config cchk.toml inherits from "../org.toml", outside the '
            "repository; nothing was checked",
            out,
        )

    def test_a_remote_parent_is_left_to_commit_check(self):
        base = self._commit(
            {"cchk.toml": 'inherit_from = "github:org/policy:cchk.toml"\n'}
        )
        self._base(base)
        ok, out = self._use_base()
        self.assertTrue(ok)
        self.assertEqual(
            os.listdir(os.path.dirname(self._config_file())), ["cchk.toml"]
        )
        self.assertEqual(out, "")

    def test_a_config_the_pull_request_adds_waits_for_the_merge(self):
        base = self._commit({"README.md": "hi\n"})
        self._commit({"cchk.toml": "[commit]\nsubject_max_length = 200\n"})
        self._base(base)
        ok, out = self._use_base()
        self.assertTrue(ok)
        config = self._config_file()
        self.assertEqual(os.path.basename(config), "cchk.toml")
        self.assertEqual(self._read(config), "")
        self.assertIn(
            "This pull request adds a commit-check config. The checks use the "
            "defaults, as the base branch has no config; the new config takes "
            "effect after it merges",
            out,
        )

    def test_no_config_on_either_side_says_nothing(self):
        base = self._commit({"README.md": "hi\n"})
        self._base(base)
        ok, out = self._use_base()
        self.assertTrue(ok)
        self.assertEqual(out, "")

    def test_a_base_commit_the_event_lacks_fails_closed(self):
        self._base(None)
        ok, out = self._use_base()
        self.assertFalse(ok)
        self.assertEqual(base_config.CONFIG_ARGS, [])
        self.assertIn(
            "::error title=commit-check::config-source is base, but the base "
            "branch commit could not be read (is actions/checkout using "
            "fetch-depth: 0?); nothing was checked, so that a config the pull "
            "request changes cannot apply",
            out,
        )

    def test_a_base_commit_the_clone_lacks_is_fetched(self):
        self._base("a" * 40)
        with (
            patch("action.context._fetch_commit", return_value=False) as fetch,
            patch("sys.stdout", io.StringIO()),
        ):
            self.assertFalse(base_config.use_base_config())
        fetch.assert_called_once_with("a" * 40)
        with (
            patch("action.context._rev_resolves", return_value=False),
            patch("action.context._fetch_commit", return_value=True),
            patch("action.base_config._base_blob", return_value=None),
            patch("sys.stdout", io.StringIO()),
        ):
            self.assertTrue(base_config.use_base_config())
        self.assertEqual(self._read(self._config_file()), "")

    def test_git_missing_fails_closed(self):
        with (
            patch("subprocess.run", side_effect=OSError("no git")),
            self.assertRaisesRegex(base_config.BaseConfigError, "git could not run"),
        ):
            base_config._base_blob("a" * 40, "cchk.toml")

    def test_a_commit_git_cannot_list_fails_closed(self):
        with self.assertRaisesRegex(
            base_config.BaseConfigError, "could not list cchk.toml"
        ):
            base_config._base_blob("a" * 40, "cchk.toml")

    def test_a_file_git_cannot_read_is_not_taken_for_no_config(self):
        """A partial clone that cannot fetch the blob must not read as absent."""
        listed = MagicMock(returncode=0, stdout=b"100644 blob abc\tcchk.toml\n")
        unread = MagicMock(returncode=128, stdout=b"")
        with (
            patch("subprocess.run", side_effect=[listed, unread]),
            self.assertRaisesRegex(
                base_config.BaseConfigError, "cchk.toml could not be read from the base"
            ),
        ):
            base_config._base_blob("a" * 40, "cchk.toml")
        base = self._commit({"cchk.toml": "[commit]\n"})
        self._base(base)
        real_run = subprocess.run

        def no_blobs(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "cat-file"]:
                return unread
            return real_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=no_blobs):
            ok, out = self._use_base()
        self.assertFalse(ok)
        self.assertEqual(base_config.CONFIG_ARGS, [])
        self.assertIn(
            "::error title=commit-check::config-source is base, but cchk.toml "
            "could not be read from the base commit; nothing was checked",
            out,
        )

    @unittest.skipIf(sys.platform == "win32", "symbolic links need privileges")
    def test_a_symbolic_link_is_refused(self):
        self._commit({"policy.toml": "[commit]\n"})
        os.symlink("policy.toml", os.path.join(self.repo, "cchk.toml"))
        base = self._commit({})
        self._base(base)
        ok, out = self._use_base()
        self.assertFalse(ok)
        self.assertIn(
            "cchk.toml is a symbolic link on the base branch, which "
            "config-source: base does not follow; nothing was checked",
            out,
        )

    def test_a_folder_named_like_a_config_is_no_config(self):
        base = self._commit({"cchk.toml/README.md": "hi\n"})
        self.assertIsNone(base_config._base_blob(base, "cchk.toml"))

    def test_an_absolute_parent_fails_closed(self):
        for parent in ("/etc/cchk.toml", "\\\\server\\cchk.toml"):
            base_config.CONFIG_ARGS.clear()
            base = self._commit({"cchk.toml": f"inherit_from = {json.dumps(parent)}\n"})
            self._base(base)
            ok, out = self._use_base()
            self.assertFalse(ok, parent)
            self.assertEqual(base_config.CONFIG_ARGS, [])
            self.assertIn("an absolute path the base branch cannot vouch for", out)

    def test_a_parent_path_through_a_folder_resolves_to_the_base_copy(self):
        """``policies/../shared.toml`` needs ``policies`` to exist to resolve;
        without it commit-check would fall back to the checkout's file."""
        base = self._commit(
            {
                "cchk.toml": 'inherit_from = "policies/../shared.toml"\n',
                "shared.toml": "[commit]\nsubject_max_length = 20\n",
            }
        )
        self._commit({"shared.toml": "[commit]\nsubject_max_length = 200\n"})
        self._base(base)
        self.assertTrue(self._use_base()[0])
        config_dir = os.path.dirname(self._config_file())
        self.assertEqual(
            self._read(os.path.join(config_dir, "policies", "..", "shared.toml")),
            "[commit]\nsubject_max_length = 20\n",
        )

    def test_only_a_local_parent_is_copied(self):
        for data in (
            b'inherit_from = "https://example.com/cchk.toml"\n',
            b'inherit_from = "http://example.com/cchk.toml"\n',
            b'inherit_from = "github:org/policy:cchk.toml"\n',
            b'inherit_from = ""\n',
            b"inherit_from = 3\n",
            b"[commit\n",
            b"\xff\xfe",
            b"",
        ):
            self.assertIsNone(base_config._local_parent(data), data)
        self.assertEqual(
            base_config._local_parent(b'inherit_from = "a/b.toml"\n'), "a/b.toml"
        )
        self.assertEqual(
            base_config._local_parent(b'inherit_from = "/etc/c.toml"\n'), "/etc/c.toml"
        )

    @unittest.skipUnless(shutil.which("commit-check"), "needs commit-check")
    def test_commit_check_runs_on_the_base_rules(self):
        base = self._commit({"cchk.toml": '[commit]\nallow_commit_types = ["feat"]\n'})
        self._commit({"cchk.toml": '[commit]\nallow_commit_types = ["chore"]\n'})
        self._base(base)
        self.assertTrue(self._use_base()[0])
        with patch("sys.stderr", io.StringIO()):
            rc, data, _raw = cli.run_check_json(
                ["--message"], input_text="chore: tidy up\n"
            )
        self.assertEqual(rc, 1)
        self.assertEqual(data["status"], "fail")

    def test_every_check_gets_the_config(self):
        base_config.CONFIG_ARGS[:] = ["--config", "/tmp/x/cchk.toml"]
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=mock_result) as run:
            cli.run_check_json(["--branch"])
        self.assertEqual(
            run.call_args[0][0],
            [
                "commit-check",
                "--format",
                "json",
                "--config",
                "/tmp/x/cchk.toml",
                "--branch",
            ],
        )

    def test_an_unknown_value_fails_before_any_check(self):
        out = io.StringIO()
        with (
            patch("action.inputs.CONFIG_SOURCE", "main"),
            patch.dict(os.environ, {"CONFIG_SOURCE": "main"}),
            patch("sys.stdout", out),
            patch("action.cli.run_commit_check") as run,
            self.assertRaises(SystemExit) as cm,
        ):
            main.main()
        self.assertEqual(cm.exception.code, 1)
        run.assert_not_called()
        self.assertIn(
            "::error::Input config-source must be head or base, got 'main'",
            out.getvalue(),
        )

    def test_main_stops_when_the_base_config_cannot_be_read(self):
        with (
            patch("action.inputs.log_env_vars"),
            patch("action.base_config.use_base_config", return_value=False),
            patch("action.cli.run_commit_check") as run,
            self.assertRaises(SystemExit) as cm,
        ):
            main.main()
        self.assertEqual(cm.exception.code, 1)
        run.assert_not_called()

    def test_action_yml_declares_it_and_passes_it_on(self):
        here = os.path.dirname(os.path.abspath(main.__file__))
        with open(os.path.join(here, "action.yml"), encoding="utf-8") as f:
            action = f.read()
        with open(os.path.join(here, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertRegex(
            action,
            re.compile(
                r"^  config-source:\n    description: .+\n    required: false\n"
                r"    default: head$",
                re.MULTILINE,
            ),
        )
        self.assertIn("        CONFIG_SOURCE: ${{ inputs.config-source }}\n", action)
        self.assertIn("| `config-source` | `head` |", readme)
