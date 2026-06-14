#!/usr/bin/env python3
"""Synchronize a local directory tree with a remote one over SSH.

Comparison is driven entirely by the .fingerprint files written by
fingerprint.py; this tool never diffs file contents directly. Synchronization
is bidirectional and conservative:

  - Files in the local .fingerprint but absent from the remote one are pushed
    local -> remote.
  - Files in the remote .fingerprint but absent from the local one are pulled
    remote -> local.
  - A local-only file and a remote-only file in the same directory with
    identical content (md5 + size) that is unique there look like a rename:
    they are reported and left in place, not copied to both sides. (For now
    detection only logs the pair; it performs no rename.)
  - Files whose name appears in BOTH fingerprints are left untouched, even if
    their md5/size differ. No file is ever overwritten.
  - A fingerprint entry whose backing file is missing on disk is silently
    skipped; use 'fingerprint.py --prune' to clean these up separately.
  - A subdirectory whose fingerprint pointer (md5 + size of the child
    .fingerprint file) matches on both sides is skipped without recursion.

Every transfer is written first to '<name>.~sync~' on the destination and
then renamed into place, so an interrupted run never leaves a half-written
file under its real name. fingerprint.py is configured to ignore .~sync~
sidecars.

Optional pre-sync step: --refresh runs the fingerprint tool (incremental) on
the local tree so any local changes are visible to the comparison.

Fingerprint refresh: by default each directory's .fingerprint is refreshed
incrementally on both sides as soon as that directory finishes (bottom-up), so
subsequent runs see the new state and an interrupted run still leaves every
completed directory current. Pass --no-refresh-after to write no fingerprints.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Optional

# Reuse constants and helpers from the sibling fingerprint module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fingerprint  # noqa: E402

FINGERPRINT_FILENAME = fingerprint.FINGERPRINT_FILENAME
SYNC_SUFFIX = fingerprint.SYNC_SUFFIX


# ---------------------------------------------------------------------------
# SSH transport
# ---------------------------------------------------------------------------


class SSHConn:
    """ssh + scp with a shared ControlMaster connection.

    All ssh/scp invocations route through the same OpenSSH control socket so
    only the first call pays connection-setup cost. close() tears the master
    down and removes the socket directory.
    """

    def __init__(self, host: str, port: Optional[int] = None) -> None:
        self.host = host
        self.port = port
        self._tmpdir = tempfile.mkdtemp(prefix="fpsync-")
        self.control_path = os.path.join(self._tmpdir, "cm")
        self._mux = [
            "-o", f"ControlPath={self.control_path}",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=300",
        ]

    def _ssh_args(self, cmd_str: str) -> list[str]:
        args = ["ssh", *self._mux]
        if self.port is not None:
            args += ["-p", str(self.port)]
        args += [self.host, cmd_str]
        return args

    def _scp_args(self, src: str, dst: str) -> list[str]:
        # -O forces the legacy SCP protocol on every OpenSSH version. We rely
        # on it because it has a single, predictable quoting rule: the remote
        # path is handed to the remote login shell, so callers shlex.quote the
        # remote half of any host:path argument. (SFTP-mode scp, the >=9.0
        # default, would instead treat those quotes as literal filename bytes.)
        # -T disables strict filename checking, which would otherwise reject
        # our quoted request with "protocol error: filename does not match
        # request" because the shell strips the quotes before echoing the name.
        args = ["scp", *self._mux, "-O", "-T", "-q"]
        if self.port is not None:
            args += ["-P", str(self.port)]
        args += [src, dst]
        return args

    def run(self, cmd_str: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._ssh_args(cmd_str),
            check=check,
            text=True,
            capture_output=True,
        )

    def upload(self, local: Path, remote: str) -> None:
        # Legacy-protocol scp (forced via -O in _scp_args) runs the remote half
        # through the remote login shell, so quote it or names containing
        # spaces, ';', '()', quotes, etc. get mangled by that shell.
        target = f"{self.host}:{shlex.quote(remote)}"
        subprocess.run(
            self._scp_args(str(local), target),
            check=True, text=True, capture_output=True,
        )

    def download(self, remote: str, local: Path) -> None:
        source = f"{self.host}:{shlex.quote(remote)}"
        subprocess.run(
            self._scp_args(source, str(local)),
            check=True, text=True, capture_output=True,
        )

    def run_with_stdin(self, cmd_str: str, stdin_text: str) -> None:
        subprocess.run(
            self._ssh_args(cmd_str),
            input=stdin_text,
            text=True,
            check=True,
        )

    def close(self) -> None:
        try:
            args = ["ssh", *self._mux]
            if self.port is not None:
                args += ["-p", str(self.port)]
            args += ["-O", "exit", self.host]
            subprocess.run(args, check=False, capture_output=True)
        finally:
            shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fingerprint loading
# ---------------------------------------------------------------------------


def load_local_fingerprint(directory: Path) -> dict:
    fp = directory / FINGERPRINT_FILENAME
    if not fp.is_file():
        return fingerprint.empty_fingerprint()
    return fingerprint.load_fingerprint(fp)


def load_remote_fingerprint(conn: SSHConn, remote_dir: str) -> tuple[dict, bool]:
    """Return (fingerprint, present).

    `present` is True only when a valid fingerprint was loaded. Missing,
    unreadable, or malformed remote fingerprints all yield an empty fingerprint
    with present=False, signalling the caller that this remote directory has no
    usable .fingerprint yet and one should be written.
    """
    fp_path = posixpath.join(remote_dir, FINGERPRINT_FILENAME)
    # Missing remote fingerprint is the normal case for an empty remote, so
    # treat any non-zero exit as "no fingerprint here" rather than an error.
    result = conn.run(f"cat -- {shlex.quote(fp_path)}", check=False)
    if result.returncode != 0:
        return fingerprint.empty_fingerprint(), False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return fingerprint.empty_fingerprint(), False
    if not isinstance(data, dict):
        return fingerprint.empty_fingerprint(), False
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    dirs = data.get("dirs") if isinstance(data.get("dirs"), dict) else {}
    return {"files": files, "dirs": dirs}, True


# ---------------------------------------------------------------------------
# File transfer (via .~sync~ sidecar + rename)
# ---------------------------------------------------------------------------


def ensure_remote_dir(conn: SSHConn, remote_dir: str) -> None:
    conn.run(f"mkdir -p -- {shlex.quote(remote_dir)}")


def push_file(
    conn: SSHConn, local_file: Path, remote_dir: str, name: str, verbose: bool
) -> bool:
    """Push one file local -> remote. Returns True iff the remote changed."""
    if not local_file.is_file():
        # Fingerprint listed it but the file is gone — not an error.
        if verbose:
            print(f"skip push (missing locally): {local_file}", file=sys.stderr)
        return False
    remote_tmp = posixpath.join(remote_dir, name + SYNC_SUFFIX)
    remote_final = posixpath.join(remote_dir, name)
    try:
        conn.upload(local_file, remote_tmp)
    except subprocess.CalledProcessError as exc:
        print(
            f"warning: upload failed: {local_file}: {(exc.stderr or '').strip()}",
            file=sys.stderr,
        )
        conn.run(f"rm -f -- {shlex.quote(remote_tmp)}", check=False)
        return False
    try:
        conn.run(f"mv -- {shlex.quote(remote_tmp)} {shlex.quote(remote_final)}")
    except subprocess.CalledProcessError as exc:
        print(
            f"warning: remote rename failed for {remote_final}: "
            f"{(exc.stderr or '').strip()}",
            file=sys.stderr,
        )
        conn.run(f"rm -f -- {shlex.quote(remote_tmp)}", check=False)
        return False
    if verbose:
        print(f"push: {local_file} -> {conn.host}:{remote_final}", file=sys.stderr)
    return True


def pull_file(
    conn: SSHConn, remote_dir: str, name: str, local_dir: Path, verbose: bool
) -> bool:
    """Pull one file remote -> local. Returns True iff the local tree changed."""
    local_tmp = local_dir / (name + SYNC_SUFFIX)
    local_final = local_dir / name
    remote_path = posixpath.join(remote_dir, name)
    try:
        conn.download(remote_path, local_tmp)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        # The remote file may have been removed since the fingerprint was
        # written — that's not an error.
        if "No such file" not in stderr:
            print(
                f"warning: download failed: {remote_path}: {stderr}",
                file=sys.stderr,
            )
        elif verbose:
            print(f"skip pull (missing remotely): {remote_path}", file=sys.stderr)
        if local_tmp.exists():
            try:
                local_tmp.unlink()
            except OSError:
                pass
        return False
    try:
        os.replace(local_tmp, local_final)
    except OSError as exc:
        print(f"warning: local rename failed for {local_final}: {exc}", file=sys.stderr)
        if local_tmp.exists():
            try:
                local_tmp.unlink()
            except OSError:
                pass
        return False
    if verbose:
        print(f"pull: {conn.host}:{remote_path} -> {local_final}", file=sys.stderr)
    return True


# ---------------------------------------------------------------------------
# Recursive sync
# ---------------------------------------------------------------------------


def _dir_entries_match(a: Optional[dict], b: Optional[dict]) -> bool:
    if not (a and b):
        return False
    return a.get("md5") == b.get("md5") and a.get("size") == b.get("size")


def _content_key(entry: dict) -> tuple:
    return (entry.get("md5"), entry.get("size"))


def detect_renames(
    local_files: dict, remote_files: dict
) -> list[tuple[str, str]]:
    """Find (local_name, remote_name) pairs that look like a rename.

    A pair qualifies when the local file is absent remotely, the remote file is
    absent locally, the two share identical content (md5 + size), and that
    content is unique within the directory on both sides (no other file — on
    either side — has it). Returned sorted by local name.
    """
    local_counts = Counter(_content_key(e) for e in local_files.values())
    remote_counts = Counter(_content_key(e) for e in remote_files.values())
    remote_only_by_content = {
        _content_key(e): n
        for n, e in remote_files.items()
        if n not in local_files
    }

    renames: list[tuple[str, str]] = []
    for lname in sorted(local_files):
        if lname in remote_files:
            continue
        key = _content_key(local_files[lname])
        # Content must appear exactly once on each side for the match to be
        # unambiguous; that single remote file is then necessarily remote-only.
        if local_counts[key] != 1 or remote_counts[key] != 1:
            continue
        rname = remote_only_by_content.get(key)
        if rname is None:
            continue
        renames.append((lname, rname))
    return renames


def sync_directory(
    conn: SSHConn,
    local_dir: Path,
    remote_dir: str,
    verbose: bool,
    refresher: Optional[RemoteFingerprinter] = None,
) -> tuple[bool, bool]:
    """Sync one directory and recurse.

    Returns (local_changed, remote_changed) for this directory's whole subtree:
    pulls dirty the local side, pushes dirty the remote side, and a changed
    child dirties both ancestors' dir pointers on the side that changed.

    When refresher is given, each directory's .fingerprint is refreshed as soon
    as that directory (and its children) finish — on the side that actually
    changed, or on a side that has no usable .fingerprint yet (so a freshly
    created, even empty, directory still gets one). A pull-only side whose
    fingerprint is already current costs no refresh. Writing a .fingerprint
    counts as a change to that side, so the returned flag dirties the parent's
    pointer. Bottom-up + per-directory means an interrupted run still leaves
    every finished directory current. When refresher is None no fingerprints are
    written (the --no-refresh-after path).
    """
    print(f"Starting sync {local_dir}", file=sys.stderr, flush=True, end="")
    local_fp = load_local_fingerprint(local_dir)
    local_fp_present = (local_dir / FINGERPRINT_FILENAME).is_file()
    remote_fp, remote_fp_present = load_remote_fingerprint(conn, remote_dir)

    local_files = local_fp.get("files", {})
    remote_files = remote_fp.get("files", {})
    local_dirs = local_fp.get("dirs", {})
    remote_dirs = remote_fp.get("dirs", {})

    local_changed = False
    remote_changed = False

    # A file that was merely renamed appears as local-only on one side and
    # remote-only on the other with identical content. Detect those pairs and
    # leave them in place (for now we only report them) instead of copying the
    # content to both sides under both names.
    renamed_local: set[str] = set()
    renamed_remote: set[str] = set()
    for lname, rname in detect_renames(local_files, remote_files):
        renamed_local.add(lname)
        renamed_remote.add(rname)
        print(
            f"rename: {local_dir / lname} <-> {posixpath.join(remote_dir, rname)}",
            file=sys.stderr,
        )

    for name in sorted(local_files):
        if name in remote_files or name in renamed_local:
            continue
        if push_file(conn, local_dir / name, remote_dir, name, verbose):
            remote_changed = True

    for name in sorted(remote_files):
        if name in local_files or name in renamed_remote:
            continue
        if pull_file(conn, remote_dir, name, local_dir, verbose):
            local_changed = True

    print(f"Subdir processing", file=sys.stderr, flush=True)
    for name in sorted(set(local_dirs) | set(remote_dirs)):
        if name in local_files or name in remote_files:
            # The same name appears as a file on one side and a directory on
            # the other. Skip rather than guess which side wins.
            print(
                f"warning: skipping {name}: type conflict (file vs directory)",
                file=sys.stderr,
            )
            continue

        if _dir_entries_match(local_dirs.get(name), remote_dirs.get(name)):
            if verbose:
                print(
                    f"skip identical subtree: {local_dir / name}",
                    file=sys.stderr,
                )
            continue

        sub_local = local_dir / name
        sub_remote = posixpath.join(remote_dir, name)
        try:
            sub_local.mkdir(exist_ok=True)
        except OSError as exc:
            print(f"warning: cannot create {sub_local}: {exc}", file=sys.stderr)
            continue
        try:
            ensure_remote_dir(conn, sub_remote)
        except subprocess.CalledProcessError as exc:
            print(
                f"warning: cannot create remote dir {sub_remote}: "
                f"{(exc.stderr or '').strip()}",
                file=sys.stderr,
            )
            continue
        sub_local_changed, sub_remote_changed = sync_directory(
            conn, sub_local, sub_remote, verbose, refresher
        )
        local_changed = local_changed or sub_local_changed
        remote_changed = remote_changed or sub_remote_changed

    # Record this directory's new state, but only on the side that needs it: a
    # changed child (or a pull/push here) dirties that side's dir pointer, and a
    # side with no usable .fingerprint yet (e.g. a freshly-created remote dir,
    # including an empty one that received no pushes) must get one written so the
    # parent can record a matching pointer and skip the subtree next run. Writing
    # a .fingerprint changes it, so we report that side as changed to dirty the
    # parent's pointer. An untouched side that already has a current fingerprint
    # needs no work. Refreshing bottom-up + per-directory leaves every finished
    # directory usable even if the run is interrupted.
    if refresher is not None:
        if local_changed or not local_fp_present:
            refresh_local_directory(local_dir, verbose)
            local_changed = True
        if remote_changed or not remote_fp_present:
            refresher.refresh_one(remote_dir)
            remote_changed = True

    return local_changed, remote_changed


# ---------------------------------------------------------------------------
# Pre/post fingerprint refresh
# ---------------------------------------------------------------------------


def refresh_local_fingerprints(local_dir: Path, verbose: bool) -> None:
    """Recursively refresh every local .fingerprint under local_dir."""
    if verbose:
        print(f"refresh local fingerprints under {local_dir}", file=sys.stderr)
    fingerprint.process_directory(
        local_dir, full_recompute=False, prune=False, verbose=verbose
    )


def refresh_local_directory(local_dir: Path, verbose: bool) -> None:
    """Refresh just this one local directory's .fingerprint (no recursion).

    Children's .fingerprint files are assumed already current (sync_directory
    refreshes bottom-up), so this only rehashes files newly landed in this
    directory and re-reads the children's pointers.
    """
    fingerprint.process_directory(
        local_dir, full_recompute=False, prune=False, verbose=verbose,
        recurse=False,
    )


class RemoteFingerprinter:
    """Refresh individual remote directories' .fingerprint files on demand.

    fingerprint.py is streamed to a temp file on the remote once (lazily, on
    the first refresh), then invoked per directory with --incremental
    --no-recurse so each call only rehashes the files just transferred into
    that one directory. cleanup() removes the deployed script. This avoids
    assuming fingerprint.py is already present on the remote and avoids
    re-streaming it for every directory.
    """

    def __init__(self, conn: SSHConn, verbose: bool = False) -> None:
        self.conn = conn
        self.verbose = verbose
        self._remote_script: Optional[str] = None

    def _ensure_deployed(self) -> None:
        if self._remote_script is not None:
            return
        script = (Path(__file__).resolve().parent / "fingerprint.py").read_text(
            encoding="utf-8"
        )
        path = self.conn.run("mktemp").stdout.strip()
        self.conn.run_with_stdin(f"cat > {shlex.quote(path)}", script)
        self._remote_script = path

    def refresh_one(self, remote_dir: str) -> None:
        try:
            self._ensure_deployed()
            cmd = (
                f"python3 {shlex.quote(self._remote_script)} "
                f"{shlex.quote(remote_dir)} --incremental --no-recurse"
            )
            if self.verbose:
                print(f"refresh remote fingerprint: {remote_dir}", file=sys.stderr)
            self.conn.run(cmd)
        except subprocess.CalledProcessError as exc:
            print(
                f"warning: remote fingerprint refresh failed for {remote_dir}: "
                f"{(exc.stderr or '').strip()}",
                file=sys.stderr,
            )

    def cleanup(self) -> None:
        if self._remote_script is not None:
            self.conn.run(f"rm -f -- {shlex.quote(self._remote_script)}", check=False)
            self._remote_script = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize a local directory tree with a remote one over SSH, "
            "using .fingerprint files for comparison. Bidirectional, "
            "non-overwriting: only files missing on one side are copied from "
            "the other."
        ),
    )
    parser.add_argument("local_dir", type=Path, help="Local directory to sync.")
    parser.add_argument("host", help="Remote SSH host (may be 'user@host').")
    parser.add_argument("remote_dir", help="Remote directory to sync.")
    parser.add_argument(
        "-p", "--port", type=int, default=None,
        help="Remote SSH port (defaults to whatever ssh would normally use).",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Refresh local .fingerprint files (incremental) before syncing.",
    )
    parser.add_argument(
        "--no-refresh-after", action="store_true",
        help=(
            "Do not write .fingerprint files on either side during the sync. By "
            "default each directory is refreshed incrementally on both sides as "
            "soon as it finishes, so subsequent runs (and interrupted ones) see "
            "the new state."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    local_dir = args.local_dir.resolve()
    if not local_dir.is_dir():
        print(f"error: {local_dir} is not a directory", file=sys.stderr)
        return 1

    if args.refresh:
        refresh_local_fingerprints(local_dir, args.verbose)

    conn = SSHConn(args.host, port=args.port)
    refresher = (
        None if args.no_refresh_after else RemoteFingerprinter(conn, args.verbose)
    )
    try:
        try:
            ensure_remote_dir(conn, args.remote_dir)
        except subprocess.CalledProcessError as exc:
            print(
                f"error: cannot create remote dir {args.remote_dir}: "
                f"{(exc.stderr or '').strip()}",
                file=sys.stderr,
            )
            return 1
        sync_directory(conn, local_dir, args.remote_dir, args.verbose, refresher)
    finally:
        if refresher is not None:
            refresher.cleanup()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
