from __future__ import annotations

import platform
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CRON_WRAPPER = REPO_ROOT / "run_autocommit_cron.sh"
TRAY_WRAPPER = REPO_ROOT / "run_autocommit_tray.sh"

CRON_BEGIN = "# BEGIN autocommit-backup (managed by autocommit_tray)"
CRON_END = "# END autocommit-backup"

LAUNCHD_BACKUP_LABEL = "org.local.autocommit-backup"
LAUNCHD_TRAY_LABEL = "org.local.autocommit-backup-tray"

DESKTOP_FILE = Path("~/.config/autostart/autocommit-backup-tray.desktop").expanduser()
LAUNCHD_DIR = Path("~/Library/LaunchAgents").expanduser()


@dataclass
class InstallResult:
    ok: bool
    message: str


def is_macos() -> bool:
    return platform.system() == "Darwin"


# -------------------- Backup scheduler (cron / launchd) --------------------


def install_backup_schedule(config_path: Path, cron_schedule: str) -> InstallResult:
    if is_macos():
        return _install_launchd_backup(config_path, cron_schedule)
    return _install_cron(config_path, cron_schedule)


def uninstall_backup_schedule() -> InstallResult:
    if is_macos():
        return _uninstall_launchd(LAUNCHD_BACKUP_LABEL)
    return _remove_cron()


def _read_crontab() -> str:
    proc = subprocess.run(["crontab", "-l"], text=True, capture_output=True)
    if proc.returncode == 0:
        return proc.stdout
    if "no crontab" in proc.stderr.lower():
        return ""
    if proc.returncode == 1 and not proc.stdout:
        return ""
    return proc.stdout


def _write_crontab(content: str) -> InstallResult:
    proc = subprocess.run(["crontab", "-"], input=content, text=True, capture_output=True)
    if proc.returncode != 0:
        return InstallResult(False, f"crontab write failed: {proc.stderr.strip()}")
    return InstallResult(True, "crontab updated")


def _replace_block(existing: str, new_block: str | None) -> str:
    lines = existing.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == CRON_BEGIN:
            skipping = True
            continue
        if line.strip() == CRON_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    if new_block is not None:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(CRON_BEGIN)
        out.extend(new_block.splitlines())
        out.append(CRON_END)
    cleaned = "\n".join(out).rstrip("\n")
    return cleaned + "\n" if cleaned else ""


def _install_cron(config_path: Path, cron_schedule: str) -> InstallResult:
    if shutil.which("crontab") is None:
        return InstallResult(False, "crontab binary not found")
    command = f"{cron_schedule} {CRON_WRAPPER} {config_path}"
    new_crontab = _replace_block(_read_crontab(), command)
    return _write_crontab(new_crontab)


def _remove_cron() -> InstallResult:
    if shutil.which("crontab") is None:
        return InstallResult(False, "crontab binary not found")
    new_crontab = _replace_block(_read_crontab(), None)
    return _write_crontab(new_crontab)


def _cron_to_launchd(cron_schedule: str) -> dict[str, object]:
    parts = cron_schedule.split()
    if len(parts) != 5:
        return {"StartInterval": 3600}
    minute, hour, dom, month, dow = parts

    def lit(v: str) -> int | None:
        if v.isdigit():
            return int(v)
        return None

    cal: dict[str, int] = {}
    m = lit(minute)
    h = lit(hour)
    if m is not None:
        cal["Minute"] = m
    if h is not None:
        cal["Hour"] = h
    if lit(dom) is not None:
        cal["Day"] = int(dom)
    if lit(month) is not None:
        cal["Month"] = int(month)
    if lit(dow) is not None:
        cal["Weekday"] = int(dow)

    if cal:
        return {"StartCalendarInterval": cal}
    return {"StartInterval": 3600}


def _launchd_plist_path(label: str) -> Path:
    return LAUNCHD_DIR / f"{label}.plist"


def _install_launchd_backup(config_path: Path, cron_schedule: str) -> InstallResult:
    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
    plist_path = _launchd_plist_path(LAUNCHD_BACKUP_LABEL)
    body: dict[str, object] = {
        "Label": LAUNCHD_BACKUP_LABEL,
        "ProgramArguments": [str(CRON_WRAPPER), str(config_path)],
        "RunAtLoad": False,
    }
    body.update(_cron_to_launchd(cron_schedule))
    return _write_plist_and_load(plist_path, body, LAUNCHD_BACKUP_LABEL)


def _write_plist_and_load(path: Path, body: dict[str, object], label: str) -> InstallResult:
    _unload_launchd_silently(label, path)
    with path.open("wb") as fh:
        plistlib.dump(body, fh)
    proc = subprocess.run(
        ["launchctl", "load", "-w", str(path)], text=True, capture_output=True
    )
    if proc.returncode != 0:
        return InstallResult(False, f"launchctl load failed: {proc.stderr.strip()}")
    return InstallResult(True, f"launchd agent loaded: {label}")


def _unload_launchd_silently(label: str, path: Path) -> None:
    if not path.exists():
        return
    subprocess.run(
        ["launchctl", "unload", "-w", str(path)], text=True, capture_output=True
    )


def _uninstall_launchd(label: str) -> InstallResult:
    path = _launchd_plist_path(label)
    _unload_launchd_silently(label, path)
    if path.exists():
        path.unlink()
    return InstallResult(True, f"launchd agent removed: {label}")


# -------------------- Tray autostart --------------------


def install_tray_autostart() -> InstallResult:
    if is_macos():
        return _install_launchd_tray()
    return _install_desktop_autostart()


def uninstall_tray_autostart() -> InstallResult:
    if is_macos():
        return _uninstall_launchd(LAUNCHD_TRAY_LABEL)
    if DESKTOP_FILE.exists():
        DESKTOP_FILE.unlink()
    return InstallResult(True, f"autostart file removed: {DESKTOP_FILE}")


def _install_desktop_autostart() -> InstallResult:
    DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_FILE.write_text(
        "\n".join(
            [
                "[Desktop Entry]",
                "Type=Application",
                "Name=Autocommit Backup Tray",
                f"Exec={TRAY_WRAPPER}",
                "X-GNOME-Autostart-enabled=true",
                "Terminal=false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return InstallResult(True, f"autostart file written: {DESKTOP_FILE}")


def _install_launchd_tray() -> InstallResult:
    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
    plist_path = _launchd_plist_path(LAUNCHD_TRAY_LABEL)
    body: dict[str, object] = {
        "Label": LAUNCHD_TRAY_LABEL,
        "ProgramArguments": [str(TRAY_WRAPPER)],
        "RunAtLoad": True,
        "KeepAlive": True,
    }
    return _write_plist_and_load(plist_path, body, LAUNCHD_TRAY_LABEL)


def tray_autostart_enabled() -> bool:
    if is_macos():
        return _launchd_plist_path(LAUNCHD_TRAY_LABEL).exists()
    return DESKTOP_FILE.exists()


def backup_schedule_installed() -> bool:
    if is_macos():
        return _launchd_plist_path(LAUNCHD_BACKUP_LABEL).exists()
    return CRON_BEGIN in _read_crontab()
