#!/bin/sh
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Video and audio decoding for the base prefix (tools/pw_base_prefix.py
# --media). The console's Wine is built without GStreamer and FFmpeg
# (build_wine_ps5.sh), so Media Foundation has no decoders and no file
# sources: every game video through it fails. This builds the two missing
# pieces as Windows DLLs that run inside the game's process instead:
#
# - winegstreamer.dll: Wine's own Windows-side winegstreamer (the H.264,
#   AAC, WMV, WMA and MPEG audio decoders, the video and audio converters),
#   with its Unix calls answered by FFmpeg (tools/media/winegstreamer).
# - winedmo.dll: Wine's FFmpeg demuxer code, which normally runs as
#   winedmo.so, built into the DLL (tools/media/winedmo). Media Foundation's
#   MP4, AVI, ASF/WMV, WAV and MP3 sources read files through it.
#
# Both use a small LGPL FFmpeg 7.1 (decoders and demuxers only, no
# encoders), built here as -pw DLLs so a game's own FFmpeg can't clash.
# Wine's sources come from the host Wine build (tools/build_host_wine.sh),
# whose build tree provides widl, wrc, winegcc and the import libraries.
#
# Usage:
#   tools/build_media.sh --out DIR [--work DIR] [--wine-work DIR] [--jobs N]
# DIR gets x86_64-windows/ and i386-windows/, each with winegstreamer.dll,
# winedmo.dll and the FFmpeg DLLs.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=$root/.deps/media
wine_work=${PROSPERO_HOST_WINE_WORK:-$root/.deps/wine-host}
jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)
out=
ffmpeg_tag=n7.1
fail() { echo "build_media: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
    case $1 in
    --out) out=$2; shift ;;
    --work) work=$2; shift ;;
    --wine-work) wine_work=$2; shift ;;
    --jobs) jobs=$2; shift ;;
    *) fail "unknown argument $1" ;;
    esac
    shift
done
[ -n "$out" ] || fail "--out DIR is required"
wine_src=$wine_work/source
wine_build=$wine_work/build
[ -x "$wine_build/tools/winegcc/winegcc" ] || fail "no host Wine build in $wine_build (tools/build_host_wine.sh)"
for tool in i686-w64-mingw32-gcc x86_64-w64-mingw32-gcc nasm git; do
    command -v "$tool" >/dev/null || fail "$tool is required"
done
mkdir -p "$work" "$out"
work=$(cd "$work" && pwd)
out=$(cd "$out" && pwd)

# --- FFmpeg ------------------------------------------------------------------
[ -d "$work/ffmpeg/.git" ] ||
    git clone -q --depth 1 --branch "$ffmpeg_tag" https://github.com/FFmpeg/FFmpeg "$work/ffmpeg"
ffmpeg() {
    arch=$1
    [ -f "$work/ff-$arch/lib/libavcodec-pw.dll.a" ] && return
    mkdir -p "$work/build-$arch"
    (cd "$work/build-$arch" && "$work/ffmpeg/configure" --prefix="$work/ff-$arch" --target-os=mingw32 \
        --arch="$arch" --cross-prefix="$arch-w64-mingw32-" --build-suffix=-pw --enable-shared --disable-static \
        --disable-programs --disable-doc --disable-network --disable-everything --disable-autodetect \
        --enable-w32threads --disable-avdevice --disable-avfilter --enable-swscale --enable-swresample \
        --enable-decoder=h264,hevc,aac,aac_latm,mp1,mp2,mp3,mp1float,mp2float,mp3float,wmav1,wmav2,wmapro,wmavoice,wmv1,wmv2,wmv3,vc1,mpeg1video,mpeg2video,mpeg4,msmpeg4v1,msmpeg4v2,msmpeg4v3,cinepak,indeo5,vp8,vp9,theora,vorbis,opus,flac,pcm_s16le,pcm_s24le,pcm_f32le,adpcm_ms,adpcm_ima_wav,mjpeg \
        --enable-parser=h264,hevc,aac,aac_latm,mpegaudio,mpegvideo,mpeg4video,vc1,vp8,vp9,vorbis,opus,flac \
        --enable-demuxer=mov,avi,asf,matroska,mpegts,mpegps,mpegvideo,mp3,aac,wav,ogg,flac,h264,hevc,m4v \
        --enable-bsf=h264_mp4toannexb,hevc_mp4toannexb,aac_adtstoasc,extract_extradata,vp9_superframe_split \
        --enable-protocol=file --extra-ldflags=-static-libgcc >configure.log &&
        make -j"$jobs" >make.log && make install >/dev/null) || fail "FFmpeg $arch failed (see $work/build-$arch)"
}
ffmpeg x86_64
ffmpeg i686

# --- Wine's sources, patched ---------------------------------------------------
staged=$work/wine
rm -rf "$staged"
mkdir -p "$staged/dlls"
cp -r "$wine_src/dlls/winegstreamer" "$wine_src/dlls/winedmo" "$staged/dlls/"
(cd "$staged" && patch -s -p1 <"$root/tools/media/winegstreamer/winegstreamer.patch" &&
    patch -s -p1 <"$root/tools/media/winedmo/winedmo.patch") || fail "the patches do not apply to $wine_src"
cp "$root"/tools/media/winegstreamer/*.c "$staged/dlls/winegstreamer/"

# --- the DLLs ------------------------------------------------------------------
build() {
    arch=$1
    case $arch in
    x86_64) triple=x86_64-w64-mingw32; ff=$work/ff-x86_64; flags="-mlong-double-64 -mcx16 -mcmodel=small" ;;
    i386) triple=i686-w64-mingw32; ff=$work/ff-i686
          flags="-fno-omit-frame-pointer -mpreferred-stack-boundary=2 -mlong-double-64 -msse2" ;;
    esac
    obj=$work/obj-$arch
    dest=$out/$arch-windows
    mkdir -p "$obj/gst" "$obj/dmo" "$dest"
    lib() { echo "$wine_build/$1/$arch-windows/lib$2.a"; }
    base="-I$wine_build/include -I$wine_src/include -I$wine_src/include/msvcrt -D_UCRT -D__WINESRC__ -D__WINE_PE_BUILD \
        -Wall -fno-strict-aliasing -ffunction-sections -Wno-format -O2 $flags"
    crt="$(lib libs/winecrt0 winecrt0) $(lib libs/compiler-rt compiler-rt) $(lib dlls/ucrtbase ucrtbase) \
        $(lib dlls/kernel32 kernel32) $(lib dlls/ntdll ntdll)"

    # winegstreamer: the Windows-side files only (the others are its Unix side)
    gst=$staged/dlls/winegstreamer
    inc="-I$obj/gst -I$gst $base"
    "$wine_build/tools/widl/widl" -o "$obj/gst/winegstreamer_classes_r.res" -b $triple --nostdinc \
        -L"$wine_build/dlls/*" -I$gst -I$wine_build/include -I$wine_src/include "$gst/winegstreamer_classes.idl"
    "$wine_build/tools/wrc/wrc" -u -o "$obj/gst/rsrc.res" --nostdinc -I$gst -I$wine_build/include -I$wine_src/include \
        "$gst/rsrc.rc"
    "$wine_build/tools/wrc/wrc" -u -o "$obj/gst/version.res" --nostdinc -I$wine_build/include -I$wine_src/include \
        -DVER_FILEDESCRIPTION_STR="\"Wine GStreamer (FFmpeg, prospero-win)\"" \
        -DVER_INTERNALNAME_STR="\"winegstreamer.dll\"" -DVER_OLESELFREGISTER=1 "$wine_src/include/common.ver"
    objs=
    for c in aac_decoder main media_sink media_source mfplat quartz_parser quartz_transform video_decoder \
             video_encoder wg_sample wm_reader wma_decoder pw_unixcall pw_ffmpeg; do
        extra=
        [ $c = pw_ffmpeg ] && extra="-I$ff/include -Wno-deprecated-declarations"
        $triple-gcc -c -o "$obj/gst/$c.o" "$gst/$c.c" $inc $extra
        objs="$objs $obj/gst/$c.o"
    done
    # not --wine-builtin: a native DLL in the prefix, the console's Wine has none
    "$wine_build/tools/winegcc/winegcc" -o "$dest/winegstreamer.dll" --wine-objdir "$wine_build" \
        --cc-cmd="$triple-gcc" -b $triple -shared "$gst/winegstreamer.spec" $objs \
        "$obj/gst/rsrc.res" "$obj/gst/version.res" "$obj/gst/winegstreamer_classes_r.res" \
        "$(lib libs/strmbase strmbase)" "$(lib dlls/ole32 ole32)" "$(lib dlls/oleaut32 oleaut32)" \
        "$(lib dlls/msdmo msdmo)" "$(lib dlls/user32 user32)" \
        "$wine_build/dlls/mfplat/$arch-windows/libmfplat.delay.a" "$wine_build/dlls/mf/$arch-windows/libmf.delay.a" \
        "$(lib libs/uuid uuid)" "$(lib libs/mfuuid mfuuid)" "$(lib libs/dmoguids dmoguids)" \
        "$(lib libs/strmiids strmiids)" "$(lib libs/wmcodecdspuuid wmcodecdspuuid)" \
        "$ff/lib/libavcodec-pw.dll.a" "$ff/lib/libavutil-pw.dll.a" "$ff/lib/libswscale-pw.dll.a" \
        "$ff/lib/libswresample-pw.dll.a" $crt

    # winedmo: everything, the Unix side included
    dmo=$staged/dlls/winedmo
    "$wine_build/tools/wrc/wrc" -u -o "$obj/dmo/version.res" --nostdinc -I$wine_build/include -I$wine_src/include \
        -DVER_FILEDESCRIPTION_STR="\"Wine demuxers (FFmpeg, prospero-win)\"" -DVER_INTERNALNAME_STR="\"winedmo.dll\"" \
        "$wine_src/include/common.ver"
    objs=
    for c in main unix_demuxer unix_media_type unixlib; do
        extra=
        case $c in unix*) extra="-I$ff/include -DHAVE_FFMPEG -DHAVE_LIBAVCODEC_BSF_H -Wno-deprecated-declarations" ;; esac
        $triple-gcc -c -o "$obj/dmo/$c.o" "$dmo/$c.c" -I$dmo $base $extra
        objs="$objs $obj/dmo/$c.o"
    done
    "$wine_build/tools/winegcc/winegcc" -o "$dest/winedmo.dll" --wine-objdir "$wine_build" \
        --cc-cmd="$triple-gcc" -b $triple -shared "$dmo/winedmo.spec" $objs "$obj/dmo/version.res" \
        "$(lib libs/mfuuid mfuuid)" "$ff/lib/libavformat-pw.dll.a" "$ff/lib/libavcodec-pw.dll.a" \
        "$ff/lib/libavutil-pw.dll.a" $crt

    cp "$ff"/bin/*-pw-*.dll "$dest/"
    $triple-strip --strip-debug "$dest"/*.dll
}
build x86_64
build i386
for arch in x86_64 i386; do
    echo "build_media: $out/$arch-windows: $(ls "$out/$arch-windows" | tr '\n' ' ')"
done
