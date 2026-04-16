#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
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


def ensure_nested_repos_ignored(root: Path, repos: list[Path]) -> None:
    for repo in repos:
        if repo == root:
            continue
        parent = nearest_parent_repo(repos, repo)
        rel = repo.relative_to(parent)
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
    proc = run_git(repo, ["status", "--porcelain", "--untracked-files=normal", "--ignored=no"])
    lines = [line.rstrip("\n") for line in proc.stdout.splitlines() if line.strip()]
    return lines


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

    return RepoConfig(
        commit_type=commit_type,
        commit_branch=commit_branch,
        max_size=max_size,
        max_files=max_files,
        push=push,
    )


def current_branch(repo: Path) -> str:
    proc = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = proc.stdout.strip()
    if not branch:
        raise AutocommitError("Unable to determine current branch", repo=repo)
    return branch


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


def stash_selected_paths(repo: Path, files: list[str]) -> str:
    if not files:
        raise AutocommitError("Cannot stash empty file selection", repo=repo)

    stash_message = f"autocommit-temporary-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    proc = run_git(repo, ["stash", "push", "-u", "-m", stash_message, "--", *files], check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip() or "unknown git error"
        raise AutocommitError(f"git stash push failed: {stderr}", repo=repo)

    out = (proc.stdout or "") + (proc.stderr or "")
    if "No local changes to save" in out:
        raise AutocommitError("No changes available to stash for '#new' backup flow", repo=repo)

    stash_commit = run_git(repo, ["rev-parse", "--verify", "refs/stash"]).stdout.strip()
    if not stash_commit:
        raise AutocommitError("Unable to resolve stash reference after stashing", repo=repo)
    return stash_commit


def commit_and_maybe_push(repo: Path, cfg: RepoConfig, files_to_commit: list[str]) -> None:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    commit_message = f"chore(backup): automatic commit {now}"

    original_branch = current_branch(repo)
    created_new_branch = False
    stashed_paths = False
    stash_ref: str | None = None

    if not files_to_commit:
        print(f"[{repo}] No files left to commit after filters")
        return

    if cfg.commit_branch == "#new":
        previous_backup = latest_backup_branch(repo)
        base_ref = previous_backup or original_branch

        add_files(repo, files_to_commit)

        has_staged = run_git(repo, ["diff", "--cached", "--quiet", "--", *files_to_commit], check=False)
        if has_staged.returncode == 0:
            print(f"[{repo}] No files left to commit after filters")
            return
        if has_staged.returncode not in (0, 1):
            stderr = has_staged.stderr.strip() or has_staged.stdout.strip() or "unknown git error"
            raise AutocommitError(f"git diff --cached --quiet failed: {stderr}", repo=repo)

        if previous_backup is not None:
            differs_from_last_backup = run_git(
                repo,
                ["diff", "--cached", "--quiet", previous_backup, "--", *files_to_commit],
                check=False,
            )
            if differs_from_last_backup.returncode == 0:
                unstage_files(repo, files_to_commit)
                print(
                    f"[{repo}] Skipping backup: current contents match latest backup branch {previous_backup}"
                )
                return
            if differs_from_last_backup.returncode not in (0, 1):
                stderr = (
                    differs_from_last_backup.stderr.strip()
                    or differs_from_last_backup.stdout.strip()
                    or "unknown git error"
                )
                raise AutocommitError(f"git diff --cached --quiet against {previous_backup} failed: {stderr}", repo=repo)

        stash_ref = stash_selected_paths(repo, files_to_commit)
        stashed_paths = True

        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        new_branch = f"backup/{ts}"
        # TODO: remove old backup/* branches based on retention policy.
        run_git(repo, ["checkout", "-b", new_branch, base_ref])
        run_git(repo, ["stash", "apply", "--index", stash_ref])
        target_branch = new_branch
        created_new_branch = True
    else:
        target_branch = cfg.commit_branch
        if original_branch != target_branch:
            raise AutocommitError(
                f"Current branch is '{original_branch}', expected '{target_branch}'",
                repo=repo,
            )

        add_files(repo, files_to_commit)

        has_staged = run_git(repo, ["diff", "--cached", "--quiet", "--", *files_to_commit], check=False)
        if has_staged.returncode == 0:
            print(f"[{repo}] No files left to commit after filters")
            return
        if has_staged.returncode not in (0, 1):
            stderr = has_staged.stderr.strip() or has_staged.stdout.strip() or "unknown git error"
            raise AutocommitError(f"git diff --cached --quiet failed: {stderr}", repo=repo)

    try:
        run_git(repo, ["commit", "-m", commit_message, "--", *files_to_commit])
        print(f"[{repo}] Commit created on branch {target_branch}")

        if cfg.push:
            if cfg.commit_branch == "#new":
                run_git(repo, ["push", "-u", "origin", target_branch])
            else:
                run_git(repo, ["push"])
            print(f"[{repo}] Push successful")
    except Exception:
        if created_new_branch:
            try:
                run_git(repo, ["checkout", original_branch])
            except AutocommitError as checkout_exc:
                print(
                    f"WARNING [{repo}]: failed to restore original branch '{original_branch}': {checkout_exc}",
                    file=sys.stderr,
                )
        if stashed_paths and stash_ref is not None:
            try:
                run_git(repo, ["stash", "pop", "--index", stash_ref])
            except AutocommitError as stash_exc:
                print(
                    f"WARNING [{repo}]: failed to restore stashed files on '{original_branch}': {stash_exc}",
                    file=sys.stderr,
                )
        raise
    else:
        if created_new_branch:
            run_git(repo, ["checkout", original_branch])
        if stashed_paths and stash_ref is not None:
            run_git(repo, ["stash", "pop", "--index", stash_ref])


def process_repository(repo: Path, dirty_tracked: list[str], dirty_untracked: list[str]) -> None:
    dirty_all = sorted(set(dirty_tracked + dirty_untracked))
    if not dirty_all:
        return

    marker = repo / ".autocommit"
    if not marker.exists():
        print(
            f"[{repo}] Dirty repository has no .autocommit marker; skipping."
            f" Examples:{format_examples(dirty_all)}"
        )
        return

    cfg = load_repo_config(repo)
    candidates = dirty_tracked[:] if cfg.commit_type == "changed" else dirty_all[:]
    candidates = sorted(set(candidates))

    candidates, ignored_large = filter_by_max_size(repo, candidates, cfg.max_size)
    if ignored_large:
        print(
            f"[{repo}] Ignored files larger than max-size={cfg.max_size}."
            f" Examples:{format_examples(sorted(ignored_large))}"
        )

    to_commit, ignored_over_limit = cap_by_max_files(candidates, cfg.max_files)
    if ignored_over_limit:
        print(
            f"[{repo}] Ignored files beyond max-files={cfg.max_files}."
            f" Examples:{format_examples(sorted(ignored_over_limit))}"
        )

    commit_and_maybe_push(repo, cfg, to_commit)


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

        for repo in repos:
            lines = status_lines(repo)
            tracked = tracked_changed_files(repo, lines)
            untracked = untracked_files(lines)
            process_repository(repo, tracked, untracked)

        print("Done")
        return 0
    except AutocommitError as exc:
        if exc.repo is not None:
            print(f"ERROR [{exc.repo}]: {exc}", file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())