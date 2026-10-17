// tools/native-memory-export/winhttp-proxy.cpp
//
// Coexistence proxy for the system WINHTTP.dll.
//
// The loader resolves the "WinHttp*" imports of a colocated UnityPlayer.dll
// against this DLL, so every advertised entry forwards to the real, full-path
// System32\winhttp.dll. The first forwarded call drives the lazy (never-in-
// DllMain) initialization, the module pins itself, and the shared exporter
// entrypoint runs exactly once. DLL_PROCESS_ATTACH additionally schedules one
// bounded target-gated background start (see bootstrap.h) so a host that never
// makes a forwarded call still starts the exporter. No hooks, no network
// interception, no remote-process APIs, no TLS/network changes.
//
// The four imports UnityPlayer.dll uses explicitly are WinHttpOpen,
// WinHttpGetIEProxyConfigForCurrentUser, WinHttpGetProxyForUrl and
// WinHttpCloseHandle; they are part of the baseline set below.
//
// Forward table order MUST match winhttp-proxy.asm slot numbers and the export
// order in winhttp.def.

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <intrin.h>
#include <string>
#include "early-diagnostics.h"
#include "bootstrap.h"

extern "C" void StartMemoryExportOnce();

// Resolved forward targets. Every slot is pre-seeded with the fail handler and
// replaced with the real System32 address during Initialize.
extern "C" { void (*winhttpFunctions[32])() = {}; }

// Explicit, non-returning fail handler. It is installed for any export that
// cannot be resolved on the running OS (for example an optional newer-than-
// baseline API). Because it never returns it cannot accidentally report
// success for a DWORD-returning API the way a generic "return 0" stub would.
// Loading the proxy never fails merely because an optional symbol is absent.
extern "C" __declspec(noreturn) void WinHttpMissingExport() {
    RaiseFailFastException(nullptr, nullptr, 0);
    __fastfail(FAST_FAIL_FATAL_APP_EXIT);
}

namespace {

constexpr unsigned kFunctionCount = 32;

INIT_ONCE g_once = INIT_ONCE_STATIC_INIT;

// Index -> export name. Stable Windows 10 baseline set plus the Windows 8 era
// proxy-resolver additions; no Windows 11-only entry points.
const char* const kNames[kFunctionCount] = {
    "WinHttpAddRequestHeaders",                // 0
    "WinHttpCheckPlatform",                    // 1
    "WinHttpCloseHandle",                      // 2
    "WinHttpConnect",                          // 3
    "WinHttpCrackUrl",                         // 4
    "WinHttpCreateProxyResolver",              // 5
    "WinHttpCreateUrl",                        // 6
    "WinHttpDetectAutoProxyConfigUrl",         // 7
    "WinHttpFreeProxyResult",                  // 8
    "WinHttpGetDefaultProxyConfiguration",     // 9
    "WinHttpGetIEProxyConfigForCurrentUser",  // 10
    "WinHttpGetProxyForUrl",                  // 11
    "WinHttpGetProxyForUrlEx",                // 12
    "WinHttpGetProxyResult",                  // 13
    "WinHttpOpen",                            // 14
    "WinHttpOpenRequest",                     // 15
    "WinHttpQueryAuthSchemes",                // 16
    "WinHttpQueryDataAvailable",              // 17
    "WinHttpQueryHeaders",                    // 18
    "WinHttpQueryOption",                     // 19
    "WinHttpReadData",                        // 20
    "WinHttpReceiveResponse",                 // 21
    "WinHttpResetAutoProxy",                  // 22
    "WinHttpSendRequest",                     // 23
    "WinHttpSetCredentials",                  // 24
    "WinHttpSetDefaultProxyConfiguration",    // 25
    "WinHttpSetOption",                       // 26
    "WinHttpSetStatusCallback",               // 27
    "WinHttpSetTimeouts",                     // 28
    "WinHttpTimeFromSystemTime",              // 29
    "WinHttpTimeToSystemTime",                // 30
    "WinHttpWriteData",                       // 31
};

static_assert(sizeof(kNames) / sizeof(kNames[0]) == kFunctionCount, "forward table size mismatch");

BOOL CALLBACK Initialize(PINIT_ONCE, PVOID, PVOID*) {
    gkearly::Log("initialize_begin");

    // Default every slot to the fail handler so a missing symbol only matters
    // when it is actually invoked.
    for (unsigned i = 0; i < kFunctionCount; ++i) winhttpFunctions[i] = &WinHttpMissingExport;

    wchar_t system[MAX_PATH];
    if (!GetSystemDirectoryW(system, MAX_PATH)) {
        gkearly::Log("initialize_system_directory_failed", nullptr, GetLastError());
        return FALSE;
    }
    gkearly::Log("initialize_system_directory_ok");
    std::wstring path = std::wstring(system) + L"\\winhttp.dll";
    gkearly::Log("initialize_load_begin", "winhttp.dll");
    HMODULE real = LoadLibraryExW(path.c_str(), nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!real) {
        gkearly::Log("initialize_load_failed", "winhttp.dll", GetLastError());
        return FALSE;
    }
    gkearly::Log("initialize_load_ok", "winhttp.dll");

    gkearly::Log("initialize_getprocaddress_begin");
    for (unsigned i = 0; i < kFunctionCount; ++i) {
        FARPROC proc = GetProcAddress(real, kNames[i]);
        if (proc) winhttpFunctions[i] = reinterpret_cast<void (*)()>(proc);
        else gkearly::Log("initialize_getprocaddress_optional_missing", kNames[i], GetLastError());
    }
    gkearly::Log("initialize_getprocaddress_ok");

    // Pin this module so the thunks and their forward targets can never vanish.
    HMODULE pinned = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                       reinterpret_cast<LPCWSTR>(&Initialize), &pinned);

    gkearly::Log("initialize_call_start_memory_export");
    StartMemoryExportOnce();
    return TRUE;
}

} // namespace

// Called by every forwarding thunk before its tail jump. DllMain deliberately
// does not touch initialization; the first forwarded call drives it. The
// caller's LastError is preserved across the one-time init so forwarded calls
// keep the original GetLastError semantics.
extern "C" void EnsureWinHttpProxy() {
    const DWORD saved = GetLastError();
    if (!InitOnceExecuteOnce(&g_once, Initialize, nullptr, nullptr))
        RaiseFailFastException(nullptr, nullptr, 0);
    SetLastError(saved);
}

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        gkearly::ResetLog();
        const BOOL disabled = DisableThreadLibraryCalls(module);
        gkearly::Log("dllmain_attach",
            disabled ? "disable_thread_library_calls_ok" : "disable_thread_library_calls_error",
            disabled ? 0 : GetLastError());
        // Automatic startup: only for the real game, and only a target gate
        // plus one plain CreateThread handled inside the header. The lazy
        // Initialize path above stays unchanged as the fallback when this is
        // skipped or fails.
        if (gkearly::IsTargetProcessEarly()) gkbootstrap::Schedule();
    }
    return TRUE;
}
