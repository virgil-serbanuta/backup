from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import cron

Outcome = Literal["success", "failure", "none"]
IconColor = Literal["green", "red", "yellow"]

LOG_FILENAME_RE = re.compile(
    r"^(?P<prefix>.+)-(?P<ts>\d{8}-\d{4})\.(?P<outcome>success|failure)$"
)


@dataclass
class PrefixStatus:
    prefix: str
    outcome: Outcome
    latest_path: Path | None
    timestamp: dt.datetime | None


def _parse_timestamp(ts: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(ts, "%Y%m%d-%H%M")
    except ValueError:
        return None


def scan(log_dir: Path, prefixes: list[str]) -> dict[str, PrefixStatus]:
    result: dict[str, PrefixStatus] = {
        p: PrefixStatus(prefix=p, outcome="none", latest_path=None, timestamp=None)
        for p in prefixes
    }
    if not log_dir.is_dir():
        return result

    latest: dict[str, tuple[dt.datetime, Path, Outcome]] = {}
    for entry in log_dir.iterdir():
        if not entry.is_file():
            continue
        m = LOG_FILENAME_RE.match(entry.name)
        if not m:
            continue
        prefix = m.group("prefix")
        if prefix not in result:
            continue
        ts = _parse_timestamp(m.group("ts"))
        if ts is None:
            continue
        outcome: Outcome = m.group("outcome")  # type: ignore[assignment]
        current = latest.get(prefix)
        if current is None or ts > current[0]:
            latest[prefix] = (ts, entry, outcome)

    for prefix, (ts, path, outcome) in latest.items():
        result[prefix] = PrefixStatus(
            prefix=prefix, outcome=outcome, latest_path=path, timestamp=ts
        )
    return result


def running_prefix(log_dir: Path) -> str | None:
    marker = cron.running_marker_path(log_dir)
    try:
        name = marker.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    return name or None


def icon_color(statuses: dict[str, PrefixStatus], running: str | None) -> IconColor:
    if running is not None:
        return "yellow"
    for status in statuses.values():
        if status.outcome == "failure":
            return "red"
    return "green"


def failed_prefixes(statuses: dict[str, PrefixStatus]) -> list[str]:
    return [s.prefix for s in statuses.values() if s.outcome == "failure"]


def format_tooltip(
    statuses: dict[str, PrefixStatus], running: str | None
) -> str:
    lines = ["Autocommit Backup"]
    if running is not None:
        lines.append(f"Running: {running}")
    for status in statuses.values():
        if status.outcome == "none":
            lines.append(f"{status.prefix}: no runs yet")
        else:
            ts = status.timestamp.strftime("%Y-%m-%d %H:%M") if status.timestamp else "?"
            label = "OK" if status.outcome == "success" else "FAIL"
            lines.append(f"{status.prefix}: {label} at {ts}")
    return "\n".join(lines)
