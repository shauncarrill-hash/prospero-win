#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""A window for sending a game to the PS5: pick the game's zip or folder,
type the PS5's address, press Send. tools/pw_quick.py does the work: it
finds the game's executable, picks its settings, builds its prefix on the
base prefix and sends it all over FTP. The same window installs or updates
the prospero-win app itself from a release zip.

Python's own Tk, nothing else, so it runs wherever Python does and packages
into one Windows .exe (tools/build_gui.py).
"""
from __future__ import annotations

import ftplib
import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pw_quick  # noqa: E402
from pw_quick import QuickError  # noqa: E402

VERSION = "4.3"
TITLE = f"prospero-win sender v{VERSION}: send a game to your PS5"
SETTINGS = pw_quick.STATE_DIR / "settings.json"
BASE_NAME = "prospero-base-prefix.zip"
RESOLUTIONS = ("1920x1080", "1280x720", "2560x1440", "3840x2160", "1024x768", "800x600")
GRAPHICS_LABELS = {
    "dxvk": "Direct3D 8-11 (DXVK)",
    "opengl": "OpenGL",
    "gdi": "2D (GDI)",
    "auto": "Automatic (Vulkan, DirectDraw)",
}


def here() -> Path:
    """The folder the program is in: beside the .exe once packaged."""
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(values: dict) -> None:
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(values, indent=1))
    except OSError:
        pass


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.source: pw_quick.Source | None = None
        self.exes: list[pw_quick.Executable] = []
        settings = load_settings()

        root.title(TITLE)
        root.minsize(620, 0)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("TkDefaultFont", 13, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Horizontal.TProgressbar", background="#1677E8")

        self.host = tk.StringVar(value=settings.get("host", ""))
        self.port = tk.StringVar(value=str(settings.get("port", 2121)))
        default_base = here() / BASE_NAME
        self.base = tk.StringVar(value=settings.get("base") or (str(default_base) if default_base.exists() else ""))
        self.game_path = tk.StringVar()
        self.name = tk.StringVar()
        self.exe = tk.StringVar()
        self.graphics = tk.StringVar()
        self.detected = tk.StringVar()
        self.desktop = tk.StringVar(value=settings.get("desktop", "1920x1080"))
        self.preset = tk.StringVar()
        self.arguments = tk.StringVar()
        self.environment = tk.StringVar()
        self.winedebug = tk.StringVar()
        self.checks = tk.StringVar()
        self.status = tk.StringVar(value="Pick a game to start.")

        outer = ttk.Frame(root, padding=16)
        outer.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        ttk.Label(outer, text="Send a game to your PS5", style="Title.TLabel").grid(sticky="w")
        ttk.Label(outer, style="Hint.TLabel", wraplength=580, justify="left",
                  text="For games that are already installed: a folder copied from a Windows PC, "
                       "or a zip of one. Games that need their installer to run still go through "
                       "the recipes in prospero-win-profiles.").grid(sticky="w", pady=(2, 12))

        ps5 = ttk.LabelFrame(outer, text="PS5", padding=10)
        ps5.grid(sticky="ew", pady=(0, 10))
        ps5.columnconfigure(1, weight=1)
        ttk.Label(ps5, text="IP address").grid(row=0, column=0, sticky="w")
        ttk.Entry(ps5, textvariable=self.host, width=18).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(ps5, text="FTP port").grid(row=0, column=2, sticky="e")
        ttk.Entry(ps5, textvariable=self.port, width=7).grid(row=0, column=3, sticky="w", padx=8)
        self.check_button = ttk.Button(ps5, text="Check", command=self.check_console)
        self.check_button.grid(row=0, column=4)

        game = ttk.LabelFrame(outer, text="Game", padding=10)
        game.grid(sticky="ew", pady=(0, 10))
        game.columnconfigure(1, weight=1)
        pick = ttk.Frame(game)
        pick.grid(row=0, column=0, columnspan=3, sticky="ew")
        pick.columnconfigure(0, weight=1)
        ttk.Entry(pick, textvariable=self.game_path, state="readonly").grid(row=0, column=0, sticky="ew")
        self.zip_button = ttk.Button(pick, text="Choose zip…", command=self.choose_zip)
        self.zip_button.grid(row=0, column=1, padx=(8, 0))
        self.folder_button = ttk.Button(pick, text="Choose folder…", command=self.choose_folder)
        self.folder_button.grid(row=0, column=2, padx=(8, 0))

        rows = (("Name in the launcher", ttk.Entry(game, textvariable=self.name)),
                ("Program to start", ttk.Combobox(game, textvariable=self.exe, state="readonly")),
                ("Graphics", ttk.Combobox(game, textvariable=self.graphics, state="readonly",
                                          values=list(GRAPHICS_LABELS.values()))),
                ("Resolution", ttk.Combobox(game, textvariable=self.desktop, values=RESOLUTIONS)),
                ("Controller preset", ttk.Entry(game, textvariable=self.preset)),
                ("Arguments", ttk.Entry(game, textvariable=self.arguments)),
                ("Environment", ttk.Entry(game, textvariable=self.environment)),
                ("Wine log channels", ttk.Entry(game, textvariable=self.winedebug)))
        self.fields = []
        for index, (label, widget) in enumerate(rows, start=1):
            ttk.Label(game, text=label).grid(row=index, column=0, sticky="w", pady=3)
            widget.grid(row=index, column=1, sticky="ew", padx=(8, 0), pady=3)
            self.fields.append(widget)
        self.exe_box = self.fields[1]
        self.exe_box.bind("<<ComboboxSelected>>", self.exe_changed)
        ttk.Label(game, textvariable=self.detected, style="Hint.TLabel").grid(row=3, column=2, sticky="w", padx=8)
        ttk.Label(game, text="optional, e.g. warcraft3", style="Hint.TLabel").grid(row=5, column=2, sticky="w", padx=8)
        ttk.Label(game, text="NAME=VALUE; …", style="Hint.TLabel").grid(row=7, column=2, sticky="w", padx=8)
        ttk.Label(game, text="for logs, e.g. +seh", style="Hint.TLabel").grid(row=8, column=2, sticky="w", padx=8)
        ttk.Label(game, textvariable=self.checks, wraplength=580, justify="left").grid(
            row=len(rows) + 1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.set_fields("disabled")

        base = ttk.LabelFrame(outer, text="Base prefix", padding=10)
        base.grid(sticky="ew", pady=(0, 10))
        base.columnconfigure(0, weight=1)
        ttk.Entry(base, textvariable=self.base).grid(row=0, column=0, sticky="ew")
        ttk.Button(base, text="Browse…", command=self.choose_base).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(base, style="Hint.TLabel", wraplength=580, justify="left",
                  text=f"A clean Wine prefix every game starts from ({BASE_NAME}). "
                       "Made once with tools/pw_base_prefix.py.").grid(row=1, column=0, columnspan=2, sticky="w")

        self.bar = ttk.Progressbar(outer, mode="determinate", maximum=1000)
        self.bar.grid(sticky="ew", pady=(4, 4))
        ttk.Label(outer, textvariable=self.status, wraplength=580, justify="left").grid(sticky="w")

        buttons = ttk.Frame(outer)
        buttons.grid(sticky="ew", pady=(12, 0))
        buttons.columnconfigure(0, weight=1)
        self.app_button = ttk.Button(buttons, text="Install or update the app…", command=self.install_app)
        self.app_button.grid(row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.cancel_button.grid(row=0, column=1, padx=(0, 8))
        self.send_button = ttk.Button(buttons, text="Send to PS5", command=self.send, state="disabled")
        self.send_button.grid(row=0, column=2)

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.pump)

    # --- helpers ---------------------------------------------------------------
    def set_fields(self, state: str) -> None:
        resolution = self.fields[3]
        for widget in self.fields:
            choice_only = isinstance(widget, ttk.Combobox) and widget is not resolution
            widget.configure(state="readonly" if state == "normal" and choice_only else state)

    def busy(self, on: bool) -> None:
        for button in (self.zip_button, self.folder_button, self.app_button, self.check_button):
            button.configure(state="disabled" if on else "normal")
        self.send_button.configure(state="disabled" if on or not self.exes else "normal")
        self.cancel_button.configure(state="normal" if on else "disabled")
        self.set_fields("disabled" if on or not self.exes else "normal")

    def address(self) -> tuple[str, int]:
        host = self.host.get().strip()
        if not host:
            raise QuickError("Type the PS5's IP address first (it's shown by the FTP payload when it starts).")
        try:
            port = int(self.port.get().strip())
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            raise QuickError(f"{self.port.get()!r} is not a port number (the PS5's FTP payload usually uses 2121).")
        return host, port

    def remember(self) -> None:
        save_settings({"host": self.host.get().strip(), "port": self.port.get().strip(),
                       "base": self.base.get().strip(), "desktop": self.desktop.get().strip()})

    def run(self, work, done=None) -> None:
        """work(report) in the background; done(result) back on the window."""
        self.cancel.clear()
        self.busy(True)

        def report(progress: pw_quick.Progress) -> None:
            self.events.put(("progress", (progress.done_bytes, progress.total_bytes, progress.done_files,
                                          progress.total_files, progress.current, progress.message)))

        def body() -> None:
            try:
                self.events.put(("done", (done, work(report))))
            except pw_quick.NeedsOverwrite as error:
                self.events.put(("overwrite", str(error)))
            except pw_quick.Cancelled:
                self.events.put(("error", "Stopped. Press Send again to carry on where it stopped."))
            except (QuickError, OSError, EOFError, *ftplib.all_errors) as error:
                self.events.put(("error", str(error)))
            except Exception as error:  # a bug: say so rather than hang
                self.events.put(("error", f"Something went wrong: {type(error).__name__}: {error}"))
        self.worker = threading.Thread(target=body, daemon=True)
        self.worker.start()

    def pump(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    done, total, files, total_files, current, message = value
                    if total:
                        self.bar.configure(value=1000 * done / total)
                        self.status.set(message or f"{human(done)} of {human(total)} · file {files} of "
                                                   f"{total_files} · {current}")
                    elif message:
                        self.status.set(message)
                elif kind == "status":
                    self.status.set(value)
                elif kind == "done":
                    callback, result = value
                    self.busy(False)
                    if callback:
                        callback(result)
                elif kind == "overwrite":
                    self.busy(False)
                    answer = messagebox.askyesnocancel(
                        "The game is already on the PS5",
                        value + "\n\nYes: update its settings only (program, graphics, arguments, "
                        "resolution, preset). Quick, and keeps its saves.\n\n"
                        "No: replace it. Files this PC already sent at the same size are skipped, "
                        "but its saves and Windows settings on the PS5 are replaced.",
                        icon="question", parent=self.root)
                    if answer is None:
                        self.status.set("Nothing was sent.")
                    else:
                        self.send(overwrite=not answer, settings_only=answer)
                elif kind == "error":
                    self.busy(False)
                    self.status.set(value)
                    messagebox.showerror("prospero-win", value, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    # --- actions ---------------------------------------------------------------
    def choose_zip(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="Choose the game's zip",
                                          filetypes=(("Zip files", "*.zip"), ("All files", "*.*")))
        if path:
            self.open_game(path)

    def choose_folder(self) -> None:
        path = filedialog.askdirectory(parent=self.root, title="Choose the game's folder", mustexist=True)
        if path:
            self.open_game(path)

    def choose_base(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="Choose the base prefix",
                                          filetypes=(("Zip files", "*.zip"), ("All files", "*.*")))
        if path:
            self.base.set(path)
            self.remember()

    def open_game(self, path: str) -> None:
        self.game_path.set(path)
        self.status.set("Looking through the game's files…")
        self.bar.configure(mode="indeterminate")
        self.bar.start(15)
        if self.source:
            self.source.close()
        self.source, self.exes = None, []

        def work(report):
            source = pw_quick.Source(path)
            game, exes = pw_quick.suggest(source)
            return source, game, exes

        def done(result) -> None:
            self.bar.stop()
            self.bar.configure(mode="determinate", value=0)
            self.source, game, self.exes = result
            self.name.set(game.name)
            self.exe_box.configure(values=[exe.key for exe in self.exes])
            self.exe.set(game.exe)
            self.show_game(game)
            self.winedebug.set("")
            self.busy(False)
            size = sum(self.source.files.values())
            self.status.set(f"{len(self.source.files)} files, {human(size)}. {game.bits}-bit game. "
                            "Check the settings, then press Send to PS5.")
        self.run(work, done)
        # A failed scan leaves the bar spinning: stop it when the worker ends.
        self.root.after(200, self.stop_spinner_when_idle)

    def stop_spinner_when_idle(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(200, self.stop_spinner_when_idle)
        elif str(self.bar.cget("mode")) == "indeterminate":
            self.bar.stop()
            self.bar.configure(mode="determinate", value=0)

    def show_graphics(self, graphics: str) -> None:
        self.graphics.set(GRAPHICS_LABELS[graphics])
        self.detected.set("detected")

    def show_game(self, game: pw_quick.Game) -> None:
        """What the sender worked out for this program (tools/pw_autoconfig.py)."""
        self.found = (game.engine, game.checks)
        self.show_graphics(game.graphics)
        self.arguments.set(game.arguments)
        self.environment.set(pw_quick.format_environment(game.environment))
        lines = [f"Engine: {game.engine}"] if game.engine else []
        self.checks.set("\n".join(lines + [str(check) for check in game.checks]))

    def exe_changed(self, _event=None) -> None:
        exe = self.current_exe()
        if exe and self.source:
            exe, plan = pw_quick.autoconfigure(self.source, exe, self.exes)
            self.exe.set(exe.key)
            self.show_game(pw_quick.configured(self.source, exe, plan, self.name.get()))

    def current_exe(self) -> pw_quick.Executable | None:
        return next((exe for exe in self.exes if exe.key == self.exe.get()), None)

    def game_settings(self) -> pw_quick.Game:
        exe = self.current_exe()
        if exe is None:
            raise QuickError("Choose the program to start.")
        name = self.name.get().strip() or exe.key
        graphics = next(key for key, label in GRAPHICS_LABELS.items() if label == self.graphics.get())
        game = pw_quick.Game(name=name, slug=pw_quick.slugify(name), exe=exe.key, bits=exe.info.bits,
                             graphics=graphics, arguments=self.arguments.get().strip(),
                             desktop=self.desktop.get().strip(), preset=self.preset.get().strip(),
                             environment=pw_quick.parse_environment(self.environment.get()),
                             winedebug=self.winedebug.get().strip())
        game.engine, game.checks = getattr(self, "found", ("", []))
        game.check()
        return game

    def send(self, overwrite: bool = False, settings_only: bool = False) -> None:
        try:
            host, port = self.address()
            game = self.game_settings()
            base_path = self.base.get().strip()
            if not base_path or not Path(base_path).exists():
                raise QuickError(f"Choose the base prefix ({BASE_NAME}) first.")
        except QuickError as error:
            messagebox.showerror("prospero-win", str(error), parent=self.root)
            return
        self.remember()
        source = self.source

        def work(report):
            self.events.put(("status", "Opening the base prefix…"))
            base = pw_quick.Source(base_path)
            dxvk = None
            if game.graphics == "dxvk":
                self.events.put(("status", "Getting DXVK (downloaded once)…"))
                dxvk = pw_quick.dxvk_files(pw_quick.fetch_dxvk())
            self.events.put(("status", f"Connecting to {host}:{port}…"))
            remote = pw_quick.connect(host, port)
            try:
                sender = pw_quick.Sender(remote, game, source, base, dxvk=dxvk, host=host, report=report,
                                         cancel=self.cancel)
                if settings_only:
                    sender.update_settings()
                else:
                    sender.send(overwrite)
            finally:
                try:
                    remote.close()
                except (OSError, EOFError, *ftplib.all_errors):
                    pass
            return game

        def done(game) -> None:
            self.bar.configure(value=1000)
            self.status.set(f"Done. {game.name} is on your PS5: start prospero-win and pick it in the launcher.")
            messagebox.showinfo("prospero-win", f"{game.name} is on your PS5.\n\n"
                                "Start prospero-win and pick it in the launcher.", parent=self.root)
        self.run(work, done)

    def check_console(self) -> None:
        try:
            host, port = self.address()
        except QuickError as error:
            messagebox.showerror("prospero-win", str(error), parent=self.root)
            return
        self.remember()
        self.status.set(f"Connecting to {host}:{port}…")

        def work(report):
            remote = pw_quick.connect(host, port)
            try:
                app = remote.size(pw_quick.APP_CPU_DLL) is not None
                games = 0
                if remote.exists(f"{pw_quick.REMOTE_ROOT}/profiles"):
                    games = sum(1 for name in remote.listdir(f"{pw_quick.REMOTE_ROOT}/profiles")
                                if name.endswith(".profile"))
                return app, games
            finally:
                remote.close()

        def done(result) -> None:
            app, games = result
            if app:
                self.status.set(f"Connected. The prospero-win app is installed, with {games} game(s).")
            else:
                self.status.set("Connected, but the prospero-win app isn't installed on this PS5 yet. "
                                "Use \"Install or update the app…\" with the release zip.")
        self.run(work, done)

    def install_app(self) -> None:
        try:
            host, port = self.address()
        except QuickError as error:
            messagebox.showerror("prospero-win", str(error), parent=self.root)
            return
        path = filedialog.askopenfilename(parent=self.root, title="Choose the prospero-win release zip",
                                          filetypes=(("Zip files", "*.zip"), ("All files", "*.*")))
        if not path:
            return
        self.remember()

        def work(report):
            release = pw_quick.Source(path)
            remote = pw_quick.connect(host, port)
            messages = []

            def keep(progress):
                if progress.message:
                    messages.append(progress.message)
                report(progress)
            try:
                pw_quick.install_app(remote, release, keep, self.cancel)
            finally:
                remote.close()
                release.close()
            return messages[-1] if messages else "The app is installed."

        def done(message) -> None:
            self.bar.configure(value=1000)
            self.status.set(message[0].upper() + message[1:] + ".")
        self.run(work, done)

    def stop(self) -> None:
        self.cancel.set()
        self.status.set("Stopping…")

    def close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("prospero-win", "A transfer is running. Stop it and quit? "
                                       "Sending again later carries on where it stopped.", parent=self.root):
                return
            self.cancel.set()
        self.remember()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    app = App(root)
    # A game's zip or folder dropped on the .exe, or named on the command line
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        root.after(200, lambda: app.open_game(sys.argv[1]))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
