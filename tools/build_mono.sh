#!/bin/sh
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# wine-mono-<version>.zip for the sender (tools/pw_quick.py MONO_*): Wine
# Mono, Wine's .NET Framework 4 stand-in (with FNA for XNA games), as the
# app's Wine asks for it (dlls/appwiz.cpl/addons.c MONO_VERSION). The
# release is trimmed of what only compiling needs (the *-api reference
# assemblies, msbuild, xbuild): 228 MB and 2790 files down to 125 MB and 445.
# The sender puts it on the console once, in /data/prospero-win/shared.
#
# Usage: tools/build_mono.sh --out DIR [--version 11.3.0]
set -eu
version=11.3.0
out=
while [ $# -gt 0 ]; do
    case $1 in
    --out) out=$2; shift ;;
    --version) version=$2; shift ;;
    *) echo "build_mono: unknown argument $1" >&2; exit 2 ;;
    esac
    shift
done
[ -n "$out" ] || { echo "build_mono: --out DIR is required" >&2; exit 2; }
mkdir -p "$out"
out=$(cd "$out" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
name=wine-mono-$version
curl -sSLf -o "$work/$name.tar.xz" \
    "https://github.com/madewokherd/wine-mono/releases/download/$name/$name-x86.tar.xz"
curl -sSLf -o "$work/COPYING" "https://raw.githubusercontent.com/madewokherd/wine-mono/$name/COPYING"
tar -xJf "$work/$name.tar.xz" -C "$work"
(cd "$work/$name/lib/mono" && rm -rf ./*-api msbuild xbuild xbuild-frameworks lldb)
cp "$work/COPYING" "$work/$name/COPYING"
rm -f "$out/$name.zip"
(cd "$work" && zip -qr -9 "$out/$name.zip" "$name")
echo "build_mono: $out/$name.zip ($(du -h "$out/$name.zip" | cut -f1))"
