#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Send an already-installed Windows game to the console in one step.

tools/pw_install.py and tools/pw_prefix.py cover every game, but they need a
Linux PC with the pinned Wine, because the console cannot make a prefix
(wineboot is off there). Many games need nothing an installer does: a folder
copied from a Windows PC, a GOG or Steam install, a zip of either. For
those, this tool needs no Wine at all:

- the prefix is a base prefix, made once with the pinned Wine by
  tools/pw_base_prefix.py and shared as a zip (it holds only Wine's files);
- the game's files go to C:\\Games\\<slug> inside it, read straight out of
  the game's zip or folder;
- the profile is written from the game's executable: its architecture from
  the PE header, the graphics backend from the DLLs it and the game's own
  DLLs import (Direct3D 8-11 through DXVK, OpenGL, or GDI);
- DXVK's DLLs, when the game uses Direct3D, come from the pinned release
  (tools/pw_install.py's DXVK_RELEASES);
- wowprospero.dll comes from the app already installed on the console;
- everything is streamed over FTP (tools/pw_prefix.py's FtpRemote), and a
  record of each file the console has lets an interrupted send resume.

tools/pw_gui.py puts a window on this; the command line is for scripts:

    pw_quick.py GAME.zip|DIR --base BASE.zip|DIR --host IP [--port 2121]
        [--name NAME] [--slug SLUG] [--exe PATH] [--graphics auto|gdi|dxvk|opengl]
        [--desktop WxH] [--preset NAME] [--overwrite]
    pw_quick.py --install-app prospero-win-<version>.zip --host IP
"""
from __future__ import annotations

import argparse
import fnmatch
import ftplib
import io
import json
import os
import posixpath
import re
import struct
import sys
import tarfile
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pw_autoconfig  # noqa: E402
import pw_prefix  # noqa: E402
from pw_prefix import CHUNK, CPU_DLL, REGISTRY, FtpRemote, to_console  # noqa: E402

REMOTE_ROOT = "/data/prospero-win"
# The app's own folder, where the release's PPSA99995 folder goes.
TITLE_ID = "PPSA99995"
APP_ROOT = f"/data/homebrew/{TITLE_ID}"
# The translator the app ships (tools/package_release.sh), which a prefix
# needs in its own system32 (pw_prefix.CPU_DLL).
APP_CPU_DLL = f"{APP_ROOT}/win/wine/lib/wine/x86_64-windows/wowprospero.dll"
GAMES = "drive_c/Games"
# The C: and Z: drives, as pw_prefix.py's push writes a prefix's links.
BASE_LINKS = f"dosdevices/{pw_prefix.LINK_TABLE}"
DXVK_VERSION = "2.6.2"
DXVK_DLLS = ("d3d8", "d3d9", "d3d10core", "d3d11", "dxgi")
GRAPHICS = ("auto", "gdi", "dxvk", "opengl")
SCALING = ("fit", "integer", "stretch")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# Files a zip or a copied folder carries that the game does not need.
JUNK = ("__MACOSX/*", "*/.DS_Store", ".DS_Store", "Thumbs.db", "*/Thumbs.db", "desktop.ini", "*/desktop.ini")
# Executables that are rarely the game itself.
NOT_THE_GAME = re.compile(
    r"unins|setup|install|vcredist|vc_redist|dxsetup|dxwebsetup|directx|redist|crash|report|"
    r"update|patch|config|settings|dotnet|ue4prereq|prereq|easyanticheat|battleye|^be_|cleanup|"
    r"launcher|register|activation|helper|server|dedicated|benchmark|editor|tool|7z|unrar|python|java")
D3D = re.compile(r"^(d3d8|d3d9|d3d10(_1)?(core)?|d3d11|dxgi)\.dll$")
SCAN_DLLS, SCAN_LIMIT = 400, 96 << 20
STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
                 or Path.home() / ".local" / "state") / "prospero-win" / "quick"


class QuickError(Exception):
    pass


class NeedsOverwrite(QuickError):
    """The console already has this game, and this PC never sent it."""


class Cancelled(Exception):
    pass


# --- where files come from ---------------------------------------------------
class Source:
    """A zip or a folder, as a flat list of files (posix paths) and folders.
    A zip whose files all sit in one top folder is read from inside it."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.files: dict[str, int] = {}
        self.dirs: set[str] = set()
        self.zip: zipfile.ZipFile | None = None
        self.links = 0      # symbolic links in a folder, which are left out
        self.lock = threading.Lock()
        if self.path.is_dir():
            self._scan_dir()
        elif zipfile.is_zipfile(self.path):
            self._scan_zip()
        else:
            raise QuickError(f"{self.path} is neither a folder nor a zip")
        if not self.files:
            raise QuickError(f"{self.path} holds no files")

    def _scan_dir(self) -> None:
        root = self.path
        for directory, subdirs, names in os.walk(root):
            relative = Path(directory).relative_to(root).as_posix()
            relative = "" if relative == "." else relative
            for name in list(subdirs):
                if os.path.islink(os.path.join(directory, name)):
                    subdirs.remove(name)
                    self.links += 1
            for name in names:
                key = posixpath.join(relative, name) if relative else name
                if os.path.islink(os.path.join(directory, name)):
                    self.links += 1
                elif not junk(key) and os.path.isfile(os.path.join(directory, name)):
                    self.files[key] = os.path.getsize(os.path.join(directory, name))
            if relative:
                self.dirs.add(relative)
        self.prefix = ""

    def _scan_zip(self) -> None:
        self.zip = zipfile.ZipFile(self.path)
        names = []
        for info in self.zip.infolist():
            name = info.filename.replace("\\", "/")
            if junk(name) or name.startswith("/") or ".." in PurePosixPath(name).parts:
                continue
            names.append((name, info))
        tops = {name.split("/", 1)[0] for name, info in names if "/" in name.rstrip("/") or info.is_dir()}
        loose = [name for name, info in names if "/" not in name.rstrip("/") and not info.is_dir()]
        self.prefix = f"{tops.pop()}/" if len(tops) == 1 and not loose else ""
        self.members: dict[str, zipfile.ZipInfo] = {}
        for name, info in names:
            if not name.startswith(self.prefix):
                continue
            key = name[len(self.prefix):].rstrip("/")
            if not key:
                continue
            if info.is_dir():
                self.dirs.add(key)
            else:
                self.files[key] = info.file_size
                self.members[key] = info
        for key in list(self.files):
            parent = posixpath.dirname(key)
            while parent:
                self.dirs.add(parent)
                parent = posixpath.dirname(parent)

    @property
    def name(self) -> str:
        """The game's likely name: the zip's top folder, or the file or folder name."""
        if self.prefix:
            return self.prefix.rstrip("/")
        return self.path.stem if self.zip else self.path.name

    def open(self, key: str):
        if self.zip is not None:
            return self.zip.open(self.members[key])
        return open(self.path / key, "rb")

    def read(self, key: str, limit: int | None = None) -> bytes:
        with self.lock, self.open(key) as stream:
            return stream.read() if limit is None else stream.read(limit)

    def close(self) -> None:
        if self.zip is not None:
            self.zip.close()


def junk(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in JUNK)


# --- reading executables -------------------------------------------------------
@dataclass
class PeInfo:
    bits: int
    imports: set[str]
    gui: bool


def pe_info(data: bytes) -> PeInfo | None:
    """Architecture, subsystem and imported DLLs (normal and delay-loaded) of
    a PE image, or None when data is not one."""
    try:
        if data[:2] != b"MZ":
            return None
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            return None
        sections, optional_size = struct.unpack_from("<H12xH", data, pe + 6)
        optional = pe + 24
        magic = struct.unpack_from("<H", data, optional)[0]
        if magic not in (0x10B, 0x20B):
            return None
        bits = 32 if magic == 0x10B else 64
        subsystem = struct.unpack_from("<H", data, optional + 68)[0]
        directories = optional + (96 if bits == 32 else 112)
        count = struct.unpack_from("<I", data, directories - 4)[0]
        table = optional + optional_size
        spans = []
        for index in range(sections):
            size, address, raw_size, raw = struct.unpack_from("<4xIIII", data, table + 40 * index + 4)
            spans.append((address, max(size, raw_size), raw))

        def offset(rva: int) -> int | None:
            for address, size, raw in spans:
                if address <= rva < address + size:
                    return rva - address + raw
            return rva if rva < (spans[0][2] if spans else len(data)) else None

        def string(rva: int) -> str:
            start = offset(rva)
            if start is None or start >= len(data):
                return ""
            end = data.find(b"\0", start, start + 256)
            return data[start:end if end >= 0 else start + 256].decode("latin-1").lower()

        imports: set[str] = set()
        for index, stride, name_at in ((1, 20, 12), (13, 32, 4)):   # imports, delay imports
            if count <= index:
                continue
            rva = struct.unpack_from("<I", data, directories + 8 * index)[0]
            at = offset(rva) if rva else None
            while at is not None and at + stride <= len(data) and len(imports) < 512:
                entry = data[at:at + stride]
                if not any(entry):
                    break
                name_rva = struct.unpack_from("<I", entry, name_at)[0]
                if name_rva:
                    name = string(name_rva)
                    if name:
                        imports.add(name)
                at += stride
        return PeInfo(bits, imports, subsystem == 2)
    except (struct.error, IndexError):
        return None


@dataclass
class Executable:
    key: str
    size: int
    info: PeInfo
    score: float


def find_executables(source: Source, name: str = "") -> list[Executable]:
    """The source's Windows programs, likeliest to be the game first."""
    wanted = re.sub(r"[^a-z0-9]", "", name.lower())
    found = []
    for key, size in source.files.items():
        if not key.lower().endswith(".exe") or size < 1024:
            continue
        info = pe_info(source.read(key, limit=min(size, SCAN_LIMIT)))
        if info is None:
            continue
        stem = re.sub(r"[^a-z0-9]", "", PurePosixPath(key).stem.lower())
        score = min(size, 64 << 20) / (1 << 20)          # bigger is likelier, up to a point
        score -= 25 * key.count("/")                      # the top folder is likelier
        if NOT_THE_GAME.search(PurePosixPath(key).stem.lower()):
            score -= 200
        if not info.gui:
            score -= 50
        if wanted and stem and (stem in wanted or wanted in stem):
            score += 100
        found.append(Executable(key, size, info, score))
    return sorted(found, key=lambda exe: (-exe.score, exe.key))


def game_imports(source: Source, exe: Executable) -> set[str]:
    """What the executable and the game's own DLLs import. Engines load
    their renderer from a DLL (Half-Life's hw.dll, Source's
    shaderapidx9.dll), so those count too. Read once per source."""
    cache = source.__dict__.setdefault("_dll_imports", None)
    if cache is None:
        cache = set()
        folder = posixpath.dirname(exe.key)
        dlls = sorted((key for key in source.files if key.lower().endswith(".dll")),
                      key=lambda key: (posixpath.dirname(key) != folder, key))
        for key in dlls[:SCAN_DLLS]:
            if source.files[key] > SCAN_LIMIT:
                continue
            info = pe_info(source.read(key))
            if info:
                cache.update(info.imports)
        source._dll_imports = cache
    return cache | exe.info.imports


def graphics_of(imports: Iterable[str]) -> str | None:
    imports = set(imports)
    if any(D3D.match(name) for name in imports):
        return "dxvk"
    if "opengl32.dll" in imports:
        return "opengl"
    if "ddraw.dll" in imports:
        return "auto"
    return None


def guess_graphics(source: Source, exe: Executable) -> str:
    """dxvk for Direct3D 8-11, opengl, or gdi, from what the executable and
    the game's own DLLs import. Direct3D wins over OpenGL: DXVK is the
    backend every build has."""
    direct = graphics_of(exe.info.imports)
    if direct == "dxvk":
        return direct
    found = graphics_of(game_imports(source, exe))
    if "dxvk" in (direct, found):
        return "dxvk"
    return direct or found or "gdi"


def autoconfigure(source: Source, exe: Executable, exes: list[Executable]) -> tuple[Executable, Plan]:
    """The executable to start and what it needs (tools/pw_autoconfig.py)."""
    plan = pw_autoconfig.plan(source, exe, game_imports(source, exe), guess_graphics(source, exe))
    better = next((other for other in exes if other.key == plan.exe), None)
    if better is not None and better is not exe:
        followed = pw_autoconfig.plan(source, better, game_imports(source, better),
                                      guess_graphics(source, better))
        followed.checks = [check for check in plan.checks if plan.exe in check.text] + followed.checks
        return better, followed
    return exe, plan


# --- the game and its profile --------------------------------------------------
def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64].strip("-")
    return slug if SLUG.match(slug or "") else "game"


@dataclass
class Game:
    name: str
    slug: str
    exe: str                       # inside the game's folder, posix
    bits: int
    graphics: str = "dxvk"
    arguments: str = ""
    desktop: str = "1920x1080"
    scaling: str = "fit"
    preset: str = ""
    show_fps: bool = True
    # Variables for the game's process, through the prefix's HKCU\Environment
    # (Wine's ntdll adds them to every process), and the profile's Wine log
    # channels ([debug] winedebug).
    environment: dict[str, str] = field(default_factory=dict)
    winedebug: str = ""
    engine: str = ""
    checks: list = field(default_factory=list)     # pw_autoconfig.Check

    def check(self) -> None:
        if not SLUG.match(self.slug):
            raise QuickError(f"{self.slug!r} is not a usable id: lower-case letters, digits and dashes")
        if self.graphics not in GRAPHICS:
            raise QuickError(f"graphics {self.graphics!r} is not one of {', '.join(GRAPHICS)}")
        if self.scaling not in SCALING:
            raise QuickError(f"scaling {self.scaling!r} is not one of {', '.join(SCALING)}")
        if not re.fullmatch(r"\d{3,5}x\d{3,5}", self.desktop):
            raise QuickError(f"desktop {self.desktop!r} is not WIDTHxHEIGHT")
        if any(ch in self.name for ch in "\r\n") or any(ch in self.arguments for ch in "\r\n"):
            raise QuickError("the name and arguments must be one line")
        if self.preset and not re.fullmatch(r"[A-Za-z0-9_.-]+", self.preset):
            raise QuickError(f"preset {self.preset!r} is not a preset file name")
        for name, value in self.environment.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) or any(ch in value for ch in "\r\n\0"):
                raise QuickError(f"{name}={value} is not an environment variable")
        if self.winedebug and not re.fullmatch(r"[A-Za-z0-9_+,.-]+", self.winedebug):
            raise QuickError(f"{self.winedebug!r} is not a list of Wine log channels")

    @property
    def folder(self) -> str:
        """Where the game's files go, in the prefix."""
        return f"{GAMES}/{self.slug}"

    def windows(self, key: str) -> str:
        """A path in the prefix as the game sees it: C:\\Games\\..."""
        parts = PurePosixPath(key).relative_to("drive_c").parts
        return str(PureWindowsPath("C:\\", *parts))

    def profile(self) -> str:
        exe = f"{self.folder}/{self.exe}"
        lines = [f"; {self.name}: generated by tools/pw_quick.py from the game's own files."]
        if self.engine:
            lines.append(f"; engine: {self.engine}")
        lines += [f"; {check}" for check in self.checks]
        lines += [
                 "[application]", f"id = {self.slug}", f"name = {self.name}",
                 f"executable = {self.windows(exe)}",
                 f"working_directory = {self.windows(posixpath.dirname(exe))}"]
        if self.arguments:
            lines.append(f"arguments = {self.arguments}")
        if self.graphics == "dxvk":
            lines.append(f"dll_overrides = {','.join(DXVK_DLLS)}=n")
        elif self.graphics == "opengl":
            lines.append("dll_overrides = opengl32=b")
        lines += [f"prefix = {self.slug}", "runtime = wine-wow64", f"architecture = pe{self.bits}",
                  f"graphics = {self.graphics}",
                  "", "[display]", f"desktop = {self.desktop}", f"scaling = {self.scaling}"]
        if not self.show_fps:
            lines.append("show_fps = false")
        if self.preset:
            lines += ["", "[input]", f"preset = {self.preset}"]
        if self.winedebug:
            lines += ["", "[debug]", f"winedebug = {self.winedebug}"]
        return "\n".join(lines) + "\n"


def suggest(source: Source) -> tuple[Game, list[Executable]]:
    """A first guess at the game's settings, and every executable it could be."""
    name = re.sub(r"[_.]+", " ", source.name).strip() or "Game"
    exes = find_executables(source, name)
    if not exes:
        raise QuickError(f"{source.path} has no Windows executable (.exe)")
    exe, plan = autoconfigure(source, exes[0], exes)
    return configured(source, exe, plan, name), exes


def configured(source: Source, exe: Executable, plan: pw_autoconfig.Plan, name: str) -> Game:
    environment = suggest_environment(source)
    environment.update(plan.environment)
    return Game(name=name, slug=slugify(name), exe=exe.key, bits=exe.info.bits,
                graphics=plan.graphics or guess_graphics(source, exe), arguments=" ".join(plan.arguments),
                environment=environment, engine=plan.engine, checks=plan.checks)


# .NET's runtime (Godot's C# builds, MonoGame, ...) starts by reserving
# address space for its garbage collector: 256 GB or more since .NET 7. The
# console refuses reservations much past 4 GB (ProbeTris: 4 GB reserved, 16 GB
# refused), and reports about 512 MB of RAM with none free, which the GC sizes
# itself from. A Godot C# game froze right after coreclr.dll started. These
# cap the GC's reservation and heap at sizes the console grants. W^X (the
# writable and executable double mapping of generated code, on since .NET 7)
# goes off too: one less mapping trick for the console's Wine.
DOTNET = {
    "DOTNET_GCRegionRange": "0xC0000000",      # 3 GB of address space for the GC
    "DOTNET_GCHeapHardLimit": "0x80000000",    # 2 GB of heap
    "DOTNET_EnableWriteXorExecute": "0",
}


def suggest_environment(source: Source) -> dict[str, str]:
    names = {posixpath.basename(key).lower() for key in source.files}
    return dict(DOTNET) if "coreclr.dll" in names else {}


def parse_environment(text: str) -> dict[str, str]:
    """NAME=VALUE pairs separated by ';' or new lines."""
    out = {}
    for item in re.split(r"[;\n]", text):
        if item.strip():
            name, sep, value = item.strip().partition("=")
            if not sep:
                raise QuickError(f"{item.strip()!r} is not NAME=VALUE")
            out[name.strip()] = value.strip()
    return out


def format_environment(environment: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in environment.items())


def set_environment(user_reg: bytes, environment: dict[str, str]) -> bytes:
    """user.reg with these values in HKCU\\Environment (made if missing)."""
    return set_values(user_reg, "Environment", environment)


def set_values(user_reg: bytes, key: str, values: dict[str, str]) -> bytes:
    """user.reg with these string values in HKCU\\key (made if missing)."""
    if not values:
        return user_reg
    def quote(text: str) -> str:
        text = text.replace("\\", "\\\\").replace('"', '\\"')
        # Wine's .reg files spell what isn't ASCII as \xXXXX
        return '"' + "".join(ch if ord(ch) < 0x80 else f"\\x{ord(ch):04x}" for ch in text) + '"'
    header = "[" + key.replace("\\", "\\\\") + "]"
    lines = user_reg.decode("utf-8", "surrogateescape").split("\n")
    start = next((i for i, line in enumerate(lines)
                  if line.lower() == header.lower() or line.lower().startswith(header.lower() + " ")), None)
    if start is None:
        if lines and lines[-1] == "":
            lines.pop()
        lines += ["", f"{header} 0"]
        start = len(lines) - 1
        lines.append("")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    names = {quote(name).lower() for name in values}
    body = [line for line in lines[start + 1:end]
            if line.split("=", 1)[0].lower() not in names and line != ""]
    body += [f"{quote(name)}={quote(value)}" for name, value in values.items()]
    lines[start + 1:end] = body + [""]
    return "\n".join(lines).encode("utf-8", "surrogateescape")


# Fonts the console lacks (ProbeTris locale.fonts.missing), drawn with the
# metric-compatible ones tools/pw_base_prefix.py --fonts adds, through
# Wine's HKCU\Software\Wine\Fonts\Replacements.
FONT_KEY = "Software\\Wine\\Fonts\\Replacements"
FONT_REPLACEMENTS = {
    "LiberationSans-Regular.ttf": {
        "Arial": "Liberation Sans", "Helvetica": "Liberation Sans", "Verdana": "Liberation Sans",
        "Segoe UI": "Liberation Sans", "Microsoft Sans Serif": "Liberation Sans", "MS Sans Serif": "Liberation Sans",
        "Trebuchet MS": "Liberation Sans", "Calibri": "Liberation Sans"},
    "LiberationSerif-Regular.ttf": {"Times New Roman": "Liberation Serif", "Georgia": "Liberation Serif",
                                    "Cambria": "Liberation Serif"},
    "LiberationMono-Regular.ttf": {"Courier New": "Liberation Mono", "Consolas": "Liberation Mono",
                                   "Lucida Console": "Liberation Mono"},
    "ipag.ttf": {name: "IPAGothic" for name in (
        "MS Gothic", "MS PGothic", "MS UI Gothic", "Meiryo", "Meiryo UI", "Yu Gothic",
        "\uff2d\uff33 \u30b4\u30b7\u30c3\u30af", "\uff2d\uff33 \uff30\u30b4\u30b7\u30c3\u30af")},
}


def font_replacements(base: Source) -> dict[str, str]:
    """The replacements for the fonts the base prefix brings."""
    have = {posixpath.basename(key).lower() for key in base.files if key.lower().startswith("drive_c/windows/fonts/")}
    out: dict[str, str] = {}
    for font, names in FONT_REPLACEMENTS.items():
        if font.lower() in have:
            out.update(names)
    return out


# --- DXVK ------------------------------------------------------------------------
def dxvk_files(archive: Path, version: str = DXVK_VERSION) -> dict[str, bytes]:
    """The prefix's DXVK DLLs from a release tarball: x64 in system32, x32 in syswow64."""
    out = {}
    with tarfile.open(archive) as release:
        for bits, folder in (("x64", "system32"), ("x32", "syswow64")):
            for dll in DXVK_DLLS:
                member = release.extractfile(f"dxvk-{version}/{bits}/{dll}.dll")
                if member is None:
                    raise QuickError(f"{archive} has no {bits}/{dll}.dll")
                out[f"drive_c/windows/{folder}/{dll}.dll"] = member.read()
    return out


def fetch_dxvk(version: str = DXVK_VERSION) -> Path:
    """The pinned DXVK release, downloaded once and checked by its hash."""
    import pw_install
    url, digest = pw_install.dxvk_release(version)
    try:
        return pw_install.fetch(url, digest, f"dxvk-{version}.tar.gz")
    except (OSError, pw_install.InstallError) as error:
        raise QuickError(f"could not get DXVK {version}: {error}") from error


# --- sending ---------------------------------------------------------------------
@dataclass
class Item:
    key: str                                       # in the prefix
    size: int
    open: Callable[[], object] | None = None       # a stream
    data: bytes | None = None                      # or the bytes themselves


@dataclass
class Progress:
    done_bytes: int = 0
    total_bytes: int = 0
    done_files: int = 0
    total_files: int = 0
    current: str = ""
    skipped: int = 0
    message: str = ""
    log: list[str] = field(default_factory=list)


class CountingReader:
    """A stream read CHUNK at a time, counted, and stopped when cancelled."""

    def __init__(self, stream, tick: Callable[[int], None], cancel: threading.Event):
        self.stream, self.tick, self.cancel, self.size = stream, tick, cancel, 0

    def read(self, size: int = -1) -> bytes:
        if self.cancel.is_set():
            raise Cancelled()
        data = self.stream.read(CHUNK if size is None or size < 0 or size > CHUNK else size)
        self.size += len(data)
        self.tick(len(data))
        return data


class Sender:
    """Puts one game on the console: its prefix, its profile, its launcher entry."""

    def __init__(self, remote, game: Game, source: Source, base: Source,
                 dxvk: dict[str, bytes] | None = None, cpu_dll: bytes | None = None,
                 remote_root: str = REMOTE_ROOT, state_dir: Path = STATE_DIR, host: str = "console",
                 report: Callable[[Progress], None] | None = None, cancel: threading.Event | None = None):
        game.check()
        if game.exe not in source.files:
            raise QuickError(f"{game.exe} is not in {source.path}")
        if "system.reg" not in base.files or "user.reg" not in base.files:
            raise QuickError(f"{base.path} is not a base prefix: it has no system.reg and user.reg")
        if BASE_LINKS not in base.files:
            raise QuickError(f"{base.path} has no {BASE_LINKS}: make the base prefix with "
                             "tools/pw_base_prefix.py, which keeps Wine's links the way the console reads them")
        if game.graphics == "dxvk" and not dxvk:
            raise QuickError("the game uses DXVK, and no DXVK DLLs were given")
        self.remote, self.game, self.source, self.base = remote, game, source, base
        self.dxvk, self.cpu_dll = (dxvk or {}) if game.graphics == "dxvk" else {}, cpu_dll
        self.root = remote_root.rstrip("/")
        self.remote_prefix = f"{self.root}/prefixes/{game.slug}"
        self.state_path = Path(state_dir) / f"{re.sub(r'[^A-Za-z0-9.-]', '_', host)}-{game.slug}.json"
        self.progress = Progress()
        self.report = report or (lambda progress: None)
        self.cancel = cancel or threading.Event()

    def say(self, message: str) -> None:
        self.progress.message = message
        self.progress.log.append(message)
        self.report(self.progress)

    # what goes where
    def items(self) -> tuple[list[Item], set[str]]:
        items: dict[str, Item] = {}
        dirs = set(self.base.dirs)
        for key, size in self.base.files.items():
            if key in REGISTRY:
                data = self.base.read(key)
                if key == "user.reg":
                    data = set_environment(data, self.game.environment)
                    data = set_values(data, FONT_KEY, font_replacements(self.base))
                data = to_console(key, data)
                items[key] = Item(key, len(data), data=data)
            else:
                items[key] = Item(key, size, open=lambda key=key: self.base.open(key))
        folder = self.game.folder
        dirs.update(posixpath.join(folder, d) for d in self.source.dirs)
        dirs.add(folder)
        for key, size in self.source.files.items():
            target = f"{folder}/{key}"
            items[target] = Item(target, size, open=lambda key=key: self.source.open(key))
        for key, data in self.dxvk.items():
            items[key] = Item(key, len(data), data=data)
        if self.cpu_dll is not None:
            items[CPU_DLL] = Item(CPU_DLL, len(self.cpu_dll), data=self.cpu_dll)
        for item in items.values():
            parent = posixpath.dirname(item.key)
            while parent:
                dirs.add(parent)
                parent = posixpath.dirname(parent)
        return sorted(items.values(), key=lambda item: item.key), dirs

    def load_state(self) -> tuple[dict[str, int] | None, bool]:
        """The files this PC sent the console, and whether that send finished."""
        try:
            state = json.loads(self.state_path.read_text())
            return {key: int(size) for key, size in state["files"].items()}, bool(state.get("complete"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None, False

    def save_state(self, files: dict[str, int], complete: bool = False) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.state_path.with_name(self.state_path.name + ".tmp")
        partial.write_text(json.dumps({"slug": self.game.slug, "complete": complete, "files": files},
                                      indent=1, sort_keys=True))
        os.replace(partial, self.state_path)

    def send(self, overwrite: bool = False) -> None:
        """Sends what the console does not have yet. A send that stopped part
        way carries on; a finished one, or a copy from elsewhere, is only
        replaced with overwrite, since the console's copy holds the saves."""
        state, complete = self.load_state()
        if not overwrite and self.remote.exists(self.remote_prefix):
            if state is None:
                raise NeedsOverwrite(f"the console already has {self.game.slug}, sent from somewhere else. "
                                     "Replacing it loses its saves and settings there.")
            if complete:
                raise NeedsOverwrite(f"{self.game.slug} is already on the console. Sending it again "
                                     "replaces its saves and settings there.")
        if overwrite and state is not None:
            # The game's files this PC sent at the same size stay; the
            # registry, which holds the settings, goes again.
            state = {key: size for key, size in state.items() if key not in REGISTRY}
        if self.cpu_dll is None and self.remote.size(f"{self.remote_prefix}/{CPU_DLL}") is None:
            self.cpu_dll = fetch_app_cpu_dll(self.remote)
        items, dirs = self.items()
        state = dict(state or {})
        self.progress.total_files = len(items)
        self.progress.total_bytes = sum(item.size for item in items)
        self.say(f"making {len(dirs)} folders")
        for directory in sorted(dirs):
            self.check_cancel()
            self.remote.makedirs(f"{self.remote_prefix}/{directory}")
        self.say(f"sending {len(items)} files ({pw_prefix.gib(self.progress.total_bytes)})")
        unsaved = 0
        try:
            for item in items:
                self.check_cancel()
                self.progress.current = item.key
                # What this PC already sent at this size is there.
                if state.get(item.key) == item.size:
                    self.progress.skipped += 1
                    self.advance(item.size)
                    continue
                self.put(item)
                state[item.key] = item.size
                unsaved += 1
                if unsaved >= pw_prefix.SAVE_EVERY_FILES:
                    self.save_state(state)
                    unsaved = 0
        except BaseException:
            self.save_state(state)
            raise
        self.put_profile()
        self.save_state(state, complete=True)
        self.progress.current = ""
        self.say(f"{self.game.name} is on the console: start prospero-win and pick it in the launcher")

    def update_settings(self) -> None:
        """Rewrites only the game's profile (program, graphics, arguments,
        resolution, preset), plus DXVK's DLLs if it now needs them and the
        console lacks them. The game's files, its registry and its saves
        stay as they are."""
        if not self.remote.exists(self.remote_prefix):
            raise QuickError(f"{self.game.slug} is not on the console yet: send it first")
        for item in (Item(key, len(data), data=data) for key, data in sorted(self.dxvk.items())):
            if self.remote.size(f"{self.remote_prefix}/{item.key}") != item.size:
                self.say(f"sending {item.key}")
                self.put(item)
        self.put_profile()
        self.say(f"{self.game.name}'s settings are updated on the console")

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def advance(self, size: int, files: int = 1) -> None:
        self.progress.done_bytes += size
        self.progress.done_files += files
        self.report(self.progress)

    def put(self, item: Item) -> None:
        target = f"{self.remote_prefix}/{item.key}"
        if item.data is not None:
            self.remote.write(target, item.data)
            self.advance(item.size)
        else:
            def tick(count: int) -> None:
                self.progress.done_bytes += count
                self.report(self.progress)
            with item.open() as stream:
                reader = CountingReader(stream, tick, self.cancel)
                try:
                    self.remote.write_stream(target, reader)
                except Cancelled:
                    self.progress.done_bytes -= reader.size
                    raise
            self.progress.done_files += 1
        stored = self.remote.size(target)
        if stored != item.size:
            raise QuickError(f"{target}: the console stored {stored} bytes, not {item.size}")

    def put_profile(self) -> None:
        profiles = f"{self.root}/profiles"
        name = f"{self.game.slug}.profile"
        self.remote.makedirs(profiles)
        self.remote.write(f"{profiles}/{name}", self.game.profile().encode("utf-8"))
        # The launcher lists profiles.lst's order when there is one
        # (native/pw_wine_library.c), so a game must be in it to show.
        listing = f"{profiles}/profiles.lst"
        if self.remote.size(listing) is not None:
            lines = self.remote.read(listing).decode("latin-1").splitlines()
            if name not in (line.strip() for line in lines):
                lines = [line for line in lines if line.strip()] + [name]
                self.remote.write(listing, ("\n".join(lines) + "\n").encode("latin-1"))


def fetch_app_cpu_dll(remote) -> bytes:
    """wowprospero.dll from the app on the console, which 32-bit games need in their prefix."""
    if remote.size(APP_CPU_DLL) is None:
        raise QuickError(f"the console has no {APP_CPU_DLL}: install the prospero-win app first")
    return remote.read(APP_CPU_DLL)


# --- the app itself --------------------------------------------------------------
def install_app(remote, release: Source, report: Callable[[Progress], None] | None = None,
                cancel: threading.Event | None = None) -> None:
    """Uploads the release's PPSA99995 folder to /data/homebrew and marks its
    eboot.bin and modules executable, which some FTP servers do not do."""
    report = report or (lambda progress: None)
    cancel = cancel or threading.Event()
    files = dict(release.files)
    if "PPSA99995/eboot.bin" in files:
        strip = "PPSA99995/"
    elif "eboot.bin" in files:
        strip = ""
    else:
        raise QuickError(f"{release.path} is not a prospero-win release: no PPSA99995/eboot.bin")
    progress = Progress(total_files=len(files), total_bytes=sum(files.values()))
    keys = sorted(key for key in files if key.startswith(strip))
    dirs = {posixpath.dirname(key[len(strip):]) for key in keys}
    for directory in sorted(dirs):
        remote.makedirs(f"{APP_ROOT}/{directory}" if directory else APP_ROOT)
    executable = []
    for key in keys:
        if cancel.is_set():
            raise Cancelled()
        relative = key[len(strip):]
        target = f"{APP_ROOT}/{relative}"
        progress.current = relative
        report(progress)

        def tick(count: int) -> None:
            progress.done_bytes += count
            report(progress)
        with release.open(key) as stream:
            remote.write_stream(target, CountingReader(stream, tick, cancel))
        if remote.size(target) != files[key]:
            raise QuickError(f"{target}: the console stored a different size")
        progress.done_files += 1
        if relative == "eboot.bin" or relative.endswith(".prx"):
            executable.append(target)
    chmod = getattr(remote, "chmod", None)
    refused = 0
    for target in executable:
        if chmod and not chmod(target, "755"):
            refused += 1
    progress.current = ""
    progress.message = ("the app is installed: let your loader register it" if not refused else
                        f"the app is installed, but the FTP server refused to mark {refused} files executable: "
                        "set 755 on eboot.bin and the .prx files from an FTP client")
    report(progress)


class Remote(FtpRemote):
    """pw_prefix's FTP remote, with SITE CHMOD for the app's executables."""

    def __init__(self, host: str, port: int):
        super().__init__(host, port)
        # Binary mode from the start: some servers answer SIZE only in it.
        try:
            self.ftp.voidcmd("TYPE I")
        except ftplib.all_errors:
            pass

    def chmod(self, path: str, mode: str) -> bool:
        try:
            self.ftp.sendcmd(f"SITE CHMOD {mode} {path}")
            return True
        except ftplib.all_errors:
            return False


def connect(host: str, port: int) -> Remote:
    try:
        return Remote(host, port)
    except (OSError, EOFError, *ftplib.all_errors) as error:
        raise QuickError(f"no FTP server answers at {host}:{port} ({error}): "
                         "check the address and that the PS5's FTP payload is running") from error


# --- command line ----------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("game", nargs="?", help="the game's zip or folder")
    parser.add_argument("--base", help="the base prefix's zip or folder (tools/pw_base_prefix.py)")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=2121)
    parser.add_argument("--name")
    parser.add_argument("--slug")
    parser.add_argument("--exe", help="the game's executable, inside its zip or folder")
    parser.add_argument("--graphics", choices=GRAPHICS)
    parser.add_argument("--arguments", default="")
    parser.add_argument("--desktop", default="1920x1080")
    parser.add_argument("--preset", default="")
    parser.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                        help="an environment variable for the game (added to the ones detected)")
    parser.add_argument("--winedebug", default="", help="Wine log channels for the game, e.g. err+all,+seh")
    parser.add_argument("--dxvk", help="a DXVK release tarball, instead of downloading the pinned one")
    parser.add_argument("--overwrite", action="store_true", help="replace the console's copy of the game")
    parser.add_argument("--settings-only", action="store_true",
                        help="only rewrite the profile of a game already on the console")
    parser.add_argument("--install-app", metavar="RELEASE_ZIP", help="install the prospero-win app instead")
    args = parser.parse_args(argv)

    last = [0]

    def report(progress: Progress) -> None:
        percent = int(100 * progress.done_bytes / max(progress.total_bytes, 1))
        if percent != last[0] or progress.message:
            print(f"pw_quick: {percent}% {progress.message or progress.current}", flush=True)
            progress.message, last[0] = "", percent
    try:
        if args.install_app:
            remote = connect(args.host, args.port)
            install_app(remote, Source(args.install_app), report)
            return 0
        if not args.game or not args.base:
            parser.error("give the game's zip or folder and --base")
        source, base = Source(args.game), Source(args.base)
        game, exes = suggest(source)
        if args.exe:
            match = next((exe for exe in exes if exe.key.lower() == args.exe.replace("\\", "/").lower()), None)
            if match is None:
                raise QuickError(f"{args.exe} is not one of the game's executables: "
                                 + ", ".join(exe.key for exe in exes))
            game.exe, game.bits = match.key, match.info.bits
            game.graphics = guess_graphics(source, match)
        if args.name:
            game.name = args.name
            game.slug = slugify(args.name)
        game.slug = args.slug or game.slug
        game.graphics = args.graphics or game.graphics
        game.arguments, game.desktop, game.preset = args.arguments, args.desktop, args.preset
        game.environment.update(parse_environment("\n".join(args.env)))
        game.winedebug = args.winedebug
        print(f"pw_quick: {game.name} ({game.slug}): {game.exe}, {game.bits}-bit, graphics {game.graphics}")
        dxvk = None
        if game.graphics == "dxvk":
            dxvk = dxvk_files(Path(args.dxvk) if args.dxvk else fetch_dxvk())
        remote = connect(args.host, args.port)
        sender = Sender(remote, game, source, base, dxvk=dxvk, host=args.host, report=report)
        if args.settings_only:
            sender.update_settings()
        else:
            sender.send(args.overwrite)
    except (QuickError, OSError, *ftplib.all_errors) as error:
        print(f"pw_quick: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, Cancelled):
        print("pw_quick: stopped; send again to carry on where it stopped", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
