# Sending a game with the sender app

The sender is a small window for Windows (and Linux and macOS): pick a game's
zip or folder, type your PS5's address, press **Send to PS5**. It needs no
Linux PC and no Wine.

It's for games that are already installed: a folder copied from a Windows PC,
a GOG or Steam install, or a zip of one. Games whose installer has to run, or
that need registry entries an installer writes, still go through a recipe
([installing games](INSTALLING_GAMES.md)).

## What you need

- The PS5 set up as in [getting started](GETTING_STARTED.md): the FTP server
  and ELF loader running.
- `prospero-win-sender.exe`, and the base prefix,
  `prospero-base-prefix.zip`, next to it. The base prefix is a clean Wine
  prefix every game starts from (see [making the base
  prefix](#making-the-base-prefix)).
- The game, as a folder or a zip.

## .NET Framework and XNA games

The console's Wine has no .NET Framework. Games that need it (and XNA games)
run on Wine Mono instead: `wine-mono-11.3.0.zip` next to the sender
(`tools/build_mono.sh` makes it) is put on the PS5 once, in
`/data/prospero-win/shared`, and each such game's prefix points at it. XNA
games go through Wine Mono's FNA on Direct3D 11 (DXVK).

Mono loads the type of every field of a class as soon as it compiles code
that uses the class, where .NET waits until the code runs. So a store build
that names an assembly it never ships still needs it on Mono: Stardew
Valley from GOG names Steamworks.NET in a class it never uses, and quit with
a `TypeLoadException`. The sender copies a stand-in
(`tools/stubs/Steamworks.NET.dll`, made by `tools/build_steamworks.sh`) beside
the game's exe when the exe names Steamworks.NET and the folder lacks it.

## Getting the logs

**Get logs** (next to **Check**) copies the app's logs
(`/data/prospero-win/logs`) and the games' profiles from the PS5 into one
zip in a `logs` folder beside the sender, and says which session log is the
newest run.

## Sending payloads

The **Payloads** box sends `.elf` and `.bin` payloads (an FTP server,
etaHEN, ...) to the PS5's ELF loader, port 9021 by default. **Add…** them
once; the sender remembers the list and which ones are selected. Click to
select, ↑ and ↓ to reorder, and **Inject selected** sends them top to bottom,
2 seconds apart, to the IP address in the PS5 box.

## Sending a game

1. Type the PS5's IP address and its FTP port (2121 for ps5-payload-dev's
   ftpsrv), and press **Check**. It says whether the prospero-win app is
   installed. If it isn't, press **Install or update the app…** and pick the
   release zip: it uploads `PPSA99995` to `/data/homebrew` and marks
   `eboot.bin` and the modules executable.
2. Press **Choose zip…** or **Choose folder…** and pick the game. The sender
   reads the game's programs and fills in:
   - **Program to start:** the likeliest executable, skipping installers,
     uninstallers and tools. Pick another if it guessed wrong.
   - **Graphics:** Direct3D 8 to 11 through DXVK, OpenGL, or 2D, from the
     DLLs the game's program and its own DLLs use.
   - **Resolution**, a **controller preset** from prospero-win-profiles'
     `input/` folder (optional), and **arguments** for the program.
3. Press **Send to PS5**.

The game appears in the launcher under the name you gave it. You can stop a
send and press **Send** again later: it carries on where it stopped. Sending a
game that is already on the PS5 asks first, since its saves and settings
there would be replaced.

Files go over four FTP connections at once (`FTP_LANES` in
`tools/pw_quick.py`). Each file costs several round trips before its bytes
move, which for a prefix's thousands of small files is most of a send;
ftpsrv serves each connection on its own thread, so they overlap. A console
that refuses the extra connections gets as many as it takes.

## What it does

It does what [installing games](INSTALLING_GAMES.md) and `pw_prefix.py push`
do, from the game's files instead of an installer:

```text
/data/prospero-win/
  prefixes/<game>/                  the base prefix, plus:
    drive_c/Games/<game>/...        the game's files, read straight from the zip
    drive_c/windows/system32/       DXVK (64-bit) and wowprospero.dll
    drive_c/windows/syswow64/       DXVK (32-bit)
  profiles/<game>.profile           written from the game's program
  profiles/profiles.lst             the game added, when the list exists
```

- DXVK is the pinned release from `tools/pw_install.py`, downloaded once and
  checked against its SHA-256.
- `wowprospero.dll` is copied from the app on the PS5.
- The registry's WoW64 CPU is switched to prospero-win's translator, as
  `pw_prefix.py` does.
- What has been sent is recorded per PS5 and game in
  `%LOCALAPPDATA%\prospero-win\quick` (`~/.local/state/prospero-win/quick`
  elsewhere).

The command line does the same, for scripts:

```sh
python3 tools/pw_quick.py "Half-Life.zip" --base prospero-base-prefix.zip --host <PS5 IP>
python3 tools/pw_quick.py --install-app prospero-win-<version>.zip --host <PS5 IP>
```

## Making the base prefix

The PS5 can't make a Wine prefix, so the base prefix is made once on Linux
with the Wine that matches the PS5's, and then shared:

```sh
tools/build_host_wine.sh --source <pinned Wine checkout> --jobs 8
python3 tools/pw_base_prefix.py --wine <path printed above> --out prospero-base-prefix.zip
```

It holds only what Wine's `wineboot` writes, Wine's own files, under Wine's
licence (LGPL-2.1-or-later). Wine's symbolic links are stored as
`.pw-symlinks` tables, as `pw_prefix.py` sends them, so the zip works from any
PC. The `sender` workflow's **base prefix** job builds it on GitHub Actions.

## Building the sender

```sh
python3 -m pip install pyinstaller
python3 -m PyInstaller --onefile --windowed --name prospero-win-sender \
    --paths tools --exclude-module yaml tools/pw_gui.py
```

The `sender` workflow builds `prospero-win-sender.exe` on Windows.

## What the sender works out by itself

When a game is picked, `tools/pw_autoconfig.py` reads its files and decides
the engine, the program to start, the graphics backend and arguments, and
lists checks in the window (and as comments in the profile): ✓ works, ! may
not fully work, ✗ will not run. Its rules come from ProbeTris v6.1 on a PS5:

| Game | What the sender does |
| --- | --- |
| Unity | DXVK, `-force-d3d11` |
| Unreal Engine | starts the `*-Shipping.exe` (the launcher stub can't start it), DXVK, `-dx11` |
| Godot | reads the pack: Vulkan with the Mobile renderer for Compatibility projects (the console's OpenGL driver crashes on their 3D shaders), OpenGL for Godot 3; `--rendering-driver vulkan` when the project asks for Direct3D 12 |
| Ren'Py, LÖVE, Java | OpenGL; Ren'Py's inner `lib/py*-windows-x86_64` program |
| NW.js, Electron, CEF | single-process arguments; warns, since the console refuses child processes |
| Direct3D 12 only, .NET Framework, XNA, anti-cheat | marked as not running |

It also notes Media Foundation videos (decoded by the base prefix's FFmpeg
DLLs, see [video decoding](VIDEO_DECODING.md)), MIDI music, DirectInput-only controllers, Steam builds and DLLs
the console lacks.

`tools/pw_base_prefix.py --fonts DIR --lav DIR --media DIR` makes a base
prefix with stand-in fonts (Liberation, IPAGothic), LAV Filters, which
DirectShow video needs, and the Media Foundation decoders from
`tools/build_media.sh`; the sender writes the font replacements into each game's
`user.reg`.

## Registry entries (.reg files)

A game that wants something in the registry, like a CD key, can carry it as
a `.reg` file exported with Windows' regedit. Put the file anywhere in the
game's folder or zip: the sender puts its keys in the game's own registry
(HKLM and HKCR in system.reg, HKCU in user.reg), on a full send and on a
settings-only send. For a 32-bit game, keys exported straight under
HKLM\Software also go under Wow6432Node, where a 32-bit program looks.

## Direct3D 12 games

A game that draws only with Direct3D 12 (Hades II) gets Graphics
"Direct3D 12 (vkd3d-proton)". The sender puts three files beside its exe:
vkd3d-proton 3.0.1 (`d3d12_original.dll`, `d3d12core.dll`) and a small proxy
`d3d12.dll` (tools/d3d12_proxy). The profile keeps DXVK for dxgi and adds
`d3d12,d3d12core` to the native overrides; a settings-only send keeps all of it.

The proxy:
- exports Windows' ordinals 100-117 (games import D3D12CreateDevice by
  ordinal 101, and Wine aborts on a missing one);
- creates the device at feature level 11_0 when a game asks for more (the
  PS5's vkd3d-proton device tops out at 11_1); `PW_D3D12_FEATURE_LEVEL`
  (hex) changes it, 0 passes requests on;
- raises the RAM the game is told about to `PW_FAKE_RAM_GB` (16 by default
  for these games; Wine reports about 546 MB on the console, below most
  games' minimum). It adds no real memory; 0 turns it off.
It logs to `d3d12-proxy.log` beside the exe and to the session log.
A game that needs real feature level 12 features still fails.
