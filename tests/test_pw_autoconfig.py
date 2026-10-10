#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""tools/pw_autoconfig.py: what pw_quick.suggest makes of games built with
common engines, from tiny stand-ins for their files."""
from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import pw_quick  # noqa: E402
from test_pw_quick import make_zip, pe  # noqa: E402


def suggest(files: dict[str, bytes]) -> pw_quick.Game:
    with tempfile.TemporaryDirectory() as tmp:
        source = pw_quick.Source(make_zip(Path(tmp) / "game.zip", files))
        game, _ = pw_quick.suggest(source)
        source.close()
        return game


def levels(game: pw_quick.Game) -> set[str]:
    return {check.level for check in game.checks}


def text(game: pw_quick.Game) -> str:
    return "\n".join(map(str, game.checks))


def test_unity() -> None:
    game = suggest({"Cube/Cube.exe": pe(64, ("kernel32.dll",), padding=100_000),
                    "Cube/UnityPlayer.dll": pe(64, ("d3d11.dll", "d3d12.dll")),
                    "Cube/GameAssembly.dll": pe(64, ("kernel32.dll",)),
                    "Cube/Cube_Data/StreamingAssets/intro.mp4": b"v" * 100})
    assert game.engine == "Unity (IL2CPP)" and game.graphics == "dxvk", game
    assert game.arguments == "-force-d3d11"
    assert "Media Foundation" in text(game) and "stop" not in levels(game), text(game)
    assert "; engine: Unity (IL2CPP)" in game.profile()


def test_unreal_starts_the_shipping_exe() -> None:
    game = suggest({"Hall/Hall.exe": pe(64, ("kernel32.dll",), padding=300_000),
                    "Hall/Engine/Binaries/ThirdParty/x.dll": pe(64, ("kernel32.dll",)),
                    "Hall/Hall/Binaries/Win64/Hall-Win64-Shipping.exe": pe(64, ("d3d12.dll", "d3d11.dll"))})
    assert game.exe == "Hall/Binaries/Win64/Hall-Win64-Shipping.exe", game.exe
    assert game.engine == "Unreal Engine" and game.arguments == "-dx11" and game.graphics == "dxvk"
    assert "launcher" in text(game) and "stop" not in levels(game), text(game)


def pck(project: bytes, version: int = 2) -> bytes:
    return b"GDPC" + struct.pack("<IIII", version, 4, 3, 0) + b"\0" * 64 + project


def test_godot_compatibility_renderer_moves_to_vulkan() -> None:
    game = suggest({"Horror/Horror.exe": pe(64, ("kernel32.dll", "dxgi.dll"), padding=100_000),
                    "Horror/Horror.pck": pck(b"rendering/renderer/rendering_method\0\0\x04\0\0\0"
                                             b"\x10\0\0\0gl_compatibility")})
    assert game.engine == "Godot 2" or game.engine.startswith("Godot"), game.engine
    assert game.graphics == "auto" and "--rendering-method mobile" in game.arguments, game


def test_godot_d3d12_driver_goes_to_vulkan() -> None:
    game = suggest({"Mecha/Mecha.exe": pe(64, ("kernel32.dll", "vulkan-1.dll"), padding=100_000),
                    "Mecha/Mecha.pck": pck(b"rendering/rendering_device/driver.windows\0\x04d3d12"),
                    "Mecha/data_Mecha_windows_x86_64/coreclr.dll": pe(64, ("kernel32.dll",))})
    assert game.engine.endswith("C#") and "--rendering-driver vulkan" in game.arguments, game
    assert game.environment == pw_quick.DOTNET


def test_godot_embedded_pack() -> None:
    body = pe(64, ("kernel32.dll",), padding=100_000)
    pack = pck(b"")
    exe = body + pack + struct.pack("<Q", len(pack)) + b"GDPC"
    assert suggest({"G/G.exe": exe}).engine.startswith("Godot")


def test_xna_runs_on_wine_mono() -> None:
    game = suggest({"X/X.exe": pe(32, ("mscoree.dll",), padding=100_000),
                    "X/Microsoft.Xna.Framework.dll": pe(32, ("mscoree.dll",))})
    assert "stop" not in levels(game) and "Wine Mono" in text(game), text(game)
    assert game.mono and game.graphics == "dxvk"


def test_xna_named_by_the_exe() -> None:
    game = suggest({"S/S.exe": pe(32, ("mscoree.dll",), padding=100_000) + b"Microsoft.Xna.Framework.Game"})
    assert game.engine == "XNA" and game.graphics == "dxvk", (game.engine, game.graphics)


def test_direct3d12_only_is_stopped() -> None:
    game = suggest({"D/D.exe": pe(64, ("d3d12.dll", "kernel32.dll"), padding=100_000)})
    assert "stop" in levels(game) and "Direct3D 12" in text(game), text(game)
    game = suggest({"D/D.exe": pe(64, ("d3d12.dll", "dxgi.dll"), padding=100_000)})
    assert "stop" in levels(game), text(game)
    game = suggest({"D/D.exe": pe(64, ("d3d12.dll", "d3d11.dll"), padding=100_000)})
    assert "stop" not in levels(game) and game.graphics == "dxvk", text(game)


def test_renpy_starts_its_inner_exe() -> None:
    game = suggest({"Story/Story.exe": pe(64, ("kernel32.dll",), padding=100_000),
                    "Story/renpy/__init__.py": b"#",
                    "Story/lib/py3-windows-x86_64/Story.exe": pe(64, ("kernel32.dll",), padding=50_000)})
    assert game.exe == "lib/py3-windows-x86_64/Story.exe" and game.graphics == "opengl", game


def test_plain_game_notes() -> None:
    game = suggest({"Old/Old.exe": pe(32, ("dinput8.dll", "d3d9.dll", "openal32.dll"), padding=100_000),
                    "Old/music/title.mid": b"MThd",
                    "Old/steam_api.dll": pe(32, ("kernel32.dll",))})
    out = text(game)
    for words in ("DirectInput", "OpenAL", "MIDI", "Steam", "DXVK"):
        assert words in out, (words, out)


def test_anticheat_is_stopped() -> None:
    game = suggest({"A/A.exe": pe(64, ("d3d11.dll",), padding=100_000),
                    "A/EasyAntiCheat/EasyAntiCheat_Setup.exe": pe(64, ("kernel32.dll",))})
    assert "stop" in levels(game), text(game)


def pack_with_directory(files: dict[bytes, bytes], version: int = 3, filler: int = 300_000) -> bytes:
    """A Godot 4.4+ pack (format 3): header, file data, then the directory."""
    header_size = 4 + 16 + 12 + 8 + 64
    data = b"\0" * filler
    blobs, offsets = b"", {}
    for path, body in files.items():
        offsets[path] = len(data) + len(blobs)
        blobs += body
    file_base = header_size
    dir_offset = header_size + len(data) + len(blobs)
    directory = struct.pack("<I", len(files))
    for path, body in files.items():
        padded = path + b"\0" * (-len(path) % 4)
        directory += struct.pack("<I", len(padded)) + padded + struct.pack("<QQ", offsets[path], len(body))
        directory += b"\0" * 16 + struct.pack("<I", 0)
    header = b"GDPC" + struct.pack("<IIII", version, 4, 7, 2) + struct.pack("<IQ", 0, file_base)
    header += struct.pack("<Q", dir_offset) + b"\0" * 64
    return header + data + blobs + directory


def test_godot_reads_project_settings_from_the_pack_directory() -> None:
    project = (b"ECFG" + b"\0" * 8 + b"rendering/renderer/rendering_method\0\0\x04\0\0\0"
               b"\x10\0\0\0gl_compatibility")
    big = pack_with_directory({b"res://icon.png": b"x" * 100, b"res://project.binary": project},
                              filler=150 << 20)
    game = suggest({"H/H.exe": pe(64, ("kernel32.dll",), padding=100_000), "H/H.pck": big})
    assert game.engine == "Godot 4" and "--rendering-method mobile" in game.arguments, (game.engine, game.arguments)


def test_godot_vulkan_game_is_automatic() -> None:
    game = suggest({"V/V.exe": pe(64, ("kernel32.dll",), padding=100_000),
                    "V/V.pck": pack_with_directory({b"res://project.binary": b"ECFG" + b"\0" * 8})})
    assert game.graphics == "auto" and game.arguments == "", game
