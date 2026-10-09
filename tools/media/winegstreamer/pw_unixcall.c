/*
 * winegstreamer's Unix calls, answered in this process (prospero-win).
 *
 * Transforms run on FFmpeg (pw_ffmpeg.c). Parsers and muxers aren't
 * implemented: their callers fail the way they do without GStreamer.
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 */

#include "ntstatus.h"
#define WIN32_NO_STATUS
#include "gst_private.h"

#include "wine/debug.h"

WINE_DEFAULT_DEBUG_CHANNEL(mfplat);

NTSTATUS pw_transform_create(void *args);
NTSTATUS pw_transform_destroy(void *args);
NTSTATUS pw_transform_get_output_type(void *args);
NTSTATUS pw_transform_set_output_type(void *args);
NTSTATUS pw_transform_push_data(void *args);
NTSTATUS pw_transform_read_data(void *args);
NTSTATUS pw_transform_get_status(void *args);
NTSTATUS pw_transform_drain(void *args);
NTSTATUS pw_transform_flush(void *args);
NTSTATUS pw_transform_notify_qos(void *args);

NTSTATUS pw_unix_call(unsigned int code, void *args)
{
    switch (code)
    {
    case unix_wg_init_gstreamer: return STATUS_SUCCESS;
    case unix_wg_transform_create: return pw_transform_create(args);
    case unix_wg_transform_destroy: return pw_transform_destroy(args);
    case unix_wg_transform_get_output_type: return pw_transform_get_output_type(args);
    case unix_wg_transform_set_output_type: return pw_transform_set_output_type(args);
    case unix_wg_transform_push_data: return pw_transform_push_data(args);
    case unix_wg_transform_read_data: return pw_transform_read_data(args);
    case unix_wg_transform_get_status: return pw_transform_get_status(args);
    case unix_wg_transform_drain: return pw_transform_drain(args);
    case unix_wg_transform_flush: return pw_transform_flush(args);
    case unix_wg_transform_notify_qos: return pw_transform_notify_qos(args);
    default:
        FIXME("Call %u isn't implemented without GStreamer.\n", code);
        return STATUS_NOT_IMPLEMENTED;
    }
}
