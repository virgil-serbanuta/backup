from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


DEFAULT_CONFIG_PATH = Path("~/.config/autocommit-backup/config.yaml").expanduser()
DEFAULT_LOG_DIR = Path("~/.local/state/autocommit-backup/logs").expanduser()

PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(Exception):
    pass


@dataclass
class PrefixEntry:
    prefix: str
    directory: Path


@dataclass
class Config:
    log_dir: Path = field(default_factory=lambda: DEFAULT_LOG_DIR)
    log_retention_days: int = 10
    notification_time: str = "09:00"
    cron_schedule: str = "0 * * * *"
    poll_interval_seconds: int = 60
    prefixes: list[PrefixEntry] = field(default_factory=list)

    @staticmethod
    def default() -> Config:
        return Config(prefixes=[PrefixEntry(prefix="home", directory=Path.home())])


def _expand(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser()


def load(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Cannot parse YAML at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Config at {path} must be a YAML mapping")
    return _from_dict(raw, source=path)


def save(config: Config, path: Path | None = None) -> None:
    path = path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(_to_dict(config), sort_keys=False), encoding="utf-8")


def _to_dict(config: Config) -> dict[str, object]:
    return {
        "log_dir": str(config.log_dir),
        "log_retention_days": config.log_retention_days,
        "notification_time": config.notification_time,
        "cron_schedule": config.cron_schedule,
        "poll_interval_seconds": config.poll_interval_seconds,
        "prefixes": [
            {"prefix": p.prefix, "directory": str(p.directory)} for p in config.prefixes
        ],
    }


def _from_dict(raw: dict, source: Path) -> Config:
    cfg = Config.default()

    if "log_dir" in raw:
        if not isinstance(raw["log_dir"], str):
            raise ConfigError("'log_dir' must be a string")
        cfg.log_dir = _expand(raw["log_dir"])

    if "log_retention_days" in raw:
        val = raw["log_retention_days"]
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            raise ConfigError("'log_retention_days' must be a positive integer")
        cfg.log_retention_days = val

    if "notification_time" in raw:
        val = raw["notification_time"]
        if not isinstance(val, str) or not TIME_RE.match(val):
            raise ConfigError("'notification_time' must be a HH:MM string (24h)")
        cfg.notification_time = val

    if "cron_schedule" in raw:
        val = raw["cron_schedule"]
        if not isinstance(val, str) or len(val.split()) != 5:
            raise ConfigError("'cron_schedule' must be a 5-field cron expression")
        cfg.cron_schedule = val

    if "poll_interval_seconds" in raw:
        val = raw["poll_interval_seconds"]
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            raise ConfigError("'poll_interval_seconds' must be a positive integer")
        cfg.poll_interval_seconds = val

    prefixes_raw = raw.get("prefixes")
    if prefixes_raw is None:
        raise ConfigError("'prefixes' is required (list of {prefix, directory})")
    if not isinstance(prefixes_raw, list) or not prefixes_raw:
        raise ConfigError("'prefixes' must be a non-empty list")

    seen: set[str] = set()
    entries: list[PrefixEntry] = []
    for i, entry in enumerate(prefixes_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"prefixes[{i}] must be a mapping")
        prefix = entry.get("prefix")
        directory = entry.get("directory")
        if not isinstance(prefix, str) or not PREFIX_RE.match(prefix):
            raise ConfigError(
                f"prefixes[{i}].prefix must match {PREFIX_RE.pattern} (got {prefix!r})"
            )
        if prefix in seen:
            raise ConfigError(f"duplicate prefix: {prefix!r}")
        seen.add(prefix)
        if not isinstance(directory, str) or not directory:
            raise ConfigError(f"prefixes[{i}].directory must be a non-empty string")
        entries.append(PrefixEntry(prefix=prefix, directory=_expand(directory)))
    cfg.prefixes = entries

    return cfg


def ensure_exists(path: Path | None = None) -> Path:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        save(Config.default(), path)
    return path
