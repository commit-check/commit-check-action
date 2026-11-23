"""The pinned commit-check, unmocked."""

import importlib.metadata
import io
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import main


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

    def _run(self, message: str, check_type: str = "--message") -> tuple[int, dict]:
        rc, data, raw = main.run_check_json(
            [check_type, "--config", self.config], input_text=message
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
        # As a commit message the title fails the rules only a commit can meet.
        rc, data = self._run("feat: add login page")
        failed = {c["rule_id"] for c in data["checks"] if c["status"] == "fail"}
        self.assertEqual(rc, 1)
        self.assertEqual(failed, {"CC011", "CC012"})
        # --pr-title, which the action runs on a title, leaves them out.
        rc, data = self._run("feat: add login page", "--pr-title")
        ran = {c["rule_id"] for c in data["checks"]}
        self.assertEqual(rc, 0)
        self.assertFalse(ran & {"CC011", "CC012", "CC017"})
        scope = main.ScopeResult(label="PR title", checks=data["checks"])
        self.assertEqual(scope.status, "pass")

    def test_a_bad_title_still_fails(self):
        rc, data = self._run("Added login page", "--pr-title")
        failed = {c["rule_id"] for c in data["checks"] if c["status"] == "fail"}
        self.assertEqual(rc, 1)
        self.assertIn("CC001", failed)


@unittest.skipUnless(
    shutil.which("commit-check") and shutil.which("git"), "needs commit-check and git"
)
class TestFilesAgainstRealCommits(unittest.TestCase):
    """The pinned commit-check and git, unmocked, on a small repository."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.join(tmp.name, "repo")
        os.mkdir(self.repo)
        gitconfig = os.path.join(tmp.name, "gitconfig")
        with open(gitconfig, "w", encoding="utf-8") as f:
            f.write(
                "[user]\n\tname = test-author\n\temail = test@example.com\n"
                "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n"
            )
        env = {k: v for k, v in os.environ.items() if not k.startswith("CCHK_")}
        env["GIT_CONFIG_GLOBAL"] = gitconfig
        env["GIT_CONFIG_SYSTEM"] = gitconfig
        env_patch = patch.dict(os.environ, env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.repo)
        self._git("init", "-q")
        self._commit(
            "chore: init", {"cchk.toml": '[files]\nprohibited_patterns = ["*.pem"]\n'}
        )

    def _git(self, *args: str, cwd: str | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        return result.stdout.strip()

    def _commit(self, subject: str, files: dict[str, str | None]) -> str:
        for name, content in files.items():
            if content is None:
                self._git("rm", "-q", name)
                continue
            with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
                f.write(content)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", subject)
        return self._git("rev-parse", "HEAD")

    def test_each_commit_answers_for_the_files_it_changes(self):
        good = self._commit("feat: app", {"app.py": "x = 1\n"})
        bad = self._commit("feat: key", {"key.pem": "k\n"})
        gone = self._commit("fix: drop key", {"key.pem": None})
        with patch("sys.stdout", io.StringIO()):
            results = main.run_files_checks([good, bad, gone])
        self.assertEqual(
            [(s.sha, s.status) for s in results],
            [(good, "pass"), (bad, "fail"), (gone, "skip")],
        )
        self.assertEqual(
            [(c["rule_id"], c["value"]) for c in results[1].failures],
            [("CC303", "key.pem (pattern *.pem)")],
        )
        self.assertEqual(results[2].skip_reason, main.FILES_NOTHING_CHANGED)

    def test_no_rule_set_is_reported_as_skipped(self):
        sha = self._commit("chore: no rules", {"cchk.toml": ""})
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            results = main.run_files_checks([sha])
        self.assertEqual(
            [(s.status, s.skip_reason) for s in results],
            [("skip", main.FILES_UNCONFIGURED)],
        )

    def test_a_shallow_clone_skips_the_commit_at_its_edge(self):
        """Without the guard it would answer for every file in the repository."""
        self._commit("feat: key", {"key.pem": "k\n"})
        tip = self._commit("feat: app", {"app.py": "x = 1\n"})
        shallow = os.path.join(os.path.dirname(self.repo), "shallow")
        # A URL, not a path: git ignores --depth when cloning a local path.
        origin = pathlib.Path(self.repo).as_uri()
        self._git(
            "clone",
            "-q",
            "--depth",
            "1",
            origin,
            shallow,
            cwd=os.path.dirname(self.repo),
        )
        os.chdir(shallow)
        self.assertEqual(main._shallow_boundary(), {tip})
        out = io.StringIO()
        with patch("sys.stdout", out):
            results = main.run_files_checks([tip])
        self.assertEqual(
            [(s.status, s.skip_reason) for s in results],
            [("skip", main.FILES_NOT_IN_CLONE)],
        )
        self.assertIn("fetch-depth: 0", out.getvalue())
