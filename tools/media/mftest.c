/* SPDX-License-Identifier: LGPL-2.1-or-later */
/* Media Foundation decoding checks for the prefix's winegstreamer.
 *   mftest h264 FILE     raw H.264 through the H.264 decoder MFT, NV12 out
 *   mftest aac FILE      ADTS AAC through the AAC decoder MFT, PCM out
 *   mftest reader FILE   a file through IMFSourceReader, RGB32 and PCM out
 * Prints counts and checks; exit 0 when frames came out. */
#define COBJMACROS
#include <windows.h>
#include <initguid.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mftransform.h>
#include <mferror.h>
#include <wmcodecdsp.h>
#include <stdio.h>

static BYTE *load(const char *path, DWORD *size)
{
    FILE *f = fopen(path, "rb");
    BYTE *data;
    if (!f) { printf("can't open %s\n", path); exit(2); }
    fseek(f, 0, SEEK_END); *size = ftell(f); fseek(f, 0, SEEK_SET);
    data = malloc(*size);
    fread(data, 1, *size, f);
    fclose(f);
    return data;
}

static IMFSample *make_sample(const BYTE *data, DWORD size, LONGLONG pts)
{
    IMFMediaBuffer *buffer;
    IMFSample *sample;
    BYTE *p;
    MFCreateMemoryBuffer(size, &buffer);
    IMFMediaBuffer_Lock(buffer, &p, NULL, NULL);
    memcpy(p, data, size);
    IMFMediaBuffer_Unlock(buffer);
    IMFMediaBuffer_SetCurrentLength(buffer, size);
    MFCreateSample(&sample);
    IMFSample_AddBuffer(sample, buffer);
    IMFMediaBuffer_Release(buffer);
    if (pts >= 0) IMFSample_SetSampleTime(sample, pts);
    return sample;
}

static int frames, first_checked;
static double first_mean;

/* Pull everything out; returns the last hr. */
static HRESULT pull(IMFTransform *mft, BOOL video)
{
    MFT_OUTPUT_STREAM_INFO info;
    HRESULT hr;
    for (;;)
    {
        MFT_OUTPUT_DATA_BUFFER out = {0};
        IMFMediaBuffer *buffer;
        DWORD status, len;
        BYTE *p;

        IMFTransform_GetOutputStreamInfo(mft, 0, &info);
        if (!(info.dwFlags & MFT_OUTPUT_STREAM_PROVIDES_SAMPLES))
        {
            IMFSample *sample;
            MFCreateSample(&sample);
            MFCreateMemoryBuffer(info.cbSize ? info.cbSize : 1 << 20, &buffer);
            IMFSample_AddBuffer(sample, buffer);
            IMFMediaBuffer_Release(buffer);
            out.pSample = sample;
        }
        hr = IMFTransform_ProcessOutput(mft, 0, 1, &out, &status);
        if (hr == MF_E_TRANSFORM_STREAM_CHANGE)
        {
            IMFMediaType *type;
            UINT64 size = 0;
            if (out.pSample) IMFSample_Release(out.pSample);
            IMFTransform_GetOutputAvailableType(mft, 0, 0, &type);
            IMFMediaType_GetUINT64(type, &MF_MT_FRAME_SIZE, &size);
            printf("stream change: %ux%u\n", (UINT)(size >> 32), (UINT)size);
            hr = IMFTransform_SetOutputType(mft, 0, type, 0);
            if (FAILED(hr)) printf("SetOutputType after change: %#lx\n", hr);
            IMFMediaType_Release(type);
            continue;
        }
        if (FAILED(hr))
        {
            if (out.pSample) IMFSample_Release(out.pSample);
            if (out.pEvents) IMFCollection_Release(out.pEvents);
            return hr;
        }
        frames++;
        IMFSample_ConvertToContiguousBuffer(out.pSample, &buffer);
        IMFMediaBuffer_Lock(buffer, &p, NULL, &len);
        if (!first_checked && len)
        {
            double sum = 0;
            DWORD i, n = video ? len * 2 / 3 : len;
            for (i = 0; i < n; ++i) sum += video ? p[i] : abs(((short *)p)[i / 2 * 2 / 2]);
            first_mean = sum / n;
            first_checked = 1;
            printf("first output: %lu bytes, mean %.1f\n", len, first_mean);
        }
        IMFMediaBuffer_Unlock(buffer);
        IMFMediaBuffer_Release(buffer);
        IMFSample_Release(out.pSample);
    }
}

static IMFTransform *create(REFCLSID clsid)
{
    IMFTransform *mft;
    HRESULT hr = CoCreateInstance(clsid, NULL, CLSCTX_INPROC_SERVER, &IID_IMFTransform, (void **)&mft);
    if (FAILED(hr)) { printf("CoCreateInstance: %#lx\n", hr); exit(1); }
    return mft;
}

static int test_h264(const char *path)
{
    IMFTransform *mft = create(&CLSID_CMSH264DecoderMFT);
    IMFMediaType *type;
    DWORD size, off, i;
    BYTE *data = load(path, &size);
    HRESULT hr;

    MFCreateMediaType(&type);
    IMFMediaType_SetGUID(type, &MF_MT_MAJOR_TYPE, &MFMediaType_Video);
    IMFMediaType_SetGUID(type, &MF_MT_SUBTYPE, &MFVideoFormat_H264);
    hr = IMFTransform_SetInputType(mft, 0, type, 0);
    printf("SetInputType: %#lx\n", hr);
    IMFMediaType_Release(type);
    for (i = 0; SUCCEEDED(IMFTransform_GetOutputAvailableType(mft, 0, i, &type)); ++i)
    {
        GUID subtype;
        IMFMediaType_GetGUID(type, &MF_MT_SUBTYPE, &subtype);
        if (IsEqualGUID(&subtype, &MFVideoFormat_NV12))
        {
            hr = IMFTransform_SetOutputType(mft, 0, type, 0);
            printf("SetOutputType NV12: %#lx\n", hr);
            IMFMediaType_Release(type);
            break;
        }
        IMFMediaType_Release(type);
    }
    IMFTransform_ProcessMessage(mft, MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
    for (off = 0; off < size; )
    {
        DWORD chunk = min(1000, size - off);
        IMFSample *sample = make_sample(data + off, chunk, -1);
        hr = IMFTransform_ProcessInput(mft, 0, sample, 0);
        if (hr == MF_E_NOTACCEPTING)
        {
            IMFSample_Release(sample);
            pull(mft, TRUE);
            continue;
        }
        IMFSample_Release(sample);
        if (FAILED(hr)) { printf("ProcessInput: %#lx\n", hr); return 1; }
        off += chunk;
        pull(mft, TRUE);
    }
    IMFTransform_ProcessMessage(mft, MFT_MESSAGE_COMMAND_DRAIN, 0);
    hr = pull(mft, TRUE);
    printf("h264: %d frames, last hr %#lx\n", frames, hr);
    IMFTransform_Release(mft);
    return frames ? 0 : 1;
}

static int test_aac(const char *path)
{
    IMFTransform *mft = create(&CLSID_CMSAACDecMFT);
    IMFMediaType *type;
    DWORD size, off, i;
    BYTE *data = load(path, &size);
    HRESULT hr;

    MFCreateMediaType(&type);
    IMFMediaType_SetGUID(type, &MF_MT_MAJOR_TYPE, &MFMediaType_Audio);
    IMFMediaType_SetGUID(type, &MF_MT_SUBTYPE, &MFAudioFormat_AAC);
    IMFMediaType_SetUINT32(type, &MF_MT_AAC_PAYLOAD_TYPE, 1);   /* ADTS */
    IMFMediaType_SetUINT32(type, &MF_MT_AUDIO_SAMPLES_PER_SECOND, 44100);
    IMFMediaType_SetUINT32(type, &MF_MT_AUDIO_NUM_CHANNELS, 1);
    IMFMediaType_SetUINT32(type, &MF_MT_AUDIO_BITS_PER_SAMPLE, 16);
    {
        /* HEAACWAVEINFO after WAVEFORMATEX (payload ADTS), then AudioSpecificConfig: LC, 44.1 kHz, mono */
        static const BYTE user[] = {1,0, 0xfe,0, 0,0, 0,0,0,0, 0,0, 0x12,0x08};
        IMFMediaType_SetBlob(type, &MF_MT_USER_DATA, user, sizeof(user));
    }
    hr = IMFTransform_SetInputType(mft, 0, type, 0);
    printf("SetInputType: %#lx\n", hr);
    IMFMediaType_Release(type);
    for (i = 0; SUCCEEDED(IMFTransform_GetOutputAvailableType(mft, 0, i, &type)); ++i)
    {
        GUID subtype;
        IMFMediaType_GetGUID(type, &MF_MT_SUBTYPE, &subtype);
        if (IsEqualGUID(&subtype, &MFAudioFormat_PCM))
        {
            hr = IMFTransform_SetOutputType(mft, 0, type, 0);
            printf("SetOutputType PCM: %#lx\n", hr);
            IMFMediaType_Release(type);
            break;
        }
        IMFMediaType_Release(type);
    }
    for (off = 0; off < size; )
    {
        DWORD chunk = min(700, size - off);
        IMFSample *sample = make_sample(data + off, chunk, -1);
        hr = IMFTransform_ProcessInput(mft, 0, sample, 0);
        IMFSample_Release(sample);
        if (hr == MF_E_NOTACCEPTING) { pull(mft, FALSE); continue; }
        if (FAILED(hr)) { printf("ProcessInput: %#lx\n", hr); return 1; }
        off += chunk;
        pull(mft, FALSE);
    }
    IMFTransform_ProcessMessage(mft, MFT_MESSAGE_COMMAND_DRAIN, 0);
    hr = pull(mft, FALSE);
    printf("aac: %d outputs, last hr %#lx\n", frames, hr);
    IMFTransform_Release(mft);
    return frames ? 0 : 1;
}

static int test_reader(const char *path)
{
    WCHAR wpath[MAX_PATH];
    IMFSourceReader *reader;
    IMFMediaType *type;
    IMFAttributes *attrs;
    int video = 0, audio = 0;
    HRESULT hr;

    MultiByteToWideChar(CP_ACP, 0, path, -1, wpath, MAX_PATH);
    MFCreateAttributes(&attrs, 1);
    IMFAttributes_SetUINT32(attrs, &MF_SOURCE_READER_ENABLE_VIDEO_PROCESSING, TRUE);
    hr = MFCreateSourceReaderFromURL(wpath, attrs, &reader);
    printf("MFCreateSourceReaderFromURL: %#lx\n", hr);
    if (FAILED(hr)) return 1;

    MFCreateMediaType(&type);
    IMFMediaType_SetGUID(type, &MF_MT_MAJOR_TYPE, &MFMediaType_Video);
    IMFMediaType_SetGUID(type, &MF_MT_SUBTYPE, &MFVideoFormat_RGB32);
    hr = IMFSourceReader_SetCurrentMediaType(reader, MF_SOURCE_READER_FIRST_VIDEO_STREAM, NULL, type);
    printf("video RGB32: %#lx\n", hr);
    IMFMediaType_Release(type);
    MFCreateMediaType(&type);
    IMFMediaType_SetGUID(type, &MF_MT_MAJOR_TYPE, &MFMediaType_Audio);
    IMFMediaType_SetGUID(type, &MF_MT_SUBTYPE, &MFAudioFormat_PCM);
    hr = IMFSourceReader_SetCurrentMediaType(reader, MF_SOURCE_READER_FIRST_AUDIO_STREAM, NULL, type);
    printf("audio PCM: %#lx\n", hr);
    IMFMediaType_Release(type);

    for (;;)
    {
        DWORD index, flags;
        LONGLONG ts;
        IMFSample *sample = NULL;
        hr = IMFSourceReader_ReadSample(reader, MF_SOURCE_READER_ANY_STREAM, 0, &index, &flags, &ts, &sample);
        if (FAILED(hr)) { printf("ReadSample: %#lx\n", hr); break; }
        if (sample)
        {
            IMFMediaType *current;
            GUID major;
            IMFSourceReader_GetCurrentMediaType(reader, index, &current);
            IMFMediaType_GetGUID(current, &MF_MT_MAJOR_TYPE, &major);
            if (IsEqualGUID(&major, &MFMediaType_Video))
            {
                if (video++ == 10)
                {
                    /* the 11th frame as a bitmap, to look at */
                    UINT64 size = 0;
                    IMFMediaBuffer *buffer;
                    BYTE *p;
                    DWORD len;
                    IMFMediaType_GetUINT64(current, &MF_MT_FRAME_SIZE, &size);
                    IMFSample_ConvertToContiguousBuffer(sample, &buffer);
                    IMFMediaBuffer_Lock(buffer, &p, NULL, &len);
                    {
                        UINT w = size >> 32, h = (UINT)size;
                        BITMAPFILEHEADER fh = {0x4d42, sizeof(fh) + sizeof(BITMAPINFOHEADER) + len, 0, 0,
                                               sizeof(fh) + sizeof(BITMAPINFOHEADER)};
                        BITMAPINFOHEADER ih = {sizeof(ih), w, -(LONG)h, 1, 32};
                        FILE *f = fopen("frame.bmp", "wb");
                        fwrite(&fh, sizeof(fh), 1, f); fwrite(&ih, sizeof(ih), 1, f); fwrite(p, 1, len, f);
                        fclose(f);
                        printf("frame 10: %ux%u, %lu bytes -> frame.bmp\n", w, h, len);
                    }
                    IMFMediaBuffer_Unlock(buffer);
                    IMFMediaBuffer_Release(buffer);
                }
            }
            else audio++;
            IMFMediaType_Release(current);
            IMFSample_Release(sample);
        }
        if (flags & MF_SOURCE_READERF_ENDOFSTREAM)
        {
            /* one stream ended; keep reading the other until all do */
            if (FAILED(IMFSourceReader_SetStreamSelection(reader, index, FALSE))) break;
            {
                BOOL any = FALSE, sel;
                DWORD s;
                for (s = 0; SUCCEEDED(IMFSourceReader_GetStreamSelection(reader, s, &sel)); ++s) any |= sel;
                if (!any) break;
            }
        }
        if (flags & MF_SOURCE_READERF_ERROR) { printf("stream error\n"); break; }
    }
    printf("reader: %d video frames, %d audio samples\n", video, audio);
    IMFSourceReader_Release(reader);
    return video || audio ? 0 : 1;
}

int main(int argc, char **argv)
{
    int ret;
    if (argc < 3) { printf("usage: mftest h264|aac|reader FILE\n"); return 2; }
    CoInitializeEx(NULL, COINIT_MULTITHREADED);
    MFStartup(MF_VERSION, 0);
    if (!strcmp(argv[1], "h264")) ret = test_h264(argv[2]);
    else if (!strcmp(argv[1], "aac")) ret = test_aac(argv[2]);
    else ret = test_reader(argv[2]);
    MFShutdown();
    printf("%s\n", ret ? "FAIL" : "PASS");
    return ret;
}
