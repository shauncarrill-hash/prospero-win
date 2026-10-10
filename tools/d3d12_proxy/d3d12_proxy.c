/* d3d12.dll for prospero-win: sits in front of vkd3d-proton (renamed
 * d3d12_original.dll, with its d3d12core.dll) beside a Direct3D 12 game.
 *
 * - Windows' ordinals 100-117 (d3d12.def): games import D3D12CreateDevice
 *   by ordinal 101, and Wine aborts on an ordinal a DLL lacks.
 * - The PS5's vkd3d-proton device tops out at feature level 11_1, so a
 *   request above that is made at 11_0 (Hades II asks for 12_1 and runs).
 *   PW_D3D12_FEATURE_LEVEL (hex, e.g. b100) picks another; 0 passes requests on.
 * - Wine reports about 546 MB of RAM on the console, and games refuse to
 *   start below their minimum. PW_FAKE_RAM_GB (the sender sets 16) raises
 *   what GlobalMemoryStatus(Ex) and GetPhysicallyInstalledSystemMemory
 *   return to every module but Wine's own, by patching import tables.
 *   It adds no real memory. Unset or 0: off.
 *
 * Writes d3d12-proxy.log beside itself and the same lines to stdout. */
#include <windows.h>
#include <psapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef HRESULT (WINAPI *create_device_fn)(IUnknown *, int, REFIID, void **);
typedef HRESULT (WINAPI *two_fn)(const void *, void *);
typedef HRESULT (WINAPI *three_fn)(const void *, const void *, void *);
typedef HRESULT (WINAPI *four_fn)(const void *, const void *, const void *, void *);

static HMODULE self, original;
static char folder[MAX_PATH];
static unsigned feature_level = 0xb000;
static ULONGLONG fake_ram;
static CRITICAL_SECTION lock;

static void say(const char *format, ...)
{
    char line[512], path[MAX_PATH + 32];
    va_list args;
    FILE *file;
    va_start(args, format);
    _vsnprintf(line, sizeof(line) - 2, format, args);
    va_end(args);
    line[sizeof(line) - 2] = 0;
    strcat(line, "\n");
    fputs(line, stdout);
    fflush(stdout);
    snprintf(path, sizeof(path), "%sd3d12-proxy.log", folder);
    if ((file = fopen(path, "a"))) {
        fputs(line, file);
        fclose(file);
    }
}

static FARPROC real(const char *name)
{
    return original ? GetProcAddress(original, name) : NULL;
}

/* --- the RAM floor ------------------------------------------------------ */
static BOOL (WINAPI *real_status_ex)(MEMORYSTATUSEX *);
static void (WINAPI *real_status)(MEMORYSTATUS *);
static BOOL (WINAPI *real_installed)(ULONGLONG *);
static HMODULE (WINAPI *real_load_a)(LPCSTR);
static HMODULE (WINAPI *real_load_w)(LPCWSTR);
static HMODULE (WINAPI *real_load_ex_a)(LPCSTR, HANDLE, DWORD);
static HMODULE (WINAPI *real_load_ex_w)(LPCWSTR, HANDLE, DWORD);
static int told;

static BOOL WINAPI fake_status_ex(MEMORYSTATUSEX *status)
{
    BOOL ok = real_status_ex(status);
    if (ok && status) {
        ULONGLONG avail = fake_ram / 4 * 3;
        if (status->ullTotalPhys < fake_ram) status->ullTotalPhys = fake_ram;
        if (status->ullAvailPhys < avail) status->ullAvailPhys = avail;
        if (status->ullTotalPageFile < fake_ram) status->ullTotalPageFile = fake_ram;
        if (status->ullAvailPageFile < avail) status->ullAvailPageFile = avail;
        if (status->dwMemoryLoad > 40) status->dwMemoryLoad = 25;
        if (told++ < 4)
            say("D3D12_PROXY ram seen total=%I64u avail=%I64u", status->ullTotalPhys, status->ullAvailPhys);
    }
    return ok;
}

static void WINAPI fake_status(MEMORYSTATUS *status)
{
    real_status(status);
    if (status) {
        if (status->dwTotalPhys < 0xffffffffu) status->dwTotalPhys = 0xffffffffu;
        if (status->dwAvailPhys < 0xc0000000u) status->dwAvailPhys = 0xc0000000u;
        if (status->dwMemoryLoad > 40) status->dwMemoryLoad = 25;
    }
}

static BOOL WINAPI fake_installed(ULONGLONG *kb)
{
    BOOL ok = real_installed ? real_installed(kb) : FALSE;
    if (kb && (!ok || *kb < fake_ram / 1024)) {
        *kb = fake_ram / 1024;
        ok = TRUE;
    }
    return ok;
}

static void patch_all(void);

static HMODULE WINAPI fake_load_a(LPCSTR name) { HMODULE m = real_load_a(name); patch_all(); return m; }
static HMODULE WINAPI fake_load_w(LPCWSTR name) { HMODULE m = real_load_w(name); patch_all(); return m; }
static HMODULE WINAPI fake_load_ex_a(LPCSTR name, HANDLE file, DWORD flags)
{ HMODULE m = real_load_ex_a(name, file, flags); patch_all(); return m; }
static HMODULE WINAPI fake_load_ex_w(LPCWSTR name, HANDLE file, DWORD flags)
{ HMODULE m = real_load_ex_w(name, file, flags); patch_all(); return m; }

static const struct { const char *name; void *hook; } hooks[] = {
    {"GlobalMemoryStatusEx", fake_status_ex},
    {"GlobalMemoryStatus", fake_status},
    {"GetPhysicallyInstalledSystemMemory", fake_installed},
    {"LoadLibraryA", fake_load_a},
    {"LoadLibraryW", fake_load_w},
    {"LoadLibraryExA", fake_load_ex_a},
    {"LoadLibraryExW", fake_load_ex_w},
};

static int wine_own(HMODULE module)
{
    char path[MAX_PATH], *base;
    if (!GetModuleFileNameA(module, path, sizeof(path))) return 1;
    base = strrchr(path, '\\');
    base = base ? base + 1 : path;
    return !_stricmp(base, "ntdll.dll") || !_stricmp(base, "kernel32.dll") || !_stricmp(base, "kernelbase.dll");
}

static int patch_module(HMODULE module)
{
    BYTE *base = (BYTE *)module;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt;
    IMAGE_DATA_DIRECTORY *dir;
    IMAGE_IMPORT_DESCRIPTOR *desc;
    int slots = 0;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return 0;
    dir = &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir->VirtualAddress || !dir->Size) return 0;
    for (desc = (IMAGE_IMPORT_DESCRIPTOR *)(base + dir->VirtualAddress); desc->Name; desc++) {
        IMAGE_THUNK_DATA *names = (IMAGE_THUNK_DATA *)(base + (desc->OriginalFirstThunk ? desc->OriginalFirstThunk : desc->FirstThunk));
        IMAGE_THUNK_DATA *slot = (IMAGE_THUNK_DATA *)(base + desc->FirstThunk);
        if (!desc->OriginalFirstThunk) continue;     /* no names left to match */
        for (; names->u1.AddressOfData; names++, slot++) {
            IMAGE_IMPORT_BY_NAME *import;
            size_t i;
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal)) continue;
            import = (IMAGE_IMPORT_BY_NAME *)(base + names->u1.AddressOfData);
            for (i = 0; i < sizeof(hooks) / sizeof(hooks[0]); i++) {
                DWORD old;
                if (strcmp((const char *)import->Name, hooks[i].name)) continue;
                if ((void *)slot->u1.Function == hooks[i].hook) { slots++; break; }
                if (VirtualProtect(&slot->u1.Function, sizeof(void *), PAGE_READWRITE, &old)) {
                    slot->u1.Function = (ULONG_PTR)hooks[i].hook;
                    VirtualProtect(&slot->u1.Function, sizeof(void *), old, &old);
                    slots++;
                }
                break;
            }
        }
    }
    return slots;
}

static void patch_all(void)
{
    HMODULE modules[1024];
    DWORD needed = 0, i, count;
    int slots = 0;
    static int first = 1;
    if (!fake_ram) return;
    EnterCriticalSection(&lock);
    if (EnumProcessModules(GetCurrentProcess(), modules, sizeof(modules), &needed)) {
        count = needed / sizeof(HMODULE);
        if (count > 1024) count = 1024;
        for (i = 0; i < count; i++)
            if (modules[i] != self && !wine_own(modules[i])) slots += patch_module(modules[i]);
        if (first) {
            say("D3D12_PROXY ram slots=%d modules=%lu", slots, (unsigned long)count);
            first = 0;
        }
    }
    LeaveCriticalSection(&lock);
}

static void start_ram_floor(void)
{
    HMODULE kernel32 = GetModuleHandleA("kernel32.dll");
    const char *text = getenv("PW_FAKE_RAM_GB");
    MEMORYSTATUSEX status;
    if (!text || !atoi(text)) return;
    fake_ram = (ULONGLONG)atoi(text) << 30;
    real_status_ex = (void *)GetProcAddress(kernel32, "GlobalMemoryStatusEx");
    real_status = (void *)GetProcAddress(kernel32, "GlobalMemoryStatus");
    real_installed = (void *)GetProcAddress(kernel32, "GetPhysicallyInstalledSystemMemory");
    real_load_a = (void *)GetProcAddress(kernel32, "LoadLibraryA");
    real_load_w = (void *)GetProcAddress(kernel32, "LoadLibraryW");
    real_load_ex_a = (void *)GetProcAddress(kernel32, "LoadLibraryExA");
    real_load_ex_w = (void *)GetProcAddress(kernel32, "LoadLibraryExW");
    if (!real_status_ex || !real_status || !real_load_a || !real_load_w || !real_load_ex_a || !real_load_ex_w) {
        fake_ram = 0;
        return;
    }
    status.dwLength = sizeof(status);
    if (real_status_ex(&status))
        say("D3D12_PROXY ram was total=%I64u avail=%I64u, floor %I64u", status.ullTotalPhys, status.ullAvailPhys, fake_ram);
    patch_all();
}

/* --- exports ------------------------------------------------------------ */
HRESULT WINAPI D3D12CreateDevice(IUnknown *adapter, int level, REFIID iid, void **device)
{
    create_device_fn create = (create_device_fn)real("D3D12CreateDevice");
    int using = level;
    HRESULT hr;
    if (!create) {
        say("D3D12_PROXY CreateDevice missing original");
        return E_NOINTERFACE;
    }
    if (feature_level && level > 0xb100) using = feature_level;
    hr = create(adapter, using, iid, device);
    say("D3D12_PROXY CreateDevice requested=0x%x using=0x%x hr=0x%08lx device=%p",
        level, using, (unsigned long)hr, device ? *device : NULL);
    return hr;
}

HRESULT WINAPI D3D12GetDebugInterface(REFIID iid, void **out)
{
    two_fn fn = (two_fn)real("D3D12GetDebugInterface");
    return fn ? fn(iid, out) : E_NOTIMPL;
}

HRESULT WINAPI D3D12CreateRootSignatureDeserializer(const void *data, SIZE_T size, REFIID iid, void **out)
{
    HRESULT (WINAPI *fn)(const void *, SIZE_T, REFIID, void **) = (void *)real("D3D12CreateRootSignatureDeserializer");
    return fn ? fn(data, size, iid, out) : E_NOTIMPL;
}

HRESULT WINAPI D3D12CreateVersionedRootSignatureDeserializer(const void *data, SIZE_T size, REFIID iid, void **out)
{
    HRESULT (WINAPI *fn)(const void *, SIZE_T, REFIID, void **) = (void *)real("D3D12CreateVersionedRootSignatureDeserializer");
    return fn ? fn(data, size, iid, out) : E_NOTIMPL;
}

HRESULT WINAPI D3D12EnableExperimentalFeatures(UINT count, const IID *iids, void *configs, UINT *sizes)
{
    HRESULT (WINAPI *fn)(UINT, const IID *, void *, UINT *) = (void *)real("D3D12EnableExperimentalFeatures");
    return fn ? fn(count, iids, configs, sizes) : E_NOTIMPL;
}

HRESULT WINAPI D3D12GetInterface(REFCLSID clsid, REFIID iid, void **out)
{
    three_fn fn = (three_fn)real("D3D12GetInterface");
    say("D3D12_PROXY GetInterface clsid=0x%08lx", clsid ? (unsigned long)clsid->Data1 : 0ul);
    return fn ? fn(clsid, iid, out) : E_NOTIMPL;
}

HRESULT WINAPI D3D12SerializeRootSignature(const void *desc, int version, void **blob, void **error)
{
    HRESULT (WINAPI *fn)(const void *, int, void **, void **) = (void *)real("D3D12SerializeRootSignature");
    return fn ? fn(desc, version, blob, error) : E_NOTIMPL;
}

HRESULT WINAPI D3D12SerializeVersionedRootSignature(const void *desc, void **blob, void **error)
{
    three_fn fn = (three_fn)real("D3D12SerializeVersionedRootSignature");
    return fn ? fn(desc, blob, error) : E_NOTIMPL;
}

/* Windows-only exports vkd3d-proton lacks: real functions, not Wine's abort */
HRESULT WINAPI GetBehaviorValue(void) { return 0; }
HRESULT WINAPI SetAppCompatStringPointer(void) { return 0; }
HRESULT WINAPI D3D12CoreCreateLayeredDevice(void) { return E_NOTIMPL; }
HRESULT WINAPI D3D12CoreGetLayeredDeviceSize(void) { return E_NOTIMPL; }
HRESULT WINAPI D3D12CoreRegisterLayers(void) { return E_NOTIMPL; }
HRESULT WINAPI D3D12DeviceRemovedExtendedData(void) { return E_NOTIMPL; }
HRESULT WINAPI D3D12PIXEventsReplaceBlock(void) { return E_NOTIMPL; }
HRESULT WINAPI D3D12PIXGetThreadInfo(void) { return E_NOTIMPL; }
HRESULT WINAPI D3D12PIXNotifyWakeFromFenceSignal(void) { return E_NOTIMPL; }
HRESULT WINAPI D3D12PIXReportCounter(void) { return E_NOTIMPL; }

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, void *reserved)
{
    char path[MAX_PATH + 32], *slash;
    const char *level;
    (void)reserved;
    if (reason != DLL_PROCESS_ATTACH) return TRUE;
    self = instance;
    DisableThreadLibraryCalls(instance);
    InitializeCriticalSection(&lock);
    GetModuleFileNameA(instance, folder, MAX_PATH);
    slash = strrchr(folder, '\\');
    if (slash) slash[1] = 0; else folder[0] = 0;
    if ((level = getenv("PW_D3D12_FEATURE_LEVEL"))) feature_level = (unsigned)strtoul(level, NULL, 16);
    say("D3D12_PROXY prospero-win 1.0 feature_level=0x%x", feature_level);
    start_ram_floor();
    /* the core first, by full path, so vkd3d's shim finds it already loaded */
    snprintf(path, sizeof(path), "%sd3d12core.dll", folder);
    if (!LoadLibraryA(path)) say("D3D12_PROXY d3d12core.dll did not load (%lu)", GetLastError());
    snprintf(path, sizeof(path), "%sd3d12_original.dll", folder);
    if (!(original = LoadLibraryA(path))) say("D3D12_PROXY d3d12_original.dll did not load (%lu)", GetLastError());
    return TRUE;
}
