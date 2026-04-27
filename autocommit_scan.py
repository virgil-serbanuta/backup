#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import cast

import yaml


class AutocommitError(Exception):
    def __init__(self, message: str, repo: Path | None = None):
        super().__init__(message)
        self.repo = repo


@dataclass
class RepoConfig:
    commit_type: str
    commit_branch: str
    max_size: int
    max_files: int
    push: bool
    auto_add: list[str] = field(default_factory=list[str])


def run_git(repo: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo), *args]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip() or "unknown git error"
        raise AutocommitError(f"git {' '.join(args)} failed: {stderr}", repo=repo)
    return proc


def format_examples(items: list[str], max_items: int = 5) -> str:
    if not items:
        return ""
    shown = items[:max_items]
    suffix = "\n  ..." if len(items) > max_items else ""
    return "\n  " + "\n  ".join(shown) + suffix


def ensure_root_repo(root: Path) -> Path:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise AutocommitError(f"Directory is not a git repository: {root}")
    top = Path(proc.stdout.strip()).resolve()
    if top != root.resolve():
        raise AutocommitError(
            f"Directory is inside a git repository but is not its root: {root} (root: {top})"
        )
    return top


def discover_repositories(root: Path) -> list[Path]:
    repos: set[Path] = {root.resolve()}
    for dirpath, dirnames, _ in os.walk(root):
        current = Path(dirpath)
        is_repo = False
        if current != root and ".git" in dirnames:
            is_repo = True
        git_file = current / ".git"
        if current != root and git_file.is_file():
            is_repo = True
        if is_repo:
            repos.add(current.resolve())
        if ".git" in dirnames:
            dirnames[:] = [d for d in dirnames if d != ".git"]
    return sorted(repos)


def nearest_parent_repo(repos: list[Path], repo: Path) -> Path:
    candidates = [r for r in repos if r != repo and r in repo.parents]
    if not candidates:
        raise AutocommitError(f"No parent repository found for nested repository: {repo}", repo=repo)
    return max(candidates, key=lambda p: len(p.parts))


def is_submodule(parent: Path, rel: Path) -> bool:
    """Return True if rel (relative to parent) is a registered git submodule."""
    proc = run_git(parent, ["ls-files", "--stage", "--", str(rel)], check=False)
    return proc.returncode == 0 and proc.stdout.strip().startswith("160000")


def ensure_nested_repos_ignored(root: Path, repos: list[Path]) -> None:
    for repo in repos:
        if repo == root:
            continue
        parent = nearest_parent_repo(repos, repo)
        rel = repo.relative_to(parent)
        if is_submodule(parent, rel):
            continue
        proc = run_git(parent, ["check-ignore", "-q", str(rel)], check=False)
        if proc.returncode == 0:
            continue
        if proc.returncode == 1:
            raise AutocommitError(
                f"Nested repository is not ignored by parent repository. parent={parent} nested={repo}",
                repo=repo,
            )
        stderr = proc.stderr.strip() or proc.stdout.strip() or "unknown git error"
        raise AutocommitError(f"git check-ignore failed: {stderr}", repo=parent)


def status_lines(repo: Path) -> list[str]:
    # -z: NUL-terminated entries with paths emitted as-is. Without -z, git
    # wraps paths containing spaces or unusual characters in double quotes,
    # which would break downstream pattern matching and `git add` pathspecs.
    # --untracked-files=all so individual files inside new directories are
    # visible — required for auto-add globs to match files at any depth.
    proc = run_git(
        repo,
        ["status", "--porcelain", "-z", "--untracked-files=all", "--ignored=no"],
    )
    raw = proc.stdout
    if not raw:
        return []
    entries = raw.split("\0")
    if entries and entries[-1] == "":
        entries.pop()

    result: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) < 3:
            i += 1
            continue
        # With -z, a rename/copy entry's source path is in the next token.
        # We only care about the destination (already in `entry[3:]`), so
        # consume and discard the source.
        if entry[0] in ("R", "C"):
            i += 2
        else:
            i += 1
        result.append(entry)
    return result


def _matches_any_glob(rel_path: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    p = PurePath(rel_path)
    return any(p.match(pat) for pat in patterns)


def tracked_changed_files(repo: Path, lines: list[str]) -> list[str]:
    files: list[str] = []
    for line in lines:
        if line.startswith("?? "):
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return sorted(set(files))


def untracked_files(lines: list[str]) -> list[str]:
    files = [line[3:] for line in lines if line.startswith("?? ")]
    return sorted(set(files))


def load_repo_config(repo: Path) -> RepoConfig:
    cfg_path = repo / ".autocommit.yaml"
    if not cfg_path.exists():
        raise AutocommitError("Missing .autocommit.yaml", repo=repo)

    try:
        content = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AutocommitError(f"Cannot read .autocommit.yaml: {exc}", repo=repo) from exc

    if not isinstance(content, dict):
        raise AutocommitError(".autocommit.yaml must contain a YAML object", repo=repo)

    content_map = cast(dict[str, object], content)

    commit_type = content_map.get("commit type")
    commit_branch = content_map.get("commit branch")
    max_size = content_map.get("max-size")
    max_files = content_map.get("max-files")
    push = content_map.get("push")

    if not isinstance(commit_type, str) or commit_type not in {"changed", "all"}:
        raise AutocommitError("'commit type' must be 'changed' or 'all'", repo=repo)
    if not isinstance(commit_branch, str) or not commit_branch:
        raise AutocommitError("'commit branch' must be a non-empty string", repo=repo)
    if commit_branch != "#new":
        branch_check = run_git(repo, ["check-ref-format", "--branch", commit_branch], check=False)
        if branch_check.returncode != 0:
            raise AutocommitError(f"Invalid git branch name in 'commit branch': {commit_branch}", repo=repo)
    if not isinstance(max_size, int) or max_size < 0:
        raise AutocommitError("'max-size' must be a non-negative integer", repo=repo)
    if not isinstance(max_files, int) or max_files < 0:
        raise AutocommitError("'max-files' must be a non-negative integer", repo=repo)
    if not isinstance(push, bool):
        raise AutocommitError("'push' must be a boolean", repo=repo)

    auto_add_raw = content_map.get("auto-add")
    auto_add: list[str]
    if auto_add_raw is None:
        auto_add = []
    elif isinstance(auto_add_raw, str):
        if not auto_add_raw:
            raise AutocommitError("'auto-add' must be a non-empty string or list of non-empty strings", repo=repo)
        auto_add = [auto_add_raw]
    elif isinstance(auto_add_raw, list):
        items = cast(list[object], auto_add_raw)
        if not all(isinstance(p, str) and p for p in items):
            raise AutocommitError("'auto-add' must be a non-empty string or list of non-empty strings", repo=repo)
        auto_add = [cast(str, p) for p in items]
    else:
        raise AutocommitError("'auto-add' must be a non-empty string or list of non-empty strings", repo=repo)

    return RepoConfig(
        commit_type=commit_type,
        commit_branch=commit_branch,
        max_size=max_size,
        max_files=max_files,
        push=push,
        auto_add=auto_add,
    )


def current_branch(repo: Path) -> str:
    proc = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = proc.stdout.strip()
    if not branch:
        raise AutocommitError("Unable to determine current branch", repo=repo)
    return branch


def has_commits(repo: Path) -> bool:
    proc = run_git(repo, ["rev-parse", "--verify", "--quiet", "HEAD"], check=False)
    return proc.returncode == 0


def filter_by_max_size(repo: Path, files: list[str], max_size: int) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    ignored_large: list[str] = []
    for rel in files:
        path = repo / rel
        if path.exists() and path.is_file() and path.stat().st_size > max_size:
            ignored_large.append(rel)
            continue
        kept.append(rel)
    return kept, ignored_large


def cap_by_max_files(files: list[str], max_files: int) -> tuple[list[str], list[str]]:
    if max_files == 0:
        return [], files[:]
    if len(files) <= max_files:
        return files, []
    return files[:max_files], files[max_files:]


def add_files(repo: Path, files: list[str]) -> None:
    if not files:
        return
    run_git(repo, ["add", "-A", "--", *files])


def unstage_files(repo: Path, files: list[str]) -> None:
    if not files:
        return
    run_git(repo, ["reset", "-q", "--", *files])


def latest_backup_branch(repo: Path) -> str | None:
    proc = run_git(
        repo,
        ["for-each-ref", "--sort=-refname", "--format=%(refname:short)", "refs/heads/backup"],
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip() or "unknown git error"
        raise AutocommitError(f"Unable to list backup branches: {stderr}", repo=repo)

    pattern = re.compile(r"^backup/\d{8}-\d{6}$")
    for line in proc.stdout.splitlines():
        branch = line.strip()
        if pattern.match(branch):
            return branch
    return None


def commit_and_maybe_push(repo: Path, cfg: RepoConfig, files_to_commit: list[str]) -> None:
    if not files_to_commit:
        print(f"[{repo}] No files left to commit after filters")
        return
    if cfg.commit_branch == "#new":
        _commit_via_worktree(repo, cfg, files_to_commit)
    else:
        _commit_to_existing_branch(repo, cfg, files_to_commit)


def _commit_to_existing_branch(repo: Path, cfg: RepoConfig, files_to_commit: list[str]) -> None:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    commit_message = f"chore(backup): automatic commit {now}"
    target_branch = cfg.commit_branch

    original_branch = current_branch(repo)
    if original_branch != target_branch:
        raise AutocommitError(
            f"Current branch is '{original_branch}', expected '{target_branch}'",
            repo=repo,
        )

    add_files(repo, files_to_commit)
    files_staged = True
    try:
        has_staged = run_git(repo, ["diff", "--cached", "--quiet", "--", *files_to_commit], check=False)
        if has_staged.returncode == 0:
            print(f"[{repo}] No files left to commit after filters")
            run_git(repo, ["reset", "-q", "--", *files_to_commit])
            files_staged = False
            return
        if has_staged.returncode not in (0, 1):
            stderr = has_staged.stderr.strip() or has_staged.stdout.strip() or "unknown git error"
            raise AutocommitError(f"git diff --cached --quiet failed: {stderr}", repo=repo)

        run_git(repo, ["commit", "-m", commit_message, "--", *files_to_commit])
        files_staged = False
        print(f"[{repo}] Commit created on branch {target_branch}")

        if cfg.push:
            run_git(repo, ["push"])
            print(f"[{repo}] Push successful")
    except Exception:
        if files_staged:
            try:
                run_git(repo, ["reset", "-q", "--", *files_to_commit])
            except AutocommitError as exc:
                print(f"WARNING [{repo}]: failed to unstage files: {exc}", file=sys.stderr)
        raise


def _populate_worktree(main_repo: Path, wt_path: Path, files: list[str]) -> None:
    """Mirror the working-tree state of `files` from main_repo into wt_path."""
    for rel in files:
        src = main_repo / rel
        dst = wt_path / rel
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        elif os.path.lexists(str(dst)):
            dst.unlink()
        if not os.path.lexists(str(src)):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)


def _remove_worktree(repo: Path, wt_path: Path) -> None:
    if wt_path.exists():
        run_git(repo, ["worktree", "remove", "--force", str(wt_path)], check=False)
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)
        run_git(repo, ["worktree", "prune"], check=False)


def _commit_via_worktree(repo: Path, cfg: RepoConfig, files_to_commit: list[str]) -> None:
    now = dt.datetime.now()
    ts = now.strftime("%Y%m%d-%H%M%S")
    new_branch = f"backup/{ts}"
    commit_message = f"chore(backup): automatic commit {now.strftime('%Y-%m-%d %H:%M:%S')}"

    previous_backup = latest_backup_branch(repo)
    base_ref = previous_backup or current_branch(repo)

    wt_path = Path(tempfile.gettempdir()) / f"autocommit-wt-{ts}-{os.getpid()}"
    if wt_path.exists() or _is_registered_worktree(repo, wt_path):
        _remove_worktree(repo, wt_path)

    branch_created = False
    keep_branch = False
    worktree_added = False
    try:
        run_git(repo, ["worktree", "add", "--detach", str(wt_path), base_ref])
        worktree_added = True
        try:
            _populate_worktree(repo, wt_path, files_to_commit)
            run_git(wt_path, ["add", "-A", "--", *files_to_commit])

            no_changes = run_git(wt_path, ["diff", "--cached", "--quiet"], check=False)
            if no_changes.returncode == 0:
                target = previous_backup or "current branch"
                print(f"[{repo}] Skipping backup: contents match {target}")
                return
            if no_changes.returncode != 1:
                stderr = no_changes.stderr.strip() or "unknown git error"
                raise AutocommitError(f"git diff --cached --quiet failed: {stderr}", repo=repo)

            run_git(wt_path, ["checkout", "-b", new_branch])
            branch_created = True

            run_git(wt_path, ["commit", "-m", commit_message])
            print(f"[{repo}] Commit created on branch {new_branch}")

            if cfg.push:
                run_git(wt_path, ["push", "-u", "origin", new_branch])
                print(f"[{repo}] Push successful")

            keep_branch = True
        finally:
            if worktree_added:
                _remove_worktree(repo, wt_path)
    finally:
        if branch_created and not keep_branch:
            run_git(repo, ["branch", "-D", new_branch], check=False)


def _is_registered_worktree(repo: Path, wt_path: Path) -> bool:
    proc = run_git(repo, ["worktree", "list", "--porcelain"], check=False)
    if proc.returncode != 0:
        return False
    target = str(wt_path)
    for line in proc.stdout.splitlines():
        if line.startswith("worktree ") and line[len("worktree "):] == target:
            return True
    return False


def process_repository(repo: Path, dirty_tracked: list[str], dirty_untracked: list[str]) -> bool:
    dirty_all = sorted(set(dirty_tracked + dirty_untracked))
    if not dirty_all:
        return False

    if not has_commits(repo):
        print(
            f"[{repo}] Repository has no commits yet (unborn HEAD); skipping."
            f" Create an initial commit before autocommit can back it up."
            f" Examples:{format_examples(dirty_all)}"
        )
        return True

    marker = repo / ".autocommit.yaml"
    if not marker.exists():
        print(
            f"[{repo}] Dirty repository has no .autocommit.yaml marker; skipping."
            f" Examples:{format_examples(dirty_all)}"
        )
        return True

    cfg = load_repo_config(repo)
    warned = False

    if cfg.commit_type == "changed":
        auto_added = {f for f in dirty_untracked if _matches_any_glob(f, cfg.auto_add)}
        unmatched_untracked = [f for f in dirty_untracked if f not in auto_added]
        if unmatched_untracked:
            print(
                f"[{repo}] commit type is 'changed' but repository has untracked files; they will not be committed."
                f" Examples:{format_examples(sorted(unmatched_untracked))}"
            )
            warned = True
        candidates = sorted(set(dirty_tracked) | auto_added)
    else:
        candidates = sorted(set(dirty_all))

    candidates, ignored_large = filter_by_max_size(repo, candidates, cfg.max_size)
    if ignored_large:
        print(
            f"[{repo}] Ignored files larger than max-size={cfg.max_size}."
            f" Examples:{format_examples(sorted(ignored_large))}"
        )
        warned = True

    to_commit, ignored_over_limit = cap_by_max_files(candidates, cfg.max_files)
    if ignored_over_limit:
        print(
            f"[{repo}] Ignored files beyond max-files={cfg.max_files}."
            f" Examples:{format_examples(sorted(ignored_over_limit))}"
        )
        warned = True

    commit_and_maybe_push(repo, cfg, to_commit)
    return warned


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan nested git repositories and auto-commit")
    parser.add_argument(
        "directory",
        nargs="?",
        default=str(Path.home()),
        help="Root git repository (default: HOME)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.directory).expanduser().resolve()

    try:
        root = ensure_root_repo(root)
        repos = discover_repositories(root)
        ensure_nested_repos_ignored(root, repos)

        print(f"Found {len(repos)} repository/repositories under {root}")

        warned = False
        for repo in repos:
            lines = status_lines(repo)
            tracked = tracked_changed_files(repo, lines)
            untracked = untracked_files(lines)
            if process_repository(repo, tracked, untracked):
                warned = True

        print("Done")
        return 1 if warned else 0
    except AutocommitError as exc:
        if exc.repo is not None:
            print(f"ERROR [{exc.repo}]: {exc}", file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())