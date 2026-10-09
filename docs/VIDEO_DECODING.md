# Video playback on the console: status and a plan

## What doesn't work

No Media Foundation video plays on the console. ProbeTris v6.1 on a PS5
(2026-10-08):

- `MFCreateSourceReaderFromURL` fails with `MF_E_UNSUPPORTED_BYTESTREAM_TYPE`
  (c00d36c4) for MP4/H.264, WMV, WebM and MPEG-1.
- DirectShow graphs fail (80040218) until LAV Filters are in the prefix.
  The sender's base prefix ships them (`tools/pw_base_prefix.py --lav`),
  which fixes DirectShow only.

`tools/build_wine_ps5.sh` configures Wine `--without-gstreamer
--without-ffmpeg`. In Wine 11, `winedmo` (FFmpeg) only demuxes, and the
decoder MFTs (H.264, AAC, ...) come from `winegstreamer`. So an FFmpeg port
alone would not decode. This affects Unity's VideoPlayer and Unreal's
WmfMedia intros and cutscenes.

## Fix: FFmpeg inside the prefix (sender v4.5)

Nothing on the console has to change: the missing pieces run as Windows DLLs
in the game's own process. `tools/build_media.sh --out DIR` builds them and
`tools/pw_base_prefix.py --media DIR` puts them in the base prefix:

- `winegstreamer.dll`: Wine's own Windows side (the H.264, AAC, WMV, WMA and
  MPEG audio decoder MFTs, the video and audio converters), patched so its
  Unix calls go to `tools/media/winegstreamer/pw_ffmpeg.c`, which answers
  the `wg_transform_*` calls with libavcodec, swscale and swresample.
- `winedmo.dll`: Wine's FFmpeg demuxer code built into the DLL
  (`tools/media/winedmo/winedmo.patch`), for the MP4, AVI, ASF, WAV and MP3
  file sources.
- A minimal LGPL FFmpeg 7.1 (decoders and demuxers only), as `*-pw-*.dll` so
  a game's own FFmpeg can't clash.

The base prefix registers winegstreamer and wmadmod (64- and 32-bit), sets
`winedmo=native` and `DisableGstByteStreamHandler=1`. With the pinned host
Wine, built like the console's (no GStreamer, no winedmo.so in use),
`tools/media/mftest.c` passes for 64- and 32-bit programs: H.264 elementary
stream (30 of 30 frames), AAC (ADTS), MP4 through the source reader (H.264
to RGB32, AAC to PCM, frame matches FFmpeg's own decode exactly) and WMV/WMA.
Without the DLLs the same tests fail with the console's errors (80040154,
c00d36c4). Not yet confirmed on the console.

## Idea: the console's own decoders (not needed for the above)

The console has hardware decoders that its games use, and prospero-win
already loads system modules through `sceSysmodule` (`native/pw_hid_ps5.c`,
`native/pw_agc_ps5.c`). [KytyPS5](https://github.com/KytyPS5/KytyPS5) (a
PS5 emulator, GPL-2.0) documents the interfaces in `src/libs`:

- `libSceVideodec2`: H.264/HEVC decoding (`src/libs/libVideoDec2.cpp`).
  Its NIDs:

  | Function | NID |
  | --- | --- |
  | `QueryDecoderMemoryInfo` | `qqMCwlULR+E` |
  | `CreateDecoder` | `CNNRoRYd8XI` |
  | `Decode` | `852F5+q6+iM` |
  | `Flush` | `l1hXwscLuCY` |
  | `Reset` | `wJXikG6QFN8` |
  | `DeleteDecoder` | `jwImxXRGSKA` |
  | `GetPictureInfo` | `NtXRa3dRzU0`, `kjrLbcyhEiw` |

  The library also has `QueryComputeMemoryInfo`, `AllocateComputeQueue`
  and `ReleaseComputeQueue`.
- `libSceAvPlayer`: a complete MP4 player (`src/libs/avPlayer.cpp`).
- `libSceAjm`: AAC, MP3 and Opus audio decoding (`src/libs/ajm/`).

A possible shape:

1. Port FFmpeg's demuxers only (plain C, builds on FreeBSD) so `winedmo`
   works.
2. Add H.264 and AAC decoder MFTs whose Unix side calls Videodec2 and Ajm.

First, check whether a payload process may load `libSceVideodec2` at all,
the way the HID modules are loaded. That experiment is cheap.

## Unknowns

- Whether the homebrew title is allowed to load these modules.
- Memory: Videodec2 wants direct memory and a compute queue.
- Kyty reimplements these libraries with FFmpeg on a PC, so it shows the
  call shapes, not the console's behaviour.

This work needs the payload SDK and a console.
