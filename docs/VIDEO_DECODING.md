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

## Idea: the console's own decoders

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
