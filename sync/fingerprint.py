#!/usr/bin/env python3
"""Write .fingerprint files describing every directory under a root.

Each .fingerprint file is JSON of the form:

    {
      "files": { "name": {"md5": "...", "size": 123}, ... },
      "dirs":  { "name": {"md5": "...", "size": 456}, ... }
    }

For files the md5/size describe the file itself. For subdirectories the
md5/size describe that subdirectory's own .fingerprint file. Subdirectories
are processed before their parent, so a parent's .fingerprint always reflects
the freshly-written children.

Entries for files or subdirectories that no longer exist on disk are kept
verbatim from the previous .fingerprint, unless --prune is passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

FINGERPRINT_FILENAME = ".fingerprint"
TMP_SUFFIX = ".tmp"
# Sidecar suffix used by the sync tool while a file is being copied. Excluded
# here so half-written transfers don't pollute the fingerprint.
SYNC_SUFFIX = ".~sync~"
CHUNK_SIZE = 1024 * 1024  # 1 MiB

# Files written by this tool that should never appear inside a fingerprint.
RESERVED_NAMES = {FINGERPRINT_FILENAME, FINGERPRINT_FILENAME + TMP_SUFFIX}


def _is_reserved(name: str) -> bool:
    return name in RESERVED_NAMES or name.endswith(SYNC_SUFFIX)

Entry = Dict[str, object]  # {"md5": str, "size": int}
Fingerprint = Dict[str, Dict[str, Entry]]  # {"files": {...}, "dirs": {...}}


def empty_fingerprint() -> Fingerprint:
    return {"files": {}, "dirs": {}}


def md5_of_file(path: Path, on_chunk: Optional[Callable[[int], None]] = None) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
            if on_chunk is not None:
                on_chunk(len(chunk))
    return h.hexdigest()


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


def _ellipsize_left(s: str, max_len: int) -> str:
    if max_len <= 3 or len(s) <= max_len:
        return s
    return "..." + s[-(max_len - 3):]


class _NullReporter:
    def note_directory(self, path: Path) -> None: ...
    def note_bytes(self, n: int) -> None: ...
    def note_file(self) -> None: ...
    def close(self) -> None: ...


class HeartbeatReporter:
    """In-place stderr progress line, refreshed at most every INTERVAL seconds.

    Updates are driven from two callsites: note_directory() when entering a
    new directory, and note_bytes() from inside md5_of_file's chunk loop so a
    single huge file still produces liveness output mid-hash.
    """

    INTERVAL = 1.0

    def __init__(self, stream=sys.stderr) -> None:
        self.stream = stream
        self._files = 0
        self._bytes = 0
        self._current_dir: Optional[Path] = None
        self._last_emit = time.monotonic()  # avoids a tick on the very first call
        self._emitted_anything = False

    def note_directory(self, path: Path) -> None:
        self._current_dir = path
        self._maybe_emit()

    def note_bytes(self, n: int) -> None:
        self._bytes += n
        self._maybe_emit()

    def note_file(self) -> None:
        self._files += 1

    def _maybe_emit(self) -> None:
        now = time.monotonic()
        if now - self._last_emit < self.INTERVAL:
            return
        self._last_emit = now
        try:
            cols = os.get_terminal_size(self.stream.fileno()).columns
        except (OSError, ValueError):
            cols = 80
        head = f"  hashing: {self._files:,} files, {_fmt_bytes(self._bytes)}"
        if self._current_dir is not None:
            tail = "  " + _ellipsize_left(str(self._current_dir), max(10, cols - len(head) - 2))
        else:
            tail = ""
        msg = (head + tail).ljust(cols - 1)[: cols - 1]
        self.stream.write("\r" + msg)
        self.stream.flush()
        self._emitted_anything = True

    def close(self) -> None:
        if self._emitted_anything:
            self.stream.write("\n")
            self.stream.flush()


def load_fingerprint(path: Path) -> Fingerprint:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return empty_fingerprint()
    if not isinstance(data, dict):
        return empty_fingerprint()
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    dirs = data.get("dirs") if isinstance(data.get("dirs"), dict) else {}
    return {"files": files, "dirs": dirs}


def save_fingerprint(path: Path, data: Fingerprint) -> None:
    tmp = path.with_name(path.name + TMP_SUFFIX)
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def md5_and_size(path: Path) -> Tuple[str, int]:
    size = path.stat().st_size
    return md5_of_file(path), size


def _valid_entry(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("md5"), str)
        and isinstance(value.get("size"), int)
    )


def process_directory(
    directory: Path,
    full_recompute: bool,
    prune: bool,
    verbose: bool,
    reporter: Optional[object] = None,
) -> None:
    """Depth-first: write children's .fingerprint first, then this dir's."""

    if reporter is None:
        reporter = _NullReporter()
    reporter.note_directory(directory)

    fingerprint_path = directory / FINGERPRINT_FILENAME
    # Always load existing entries: even in --full mode they hold the md5/size
    # for files and dirs that no longer exist on disk, which we preserve by
    # default.
    existing = load_fingerprint(fingerprint_path)

    try:
        entries = list(os.scandir(directory))
    except (PermissionError, FileNotFoundError) as exc:
        print(f"warning: cannot read {directory}: {exc}", file=sys.stderr)
        return

    file_entries = []
    dir_entries = []
    present_names: set[str] = set()
    for entry in entries:
        if _is_reserved(entry.name):
            continue
        try:
            if entry.is_symlink():
                # Skip symlinks; including them would require deciding whether
                # to hash the link target or the link itself, and either choice
                # has surprising semantics for a fingerprint tool.
                continue
            if entry.is_dir(follow_symlinks=False):
                dir_entries.append(entry)
                present_names.add(entry.name)
            elif entry.is_file(follow_symlinks=False):
                file_entries.append(entry)
                present_names.add(entry.name)
        except OSError as exc:
            print(f"warning: cannot stat {entry.path}: {exc}", file=sys.stderr)

    new_dirs: Dict[str, Entry] = {}
    for entry in dir_entries:
        sub_path = Path(entry.path)
        process_directory(sub_path, full_recompute, prune, verbose, reporter)
        # Re-assert the current directory after the recursion bubbles back.
        reporter.note_directory(directory)
        sub_fp = sub_path / FINGERPRINT_FILENAME
        try:
            size = sub_fp.stat().st_size
            md5 = md5_of_file(sub_fp, on_chunk=reporter.note_bytes)
            reporter.note_file()
            new_dirs[entry.name] = {"md5": md5, "size": size}
        except (FileNotFoundError, PermissionError) as exc:
            print(f"warning: cannot read {sub_fp}: {exc}", file=sys.stderr)

    new_files: Dict[str, Entry] = {}
    for entry in file_entries:
        name = entry.name
        try:
            size = entry.stat(follow_symlinks=False).st_size
        except OSError as exc:
            print(f"warning: cannot stat {entry.path}: {exc}", file=sys.stderr)
            continue

        prior = existing["files"].get(name) if not full_recompute else None
        if prior is not None and _valid_entry(prior) and prior["size"] == size:
            md5 = prior["md5"]
        else:
            try:
                md5 = md5_of_file(Path(entry.path), on_chunk=reporter.note_bytes)
            except (PermissionError, FileNotFoundError, OSError) as exc:
                print(f"warning: cannot hash {entry.path}: {exc}", file=sys.stderr)
                continue
            reporter.note_file()
        new_files[name] = {"md5": md5, "size": size}

    preserved = 0
    if not prune:
        for name, prior in existing["files"].items():
            if name in present_names or name in new_files or name in new_dirs:
                continue
            if _valid_entry(prior):
                new_files[name] = prior
                preserved += 1
        for name, prior in existing["dirs"].items():
            if name in present_names or name in new_files or name in new_dirs:
                continue
            if _valid_entry(prior):
                new_dirs[name] = prior
                preserved += 1

    save_fingerprint(fingerprint_path, {"files": new_files, "dirs": new_dirs})
    if verbose:
        extra = f", {preserved} preserved" if preserved else ""
        print(
            f"{directory}: {len(new_files)} files, {len(new_dirs)} dirs{extra}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write .fingerprint files for every directory under ROOT."
    )
    parser.add_argument("directory", type=Path, help="Root directory to fingerprint")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--full",
        dest="full",
        action="store_true",
        help="Recompute every md5, ignoring existing .fingerprint files (default).",
    )
    mode.add_argument(
        "--incremental",
        dest="full",
        action="store_false",
        help=(
            "Load existing .fingerprint files and only rehash files whose size "
            "changed; add new files and drop missing ones."
        ),
    )
    parser.set_defaults(full=True)
    parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "Drop entries from .fingerprint for files and subdirectories that "
            "are no longer on disk. By default such entries are preserved."
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Log each directory as it is written."
    )
    args = parser.parse_args(argv)

    root: Path = args.directory
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    # Show a live heartbeat on an interactive terminal. Suppressed under -v
    # (which already prints per-directory lines, and would visually fight
    # with the in-place \r line).
    reporter: object
    if not args.verbose and sys.stderr.isatty():
        reporter = HeartbeatReporter()
    else:
        reporter = _NullReporter()

    try:
        process_directory(
            root,
            full_recompute=args.full,
            prune=args.prune,
            verbose=args.verbose,
            reporter=reporter,
        )
    finally:
        reporter.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
