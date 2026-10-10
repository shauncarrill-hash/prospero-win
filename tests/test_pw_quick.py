#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""tools/pw_quick.py sends an installed game to the console without Wine,
and tools/pw_base_prefix.py makes the prefix it starts from.

The console is a directory, behind tests/test_pw_prefix.py's DirRemote. The
games are zips and folders of tiny PE files whose import tables name the
DLLs a real game would use."""
from __future__ import annotations

import os
import shutil
import struct
import sys
import tarfile
import tempfile
import threading
import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import pw_base_prefix  # noqa: E402
import pw_prefix  # noqa: E402
import pw_quick  # noqa: E402
from test_pw_prefix import DirRemote  # noqa: E402

SYSTEM_REG = (b"WINE REGISTRY Version 2\n\n[Software\\\\Microsoft\\\\Wow64\\\\x86] 1700000000\n"
              b'@="wow64cpu.dll"\n\n[Software\\\\Wine] 1700000000\n"Version"="wine-11.17"\n')


def pe(bits: int = 32, imports: tuple[str, ...] = (), delay: tuple[str, ...] = (), gui: bool = True,
       padding: int = 4096) -> bytes:
    """A PE image with one section holding its import and delay-import tables."""
    data = bytearray(0x400)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    optional_size = 0xE0 if bits == 32 else 0xF0
    struct.pack_into("<HH12xHH", data, 0x84, 0x14C if bits == 32 else 0x8664, 1, optional_size, 0x102)
    optional = 0x98
    struct.pack_into("<H", data, optional, 0x10B if bits == 32 else 0x20B)
    struct.pack_into("<H", data, optional + 68, 2 if gui else 3)
    directories = optional + (96 if bits == 32 else 112)
    struct.pack_into("<I", data, directories - 4, 16)
    table = optional + optional_size
    section_rva, section_raw = 0x1000, 0x200
    data[table:table + 8] = b".idata\0\0"
    struct.pack_into("<IIII", data, table + 8, 0x200, section_rva, 0x200, section_raw)
    names_at = section_raw + 0x100
    cursor = section_raw

    def name(text: str) -> int:
        nonlocal names_at
        rva = section_rva + names_at - section_raw
        data[names_at:names_at + len(text)] = text.encode()
        names_at += len(text) + 1
        return rva
    if imports:
        struct.pack_into("<II", data, directories + 8, section_rva + cursor - section_raw, 20 * (len(imports) + 1))
        for dll in imports:
            struct.pack_into("<12xI4x", data, cursor, name(dll))
            cursor += 20
        cursor += 20
    if delay:
        struct.pack_into("<II", data, directories + 8 * 13, section_rva + cursor - section_raw, 32 * (len(delay) + 1))
        for dll in delay:
            struct.pack_into("<4xI24x", data, cursor, name(dll))
            cursor += 32
    return bytes(data) + bytes(padding)


def make_zip(path: Path, files: dict[str, bytes], dirs: tuple[str, ...] = ()) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for directory in dirs:
            archive.writestr(zipfile.ZipInfo(directory.rstrip("/") + "/"), b"")
        for key, data in files.items():
            archive.writestr(key, data)
    return path


def make_base(root: Path) -> Path:
    return make_zip(root / "base.zip", {
        "system.reg": SYSTEM_REG, "user.reg": b"WINE REGISTRY Version 2\n", "userdef.reg": b"WINE\n",
        "dosdevices/.pw-symlinks": b"c:\t../drive_c\nz:\t/\n",
        "drive_c/windows/system32/kernel32.dll": b"k" * 3000,
        "drive_c/windows/syswow64/kernel32.dll": b"K" * 2000,
    }, dirs=("drive_c/users/prospero/Documents", "drive_c/windows/temp"))


def make_game(root: Path) -> Path:
    return make_zip(root / "Space Cadet.zip", {
        "Space Cadet/PINBALL.EXE": pe(32, ("kernel32.dll", "user32.dll", "d3d9.dll"), padding=300_000),
        "Space Cadet/unins000.exe": pe(32, ("kernel32.dll",), padding=900_000),
        "Space Cadet/tools/console.exe": pe(64, ("kernel32.dll",), gui=False),
        "Space Cadet/data/table.dat": os.urandom(5_000_000),
        "Space Cadet/data/sound/hit.wav": b"RIFF" + bytes(1000),
        "__MACOSX/Space Cadet/._PINBALL.EXE": b"junk",
        "Space Cadet/Thumbs.db": b"junk",
    })


def fake_dxvk(root: Path) -> dict[str, bytes]:
    archive = root / "dxvk.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for bits in ("x32", "x64"):
            for dll in pw_quick.DXVK_DLLS:
                payload = f"{bits}/{dll}".encode()
                info = tarfile.TarInfo(f"dxvk-{pw_quick.DXVK_VERSION}/{bits}/{dll}.dll")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
    return pw_quick.dxvk_files(archive)


def make_console(root: Path) -> tuple[Path, DirRemote]:
    console = root / "console"
    cpu = console / pw_quick.APP_CPU_DLL.lstrip("/")
    cpu.parent.mkdir(parents=True)
    cpu.write_bytes(b"wowprospero" * 100)
    profiles = console / "data/prospero-win/profiles"
    profiles.mkdir(parents=True)
    (profiles / "profiles.lst").write_text("warcraft-iii.profile\n")
    return console, DirRemote(console)


def test_pe_info() -> None:
    info = pw_quick.pe_info(pe(64, ("KERNEL32.dll", "opengl32.dll"), delay=("D3D11.dll",)))
    assert info.bits == 64 and info.gui
    assert info.imports == {"kernel32.dll", "opengl32.dll", "d3d11.dll"}, info.imports
    assert pw_quick.pe_info(pe(32, gui=False)).bits == 32
    assert pw_quick.pe_info(b"not a program") is None
    assert pw_quick.pe_info(b"MZ" + bytes(100)) is None


def test_suggest_from_zip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = pw_quick.Source(make_game(Path(tmp)))
        assert source.name == "Space Cadet"
        assert "PINBALL.EXE" in source.files and "Thumbs.db" not in source.files
        assert not any(key.startswith("__MACOSX") for key in source.files)
        assert {"data", "data/sound", "tools"} <= source.dirs
        game, exes = pw_quick.suggest(source)
        assert [exe.key for exe in exes][0] == "PINBALL.EXE", [exe.key for exe in exes]
        assert game.slug == "space-cadet" and game.bits == 32 and game.graphics == "dxvk"
        source.close()


def test_graphics_from_the_game_dlls() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Half-Life"
        root.mkdir()
        (root / "hl.exe").write_bytes(pe(32, ("kernel32.dll",)))
        (root / "hw.dll").write_bytes(pe(32, ("opengl32.dll",)))
        source = pw_quick.Source(root)
        game, _ = pw_quick.suggest(source)
        assert game.name == "Half-Life" and game.graphics == "opengl"
        (root / "shaderapidx9.dll").write_bytes(pe(32, ("d3d9.dll",)))
        assert pw_quick.suggest(pw_quick.Source(root))[0].graphics == "dxvk"
        (root / "hw.dll").unlink()
        (root / "shaderapidx9.dll").unlink()
        assert pw_quick.suggest(pw_quick.Source(root))[0].graphics == "gdi"
        (root / "ddraw.dll").write_bytes(pe(32, ("ddraw.dll",)))
        assert pw_quick.suggest(pw_quick.Source(root))[0].graphics == "auto"


def test_profile() -> None:
    game = pw_quick.Game(name="Space Cadet", slug="space-cadet", exe="bin/PINBALL.EXE", bits=32,
                         graphics="dxvk", preset="pinball", arguments="-fullscreen")
    text = game.profile()
    for line in ("id = space-cadet", "name = Space Cadet", r"executable = C:\Games\space-cadet\bin\PINBALL.EXE",
                 r"working_directory = C:\Games\space-cadet\bin", "arguments = -fullscreen",
                 "dll_overrides = d3d8,d3d9,d3d10core,d3d11,dxgi=n", "prefix = space-cadet",
                 "runtime = wine-wow64", "architecture = pe32", "graphics = dxvk",
                 "desktop = 1920x1080", "scaling = fit", "[input]", "preset = pinball"):
        assert line in text.splitlines(), (line, text)
    for bad in (dict(slug="Bad Slug"), dict(graphics="vulkan"), dict(desktop="big"), dict(name="a\nb"),
                dict(preset="../x")):
        try:
            pw_quick.Game(**{**dict(name="x", slug="x", exe="x.exe", bits=32), **bad}).check()
        except pw_quick.QuickError:
            continue
        raise AssertionError(f"{bad} was accepted")


def send(root: Path, remote, cancel=None, report=None, overwrite=False, state=None) -> pw_quick.Sender:
    source, base = pw_quick.Source(root / "Space Cadet.zip"), pw_quick.Source(root / "base.zip")
    game, _ = pw_quick.suggest(source)
    sender = pw_quick.Sender(remote, game, source, base, dxvk=fake_dxvk(root), state_dir=state or root / "state",
                             host="ps5", report=report, cancel=cancel)
    sender.send(overwrite)
    return sender


def test_send() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_game(root), make_base(root)
        console, remote = make_console(root)
        sender = send(root, remote)
        prefix = console / "data/prospero-win/prefixes/space-cadet"
        system = (prefix / "system.reg").read_bytes()
        assert b'@="wowprospero.dll"' in system and b"wow64cpu" not in system
        assert (prefix / "dosdevices/.pw-symlinks").read_text() == "c:\t../drive_c\nz:\t/\n"
        assert (prefix / "drive_c/users/prospero/Documents").is_dir()
        assert (prefix / "drive_c/Games/space-cadet/PINBALL.EXE").is_file()
        assert (prefix / "drive_c/Games/space-cadet/data/sound/hit.wav").stat().st_size == 1004
        game_zip = zipfile.ZipFile(root / "Space Cadet.zip")
        assert (prefix / "drive_c/Games/space-cadet/data/table.dat").read_bytes() == \
            game_zip.read("Space Cadet/data/table.dat")
        assert not (prefix / "drive_c/Games/space-cadet/Thumbs.db").exists()
        assert (prefix / "drive_c/windows/system32/d3d9.dll").read_bytes() == b"x64/d3d9"
        assert (prefix / "drive_c/windows/syswow64/d3d9.dll").read_bytes() == b"x32/d3d9"
        assert (prefix / pw_prefix.CPU_DLL).read_bytes() == b"wowprospero" * 100
        profiles = console / "data/prospero-win/profiles"
        assert "executable = C:\\Games\\space-cadet\\PINBALL.EXE" in (profiles / "space-cadet.profile").read_text()
        assert (profiles / "profiles.lst").read_text() == "warcraft-iii.profile\nspace-cadet.profile\n"
        assert sender.progress.done_bytes == sender.progress.total_bytes
        assert sender.progress.done_files == sender.progress.total_files

        # Sending a finished game again would replace its saves: it asks first.
        try:
            send(root, remote)
            raise AssertionError("a finished game was sent again without asking")
        except pw_quick.NeedsOverwrite:
            pass
        writes = len(remote.writes)
        send(root, remote, overwrite=True)
        assert len(remote.writes) > writes
        assert (profiles / "profiles.lst").read_text().count("space-cadet.profile") == 1

        # A copy this PC did not send is not replaced without asking either.
        try:
            send(root, remote, state=root / "elsewhere")
            raise AssertionError("someone else's copy was replaced without asking")
        except pw_quick.NeedsOverwrite:
            pass


def test_stop_and_carry_on() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_game(root), make_base(root)
        console, remote = make_console(root)
        cancel = threading.Event()

        def report(progress):
            if progress.done_files >= 5:
                cancel.set()
        try:
            send(root, remote, cancel=cancel, report=report)
            raise AssertionError("the send did not stop")
        except pw_quick.Cancelled:
            pass
        assert not (console / "data/prospero-win/profiles/space-cadet.profile").exists()
        sent = len(remote.writes)
        assert 0 < sent
        remote.writes.clear()
        sender = send(root, remote)
        assert sender.progress.skipped == sent, (sender.progress.skipped, sent)
        assert (console / "data/prospero-win/profiles/space-cadet.profile").exists()
        assert (console / "data/prospero-win/prefixes/space-cadet/drive_c/Games/space-cadet/data/table.dat"
                ).stat().st_size == 5_000_000


def test_no_app_on_the_console() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_game(root), make_base(root)
        (root / "console").mkdir()
        try:
            send(root, DirRemote(root / "console"))
            raise AssertionError("sent without wowprospero.dll")
        except pw_quick.QuickError as error:
            assert "install the prospero-win app" in str(error)


def test_install_app() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        release = make_zip(root / "prospero-win-0.1.zip", {
            "PPSA99995/eboot.bin": b"\x7fELF" + bytes(100),
            "PPSA99995/sce_module/libc.prx": b"prx",
            "PPSA99995/win/wine/lib/wine/x86_64-windows/wowprospero.dll": b"cpu",
            "PPSA99995/sce_sys/param.json": b"{}",
        })
        (root / "console").mkdir()
        remote = DirRemote(root / "console")
        modes = []
        remote.chmod = lambda path, mode: modes.append((path, mode)) or True
        pw_quick.install_app(remote, pw_quick.Source(release))
        app = root / "console" / pw_quick.APP_ROOT.lstrip("/")
        assert (app / "eboot.bin").read_bytes().startswith(b"\x7fELF")
        assert (app / "win/wine/lib/wine/x86_64-windows/wowprospero.dll").read_bytes() == b"cpu"
        assert sorted(modes) == [(f"{pw_quick.APP_ROOT}/eboot.bin", "755"),
                                 (f"{pw_quick.APP_ROOT}/sce_module/libc.prx", "755")]
        try:
            pw_quick.install_app(remote, pw_quick.Source(make_zip(root / "other.zip", {"a.txt": b"a"})))
            raise AssertionError("a zip without the app was installed")
        except pw_quick.QuickError:
            pass


def test_base_prefix_zip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        prefix = root / "prefix"
        (prefix / "drive_c/users/prospero/Documents").mkdir(parents=True)
        (prefix / "drive_c/windows/system32").mkdir(parents=True)
        (prefix / "drive_c/windows/system32/kernel32.dll").write_bytes(b"k")
        (prefix / "dosdevices").mkdir()
        os.symlink("../drive_c", prefix / "dosdevices/c:")
        os.symlink("/", prefix / "dosdevices/z:")
        os.symlink(str(root), prefix / "drive_c/users/prospero/Desktop")
        (prefix / "system.reg").write_bytes(SYSTEM_REG)
        (prefix / "user.reg").write_bytes(b"WINE REGISTRY Version 2\n")
        out = root / "base.zip"
        pw_base_prefix.write_zip(prefix, out)
        names = zipfile.ZipFile(out).namelist()
        assert "dosdevices/.pw-symlinks" in names and "drive_c/users/prospero/Desktop/" in names
        assert zipfile.ZipFile(out).read("dosdevices/.pw-symlinks") == b"c:\t../drive_c\nz:\t/\n"
        base = pw_quick.Source(out)
        assert base.prefix == "" and "system.reg" in base.files
        # And a game builds on it.
        make_game(root)
        console, remote = make_console(root)
        send(root, remote)
        assert (console / "data/prospero-win/prefixes/space-cadet/drive_c/windows/system32/kernel32.dll").is_file()


def test_folder_base_without_link_tables_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        base = root / "base"
        base.mkdir()
        (base / "system.reg").write_bytes(SYSTEM_REG)
        (base / "user.reg").write_bytes(b"x")
        game_dir = root / "game"
        game_dir.mkdir()
        (game_dir / "game.exe").write_bytes(pe())
        source = pw_quick.Source(game_dir)
        game, _ = pw_quick.suggest(source)
        game.graphics = "gdi"
        try:
            pw_quick.Sender(None, game, source, pw_quick.Source(base))
            raise AssertionError("a base prefix without link tables was accepted")
        except pw_quick.QuickError as error:
            assert "pw_base_prefix" in str(error)


def test_dotnet_game() -> None:
    """A .NET game gets W^X off through the prefix's HKCU\\Environment, and a
    second send with overwrite sends the registry again, not the game."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_zip(root / "Space Cadet.zip", {
            "Mecha/mecha.exe": pe(64, ("kernel32.dll", "vulkan-1.dll"), padding=200_000),
            "Mecha/data_Mecha_windows_x86_64/coreclr.dll": pe(64, ("kernel32.dll",)),
            "Mecha/data_Mecha_windows_x86_64/hostfxr.dll": pe(64, ("kernel32.dll",)),
        })
        make_base(root)
        console, remote = make_console(root)
        source = pw_quick.Source(root / "Space Cadet.zip")
        game, _ = pw_quick.suggest(source)
        assert game.environment == pw_quick.DOTNET and "DOTNET_GCRegionRange" in game.environment, game.environment
        game.winedebug = "err+all,+seh"
        base = pw_quick.Source(root / "base.zip")
        pw_quick.Sender(remote, game, source, base, state_dir=root / "state").send()
        prefix = console / "data/prospero-win/prefixes/mecha"
        user = (prefix / "user.reg").read_text()
        assert '[Environment] 0\n"DOTNET_GCRegionRange"="0xC0000000"\n' in user, user
        assert '"DOTNET_EnableWriteXorExecute"="0"\n' in user, user
        assert "[debug]\nwinedebug = err+all,+seh\n" in (console / "data/prospero-win/profiles/mecha.profile").read_text()
        remote.writes.clear()
        game.environment["DOTNET_gcServer"] = "0"
        pw_quick.Sender(remote, game, source, base, state_dir=root / "state").send(overwrite=True)
        sent = {path.rsplit("/prefixes/mecha/", 1)[-1] for path in remote.writes}
        assert not any(key.startswith("drive_c/") for key in sent), sent
        assert {"system.reg", "user.reg", "userdef.reg"} <= sent, sent
        assert '"DOTNET_gcServer"="0"' in (prefix / "user.reg").read_text()


def test_environment_text() -> None:
    assert pw_quick.parse_environment("A=1; B = two\nC=") == {"A": "1", "B": "two", "C": ""}
    assert pw_quick.format_environment({"A": "1", "B": "2"}) == "A=1; B=2"
    for bad in ("A", ):
        try:
            pw_quick.parse_environment(bad)
            raise AssertionError(bad)
        except pw_quick.QuickError:
            pass
    try:
        pw_quick.Game(name="x", slug="x", exe="x.exe", bits=64, environment={"BAD NAME": "1"}).check()
        raise AssertionError("a bad name was accepted")
    except pw_quick.QuickError:
        pass


def test_slow_console_reconnects() -> None:
    class Flaky(DirRemote):
        failures = 0
        reconnects = 0

        def write_stream(self, path, stream):
            if path.endswith("table.dat") and self.failures < 2:
                self.failures += 1
                stream.read(1000)
                raise TimeoutError("timed out")
            super().write_stream(path, stream)

        def reconnect(self):
            self.reconnects += 1

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_game(root), make_base(root)
        console = make_console(root)[0]
        remote = Flaky(console)
        waits = pw_quick.threading.Event.wait
        pw_quick.threading.Event.wait = lambda self, timeout=None: False
        try:
            sender = send(root, remote)
        finally:
            pw_quick.threading.Event.wait = waits
        assert remote.failures == 2 and remote.reconnects == 2
        assert (console / "data/prospero-win/prefixes/space-cadet/drive_c/Games/space-cadet/data/table.dat"
                ).stat().st_size == 5_000_000
        assert sender.progress.done_bytes == sender.progress.total_bytes


def test_deleted_game_is_sent_again_in_full() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_game(root), make_base(root)
        console, remote = make_console(root)
        send(root, remote)
        shutil.rmtree(console / "data/prospero-win/prefixes/space-cadet")
        sender = send(root, remote)
        assert sender.progress.skipped == 0
        assert (console / "data/prospero-win/prefixes/space-cadet/drive_c/Games/space-cadet/PINBALL.EXE").is_file()


def test_payloads_go_to_the_elf_loader_in_order() -> None:
    import socket
    received = []
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def serve():
        for _ in range(2):
            connection, _ = server.accept()
            with connection:
                data = b""
                while chunk := connection.recv(65536):
                    data += chunk
                received.append(data)
    thread = threading.Thread(target=serve)
    thread.start()
    with tempfile.TemporaryDirectory() as tmp:
        first, second = Path(tmp) / "etaHEN.bin", Path(tmp) / "ftpsrv.elf"
        first.write_bytes(b"\x7fELF" + os.urandom(300_000))
        second.write_bytes(b"\x7fELF" + b"x" * 100)
        assert pw_quick.send_payloads("127.0.0.1", port, [first, second], say=lambda text: None, gap=0.01) == 2
        thread.join(5)
        assert received == [first.read_bytes(), second.read_bytes()]
    server.close()
    try:
        pw_quick.send_payload("127.0.0.1", port, __file__)
        raise AssertionError("a closed port took a payload")
    except pw_quick.QuickError:
        pass


def test_logs_come_back_in_one_zip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        console, remote = make_console(root)
        logs = console / "data/prospero-win/logs"
        logs.mkdir(parents=True)
        for index in range(3):
            (logs / f"session-{index}.log").write_text(f"run {index}\n")
        (logs / "next.txt").write_text("2\n")
        out, newest = pw_quick.fetch_logs(remote, root / "out", "now")
        assert newest == "session-1.log"
        names = zipfile.ZipFile(out).namelist()
        assert "logs/session-2.log" in names and "profiles/profiles.lst" in names, names


def test_shared_sections_are_made_private() -> None:
    exe = bytearray(pe(32, ("kernel32.dll",), padding=200_000))
    at = struct.unpack_from("<I", exe, 0x3C)[0]
    sections, optional_size = struct.unpack_from("<H12xH", exe, at + 6)
    assert sections >= 1
    flags_at = at + 24 + optional_size + 36
    struct.pack_into("<I", exe, flags_at, struct.unpack_from("<I", exe, flags_at)[0] | pw_quick.SCN_MEM_SHARED)
    assert pw_quick.unshared(bytes(pe(32, ("kernel32.dll",)))) is None
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_zip(root / "Space Cadet.zip", {"Space Cadet/PINBALL.EXE": bytes(exe)})
        make_base(root)
        console, remote = make_console(root)
        send(root, remote)
        sent = (console / "data/prospero-win/prefixes/space-cadet/drive_c/Games/space-cadet/PINBALL.EXE").read_bytes()
        assert len(sent) == len(exe)
        assert not struct.unpack_from("<I", sent, flags_at)[0] & pw_quick.SCN_MEM_SHARED
        remote.writes.clear()
        send(root, remote, overwrite=True)
        assert any(path.endswith("PINBALL.EXE") for path in remote.writes), remote.writes


def test_net_framework_game_gets_wine_mono_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_zip(root / "Farm.zip", {"Farm/Farm.exe": pe(32, ("mscoree.dll",), padding=100_000),
                                     "Farm/Content/a.xnb": b"x" * 10})
        mono_zip = make_zip(root / f"{pw_quick.MONO_NAME}.zip", {
            f"{pw_quick.MONO_NAME}/bin/libmono-2.0-x86.dll": b"m" * 5000,
            f"{pw_quick.MONO_NAME}/lib/mono/4.5/mscorlib.dll": b"c" * 3000})
        make_base(root)
        console, remote = make_console(root)
        source, base = pw_quick.Source(root / "Farm.zip"), pw_quick.Source(root / "base.zip")
        game, _ = pw_quick.suggest(source)
        assert game.mono, game.engine
        try:
            pw_quick.Sender(remote, game, source, base, state_dir=root / "state")
            raise AssertionError("a .NET Framework game was accepted without Wine Mono")
        except pw_quick.QuickError:
            pass
        pw_quick.Sender(remote, game, source, base, state_dir=root / "state",
                        mono=pw_quick.Source(mono_zip)).send()
        shared = console / "data/prospero-win/shared" / pw_quick.MONO_NAME
        assert (shared / "bin/libmono-2.0-x86.dll").stat().st_size == 5000
        assert (shared / pw_quick.MONO_MARKER).is_file()
        user = (console / "data/prospero-win/prefixes/farm/user.reg").read_text()
        assert "[Software\\\\Wine\\\\Mono]" in user, user
        assert f'"RuntimePath"="Z:\\\\data\\\\prospero-win\\\\shared\\\\{pw_quick.MONO_NAME}"' in user, user
        remote.writes.clear()
        pw_quick.Sender(remote, game, source, base, state_dir=root / "state",
                        mono=pw_quick.Source(mono_zip)).send(overwrite=True)
        assert not any("/shared/" in path for path in remote.writes), remote.writes

        # A settings-only send of a game sent before Wine Mono existed adds it too.
        shutil.rmtree(shared)
        prefix_user = console / "data/prospero-win/prefixes/farm/user.reg"
        prefix_user.write_text(user.replace("RuntimePath", "Other"))
        pw_quick.Sender(remote, game, source, base, state_dir=root / "state",
                        mono=pw_quick.Source(mono_zip)).update_settings()
        assert (shared / pw_quick.MONO_MARKER).is_file()
        assert '"RuntimePath"=' in prefix_user.read_text()


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"test_pw_quick: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def test_update_settings_only() -> None:
    """A settings update rewrites the profile and nothing else."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_zip(root / "Mecha.zip", {"Mecha/mecha.exe": pe(64, ("kernel32.dll",), padding=200_000)})
        make_base(root)
        console, remote = make_console(root)
        source, base = pw_quick.Source(root / "Mecha.zip"), pw_quick.Source(root / "base.zip")
        game, _ = pw_quick.suggest(source)
        sender = pw_quick.Sender(remote, game, source, base, state_dir=root / "state")
        try:
            sender.update_settings()
            raise AssertionError("a game not on the console was updated")
        except pw_quick.QuickError:
            pass
        sender.send()
        remote.writes.clear()
        game.arguments = "--rendering-method mobile"
        pw_quick.Sender(remote, game, source, base, state_dir=root / "state").update_settings()
        assert all(path.endswith("mecha.profile") for path in remote.writes), remote.writes
        profile = (console / "data/prospero-win/profiles/mecha.profile").read_text()
        assert "arguments = --rendering-method mobile" in profile, profile


def test_preset_goes_to_the_console() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_zip(root / "Mecha.zip", {"Mecha/mecha.exe": pe(64, ("kernel32.dll",), padding=200_000)})
        make_base(root)
        console, remote = make_console(root)
        source, base = pw_quick.Source(root / "Mecha.zip"), pw_quick.Source(root / "base.zip")
        game, _ = pw_quick.suggest(source)
        game.preset = "mouse"
        pw_quick.Sender(remote, game, source, base, state_dir=root / "state").send()
        assert "mouse = left_stick" in (console / "data/prospero-win/input/mouse.input").read_text()
