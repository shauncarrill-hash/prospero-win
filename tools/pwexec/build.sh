#!/bin/sh
# Builds tools/pwexec/pwexec32.exe and pwexec64.exe (pwexec.c), placed
# high in the address space and relocatable, so the programs it runs get
# the bases they were linked for (0x400000 for most).
set -e
here=$(cd "$(dirname "$0")" && pwd)
flags="-O2 -Wall -Wno-array-bounds -ffreestanding -fno-builtin -fno-stack-protector -fno-stack-check -nostdlib -nostartfiles -mwindows -s"
i686-w64-mingw32-gcc $flags -o "$here/pwexec32.exe" "$here/pwexec.c" -Wl,-e,_start -Wl,--image-base,0x6f000000 \
    -Wl,--dynamicbase -Wl,--enable-reloc-section -lkernel32
x86_64-w64-mingw32-gcc $flags -o "$here/pwexec64.exe" "$here/pwexec.c" -Wl,-e,start -Wl,--image-base,0x16f000000 \
    -Wl,--dynamicbase -Wl,--enable-reloc-section -lkernel32
