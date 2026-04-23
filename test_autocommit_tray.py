from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path

import pytest

from autocommit_tray import config as config_mod
from autocommit_tray import cron as cron_mod
from autocommit_tray import state as state_mod
from autocommit_tray.scheduler_install import _replace_block, CRON_BEGIN, CRON_END


# -------------------- config --------------------


def test_config_roundtrip(tmp_path: Path) -> None:
    cfg = config_mod.Config(
        log_dir=tmp_path / "logs",
        log_retention_days=5,
        notification_time="08:30",
        cron_schedule="*/15 * * * *",
        poll_interval_seconds=45,
        prefixes=[config_mod.PrefixEntry(prefix="home", directory=tmp_path)],
    )
    path = tmp_path / "cfg.yaml"
    config_mod.save(cfg, path)
    loaded = config_mod.load(path)
    assert loaded.log_dir == cfg.log_dir
    assert loaded.log_retention_days == 5
    assert loaded.notification_time == "08:30"
    assert loaded.cron_schedule == "*/15 * * * *"
    assert loaded.poll_interval_seconds == 45
    assert loaded.prefixes[0].prefix == "home"
    assert loaded.prefixes[0].directory == tmp_path


def test_config_rejects_missing_prefixes(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("log_dir: /tmp\n", encoding="utf-8")
    with pytest.raises(config_mod.ConfigError, match="prefixes"):
        config_mod.load(path)


def test_config_rejects_duplicate_prefix(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "prefixes:\n  - {prefix: a, directory: /tmp}\n  - {prefix: a, directory: /tmp}\n",
        encoding="utf-8",
    )
    with pytest.raises(config_mod.ConfigError, match="duplicate"):
        config_mod.load(path)


def test_config_rejects_bad_prefix_chars(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "prefixes:\n  - {prefix: 'bad name', directory: /tmp}\n",
        encoding="utf-8",
    )
    with pytest.raises(config_mod.ConfigError, match="prefix"):
        config_mod.load(path)


def test_config_rejects_bad_time(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "notification_time: '25:00'\nprefixes:\n  - {prefix: a, directory: /tmp}\n",
        encoding="utf-8",
    )
    with pytest.raises(config_mod.ConfigError, match="notification_time"):
        config_mod.load(path)


def test_config_rejects_non_positive_interval(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "poll_interval_seconds: 0\nprefixes:\n  - {prefix: a, directory: /tmp}\n",
        encoding="utf-8",
    )
    with pytest.raises(config_mod.ConfigError, match="poll_interval_seconds"):
        config_mod.load(path)


def test_config_ensure_exists_creates_default(tmp_path: Path) -> None:
    path = tmp_path / "new" / "cfg.yaml"
    config_mod.ensure_exists(path)
    assert path.exists()
    loaded = config_mod.load(path)
    assert loaded.prefixes[0].prefix == "home"


# -------------------- state --------------------


def _write_log(log_dir: Path, name: str, body: str = "") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / name
    p.write_text(body, encoding="utf-8")
    return p


def test_state_picks_latest_per_prefix(tmp_path: Path) -> None:
    _write_log(tmp_path, "home-20260101-0900.success")
    _write_log(tmp_path, "home-20260101-1000.failure", "boom")
    _write_log(tmp_path, "work-20260101-0800.success")
    result = state_mod.scan(tmp_path, ["home", "work"])
    assert result["home"].outcome == "failure"
    assert result["home"].latest_path is not None
    assert result["home"].latest_path.name == "home-20260101-1000.failure"
    assert result["work"].outcome == "success"


def test_state_missing_prefix_has_none(tmp_path: Path) -> None:
    result = state_mod.scan(tmp_path, ["nope"])
    assert result["nope"].outcome == "none"
    assert result["nope"].latest_path is None


def test_icon_color_green_when_all_success(tmp_path: Path) -> None:
    _write_log(tmp_path, "home-20260101-0900.success")
    statuses = state_mod.scan(tmp_path, ["home"])
    assert state_mod.icon_color(statuses, None) == "green"


def test_icon_color_red_when_any_failure(tmp_path: Path) -> None:
    _write_log(tmp_path, "home-20260101-0900.success")
    _write_log(tmp_path, "work-20260101-0900.failure")
    statuses = state_mod.scan(tmp_path, ["home", "work"])
    assert state_mod.icon_color(statuses, None) == "red"


def test_icon_color_yellow_when_running(tmp_path: Path) -> None:
    _write_log(tmp_path, "home-20260101-0900.success")
    statuses = state_mod.scan(tmp_path, ["home"])
    assert state_mod.icon_color(statuses, "home") == "yellow"


def test_running_prefix_from_marker(tmp_path: Path) -> None:
    marker = cron_mod.running_marker_path(tmp_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("home\n", encoding="utf-8")
    assert state_mod.running_prefix(tmp_path) == "home"


def test_running_prefix_none_when_absent(tmp_path: Path) -> None:
    assert state_mod.running_prefix(tmp_path) is None


def test_tooltip_includes_running_line(tmp_path: Path) -> None:
    _write_log(tmp_path, "home-20260101-0900.success")
    statuses = state_mod.scan(tmp_path, ["home"])
    text = state_mod.format_tooltip(statuses, "home")
    assert "Running: home" in text
    assert "home:" in text


# -------------------- cron --------------------


def _make_cfg(tmp_path: Path, prefixes: list[tuple[str, Path]]) -> config_mod.Config:
    return config_mod.Config(
        log_dir=tmp_path / "logs",
        log_retention_days=10,
        notification_time="09:00",
        cron_schedule="0 * * * *",
        poll_interval_seconds=60,
        prefixes=[config_mod.PrefixEntry(prefix=n, directory=d) for n, d in prefixes],
    )


def test_cron_writes_success_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cron_mod, "_run_scanner", lambda directory: (0, "all good\n"))
    cfg = _make_cfg(tmp_path, [("home", tmp_path)])
    rc = cron_mod.run(cfg)
    assert rc == cron_mod.EXIT_OK
    files = sorted((tmp_path / "logs").glob("home-*.success"))
    assert len(files) == 1
    assert files[0].read_text() == "all good\n"


def test_cron_writes_failure_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cron_mod, "_run_scanner", lambda directory: (1, "bang\n"))
    cfg = _make_cfg(tmp_path, [("home", tmp_path)])
    rc = cron_mod.run(cfg)
    assert rc == cron_mod.EXIT_FAILURE
    files = sorted((tmp_path / "logs").glob("home-*.failure"))
    assert len(files) == 1
    assert "bang" in files[0].read_text()


def test_cron_skips_prefix_already_succeeded_today(tmp_path: Path, monkeypatch) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    today = dt.datetime.now().strftime("%Y%m%d")
    _write_log(log_dir, f"home-{today}-0800.success")

    calls: list[Path] = []

    def fake_run(directory: Path) -> tuple[int, str]:
        calls.append(directory)
        return (0, "")

    monkeypatch.setattr(cron_mod, "_run_scanner", fake_run)
    cfg = _make_cfg(tmp_path, [("home", tmp_path)])
    rc = cron_mod.run(cfg)
    assert rc == cron_mod.EXIT_OK
    assert calls == []


def test_cron_force_overrides_skip(tmp_path: Path, monkeypatch) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    today = dt.datetime.now().strftime("%Y%m%d")
    _write_log(log_dir, f"home-{today}-0800.success")

    calls: list[Path] = []

    def fake_run(directory: Path) -> tuple[int, str]:
        calls.append(directory)
        return (0, "")

    monkeypatch.setattr(cron_mod, "_run_scanner", fake_run)
    cfg = _make_cfg(tmp_path, [("home", tmp_path)])
    rc = cron_mod.run(cfg, force=True)
    assert rc == cron_mod.EXIT_OK
    assert calls == [tmp_path]


def test_cron_only_prefix_filter(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(directory: Path) -> tuple[int, str]:
        calls.append(directory.name)
        return (0, "")

    monkeypatch.setattr(cron_mod, "_run_scanner", fake_run)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    cfg = _make_cfg(tmp_path, [("a", tmp_path / "a"), ("b", tmp_path / "b")])
    rc = cron_mod.run(cfg, only_prefix="b")
    assert rc == cron_mod.EXIT_OK
    assert calls == ["b"]


def test_cron_unknown_prefix_returns_config_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cron_mod, "_run_scanner", lambda d: (0, ""))
    cfg = _make_cfg(tmp_path, [("home", tmp_path)])
    rc = cron_mod.run(cfg, only_prefix="missing")
    assert rc == cron_mod.EXIT_CONFIG_ERROR


def test_cron_lock_blocks_second_invocation(tmp_path: Path, monkeypatch) -> None:
    release = threading.Event()
    started = threading.Event()

    def slow_scanner(directory: Path) -> tuple[int, str]:
        started.set()
        release.wait(timeout=5)
        return (0, "")

    monkeypatch.setattr(cron_mod, "_run_scanner", slow_scanner)
    cfg = _make_cfg(tmp_path, [("home", tmp_path)])

    holder = {"rc": None}
    t = threading.Thread(target=lambda: holder.__setitem__("rc", cron_mod.run(cfg)))
    t.start()
    assert started.wait(timeout=5)
    rc2 = cron_mod.run(cfg, force=True)
    assert rc2 == cron_mod.EXIT_LOCKED
    release.set()
    t.join(timeout=5)
    assert holder["rc"] == cron_mod.EXIT_OK


def test_cron_prunes_old_logs(tmp_path: Path, monkeypatch) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = _write_log(log_dir, "home-20200101-0900.success")
    old_mtime = time.time() - 30 * 86400
    import os
    os.utime(old, (old_mtime, old_mtime))
    recent = _write_log(log_dir, "home-20260420-0900.success")

    monkeypatch.setattr(cron_mod, "_run_scanner", lambda d: (0, ""))
    cfg = _make_cfg(tmp_path, [("home", tmp_path)])
    cfg.log_retention_days = 10
    cron_mod.run(cfg, force=True)

    assert not old.exists()
    assert recent.exists()


def test_cron_clears_running_marker_on_completion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cron_mod, "_run_scanner", lambda d: (0, ""))
    cfg = _make_cfg(tmp_path, [("home", tmp_path)])
    cron_mod.run(cfg)
    assert not cron_mod.running_marker_path(cfg.log_dir).exists()


# -------------------- scheduler install --------------------


def test_replace_block_inserts_when_absent() -> None:
    existing = "0 0 * * * echo other\n"
    out = _replace_block(existing, "0 * * * * run")
    assert CRON_BEGIN in out
    assert CRON_END in out
    assert "0 * * * * run" in out
    assert "echo other" in out


def test_replace_block_replaces_existing() -> None:
    existing = f"""0 0 * * * echo other
{CRON_BEGIN}
0 * * * * old
{CRON_END}
"""
    out = _replace_block(existing, "0 * * * * new")
    assert "0 * * * * old" not in out
    assert "0 * * * * new" in out
    assert "echo other" in out


def test_replace_block_removes_when_none() -> None:
    existing = f"""0 0 * * * echo other
{CRON_BEGIN}
0 * * * * old
{CRON_END}
"""
    out = _replace_block(existing, None)
    assert CRON_BEGIN not in out
    assert "old" not in out
    assert "echo other" in out
