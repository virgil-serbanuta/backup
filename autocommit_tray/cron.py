from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config as config_mod

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_LOCKED = 2
EXIT_CONFIG_ERROR = 3

LOCK_FILENAME = ".cron.lock"
RUNNING_MARKER = ".cron.running"  # contains the name of the prefix currently running


def lock_path(log_dir: Path) -> Path:
    return log_dir / LOCK_FILENAME


def running_marker_path(log_dir: Path) -> Path:
    return log_dir / RUNNING_MARKER


@contextmanager
def _try_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _today_str() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M")


def _has_success_today(log_dir: Path, prefix: str) -> bool:
    today = _today_str()
    for p in log_dir.glob(f"{prefix}-{today}-*.success"):
        if p.is_file():
            return True
    return False


def _scanner_cmd(directory: Path) -> list[str]:
    scanner = Path(__file__).resolve().parent.parent / "autocommit_scan.py"
    return [sys.executable, str(scanner), str(directory)]


def _run_scanner(directory: Path) -> tuple[int, str]:
    proc = subprocess.run(
        _scanner_cmd(directory),
        text=True,
        capture_output=True,
    )
    output = proc.stdout
    if proc.stderr:
        if output and not output.endswith("\n"):
            output += "\n"
        output += proc.stderr
    return proc.returncode, output


def _write_log(log_dir: Path, prefix: str, success: bool, body: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    suffix = "success" if success else "failure"
    path = log_dir / f"{prefix}-{_now_stamp()}.{suffix}"
    path.write_text(body, encoding="utf-8")
    return path


def _prune(log_dir: Path, retention_days: int) -> None:
    if not log_dir.is_dir():
        return
    cutoff = time.time() - retention_days * 86400
    for entry in log_dir.iterdir():
        if entry.name in {LOCK_FILENAME, RUNNING_MARKER}:
            continue
        if entry.suffix not in {".success", ".failure"}:
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def run(
    cfg: config_mod.Config,
    only_prefix: str | None = None,
    force: bool = False,
) -> int:
    log_dir = cfg.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    with _try_lock(lock_path(log_dir)) as got:
        if not got:
            return EXIT_LOCKED

        targets = cfg.prefixes
        if only_prefix is not None:
            targets = [p for p in cfg.prefixes if p.prefix == only_prefix]
            if not targets:
                print(f"ERROR: prefix not found in config: {only_prefix}", file=sys.stderr)
                return EXIT_CONFIG_ERROR

        any_failure = False
        marker = running_marker_path(log_dir)
        try:
            for entry in targets:
                if not force and _has_success_today(log_dir, entry.prefix):
                    continue
                marker.write_text(entry.prefix, encoding="utf-8")
                rc, output = _run_scanner(entry.directory)
                ok = rc == 0
                _write_log(log_dir, entry.prefix, ok, output)
                if not ok:
                    any_failure = True
        finally:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass

        _prune(log_dir, cfg.log_retention_days)
        return EXIT_FAILURE if any_failure else EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run autocommit_scan for each configured prefix")
    parser.add_argument("config", type=Path, help="Path to config YAML")
    parser.add_argument("--prefix", help="Run only this prefix")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the skip-if-already-succeeded-today check",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    return run(cfg, only_prefix=args.prefix, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
