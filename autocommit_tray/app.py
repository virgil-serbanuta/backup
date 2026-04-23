from __future__ import annotations

import datetime as dt
import fcntl
import os
import platform
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pystray

from . import config as config_mod
from . import cron as cron_mod
from . import icons as icons_mod
from . import state as state_mod

REPO_ROOT = Path(__file__).resolve().parent.parent


def _instance_lock_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".cache")
    d = Path(base) / "autocommit-backup"
    d.mkdir(parents=True, exist_ok=True)
    return d / "tray.lock"


def acquire_singleton() -> int | None:
    path = _instance_lock_path()
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _open_path(path: Path) -> None:
    if platform.system() == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _notify(title: str, message: str) -> None:
    try:
        if platform.system() == "Darwin":
            script = f'display notification {shlex.quote(message)} with title {shlex.quote(title)}'
            subprocess.Popen(["osascript", "-e", script])
        else:
            subprocess.Popen(["notify-send", title, message])
    except FileNotFoundError:
        pass


class TrayApp:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.cfg = self._load_config()
        self.config_mtime = self._config_mtime()

        self._lock = threading.RLock()
        self._statuses: dict[str, state_mod.PrefixStatus] = {}
        self._running_prefix: str | None = None
        self._current_color: str | None = None
        self._last_notified_date: str | None = self._read_last_notified()
        self._stop = threading.Event()
        self._subprocesses: list[subprocess.Popen] = []

        self.icon = pystray.Icon(
            "autocommit-backup",
            icons_mod.get_icon("yellow"),
            "Autocommit Backup",
            menu=self._build_menu(),
        )

    # -------------------- config --------------------

    def _load_config(self) -> config_mod.Config:
        try:
            return config_mod.load(self.config_path)
        except config_mod.ConfigError as exc:
            _notify("Autocommit Backup", f"Config error: {exc}")
            return config_mod.Config.default()

    def _config_mtime(self) -> float:
        try:
            return self.config_path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def _last_notified_file(self) -> Path:
        return self.cfg.log_dir / ".last_notified"

    def _read_last_notified(self) -> str | None:
        try:
            return self._last_notified_file().read_text(encoding="utf-8").strip() or None
        except (FileNotFoundError, OSError):
            return None

    def _write_last_notified(self, date_str: str) -> None:
        path = self._last_notified_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(date_str, encoding="utf-8")

    # -------------------- polling --------------------

    def poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # don't let the loop die
                print(f"Tray poll error: {exc}", file=sys.stderr)
            self._stop.wait(max(5, self.cfg.poll_interval_seconds))

    def _tick(self) -> None:
        mtime = self._config_mtime()
        if mtime != self.config_mtime:
            self.cfg = self._load_config()
            self.config_mtime = mtime

        prefixes = [p.prefix for p in self.cfg.prefixes]
        statuses = state_mod.scan(self.cfg.log_dir, prefixes)
        running = state_mod.running_prefix(self.cfg.log_dir)
        color = state_mod.icon_color(statuses, running)
        tooltip = state_mod.format_tooltip(statuses, running)

        with self._lock:
            self._statuses = statuses
            self._running_prefix = running

        if color != self._current_color:
            self.icon.icon = icons_mod.get_icon(color)
            self._current_color = color
        self.icon.title = tooltip
        self.icon.menu = self._build_menu()
        self.icon.update_menu()

        self._maybe_notify(statuses)

    def _maybe_notify(self, statuses: dict[str, state_mod.PrefixStatus]) -> None:
        failed = state_mod.failed_prefixes(statuses)
        if not failed:
            return
        now = dt.datetime.now()
        today = now.strftime("%Y%m%d")
        if self._last_notified_date == today:
            return
        try:
            hh, mm = self.cfg.notification_time.split(":")
            target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except ValueError:
            return
        if now < target:
            return
        _notify(
            "Autocommit Backup — failure",
            f"Last cron run failed for: {', '.join(failed)}",
        )
        self._last_notified_date = today
        self._write_last_notified(today)

    # -------------------- menu --------------------

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                "Open last failing log",
                pystray.Menu(self._failing_log_items),
                enabled=lambda item: bool(self._failed()),
            ),
            pystray.MenuItem(
                "Re-run",
                pystray.Menu(self._rerun_items),
                enabled=lambda item: bool(self._failed()),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings…", self._on_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    def _failed(self) -> list[str]:
        with self._lock:
            return state_mod.failed_prefixes(self._statuses)

    def _failing_log_items(self) -> list[pystray.MenuItem]:
        with self._lock:
            items: list[pystray.MenuItem] = []
            for prefix in state_mod.failed_prefixes(self._statuses):
                path = self._statuses[prefix].latest_path
                items.append(
                    pystray.MenuItem(
                        prefix,
                        (lambda p=path: _open_path(p)) if path else (lambda: None),
                    )
                )
            return items

    def _rerun_items(self) -> list[pystray.MenuItem]:
        with self._lock:
            failed = state_mod.failed_prefixes(self._statuses)
        items: list[pystray.MenuItem] = [
            pystray.MenuItem(p, lambda _i, _it, p=p: self._rerun(p)) for p in failed
        ]
        if len(failed) > 1:
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("All failed", lambda _i, _it: self._rerun_all(failed)))
        return items

    # -------------------- actions --------------------

    def _rerun(self, prefix: str) -> None:
        self._spawn_cron(["--prefix", prefix, "--force"], context=prefix)

    def _rerun_all(self, prefixes: list[str]) -> None:
        for p in prefixes:
            self._rerun(p)

    def _spawn_cron(self, extra_args: list[str], context: str) -> None:
        cmd = [sys.executable, "-m", "autocommit_tray.cron", str(self.config_path), *extra_args]
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._subprocesses.append(proc)
        threading.Thread(target=self._watch_subprocess, args=(proc, context), daemon=True).start()

    def _watch_subprocess(self, proc: subprocess.Popen, context: str) -> None:
        rc = proc.wait()
        if rc == cron_mod.EXIT_LOCKED:
            _notify(
                "Autocommit Backup",
                f"Re-run of '{context}' skipped: another backup run is already in progress.",
            )
        self._tick()

    def _on_settings(self, _icon, _item) -> None:
        subprocess.Popen(
            [sys.executable, "-m", "autocommit_tray.settings", str(self.config_path)],
            cwd=str(REPO_ROOT),
        )

    def _on_quit(self, _icon, _item) -> None:
        self._stop.set()
        self.icon.stop()

    # -------------------- lifecycle --------------------

    def run(self) -> None:
        threading.Thread(target=self.poll_loop, daemon=True).start()
        self.icon.run()


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    config_path = config_mod.DEFAULT_CONFIG_PATH
    if argv:
        config_path = Path(argv[0])
    config_mod.ensure_exists(config_path)

    lock_fd = acquire_singleton()
    if lock_fd is None:
        print("Tray app is already running.", file=sys.stderr)
        return 1

    app = TrayApp(config_path)
    try:
        app.run()
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
