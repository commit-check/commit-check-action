"""The event, the pull request and its commits, read from git or the API."""

import functools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from helpers import SHA_A, SHA_B

from action import context

#: ``git log`` output in COMMIT_LOG_FORMAT for two commits: hash, NUL, message
#: (with its trailing newline), NUL, then git's own newline before the next.
TWO_COMMITS_LOG = f"{SHA_A}\x00fix: first\n\x00\n{SHA_B}\x00feat: second\n\x00"


class TestParseCommitMessages(unittest.TestCase):
    def test_splits_messages_and_trims_surrounding_newlines(self):
        result = context.parse_commit_messages(
            f"{SHA_A}\x00\nfix: first\n\x00\n{SHA_B}\x00\nfeat: second\n\n\x00"
        )
        self.assertEqual(result, [(SHA_A, "fix: first"), (SHA_B, "feat: second")])

    def test_real_git_log_layout(self):
        self.assertEqual(
            context.parse_commit_messages(TWO_COMMITS_LOG),
            [(SHA_A, "fix: first"), (SHA_B, "feat: second")],
        )

    def test_the_format_asks_git_for_the_hash_and_the_body(self):
        """A literal NUL cannot be an argument; git spells it %x00."""
        self.assertEqual(context.COMMIT_LOG_FORMAT, "--pretty=format:%H%x00%B%x00")
        self.assertNotIn("\x00", context.COMMIT_LOG_FORMAT)

    def test_an_empty_message_is_kept_and_does_not_shift_the_hashes(self):
        output = f"{SHA_A}\x00\n\x00\n{SHA_B}\x00feat: second\n\x00"
        self.assertEqual(
            context.parse_commit_messages(output),
            [(SHA_A, ""), (SHA_B, "feat: second")],
        )

    def test_empty_output_is_no_commits(self):
        self.assertEqual(context.parse_commit_messages(""), [])


class TestGetPrTitle(unittest.TestCase):
    def test_non_pr_event_returns_none(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}):
            self.assertIsNone(context.get_pr_title())

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
            self.assertEqual(context.get_pr_title(), "feat: add login page")
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
            self.assertEqual(context.get_pr_title(), "fix: resolve timeout")
        os.unlink(event_path)

    def test_missing_event_path_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ["GITHUB_EVENT_NAME"] = "pull_request"
            os.environ.pop("GITHUB_EVENT_PATH", None)
            self.assertIsNone(context.get_pr_title())

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
            self.assertIsNone(context.get_pr_title())
        os.unlink(event_path)


class TestLocalAuthor(unittest.TestCase):
    """The author of a commit the clone holds, read from git."""

    def test_reads_name_and_email(self):
        result = MagicMock(returncode=0, stdout="Jane Dev\x1fjane@example.com\n")
        with patch("subprocess.run", return_value=result) as mock_run:
            self.assertEqual(
                context._local_author(SHA_A), ("Jane Dev", "jane@example.com")
            )
        self.assertEqual(
            mock_run.call_args[0][0],
            ["git", "show", "-s", "--format=%an%x1f%ae", SHA_A],
        )

    def test_a_commit_git_cannot_show_has_no_author(self):
        result = MagicMock(returncode=128, stdout="")
        with patch("subprocess.run", return_value=result):
            self.assertIsNone(context._local_author(SHA_A))

    def test_output_without_the_separator_has_no_author(self):
        result = MagicMock(returncode=0, stdout="unexpected\n")
        with patch("subprocess.run", return_value=result):
            self.assertIsNone(context._local_author(SHA_A))

    def test_missing_git_has_no_author(self):
        with patch("subprocess.run", side_effect=OSError("no git")):
            self.assertIsNone(context._local_author(SHA_A))

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
                author = context._local_author(sha)
            finally:
                os.chdir(cwd)
        self.assertEqual(author, ("Renovate Bot", "bot@renovateapp.com"))


class TestGetPrCommitMessages(unittest.TestCase):
    def setUp(self):
        # Never the event of whatever CI run the tests happen to be in.
        patcher = patch("action.context.get_pr_event", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_pr_event_returns_empty(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}):
            result = context.get_pr_commit_messages()
        self.assertEqual(result, [])

    def test_event_range_is_preferred(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch(
                "action.context.get_messages_from_event_range",
                return_value=["fix: first", "feat: second"],
            ) as mock_range,
            patch("action.context.get_messages_from_merge_ref") as mock_merge,
            patch("action.context.get_messages_from_head_ref") as mock_head,
        ):
            result = context.get_pr_commit_messages()
        self.assertEqual(result, ["fix: first", "feat: second"])
        mock_range.assert_called_once()
        mock_merge.assert_not_called()
        mock_head.assert_not_called()

    def test_merge_ref_is_next(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("action.context.get_messages_from_event_range", return_value=[]),
            patch(
                "action.context.get_messages_from_merge_ref",
                return_value=["fix: first", "feat: second"],
            ) as mock_merge,
            patch("action.context.get_messages_from_head_ref") as mock_head,
        ):
            result = context.get_pr_commit_messages()
        self.assertEqual(result, ["fix: first", "feat: second"])
        mock_merge.assert_called_once()
        mock_head.assert_not_called()

    def test_pull_request_target_is_supported(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
            patch(
                "action.context.get_messages_from_event_range",
                return_value=["fix: first"],
            ),
        ):
            result = context.get_pr_commit_messages()
        self.assertEqual(result, ["fix: first"])

    def test_a_range_cut_short_by_a_shallow_clone_asks_the_api(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch(
                "action.context.get_messages_from_event_range",
                return_value=[("b2", "fix")],
            ),
            patch("action.context.get_pr_event", return_value={"commits": 2}),
            patch("action.context.get_messages_from_api", return_value=[]) as api,
            patch("action.context.warn_shallow_checkout") as warn,
        ):
            # The API cannot help either: check what the clone had, and say so.
            self.assertEqual(context.get_pr_commit_messages(), [("b2", "fix")])
        api.assert_called_once()
        self.assertIn("Only 1 of the pull request's 2 commits", warn.call_args[0][0])

    def test_asks_the_api_when_the_clone_holds_none_of_them(self):
        with (
            patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": "main"},
            ),
            patch("action.context.get_messages_from_event_range", return_value=[]),
            patch("action.context.get_messages_from_merge_ref", return_value=[]),
            patch("action.context.get_messages_from_head_ref", return_value=[]),
            patch(
                "action.context.get_messages_from_api",
                return_value=[("a1", "feat: one")],
            ),
        ):
            self.assertEqual(context.get_pr_commit_messages(), [("a1", "feat: one")])

    def test_nothing_listed_anywhere_is_empty_and_not_warned_here(self):
        """With no partial list there is no "only N of M" to report; the
        caller warns that it fell back to HEAD."""
        with (
            patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": ""},
            ),
            patch("action.context.get_messages_from_event_range", return_value=[]),
            patch("action.context.get_messages_from_merge_ref", return_value=[]),
            patch("action.context.get_messages_from_head_ref") as mock_head,
            patch("action.context.get_messages_from_api", return_value=[]) as api,
            patch("action.context.warn_shallow_checkout") as warn,
        ):
            self.assertEqual(context.get_pr_commit_messages(), [])
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
            patch("action.context.get_messages_from_event_range", return_value=[]),
            patch("action.context.get_messages_from_merge_ref", return_value=[]),
            patch(
                "action.context.get_messages_from_head_ref",
                return_value=["fix: first", "feat: second"],
            ) as mock_head,
        ):
            result = context.get_pr_commit_messages()
        self.assertEqual(result, ["fix: first", "feat: second"])
        mock_head.assert_called_once_with("main")

    def test_exception_returns_empty(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch(
                "action.context.get_messages_from_event_range",
                side_effect=Exception("git failed"),
            ),
        ):
            result = context.get_pr_commit_messages()
        self.assertEqual(result, [])


class TestGitMessageReaders(unittest.TestCase):
    def test_get_messages_from_merge_ref(self):
        mock_result = MagicMock(returncode=0, stdout=TWO_COMMITS_LOG)
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("subprocess.run", return_value=mock_result) as mock_run,
        ):
            result = context.get_messages_from_merge_ref()
        self.assertEqual(result, [(SHA_A, "fix: first"), (SHA_B, "feat: second")])
        self.assertEqual(
            mock_run.call_args[0][0],
            ["git", "log", context.COMMIT_LOG_FORMAT, "--reverse", "HEAD^1..HEAD^2"],
        )

    def test_merge_ref_is_never_read_on_pull_request_target(self):
        """HEAD is the base branch there; HEAD^2 belongs to some other merge."""
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
            patch("subprocess.run") as mock_run,
        ):
            self.assertEqual(context.get_messages_from_merge_ref(), [])
        mock_run.assert_not_called()

    def test_get_messages_from_event_range(self):
        commands: list[list[str]] = []

        def run(command, **_kwargs):
            commands.append(command)
            if command[:2] == ["git", "rev-parse"]:
                return MagicMock(returncode=0, stdout="x\n")
            return MagicMock(returncode=0, stdout=TWO_COMMITS_LOG)

        with (
            patch("action.context.get_pr_base_sha", return_value="base111"),
            patch("action.context.get_pr_head_sha", return_value="head222"),
            patch("subprocess.run", side_effect=run),
        ):
            result = context.get_messages_from_event_range()
        self.assertEqual(result, [(SHA_A, "fix: first"), (SHA_B, "feat: second")])
        self.assertIn(
            ["git", "log", context.COMMIT_LOG_FORMAT, "--reverse", "base111..head222"],
            commands,
        )

    def test_event_range_needs_both_commits_in_the_clone(self):
        with (
            patch("action.context.get_pr_base_sha", return_value="base111"),
            patch("action.context.get_pr_head_sha", return_value="head222"),
            patch("subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            self.assertEqual(context.get_messages_from_event_range(), [])

    def test_event_range_without_a_payload_is_empty(self):
        with (
            patch("action.context.get_pr_base_sha", return_value=None),
            patch("action.context.get_pr_head_sha", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            self.assertEqual(context.get_messages_from_event_range(), [])
        mock_run.assert_not_called()

    def test_a_failing_git_log_yields_no_messages(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=128)):
            self.assertEqual(context.get_messages_from_head_ref("main"), [])

    def test_get_messages_from_head_ref(self):
        mock_result = MagicMock(returncode=0, stdout=f"{SHA_A}\x00fix: first\n\x00")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = context.get_messages_from_head_ref("main")
        self.assertEqual(result, [(SHA_A, "fix: first")])
        self.assertEqual(
            mock_run.call_args[0][0],
            [
                "git",
                "log",
                context.COMMIT_LOG_FORMAT,
                "--reverse",
                "origin/main..HEAD",
            ],
        )


class TestHeadSha(unittest.TestCase):
    def test_returns_the_full_hash(self):
        mock_result = MagicMock(returncode=0, stdout=f"{SHA_A}\n")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            self.assertEqual(context.head_sha(), SHA_A)
        self.assertEqual(mock_run.call_args[0][0], ["git", "rev-parse", "HEAD"])

    def test_no_head_is_empty(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=128)):
            self.assertEqual(context.head_sha(), "")

    def test_missing_git_is_empty(self):
        with patch("subprocess.run", side_effect=OSError("no git")):
            self.assertEqual(context.head_sha(), "")


class TestPrHeadRev(unittest.TestCase):
    def test_payload_head_sha_wins_when_the_clone_has_it(self):
        with (
            patch("action.context.get_pr_head_sha", return_value="abc123"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
        ):
            self.assertEqual(context.pr_head_rev(), "abc123")
        self.assertEqual(
            mock_run.call_args[0][0],
            ["git", "rev-parse", "--verify", "--quiet", "abc123^{commit}"],
        )

    def test_pull_request_falls_back_to_head2(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("action.context.get_pr_head_sha", return_value=None),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
        ):
            self.assertEqual(context.pr_head_rev(), "HEAD^2")
        self.assertEqual(
            mock_run.call_args[0][0],
            ["git", "rev-parse", "--verify", "--quiet", "HEAD^2^{commit}"],
        )

    def test_pull_request_target_does_not_fall_back_to_head2(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
            patch("action.context.get_pr_head_sha", return_value=None),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
        ):
            self.assertIsNone(context.pr_head_rev())
        mock_run.assert_not_called()

    def test_unfetched_payload_sha_on_pull_request_target_returns_none(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}),
            patch("action.context.get_pr_head_sha", return_value="abc123"),
            patch("subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            self.assertIsNone(context.pr_head_rev())

    def test_shallow_clone_returns_none(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("action.context.get_pr_head_sha", return_value=None),
            patch("subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            self.assertIsNone(context.pr_head_rev())

    def test_missing_git_returns_none(self):
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}),
            patch("action.context.get_pr_head_sha", return_value=None),
            patch("subprocess.run", side_effect=OSError("no git")),
        ):
            self.assertIsNone(context.pr_head_rev())


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
            patch("action.context.get_pr_head_sha", return_value="abc123"),
            patch("subprocess.run", run),
        ):
            self.assertEqual(context.pr_head_rev(), "abc123")
        self.assertEqual(
            run.call_args_list[1][0][0],
            ["git", "fetch", "--no-tags", "--quiet", "--depth=1", "origin", "abc123"],
        )

    def test_a_fetch_without_git_gives_up(self):
        with patch("subprocess.run", side_effect=OSError("no git")):
            self.assertFalse(context._fetch_commit("abc123"))


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
            patch(
                "action.context.get_pr_event",
                return_value={"number": 7, "commits": total},
            ),
            patch("action.context.get_pr_head_sha", return_value=head),
            patch.dict(sys.modules, {"github": github_module}),
        ):
            return context.get_messages_from_api(), github_module

    def test_lists_the_commits_oldest_first(self):
        commits = [("a1", "feat: one"), ("b2", "fix: two")]
        result, github_module = self._run(2, commits)
        self.assertEqual(result, commits)
        self.assertEqual(
            github_module.Github.call_args[1]["base_url"],
            "https://ghe.example.com/api/v3",
        )

    def test_records_each_commit_author(self):
        context.API_AUTHORS.clear()
        self.addCleanup(context.API_AUTHORS.clear)
        self._run(2, [("a1", "feat: one"), ("b2", "fix: two")])
        self.assertEqual(
            context.API_AUTHORS,
            {
                "a1": ("author a1", "a1@example.com"),
                "b2": ("author b2", "b2@example.com"),
            },
        )

    def test_a_commit_without_an_author_records_nothing(self):
        context.API_AUTHORS.clear()
        self.addCleanup(context.API_AUTHORS.clear)
        pull = MagicMock()
        pull.get_commits.return_value = [
            MagicMock(sha="b2", commit=MagicMock(message="fix: two", author=None))
        ]
        github_module = MagicMock()
        github_module.Github.return_value.get_repo.return_value.get_pull.return_value = pull
        env = {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "owner/repo"}
        with (
            patch.dict(os.environ, env),
            patch(
                "action.context.get_pr_event", return_value={"number": 7, "commits": 1}
            ),
            patch("action.context.get_pr_head_sha", return_value="b2"),
            patch.dict(sys.modules, {"github": github_module}),
        ):
            self.assertEqual(context.get_messages_from_api(), [("b2", "fix: two")])
        self.assertEqual(context.API_AUTHORS, {})

    def test_a_list_that_ends_elsewhere_is_not_used(self):
        # A force push after the event: same count, other commits.
        result, _ = self._run(2, [("a1", "feat: one"), ("c3", "fix: other")])
        self.assertEqual(result, [])

    def test_a_partial_list_is_not_used(self):
        result, _ = self._run(3, [("a1", "feat: one"), ("b2", "fix: two")])
        self.assertEqual(result, [])

    def test_more_than_the_api_returns_is_left_unlisted(self):
        result, github_module = self._run(context.API_COMMIT_LIMIT + 1, [])
        self.assertEqual(result, [])
        github_module.Github.assert_not_called()


class TestCheckoutHint(unittest.TestCase):
    def test_pull_request_names_fetch_depth(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            self.assertEqual(context.checkout_hint(), context.SHALLOW_CHECKOUT_HINT)

    def test_pull_request_target_names_the_checkout(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}):
            self.assertEqual(context.checkout_hint(), context.TARGET_CHECKOUT_HINT)
            self.assertIn("refs/pull/<number>/merge", context.checkout_hint())


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
                self.assertEqual(context.get_pr_head_sha(), "abc123")
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
                self.assertEqual(context.get_pr_base_sha(), "base111")
                self.assertIsNone(context.get_pr_head_sha())
        finally:
            os.unlink(event_path)

    def test_not_a_pr_event_returns_none(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}):
            self.assertIsNone(context.get_pr_head_sha())
            self.assertIsNone(context.get_pr_base_sha())

    def test_missing_event_path_returns_none(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            os.environ.pop("GITHUB_EVENT_PATH", None)
            self.assertIsNone(context.get_pr_head_sha())

    def test_unreadable_event_returns_none(self):
        with patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_EVENT_PATH": "/nonexistent.json",
            },
        ):
            self.assertIsNone(context.get_pr_head_sha())


def _pr_event(pull_request: dict) -> str:
    """Write a pull_request event payload and return its path."""
    path = os.path.join(tempfile.mkdtemp(), "event.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pull_request": pull_request}, f)
    return path


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
            self.assertEqual(context.get_pr_body(), "Adds a parser.")

    def test_no_description_is_none(self):
        for body in (None, "", " \n "):
            with self.subTest(body=body), self._env({"body": body}):
                self.assertIsNone(context.get_pr_body())

    def test_an_agent_app_opened_it(self):
        with self._env({"user": {"login": "Copilot-SWE-Agent[bot]"}}):
            self.assertEqual(context.get_pr_opener_tool(), "GitHub Copilot")

    def test_a_person_or_another_bot_opened_it(self):
        for user in ({"login": "octocat"}, {"login": "dependabot[bot]"}, None, {}):
            with self.subTest(user=user), self._env({"user": user}):
                self.assertIsNone(context.get_pr_opener_tool())
