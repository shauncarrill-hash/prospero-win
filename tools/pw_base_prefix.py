#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Make the base prefix tools/pw_quick.py and tools/pw_gui.py build games on.

The console cannot make a Wine prefix (wineboot is off there), so a game
that needs no installer still needs one made on a PC. This makes it once,
with the pinned host Wine (tools/build_host_wine.sh), as tools/pw_install.py's
create_prefix does: as the user prospero, without Gecko and Mono, and with
the user folders that Wine links into the PC's home made real folders.

The result is a zip anyone can use without Wine. It holds only what
wineboot wrote, Wine's own files (LGPL-2.1-or-later, as Wine). Symbolic
links become .pw-symlinks tables, the way tools/pw_prefix.py sends them,
so the zip unpacks and sends the same from any PC.

Two optional extras fill gaps ProbeTris found on the console:

- --fonts DIR copies TrueType fonts into C:\\windows\\Fonts. The console's
  Wine has only Tahoma and Wingdings; Liberation Sans, Serif and Mono (SIL
  OFL) and IPAGothic (IPA Font License) stand in for Arial, Times New Roman,
  Courier New and the Japanese system fonts, through the replacements
  tools/pw_quick.py writes (FONT_REPLACEMENTS).
- --lav DIR installs LAV Filters (GPL-2.0-or-later) from the release's
  LAVFilters-*-x64.zip and -x86.zip in DIR, registered for 64- and 32-bit
  programs. The console's Wine has no GStreamer, so DirectShow plays video
  only through them.

Usage:
    pw_base_prefix.py --wine PATH --out prospero-base-prefix.zip [--fonts DIR] [--lav DIR]
    pw_base_prefix.py --prefix DIR --out prospero-base-prefix.zip   # an existing clean prefix
"""
from __future__ import annotations

import argparse
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pw_prefix  # noqa: E402
from pw_install import Installer  # noqa: E402


def make_prefix(wine: Path, prefix: Path) -> None:
    wine = Path(os.path.abspath(wine))
    server = next((p for p in (wine.with_name("wineserver"), wine.parent / "server" / "wineserver")
                   if p.is_file()), None)
    if not wine.is_file() or server is None:
        raise SystemExit(f"pw_base_prefix: {wine} is not a Wine with a wineserver beside it")
    env = dict(os.environ, USER="prospero", LOGNAME="prospero", WINEPREFIX=str(prefix), WINEARCH="win64",
               WINEDEBUG="-all", WINEDLLOVERRIDES="winemenubuilder.exe=d;mshtml=;mscoree=")
    subprocess.run([str(wine), "wineboot", "--init"], env=env, check=True)
    subprocess.run([str(server), "-w"], env=env, check=True)
    Installer.unlink_home_folders(prefix)


def add_fonts(prefix: Path, fonts: Path) -> int:
    target = prefix / "drive_c" / "windows" / "Fonts"
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for font in sorted(fonts.iterdir()):
        if font.suffix.lower() in (".ttf", ".ttc", ".otf"):
            shutil.copyfile(font, target / font.name)
            count += 1
    return count


LAV_FILTERS = ("LAVSplitter.ax", "LAVVideo.ax", "LAVAudio.ax")


def add_lav(wine: Path, prefix: Path, releases: Path) -> None:
    env = dict(os.environ, USER="prospero", LOGNAME="prospero", WINEPREFIX=str(prefix), WINEDEBUG="-all")
    for bits, system in (("x64", "system32"), ("x86", "syswow64")):
        archive = next(iter(sorted(releases.glob(f"LAVFilters-*-{bits}.zip"))), None)
        if archive is None:
            raise SystemExit(f"pw_base_prefix: {releases} has no LAVFilters-*-{bits}.zip")
        folder = prefix / "drive_c" / "Program Files" / "LAV Filters" / bits
        folder.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as release:
            for info in release.infolist():
                name = posixpath.basename(info.filename)
                if name.lower().endswith((".ax", ".dll", ".manifest")) and "/" not in info.filename:
                    (folder / name).write_bytes(release.read(info))
        regsvr32 = f"C:\\windows\\{system}\\regsvr32.exe"
        for name in LAV_FILTERS:
            path = f"C:\\Program Files\\LAV Filters\\{bits}\\{name}"
            subprocess.run([str(wine), regsvr32, "/s", path], env=env, check=True)
    server = wine.with_name("wineserver")
    subprocess.run([str(server if server.is_file() else "wineserver"), "-w"], env=env, check=True)


def write_zip(prefix: Path, out: Path) -> int:
    files, dirs, links = pw_prefix.local_tree(prefix)
    if "system.reg" not in files or "user.reg" not in files:
        raise SystemExit(f"pw_base_prefix: {prefix} is not a Wine prefix (no system.reg and user.reg)")
    if not (prefix / "drive_c" / "users" / pw_prefix.CONSOLE_USER).is_dir():
        print(f"pw_base_prefix: warning: {prefix} has no drive_c/users/{pw_prefix.CONSOLE_USER}, "
              "the user the console runs Wine as", file=sys.stderr)
    partial = out.with_name(out.name + ".part")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for directory in sorted(dirs - {""}):
            archive.writestr(zipfile.ZipInfo(directory + "/"), b"")
        for key, path in sorted(files.items()):
            archive.write(path, key)
        for directory, table in sorted(links.items()):
            text = "".join(f"{name}\t{target}\n" for name, target in sorted(table.items()))
            archive.writestr(posixpath.join(directory, pw_prefix.LINK_TABLE) if directory
                             else pw_prefix.LINK_TABLE, text)
    os.replace(partial, out)
    return len(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--wine", type=Path, help="the pinned host Wine (tools/build_host_wine.sh)")
    source.add_argument("--prefix", type=Path, help="zip this clean prefix instead of making one")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fonts", type=Path, help="a folder of TrueType fonts to add")
    parser.add_argument("--lav", type=Path, help="a folder with LAV Filters' x64 and x86 release zips")
    args = parser.parse_args(argv)
    if args.lav and not args.wine:
        parser.error("--lav needs --wine, to register the filters")
    if args.prefix:
        if args.fonts:
            add_fonts(args.prefix, args.fonts)
        count = write_zip(args.prefix, args.out)
    else:
        work = Path(tempfile.mkdtemp(prefix="pw-base-"))
        try:
            make_prefix(args.wine, work / "prefix")
            if args.fonts:
                print(f"pw_base_prefix: {add_fonts(work / 'prefix', args.fonts)} fonts added")
            if args.lav:
                add_lav(Path(os.path.abspath(args.wine)), work / "prefix", args.lav)
                print("pw_base_prefix: LAV Filters registered")
            count = write_zip(work / "prefix", args.out)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print(f"pw_base_prefix: {args.out}: {count} files, {args.out.stat().st_size / (1 << 20):.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
