from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import config as config_mod
from . import scheduler_install


class SettingsWindow:
    def __init__(self, root: tk.Tk, config_path: Path):
        self.root = root
        self.config_path = config_path
        try:
            self.cfg = config_mod.load(config_path)
        except config_mod.ConfigError:
            self.cfg = config_mod.Config.default()

        root.title("Autocommit Backup — Settings")
        root.geometry("640x520")

        main = ttk.Frame(root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        self._build_general(main)
        self._build_prefixes(main)
        self._build_scheduler(main)
        self._build_buttons(main)

    def _build_general(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="General", padding=8)
        frame.pack(fill=tk.X, pady=(0, 8))
        for col in range(3):
            frame.columnconfigure(col, weight=1 if col == 1 else 0)

        ttk.Label(frame, text="Log directory:").grid(row=0, column=0, sticky="w", pady=2)
        self.log_dir_var = tk.StringVar(value=str(self.cfg.log_dir))
        ttk.Entry(frame, textvariable=self.log_dir_var).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(frame, text="Browse…", command=self._choose_log_dir).grid(row=0, column=2)

        ttk.Label(frame, text="Retention (days):").grid(row=1, column=0, sticky="w", pady=2)
        self.retention_var = tk.IntVar(value=self.cfg.log_retention_days)
        ttk.Spinbox(frame, from_=1, to=365, textvariable=self.retention_var, width=8).grid(
            row=1, column=1, sticky="w", padx=4
        )

        ttk.Label(frame, text="Notification time (HH:MM):").grid(row=2, column=0, sticky="w", pady=2)
        self.notify_var = tk.StringVar(value=self.cfg.notification_time)
        ttk.Entry(frame, textvariable=self.notify_var, width=10).grid(row=2, column=1, sticky="w", padx=4)

        ttk.Label(frame, text="Poll interval (seconds):").grid(row=3, column=0, sticky="w", pady=2)
        self.poll_var = tk.IntVar(value=self.cfg.poll_interval_seconds)
        ttk.Spinbox(frame, from_=5, to=3600, textvariable=self.poll_var, width=8).grid(
            row=3, column=1, sticky="w", padx=4
        )

        ttk.Label(frame, text="Cron schedule (5 fields):").grid(row=4, column=0, sticky="w", pady=2)
        self.cron_var = tk.StringVar(value=self.cfg.cron_schedule)
        ttk.Entry(frame, textvariable=self.cron_var).grid(row=4, column=1, sticky="we", padx=4)

    def _build_prefixes(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Prefixes", padding=8)
        frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(frame, columns=("prefix", "directory"), show="headings", height=6)
        self.tree.heading("prefix", text="Prefix")
        self.tree.heading("directory", text="Directory")
        self.tree.column("prefix", width=120, anchor="w")
        self.tree.column("directory", width=380, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        for entry in self.cfg.prefixes:
            self.tree.insert("", tk.END, values=(entry.prefix, str(entry.directory)))

        btns = ttk.Frame(frame)
        btns.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(btns, text="Add…", command=self._add_prefix).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btns, text="Edit…", command=self._edit_prefix).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Remove", command=self._remove_prefix).pack(side=tk.LEFT, padx=4)

    def _build_scheduler(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Scheduler", padding=8)
        frame.pack(fill=tk.X, pady=(0, 8))

        self.cron_installed_var = tk.StringVar(value=self._cron_label())
        ttk.Label(frame, textvariable=self.cron_installed_var).grid(row=0, column=0, sticky="w")
        ttk.Button(frame, text="Install/Update backup schedule", command=self._install_cron).grid(
            row=0, column=1, padx=4
        )
        ttk.Button(frame, text="Uninstall", command=self._uninstall_cron).grid(row=0, column=2)

        self.autostart_var = tk.StringVar(value=self._autostart_label())
        ttk.Label(frame, textvariable=self.autostart_var).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Button(frame, text="Enable tray autostart", command=self._enable_autostart).grid(
            row=1, column=1, padx=4, pady=(4, 0)
        )
        ttk.Button(frame, text="Disable", command=self._disable_autostart).grid(
            row=1, column=2, pady=(4, 0)
        )

    def _build_buttons(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X)
        ttk.Button(frame, text="Cancel", command=self.root.destroy).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(frame, text="Save", command=self._save).pack(side=tk.RIGHT)

    # -------- callbacks --------

    def _choose_log_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.log_dir_var.get() or str(Path.home()))
        if chosen:
            self.log_dir_var.set(chosen)

    def _add_prefix(self) -> None:
        result = _PrefixDialog(self.root, title="Add prefix").result
        if result is not None:
            self.tree.insert("", tk.END, values=result)

    def _edit_prefix(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        current = self.tree.item(sel[0], "values")
        result = _PrefixDialog(
            self.root, title="Edit prefix", initial=current
        ).result
        if result is not None:
            self.tree.item(sel[0], values=result)

    def _remove_prefix(self) -> None:
        for iid in self.tree.selection():
            self.tree.delete(iid)

    def _install_cron(self) -> None:
        if not self._save(close=False):
            return
        res = scheduler_install.install_backup_schedule(self.config_path, self.cfg.cron_schedule)
        messagebox.showinfo("Scheduler", res.message)
        self.cron_installed_var.set(self._cron_label())

    def _uninstall_cron(self) -> None:
        res = scheduler_install.uninstall_backup_schedule()
        messagebox.showinfo("Scheduler", res.message)
        self.cron_installed_var.set(self._cron_label())

    def _enable_autostart(self) -> None:
        res = scheduler_install.install_tray_autostart()
        messagebox.showinfo("Autostart", res.message)
        self.autostart_var.set(self._autostart_label())

    def _disable_autostart(self) -> None:
        res = scheduler_install.uninstall_tray_autostart()
        messagebox.showinfo("Autostart", res.message)
        self.autostart_var.set(self._autostart_label())

    def _cron_label(self) -> str:
        return "Backup schedule: installed" if scheduler_install.backup_schedule_installed() else "Backup schedule: not installed"

    def _autostart_label(self) -> str:
        return "Tray autostart: enabled" if scheduler_install.tray_autostart_enabled() else "Tray autostart: disabled"

    def _collect(self) -> config_mod.Config | None:
        try:
            cfg = config_mod.Config(
                log_dir=Path(self.log_dir_var.get()).expanduser(),
                log_retention_days=int(self.retention_var.get()),
                notification_time=self.notify_var.get().strip(),
                cron_schedule=self.cron_var.get().strip(),
                poll_interval_seconds=int(self.poll_var.get()),
                prefixes=[
                    config_mod.PrefixEntry(
                        prefix=self.tree.item(iid, "values")[0],
                        directory=Path(self.tree.item(iid, "values")[1]).expanduser(),
                    )
                    for iid in self.tree.get_children()
                ],
            )
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Invalid value", str(exc))
            return None
        return cfg

    def _save(self, close: bool = True) -> bool:
        cfg = self._collect()
        if cfg is None:
            return False
        try:
            config_mod.save(cfg, self.config_path)
            self.cfg = config_mod.load(self.config_path)
        except config_mod.ConfigError as exc:
            messagebox.showerror("Invalid config", str(exc))
            return False
        if close:
            self.root.destroy()
        return True


class _PrefixDialog:
    def __init__(self, parent: tk.Misc, title: str, initial: tuple[str, str] | None = None):
        self.result: tuple[str, str] | None = None
        top = tk.Toplevel(parent)
        top.title(title)
        top.transient(parent)
        top.grab_set()

        frame = ttk.Frame(top, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Prefix:").grid(row=0, column=0, sticky="w")
        prefix_var = tk.StringVar(value=initial[0] if initial else "")
        ttk.Entry(frame, textvariable=prefix_var, width=24).grid(row=0, column=1, sticky="we", padx=4)

        ttk.Label(frame, text="Directory:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        dir_var = tk.StringVar(value=initial[1] if initial else str(Path.home()))
        ttk.Entry(frame, textvariable=dir_var, width=40).grid(row=1, column=1, sticky="we", padx=4, pady=(6, 0))
        ttk.Button(
            frame,
            text="Browse…",
            command=lambda: self._browse(dir_var),
        ).grid(row=1, column=2, pady=(6, 0))

        btns = ttk.Frame(frame)
        btns.grid(row=2, column=0, columnspan=3, sticky="e", pady=(12, 0))

        def on_ok() -> None:
            prefix = prefix_var.get().strip()
            directory = dir_var.get().strip()
            if not prefix or not directory:
                messagebox.showerror("Missing value", "Prefix and directory are required", parent=top)
                return
            if not config_mod.PREFIX_RE.match(prefix):
                messagebox.showerror(
                    "Invalid prefix",
                    f"Prefix must match {config_mod.PREFIX_RE.pattern}",
                    parent=top,
                )
                return
            self.result = (prefix, directory)
            top.destroy()

        ttk.Button(btns, text="Cancel", command=top.destroy).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(btns, text="OK", command=on_ok).pack(side=tk.RIGHT)

        top.wait_window()

    def _browse(self, var: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if chosen:
            var.set(chosen)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path, nargs="?", default=config_mod.DEFAULT_CONFIG_PATH)
    args = parser.parse_args(argv)
    config_mod.ensure_exists(args.config)
    root = tk.Tk()
    SettingsWindow(root, args.config)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
