# Git Autocommit Backup

A set of tools for automatically committing "backup snapshots" of dirty git
working trees, with a scheduled runner and a cross-platform system-tray
status indicator.

## What's in this project

### Scanner — [autocommit_scan.py](autocommit_scan.py)

Walks a root git repository and every nested repository underneath it, and
for each repo that contains a `.autocommit.yaml` marker file, commits the
dirty working-tree contents according to the marker's rules. Supports two
commit modes:

- `commit branch: <name>` — commit to the named branch (fails if the repo
  is on a different branch).
- `commit branch: "#new"` — stash the selected files, create a new
  `backup/YYYYMMDD-HHMMSS` branch off the latest previous backup (or off
  the current branch if none), apply the stash, and commit there.

Marker schema (all fields required):

```yaml
commit type: all           # or: changed  (track only modified-tracked files)
commit branch: "#new"      # or: a literal branch name
max-size: 10485760         # skip files larger than this many bytes
max-files: 200             # cap on files committed per run
push: false                # if true, git push after the commit
```

Invoked directly: `python autocommit_scan.py <root-dir>`.

### One-shot wrapper — [run_autocommit_home.sh](run_autocommit_home.sh)

Runs the scanner over `$HOME`. Convenient for manual use.

### Multi-prefix cron runner — [autocommit_tray.cron](autocommit_tray/cron.py) / [run_autocommit_cron.sh](run_autocommit_cron.sh)

Reads a YAML config file that lists one or more `(prefix, directory)` pairs,
and for each pair runs the scanner against that directory. Features:

- Skips any prefix that already has a `<prefix>-YYYYMMDD-*.success` log
  file for the current day.
- On each run, writes `<prefix>-YYYYMMDD-HHMM.success` (empty) or
  `<prefix>-YYYYMMDD-HHMM.failure` (containing the scanner's stdout+stderr)
  to the configured log directory.
- Prunes log files older than `log_retention_days`.
- Holds a file lock on `<log_dir>/.cron.lock` for the duration of the run.
  A second invocation (e.g. hourly cron firing while the previous run is
  still going) exits silently with code `2` instead of piling up.
- Writes `<log_dir>/.cron.running` with the currently-running prefix name
  so the tray can render a "running" state.

Designed to be called from cron (Linux) or launchd (macOS) hourly. The
shell wrapper is a thin shim that ensures Poetry dependencies are
installed, then invokes `python -m autocommit_tray.cron`.

### Tray app — [autocommit_tray](autocommit_tray/) / [run_autocommit_tray.sh](run_autocommit_tray.sh)

Long-running process that lives in the system tray / menu bar:

- Icon color: **green** when every prefix's latest log is `.success`,
  **red** if any prefix's latest is `.failure`, **yellow** while a cron run
  is in progress (with the running prefix shown in the tooltip).
- Daily desktop notification (at a configurable fixed time) when the last
  run had a failure; if the machine was asleep at that time, fires on the
  next poll after wake.
- Menu: *Open last failing log* (submenu per failed prefix), *Re-run*
  (submenu per failed prefix, plus "All failed"), *Settings…*, *Quit*.
- Settings window (tkinter): log directory, retention, notification time,
  poll interval, cron schedule, prefix list, and buttons to install or
  remove the backup schedule (crontab on Linux, LaunchAgent on macOS) and
  the autostart entry for the tray itself.

## Setup — Linux

Prerequisites:

- Python 3.10 or newer, with `tkinter` (install `python3-tk` on
  Debian/Ubuntu if missing).
- [Poetry](https://python-poetry.org/docs/#installation).
- `git`.
- `libnotify-bin` (for `notify-send`, used by the daily failure notification).
- **GNOME users:** install and enable the
  [AppIndicator and KStatusNotifierItem Support](https://extensions.gnome.org/extension/615/appindicator-support/)
  extension — vanilla GNOME does not render tray icons. KDE, XFCE, Cinnamon,
  and MATE work without extra extensions.

Install and first run:

```sh
git clone <this-repo>
cd backup

# One-shot: creates the default config at ~/.config/autocommit-backup/config.yaml
# on first start, installs Python deps into a Poetry-managed venv, and shows
# the tray icon.
./run_autocommit_tray.sh
```

Then, from the tray icon's menu, choose **Settings…** and:

1. Edit the prefix list so it points at the directories you want to back up
   (each must be a git repo with a `.autocommit.yaml` marker — see the
   scanner section above).
2. Adjust notification time, poll interval, retention, and cron schedule
   as desired.
3. Click **Install/Update backup schedule** — this adds a managed block to
   your user crontab that invokes `run_autocommit_cron.sh` at the
   configured cadence. Inspect with `crontab -l`.
4. Click **Enable tray autostart** — this writes a `.desktop` file to
   `~/.config/autostart/` so the tray starts on login.

## Setup — macOS

Prerequisites:

- Python 3.10 or newer, with `tkinter` (the python.org installer and
  Homebrew's `python-tk` both include it; the system Python may not).
- [Poetry](https://python-poetry.org/docs/#installation).
- `git`.

Install and first run:

```sh
git clone <this-repo>
cd backup
./run_autocommit_tray.sh
```

The app will appear in the right-hand menu bar. Use the **Settings…**
entry to configure prefixes, then:

- **Install/Update backup schedule** — writes a LaunchAgent at
  `~/Library/LaunchAgents/org.local.autocommit-backup.plist` (on macOS, a
  LaunchAgent is used instead of cron; simple cron expressions like
  `0 * * * *` or `0 9 * * *` are translated to `StartCalendarInterval`,
  more complex ones fall back to a 1-hour `StartInterval`). Inspect with
  `launchctl list | grep autocommit`.
- **Enable tray autostart** — writes
  `~/Library/LaunchAgents/org.local.autocommit-backup-tray.plist` with
  `RunAtLoad=true` and `KeepAlive=true`.

On first connection, macOS may prompt you to allow notifications from the
terminal/iTerm/Script Editor process that the daily notification is
delivered through.

## Config file

Default location: `~/.config/autocommit-backup/config.yaml`. Example:

```yaml
log_dir: ~/.local/state/autocommit-backup/logs
log_retention_days: 10
notification_time: "09:00"
cron_schedule: "0 * * * *"
poll_interval_seconds: 60
prefixes:
  - prefix: home
    directory: ~
```

`prefix` must match `[A-Za-z0-9_-]+` (used in log filenames).

## Tests

```sh
poetry install
poetry run pytest
```
