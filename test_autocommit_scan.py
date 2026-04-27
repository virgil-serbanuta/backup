#!/usr/bin/env python3
"""Tests for autocommit_scan.py.

Each test that touches git creates its own repository tree under pytest's
tmp_path so tests are fully isolated and leave no side effects.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

# Make sure the module is importable when running from the project root.
sys.path.insert(0, str(Path(__file__).parent))

import autocommit_scan
from autocommit_scan import (
    AutocommitError,
    RepoConfig,
    cap_by_max_files,
    commit_and_maybe_push,
    current_branch,
    discover_repositories,
    ensure_nested_repos_ignored,
    ensure_root_repo,
    filter_by_max_size,
    format_examples,
    load_repo_config,
    process_repository,
    tracked_changed_files,
    untracked_files,
)

# ---------------------------------------------------------------------------
# Git / repo helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, text=True, capture_output=True,
    )


def make_repo(path: Path, branch: str = "master") -> Path:
    """Create a git repo with one initial commit so HEAD exists."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "commit.gpgsign", "false")
    (path / ".gitkeep").write_text("")
    _git(path, "add", ".gitkeep")
    _git(path, "commit", "-m", "init")
    return path


def write_autocommit_yaml(
    repo: Path,
    *,
    commit_branch: str = "master",
    commit_type: str = "changed",
    max_size: int = 10 ** 9,
    max_files: int = 1000,
    push: bool = False,
    auto_add: list[str] | str | None = None,
) -> None:
    cfg: dict[str, object] = {
        "commit type": commit_type,
        "commit branch": commit_branch,
        "max-size": max_size,
        "max-files": max_files,
        "push": push,
    }
    if auto_add is not None:
        cfg["auto-add"] = auto_add
    (repo / ".autocommit.yaml").write_text(yaml.dump(cfg))


def make_cfg(
    commit_branch: str = "master",
    commit_type: str = "changed",
    max_size: int = 10 ** 9,
    max_files: int = 1000,
    push: bool = False,
) -> RepoConfig:
    return RepoConfig(
        commit_type=commit_type,
        commit_branch=commit_branch,
        max_size=max_size,
        max_files=max_files,
        push=push,
    )


def staged_files(repo: Path) -> list[str]:
    """Return names of files that have staged changes (X column non-space/?)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, text=True, capture_output=True,
    )
    result = []
    for line in proc.stdout.splitlines():
        if line and line[0] not in (" ", "?"):
            result.append(line[3:].strip())
    return result


def backup_branches(repo: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "backup/*"],
        check=True, text=True, capture_output=True,
    )
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def stash_count(repo: Path) -> int:
    proc = subprocess.run(
        ["git", "-C", str(repo), "stash", "list"],
        check=True, text=True, capture_output=True,
    )
    return len([l for l in proc.stdout.splitlines() if l.strip()])


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------

class TestFormatExamples:
    def test_empty(self):
        assert format_examples([]) == ""

    def test_few_items(self):
        assert format_examples(["a", "b"]) == "\n  a\n  b"

    def test_exactly_max(self):
        result = format_examples(["a"] * 5)
        assert "..." not in result

    def test_truncated_at_default(self):
        result = format_examples(["a"] * 6)
        assert "..." in result
        assert result.count("\n  a") == 5

    def test_custom_max_items(self):
        result = format_examples(["x", "y", "z"], max_items=2)
        assert "..." in result
        assert "z" not in result


class TestCapByMaxFiles:
    def test_zero_keeps_nothing(self):
        kept, over = cap_by_max_files(["a", "b"], 0)
        assert kept == []
        assert over == ["a", "b"]

    def test_within_limit(self):
        kept, over = cap_by_max_files(["a", "b"], 10)
        assert kept == ["a", "b"]
        assert over == []

    def test_at_exact_limit(self):
        kept, over = cap_by_max_files(["a", "b"], 2)
        assert kept == ["a", "b"]
        assert over == []

    def test_over_limit(self):
        kept, over = cap_by_max_files(["a", "b", "c"], 2)
        assert kept == ["a", "b"]
        assert over == ["c"]


class TestStatusLines:
    def test_paths_with_spaces_returned_unquoted(self, tmp_path):
        repo = make_repo(tmp_path)
        sub = repo / "Profile 2" / "Sessions"
        sub.mkdir(parents=True)
        (sub / "Session_42").write_text("x")
        lines = autocommit_scan.status_lines(repo)
        assert lines == ["?? Profile 2/Sessions/Session_42"]

    def test_handles_renames(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / "old name.txt").write_text("x")
        _git(repo, "add", "old name.txt")
        _git(repo, "commit", "-m", "add")
        _git(repo, "mv", "old name.txt", "new name.txt")
        lines = autocommit_scan.status_lines(repo)
        # Rename emitted as a single entry with the destination path
        assert len(lines) == 1
        assert lines[0].endswith("new name.txt")


class TestStatusLineParsers:
    def test_tracked_changed_skips_untracked(self):
        lines = ["?? new.txt", " M modified.txt"]
        assert tracked_changed_files(Path("."), lines) == ["modified.txt"]

    def test_tracked_changed_follows_rename(self):
        lines = ["R  old.txt -> new.txt"]
        assert tracked_changed_files(Path("."), lines) == ["new.txt"]

    def test_tracked_changed_deduplicates(self):
        lines = [" M a.txt", "M  a.txt"]
        assert tracked_changed_files(Path("."), lines) == ["a.txt"]

    def test_untracked_skips_tracked(self):
        lines = ["?? foo.txt", " M bar.txt"]
        assert untracked_files(lines) == ["foo.txt"]

    def test_untracked_deduplicates(self):
        lines = ["?? foo.txt", "?? foo.txt"]
        assert untracked_files(lines) == ["foo.txt"]


# ---------------------------------------------------------------------------
# ensure_root_repo
# ---------------------------------------------------------------------------

class TestEnsureRootRepo:
    def test_valid_root(self, tmp_path):
        _git(tmp_path, "init")
        assert ensure_root_repo(tmp_path) == tmp_path.resolve()

    def test_not_a_git_repo(self, tmp_path):
        with pytest.raises(AutocommitError, match="not a git repository"):
            ensure_root_repo(tmp_path)

    def test_subdir_of_repo(self, tmp_path):
        _git(tmp_path, "init")
        subdir = tmp_path / "sub"
        subdir.mkdir()
        with pytest.raises(AutocommitError, match="not its root"):
            ensure_root_repo(subdir)


# ---------------------------------------------------------------------------
# discover_repositories
# ---------------------------------------------------------------------------

class TestDiscoverRepositories:
    def test_single_repo(self, tmp_path):
        make_repo(tmp_path)
        assert discover_repositories(tmp_path) == [tmp_path.resolve()]

    def test_nested_repo_discovered(self, tmp_path):
        make_repo(tmp_path)
        nested = tmp_path / "child"
        make_repo(nested)
        repos = discover_repositories(tmp_path)
        assert tmp_path.resolve() in repos
        assert nested.resolve() in repos
        assert len(repos) == 2

    def test_does_not_descend_into_git_internals(self, tmp_path):
        make_repo(tmp_path)
        repos = discover_repositories(tmp_path)
        assert all(".git" not in str(r) for r in repos)

    def test_deeply_nested(self, tmp_path):
        make_repo(tmp_path)
        deep = tmp_path / "a" / "b" / "c"
        make_repo(deep)
        repos = discover_repositories(tmp_path)
        assert deep.resolve() in repos


# ---------------------------------------------------------------------------
# ensure_nested_repos_ignored
# ---------------------------------------------------------------------------

def add_submodule(parent: Path, sub_src: Path, rel_name: str) -> Path:
    """Register sub_src as a submodule of parent at rel_name and commit."""
    # -c protocol.file.allow=always is required on git ≥ 2.38.1 which blocks
    # local file:// transport for submodule cloning by default.
    subprocess.run(
        ["git", "-C", str(parent), "-c", "protocol.file.allow=always",
         "submodule", "add", str(sub_src), rel_name],
        check=True, capture_output=True, text=True,
    )
    _git(parent, "commit", "-m", f"add submodule {rel_name}")
    return parent / rel_name


class TestEnsureNestedReposIgnored:
    def test_ignored_nested_repo_passes(self, tmp_path):
        root = make_repo(tmp_path / "root")
        nested = make_repo(root / "nested")
        (root / ".gitignore").write_text("nested/\n")
        _git(root, "add", ".gitignore")
        _git(root, "commit", "-m", "add gitignore")
        ensure_nested_repos_ignored(root, [root, nested])  # must not raise

    def test_non_ignored_nested_repo_raises(self, tmp_path):
        root = make_repo(tmp_path / "root")
        nested = make_repo(root / "nested")
        with pytest.raises(AutocommitError, match="not ignored"):
            ensure_nested_repos_ignored(root, [root, nested])

    def test_root_alone_always_passes(self, tmp_path):
        root = make_repo(tmp_path)
        ensure_nested_repos_ignored(root, [root])  # nothing to check

    def test_submodule_passes_without_gitignore(self, tmp_path):
        root = make_repo(tmp_path / "root")
        sub_src = make_repo(tmp_path / "sub_src")
        sub = add_submodule(root, sub_src, "sub")
        ensure_nested_repos_ignored(root, [root, sub])  # must not raise

    def test_non_ignored_non_submodule_still_raises(self, tmp_path):
        root = make_repo(tmp_path / "root")
        nested = make_repo(root / "nested")
        with pytest.raises(AutocommitError, match="not ignored"):
            ensure_nested_repos_ignored(root, [root, nested])


# ---------------------------------------------------------------------------
# load_repo_config
# ---------------------------------------------------------------------------

class TestLoadRepoConfig:
    def test_valid_config(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, commit_branch="master", commit_type="all",
                              max_size=5000, max_files=50, push=True)
        cfg = load_repo_config(repo)
        assert cfg.commit_type == "all"
        assert cfg.commit_branch == "master"
        assert cfg.max_size == 5000
        assert cfg.max_files == 50
        assert cfg.push is True

    def test_hash_new_branch_is_valid(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, commit_branch="#new")
        cfg = load_repo_config(repo)
        assert cfg.commit_branch == "#new"

    def test_missing_file_raises(self, tmp_path):
        repo = make_repo(tmp_path)
        with pytest.raises(AutocommitError, match="Missing"):
            load_repo_config(repo)

    def test_invalid_commit_type_raises(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / ".autocommit.yaml").write_text(
            "commit type: wrong\ncommit branch: master\n"
            "max-size: 0\nmax-files: 0\npush: false\n"
        )
        with pytest.raises(AutocommitError, match="commit type"):
            load_repo_config(repo)

    def test_invalid_branch_name_raises(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / ".autocommit.yaml").write_text(
            "commit type: changed\ncommit branch: 'bad branch'\n"
            "max-size: 0\nmax-files: 0\npush: false\n"
        )
        with pytest.raises(AutocommitError, match="Invalid git branch"):
            load_repo_config(repo)

    def test_negative_max_size_raises(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / ".autocommit.yaml").write_text(
            "commit type: changed\ncommit branch: master\n"
            "max-size: -1\nmax-files: 0\npush: false\n"
        )
        with pytest.raises(AutocommitError, match="max-size"):
            load_repo_config(repo)

    def test_push_must_be_bool(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / ".autocommit.yaml").write_text(
            "commit type: changed\ncommit branch: master\n"
            "max-size: 0\nmax-files: 0\npush: yes-please\n"
        )
        with pytest.raises(AutocommitError, match="push"):
            load_repo_config(repo)

    def test_auto_add_default_empty(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo)
        assert load_repo_config(repo).auto_add == []

    def test_auto_add_string_accepted(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, auto_add="*.log")
        assert load_repo_config(repo).auto_add == ["*.log"]

    def test_auto_add_list_accepted(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, auto_add=["*.log", "build/*.json"])
        assert load_repo_config(repo).auto_add == ["*.log", "build/*.json"]

    def test_auto_add_empty_string_raises(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, auto_add="")
        with pytest.raises(AutocommitError, match="auto-add"):
            load_repo_config(repo)

    def test_auto_add_empty_item_in_list_raises(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, auto_add=["*.log", ""])
        with pytest.raises(AutocommitError, match="auto-add"):
            load_repo_config(repo)

    def test_auto_add_invalid_type_raises(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / ".autocommit.yaml").write_text(
            "commit type: changed\ncommit branch: master\n"
            "max-size: 0\nmax-files: 0\npush: false\nauto-add: 42\n"
        )
        with pytest.raises(AutocommitError, match="auto-add"):
            load_repo_config(repo)


# ---------------------------------------------------------------------------
# filter_by_max_size
# ---------------------------------------------------------------------------

class TestFilterByMaxSize:
    def test_small_file_kept(self, tmp_path):
        (tmp_path / "small.txt").write_text("x")
        kept, large = filter_by_max_size(tmp_path, ["small.txt"], max_size=10)
        assert kept == ["small.txt"]
        assert large == []

    def test_large_file_excluded(self, tmp_path):
        (tmp_path / "big.txt").write_bytes(b"x" * 100)
        kept, large = filter_by_max_size(tmp_path, ["big.txt"], max_size=10)
        assert kept == []
        assert large == ["big.txt"]

    def test_missing_file_not_filtered(self, tmp_path):
        # A deleted tracked file (not on disk) must still appear in the commit
        kept, large = filter_by_max_size(tmp_path, ["gone.txt"], max_size=1)
        assert kept == ["gone.txt"]
        assert large == []

    def test_exactly_at_limit_is_kept(self, tmp_path):
        (tmp_path / "f.txt").write_bytes(b"x" * 10)
        kept, large = filter_by_max_size(tmp_path, ["f.txt"], max_size=10)
        assert kept == ["f.txt"]


# ---------------------------------------------------------------------------
# commit_and_maybe_push — normal (named) branch
# ---------------------------------------------------------------------------

class TestCommitNormalBranch:
    def test_commits_tracked_modification(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / "file.txt").write_text("v1\n")
        _git(repo, "add", "file.txt")
        _git(repo, "commit", "-m", "add file")
        (repo / "file.txt").write_text("v2\n")

        commit_and_maybe_push(repo, make_cfg(), ["file.txt"])

        log = _git(repo, "log", "--oneline").stdout
        assert "chore(backup)" in log
        assert current_branch(repo) == "master"

    def test_commits_new_untracked_file(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / "new.txt").write_text("brand new\n")
        commit_and_maybe_push(repo, make_cfg(), ["new.txt"])
        log = _git(repo, "log", "--oneline").stdout
        assert "chore(backup)" in log

    def test_wrong_branch_raises(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / "f.txt").write_text("x")
        with pytest.raises(AutocommitError, match="expected 'other'"):
            commit_and_maybe_push(repo, make_cfg(commit_branch="other"), ["f.txt"])

    def test_empty_file_list_skips_commit(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        commit_and_maybe_push(repo, make_cfg(), [])
        assert "No files left" in capsys.readouterr().out
        log = _git(repo, "log", "--oneline").stdout
        assert "chore(backup)" not in log

    def test_unstages_files_on_commit_error(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path)
        (repo / "file.txt").write_text("change\n")

        real = autocommit_scan.run_git
        def failing(r, args, **kw):
            if args[0] == "commit":
                raise AutocommitError("injected failure", repo=r)
            return real(r, args, **kw)
        monkeypatch.setattr(autocommit_scan, "run_git", failing)

        with pytest.raises(AutocommitError, match="injected failure"):
            commit_and_maybe_push(repo, make_cfg(), ["file.txt"])

        assert staged_files(repo) == []
        assert current_branch(repo) == "master"
        assert (repo / "file.txt").exists()


# ---------------------------------------------------------------------------
# commit_and_maybe_push — #new branch
# ---------------------------------------------------------------------------

class TestCommitNewBranch:
    def test_creates_backup_branch(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / "file.txt").write_text("data\n")
        commit_and_maybe_push(repo, make_cfg(commit_branch="#new"), ["file.txt"])
        branches = backup_branches(repo)
        assert len(branches) == 1
        assert branches[0].startswith("backup/")

    def test_returns_to_original_branch(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / "file.txt").write_text("data\n")
        commit_and_maybe_push(repo, make_cfg(commit_branch="#new"), ["file.txt"])
        assert current_branch(repo) == "master"

    def test_restores_untracked_file_after_success(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / "file.txt").write_text("data\n")
        commit_and_maybe_push(repo, make_cfg(commit_branch="#new"), ["file.txt"])
        # The file must be present and untracked on master again
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True, text=True, capture_output=True,
        ).stdout
        assert "file.txt" in status
        assert staged_files(repo) == []
        assert stash_count(repo) == 0

    def test_restores_tracked_modification_after_success(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / "file.txt").write_text("original\n")
        _git(repo, "add", "file.txt")
        _git(repo, "commit", "-m", "add file")
        (repo / "file.txt").write_text("modified\n")

        commit_and_maybe_push(repo, make_cfg(commit_branch="#new"), ["file.txt"])

        assert current_branch(repo) == "master"
        assert (repo / "file.txt").read_text() == "modified\n"
        assert stash_count(repo) == 0

    def test_skips_if_same_as_previous_backup(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        (repo / "file.txt").write_text("data\n")
        cfg = make_cfg(commit_branch="#new")
        commit_and_maybe_push(repo, cfg, ["file.txt"])
        capsys.readouterr()

        commit_and_maybe_push(repo, cfg, ["file.txt"])

        assert "Skipping backup" in capsys.readouterr().out
        assert len(backup_branches(repo)) == 1

    def test_creates_second_branch_when_content_differs(self, tmp_path):
        repo = make_repo(tmp_path)
        cfg = make_cfg(commit_branch="#new")
        (repo / "file.txt").write_text("v1\n")
        commit_and_maybe_push(repo, cfg, ["file.txt"])
        time.sleep(1.1)  # branch names are second-granular; avoid collision
        (repo / "file.txt").write_text("v2\n")
        commit_and_maybe_push(repo, cfg, ["file.txt"])
        assert len(backup_branches(repo)) == 2

    def test_restores_state_on_commit_error(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path)
        (repo / "file.txt").write_text("data\n")

        real = autocommit_scan.run_git
        def failing(r, args, **kw):
            if args[0] == "commit":
                raise AutocommitError("injected commit failure", repo=r)
            return real(r, args, **kw)
        monkeypatch.setattr(autocommit_scan, "run_git", failing)

        with pytest.raises(AutocommitError, match="injected commit failure"):
            commit_and_maybe_push(repo, make_cfg(commit_branch="#new"), ["file.txt"])

        assert current_branch(repo) == "master"
        assert backup_branches(repo) == []
        assert staged_files(repo) == []
        assert (repo / "file.txt").read_text() == "data\n"
        assert stash_count(repo) == 0

    def test_restores_state_on_checkout_error(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path)
        (repo / "file.txt").write_text("data\n")

        real = autocommit_scan.run_git
        def failing(r, args, **kw):
            if args[0] == "checkout" and "-b" in args:
                raise AutocommitError("injected checkout failure", repo=r)
            return real(r, args, **kw)
        monkeypatch.setattr(autocommit_scan, "run_git", failing)

        with pytest.raises(AutocommitError, match="injected checkout failure"):
            commit_and_maybe_push(repo, make_cfg(commit_branch="#new"), ["file.txt"])

        assert current_branch(repo) == "master"
        assert backup_branches(repo) == []
        assert staged_files(repo) == []
        assert (repo / "file.txt").read_text() == "data\n"
        assert stash_count(repo) == 0

    def test_preserves_user_staged_files(self, tmp_path):
        repo = make_repo(tmp_path)
        # Pre-existing tracked file
        (repo / "dirty.txt").write_text("v1\n")
        _git(repo, "add", "dirty.txt")
        _git(repo, "commit", "-m", "add dirty.txt")
        # Modify dirty.txt (will be picked up by backup) and stage a new file
        (repo / "dirty.txt").write_text("v2\n")
        (repo / "user_staged.txt").write_text("staged by user\n")
        _git(repo, "add", "user_staged.txt")
        assert "user_staged.txt" in staged_files(repo)

        commit_and_maybe_push(repo, make_cfg(commit_branch="#new"), ["dirty.txt"])

        # User's staged file remains staged in the main worktree.
        assert "user_staged.txt" in staged_files(repo)
        # User's other file kept its working-tree modification.
        assert (repo / "dirty.txt").read_text() == "v2\n"
        # Backup branch was created with the dirty file's content.
        assert len(backup_branches(repo)) == 1
        assert current_branch(repo) == "master"
        assert stash_count(repo) == 0

    def test_does_not_modify_main_worktree_files(self, tmp_path):
        """Worktree approach: main repo's HEAD/index/working tree never change."""
        repo = make_repo(tmp_path)
        (repo / "file.txt").write_text("data\n")
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        status_before = _git(repo, "status", "--porcelain").stdout

        commit_and_maybe_push(repo, make_cfg(commit_branch="#new"), ["file.txt"])

        head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()
        status_after = _git(repo, "status", "--porcelain").stdout
        assert head_before == head_after
        assert status_before == status_after


# ---------------------------------------------------------------------------
# process_repository
# ---------------------------------------------------------------------------

class TestProcessRepository:
    def test_skips_if_no_dirty_files(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        process_repository(repo, [], [])
        assert capsys.readouterr().out == ""

    def test_skips_if_no_config_file(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        process_repository(repo, ["file.txt"], [])
        assert "no .autocommit.yaml marker" in capsys.readouterr().out

    def test_commit_type_changed_ignores_untracked(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, commit_type="changed")
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "add config")
        (repo / "untracked.txt").write_text("new\n")

        process_repository(repo, [], ["untracked.txt"])

        log = _git(repo, "log", "--oneline").stdout
        assert "chore(backup)" not in log

    def test_commit_type_all_includes_untracked(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, commit_type="all")
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "add config")
        (repo / "untracked.txt").write_text("new\n")

        process_repository(repo, [], ["untracked.txt"])

        log = _git(repo, "log", "--oneline").stdout
        assert "chore(backup)" in log

    def test_max_size_filters_large_file(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, commit_type="changed", max_size=5)
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "add config")
        (repo / "big.txt").write_bytes(b"x" * 100)
        _git(repo, "add", "big.txt")
        _git(repo, "commit", "-m", "add big")
        (repo / "big.txt").write_bytes(b"y" * 100)

        process_repository(repo, ["big.txt"], [])

        assert "max-size" in capsys.readouterr().out
        log = _git(repo, "log", "--oneline").stdout
        assert "chore(backup)" not in log

    def test_max_files_caps_commit(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, commit_type="changed", max_files=1)
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "add config")
        for i in range(3):
            f = repo / f"file{i}.txt"
            f.write_text("v1")
            _git(repo, "add", str(f))
            _git(repo, "commit", "-m", f"add {f.name}")
            f.write_text("v2")

        process_repository(repo, ["file0.txt", "file1.txt", "file2.txt"], [])

        assert "max-files" in capsys.readouterr().out
        # Only one file committed; the other two remain modified
        log = _git(repo, "log", "--oneline").stdout
        assert log.count("chore(backup)") == 1

    def test_warns_on_untracked_with_commit_type_changed(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, commit_type="changed")
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "add config")
        (repo / "new1.txt").write_text("a")
        (repo / "new2.txt").write_text("b")

        warned = process_repository(repo, [], ["new1.txt", "new2.txt"])

        assert warned is True
        out = capsys.readouterr().out
        assert "commit type is 'changed'" in out
        assert "new1.txt" in out
        assert "new2.txt" in out

    def test_no_warning_on_untracked_with_commit_type_all(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, commit_type="all")
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "add config")
        (repo / "new.txt").write_text("a")

        warned = process_repository(repo, [], ["new.txt"])

        assert warned is False
        assert "commit type is 'changed'" not in capsys.readouterr().out

    def test_returns_true_when_no_marker(self, tmp_path):
        repo = make_repo(tmp_path)
        (repo / "file.txt").write_text("x")
        assert process_repository(repo, [], ["file.txt"]) is True

    def test_returns_false_on_clean_commit(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, commit_type="changed")
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "add config")
        (repo / "data.txt").write_text("v1")
        _git(repo, "add", "data.txt")
        _git(repo, "commit", "-m", "add data")
        (repo / "data.txt").write_text("v2")

        assert process_repository(repo, ["data.txt"], []) is False

    def test_auto_add_includes_matching_untracked(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(
            repo, commit_type="changed", commit_branch="#new", auto_add="*.log"
        )
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "config")
        (repo / "app.log").write_text("entry\n")

        warned = process_repository(repo, [], ["app.log"])

        assert warned is False
        branches = backup_branches(repo)
        assert len(branches) == 1
        files = _git(repo, "ls-tree", "-r", "--name-only", branches[0]).stdout.splitlines()
        assert "app.log" in files

    def test_auto_add_skips_non_matching_and_warns(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(
            repo, commit_type="changed", commit_branch="#new", auto_add="*.log"
        )
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "config")
        (repo / "app.log").write_text("entry\n")
        (repo / "data.json").write_text("data\n")

        warned = process_repository(repo, [], ["app.log", "data.json"])

        assert warned is True
        out = capsys.readouterr().out
        assert "data.json" in out
        # The matched file should not appear in the "will not be committed" warning
        warning_block = out.split("will not be committed", 1)[1]
        assert "app.log" not in warning_block
        branches = backup_branches(repo)
        files = _git(repo, "ls-tree", "-r", "--name-only", branches[0]).stdout.splitlines()
        assert "app.log" in files
        assert "data.json" not in files

    def test_auto_add_matches_nested_paths(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(
            repo, commit_type="changed", commit_branch="#new", auto_add="*.log"
        )
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "config")
        (repo / "logs").mkdir()
        (repo / "logs" / "app.log").write_text("entry\n")

        warned = process_repository(repo, [], ["logs/app.log"])

        assert warned is False
        branches = backup_branches(repo)
        files = _git(repo, "ls-tree", "-r", "--name-only", branches[0]).stdout.splitlines()
        assert "logs/app.log" in files

    def test_auto_add_list_of_patterns(self, tmp_path):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(
            repo,
            commit_type="changed",
            commit_branch="#new",
            auto_add=["*.log", "build/*.json"],
        )
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "config")
        (repo / "app.log").write_text("entry\n")
        (repo / "build").mkdir()
        (repo / "build" / "out.json").write_text("{}\n")
        (repo / "other.txt").write_text("nope\n")

        warned = process_repository(
            repo, [], ["app.log", "build/out.json", "other.txt"]
        )

        assert warned is True  # other.txt warns
        branches = backup_branches(repo)
        files = _git(repo, "ls-tree", "-r", "--name-only", branches[0]).stdout.splitlines()
        assert "app.log" in files
        assert "build/out.json" in files
        assert "other.txt" not in files


# ---------------------------------------------------------------------------
# main() — end-to-end via direct call (mocking sys.argv)
# ---------------------------------------------------------------------------

class TestMain:
    def test_non_git_dir_returns_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["prog", str(tmp_path)])
        assert autocommit_scan.main() == 1

    def test_subdir_of_repo_returns_1(self, tmp_path, monkeypatch):
        make_repo(tmp_path)
        subdir = tmp_path / "sub"
        subdir.mkdir()
        monkeypatch.setattr(sys, "argv", ["prog", str(subdir)])
        assert autocommit_scan.main() == 1

    def test_clean_repo_returns_0_with_no_commit(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo)
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "add config")
        monkeypatch.setattr(sys, "argv", ["prog", str(repo)])
        assert autocommit_scan.main() == 0
        log = _git(repo, "log", "--oneline").stdout
        assert "chore(backup)" not in log

    def test_dirty_repo_commits_and_returns_0(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo)
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "add config")
        (repo / "data.txt").write_text("v1\n")
        _git(repo, "add", "data.txt")
        _git(repo, "commit", "-m", "add data")
        (repo / "data.txt").write_text("v2\n")

        monkeypatch.setattr(sys, "argv", ["prog", str(repo)])
        assert autocommit_scan.main() == 0
        log = _git(repo, "log", "--oneline").stdout
        assert "chore(backup)" in log

    def test_nested_repo_not_ignored_returns_1(self, tmp_path, monkeypatch):
        root = make_repo(tmp_path / "root")
        _child = make_repo(root / "child")
        monkeypatch.setattr(sys, "argv", ["prog", str(root)])
        assert autocommit_scan.main() == 1

    def test_warning_flips_exit_code_but_run_completes(self, tmp_path, monkeypatch, capsys):
        repo = make_repo(tmp_path)
        write_autocommit_yaml(repo, commit_type="changed")
        _git(repo, "add", ".autocommit.yaml")
        _git(repo, "commit", "-m", "add config")
        (repo / "tracked.txt").write_text("v1")
        _git(repo, "add", "tracked.txt")
        _git(repo, "commit", "-m", "add tracked")
        (repo / "tracked.txt").write_text("v2")
        (repo / "untracked.txt").write_text("new")

        monkeypatch.setattr(sys, "argv", ["prog", str(repo)])
        assert autocommit_scan.main() == 1
        out = capsys.readouterr().out
        assert "commit type is 'changed'" in out
        assert "Done" in out  # script ran to completion
        # Tracked change was still committed despite the warning
        assert "chore(backup)" in _git(repo, "log", "--oneline").stdout

    def test_submodule_processed_without_gitignore(self, tmp_path, monkeypatch):
        root = make_repo(tmp_path / "root")
        sub_src = make_repo(tmp_path / "sub_src")
        sub = add_submodule(root, sub_src, "sub")

        write_autocommit_yaml(root)
        _git(root, "add", ".autocommit.yaml")
        _git(root, "commit", "-m", "root config")
        (root / "root_file.txt").write_text("v1\n")
        _git(root, "add", "root_file.txt")
        _git(root, "commit", "-m", "add root file")
        (root / "root_file.txt").write_text("v2\n")

        write_autocommit_yaml(sub)
        _git(sub, "add", ".autocommit.yaml")
        _git(sub, "commit", "-m", "sub config")
        (sub / "sub_file.txt").write_text("v1\n")
        _git(sub, "add", "sub_file.txt")
        _git(sub, "commit", "-m", "add sub file")
        (sub / "sub_file.txt").write_text("v2\n")

        monkeypatch.setattr(sys, "argv", ["prog", str(root)])
        assert autocommit_scan.main() == 0
        assert "chore(backup)" in _git(root, "log", "--oneline").stdout
        assert "chore(backup)" in _git(sub, "log", "--oneline").stdout

    def test_nested_repo_ignored_both_processed(self, tmp_path, monkeypatch):
        root = make_repo(tmp_path / "root")
        child = make_repo(root / "child")

        # Root ignores child
        (root / ".gitignore").write_text("child/\n")
        _git(root, "add", ".gitignore")
        _git(root, "commit", "-m", "ignore child")

        # Both repos have configs and dirty files
        write_autocommit_yaml(root)
        _git(root, "add", ".autocommit.yaml")
        _git(root, "commit", "-m", "root config")
        (root / "root_file.txt").write_text("v1\n")
        _git(root, "add", "root_file.txt")
        _git(root, "commit", "-m", "add root file")
        (root / "root_file.txt").write_text("v2\n")

        write_autocommit_yaml(child)
        _git(child, "add", ".autocommit.yaml")
        _git(child, "commit", "-m", "child config")
        (child / "child_file.txt").write_text("v1\n")
        _git(child, "add", "child_file.txt")
        _git(child, "commit", "-m", "add child file")
        (child / "child_file.txt").write_text("v2\n")

        monkeypatch.setattr(sys, "argv", ["prog", str(root)])
        result = autocommit_scan.main()
        assert result == 0
        assert "chore(backup)" in _git(root, "log", "--oneline").stdout
        assert "chore(backup)" in _git(child, "log", "--oneline").stdout
