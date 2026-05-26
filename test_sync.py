#!/usr/bin/env python3
"""Tests for sync/sync.py.

The integration tests in this file drive the real tool against ssh localhost.
They are skipped automatically if:
  - sshd is not reachable on localhost with passwordless (BatchMode) auth, or
  - python3 is not on the non-interactive ssh PATH (only matters for the
    handful of tests that exercise the post-sync remote fingerprint refresh).

The non-integration tests (CLI argument handling, pure helpers) always run.

On macOS you can enable the prerequisite with System Settings -> General ->
Sharing -> Remote Login.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Make sync/ importable when running from the project root.
sys.path.insert(0, str(Path(__file__).parent / "sync"))

import fingerprint  # noqa: E402
import sync as sync_tool  # noqa: E402

FP = fingerprint.FINGERPRINT_FILENAME
SYNC_SUFFIX = fingerprint.SYNC_SUFFIX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def read_fp(directory: Path) -> dict:
    return json.loads((directory / FP).read_text())


def make_tree(root: Path, tree: dict) -> None:
    """{"name": b"..."} -> file; {"name": {...}} -> directory."""
    root.mkdir(parents=True, exist_ok=True)
    for name, value in tree.items():
        path = root / name
        if isinstance(value, (bytes, bytearray)):
            path.write_bytes(bytes(value))
        elif isinstance(value, dict):
            make_tree(path, value)
        else:
            raise TypeError(f"bad tree value for {name!r}: {value!r}")


def fingerprint_tree(directory: Path) -> None:
    fingerprint.main([str(directory)])


# ---------------------------------------------------------------------------
# Availability checks (run once at import time)
# ---------------------------------------------------------------------------


def _probe(cmd: list[str]) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10)
        return r.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


_SSH_BASE = [
    "ssh",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=accept-new",
    "localhost",
]
SSH_AVAILABLE = _probe(_SSH_BASE + ["true"])
SSH_HAS_PY3 = SSH_AVAILABLE and _probe(_SSH_BASE + ["python3 --version"])

needs_ssh = pytest.mark.skipif(
    not SSH_AVAILABLE,
    reason=(
        "ssh to localhost not available — enable passwordless ssh to localhost"
    ),
)
needs_remote_python = pytest.mark.skipif(
    not SSH_HAS_PY3,
    reason="python3 not on non-interactive ssh PATH on localhost",
)


# ---------------------------------------------------------------------------
# Pure-logic tests (no SSH required)
# ---------------------------------------------------------------------------


def test_dir_entries_match_identical() -> None:
    a = {"md5": "abc", "size": 10}
    b = {"md5": "abc", "size": 10}
    assert sync_tool._dir_entries_match(a, b) is True


def test_dir_entries_differ_in_md5() -> None:
    a = {"md5": "abc", "size": 10}
    b = {"md5": "def", "size": 10}
    assert sync_tool._dir_entries_match(a, b) is False


def test_dir_entries_differ_in_size() -> None:
    a = {"md5": "abc", "size": 10}
    b = {"md5": "abc", "size": 11}
    assert sync_tool._dir_entries_match(a, b) is False


def test_dir_entries_one_or_both_missing() -> None:
    e = {"md5": "abc", "size": 10}
    assert sync_tool._dir_entries_match(e, None) is False
    assert sync_tool._dir_entries_match(None, e) is False
    assert sync_tool._dir_entries_match(None, None) is False


def test_load_local_fingerprint_missing_returns_empty(tmp_path: Path) -> None:
    result = sync_tool.load_local_fingerprint(tmp_path)
    assert result == {"files": {}, "dirs": {}}


def test_load_local_fingerprint_reads_existing(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a.txt": b"hello"})
    fingerprint_tree(tmp_path)
    result = sync_tool.load_local_fingerprint(tmp_path)
    assert result["files"]["a.txt"]["md5"] == md5(b"hello")


def test_cli_rejects_nonexistent_local_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "no-such-dir"
    assert sync_tool.main([str(bad), "localhost", "/tmp/x"]) == 1
    assert "is not a directory" in capsys.readouterr().err


def test_cli_rejects_file_as_local_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "actually_a_file"
    f.write_text("x")
    assert sync_tool.main([str(f), "localhost", "/tmp/x"]) == 1
    assert "is not a directory" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Integration tests: actual ssh localhost transport
# ---------------------------------------------------------------------------


def _run_sync(local: Path, remote: Path, *extra: str) -> int:
    """Invoke sync.main() against ssh localhost.

    Most tests pass --no-refresh-after to avoid requiring python3 on the
    non-interactive ssh PATH; tests that specifically exercise the post-sync
    refresh override that.
    """
    args = [str(local), "localhost", str(remote), *extra]
    return sync_tool.main(args)


@needs_ssh
class TestPushOnly:
    """Local has files; remote starts empty. Files flow local -> remote."""

    def test_initial_push_copies_all_files(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"a.txt": b"alpha", "b.txt": b"beta"})
        fingerprint_tree(local)

        assert _run_sync(local, remote, "--no-refresh-after") == 0

        assert (remote / "a.txt").read_bytes() == b"alpha"
        assert (remote / "b.txt").read_bytes() == b"beta"

    def test_push_creates_remote_directory_if_missing(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote-not-yet-existing"
        make_tree(local, {"a.txt": b"a"})
        fingerprint_tree(local)
        assert not remote.exists()

        assert _run_sync(local, remote, "--no-refresh-after") == 0
        assert (remote / "a.txt").read_bytes() == b"a"

    def test_push_handles_nested_directories(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(
            local,
            {"top.txt": b"top", "a": {"x.txt": b"x", "b": {"y.txt": b"y"}}},
        )
        fingerprint_tree(local)

        _run_sync(local, remote, "--no-refresh-after")

        assert (remote / "top.txt").read_bytes() == b"top"
        assert (remote / "a" / "x.txt").read_bytes() == b"x"
        assert (remote / "a" / "b" / "y.txt").read_bytes() == b"y"

    def test_filename_with_spaces_is_quoted_correctly(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"file with spaces.txt": b"data", "ok.txt": b"k"})
        fingerprint_tree(local)

        _run_sync(local, remote, "--no-refresh-after")

        assert (remote / "file with spaces.txt").read_bytes() == b"data"
        assert (remote / "ok.txt").read_bytes() == b"k"

    def test_no_sidecar_files_remain_after_successful_sync(
        self, tmp_path: Path
    ) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"a": b"a", "sub": {"b": b"b"}})
        fingerprint_tree(local)

        _run_sync(local, remote, "--no-refresh-after")

        assert list(local.rglob("*" + SYNC_SUFFIX)) == []
        assert list(remote.rglob("*" + SYNC_SUFFIX)) == []

    def test_pre_existing_sidecar_does_not_break_sync(
        self, tmp_path: Path
    ) -> None:
        """A leftover .~sync~ on remote from a prior crashed run must not
        interfere with a fresh sync of the same name.
        """
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"a.txt": b"fresh"})
        fingerprint_tree(local)
        remote.mkdir()
        (remote / ("a.txt" + SYNC_SUFFIX)).write_bytes(b"stale leftover")

        _run_sync(local, remote, "--no-refresh-after")

        assert (remote / "a.txt").read_bytes() == b"fresh"


@needs_ssh
class TestPullOnly:
    """Remote has files; local starts empty. Files flow remote -> local."""

    def test_initial_pull_copies_all_files(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(remote, {"a.txt": b"alpha", "b.txt": b"beta"})
        fingerprint_tree(remote)
        local.mkdir()

        assert _run_sync(local, remote, "--no-refresh-after") == 0

        assert (local / "a.txt").read_bytes() == b"alpha"
        assert (local / "b.txt").read_bytes() == b"beta"

    def test_pull_handles_nested_directories(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(remote, {"a": {"x.txt": b"x", "b": {"y.txt": b"y"}}})
        fingerprint_tree(remote)
        local.mkdir()

        _run_sync(local, remote, "--no-refresh-after")

        assert (local / "a" / "x.txt").read_bytes() == b"x"
        assert (local / "a" / "b" / "y.txt").read_bytes() == b"y"


@needs_ssh
class TestBidirectional:
    """Each side has unique content; verify the union policy."""

    def test_union_of_unique_files_is_built_on_both_sides(
        self, tmp_path: Path
    ) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"only_local.txt": b"L", "shared_name.txt": b"L-version"})
        make_tree(remote, {"only_remote.txt": b"R", "shared_name.txt": b"R-version"})
        fingerprint_tree(local)
        fingerprint_tree(remote)

        _run_sync(local, remote, "--no-refresh-after")

        for d in (local, remote):
            assert (d / "only_local.txt").read_bytes() == b"L"
            assert (d / "only_remote.txt").read_bytes() == b"R"

    def test_shared_name_is_never_overwritten(self, tmp_path: Path) -> None:
        """The whole point of "only copy missing": even with different
        content, neither side touches the other's version.
        """
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"common.txt": b"LOCAL-WINS-LOCALLY"})
        make_tree(remote, {"common.txt": b"REMOTE-WINS-REMOTELY"})
        fingerprint_tree(local)
        fingerprint_tree(remote)

        _run_sync(local, remote, "--no-refresh-after")

        assert (local / "common.txt").read_bytes() == b"LOCAL-WINS-LOCALLY"
        assert (remote / "common.txt").read_bytes() == b"REMOTE-WINS-REMOTELY"

    def test_subdirectory_only_on_local_is_pushed(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"sub": {"x": b"x", "y": b"y"}})
        fingerprint_tree(local)
        remote.mkdir()
        fingerprint_tree(remote)  # empty fingerprint

        _run_sync(local, remote, "--no-refresh-after")

        assert (remote / "sub" / "x").read_bytes() == b"x"
        assert (remote / "sub" / "y").read_bytes() == b"y"

    def test_subdirectory_only_on_remote_is_pulled(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(remote, {"sub": {"x": b"x", "y": b"y"}})
        fingerprint_tree(remote)
        local.mkdir()
        fingerprint_tree(local)

        _run_sync(local, remote, "--no-refresh-after")

        assert (local / "sub" / "x").read_bytes() == b"x"
        assert (local / "sub" / "y").read_bytes() == b"y"


@needs_ssh
class TestSilentSkips:
    """Per the spec, missing-on-disk files are not errors."""

    def test_local_fingerprint_lists_file_that_no_longer_exists(
        self, tmp_path: Path
    ) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"present.txt": b"P", "ghost.txt": b"G"})
        fingerprint_tree(local)  # captures both
        (local / "ghost.txt").unlink()

        assert _run_sync(local, remote, "--no-refresh-after") == 0

        assert (remote / "present.txt").read_bytes() == b"P"
        assert not (remote / "ghost.txt").exists()

    def test_remote_fingerprint_lists_file_that_no_longer_exists(
        self, tmp_path: Path
    ) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(remote, {"present.txt": b"P", "ghost.txt": b"G"})
        fingerprint_tree(remote)
        (remote / "ghost.txt").unlink()
        local.mkdir()

        assert _run_sync(local, remote, "--no-refresh-after") == 0

        assert (local / "present.txt").read_bytes() == b"P"
        assert not (local / "ghost.txt").exists()


@needs_ssh
class TestFastSkip:
    def test_identical_subtree_is_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"sub": {"x": b"x", "y": b"y"}})
        shutil.copytree(local, remote)
        fingerprint_tree(local)
        fingerprint_tree(remote)

        # Sub's fingerprints are byte-identical (sorted JSON, identical
        # contents), so the parent's `dirs.sub` md5+size must match and the
        # recursion is short-circuited.
        _run_sync(local, remote, "--no-refresh-after", "-v")

        assert "skip identical subtree" in capsys.readouterr().err


@needs_ssh
class TestTypeConflict:
    def test_name_is_file_on_one_side_dir_on_other(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"thing": b"a file locally"})
        make_tree(remote, {"thing": {"inner": b"a dir remotely"}})
        fingerprint_tree(local)
        fingerprint_tree(remote)

        _run_sync(local, remote, "--no-refresh-after")

        # Neither side gets stomped on. We just warn.
        err = capsys.readouterr().err
        assert "type conflict" in err
        assert (local / "thing").read_bytes() == b"a file locally"
        assert (remote / "thing" / "inner").read_bytes() == b"a dir remotely"


@needs_ssh
class TestRefreshFlag:
    """--refresh runs fingerprint.py incrementally on the local tree first."""

    def test_without_refresh_a_new_unfingerprinted_local_file_is_invisible(
        self, tmp_path: Path
    ) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"old.txt": b"old"})
        fingerprint_tree(local)
        # Add a file but don't refresh the fingerprint.
        (local / "new.txt").write_bytes(b"new")

        _run_sync(local, remote, "--no-refresh-after")

        assert (remote / "old.txt").read_bytes() == b"old"
        assert not (remote / "new.txt").exists()

    def test_with_refresh_a_new_unfingerprinted_local_file_is_picked_up(
        self, tmp_path: Path
    ) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"old.txt": b"old"})
        fingerprint_tree(local)
        (local / "new.txt").write_bytes(b"new")

        _run_sync(local, remote, "--refresh", "--no-refresh-after")

        assert (remote / "new.txt").read_bytes() == b"new"


@needs_ssh
@needs_remote_python
class TestPostSyncRefresh:
    """Default behavior (no --no-refresh-after) re-runs fingerprint on remote."""

    def test_remote_fingerprint_reflects_newly_pushed_files(
        self, tmp_path: Path
    ) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"a.txt": b"alpha"})
        fingerprint_tree(local)

        _run_sync(local, remote)  # default: refresh after

        rfp = read_fp(remote)
        assert "a.txt" in rfp["files"]
        assert rfp["files"]["a.txt"]["md5"] == md5(b"alpha")

    def test_local_fingerprint_reflects_newly_pulled_files(
        self, tmp_path: Path
    ) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(remote, {"r.txt": b"r"})
        fingerprint_tree(remote)
        local.mkdir()

        _run_sync(local, remote)

        lfp = read_fp(local)
        assert "r.txt" in lfp["files"]

    def test_no_refresh_after_leaves_remote_fingerprint_absent(
        self, tmp_path: Path
    ) -> None:
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"a.txt": b"a"})
        fingerprint_tree(local)

        _run_sync(local, remote, "--no-refresh-after")

        # File got there, but the refresh did not happen.
        assert (remote / "a.txt").read_bytes() == b"a"
        assert not (remote / FP).exists()

    def test_second_sync_after_default_refresh_is_a_no_op(
        self, tmp_path: Path
    ) -> None:
        """After a sync with default refresh, both fingerprints are in sync,
        so a second run should copy nothing and short-circuit the subtree."""
        local = tmp_path / "local"
        remote = tmp_path / "remote"
        make_tree(local, {"a.txt": b"a", "sub": {"b.txt": b"b"}})
        fingerprint_tree(local)

        _run_sync(local, remote)

        remote_fp_before = (remote / FP).read_bytes()
        # Touch nothing, run again.
        time.sleep(1)  # ensure mtime changes would be visible if we cared
        _run_sync(local, remote)

        # The post-sync refresh rewrites the .fingerprint file in place but
        # with identical content, so byte equality should still hold.
        assert (remote / FP).read_bytes() == remote_fp_before
