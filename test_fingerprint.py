#!/usr/bin/env python3
"""Tests for sync/fingerprint.py.

Each test builds its own tree under pytest's tmp_path so the tests are
isolated and leave no state behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

# Make sync/ importable when running from the project root.
sys.path.insert(0, str(Path(__file__).parent / "sync"))

import fingerprint  # noqa: E402

FP = fingerprint.FINGERPRINT_FILENAME
SYNC_SUFFIX = fingerprint.SYNC_SUFFIX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def read_fp(directory: Path) -> dict:
    return json.loads((directory / FP).read_text())


def run(root: Path, *flags: str) -> int:
    return fingerprint.main([str(root), *flags])


def make_tree(root: Path, tree: dict) -> None:
    """Materialize a dict tree.

    {"name": b"..."} -> file with those bytes
    {"name": {...}}  -> directory built recursively
    """
    root.mkdir(parents=True, exist_ok=True)
    for name, value in tree.items():
        path = root / name
        if isinstance(value, (bytes, bytearray)):
            path.write_bytes(bytes(value))
        elif isinstance(value, dict):
            make_tree(path, value)
        else:
            raise TypeError(f"bad tree value for {name!r}: {value!r}")


# ---------------------------------------------------------------------------
# Single directory: basic shape, md5 correctness, determinism
# ---------------------------------------------------------------------------


def test_empty_directory_writes_empty_fingerprint(tmp_path: Path) -> None:
    assert run(tmp_path) == 0
    assert read_fp(tmp_path) == {"files": {}, "dirs": {}}


def test_single_file_md5_and_size(tmp_path: Path) -> None:
    make_tree(tmp_path, {"hello.txt": b"hello"})
    assert run(tmp_path) == 0
    assert read_fp(tmp_path) == {
        "files": {"hello.txt": {"md5": md5(b"hello"), "size": 5}},
        "dirs": {},
    }


def test_multiple_files_including_empty(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a": b"alpha", "b": b"beta", "empty": b""})
    run(tmp_path)
    files = read_fp(tmp_path)["files"]
    assert files["a"] == {"md5": md5(b"alpha"), "size": 5}
    assert files["b"] == {"md5": md5(b"beta"), "size": 4}
    assert files["empty"] == {"md5": md5(b""), "size": 0}


def test_repeated_runs_are_byte_identical(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a": b"x", "b": b"y", "sub": {"c": b"z"}})
    run(tmp_path)
    first_root = (tmp_path / FP).read_bytes()
    first_sub = (tmp_path / "sub" / FP).read_bytes()
    run(tmp_path)
    assert (tmp_path / FP).read_bytes() == first_root
    assert (tmp_path / "sub" / FP).read_bytes() == first_sub


# ---------------------------------------------------------------------------
# Nested trees: depth-first ordering and parent/child fingerprint linkage
# ---------------------------------------------------------------------------


def test_every_directory_gets_a_fingerprint(tmp_path: Path) -> None:
    make_tree(
        tmp_path,
        {
            "top.txt": b"top",
            "a": {"x.txt": b"x", "b": {"y.txt": b"y"}},
        },
    )
    run(tmp_path)
    assert (tmp_path / FP).is_file()
    assert (tmp_path / "a" / FP).is_file()
    assert (tmp_path / "a" / "b" / FP).is_file()


def test_parent_dir_entry_matches_child_fingerprint(tmp_path: Path) -> None:
    """Documents both the format ("dirs" entries describe the child .fp file)
    and the depth-first ordering (child must be written before parent reads it).
    """
    make_tree(tmp_path, {"a": {"x.txt": b"x"}})
    run(tmp_path)
    child_fp = tmp_path / "a" / FP
    assert read_fp(tmp_path)["dirs"]["a"] == {
        "md5": md5(child_fp.read_bytes()),
        "size": child_fp.stat().st_size,
    }


def test_deep_nesting_chains_correctly(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a": {"b": {"c": {"d": {"file": b"deep"}}}}})
    run(tmp_path)
    for parent, child in [("", "a"), ("a", "b"), ("a/b", "c"), ("a/b/c", "d")]:
        child_fp = tmp_path / parent / child / FP
        entry = read_fp(tmp_path / parent)["dirs"][child]
        assert entry["md5"] == md5(child_fp.read_bytes())
        assert entry["size"] == child_fp.stat().st_size


# ---------------------------------------------------------------------------
# Paths the tool must ignore
# ---------------------------------------------------------------------------


def test_fingerprint_file_is_not_listed_in_itself(tmp_path: Path) -> None:
    (tmp_path / FP).write_text("pre-existing junk")
    make_tree(tmp_path, {"real.txt": b"r"})
    run(tmp_path)
    files = read_fp(tmp_path)["files"]
    assert FP not in files
    assert "real.txt" in files


def test_leftover_tmp_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / (FP + ".tmp")).write_text("crash leftover")
    make_tree(tmp_path, {"real.txt": b"r"})
    run(tmp_path)
    assert (FP + ".tmp") not in read_fp(tmp_path)["files"]


def test_sync_sidecar_files_are_ignored(tmp_path: Path) -> None:
    """Files ending in .~sync~ are mid-transfer sidecars from the sync tool
    and must never appear in a .fingerprint.
    """
    make_tree(
        tmp_path,
        {
            "real.txt": b"real",
            "real.txt" + SYNC_SUFFIX: b"partial upload",
            "deep" + SYNC_SUFFIX: b"x",
        },
    )
    run(tmp_path)
    files = read_fp(tmp_path)["files"]
    assert "real.txt" in files
    assert ("real.txt" + SYNC_SUFFIX) not in files
    assert ("deep" + SYNC_SUFFIX) not in files


def test_sync_sidecar_in_subdirectory_is_ignored(tmp_path: Path) -> None:
    make_tree(tmp_path, {"sub": {"x.txt": b"x", "y" + SYNC_SUFFIX: b"junk"}})
    run(tmp_path)
    sub_files = read_fp(tmp_path / "sub")["files"]
    assert "x.txt" in sub_files
    assert ("y" + SYNC_SUFFIX) not in sub_files


def test_no_tmp_file_remains_after_successful_run(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a": b"a", "sub": {"b": b"b"}})
    run(tmp_path)
    assert list(tmp_path.rglob(FP + ".tmp")) == []


def test_symlink_to_file_is_skipped(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("data")
    os.symlink(target, tmp_path / "link.txt")
    run(tmp_path)
    data = read_fp(tmp_path)
    assert "target.txt" in data["files"]
    assert "link.txt" not in data["files"]
    assert "link.txt" not in data["dirs"]


def test_symlink_to_directory_is_skipped(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "file").write_text("x")
    os.symlink(real, tmp_path / "link", target_is_directory=True)
    run(tmp_path)
    data = read_fp(tmp_path)
    assert "real" in data["dirs"]
    assert "link" not in data["dirs"]
    assert "link" not in data["files"]


# ---------------------------------------------------------------------------
# --full vs --incremental: when does md5 get reused vs recomputed?
# ---------------------------------------------------------------------------


def test_incremental_reuses_md5_when_size_unchanged(tmp_path: Path) -> None:
    """Poison the stored md5 with a sentinel and check it survives a size-stable
    incremental run. If we had rehashed, the sentinel would be gone.
    """
    make_tree(tmp_path, {"a.txt": b"hello"})  # actual size 5
    sentinel = "deadbeef" * 4
    (tmp_path / FP).write_text(
        json.dumps({"files": {"a.txt": {"md5": sentinel, "size": 5}}, "dirs": {}})
    )

    run(tmp_path, "--incremental")
    assert read_fp(tmp_path)["files"]["a.txt"] == {"md5": sentinel, "size": 5}


def test_incremental_recomputes_when_size_changes(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a.txt": b"hello"})
    run(tmp_path)
    new_bytes = b"a much longer payload than before"
    (tmp_path / "a.txt").write_bytes(new_bytes)
    run(tmp_path, "--incremental")
    assert read_fp(tmp_path)["files"]["a.txt"] == {
        "md5": md5(new_bytes),
        "size": len(new_bytes),
    }


def test_full_recomputes_even_when_size_matches(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a.txt": b"hello"})
    sentinel = "deadbeef" * 4
    (tmp_path / FP).write_text(
        json.dumps({"files": {"a.txt": {"md5": sentinel, "size": 5}}, "dirs": {}})
    )
    run(tmp_path, "--full")
    assert read_fp(tmp_path)["files"]["a.txt"]["md5"] == md5(b"hello")


def test_incremental_adds_new_file(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a.txt": b"a"})
    run(tmp_path)
    (tmp_path / "b.txt").write_bytes(b"new")
    run(tmp_path, "--incremental")
    files = read_fp(tmp_path)["files"]
    assert set(files) == {"a.txt", "b.txt"}
    assert files["b.txt"] == {"md5": md5(b"new"), "size": 3}


def test_incremental_adds_new_subdirectory(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a.txt": b"a"})
    run(tmp_path)
    make_tree(tmp_path, {"sub": {"x": b"x"}})
    run(tmp_path, "--incremental")
    assert "sub" in read_fp(tmp_path)["dirs"]
    assert (tmp_path / "sub" / FP).is_file()


def test_subdirectory_change_propagates_to_parent_fingerprint(
    tmp_path: Path,
) -> None:
    make_tree(tmp_path, {"sub": {"x.txt": b"x"}})
    run(tmp_path)
    parent_before = read_fp(tmp_path)["dirs"]["sub"]

    (tmp_path / "sub" / "x.txt").write_bytes(b"a different payload")
    run(tmp_path, "--incremental")
    parent_after = read_fp(tmp_path)["dirs"]["sub"]
    assert parent_after != parent_before
    child_fp = tmp_path / "sub" / FP
    assert parent_after == {
        "md5": md5(child_fp.read_bytes()),
        "size": child_fp.stat().st_size,
    }


# ---------------------------------------------------------------------------
# Preservation of removed entries (default), and --prune
# ---------------------------------------------------------------------------


def test_removed_file_preserved_by_default_incremental(tmp_path: Path) -> None:
    make_tree(tmp_path, {"keep.txt": b"k", "gone.txt": b"gone"})
    run(tmp_path)
    gone_entry = read_fp(tmp_path)["files"]["gone.txt"]

    (tmp_path / "gone.txt").unlink()
    run(tmp_path, "--incremental")
    data = read_fp(tmp_path)
    assert data["files"]["gone.txt"] == gone_entry
    assert "keep.txt" in data["files"]


def test_removed_file_preserved_by_default_full(tmp_path: Path) -> None:
    """Preservation must work in --full too, even though --full does not reuse
    md5 for files that exist.
    """
    make_tree(tmp_path, {"keep.txt": b"k", "gone.txt": b"gone"})
    run(tmp_path)
    gone_entry = read_fp(tmp_path)["files"]["gone.txt"]

    (tmp_path / "gone.txt").unlink()
    run(tmp_path, "--full")
    assert read_fp(tmp_path)["files"]["gone.txt"] == gone_entry


def test_removed_subdirectory_preserved_by_default(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a": {"x.txt": b"x"}, "b": {"y.txt": b"y"}})
    run(tmp_path)
    b_entry = read_fp(tmp_path)["dirs"]["b"]

    shutil.rmtree(tmp_path / "b")
    run(tmp_path, "--incremental")
    data = read_fp(tmp_path)
    assert data["dirs"]["b"] == b_entry
    assert "a" in data["dirs"]


def test_preserved_entries_keep_parent_fingerprint_stable(tmp_path: Path) -> None:
    """If nothing on disk changes meaningfully, the parent's md5 should not
    drift just because a child was deleted: the preserved-entry path must
    write a byte-identical .fingerprint.
    """
    make_tree(tmp_path, {"sub": {"x": b"x", "y": b"y"}})
    run(tmp_path)
    sub_fp_before = (tmp_path / "sub" / FP).read_bytes()

    (tmp_path / "sub" / "y").unlink()
    run(tmp_path, "--incremental")  # no --prune
    assert (tmp_path / "sub" / FP).read_bytes() == sub_fp_before


def test_prune_drops_removed_file(tmp_path: Path) -> None:
    make_tree(tmp_path, {"keep.txt": b"k", "gone.txt": b"g"})
    run(tmp_path)
    (tmp_path / "gone.txt").unlink()
    run(tmp_path, "--incremental", "--prune")
    files = read_fp(tmp_path)["files"]
    assert "gone.txt" not in files
    assert "keep.txt" in files


def test_prune_drops_removed_subdirectory(tmp_path: Path) -> None:
    make_tree(tmp_path, {"a": {"x": b"x"}, "b": {"y": b"y"}})
    run(tmp_path)
    shutil.rmtree(tmp_path / "b")
    run(tmp_path, "--incremental", "--prune")
    dirs = read_fp(tmp_path)["dirs"]
    assert "a" in dirs
    assert "b" not in dirs


def test_prune_with_full_mode(tmp_path: Path) -> None:
    make_tree(tmp_path, {"keep.txt": b"k", "gone.txt": b"g"})
    run(tmp_path)
    (tmp_path / "gone.txt").unlink()
    run(tmp_path, "--full", "--prune")
    assert "gone.txt" not in read_fp(tmp_path)["files"]


# ---------------------------------------------------------------------------
# Name collisions: file/dir type changes
# ---------------------------------------------------------------------------


def test_file_replaced_by_directory(tmp_path: Path) -> None:
    (tmp_path / "thing").write_text("was a file")
    run(tmp_path)
    (tmp_path / "thing").unlink()
    make_tree(tmp_path, {"thing": {"inner": b"i"}})
    run(tmp_path, "--incremental")
    data = read_fp(tmp_path)
    assert "thing" in data["dirs"]
    assert "thing" not in data["files"]


def test_directory_replaced_by_file(tmp_path: Path) -> None:
    make_tree(tmp_path, {"thing": {"inner": b"i"}})
    run(tmp_path)
    shutil.rmtree(tmp_path / "thing")
    (tmp_path / "thing").write_text("now a file")
    run(tmp_path, "--incremental")
    data = read_fp(tmp_path)
    assert "thing" in data["files"]
    assert "thing" not in data["dirs"]


# ---------------------------------------------------------------------------
# Robustness against corrupt or malformed .fingerprint inputs
# ---------------------------------------------------------------------------


def test_corrupt_existing_fingerprint_treated_as_empty(tmp_path: Path) -> None:
    (tmp_path / FP).write_text("not json at all{{{")
    make_tree(tmp_path, {"a.txt": b"a"})
    assert run(tmp_path, "--incremental") == 0
    assert read_fp(tmp_path)["files"]["a.txt"]["md5"] == md5(b"a")


def test_existing_fingerprint_with_missing_keys(tmp_path: Path) -> None:
    (tmp_path / FP).write_text(json.dumps({"other": "stuff"}))
    make_tree(tmp_path, {"a.txt": b"a"})
    assert run(tmp_path, "--incremental") == 0
    data = read_fp(tmp_path)
    assert data["files"]["a.txt"]["md5"] == md5(b"a")
    assert data["dirs"] == {}


def test_existing_entry_with_wrong_type_forces_rehash(tmp_path: Path) -> None:
    bad = {
        "files": {"good.txt": {"md5": "abc", "size": "five"}},  # size is str
        "dirs": {},
    }
    (tmp_path / FP).write_text(json.dumps(bad))
    make_tree(tmp_path, {"good.txt": b"hello"})
    run(tmp_path, "--incremental")
    assert read_fp(tmp_path)["files"]["good.txt"]["md5"] == md5(b"hello")


def test_invalid_prior_entry_is_not_preserved(tmp_path: Path) -> None:
    bad = {
        "files": {"gone.txt": {"md5": 42, "size": 5}},  # md5 is int
        "dirs": {},
    }
    (tmp_path / FP).write_text(json.dumps(bad))
    run(tmp_path, "--incremental")  # no --prune
    assert "gone.txt" not in read_fp(tmp_path)["files"]


# ---------------------------------------------------------------------------
# CLI argument handling
# ---------------------------------------------------------------------------


def test_nonexistent_directory_returns_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "does-not-exist"
    assert fingerprint.main([str(missing)]) == 1
    assert "is not a directory" in capsys.readouterr().err


def test_file_as_argument_returns_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "afile"
    f.write_text("x")
    assert fingerprint.main([str(f)]) == 1
    assert "is not a directory" in capsys.readouterr().err


def test_full_and_incremental_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        fingerprint.main([str(tmp_path), "--full", "--incremental"])


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------


def test_fingerprint_json_is_sorted_and_newline_terminated(tmp_path: Path) -> None:
    make_tree(tmp_path, {"z": b"z", "a": b"a", "m": b"m"})
    run(tmp_path)
    text = (tmp_path / FP).read_text()
    assert text.endswith("\n")
    assert list(json.loads(text)["files"].keys()) == ["a", "m", "z"]


def test_filenames_with_spaces_and_unicode(tmp_path: Path) -> None:
    make_tree(
        tmp_path,
        {
            "name with spaces.txt": b"a",
            "café": b"b",
            "snowman ☃.txt": b"c",
        },
    )
    run(tmp_path)
    files = read_fp(tmp_path)["files"]
    assert {"name with spaces.txt", "café", "snowman ☃.txt"} <= set(files)
