#!/usr/bin/env python3
"""Synchronize a local directory tree with a remote one over SSH.

Comparison is driven entirely by the .fingerprint files written by
fingerprint.py; this tool never diffs file contents directly. Synchronization
is bidirectional and conservative:

  - Files in the local .fingerprint but absent from the remote one are pushed
    local -> remote.
  - Files in the remote .fingerprint but absent from the local one are pulled
    remote -> local.
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


def load_remote_fingerprint(conn: SSHConn, remote_dir: str) -> dict:
    fp_path = posixpath.join(remote_dir, FINGERPRINT_FILENAME)
    # Missing remote fingerprint is the normal case for an empty remote, so
    # treat any non-zero exit as "no fingerprint here" rather than an error.
    result = conn.run(f"cat -- {shlex.quote(fp_path)}", check=False)
    if result.returncode != 0:
        return fingerprint.empty_fingerprint()
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return fingerprint.empty_fingerprint()
    if not isinstance(data, dict):
        return fingerprint.empty_fingerprint()
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    dirs = data.get("dirs") if isinstance(data.get("dirs"), dict) else {}
    return {"files": files, "dirs": dirs}


# ---------------------------------------------------------------------------
# File transfer (via .~sync~ sidecar + rename)
# ---------------------------------------------------------------------------


def ensure_remote_dir(conn: SSHConn, remote_dir: str) -> None:
    conn.run(f"mkdir -p -- {shlex.quote(remote_dir)}")


def push_file(
    conn: SSHConn, local_file: Path, remote_dir: str, name: str, verbose: bool
) -> None:
    if not local_file.is_file():
        # Fingerprint listed it but the file is gone — not an error.
        if verbose:
            print(f"skip push (missing locally): {local_file}", file=sys.stderr)
        return
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
        return
    try:
        conn.run(f"mv -- {shlex.quote(remote_tmp)} {shlex.quote(remote_final)}")
    except subprocess.CalledProcessError as exc:
        print(
            f"warning: remote rename failed for {remote_final}: "
            f"{(exc.stderr or '').strip()}",
            file=sys.stderr,
        )
        conn.run(f"rm -f -- {shlex.quote(remote_tmp)}", check=False)
        return
    if verbose:
        print(f"push: {local_file} -> {conn.host}:{remote_final}", file=sys.stderr)


def pull_file(
    conn: SSHConn, remote_dir: str, name: str, local_dir: Path, verbose: bool
) -> None:
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
        return
    try:
        os.replace(local_tmp, local_final)
    except OSError as exc:
        print(f"warning: local rename failed for {local_final}: {exc}", file=sys.stderr)
        if local_tmp.exists():
            try:
                local_tmp.unlink()
            except OSError:
                pass
        return
    if verbose:
        print(f"pull: {conn.host}:{remote_path} -> {local_final}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Recursive sync
# ---------------------------------------------------------------------------


def _dir_entries_match(a: Optional[dict], b: Optional[dict]) -> bool:
    if not (a and b):
        return False
    return a.get("md5") == b.get("md5") and a.get("size") == b.get("size")


def sync_directory(
    conn: SSHConn,
    local_dir: Path,
    remote_dir: str,
    verbose: bool,
    refresher: Optional[RemoteFingerprinter] = None,
) -> None:
    """Sync one directory and recurse.

    When refresher is given, each directory's .fingerprint is refreshed on both
    sides as soon as that directory (and its children) finish, so an interrupted
    run still leaves every completed directory current. When it is None no
    fingerprints are written (the --no-refresh-after path).
    """
    local_fp = load_local_fingerprint(local_dir)
    remote_fp = load_remote_fingerprint(conn, remote_dir)

    local_files = local_fp.get("files", {})
    remote_files = remote_fp.get("files", {})
    local_dirs = local_fp.get("dirs", {})
    remote_dirs = remote_fp.get("dirs", {})

    for name in sorted(local_files):
        if name in remote_files:
            continue
        push_file(conn, local_dir / name, remote_dir, name, verbose)

    for name in sorted(remote_files):
        if name in local_files:
            continue
        pull_file(conn, remote_dir, name, local_dir, verbose)

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
        sync_directory(conn, sub_local, sub_remote, verbose, refresher)

    # Children are now current (recursed above, or identical-skipped) and any
    # files pulled into this directory sit on disk, so record this directory's
    # new state on both sides. Bottom-up + per-directory means an interrupted
    # run still leaves every finished directory's fingerprint usable next run.
    if refresher is not None:
        refresh_local_directory(local_dir, verbose)
        refresher.refresh_one(remote_dir)


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
