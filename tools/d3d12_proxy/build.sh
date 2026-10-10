#!/bin/sh
# Builds tools/d3d12/d3d12.dll (the proxy) and copies vkd3d-proton 3.0.1
# beside it as d3d12_original.dll and d3d12core.dll.
# Usage: tools/d3d12_proxy/build.sh /path/to/vkd3d-proton-3.0.1/x64
set -e
here=$(cd "$(dirname "$0")" && pwd)
out="$here/../d3d12"
mkdir -p "$out"
x86_64-w64-mingw32-gcc -O2 -Wall -shared -static-libgcc -o "$out/d3d12.dll" \
    "$here/d3d12_proxy.c" "$here/d3d12.def" -lpsapi -Wl,--enable-stdcall-fixup -s
if [ -n "$1" ]; then
    cp "$1/d3d12.dll" "$out/d3d12_original.dll"
    cp "$1/d3d12core.dll" "$out/d3d12core.dll"
fi
