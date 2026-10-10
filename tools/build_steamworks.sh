#!/bin/sh
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# tools/stubs/Steamworks.NET.dll for the sender (tools/pw_quick.py
# STUB_ASSEMBLIES): Steamworks.NET 7.0.0 (MIT), for .NET Framework games
# whose store build names it without shipping it, such as Stardew Valley from
# GOG. Wine Mono loads the types of every field of a class it compiles, so it
# needs the assembly even though the game never calls it. The one change:
# SteamAPI.Init answers false when CSteamworks.dll (Steam's glue) is missing,
# instead of throwing.
#
# Needs Wine with Wine Mono in its prefix, for Wine Mono's csc.exe.
# Usage: tools/build_steamworks.sh --wine PATH --mono DIR --out DIR
#   --mono: an unpacked wine-mono-<version> folder (tools/build_mono.sh)
set -eu
wine= mono= out=
while [ $# -gt 0 ]; do
    case $1 in
    --wine) wine=$2; shift ;;
    --mono) mono=$2; shift ;;
    --out) out=$2; shift ;;
    *) echo "build_steamworks: unknown argument $1" >&2; exit 2 ;;
    esac
    shift
done
[ -n "$wine" ] && [ -n "$mono" ] && [ -n "$out" ] || { echo "build_steamworks: --wine, --mono and --out are required" >&2; exit 2; }
mkdir -p "$out"
out=$(cd "$out" && pwd)
mono=$(cd "$mono" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git -c advice.detachedHead=false clone -q --depth 1 --branch 7.0.0 https://github.com/rlabrecque/Steamworks.NET.git "$work/src"
python3 - "$work/src/Plugins/Steamworks.NET/Steam.cs" <<'PY'
import sys
path = sys.argv[1]
text = open(path).read()
for call in ("NativeMethods.SteamAPI_InitSafe();", "NativeMethods.SteamAPI_Init();"):
    old = "\t\t\tInteropHelp.TestIfPlatformSupported();\n\t\t\treturn " + call
    new = ("\t\t\ttry {\n\t\t\t\tInteropHelp.TestIfPlatformSupported();\n\t\t\t\treturn " + call +
           "\n\t\t\t} catch (System.DllNotFoundException) {\n\t\t\t\treturn false;\n\t\t\t}")
    assert old in text, call
    text = text.replace(old, new, 1)
open(path, "w").write(text)
PY
winpath() { echo "Z:$1" | tr / '\\'; }
grep -o 'Compile Include="\.\.[^"]*"' "$work/src/Standalone/Steamworks.NET.csproj" \
    | sed 's/Compile Include="\.\.//; s/"$//' | tr '\\' / \
    | while read -r file; do winpath "$work/src$file"; done > "$work/sources.rsp"
WINEDEBUG=-all "$wine" "$(winpath "$mono/lib/mono/4.5/csc.exe")" /nologo /nowarn:618,649 \
    /target:library /optimize+ /unsafe '/define:TRACE;STEAMWORKS_WIN' \
    "/out:$(winpath "$out/Steamworks.NET.dll")" \
    "$(winpath "$work/src/Standalone/Properties/AssemblyInfo.cs")" "@$(winpath "$work/sources.rsp")"
echo "build_steamworks: $out/Steamworks.NET.dll"
