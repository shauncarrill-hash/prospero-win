# SPDX-License-Identifier: LGPL-2.1-or-later
"""What a game needs on the console, read from its own files.

tools/pw_quick.py asks plan() for each executable it could start. The plan
names the engine, any better executable to start, the graphics backend,
arguments and environment variables, and the checks shown to the player:
what will work, what may not, and what stops the game.

The rules come from ProbeTris v6.1 on a PS5 (2026-10-08). What it measured:

- Vulkan 1.4 works; DXVK's Direct3D 9 and 11 work (61 fps). OpenGL 4.6
  works with graphics = opengl, and not under DXVK's overrides.
- vkd3d-proton's D3D12CreateDevice froze the console: no Direct3D 12.
- No program can start another (CreateProcess error 50): launchers, and
  multi-process engines (NW.js, Electron, CEF), cannot start their game.
- Wine has no GStreamer: Media Foundation plays video only through the
  base prefix's FFmpeg decoders (tools/build_media.sh), and DirectShow
  only through LAV Filters (also in the base prefix).
- No MIDI device; DirectInput sees no game controller (XInput does).
- .NET Framework 4 is absent; Wine Mono stands in for it (the sender puts it
  on the console once, pw_quick.MONO_*); .NET 5+ games bring their own
  runtime and need GC limits (pw_quick.DOTNET).
- Fonts: only Tahoma and Wingdings; the base prefix adds Liberation (for
  Arial, Times New Roman, Courier New) and a Japanese Gothic.
- Single reservations past about 16 GB fail; 8 GB commits work.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

OK, WARN, STOP = "ok", "warn", "stop"

# DLLs a game may import that the console's Wine lacks (ProbeTris
# runtime.dlls-missing), and what that means.
MISSING_DLLS = {
    "mfc42.dll": "Visual C++ MFC", "mfc100.dll": "Visual C++ MFC", "mfc110.dll": "Visual C++ MFC",
    "mfc120.dll": "Visual C++ MFC", "mfc140.dll": "Visual C++ MFC",
    "openal32.dll": "OpenAL", "wrap_oal.dll": "OpenAL",
    "physxloader.dll": "PhysX", "physxcore.dll": "PhysX", "nvcuda.dll": "CUDA", "opencl.dll": "OpenCL",
    "xgameruntime.dll": "the Microsoft Store game runtime", "d3d11on12.dll": "D3D11-on-12",
    "dxcompiler.dll": "the DirectX shader compiler", "dxil.dll": "the DirectX shader compiler",
}
VIDEO = re.compile(r"\.(mp4|m4v|mov|wmv|asf|avi|webm|mkv|mpg|mpeg)$")
OWN_VIDEO = re.compile(r"\.(bik|bk2|usm|ogv)$")     # Bink, CRI, Theora: the game decodes these
CHILD_PROCESS_ARGS = "--single-process --in-process-gpu --no-sandbox --disable-gpu-sandbox"
PCK_SCAN = 64 << 20


@dataclass
class Check:
    level: str          # OK, WARN or STOP
    text: str

    def __str__(self) -> str:
        return {OK: "✓", WARN: "!", STOP: "✗"}[self.level] + " " + self.text


@dataclass
class Plan:
    engine: str = ""
    exe: str = ""                        # a better executable to start, if any
    graphics: str = ""                   # "" keeps pw_quick's guess
    arguments: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)

    def ok(self, text: str) -> None:
        self.checks.append(Check(OK, text))

    def warn(self, text: str) -> None:
        self.checks.append(Check(WARN, text))

    def stop(self, text: str) -> None:
        self.checks.append(Check(STOP, text))

    @property
    def blocked(self) -> bool:
        return any(check.level == STOP for check in self.checks)


class Files:
    """The game's file names, lower-cased, for matching."""

    def __init__(self, source):
        self.source = source
        self.keys = {key.lower(): key for key in source.files}
        self.dirs = {d.lower() for d in source.dirs}
        self.names = {}
        for lower, key in self.keys.items():
            self.names.setdefault(posixpath.basename(lower), key)

    def has(self, name: str) -> bool:
        return name.lower() in self.names

    def find(self, pattern: str) -> list[str]:
        regex = re.compile(pattern)
        return [key for lower, key in self.keys.items() if regex.search(lower)]

    def dir(self, pattern: str) -> bool:
        regex = re.compile(pattern)
        return any(regex.search(d) for d in self.dirs)


# --- Godot -------------------------------------------------------------------------
def godot_pack(source, files: Files, exe: str) -> tuple[str, int]:
    """The game's Godot pack, and where it starts: a .pck beside the exe, or
    one embedded in the exe (whose last 12 bytes are its size and GDPC)."""
    stem = posixpath.splitext(exe)[0]
    for key in (stem + ".pck", *files.find(r"\.pck$")):
        if key in source.files:
            return key, 0
    size = source.files.get(exe, 0)
    if size > 32:
        with source.lock, source.open(exe) as stream:
            stream.seek(size - 12)
            tail = stream.read(12)
        if tail[8:] == b"GDPC":
            return exe, size - 12 - int.from_bytes(tail[:8], "little")
    return "", 0


def scan(source, key: str, start: int, needles: tuple[bytes, ...]) -> dict[bytes, bytes]:
    """For each needle, the 96 bytes after its first appearance, looking at
    the first and last PCK_SCAN bytes of the file from start on."""
    found: dict[bytes, bytes] = {}
    size = source.files[key]
    spans = [(start, min(size, start + PCK_SCAN))]
    if size - PCK_SCAN > spans[0][1]:
        spans.append((size - PCK_SCAN, size))
    with source.lock:
        for begin, end in spans:
            with source.open(key) as stream:
                stream.seek(begin)
                data = stream.read(end - begin)
            for needle in needles:
                if needle not in found:
                    at = data.find(needle)
                    if at >= 0:
                        found[needle] = data[at + len(needle):at + len(needle) + 96]
    return found


def project_settings(source, pack: str, start: int) -> bytes | None:
    """The project.binary inside a Godot pack, from the pack's directory
    (core/io/file_access_pack.cpp, formats 1-3), or None when it can't be
    read: an encrypted directory or file, or an unknown format."""
    import struct
    try:
        with source.lock, source.open(pack) as stream:
            stream.seek(start)
            magic, version, _major, _minor, _patch = struct.unpack("<4s4I", stream.read(20))
            if magic != b"GDPC" or version > 3:
                return None
            flags = file_base = dir_offset = 0
            if version >= 2:
                flags, file_base = struct.unpack("<IQ", stream.read(12))
            if version >= 3:
                (dir_offset,) = struct.unpack("<Q", stream.read(8))
            if flags & 1:                                  # PACK_DIR_ENCRYPTED
                return None
            relative = start if (flags & 2 or version < 2) else 0     # PACK_REL_FILEBASE
            stream.read(64)                                # reserved
            if version >= 3:
                stream.seek(relative + dir_offset)
            (count,) = struct.unpack("<I", stream.read(4))
            for _ in range(min(count, 1_000_000)):
                (length,) = struct.unpack("<I", stream.read(4))
                path = stream.read(length).rstrip(b"\0")
                offset, size = struct.unpack("<QQ", stream.read(16))
                stream.read(16)                            # md5
                file_flags = struct.unpack("<I", stream.read(4))[0] if version >= 2 else 0
                if path.endswith(b"project.binary") and b"/" not in path.replace(b"res://", b""):
                    if file_flags & 1 or size > 16 << 20:
                        return None
                    stream.seek(relative + file_base + offset)
                    data = stream.read(size)
                    return data if data[:4] == b"ECFG" else None
    except (struct.error, OSError, EOFError, ValueError):
        return None
    return None


def godot(plan: Plan, source, files: Files, exe: str) -> None:
    pack, start = godot_pack(source, files, exe)
    version = ""
    renderer = ""
    driver = ""
    if pack:
        with source.lock, source.open(pack) as stream:
            stream.seek(start)
            header = stream.read(20)
        if header[:4] == b"GDPC":
            version = str(int.from_bytes(header[8:12], "little"))
        keys = (b"rendering/renderer/rendering_method", b"rendering/rendering_device/driver.windows",
                b"rendering/driver/driver_name")
        settings = project_settings(source, pack, start)
        if settings is not None:
            found = {}
            for key in keys:
                at = settings.find(key)
                if at >= 0:
                    found[key] = settings[at + len(key):at + len(key) + 96]
        else:
            found = scan(source, pack, start, keys)
        method = found.get(b"rendering/renderer/rendering_method", b"")
        renderer = "gl_compatibility" if b"gl_compatibility" in method else \
                   "mobile" if b"mobile" in method else ""
        driver = "d3d12" if b"d3d12" in found.get(b"rendering/rendering_device/driver.windows", b"") else ""
        if version == "3" or b"GLES2" in found.get(b"rendering/driver/driver_name", b""):
            renderer = "gles"
    csharp = files.has("GodotSharp.dll") or files.dir(r"^data_.*_windows_")
    plan.engine = f"Godot {version or '?'}" + (" C#" if csharp else "")
    if version == "3" or renderer == "gles":
        plan.graphics = "opengl"
        plan.ok(f"{plan.engine} with its OpenGL renderer: OpenGL 4.6 works on the console")
    elif renderer == "gl_compatibility":
        # The console's OpenGL driver crashed compiling Godot 4.7's
        # Compatibility scene shaders (2026-10-09, a 3D game's menu), while
        # Vulkan runs Godot's other renderers well: Mobile is the closest.
        plan.graphics = "auto"
        plan.arguments.append("--rendering-method mobile --rendering-driver vulkan")
        plan.ok(f"{plan.engine}: its Compatibility (OpenGL) renderer crashes the console's OpenGL "
                "driver in 3D scenes, so it runs on Vulkan with the Mobile renderer")
        plan.warn("lighting may look a little different from the editor; for OpenGL instead, set "
                  "Graphics to OpenGL and clear the arguments")
    else:
        # Godot loads Vulkan at run time, so its exe imports no graphics DLL
        # and would read as GDI; "auto" leaves Vulkan to it, as GDI did.
        plan.graphics = "auto"
        if driver == "d3d12":
            plan.arguments.append("--rendering-driver vulkan")
            plan.ok(f"{plan.engine}: the project asks for Direct3D 12, which freezes the console; "
                    "it is started on Vulkan instead")
        else:
            plan.ok(f"{plan.engine} on Vulkan: Vulkan 1.4 works on the console"
                    + ("" if pack else " (its pack could not be read, so the renderer was not checked)"))
    if not csharp and not version:
        plan.warn("if the game shows nothing, try Arguments: --rendering-driver opengl3")


# --- the rules ---------------------------------------------------------------------
def plan(source, exe, imports: set[str], graphics: str) -> Plan:
    """What the game needs. exe is pw_quick's Executable, imports what it
    and the game's DLLs import, graphics pw_quick's guess from them."""
    files = Files(source)
    result = Plan()
    folder = posixpath.dirname(exe.key)
    exe_imports = exe.info.imports

    # Anti-cheat never runs under Wine on the console.
    if files.dir(r"(^|/)(easyanticheat|battleye)$") or files.find(r"(^|/)(be_|easyanticheat).*\.exe$"):
        result.stop("anti-cheat (EasyAntiCheat or BattlEye) is not supported")

    shipping = files.find(r"/binaries/win(64|32)/[^/]+-win(64|32)-shipping\.exe$")
    if files.has("UnityPlayer.dll") or files.dir(r"_data/managed$") or files.find(r"_data/globalgamemanagers$"):
        il2cpp = files.has("GameAssembly.dll")
        result.engine = "Unity (" + ("IL2CPP" if il2cpp else "Mono") + ")"
        result.graphics = "dxvk"
        result.arguments.append("-force-d3d11")
        result.ok(f"{result.engine}: started on Direct3D 11 through DXVK, which works on the console")
        if files.find(r"_data/streamingassets/.*" + VIDEO.pattern):
            result.warn("its videos play through Media Foundation: they need a base prefix with "
                        "the FFmpeg decoders (sender v4.5 or later)")
    elif shipping or files.dir(r"(^|/)engine/binaries$"):
        result.engine = "Unreal Engine"
        if shipping and exe.key not in shipping:
            result.exe = sorted(shipping, key=lambda key: ("win64" not in key.lower(), key))[0]
            result.ok(f"starts {result.exe} directly: the small launcher can't start it on the console")
        result.graphics = "dxvk"
        result.arguments.append("-dx11")
        result.ok("Unreal Engine: started on Direct3D 11 through DXVK (-dx11), which works on the console")
        if files.find(r"/content/movies/.*" + VIDEO.pattern):
            result.warn("its movies play through Media Foundation: they need a base prefix with "
                        "the FFmpeg decoders (sender v4.5 or later)")
    elif godot_pack(source, files, exe.key)[0] or files.has("GodotSharp.dll"):
        godot(result, source, files, exe.key)
    elif files.has("data.win") or files.has("game.unx"):
        result.engine = "GameMaker"
        result.ok("GameMaker: Direct3D 11 through DXVK works on the console")
    elif files.has("nw.dll") or files.has("package.nw") or files.find(r"(^|/)js/(rpg|rmmz)_core\.js$"):
        result.engine = "RPG Maker MV/MZ (NW.js)" if files.find(r"js/(rpg|rmmz)_core\.js$") else "NW.js"
        result.arguments.append(CHILD_PROCESS_ARGS)
        result.warn(f"{result.engine} runs Chromium, which starts helper processes the console refuses; "
                    "it is asked to stay in one process, and may still not start")
    elif files.find(r"(^|/)resources/app\.asar$") or files.has("libcef.dll"):
        result.engine = "Electron" if files.find(r"resources/app\.asar$") else "Chromium (CEF)"
        result.arguments.append(CHILD_PROCESS_ARGS)
        result.warn(f"{result.engine} starts helper processes the console refuses; "
                    "it is asked to stay in one process, and may still not start")
    elif files.find(r"(^|/)rgss\d*\w*\.dll$") or files.has("RPG_RT.exe"):
        old = files.has("RPG_RT.exe")
        result.engine = "RPG Maker 2000/2003" if old else "RPG Maker XP/VX/VX Ace"
        result.ok(f"{result.engine}")
        if old or files.find(r"\.mid$"):
            result.warn("its MIDI music won't play: the console has no MIDI device")
    elif files.dir(r"(^|/)renpy$"):
        result.engine = "Ren'Py"
        result.graphics = "opengl"
        inner = [key for key in files.find(r"(^|/)lib/py\d?-windows-x86_64/[^/]+\.exe$")
                 if posixpath.basename(key).lower() == posixpath.basename(exe.key).lower()]
        if inner:
            result.exe = inner[0]
            result.ok(f"starts {result.exe} directly: Ren'Py's launcher can't start it on the console")
        result.ok("Ren'Py on OpenGL, which works on the console")
    elif files.has("love.dll"):
        result.engine = "LÖVE"
        result.graphics = "opengl"
        result.ok("LÖVE on OpenGL, which works on the console")
    elif files.find(r"(^|/)(jre|jdk|runtime)[^/]*/bin/javaw?\.exe$"):
        result.engine = "Java"
        result.graphics = "opengl"
        result.warn("Java games are usually started by a launcher that the console can't run; "
                    "pick the program to start carefully")

    # .NET
    xna = files.find(r"(^|/)microsoft\.xna\.framework[^/]*\.dll$")
    if not xna and "mscoree.dll" in exe_imports:
        # XNA itself is usually installed, not shipped: the exe names it
        try:
            xna = b"Microsoft.Xna.Framework" in source.read(exe.key, 32 << 20)
        except (OSError, KeyError, AttributeError):
            pass
    if files.has("coreclr.dll"):
        result.ok(".NET runtime bundled: its memory use is capped to what the console grants")
    elif "mscoree.dll" in exe_imports or xna:
        result.engine = result.engine or ("XNA" if xna else ".NET Framework")
        if xna:
            result.graphics = "dxvk"
            # FNA otherwise tries OpenGL first, which doesn't work under DXVK's overrides
            result.environment["FNA3D_FORCE_DRIVER"] = "D3D11"
            result.ok("XNA through Wine Mono's FNA, drawing with Direct3D 11 through DXVK")
        result.warn("needs .NET Framework 4: it runs on Wine Mono instead, which the sender puts on the "
                    "PS5 once (about 170 MB). New: not yet confirmed on the console")
    elif files.find(r"\.runtimeconfig\.json$") and not files.has("hostfxr.dll"):
        result.stop("it needs an installed .NET runtime; only self-contained .NET games run")

    # Graphics
    if "d3d12.dll" in imports and not result.engine:
        # dxgi.dll doesn't count: Direct3D 12 uses it too (Hades II imports
        # only d3d12 and dxgi, and stalled on a black screen)
        older = any(re.match(r"^(d3d9|d3d10(_1)?|d3d11|vulkan-1)\.dll$", name) for name in imports)
        if older:
            result.warn("it can use Direct3D 12, which freezes the console: choose Direct3D 11 "
                        "in its settings if it has the choice")
        else:
            # Wine's own dxgi and d3d12 (vkd3d) instead of DXVK's dxgi, which
            # offers no Direct3D 12 adapter: Hades II found none with it.
            result.graphics = "auto"
            result.warn("it needs Direct3D 12: it gets Wine's own (vkd3d). New and not yet tried on the "
                        "console; vkd3d-proton froze it, so if the screen stops, hold the power button")
    if not result.engine:
        result.ok({"dxvk": "Direct3D through DXVK, which works on the console",
                   "opengl": "OpenGL, which works on the console",
                   "auto": "DirectDraw, which works on the console",
                   "gdi": "Windows drawing (GDI)"}[graphics])
    if "vulkan-1.dll" in exe_imports and not result.engine:
        result.ok("Vulkan works on the console")
        if graphics == "gdi":
            result.graphics = "auto"

    # Launchers
    if re.search(r"launch|start", posixpath.basename(exe.key).lower()) and not result.exe:
        result.warn("this looks like a launcher; the console can't start other programs, "
                    "so pick the game's own program if there is one")

    # Libraries the console lacks and the game doesn't bring
    missing = sorted({MISSING_DLLS[name] for name in imports if name in MISSING_DLLS and not files.has(name)})
    for what in missing:
        result.warn(f"it uses {what}, which the console doesn't have; put the DLL in the game folder")
    if (files.has("steam_api.dll") or files.has("steam_api64.dll")) and not files.has("steam_appid.txt"):
        result.warn("it's a Steam build: if it needs Steam running, it will quit")

    # Controllers, sound, video, text
    xinput = any(name.startswith("xinput") for name in imports) or result.engine.startswith(
        ("Unity", "Unreal", "Godot", "GameMaker", "LÖVE")) or files.has("SDL2.dll") or files.has("SDL3.dll")
    if not xinput and ({"dinput8.dll", "dinput.dll"} & imports):
        result.warn("it reads controllers through DirectInput, which doesn't see the DualSense: "
                    "use a keyboard preset (see docs/CONTROLS.md)")
    videos = [key for key in files.find(VIDEO.pattern) if not OWN_VIDEO.search(key.lower())]
    if videos and not result.engine.startswith(("Unity", "Unreal", "Godot")):
        result.warn("its videos play through the base prefix's decoders: LAV Filters for "
                    "DirectShow, FFmpeg for Media Foundation (sender v4.5 or later)")
    if files.find(r"[぀-ヿ一-鿿]"):
        result.warn("Japanese or Chinese file names: Japanese text uses the prefix's Gothic font; "
                    "the console's code page is Western (1252), so older games may show garbled text")
    if files.find(r"\.mid$") and not result.engine.startswith("RPG Maker"):
        result.warn("its MIDI music won't play: the console has no MIDI device")
    return result
