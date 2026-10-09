/*
 * winegstreamer's transforms on FFmpeg, inside the Windows process.
 *
 * The console's Wine has no GStreamer, so winegstreamer's Unix side does not
 * exist there. This file stands in for it: the same wg_transform calls,
 * answered by FFmpeg DLLs loaded next to winegstreamer.dll. Every Media
 * Foundation, DirectShow and DMO decoder winegstreamer implements on top of
 * wg_transform (H.264, AAC, WMV, WMA, MPEG audio...) then works unchanged.
 *
 * Copyright 2026 prospero-win contributors
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 */

#include <stdarg.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#include "ntstatus.h"
#define WIN32_NO_STATUS
#include "windef.h"
#include "winbase.h"
#include "mfapi.h"
#include "mferror.h"
#include "mmreg.h"
#include "ks.h"
#include "ksmedia.h"

#include "unixlib.h"

#include <libavcodec/avcodec.h>
#include <libavutil/imgutils.h>
#include <libavutil/opt.h>
#include <libavutil/channel_layout.h>
#include <libswscale/swscale.h>
#include <libswresample/swresample.h>

#include "wine/debug.h"

WINE_DEFAULT_DEBUG_CHANNEL(mfplat);

#ifndef WAVE_FORMAT_RAW_AAC1
#define WAVE_FORMAT_RAW_AAC1 0x00ff
#endif
#ifndef WAVE_FORMAT_FLAC
#define WAVE_FORMAT_FLAC 0xf1ac
#endif
#ifndef WAVE_FORMAT_OPUS
#define WAVE_FORMAT_OPUS 0x704f
#endif

#define HNS 10000000 /* 100 ns units per second */

enum kind
{
    KIND_VIDEO_DECODER,
    KIND_AUDIO_DECODER,
    KIND_VIDEO_CONVERTER,
    KIND_AUDIO_CONVERTER,
};

struct packet
{
    struct packet *next;
    AVPacket *pkt;          /* compressed input */
    /* raw input, for converters */
    BYTE *data;
    UINT32 size;
    INT32 stride;
    INT64 pts;
    UINT64 duration;
    UINT32 flags;
};

struct transform
{
    enum kind kind;
    struct wg_transform_attrs attrs;
    struct wg_media_type input, output;     /* owned copies */
    struct wg_media_type reported;          /* what get_output_type answers */
    bool have_reported;

    AVCodecContext *codec;
    AVCodecParserContext *parser; /* splits byte streams into whole frames */
    bool opened;             /* decoder ready; false when its setup bytes were missing */
    struct SwsContext *sws;
    SwrContext *swr;
    AVFrame *frame;          /* decoded and not handed out yet */
    bool frame_ready;
    bool changed;            /* output format changed: STREAM_CHANGE first */
    bool first;              /* no output yet since create/flush */

    struct packet *head, *tail;
    UINT32 queued;
    bool draining;
    bool eof_sent;

    /* converted audio not handed out yet (an output buffer was too small) */
    BYTE *pcm;
    UINT32 pcm_size, pcm_offset, pcm_capacity;
    INT64 pcm_pts;
    bool pcm_has_pts;
};

static struct transform *get_transform(wg_transform_t handle)
{
    return (struct transform *)(ULONG_PTR)handle;
}

static void *sample_data(const struct wg_sample *sample)
{
    return (void *)(ULONG_PTR)sample->data;
}

static bool is_mf_video_area_empty(const MFVideoArea *area)
{
    return !area->OffsetX.value && !area->OffsetY.value && !area->Area.cx && !area->Area.cy;
}

static bool is_fourcc_guid(const GUID *guid)
{
    return !memcmp(&guid->Data2, &MFVideoFormat_Base.Data2, sizeof(GUID) - sizeof(guid->Data1));
}

/* ---- media types -------------------------------------------------------- */

static bool copy_media_type(struct wg_media_type *dst, const struct wg_media_type *src)
{
    dst->major = src->major;
    dst->format_size = src->format_size;
    if (!(dst->u.format = malloc(src->format_size ? src->format_size : 1)))
        return false;
    if (src->format_size)
        memcpy(dst->u.format, src->u.format, src->format_size);
    return true;
}

static void free_media_type(struct wg_media_type *type)
{
    free(type->u.format);
    type->u.format = NULL;
    type->format_size = 0;
}

static bool is_video(const struct wg_media_type *type)
{
    return IsEqualGUID(&type->major, &MFMediaType_Video) && type->format_size >= sizeof(MFVIDEOFORMAT);
}

static bool is_audio(const struct wg_media_type *type)
{
    return IsEqualGUID(&type->major, &MFMediaType_Audio) && type->format_size >= sizeof(WAVEFORMATEX);
}

/* The wave format tag, looking inside WAVEFORMATEXTENSIBLE. */
static WORD wave_tag(const WAVEFORMATEX *wfx, UINT32 size)
{
    if (wfx->wFormatTag == WAVE_FORMAT_EXTENSIBLE && size >= sizeof(WAVEFORMATEXTENSIBLE))
    {
        const WAVEFORMATEXTENSIBLE *ext = (const WAVEFORMATEXTENSIBLE *)wfx;
        return ext->SubFormat.Data1;
    }
    return wfx->wFormatTag;
}

static bool is_raw_audio(const struct wg_media_type *type)
{
    WORD tag;
    if (!is_audio(type))
        return false;
    tag = wave_tag(type->u.audio, type->format_size);
    return tag == WAVE_FORMAT_PCM || tag == WAVE_FORMAT_IEEE_FLOAT;
}

static enum AVSampleFormat raw_audio_format(const WAVEFORMATEX *wfx, UINT32 size)
{
    WORD tag = wave_tag(wfx, size);
    if (tag == WAVE_FORMAT_IEEE_FLOAT)
        return wfx->wBitsPerSample == 64 ? AV_SAMPLE_FMT_DBL : AV_SAMPLE_FMT_FLT;
    switch (wfx->wBitsPerSample)
    {
    case 8: return AV_SAMPLE_FMT_U8;
    case 16: return AV_SAMPLE_FMT_S16;
    case 32: return AV_SAMPLE_FMT_S32;
    default: return AV_SAMPLE_FMT_NONE; /* 24-bit is packed: not an FFmpeg format */
    }
}

static void wave_channel_layout(const WAVEFORMATEX *wfx, UINT32 size, AVChannelLayout *layout)
{
    if (wfx->wFormatTag == WAVE_FORMAT_EXTENSIBLE && size >= sizeof(WAVEFORMATEXTENSIBLE)
            && ((const WAVEFORMATEXTENSIBLE *)wfx)->dwChannelMask)
    {
        /* WinMM's speaker bits are FFmpeg's channel bits for the first 18 */
        if (!av_channel_layout_from_mask(layout, ((const WAVEFORMATEXTENSIBLE *)wfx)->dwChannelMask)
                && layout->nb_channels == wfx->nChannels)
            return;
    }
    av_channel_layout_default(layout, wfx->nChannels);
}

struct video_map
{
    DWORD fourcc;
    enum AVPixelFormat format;
};

/* MF raw video subtypes (FOURCC or D3DFORMAT in Data1) and FFmpeg's layout of them. */
static const struct video_map raw_video[] =
{
    {MAKEFOURCC('N','V','1','2'), AV_PIX_FMT_NV12},
    {MAKEFOURCC('I','4','2','0'), AV_PIX_FMT_YUV420P},
    {MAKEFOURCC('I','Y','U','V'), AV_PIX_FMT_YUV420P},
    {MAKEFOURCC('Y','V','1','2'), AV_PIX_FMT_YUV420P},      /* chroma planes swapped */
    {MAKEFOURCC('Y','U','Y','2'), AV_PIX_FMT_YUYV422},
    {MAKEFOURCC('U','Y','V','Y'), AV_PIX_FMT_UYVY422},
    {MAKEFOURCC('Y','V','Y','U'), AV_PIX_FMT_YVYU422},
    {MAKEFOURCC('A','Y','U','V'), AV_PIX_FMT_VUYA},
    {MAKEFOURCC('P','0','1','0'), AV_PIX_FMT_P010LE},
    {22 /* D3DFMT_X8R8G8B8 */, AV_PIX_FMT_BGR0},
    {21 /* D3DFMT_A8R8G8B8 */, AV_PIX_FMT_BGRA},
    {32 /* D3DFMT_A8B8G8R8 */, AV_PIX_FMT_RGBA},
    {20 /* D3DFMT_R8G8B8 */, AV_PIX_FMT_BGR24},
    {23 /* D3DFMT_R5G6B5 */, AV_PIX_FMT_RGB565LE},
    {24 /* D3DFMT_X1R5G5B5 */, AV_PIX_FMT_RGB555LE},
};

static enum AVPixelFormat raw_video_format(const GUID *subtype)
{
    unsigned int i;
    if (!is_fourcc_guid(subtype))
        return AV_PIX_FMT_NONE;
    for (i = 0; i < ARRAY_SIZE(raw_video); ++i)
        if (raw_video[i].fourcc == subtype->Data1)
            return raw_video[i].format;
    return AV_PIX_FMT_NONE;
}

static bool is_yv12(const GUID *subtype)
{
    return IsEqualGUID(subtype, &MFVideoFormat_YV12);
}

static bool is_rgb(enum AVPixelFormat format)
{
    return format == AV_PIX_FMT_BGR0 || format == AV_PIX_FMT_BGRA || format == AV_PIX_FMT_RGBA
            || format == AV_PIX_FMT_BGR24 || format == AV_PIX_FMT_RGB565LE || format == AV_PIX_FMT_RGB555LE;
}

static enum AVCodecID video_codec(const GUID *subtype)
{
    if (IsEqualGUID(subtype, &MFVideoFormat_H264_ES)) return AV_CODEC_ID_H264;
    if (IsEqualGUID(subtype, &MFVideoFormat_MPEG2)) return AV_CODEC_ID_MPEG2VIDEO;
    if (!is_fourcc_guid(subtype)) return AV_CODEC_ID_NONE;
    switch (subtype->Data1)
    {
    case MAKEFOURCC('H','2','6','4'): case MAKEFOURCC('h','2','6','4'):
    case MAKEFOURCC('A','V','C','1'): case MAKEFOURCC('a','v','c','1'): return AV_CODEC_ID_H264;
    case MAKEFOURCC('H','E','V','C'): case MAKEFOURCC('H','E','V','S'):
    case MAKEFOURCC('H','2','6','5'): return AV_CODEC_ID_HEVC;
    case MAKEFOURCC('W','M','V','1'): return AV_CODEC_ID_WMV1;
    case MAKEFOURCC('W','M','V','2'): return AV_CODEC_ID_WMV2;
    case MAKEFOURCC('W','M','V','3'): return AV_CODEC_ID_WMV3;
    case MAKEFOURCC('W','V','C','1'): case MAKEFOURCC('W','M','V','A'): return AV_CODEC_ID_VC1;
    case MAKEFOURCC('M','P','G','1'): return AV_CODEC_ID_MPEG1VIDEO;
    case MAKEFOURCC('M','P','4','V'): case MAKEFOURCC('M','P','4','S'):
    case MAKEFOURCC('M','4','S','2'): case MAKEFOURCC('X','V','I','D'):
    case MAKEFOURCC('D','I','V','X'): case MAKEFOURCC('D','X','5','0'): return AV_CODEC_ID_MPEG4;
    case MAKEFOURCC('M','P','4','3'): case MAKEFOURCC('D','I','V','3'): return AV_CODEC_ID_MSMPEG4V3;
    case MAKEFOURCC('M','P','4','2'): return AV_CODEC_ID_MSMPEG4V2;
    case MAKEFOURCC('M','P','G','4'): return AV_CODEC_ID_MSMPEG4V1;
    case MAKEFOURCC('c','v','i','d'): return AV_CODEC_ID_CINEPAK;
    case MAKEFOURCC('I','V','5','0'): return AV_CODEC_ID_INDEO5;
    case MAKEFOURCC('V','P','8','0'): return AV_CODEC_ID_VP8;
    case MAKEFOURCC('V','P','9','0'): return AV_CODEC_ID_VP9;
    case MAKEFOURCC('M','J','P','G'): return AV_CODEC_ID_MJPEG;
    case MAKEFOURCC('t','h','e','o'): return AV_CODEC_ID_THEORA;
    default: return AV_CODEC_ID_NONE;
    }
}

/* HEAACWAVEINFO's payload: 0 raw, 1 ADTS, 2 ADIF, 3 LOAS */
static int aac_payload(const WAVEFORMATEX *wfx, UINT32 size)
{
    if (wave_tag(wfx, size) == WAVE_FORMAT_MPEG_ADTS_AAC)
        return 1;
    if (wfx->wFormatTag == WAVE_FORMAT_MPEG_HEAAC && size >= sizeof(HEAACWAVEINFO))
        return ((const HEAACWAVEINFO *)wfx)->wPayloadType;
    return 0;
}

static enum AVCodecID audio_codec(const WAVEFORMATEX *wfx, UINT32 size)
{
    switch (wave_tag(wfx, size))
    {
    case WAVE_FORMAT_MPEG_HEAAC:
        return aac_payload(wfx, size) == 3 ? AV_CODEC_ID_AAC_LATM : AV_CODEC_ID_AAC;
    case WAVE_FORMAT_MPEG_ADTS_AAC:
    case WAVE_FORMAT_RAW_AAC1: return AV_CODEC_ID_AAC;
    case WAVE_FORMAT_MPEG_LOAS: return AV_CODEC_ID_AAC_LATM;
    case WAVE_FORMAT_MPEGLAYER3: return AV_CODEC_ID_MP3;
    case WAVE_FORMAT_MPEG:
        if (size >= sizeof(MPEG1WAVEFORMAT))
        {
            const MPEG1WAVEFORMAT *mpeg = (const MPEG1WAVEFORMAT *)wfx;
            if (mpeg->fwHeadLayer == ACM_MPEG_LAYER1) return AV_CODEC_ID_MP1;
            if (mpeg->fwHeadLayer == ACM_MPEG_LAYER3) return AV_CODEC_ID_MP3;
        }
        return AV_CODEC_ID_MP2;
    case WAVE_FORMAT_MSAUDIO1: return AV_CODEC_ID_WMAV1;
    case WAVE_FORMAT_WMAUDIO2: return AV_CODEC_ID_WMAV2;
    case WAVE_FORMAT_WMAUDIO3: return AV_CODEC_ID_WMAPRO;
    case WAVE_FORMAT_WMAVOICE9: return AV_CODEC_ID_WMAVOICE;
    case WAVE_FORMAT_ADPCM: return AV_CODEC_ID_ADPCM_MS;
    case WAVE_FORMAT_IMA_ADPCM: return AV_CODEC_ID_ADPCM_IMA_WAV;
    case WAVE_FORMAT_FLAC: return AV_CODEC_ID_FLAC;
    case WAVE_FORMAT_OPUS: return AV_CODEC_ID_OPUS;
    default: return AV_CODEC_ID_NONE;
    }
}

/* Codec setup bytes carried after the format structure. */
static void audio_extradata(const WAVEFORMATEX *wfx, UINT32 size, const BYTE **data, UINT32 *len)
{
    WORD tag = wave_tag(wfx, size);
    *data = NULL;
    *len = 0;
    if (tag == WAVE_FORMAT_MPEG_HEAAC && aac_payload(wfx, size))
        return;    /* ADTS and LOAS carry their own headers: parsed, no setup bytes */
    if (tag == WAVE_FORMAT_MPEG_HEAAC && size >= sizeof(HEAACWAVEINFO))
    {
        /* HEAACWAVEINFO, then AudioSpecificConfig for the raw payload type */
        *data = (const BYTE *)wfx + sizeof(HEAACWAVEINFO);
        *len = size - sizeof(HEAACWAVEINFO);
    }
    else if (wfx->wFormatTag == WAVE_FORMAT_EXTENSIBLE)
    {
        if (size > sizeof(WAVEFORMATEXTENSIBLE))
        {
            *data = (const BYTE *)wfx + sizeof(WAVEFORMATEXTENSIBLE);
            *len = size - sizeof(WAVEFORMATEXTENSIBLE);
        }
    }
    else if (wfx->cbSize && size >= sizeof(WAVEFORMATEX) + wfx->cbSize)
    {
        *data = (const BYTE *)(wfx + 1);
        *len = wfx->cbSize;
    }
}

/* ---- creating ------------------------------------------------------------ */

static bool set_extradata(AVCodecContext *codec, const BYTE *data, UINT32 len)
{
    if (!len)
        return true;
    if (!(codec->extradata = av_mallocz(len + AV_INPUT_BUFFER_PADDING_SIZE)))
        return false;
    memcpy(codec->extradata, data, len);
    codec->extradata_size = len;
    return true;
}

static int thread_count(void)
{
    SYSTEM_INFO info;
    GetSystemInfo(&info);
    return min(max((int)info.dwNumberOfProcessors, 1), 8);
}

static NTSTATUS open_video_decoder(struct transform *transform)
{
    const MFVIDEOFORMAT *format = transform->input.u.video;
    enum AVCodecID id = video_codec(&format->guidFormat);
    const AVCodec *decoder;
    AVCodecContext *codec;

    if (id == AV_CODEC_ID_NONE || !(decoder = avcodec_find_decoder(id)))
    {
        FIXME("No decoder for video subtype %s.\n", debugstr_guid(&format->guidFormat));
        return STATUS_NOT_SUPPORTED;
    }
    if (!(codec = transform->codec = avcodec_alloc_context3(decoder)))
        return STATUS_NO_MEMORY;
    codec->width = format->videoInfo.dwWidth;
    codec->height = format->videoInfo.dwHeight;
    codec->pkt_timebase = (AVRational){1, HNS};
    if (format->videoInfo.FramesPerSecond.Numerator && format->videoInfo.FramesPerSecond.Denominator)
        codec->framerate = (AVRational){format->videoInfo.FramesPerSecond.Numerator,
                                        format->videoInfo.FramesPerSecond.Denominator};
    codec->thread_count = thread_count();
    /* frame threading holds back one frame per thread: too much for low latency */
    codec->thread_type = transform->attrs.low_latency ? FF_THREAD_SLICE : FF_THREAD_FRAME | FF_THREAD_SLICE;
    if (transform->attrs.low_latency)
        codec->flags |= AV_CODEC_FLAG_LOW_DELAY;
    if (!set_extradata(codec, (const BYTE *)(format + 1), transform->input.format_size - sizeof(*format)))
        return STATUS_NO_MEMORY;
    /* Decoders are created without their setup bytes to see whether a format
     * is supported at all (WMV3 can't open without them): that's a yes. */
    if (!(transform->opened = avcodec_open2(codec, decoder, NULL) >= 0))
        WARN("Couldn't open the %s decoder yet.\n", decoder->name);
    else
        TRACE("Opened %s for %ux%u.\n", decoder->name, codec->width, codec->height);
    return STATUS_SUCCESS;
}

static NTSTATUS open_audio_decoder(struct transform *transform)
{
    const WAVEFORMATEX *wfx = transform->input.u.audio;
    enum AVCodecID id = audio_codec(wfx, transform->input.format_size);
    const AVCodec *decoder;
    AVCodecContext *codec;
    const BYTE *extra;
    UINT32 extra_len;

    if (id == AV_CODEC_ID_NONE || !(decoder = avcodec_find_decoder(id)))
    {
        FIXME("No decoder for wave format %#x.\n", wave_tag(wfx, transform->input.format_size));
        return STATUS_NOT_SUPPORTED;
    }
    if (!(codec = transform->codec = avcodec_alloc_context3(decoder)))
        return STATUS_NO_MEMORY;
    codec->sample_rate = wfx->nSamplesPerSec;
    wave_channel_layout(wfx, transform->input.format_size, &codec->ch_layout);
    codec->block_align = wfx->nBlockAlign;
    codec->bit_rate = (int64_t)wfx->nAvgBytesPerSec * 8;
    codec->bits_per_coded_sample = wfx->wBitsPerSample;
    codec->pkt_timebase = (AVRational){1, HNS};
    audio_extradata(wfx, transform->input.format_size, &extra, &extra_len);
    if (!set_extradata(codec, extra, extra_len))
        return STATUS_NO_MEMORY;
    if (!(transform->opened = avcodec_open2(codec, decoder, NULL) >= 0))
        WARN("Couldn't open the %s decoder yet.\n", decoder->name);
    else
        TRACE("Opened %s, %u Hz, %u channels.\n", decoder->name, codec->sample_rate, codec->ch_layout.nb_channels);
    return STATUS_SUCCESS;
}

/* Elementary streams may come in any chunks (GStreamer runs a parser too);
 * packetized formats (WMV, WMA, raw AAC) need none. */
static void open_parser(struct transform *transform)
{
    switch (transform->codec->codec_id)
    {
    case AV_CODEC_ID_H264: case AV_CODEC_ID_HEVC: case AV_CODEC_ID_MPEG1VIDEO: case AV_CODEC_ID_MPEG2VIDEO:
    case AV_CODEC_ID_MP1: case AV_CODEC_ID_MP2: case AV_CODEC_ID_MP3: case AV_CODEC_ID_AAC_LATM:
        break;
    case AV_CODEC_ID_AAC:
        if (!transform->codec->extradata_size)   /* ADTS */
            break;
        return;
    default:
        return;
    }
    transform->parser = av_parser_init(transform->codec->codec_id);
}

NTSTATUS pw_transform_destroy(void *args);

NTSTATUS pw_transform_create(void *args)
{
    struct wg_transform_create_params *params = args;
    struct transform *transform;
    NTSTATUS status;

    if (!(transform = calloc(1, sizeof(*transform))))
        return STATUS_NO_MEMORY;
    transform->attrs = params->attrs;
    transform->first = true;
    if (!copy_media_type(&transform->input, &params->input_type)
            || !copy_media_type(&transform->output, &params->output_type)
            || !(transform->frame = av_frame_alloc()))
    {
        status = STATUS_NO_MEMORY;
        goto failed;
    }

    if (is_video(&transform->input) && is_video(&transform->output)
            && raw_video_format(&transform->output.u.video->guidFormat) != AV_PIX_FMT_NONE)
    {
        if (raw_video_format(&transform->input.u.video->guidFormat) != AV_PIX_FMT_NONE)
        {
            transform->kind = KIND_VIDEO_CONVERTER;
            status = STATUS_SUCCESS;
        }
        else
        {
            transform->kind = KIND_VIDEO_DECODER;
            if (!(status = open_video_decoder(transform)))
                open_parser(transform);
        }
    }
    else if (is_audio(&transform->input) && is_raw_audio(&transform->output)
            && raw_audio_format(transform->output.u.audio, transform->output.format_size) != AV_SAMPLE_FMT_NONE)
    {
        if (is_raw_audio(&transform->input))
        {
            transform->kind = KIND_AUDIO_CONVERTER;
            status = raw_audio_format(transform->input.u.audio, transform->input.format_size) != AV_SAMPLE_FMT_NONE
                    ? STATUS_SUCCESS : STATUS_NOT_SUPPORTED;
        }
        else
        {
            transform->kind = KIND_AUDIO_DECODER;
            if (!(status = open_audio_decoder(transform)))
                open_parser(transform);
        }
    }
    else
    {
        FIXME("Unsupported transform: %s to %s (encoders aren't implemented).\n",
                debugstr_guid(&params->input_type.major), debugstr_guid(&params->output_type.major));
        status = STATUS_NOT_SUPPORTED;
    }
    if (status)
        goto failed;

    TRACE("Created transform %p, kind %u.\n", transform, transform->kind);
    params->transform = (wg_transform_t)(ULONG_PTR)transform;
    return STATUS_SUCCESS;

failed:
    {
        wg_transform_t handle = (wg_transform_t)(ULONG_PTR)transform;
        pw_transform_destroy(&handle);
    }
    return status;
}

static void free_packet(struct packet *packet)
{
    av_packet_free(&packet->pkt);
    free(packet->data);
    free(packet);
}

static void drop_input(struct transform *transform)
{
    struct packet *packet;
    while ((packet = transform->head))
    {
        transform->head = packet->next;
        free_packet(packet);
    }
    transform->tail = NULL;
    transform->queued = 0;
}

NTSTATUS pw_transform_destroy(void *args)
{
    struct transform *transform = get_transform(*(wg_transform_t *)args);

    drop_input(transform);
    if (transform->parser)
        av_parser_close(transform->parser);
    avcodec_free_context(&transform->codec);
    sws_freeContext(transform->sws);
    swr_free(&transform->swr);
    av_frame_free(&transform->frame);
    free_media_type(&transform->input);
    free_media_type(&transform->output);
    free_media_type(&transform->reported);
    free(transform->pcm);
    free(transform);
    return STATUS_SUCCESS;
}

/* ---- output types --------------------------------------------------------- */

static UINT32 align_up(UINT32 value, UINT32 align)
{
    return (value + align) & ~align;
}

/* The video output type for a decoded frame size, as GStreamer's side reports it. */
static NTSTATUS video_output_type(struct transform *transform, int width, int height, AVRational sar,
        struct wg_media_type *type)
{
    MFVIDEOFORMAT *format;
    UINT32 align = transform->attrs.output_plane_align;

    if (!(format = calloc(1, sizeof(*format))))
        return STATUS_NO_MEMORY;
    format->dwSize = sizeof(*format);
    format->guidFormat = transform->output.u.video->guidFormat;
    format->videoInfo.dwWidth = align_up(width, align);
    format->videoInfo.dwHeight = align_up(height, align);
    if (format->videoInfo.dwWidth != width || format->videoInfo.dwHeight != height)
    {
        format->videoInfo.MinimumDisplayAperture.Area.cx = width;
        format->videoInfo.MinimumDisplayAperture.Area.cy = height;
    }
    format->videoInfo.GeometricAperture = format->videoInfo.MinimumDisplayAperture;
    format->videoInfo.PanScanAperture = format->videoInfo.MinimumDisplayAperture;
    format->videoInfo.PixelAspectRatio.Numerator = sar.num > 0 ? sar.num : 1;
    format->videoInfo.PixelAspectRatio.Denominator = sar.den > 0 ? sar.den : 1;
    format->videoInfo.FramesPerSecond = transform->input.u.video->videoInfo.FramesPerSecond;
    if (!format->videoInfo.FramesPerSecond.Numerator && transform->codec
            && transform->codec->framerate.num && transform->codec->framerate.den)
    {
        format->videoInfo.FramesPerSecond.Numerator = transform->codec->framerate.num;
        format->videoInfo.FramesPerSecond.Denominator = transform->codec->framerate.den;
    }

    free_media_type(type);
    type->major = MFMediaType_Video;
    type->format_size = sizeof(*format);
    type->u.video = format;
    return STATUS_SUCCESS;
}

NTSTATUS pw_transform_get_output_type(void *args)
{
    struct wg_transform_get_output_type_params *params = args;
    struct transform *transform = get_transform(params->transform);
    const struct wg_media_type *type = transform->have_reported ? &transform->reported : &transform->output;
    UINT32 capacity = params->media_type.format_size;

    params->media_type.major = type->major;
    params->media_type.format_size = type->format_size;
    if (capacity < type->format_size || !params->media_type.u.format)
        return STATUS_BUFFER_TOO_SMALL;
    memcpy(params->media_type.u.format, type->u.format, type->format_size);
    return STATUS_SUCCESS;
}

NTSTATUS pw_transform_set_output_type(void *args)
{
    struct wg_transform_set_output_type_params *params = args;
    struct transform *transform = get_transform(params->transform);
    struct wg_media_type type;

    if (!copy_media_type(&type, &params->media_type))
        return STATUS_NO_MEMORY;
    if (is_video(&type) ? raw_video_format(&type.u.video->guidFormat) == AV_PIX_FMT_NONE
            : !is_raw_audio(&type) || raw_audio_format(type.u.audio, type.format_size) == AV_SAMPLE_FMT_NONE)
    {
        free_media_type(&type);
        return STATUS_UNSUCCESSFUL;
    }
    free_media_type(&transform->output);
    transform->output = type;

    /* What's decoded but not read yet is converted to the new type when it's read. */
    if (transform->have_reported)
    {
        if (is_video(&transform->reported))
        {
            transform->reported.u.video->guidFormat = type.u.video->guidFormat;
            transform->reported.u.video->videoInfo.VideoFlags = type.u.video->videoInfo.VideoFlags;
        }
        else
        {
            free_media_type(&transform->reported);
            transform->have_reported = copy_media_type(&transform->reported, &type);
        }
    }
    /* Audio already converted for the old type is converted again from scratch. */
    transform->pcm_size = transform->pcm_offset = 0;
    swr_free(&transform->swr);
    return STATUS_SUCCESS;
}

/* ---- input ---------------------------------------------------------------- */

static void queue_packet(struct transform *transform, struct packet *packet)
{
    if (transform->tail)
        transform->tail->next = packet;
    else
        transform->head = packet;
    transform->tail = packet;
    transform->queued++;
}

static struct packet *new_packet(const BYTE *data, int size, INT64 pts, INT64 duration, UINT32 flags)
{
    struct packet *packet;

    if (!(packet = calloc(1, sizeof(*packet))))
        return NULL;
    if (!(packet->pkt = av_packet_alloc()) || av_new_packet(packet->pkt, size) < 0)
    {
        free(packet);
        return NULL;
    }
    memcpy(packet->pkt->data, data, size);
    packet->pkt->pts = pts;
    if (duration > 0)
        packet->pkt->duration = duration;
    if (flags & WG_SAMPLE_FLAG_SYNC_POINT)
        packet->pkt->flags |= AV_PKT_FLAG_KEY;
    packet->flags = flags;
    return packet;
}

/* Run bytes through the parser, queueing each whole frame it finds. NULL data
 * flushes the frame it still holds. */
static NTSTATUS parse(struct transform *transform, const BYTE *data, int size, INT64 pts, UINT32 flags)
{
    bool flush = !data;

    while (size > 0 || flush)
    {
        uint8_t *out;
        int out_size, used;

        used = av_parser_parse2(transform->parser, transform->codec, &out, &out_size,
                data, size, pts, AV_NOPTS_VALUE, 0);
        if (used < 0)
            return STATUS_UNSUCCESSFUL;
        data += used;
        size -= used;
        pts = AV_NOPTS_VALUE;   /* the rest of this input belongs to the same timestamp only once */
        if (out_size)
        {
            struct packet *packet;
            UINT32 out_flags = (transform->parser->key_frame == 1 ? WG_SAMPLE_FLAG_SYNC_POINT : 0)
                    | (flags & WG_SAMPLE_FLAG_DISCONTINUITY);
            if (!(packet = new_packet(out, out_size, transform->parser->pts, 0, out_flags)))
                return STATUS_NO_MEMORY;
            queue_packet(transform, packet);
        }
        else if (flush)
            break;
        if (!used && !out_size)
            break;
    }
    return STATUS_SUCCESS;
}

NTSTATUS pw_transform_push_data(void *args)
{
    struct wg_transform_push_data_params *params = args;
    struct transform *transform = get_transform(params->transform);
    struct wg_sample *sample = params->sample;
    struct packet *packet;

    if (transform->draining || transform->queued >= transform->attrs.input_queue_length + 1)
    {
        TRACE("Refusing %u bytes, %u queued, draining %d.\n", sample->size, transform->queued, transform->draining);
        params->result = MF_E_NOTACCEPTING;
        return STATUS_SUCCESS;
    }

    if (transform->codec && !transform->opened)
    {
        ERR("The %s decoder never opened: its setup bytes are missing or bad.\n", transform->codec->codec->name);
        return STATUS_UNSUCCESSFUL;
    }
    if (transform->codec)
    {
        INT64 pts = sample->flags & WG_SAMPLE_FLAG_HAS_PTS ? sample->pts : AV_NOPTS_VALUE;
        INT64 duration = sample->flags & WG_SAMPLE_FLAG_HAS_DURATION ? sample->duration : 0;
        NTSTATUS status;

        if (transform->parser)
        {
            if ((status = parse(transform, sample_data(sample), sample->size, pts, sample->flags)))
                return status;
        }
        else if (!(packet = new_packet(sample_data(sample), sample->size, pts, duration, sample->flags)))
            return STATUS_NO_MEMORY;
        else
            queue_packet(transform, packet);
        TRACE("Queued %u bytes, %u packets queued.\n", sample->size, transform->queued);
        params->result = S_OK;
        return STATUS_SUCCESS;
    }
    else
    {
        if (!(packet = calloc(1, sizeof(*packet))))
            return STATUS_NO_MEMORY;
        packet->pts = sample->pts;
        packet->duration = sample->duration;
        packet->flags = sample->flags;
        UINT32 size = sample->stride ? sample->max_size : sample->size;
        if (!(packet->data = malloc(size ? size : 1)))
        {
            free_packet(packet);
            return STATUS_NO_MEMORY;
        }
        memcpy(packet->data, sample_data(sample), size);
        packet->size = size;
        packet->stride = sample->stride;
    }
    queue_packet(transform, packet);

    TRACE("Queued %u bytes, %u queued.\n", sample->size, transform->queued);
    params->result = S_OK;
    return STATUS_SUCCESS;
}

static struct packet *pop_input(struct transform *transform)
{
    struct packet *packet;
    if (!(packet = transform->head))
        return NULL;
    if (!(transform->head = packet->next))
        transform->tail = NULL;
    transform->queued--;
    return packet;
}

/* ---- video planes ----------------------------------------------------------- */

struct planes
{
    uint8_t *data[4];
    int linesize[4];
    UINT32 size;
};

/* Lay out a frame of format in a buffer, the way Windows does: plane 0's stride
 * is the buffer's (or the aligned width's), later planes follow at the aligned
 * height, bottom-up for RGB unless the stride says otherwise. */
static bool layout_planes(enum AVPixelFormat format, bool yv12, int width, int height, UINT32 plane_align,
        const MFVideoInfo *info, INT32 stride, BYTE *buffer, struct planes *planes)
{
    const AVPixFmtDescriptor *desc = av_pix_fmt_desc_get(format);
    int aligned_w = align_up(width, plane_align), aligned_h = align_up(height, plane_align);
    int bytes_per_pixel, i, lines[4], offset = 0;
    bool bottom_up;

    if (!desc)
        return false;
    memset(planes, 0, sizeof(*planes));

    if (!is_mf_video_area_empty(&info->MinimumDisplayAperture) && info->dwWidth >= width && info->dwHeight >= height)
    {
        aligned_w = max(aligned_w, (int)info->dwWidth);
        aligned_h = max(aligned_h, (int)info->dwHeight);
    }

    bottom_up = is_rgb(format) && (stride < 0 || (!stride && (info->VideoFlags & MFVideoFlag_BottomUpLinearRep)));
    if (stride < 0)
        stride = -stride;

    bytes_per_pixel = av_get_padded_bits_per_pixel(desc) / 8;
    if (desc->flags & AV_PIX_FMT_FLAG_PLANAR || desc->nb_components == 1)
        bytes_per_pixel = (desc->comp[0].depth + 7) / 8;
    if (format == AV_PIX_FMT_YUYV422 || format == AV_PIX_FMT_UYVY422 || format == AV_PIX_FMT_YVYU422)
        bytes_per_pixel = 2;
    if (!stride)
    {
        stride = aligned_w * bytes_per_pixel;
        if (format == AV_PIX_FMT_NV12 || format == AV_PIX_FMT_P010LE)
            stride = (stride + 1) & ~1;
        if (is_rgb(format))
            stride = (stride + 3) & ~3;
    }

    planes->linesize[0] = stride;
    lines[0] = aligned_h;
    for (i = 1; i < 4; ++i)
        lines[i] = 0;
    switch (format)
    {
    case AV_PIX_FMT_NV12: case AV_PIX_FMT_P010LE:
        planes->linesize[1] = stride;
        lines[1] = (aligned_h + 1) / 2;
        break;
    case AV_PIX_FMT_YUV420P:
        planes->linesize[1] = planes->linesize[2] = stride / 2;
        lines[1] = lines[2] = (aligned_h + 1) / 2;
        break;
    default:
        break;
    }

    for (i = 0; i < 4 && planes->linesize[i]; ++i)
    {
        planes->data[i] = buffer + offset;
        offset += planes->linesize[i] * lines[i];
    }
    planes->size = offset;
    if (yv12)
    {
        uint8_t *u = planes->data[1];
        planes->data[1] = planes->data[2];
        planes->data[2] = u;
    }
    if (bottom_up)
    {
        planes->data[0] += (height - 1) * planes->linesize[0];
        planes->linesize[0] = -planes->linesize[0];
    }
    return true;
}

static bool convert_video(struct transform *transform, const uint8_t *const src[4], const int src_linesize[4],
        enum AVPixelFormat src_format, int src_w, int src_h, struct wg_sample *sample, int dst_w, int dst_h)
{
    const MFVIDEOFORMAT *out = transform->output.u.video;
    enum AVPixelFormat dst_format = raw_video_format(&out->guidFormat);
    struct planes planes;

    if (!layout_planes(dst_format, is_yv12(&out->guidFormat), dst_w, dst_h, transform->attrs.output_plane_align,
            &out->videoInfo, sample->stride, sample_data(sample), &planes))
        return false;
    if (planes.size > sample->max_size)
    {
        ERR("Output buffer is too small: %u < %u.\n", sample->max_size, planes.size);
        return false;
    }
    if (!(transform->sws = sws_getCachedContext(transform->sws, src_w, src_h, src_format,
            dst_w, dst_h, dst_format, SWS_BILINEAR, NULL, NULL, NULL)))
    {
        ERR("Can't convert %s %dx%d to %s %dx%d.\n", av_get_pix_fmt_name(src_format), src_w, src_h,
                av_get_pix_fmt_name(dst_format), dst_w, dst_h);
        return false;
    }
    sws_scale(transform->sws, src, src_linesize, 0, src_h, planes.data, planes.linesize);
    sample->size = planes.size;
    return true;
}

/* ---- decoding --------------------------------------------------------------- */

/* Feed packets until a frame comes out. false: none without more input. */
static bool decode_frame(struct transform *transform)
{
    struct packet *packet;
    int ret;

    for (;;)
    {
        ret = avcodec_receive_frame(transform->codec, transform->frame);
        if (!ret)
            return true;
        if (ret == AVERROR_EOF)
        {
            /* drained: ready for a new stream */
            avcodec_flush_buffers(transform->codec);
            transform->draining = transform->eof_sent = false;
            return false;
        }
        if (ret != AVERROR(EAGAIN))
            WARN("Decoder error %d.\n", ret);

        if ((packet = pop_input(transform)))
        {
            if ((packet->flags & WG_SAMPLE_FLAG_DISCONTINUITY) && !transform->first)
                TRACE("Discontinuity.\n");
            ret = avcodec_send_packet(transform->codec, packet->pkt);
            if (ret < 0 && ret != AVERROR(EAGAIN))
                WARN("Failed to decode %d bytes, error %d.\n", packet->pkt->size, ret);
            free_packet(packet);
            continue;
        }
        if (transform->draining && !transform->eof_sent)
        {
            transform->eof_sent = true;
            avcodec_send_packet(transform->codec, NULL);
            continue;
        }
        if (transform->draining && ret == AVERROR(EAGAIN))
            transform->draining = false;
        return false;
    }
}

static void set_frame_times(struct transform *transform, const AVFrame *frame, struct wg_sample *sample)
{
    INT64 pts = frame->best_effort_timestamp != AV_NOPTS_VALUE ? frame->best_effort_timestamp : frame->pts;
    INT64 duration = frame->duration;

    if (pts != AV_NOPTS_VALUE)
    {
        sample->pts = pts;
        sample->flags |= WG_SAMPLE_FLAG_HAS_PTS;
        if (transform->attrs.preserve_timestamps)
            sample->flags |= WG_SAMPLE_FLAG_PRESERVE_TIMESTAMPS;
    }
    if (duration <= 0 && transform->kind == KIND_VIDEO_DECODER)
    {
        const MFRatio *fps = &transform->input.u.video->videoInfo.FramesPerSecond;
        if (fps->Numerator && fps->Denominator)
            duration = (INT64)HNS * fps->Denominator / fps->Numerator;
        else if (transform->codec->framerate.num && transform->codec->framerate.den)
            duration = (INT64)HNS * transform->codec->framerate.den / transform->codec->framerate.num;
    }
    if (duration > 0)
    {
        sample->duration = duration;
        sample->flags |= WG_SAMPLE_FLAG_HAS_DURATION;
    }
    if (frame->flags & AV_FRAME_FLAG_KEY)
        sample->flags |= WG_SAMPLE_FLAG_SYNC_POINT;
}

/* The size the output gets: the decoded size, or the output type's when its
 * size is fixed (no format changes allowed). */
static void output_size(struct transform *transform, int width, int height, int *out_w, int *out_h)
{
    const MFVideoInfo *info = &transform->output.u.video->videoInfo;

    *out_w = width;
    *out_h = height;
    if (!transform->attrs.allow_format_change && info->dwWidth && info->dwHeight)
    {
        if (!is_mf_video_area_empty(&info->MinimumDisplayAperture))
        {
            *out_w = info->MinimumDisplayAperture.Area.cx;
            *out_h = info->MinimumDisplayAperture.Area.cy;
        }
        else
        {
            *out_w = info->dwWidth;
            *out_h = info->dwHeight;
        }
    }
}

static NTSTATUS read_video(struct transform *transform, struct wg_sample *sample, HRESULT *result)
{
    AVFrame *frame = transform->frame;
    int width, height;

    if (!transform->frame_ready)
    {
        if (!decode_frame(transform))
        {
            *result = MF_E_TRANSFORM_NEED_MORE_INPUT;
            return STATUS_SUCCESS;
        }
        transform->frame_ready = true;

        /* The first frame, or a new size: like GStreamer's side, report a
         * stream change first so the caller picks up the new type. */
        {
            struct wg_media_type type = {0};
            const MFVIDEOFORMAT *old = transform->have_reported ? transform->reported.u.video : NULL;
            output_size(transform, frame->width, frame->height, &width, &height);
            if (video_output_type(transform, width, height, frame->sample_aspect_ratio, &type))
                return STATUS_NO_MEMORY;
            if (!old || transform->first || memcmp(&old->videoInfo, &type.u.video->videoInfo, sizeof(old->videoInfo)))
            {
                if (transform->attrs.allow_format_change)
                    transform->changed = true;
                free_media_type(&transform->reported);
                transform->reported = type;
                transform->have_reported = true;
            }
            else
                free_media_type(&type);
            transform->first = false;
        }
    }

    if (transform->changed)
    {
        transform->changed = false;
        TRACE("Format change: %lux%lu.\n", transform->reported.u.video->videoInfo.dwWidth,
                transform->reported.u.video->videoInfo.dwHeight);
        sample->size = 0;
        *result = MF_E_TRANSFORM_STREAM_CHANGE;
        return STATUS_SUCCESS;
    }

    output_size(transform, frame->width, frame->height, &width, &height);
    if (!convert_video(transform, (const uint8_t *const *)frame->data, frame->linesize, frame->format,
            frame->width, frame->height, sample, width, height))
    {
        sample->size = 0;
        return STATUS_UNSUCCESSFUL;
    }
    sample->flags = 0;
    set_frame_times(transform, frame, sample);
    av_frame_unref(frame);
    transform->frame_ready = false;
    *result = S_OK;
    return STATUS_SUCCESS;
}

/* Raw video in, raw video out: colour conversion and scaling. */
static NTSTATUS read_converted_video(struct transform *transform, struct wg_sample *sample, HRESULT *result)
{
    const MFVIDEOFORMAT *in = transform->input.u.video, *out = transform->output.u.video;
    enum AVPixelFormat in_format = raw_video_format(&in->guidFormat);
    int in_w = in->videoInfo.dwWidth, in_h = in->videoInfo.dwHeight, out_w, out_h;
    struct packet *packet;
    struct planes planes;
    bool ok;

    if (!(packet = pop_input(transform)))
    {
        transform->draining = false;
        *result = MF_E_TRANSFORM_NEED_MORE_INPUT;
        return STATUS_SUCCESS;
    }
    if (!is_mf_video_area_empty(&in->videoInfo.MinimumDisplayAperture))
    {
        in_w = in->videoInfo.MinimumDisplayAperture.Area.cx;
        in_h = in->videoInfo.MinimumDisplayAperture.Area.cy;
    }
    out_w = out->videoInfo.dwWidth ? (int)out->videoInfo.dwWidth : in_w;
    out_h = out->videoInfo.dwHeight ? (int)out->videoInfo.dwHeight : in_h;
    if (!is_mf_video_area_empty(&out->videoInfo.MinimumDisplayAperture))
    {
        out_w = out->videoInfo.MinimumDisplayAperture.Area.cx;
        out_h = out->videoInfo.MinimumDisplayAperture.Area.cy;
    }

    ok = layout_planes(in_format, is_yv12(&in->guidFormat), in_w, in_h, 0, &in->videoInfo, packet->stride,
            packet->data, &planes) && planes.size <= packet->size;
    if (!ok)
        ERR("Input buffer of %u bytes is too small for %dx%d.\n", packet->size, in_w, in_h);
    else
        ok = convert_video(transform, (const uint8_t *const *)planes.data, planes.linesize, in_format,
                in_w, in_h, sample, out_w, out_h);
    sample->flags = packet->flags & (WG_SAMPLE_FLAG_HAS_PTS | WG_SAMPLE_FLAG_HAS_DURATION | WG_SAMPLE_FLAG_SYNC_POINT
            | WG_SAMPLE_FLAG_DISCONTINUITY);
    sample->pts = packet->pts;
    sample->duration = packet->duration;
    free_packet(packet);
    if (!ok)
    {
        sample->size = 0;
        return STATUS_UNSUCCESSFUL;
    }
    *result = S_OK;
    return STATUS_SUCCESS;
}

/* ---- audio ------------------------------------------------------------------- */

static bool make_resampler(struct transform *transform, const AVChannelLayout *in_layout,
        enum AVSampleFormat in_format, int in_rate)
{
    const WAVEFORMATEX *out = transform->output.u.audio;
    AVChannelLayout out_layout;

    wave_channel_layout(out, transform->output.format_size, &out_layout);
    swr_free(&transform->swr);
    if (swr_alloc_set_opts2(&transform->swr, &out_layout, raw_audio_format(out, transform->output.format_size),
            out->nSamplesPerSec, in_layout, in_format, in_rate, 0, NULL) < 0 || swr_init(transform->swr) < 0)
    {
        ERR("Can't convert audio to %u Hz, %u channels.\n", (UINT)out->nSamplesPerSec, out->nChannels);
        swr_free(&transform->swr);
        return false;
    }
    return true;
}

/* Convert samples into the PCM buffer, after what's left in it. */
static bool append_pcm(struct transform *transform, const uint8_t **in, int count, int in_rate)
{
    const WAVEFORMATEX *out = transform->output.u.audio;
    int max_out = swr_get_out_samples(transform->swr, count), got;
    UINT32 need;
    uint8_t *dst;

    if (transform->pcm_offset)
    {
        memmove(transform->pcm, transform->pcm + transform->pcm_offset, transform->pcm_size - transform->pcm_offset);
        transform->pcm_size -= transform->pcm_offset;
        transform->pcm_offset = 0;
    }
    if (max_out < 0)
        return false;
    need = transform->pcm_size + max_out * out->nBlockAlign;
    if (need > transform->pcm_capacity)
    {
        BYTE *pcm = realloc(transform->pcm, need);
        if (!pcm)
            return false;
        transform->pcm = pcm;
        transform->pcm_capacity = need;
    }
    dst = transform->pcm + transform->pcm_size;
    if ((got = swr_convert(transform->swr, &dst, max_out, in, count)) < 0)
        return false;
    transform->pcm_size += got * out->nBlockAlign;
    (void)in_rate;
    return true;
}

static bool fill_pcm(struct transform *transform)
{
    const WAVEFORMATEX *out = transform->output.u.audio;
    AVFrame *frame = transform->frame;

    if (transform->kind == KIND_AUDIO_DECODER)
    {
        if (!decode_frame(transform))
        {
            /* drained: what the resampler still holds */
            if (transform->swr && swr_get_delay(transform->swr, out->nSamplesPerSec) > 0)
                append_pcm(transform, NULL, 0, 0);
            return false;
        }
        if (!transform->swr && !make_resampler(transform, &frame->ch_layout, frame->format, frame->sample_rate))
            return false;
        if (transform->pcm_size == transform->pcm_offset)
        {
            INT64 pts = frame->best_effort_timestamp != AV_NOPTS_VALUE ? frame->best_effort_timestamp : frame->pts;
            transform->pcm_has_pts = pts != AV_NOPTS_VALUE;
            transform->pcm_pts = pts;
        }
        if (!append_pcm(transform, (const uint8_t **)frame->extended_data, frame->nb_samples, frame->sample_rate))
            WARN("Failed to convert %d samples.\n", frame->nb_samples);
        av_frame_unref(frame);
        return true;
    }
    else
    {
        const WAVEFORMATEX *in = transform->input.u.audio;
        struct packet *packet = pop_input(transform);
        AVChannelLayout layout;
        const uint8_t *data[1];

        if (!packet)
        {
            transform->draining = false;
            return false;
        }
        if (!transform->swr)
        {
            wave_channel_layout(in, transform->input.format_size, &layout);
            if (!make_resampler(transform, &layout, raw_audio_format(in, transform->input.format_size),
                    in->nSamplesPerSec))
            {
                free_packet(packet);
                return false;
            }
        }
        if (transform->pcm_size == transform->pcm_offset)
        {
            transform->pcm_has_pts = !!(packet->flags & WG_SAMPLE_FLAG_HAS_PTS);
            transform->pcm_pts = packet->pts;
        }
        data[0] = packet->data;
        append_pcm(transform, data, packet->size / max(in->nBlockAlign, 1), in->nSamplesPerSec);
        free_packet(packet);
        return true;
    }
}

static NTSTATUS read_audio(struct transform *transform, struct wg_sample *sample, HRESULT *result)
{
    const WAVEFORMATEX *out = transform->output.u.audio;
    UINT32 block = max(out->nBlockAlign, 1), left, size;

    while (transform->pcm_size - transform->pcm_offset == 0)
    {
        if (!fill_pcm(transform) && transform->pcm_size - transform->pcm_offset == 0)
        {
            sample->size = 0;
            *result = MF_E_TRANSFORM_NEED_MORE_INPUT;
            return STATUS_SUCCESS;
        }
    }

    left = transform->pcm_size - transform->pcm_offset;
    size = min(left, sample->max_size / block * block);
    memcpy(sample_data(sample), transform->pcm + transform->pcm_offset, size);
    sample->size = size;
    sample->flags = WG_SAMPLE_FLAG_SYNC_POINT;
    if (size < left)
        sample->flags |= WG_SAMPLE_FLAG_INCOMPLETE;
    if (transform->pcm_has_pts)
    {
        sample->pts = transform->pcm_pts;
        sample->flags |= WG_SAMPLE_FLAG_HAS_PTS;
        if (transform->attrs.preserve_timestamps)
            sample->flags |= WG_SAMPLE_FLAG_PRESERVE_TIMESTAMPS;
    }
    sample->duration = (UINT64)size / block * HNS / max(out->nSamplesPerSec, 1);
    sample->flags |= WG_SAMPLE_FLAG_HAS_DURATION;
    transform->pcm_pts += sample->duration;
    transform->pcm_offset += size;
    *result = S_OK;
    return STATUS_SUCCESS;
}

NTSTATUS pw_transform_read_data(void *args)
{
    struct wg_transform_read_data_params *params = args;
    struct transform *transform = get_transform(params->transform);

    switch (transform->kind)
    {
    case KIND_VIDEO_DECODER: return read_video(transform, params->sample, &params->result);
    case KIND_VIDEO_CONVERTER: return read_converted_video(transform, params->sample, &params->result);
    default: return read_audio(transform, params->sample, &params->result);
    }
}

NTSTATUS pw_transform_get_status(void *args)
{
    struct wg_transform_get_status_params *params = args;
    struct transform *transform = get_transform(params->transform);

    params->accepts_input = !transform->draining && transform->queued < transform->attrs.input_queue_length + 1;
    return STATUS_SUCCESS;
}

NTSTATUS pw_transform_drain(void *args)
{
    struct transform *transform = get_transform(*(wg_transform_t *)args);

    TRACE("Draining %u packets.\n", transform->queued);
    if (transform->parser && transform->opened)
        parse(transform, NULL, 0, AV_NOPTS_VALUE, 0);
    if (transform->codec || transform->queued)
        transform->draining = true;
    transform->eof_sent = false;
    return STATUS_SUCCESS;
}

NTSTATUS pw_transform_flush(void *args)
{
    struct transform *transform = get_transform(*(wg_transform_t *)args);

    drop_input(transform);
    if (transform->parser)
    {
        /* a parser can't be flushed: start a new one */
        av_parser_close(transform->parser);
        transform->parser = av_parser_init(transform->codec->codec_id);
    }
    if (transform->codec && transform->opened)
        avcodec_flush_buffers(transform->codec);
    av_frame_unref(transform->frame);
    transform->frame_ready = transform->changed = false;
    transform->draining = transform->eof_sent = false;
    transform->pcm_size = transform->pcm_offset = 0;
    if (transform->swr)
        swr_free(&transform->swr);
    return STATUS_SUCCESS;
}

NTSTATUS pw_transform_notify_qos(void *args)
{
    return STATUS_SUCCESS;
}
