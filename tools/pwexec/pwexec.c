/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * pwexec: runs a game's launcher, and the program the launcher starts, one
 * after the other in this one process.
 *
 * The PS5 app can't create a second process (NtCreateUserProcess fails
 * with error 50), so a launcher that checks the CD and then starts the game
 * (Red Alert 2's RA2.exe starting Game.exe) never gets anywhere. The sender
 * makes this program the profile's executable, beside the launcher, with
 * the launcher's name as its first argument:
 *
 *   pwexec.exe RA2.exe [the launcher's arguments]
 *
 * It maps the launcher with LoadLibraryEx, resolves its imports itself
 * (Wine's loader leaves an .exe's imports alone), makes it the process's
 * main module (PEB ImageBaseAddress, command line, image path) and calls
 * its entry point. CreateProcessA/W and WinExec are hooked in every module's
 * import table, so a launcher's start of another program, by any route
 * (ShellExecute goes through shell32's CreateProcessW), lands here. The
 * hook doesn't return: it closes the launcher's windows, unmaps it, and
 * starts the new program the same way, in its place, with the command line
 * and folder the launcher gave. What the launcher left behind, named
 * mutexes and events, file mappings, environment, registry, stays alive,
 * since it is still the same process, so the game finds it.
 *
 * Not handled: a launcher that keeps running beside the game, or that
 * waits on the game to talk to it.
 *
 * Log: PWEXEC lines on stdout (the session log) and pwexec.log beside it.
 */
#include <windows.h>
#include <winternl.h>
#define PSAPI_VERSION 2
#include <psapi.h>

#ifdef _WIN64
typedef IMAGE_NT_HEADERS64 NT_HEADERS;
typedef IMAGE_THUNK_DATA64 THUNK;
#define ORDINAL_FLAG IMAGE_ORDINAL_FLAG64
#else
typedef IMAGE_NT_HEADERS32 NT_HEADERS;
typedef IMAGE_THUNK_DATA32 THUNK;
#define ORDINAL_FLAG IMAGE_ORDINAL_FLAG32
#endif

/* --- tiny libc ------------------------------------------------------------ */
void *memset(void *d, int c, size_t n) { unsigned char *p = d; while (n--) *p++ = (unsigned char)c; return d; }
void *memcpy(void *d, const void *s, size_t n) { unsigned char *p = d; const unsigned char *q = s; while (n--) *p++ = *q++; return d; }
static size_t wlen(const WCHAR *s) { size_t n = 0; while (s[n]) n++; return n; }
static size_t alen(const char *s) { size_t n = 0; while (s[n]) n++; return n; }
static int aieq(const char *a, const char *b)
{
    for (;; a++, b++) {
        char x = *a, y = *b;
        if (x >= 'A' && x <= 'Z') x += 32;
        if (y >= 'A' && y <= 'Z') y += 32;
        if (x != y) return 0;
        if (!x) return 1;
    }
}
static int starts(const char *text, const char *prefix)
{
    for (; *prefix; text++, prefix++) {
        char x = *text;
        if (x >= 'A' && x <= 'Z') x += 32;
        if (x != *prefix) return 0;
    }
    return 1;
}
static int aeq(const char *a, const char *b) { while (*a && *a == *b) a++, b++; return *a == *b; }
static void *grab(size_t bytes) { return HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, bytes); }

static HANDLE log_file = INVALID_HANDLE_VALUE;
static void say(const char *text)
{
    DWORD done;
    HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
    if (out && out != INVALID_HANDLE_VALUE) { WriteFile(out, "PWEXEC ", 7, &done, NULL); WriteFile(out, text, alen(text), &done, NULL); WriteFile(out, "\n", 1, &done, NULL); }
    if (log_file != INVALID_HANDLE_VALUE) { WriteFile(log_file, text, alen(text), &done, NULL); WriteFile(log_file, "\r\n", 2, &done, NULL); }
}
static void sayw(const char *what, const WCHAR *text)
{
    char line[1024];
    size_t i = 0, j = 0;
    while (what[j] && i < 500) line[i++] = what[j++];
    for (j = 0; text && text[j] && i < sizeof(line) - 1; j++) line[i++] = text[j] < 0x80 ? (char)text[j] : '?';
    line[i] = 0;
    say(line);
}
static WCHAR *widen(const char *text)
{
    int n;
    WCHAR *out;
    if (!text) return NULL;
    n = MultiByteToWideChar(CP_ACP, 0, text, -1, NULL, 0);
    out = grab((n + 1) * sizeof(WCHAR));
    MultiByteToWideChar(CP_ACP, 0, text, -1, out, n);
    return out;
}
static char *narrow(const WCHAR *text)
{
    int n = WideCharToMultiByte(CP_ACP, 0, text, -1, NULL, 0, NULL, NULL);
    char *out = grab(n + 1);
    WideCharToMultiByte(CP_ACP, 0, text, -1, out, n, NULL, NULL);
    return out;
}

/* --- the command line the program sees ----------------------------------- */
static WCHAR *line_w;
static char *line_a;
static int arg_count;
static char **arg_a;
static WCHAR **arg_w;

/* argv by the C runtime's rules (quotes, backslashes before quotes) */
static int split(const WCHAR *line, WCHAR **out)
{
    int count = 0;
    const WCHAR *p = line;
    WCHAR *store = out ? (WCHAR *)(out + 64) : NULL;
    while (*p) {
        int quoted = 0;
        while (*p == ' ' || *p == '\t') p++;
        if (!*p) break;
        if (out && count < 63) out[count] = store;
        for (; *p && (quoted || (*p != ' ' && *p != '\t')); p++) {
            int slashes = 0;
            while (*p == '\\') { slashes++; p++; }
            if (*p == '"') {
                int k;
                for (k = 0; k < slashes / 2; k++) if (store) *store++ = '\\';
                if (slashes % 2) { if (store) *store++ = '"'; }
                else quoted = !quoted;
            } else {
                int k;
                for (k = 0; k < slashes; k++) if (store) *store++ = '\\';
                if (!*p) break;
                if (store) *store++ = *p;
            }
            if (!*p) break;
        }
        if (store) *store++ = 0;
        count++;
        if (count >= 63) break;
    }
    if (out) out[count < 63 ? count : 63] = NULL;
    return count;
}

/* Every pointer in kernelbase's (and kernel32's) writable data that holds
 * old is set to new: GetCommandLineA/W return such cached pointers. */
static void swap_pointer(void *old, void *new)
{
    static const char *names[] = { "kernelbase.dll", "kernel32.dll" };
    unsigned m;
    for (m = 0; m < 2; m++) {
        BYTE *base = (BYTE *)GetModuleHandleA(names[m]);
        NT_HEADERS *nt;
        IMAGE_SECTION_HEADER *section;
        unsigned s;
        if (!base) continue;
        nt = (NT_HEADERS *)(base + ((IMAGE_DOS_HEADER *)base)->e_lfanew);
        section = IMAGE_FIRST_SECTION(nt);
        for (s = 0; s < nt->FileHeader.NumberOfSections; s++, section++) {
            void **p, **end;
            if (!(section->Characteristics & IMAGE_SCN_MEM_WRITE)) continue;
            p = (void **)(base + section->VirtualAddress);
            end = (void **)((BYTE *)p + (section->Misc.VirtualSize & ~(sizeof(void *) - 1)));
            for (; p < end; p++) if (*p == old) *p = new;
        }
    }
}

static void set_command_line(const WCHAR *exe, const WCHAR *args)
{
    RTL_USER_PROCESS_PARAMETERS *params = NtCurrentTeb()->ProcessEnvironmentBlock->ProcessParameters;
    size_t n = wlen(exe) + (args ? wlen(args) : 0) + 4;
    WCHAR *w = grab(n * sizeof(WCHAR)), *p = w, *old_w = GetCommandLineW();
    char *old_a = GetCommandLineA();
    const WCHAR *q;
    *p++ = '"';
    for (q = exe; *q; q++) *p++ = *q;
    *p++ = '"';
    if (args && *args) { *p++ = ' '; for (q = args; *q; q++) *p++ = *q; }
    *p = 0;
    line_w = w;
    line_a = narrow(w);
    swap_pointer(old_w, line_w);
    swap_pointer(old_a, line_a);
    params->CommandLine.Buffer = line_w;
    params->CommandLine.Length = (USHORT)(wlen(line_w) * sizeof(WCHAR));
    params->CommandLine.MaximumLength = params->CommandLine.Length + sizeof(WCHAR);
    {
        WCHAR *path = grab((wlen(exe) + 1) * sizeof(WCHAR));
        memcpy(path, exe, (wlen(exe) + 1) * sizeof(WCHAR));
        ((UNICODE_STRING *)&params->ImagePathName)->Buffer = path;
        ((UNICODE_STRING *)&params->ImagePathName)->Length = (USHORT)(wlen(path) * sizeof(WCHAR));
        ((UNICODE_STRING *)&params->ImagePathName)->MaximumLength = (USHORT)((wlen(path) + 1) * sizeof(WCHAR));
    }
    arg_w = grab(64 * sizeof(WCHAR *) + (wlen(line_w) + 64) * sizeof(WCHAR));
    arg_count = split(line_w, arg_w);
    arg_a = grab(64 * sizeof(char *));
    {
        int i;
        for (i = 0; i < arg_count; i++) arg_a[i] = narrow(arg_w[i]);
    }
    sayw("command line: ", line_w);
}

/* msvcrt computed argv when it loaded; the program's imports of it get these */
static void CDECL my_getmainargs(int *argc, char ***argv, char ***envp, int expand, void *info)
{
    (void)expand; (void)info;
    *argc = arg_count; *argv = arg_a;
    if (envp) { static char *none[1]; *envp = none; }
}
static void CDECL my_wgetmainargs(int *argc, WCHAR ***argv, WCHAR ***envp, int expand, void *info)
{
    (void)expand; (void)info;
    *argc = arg_count; *argv = arg_w;
    if (envp) { static WCHAR *none[1]; *envp = none; }
}
static int *CDECL my_p_argc(void) { return &arg_count; }
static char ***CDECL my_p_argv(void) { return &arg_a; }
static WCHAR ***CDECL my_p_wargv(void) { return &arg_w; }
static char **CDECL my_p_acmdln(void) { return &line_a; }
static WCHAR **CDECL my_p_wcmdln(void) { return &line_w; }

/* --- hooks ---------------------------------------------------------------- */
static void start_program(const WCHAR *exe, const WCHAR *args, const WCHAR *folder);

static BOOL WINAPI my_create_process_w(const WCHAR *app, WCHAR *cmd, SECURITY_ATTRIBUTES *pa, SECURITY_ATTRIBUTES *ta,
                                       BOOL inherit, DWORD flags, void *env, const WCHAR *folder,
                                       STARTUPINFOW *si, PROCESS_INFORMATION *pi);
static BOOL WINAPI my_create_process_a(const char *app, char *cmd, SECURITY_ATTRIBUTES *pa, SECURITY_ATTRIBUTES *ta,
                                       BOOL inherit, DWORD flags, void *env, const char *folder,
                                       STARTUPINFOA *si, PROCESS_INFORMATION *pi)
{
    (void)si;
    return my_create_process_w(widen(app), widen(cmd), pa, ta, inherit, flags & ~CREATE_UNICODE_ENVIRONMENT,
                               env, widen(folder), NULL, pi);
}
static UINT WINAPI my_winexec(const char *cmd, UINT show)
{
    (void)show;
    return my_create_process_w(NULL, widen(cmd), NULL, NULL, FALSE, 0, NULL, NULL, NULL, NULL) ? 33 : ERROR_FILE_NOT_FOUND;
}
static HMODULE WINAPI my_load_a(const char *name);
static HMODULE WINAPI my_load_w(const WCHAR *name);
static HMODULE WINAPI my_load_ex_a(const char *name, HANDLE file, DWORD flags);
static HMODULE WINAPI my_load_ex_w(const WCHAR *name, HANDLE file, DWORD flags);

/* GetModuleFileName(NULL) and (game base): a manually mapped image has no
 * entry in Wine's module index, so these return the path we kept. Any other
 * module forwards to the real one. */
static WCHAR *game_path_w;
static char *game_path_a;
static HMODULE game_base;
static DWORD WINAPI my_module_file_w(HMODULE m, WCHAR *out, DWORD size)
{
    DWORD n;
    if (m && m != game_base) return GetModuleFileNameW(m, out, size);
    n = game_path_w ? (DWORD)wlen(game_path_w) : 0;
    if (n >= size) n = size ? size - 1 : 0;
    memcpy(out, game_path_w, n * sizeof(WCHAR));
    if (size) out[n] = 0;
    SetLastError(n && n == size - 1 ? ERROR_INSUFFICIENT_BUFFER : 0);
    return n;
}
static DWORD WINAPI my_module_file_a(HMODULE m, char *out, DWORD size)
{
    DWORD n;
    if (m && m != game_base) return GetModuleFileNameA(m, out, size);
    n = game_path_a ? (DWORD)alen(game_path_a) : 0;
    if (n >= size) n = size ? size - 1 : 0;
    memcpy(out, game_path_a, n);
    if (size) out[n] = 0;
    SetLastError(n && n == size - 1 ? ERROR_INSUFFICIENT_BUFFER : 0);
    return n;
}

static const struct { const char *name; void *hook; } hooks[] = {
    { "CreateProcessA", my_create_process_a },
    { "CreateProcessW", my_create_process_w },
    { "WinExec", my_winexec },
    { "LoadLibraryA", my_load_a },
    { "LoadLibraryW", my_load_w },
    { "LoadLibraryExA", my_load_ex_a },
    { "LoadLibraryExW", my_load_ex_w },
    { "GetModuleFileNameA", my_module_file_a },
    { "GetModuleFileNameW", my_module_file_w },
};
static const struct { const char *name; void *hook; int data; } crt_hooks[] = {
    { "__getmainargs", my_getmainargs, 0 },
    { "__wgetmainargs", my_wgetmainargs, 0 },
    { "__p___argc", my_p_argc, 0 },
    { "__p___argv", my_p_argv, 0 },
    { "__p___wargv", my_p_wargv, 0 },
    { "__p__acmdln", my_p_acmdln, 0 },
    { "__p__wcmdln", my_p_wcmdln, 0 },
    { "__argc", &arg_count, 1 },
    { "__argv", &arg_a, 1 },
    { "__wargv", &arg_w, 1 },
    { "_acmdln", &line_a, 1 },
    { "_wcmdln", &line_w, 1 },
};

extern IMAGE_DOS_HEADER __ImageBase;

static int system_module(HMODULE module)
{
    char path[MAX_PATH], *base, *p;
    if (module == (HMODULE)&__ImageBase || !GetModuleFileNameA(module, path, sizeof(path))) return 1;
    for (base = p = path; *p; p++) if (*p == '\\' || *p == '/') base = p + 1;
    return aieq(base, "ntdll.dll") || aieq(base, "kernel32.dll") || aieq(base, "kernelbase.dll");
}

static void put_slot(ULONG_PTR *slot, ULONG_PTR value)
{
    DWORD old;
    if (*slot == value) return;
    if (VirtualProtect(slot, sizeof(*slot), PAGE_READWRITE, &old)) {
        *slot = value;
        VirtualProtect(slot, sizeof(*slot), old, &old);
    }
}

static IMAGE_IMPORT_DESCRIPTOR *imports_of(BYTE *base)
{
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    NT_HEADERS *nt;
    IMAGE_DATA_DIRECTORY *dir;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return NULL;
    nt = (NT_HEADERS *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return NULL;
    dir = &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    return dir->VirtualAddress && dir->Size ? (IMAGE_IMPORT_DESCRIPTOR *)(base + dir->VirtualAddress) : NULL;
}

/* The hooks into a module whose imports the loader resolved */
static void patch_module(HMODULE module)
{
    BYTE *base = (BYTE *)module;
    IMAGE_IMPORT_DESCRIPTOR *desc = imports_of(base);
    if (!desc) return;
    for (; desc->Name; desc++) {
        THUNK *names, *slot;
        if (!desc->OriginalFirstThunk) continue;
        names = (THUNK *)(base + desc->OriginalFirstThunk);
        slot = (THUNK *)(base + desc->FirstThunk);
        for (; names->u1.AddressOfData; names++, slot++) {
            IMAGE_IMPORT_BY_NAME *import;
            unsigned i;
            if (names->u1.Ordinal & ORDINAL_FLAG) continue;
            import = (IMAGE_IMPORT_BY_NAME *)(base + names->u1.AddressOfData);
            for (i = 0; i < sizeof(hooks) / sizeof(hooks[0]); i++)
                if (aeq((const char *)import->Name, hooks[i].name))
                    put_slot((ULONG_PTR *)&slot->u1.Function, (ULONG_PTR)hooks[i].hook);
        }
    }
}

static int readable(const void *p, size_t n)
{
    MEMORY_BASIC_INFORMATION m;
    if (!VirtualQuery(p, &m, sizeof(m)) || m.State != MEM_COMMIT) return 0;
    if (m.Protect & (PAGE_NOACCESS | PAGE_GUARD)) return 0;
    return (const char *)p + n <= (const char *)m.BaseAddress + m.RegionSize;
}

static void patch_all(void)
{
    static HMODULE modules[512];
    DWORD needed = 0, i;
    if (!K32EnumProcessModules(GetCurrentProcess(), modules, sizeof(modules), &needed)) return;
    for (i = 0; i < needed / sizeof(HMODULE) && i < 512; i++)
        if (readable(modules[i], 0x1000) && !system_module(modules[i])) patch_module(modules[i]);
}

static HMODULE WINAPI my_load_a(const char *name) { HMODULE m = LoadLibraryA(name); patch_all(); return m; }
static HMODULE WINAPI my_load_w(const WCHAR *name) { HMODULE m = LoadLibraryW(name); patch_all(); return m; }
static HMODULE WINAPI my_load_ex_a(const char *name, HANDLE file, DWORD flags) { HMODULE m = LoadLibraryExA(name, file, flags); patch_all(); return m; }
static HMODULE WINAPI my_load_ex_w(const WCHAR *name, HANDLE file, DWORD flags) { HMODULE m = LoadLibraryExW(name, file, flags); patch_all(); return m; }

/* --- the program's own imports, relocations and TLS ----------------------- */
static BOOL resolve(BYTE *base)
{
    IMAGE_IMPORT_DESCRIPTOR *desc = imports_of(base);
    if (!desc) return TRUE;
    for (; desc->Name; desc++) {
        const char *dll = (const char *)(base + desc->Name);
        HMODULE lib = LoadLibraryA(dll);
        THUNK *names = (THUNK *)(base + (desc->OriginalFirstThunk ? desc->OriginalFirstThunk : desc->FirstThunk));
        THUNK *slot = (THUNK *)(base + desc->FirstThunk);
        int crt = starts(dll, "msvcr") || starts(dll, "ucrtbase") || starts(dll, "api-ms-win-crt-");
        if (!lib) { char line[300] = "missing DLL: "; memcpy(line + 13, dll, alen(dll) + 1 < 280 ? alen(dll) + 1 : 280); say(line); return FALSE; }
        for (; names->u1.AddressOfData; names++, slot++) {
            ULONG_PTR value = 0;
            if (names->u1.Ordinal & ORDINAL_FLAG)
                value = (ULONG_PTR)GetProcAddress(lib, (const char *)(ULONG_PTR)(names->u1.Ordinal & 0xffff));
            else {
                const char *name = (const char *)((IMAGE_IMPORT_BY_NAME *)(base + names->u1.AddressOfData))->Name;
                unsigned i;
                value = (ULONG_PTR)GetProcAddress(lib, name);
                for (i = 0; i < sizeof(hooks) / sizeof(hooks[0]); i++)
                    if (aeq(name, hooks[i].name)) value = (ULONG_PTR)hooks[i].hook;
                if (crt && value)
                    for (i = 0; i < sizeof(crt_hooks) / sizeof(crt_hooks[0]); i++)
                        if (aeq(name, crt_hooks[i].name)) value = (ULONG_PTR)crt_hooks[i].hook;
                if (!value) {
                    char line[300] = "missing import: ";
                    size_t n = alen(name);
                    memcpy(line + 16, name, (n < 270 ? n : 270) + 1);
                    say(line);
                }
            }
            put_slot((ULONG_PTR *)&slot->u1.Function, value);
        }
    }
    return TRUE;
}

static void relocate(BYTE *base, NT_HEADERS *nt)
{
    IMAGE_DATA_DIRECTORY *dir = &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_BASERELOC];
    ULONG_PTR delta = (ULONG_PTR)base - (ULONG_PTR)nt->OptionalHeader.ImageBase;
    BYTE *p, *end;
    if (!delta || !dir->VirtualAddress) return;
    say("relocating the program");
    p = base + dir->VirtualAddress;
    end = p + dir->Size;
    while (p < end) {
        IMAGE_BASE_RELOCATION *block = (IMAGE_BASE_RELOCATION *)p;
        WORD *entry = (WORD *)(block + 1);
        DWORD count, i;
        if (!block->SizeOfBlock) break;
        count = (block->SizeOfBlock - sizeof(*block)) / 2;
        for (i = 0; i < count; i++) {
            BYTE *at = base + block->VirtualAddress + (entry[i] & 0xfff);
            DWORD old;
            int type = entry[i] >> 12;
            if (type != IMAGE_REL_BASED_HIGHLOW && type != IMAGE_REL_BASED_DIR64) continue;
            VirtualProtect(at, 8, PAGE_READWRITE, &old);
            if (type == IMAGE_REL_BASED_HIGHLOW) *(DWORD *)at += (DWORD)delta;
            else *(ULONG64 *)at += delta;
            VirtualProtect(at, 8, old, &old);
        }
        p += block->SizeOfBlock;
    }
}

/* TEB ThreadLocalStoragePointer */
#define TLS_ARRAY() ((void ***)((BYTE *)NtCurrentTeb() + (sizeof(void *) == 8 ? 0x58 : 0x2c)))

/* Implicit TLS for this thread (Wine's loader sets it up only for DLLs) */
static void start_tls(BYTE *base, NT_HEADERS *nt)
{
    IMAGE_DATA_DIRECTORY *dir = &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_TLS];
#ifdef _WIN64
    IMAGE_TLS_DIRECTORY64 *tls;
#else
    IMAGE_TLS_DIRECTORY32 *tls;
#endif
    void **old, **array;
    BYTE *block;
    size_t size, i;
    const unsigned index = 63;
    if (!dir->VirtualAddress || !dir->Size) return;
    tls = (void *)(base + dir->VirtualAddress);
    size = (size_t)(tls->EndAddressOfRawData - tls->StartAddressOfRawData);
    block = grab(size + tls->SizeOfZeroFill + 16);
    memcpy(block, (void *)(ULONG_PTR)tls->StartAddressOfRawData, size);
    old = *TLS_ARRAY();
    array = grab(64 * sizeof(void *));
    for (i = 0; old && i < index; i++) array[i] = old[i];
    array[index] = block;
    *TLS_ARRAY() = array;
    if (tls->AddressOfIndex) { DWORD prot; VirtualProtect((void *)(ULONG_PTR)tls->AddressOfIndex, 4, PAGE_READWRITE, &prot); *(DWORD *)(ULONG_PTR)tls->AddressOfIndex = index; }
    if (tls->AddressOfCallBacks) {
        PIMAGE_TLS_CALLBACK *cb = (PIMAGE_TLS_CALLBACK *)(ULONG_PTR)tls->AddressOfCallBacks;
        for (; *cb; cb++) (*cb)(base, DLL_PROCESS_ATTACH, NULL);
    }
    say("program TLS set up for the main thread");
}

/* --- running a program in this process ------------------------------------ */
static HMODULE current;

static BOOL CALLBACK close_window(HWND window, LPARAM param)
{
    BOOL (WINAPI *destroy)(HWND) = (void *)param;
    destroy(window);
    return TRUE;
}

/* Map the image from its file ourselves, not with LoadLibraryExW: Wine (and
 * Windows) relocate and resolve a module they load, and the program must be
 * mapped once, by us, at its own preferred base (0x400000 for the old games
 * this is for; pwexec sits high so that base is free). Returns the base, or
 * NULL, and the mapped size in *image_size. */
static SIZE_T mapped_size;

static BYTE *map_image(const WCHAR *exe)
{
    HANDLE file = CreateFileW(exe, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    DWORD size, read;
    BYTE *raw, *base;
    IMAGE_DOS_HEADER *dos;
    NT_HEADERS *nt;
    IMAGE_SECTION_HEADER *section;
    unsigned s;

    if (file == INVALID_HANDLE_VALUE) { sayw("couldn't open ", exe); return NULL; }
    size = GetFileSize(file, NULL);
    raw = grab(size);
    if (!ReadFile(file, raw, size, &read, NULL) || read != size) { CloseHandle(file); say("couldn't read the program"); return NULL; }
    CloseHandle(file);
    dos = (IMAGE_DOS_HEADER *)raw;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) { say("not a program"); return NULL; }
    nt = (NT_HEADERS *)(raw + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) { say("not a program"); return NULL; }

    base = VirtualAlloc((void *)(ULONG_PTR)nt->OptionalHeader.ImageBase, nt->OptionalHeader.SizeOfImage,
                        MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
    if (!base) {
        if (nt->FileHeader.Characteristics & IMAGE_FILE_RELOCS_STRIPPED) { say("the program's fixed address is taken"); return NULL; }
        base = VirtualAlloc(NULL, nt->OptionalHeader.SizeOfImage, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
        if (!base) { say("no room for the program"); return NULL; }
    }
    memcpy(base, raw, nt->OptionalHeader.SizeOfHeaders);
    section = IMAGE_FIRST_SECTION(nt);
    for (s = 0; s < nt->FileHeader.NumberOfSections; s++, section++)
        if (section->SizeOfRawData)
            memcpy(base + section->VirtualAddress, raw + section->PointerToRawData, section->SizeOfRawData);
    HeapFree(GetProcessHeap(), 0, raw);
    mapped_size = nt->OptionalHeader.SizeOfImage;
    return base;
}

/* Make the process's main module look like the program we mapped, so
 * GetModuleHandle(NULL), GetModuleFileName(NULL) and the like answer for it:
 * the first loader entry (pwexec's own) is repointed at the new base, name
 * and entry. */
/* The loader entry's own layout (mingw's winternl.h hides these fields). */
typedef struct {
    LIST_ENTRY InLoadOrderLinks;
    LIST_ENTRY InMemoryOrderLinks;
    LIST_ENTRY InInitializationOrderLinks;
    void *DllBase;
    void *EntryPoint;
    ULONG SizeOfImage;
    UNICODE_STRING FullDllName;
    UNICODE_STRING BaseDllName;
} LDR_ENTRY;

static void become_main_module(BYTE *base, NT_HEADERS *nt, const WCHAR *exe)
{
    PEB *peb = NtCurrentTeb()->ProcessEnvironmentBlock;
    LIST_ENTRY *head = &peb->Ldr->InMemoryOrderModuleList, *e;
    WCHAR *full = grab((wlen(exe) + 1) * sizeof(WCHAR)), *name = full, *p;

    memcpy(full, exe, (wlen(exe) + 1) * sizeof(WCHAR));
    for (p = full; *p; p++) if (*p == '\\' || *p == '/') name = p + 1;
    peb->Reserved3[1] = base;    /* ImageBaseAddress: GetModuleHandle(NULL) */
    game_base = (HMODULE)base;
    game_path_w = full;
    game_path_a = narrow(full);
    (void)nt; (void)name; (void)head; (void)e;
}

static void run(const WCHAR *exe, const WCHAR *args)
{
    BYTE *base;
    NT_HEADERS *nt;
    DWORD (WINAPI *entry)(void *);
    WCHAR folder[MAX_PATH], *slash = NULL, *p;
    PEB *peb = NtCurrentTeb()->ProcessEnvironmentBlock;

    for (p = (WCHAR *)exe; *p; p++) if (*p == '\\' || *p == '/') slash = p;
    if (slash && (size_t)(slash - exe) < MAX_PATH - 1) {
        memcpy(folder, exe, (slash - exe) * sizeof(WCHAR));
        folder[slash - exe] = 0;
        SetDllDirectoryW(folder);
    }
    set_command_line(exe, args);
    base = map_image(exe);
    if (!base) ExitProcess(1);
    current = (HMODULE)base;
    nt = (NT_HEADERS *)(base + ((IMAGE_DOS_HEADER *)base)->e_lfanew);
    become_main_module(base, nt, exe);
    relocate(base, nt);
    if (!resolve(base)) ExitProcess(1);
    start_tls(base, nt);
    patch_all();
    sayw("starting ", exe);
    entry = (void *)(base + nt->OptionalHeader.AddressOfEntryPoint);
    ExitProcess(entry(peb));
}

static WCHAR *full_path(const WCHAR *name, const WCHAR *folder)
{
    WCHAR *out = grab(MAX_PATH * 2 * sizeof(WCHAR)), *file;
    WCHAR saved[MAX_PATH];
    if (folder) { GetCurrentDirectoryW(MAX_PATH, saved); SetCurrentDirectoryW(folder); }
    if (!SearchPathW(NULL, name, L".exe", MAX_PATH * 2, out, &file)) out[0] = 0;
    if (folder) SetCurrentDirectoryW(saved);
    return out[0] ? out : NULL;
}

static BOOL WINAPI my_create_process_w(const WCHAR *app, WCHAR *cmd, SECURITY_ATTRIBUTES *pa, SECURITY_ATTRIBUTES *ta,
                                       BOOL inherit, DWORD flags, void *env, const WCHAR *folder,
                                       STARTUPINFOW *si, PROCESS_INFORMATION *pi)
{
    WCHAR *exe = NULL, windows[MAX_PATH];
    const WCHAR *args = NULL, *p;
    (void)pa; (void)ta; (void)inherit; (void)si; (void)pi;
    sayw("program asked to start: ", app ? app : cmd);
    /* the program and the rest of the command line */
    if (cmd) {
        p = cmd;
        while (*p == ' ' || *p == '\t') p++;
        if (*p == '"') { p++; while (*p && *p != '"') p++; if (*p) p++; }
        else while (*p && *p != ' ' && *p != '\t') p++;
        while (*p == ' ' || *p == '\t') p++;
        args = p;
    }
    if (app) exe = full_path(app, folder);
    else if (cmd && split(cmd, NULL) > 0) {
        WCHAR **words = grab(64 * sizeof(WCHAR *) + (wlen(cmd) + 64) * sizeof(WCHAR));
        split(cmd, words);
        exe = full_path(words[0], folder);
    }
    GetWindowsDirectoryW(windows, MAX_PATH);
    if (!exe || (wlen(exe) >= wlen(windows) && CompareStringOrdinal(exe, wlen(windows), windows, wlen(windows), TRUE) == CSTR_EQUAL)) {
        say("not a program beside the game: refused, as the console does");
        SetLastError(ERROR_NOT_SUPPORTED);
        return FALSE;
    }
    if (env) {
        /* the environment block the launcher gave, as this process's */
        if (flags & CREATE_UNICODE_ENVIRONMENT) {
            const WCHAR *e;
            for (e = env; *e; e += wlen(e) + 1) {
                WCHAR *copy = grab((wlen(e) + 1) * sizeof(WCHAR)), *q = copy + 1;
                memcpy(copy, e, (wlen(e) + 1) * sizeof(WCHAR));
                while (*q && *q != '=') q++;
                if (*q) { *q = 0; SetEnvironmentVariableW(copy, q + 1); }
            }
        } else {
            const char *e;
            for (e = env; *e; e += alen(e) + 1) {
                WCHAR *copy = widen(e), *q = copy + 1;
                while (*q && *q != '=') q++;
                if (*q) { *q = 0; SetEnvironmentVariableW(copy, q + 1); }
            }
        }
    }
    start_program(exe, args, folder);
    return FALSE;
}

static void start_program(const WCHAR *exe, const WCHAR *args, const WCHAR *folder)
{
    HMODULE user32 = GetModuleHandleA("user32.dll");
    WCHAR here[MAX_PATH];
    /* the launcher's windows go first: their window procedures go with it */
    if (user32) {
        BOOL (WINAPI *each)(DWORD, WNDENUMPROC, LPARAM) = (void *)GetProcAddress(user32, "EnumThreadWindows");
        void *destroy = (void *)GetProcAddress(user32, "DestroyWindow");
        if (each && destroy) each(GetCurrentThreadId(), close_window, (LPARAM)destroy);
    }
    if (folder) SetCurrentDirectoryW(folder);
    else {
        WCHAR *p, *slash = NULL;
        size_t n = wlen(exe) < MAX_PATH - 1 ? wlen(exe) : MAX_PATH - 1;
        memcpy(here, exe, n * sizeof(WCHAR)); here[n] = 0;
        for (p = here; *p; p++) if (*p == '\\') slash = p;
        if (slash) { *slash = 0; SetCurrentDirectoryW(here); }
    }
    if (current) VirtualFree(current, 0, MEM_RELEASE);
    current = NULL;
    say("launcher handed over; starting its program in its place");
    run(exe, args);
}

/* --- entry ------------------------------------------------------------------ */
int start(void)
{
    WCHAR *line = GetCommandLineW(), *p = line, *exe;
    WCHAR name[MAX_PATH], self[MAX_PATH], *slash = NULL;
    const WCHAR *args;

    GetModuleFileNameW(NULL, self, MAX_PATH);
    for (p = self; *p; p++) if (*p == '\\') slash = p;
    if (slash) {
        memcpy(slash + 1, L"pwexec.log", 22);
        log_file = CreateFileW(self, FILE_APPEND_DATA, FILE_SHARE_READ, NULL, OPEN_ALWAYS, 0, NULL);
    }
    /* skip our own name */
    p = line;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '"') { p++; while (*p && *p != '"') p++; if (*p) p++; }
    else while (*p && *p != ' ' && *p != '\t') p++;
    while (*p == ' ' || *p == '\t') p++;
    /* the launcher's name, then its arguments */
    {
        size_t n = 0;
        if (*p == '"') { p++; while (*p && *p != '"' && n < MAX_PATH - 1) name[n++] = *p++; if (*p) p++; }
        else while (*p && *p != ' ' && *p != '\t' && n < MAX_PATH - 1) name[n++] = *p++;
        name[n] = 0;
        while (*p == ' ' || *p == '\t') p++;
        args = p;
    }
    if (!name[0]) { say("usage: pwexec.exe launcher.exe [arguments]"); return 1; }
    exe = full_path(name, NULL);
    if (!exe) { sayw("not found: ", name); return 1; }
    say("pwexec 1.0");
    patch_all();
    run(exe, args);
    return 0;
}
