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
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pw_quick  # noqa: E402
from pw_quick import QuickError  # noqa: E402

VERSION = "4.19"
SECRET = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ&start_radio=1"
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


BACKGROUND = pw_quick.PRESETS.parent / "assets" / "background.png"


class Backdrop:
    """A picture behind the whole window. Tk can't make a widget see-through,
    so every frame and label shows its own piece of the picture: frames
    through a label lowered under their children, labels as their image with
    the text drawn on it. The picture is faded towards the window's colour
    beforehand (tools/assets), so the text stays easy to read."""

    def __init__(self, root: tk.Tk, path: Path, colour: str):
        # As #rrggbb: a photo image can't take Windows' names (SystemButtonFace)
        red, green, blue = (value >> 8 for value in root.winfo_rgb(colour))
        self.root, self.colour = root, f"#{red:02x}{green:02x}{blue:02x}"
        self.picture = tk.PhotoImage(master=root, file=str(path))
        self.pieces: dict[str, tk.PhotoImage] = {}
        self.behind: dict[str, tk.Label] = {}
        self.extra: dict[str, tuple[int, int]] = {}   # room a label adds around its picture
        self.pending = False
        # A label is as big as its picture then: padding would grow it each paint
        style = ttk.Style(root)
        for name in ("TLabel", "Hint.TLabel", "Title.TLabel"):
            style.configure(name, padding=0, borderwidth=0)
        style.configure("Hint.TLabel", foreground="#333333")  # grey on the picture is hard to read

    def cover(self, widget) -> None:
        """Gives widget and everything in it the picture, now and whenever
        the layout moves."""
        self.root.bind("<Configure>", self.moved, add="+")
        self.later()

    def moved(self, event) -> None:
        # Only a change of size: painting itself configures the labels
        piece = self.pieces.get(str(event.widget))
        if event.widget is self.root or (piece is not None and
                                         (piece.width(), piece.height()) != (event.width, event.height)):
            self.later()

    def later(self) -> None:
        if not self.pending:
            self.pending = True
            self.root.after(30, self.paint)

    def paint(self) -> None:
        self.pending = False
        try:
            self.paint_all()
        except tk.TclError:
            pass  # the window without its picture still works

    def paint_all(self) -> None:
        left = (self.picture.width() - self.root.winfo_width()) // 2
        stack = list(self.root.winfo_children())
        while stack:
            widget = stack.pop()
            kind = widget.winfo_class()
            if kind in ("TFrame", "TLabelframe"):
                self.fill(widget, left)
                stack.extend(child for child in widget.winfo_children() if child not in self.behind.values())
            elif kind == "TLabel":
                self.fill(widget, left)

    def fill(self, widget, left: int) -> None:
        width, height = widget.winfo_width(), widget.winfo_height()
        if width < 2 or height < 2:
            return
        x = widget.winfo_rootx() - self.root.winfo_rootx() + left
        y = widget.winfo_rooty() - self.root.winfo_rooty()
        piece = tk.PhotoImage(master=self.root, width=width, height=height)
        piece.put(self.colour, to=(0, 0, width, height))
        x1, y1 = max(x, 0), max(y, 0)
        x2, y2 = min(x + width, self.picture.width()), min(y + height, self.picture.height())
        if x2 > x1 and y2 > y1:
            piece.tk.call(piece, "copy", self.picture, "-from", x1, y1, x2, y2, "-to", x1 - x, y1 - y)
        key = str(widget)
        self.pieces[key] = piece
        if widget.winfo_class() == "TLabel":
            # Themes (Windows' vista) add room around a label's picture; the
            # picture then shrinks by that, or the label would grow each paint
            extra_x, extra_y = self.extra.get(key, (0, 0))
            if extra_x or extra_y:
                inner = tk.PhotoImage(master=self.root, width=max(width - extra_x, 1),
                                      height=max(height - extra_y, 1))
                inner.tk.call(inner, "copy", piece, "-from", extra_x // 2, extra_y // 2,
                              extra_x // 2 + max(width - extra_x, 1), extra_y // 2 + max(height - extra_y, 1))
                piece = self.pieces[key] = inner
            widget.configure(image=piece, compound="center")
            grow_x = widget.winfo_reqwidth() - width
            grow_y = widget.winfo_reqheight() - height
            if grow_x > 0 or grow_y > 0:
                self.extra[key] = (extra_x + max(grow_x, 0), extra_y + max(grow_y, 0))
                self.later()
            return
        label = self.behind.get(key)
        if label is None:
            label = self.behind[key] = tk.Label(widget, borderwidth=0, highlightthickness=0)
            label.place(x=0, y=0, relwidth=1, relheight=1)
            label.lower()
        label.configure(image=piece)


def shorten(path: str, width: int = 64) -> str:
    return path if len(path) <= width else "…" + path[-(width - 1):]


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
        # The base prefix shipped beside the program wins over a remembered one,
        # so a new release's prefix (fonts, decoders) is used without choosing it.
        self.base = tk.StringVar(value=str(default_base) if default_base.exists() else settings.get("base", ""))
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
        self.transfer = tk.StringVar()    # speed, connections and their files, while sending
        self.samples: deque[tuple[float, int]] = deque()
        self.elf_port = tk.StringVar(value=str(settings.get("elf_port", pw_quick.ELF_PORT)))
        self.payloads: list[dict] = [entry for entry in settings.get("payloads", [])
                                     if isinstance(entry, dict) and entry.get("path")]
        self.injecting = False

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
        ttk.Entry(ps5, textvariable=self.host, width=15).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(ps5, text="FTP port").grid(row=0, column=2, sticky="e")
        ttk.Entry(ps5, textvariable=self.port, width=7).grid(row=0, column=3, sticky="w", padx=8)
        self.check_button = ttk.Button(ps5, text="Check", command=self.check_console)
        self.check_button.grid(row=0, column=4)
        self.logs_button = ttk.Button(ps5, text="Get logs", command=self.get_logs)
        self.logs_button.grid(row=0, column=5, padx=(6, 0))

        loader = ttk.LabelFrame(outer, text="Payloads", padding=10)
        loader.grid(sticky="ew", pady=(0, 10))
        loader.columnconfigure(0, weight=1)
        self.payload_list = tk.Listbox(loader, height=4, selectmode="multiple", exportselection=False,
                                       activestyle="none")
        self.payload_list.grid(row=0, column=0, rowspan=3, sticky="nsew")
        self.payload_list.bind("<<ListboxSelect>>", lambda event: self.remember())
        side = ttk.Frame(loader)
        side.grid(row=0, column=1, rowspan=3, sticky="n", padx=(8, 0))
        port_row = ttk.Frame(side)
        port_row.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(port_row, text="ELF port").grid(row=0, column=0, sticky="w")
        ttk.Entry(port_row, textvariable=self.elf_port, width=7).grid(row=0, column=1, padx=(6, 0))
        buttons_row = ttk.Frame(side)
        buttons_row.grid(row=1, column=0, sticky="ew")
        ttk.Button(buttons_row, text="Add…", command=self.add_payloads).grid(row=0, column=0)
        ttk.Button(buttons_row, text="Remove", command=self.remove_payloads).grid(row=0, column=1, padx=(4, 0))
        ttk.Button(buttons_row, text="↑", width=3, command=lambda: self.move_payload(-1)).grid(row=0, column=2, padx=(4, 0))
        ttk.Button(buttons_row, text="↓", width=3, command=lambda: self.move_payload(1)).grid(row=0, column=3, padx=(4, 0))
        self.inject_button = ttk.Button(side, text="Inject selected", command=self.inject)
        self.inject_button.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(loader, style="Hint.TLabel", wraplength=580, justify="left",
                  text="Click payloads to select them; they're sent top to bottom, 2 seconds apart, "
                       "to the PS5's ELF loader at the IP address above.").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.show_payloads()

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
        ttk.Label(outer, textvariable=self.transfer, style="Hint.TLabel", wraplength=600,
                  justify="left").grid(sticky="w")

        buttons = ttk.Frame(outer)
        buttons.grid(sticky="ew", pady=(12, 0))
        buttons.columnconfigure(0, weight=1)
        self.app_button = ttk.Button(buttons, text="Install or update the app…", command=self.install_app)
        self.app_button.grid(row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.cancel_button.grid(row=0, column=1, padx=(0, 8))
        self.send_button = ttk.Button(buttons, text="Send to PS5", command=self.send, state="disabled")
        self.send_button.grid(row=0, column=2)

        if BACKGROUND.is_file():
            try:
                Backdrop(root, BACKGROUND, style.lookup("TFrame", "background") or "#f0f0f0").cover(outer)
            except tk.TclError:
                pass  # no picture is better than no window
        pi = tk.Label(root, text="π", fg="black", bg=style.lookup("TFrame", "background") or "#f0f0f0",
                      font=("Segoe UI", 8), borderwidth=0, padx=0, pady=0)
        pi.place(relx=1.0, rely=1.0, x=-3, y=-1, anchor="se")
        pi.bind("<Control-Shift-Button-1>", lambda _event: webbrowser.open(SECRET))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.pump)

    # --- helpers ---------------------------------------------------------------
    def set_fields(self, state: str) -> None:
        resolution = self.fields[3]
        for widget in self.fields:
            choice_only = isinstance(widget, ttk.Combobox) and widget is not resolution
            widget.configure(state="readonly" if state == "normal" and choice_only else state)

    def busy(self, on: bool) -> None:
        for button in (self.zip_button, self.folder_button, self.app_button, self.check_button,
                       self.logs_button):
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
        chosen = set(self.payload_list.curselection())
        for index, entry in enumerate(self.payloads):
            entry["on"] = index in chosen
        save_settings({"host": self.host.get().strip(), "port": self.port.get().strip(),
                       "base": self.base.get().strip(), "desktop": self.desktop.get().strip(),
                       "elf_port": self.elf_port.get().strip(), "payloads": self.payloads})

    # --- payloads ----------------------------------------------------------------
    def show_payloads(self) -> None:
        self.payload_list.delete(0, "end")
        for index, entry in enumerate(self.payloads):
            path = Path(entry["path"])
            self.payload_list.insert("end", path.name + ("" if path.is_file() else "  (file missing)"))
            if entry.get("on", True):
                self.payload_list.selection_set(index)

    def add_payloads(self) -> None:
        paths = filedialog.askopenfilenames(parent=self.root, title="Choose payloads to send to the PS5",
                                            filetypes=(("Payloads", "*.elf *.bin"), ("All files", "*.*")))
        known = {entry["path"] for entry in self.payloads}
        self.remember()
        self.payloads += [{"path": path, "on": True} for path in paths if path not in known]
        self.show_payloads()
        self.remember()

    def remove_payloads(self) -> None:
        chosen = set(self.payload_list.curselection())
        if not chosen:
            self.status.set("Click the payloads to remove first.")
            return
        self.payloads = [entry for index, entry in enumerate(self.payloads) if index not in chosen]
        self.show_payloads()
        self.remember()

    def move_payload(self, step: int) -> None:
        chosen = list(self.payload_list.curselection())
        active = self.payload_list.index("active")
        index = chosen[0] if len(chosen) == 1 else active
        target = index + step
        if not 0 <= index < len(self.payloads) or not 0 <= target < len(self.payloads):
            return
        self.remember()
        self.payloads[index], self.payloads[target] = self.payloads[target], self.payloads[index]
        self.show_payloads()
        self.payload_list.activate(target)
        self.remember()

    def inject(self) -> None:
        if self.injecting:
            return
        host = self.host.get().strip()
        if not host:
            messagebox.showerror("prospero-win", "Type the PS5's IP address first.", parent=self.root)
            return
        try:
            port = int(self.elf_port.get().strip())
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            messagebox.showerror("prospero-win", f"{self.elf_port.get()!r} is not a port number "
                                 f"(the ELF loader usually uses {pw_quick.ELF_PORT}).", parent=self.root)
            return
        chosen = [self.payloads[index]["path"] for index in self.payload_list.curselection()]
        if not chosen:
            messagebox.showerror("prospero-win", "Add payloads with Add…, then click the ones to send.",
                                 parent=self.root)
            return
        missing = [path for path in chosen if not Path(path).is_file()]
        if missing:
            messagebox.showerror("prospero-win", "These files are gone:\n" + "\n".join(missing), parent=self.root)
            return
        self.remember()
        self.injecting = True
        self.inject_button.configure(state="disabled")

        def body() -> None:
            try:
                count = pw_quick.send_payloads(host, port, chosen,
                                               say=lambda text: self.events.put(("status", text)))
                self.events.put(("injected", f"Sent {count} payload{'s' if count != 1 else ''} to {host}:{port}."))
            except (QuickError, OSError) as error:
                self.events.put(("injected", f"error: {error}"))
        threading.Thread(target=body, daemon=True).start()

    def run(self, work, done=None) -> None:
        """work(report) in the background; done(result) back on the window."""
        self.cancel.clear()
        self.busy(True)

        self.samples.clear()
        self.transfer.set("")

        def report(progress: pw_quick.Progress) -> None:
            self.events.put(("progress", (progress.done_bytes, progress.total_bytes, progress.done_files,
                                          progress.total_files, progress.current, progress.message,
                                          progress.connections, tuple(progress.active.copy().values()))))

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

    def show_transfer(self, done: int, total: int, files: int, total_files: int,
                      connections: int, active: tuple[str, ...]) -> None:
        """Speed over the last few seconds, and what each connection is sending."""
        now = time.monotonic()
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[0][0] > 3:
            self.samples.popleft()
        then, before = self.samples[0]
        speed = f"{human(max(done - before, 0) / (now - then))}/s" if now - then >= 0.5 else "…"
        lines = [f"{speed} · {human(done)} of {human(total)} · file {files} of {total_files} · "
                 f"{connections} connection{'s' if connections != 1 else ''}"]
        if active:
            lines.append("   ".join(f"{index}: {shorten(name.rsplit('/', 1)[-1], 28)}"
                                    for index, name in enumerate(active, start=1)))
        self.transfer.set("\n".join(lines))

    def pump(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    done, total, files, total_files, current, message, connections, active = value
                    if total:
                        self.bar.configure(value=1000 * done / total)
                        self.status.set(message or f"{human(done)} of {human(total)} · file {files} of "
                                                   f"{total_files} · {current}")
                        self.show_transfer(done, total, files, total_files, connections, active)
                    elif message:
                        self.status.set(message)
                elif kind == "status":
                    self.status.set(value)
                elif kind == "injected":
                    self.injecting = False
                    self.inject_button.configure(state="normal")
                    if value.startswith("error: "):
                        self.status.set(value[7:])
                        messagebox.showerror("prospero-win", value[7:], parent=self.root)
                    else:
                        self.status.set(value)
                elif kind == "done":
                    callback, result = value
                    self.transfer.set("")
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
                    self.transfer.set("")
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
            mono = None
            if game.mono and (here() / f"{pw_quick.MONO_NAME}.zip").exists():
                mono = pw_quick.Source(here() / f"{pw_quick.MONO_NAME}.zip")
            self.events.put(("status", f"Connecting to {host}:{port}…"))
            remote = pw_quick.connect(host, port)
            try:
                sender = pw_quick.Sender(remote, game, source, base, dxvk=dxvk, host=host, report=report,
                                         cancel=self.cancel, mono=mono)
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

    def get_logs(self) -> None:
        try:
            host, port = self.address()
        except QuickError as error:
            messagebox.showerror("prospero-win", str(error), parent=self.root)
            return
        self.remember()
        self.status.set(f"Getting the logs from {host}:{port}…")
        stamp = time.strftime("%Y-%m-%d-%H%M%S")

        def work(report):
            remote = pw_quick.connect(host, port)
            try:
                return pw_quick.fetch_logs(remote, here() / "logs", stamp)
            finally:
                remote.close()

        def done(result) -> None:
            out, newest = result
            self.status.set(f"Saved {out}" + (f" (the newest run is {newest})" if newest else "") + ".")
            try:
                if sys.platform == "win32":
                    subprocess.Popen(["explorer", "/select,", str(out)])
            except OSError:
                pass
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
