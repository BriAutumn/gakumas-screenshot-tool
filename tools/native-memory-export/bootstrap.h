// tools/native-memory-export/bootstrap.h
//
// Bounded automatic export startup for the version.dll / winhttp.dll
// coexistence proxies.
//
// Core problem this solves: the proxies only triggered the lazy exporter
// Initialize() from the first *forwarded* call. A host that loads the DLL but
// never calls one of the forwarded exports (the reported "DLL loads but no
// proxy calls" case) therefore never started the exporter at all.
//
// This header schedules a single, target-gated background start from
// DLL_PROCESS_ATTACH. Design constraints, all enforced here:
//   * DllMain only performs a target gate and one plain Win32 CreateThread,
//     then logs and CloseHandles. No waits, no Sleep, no LoadLibrary, no CRT,
//     no game reads and no thread joins happen in DllMain.
//   * Windows serialises thread creation against the loader lock, so the new
//     thread body runs only after DLL initialization has exited. That is why
//     the pin below is safe to do on the callback thread but not in DllMain.
//   * The callback pins *this* DLL (GetModuleHandleExW FROM_ADDRESS|PIN using
//     its own address) before it touches the exporter; a pin failure is
//     recorded and the callback returns safely without starting anything.
//   * The callback calls the shared StartMemoryExportOnce() and never invokes
//     the lazy Initialize() or resolves any proxy forward table. Those stay
//     lazy for real forwarded calls.
//   * A per-DLL InterlockedCompareExchange guard schedules exactly one thread
//     even if DllMain attaches more than once.
//   * GK_BOOTSTRAP_TEST_FAIL_CREATE (default 0) simulates a CreateThread
//     failure so the legacy lazy fallback can be exercised; production is
//     never forced to fail.
//
// The lazy Initialize path is deliberately unchanged and remains the automatic
// fallback that starts the exporter when bootstrap scheduling fails.

#pragma once
#ifndef GK_BOOTSTRAP_H
#define GK_BOOTSTRAP_H

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif

#include <windows.h>

#include "early-diagnostics.h"

// Shared one-shot exporter entrypoint (defined in exporter.cpp). Declared here
// so the bootstrap callback needs no proxy-header coupling.
extern "C" void StartMemoryExportOnce();

#ifndef GK_BOOTSTRAP_TEST_FAIL_CREATE
#define GK_BOOTSTRAP_TEST_FAIL_CREATE 0
#endif

// Automatic startup is gated inside ExportWorker on this game's late native
// module, then attaches before metadata access. Validated on the real game;
// the withdrawn 1.0.12 entered IL2CPP before its runtime was initialized.
#ifndef GK_BOOTSTRAP_ENABLED
#define GK_BOOTSTRAP_ENABLED 1
#endif

namespace gkbootstrap {

// Per-DLL scheduling guard. `inline` gives exactly one instance per DLL image;
// 0 = not scheduled, non-zero = scheduled (or attempted). The flag is never
// cleared, so a failed schedule cannot be retried into a second thread.
inline volatile LONG g_scheduleState = 0;

// Runs on the bootstrap thread, which Windows starts only after DLL
// initialization has exited. Logs its stages, pins this module and then calls
// the shared exporter entrypoint once.
inline DWORD WINAPI BootstrapThread(LPVOID) {
    gkearly::Log("bootstrap_entered");

    // Pin this module from our own code address *before* touching the exporter
    // so the exporter's code/data can never be unloaded while its worker runs.
    // A failure is recorded and the thread returns without starting anything;
    // the unchanged lazy Initialize path remains available.
    HMODULE module = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                                GET_MODULE_HANDLE_EX_FLAG_PIN,
                            reinterpret_cast<LPCWSTR>(&BootstrapThread),
                            &module)) {
        gkearly::Log("bootstrap_pin_failed", nullptr, GetLastError());
        return 0;
    }
    gkearly::Log("bootstrap_pinned");

    // Deliberately no Initialize() and no forward-table resolution here: those
    // stay lazy for real forwarded calls. Only the shared one-shot entry runs.
    gkearly::Log("bootstrap_call_export");
    StartMemoryExportOnce();
    return 0;
}

// Called from DllMain(DLL_PROCESS_ATTACH) after the early log, once the target
// gate has passed. Performs a single guarded plain Win32 CreateThread, logs the
// schedule stage and CloseHandles the thread. Never waits, sleeps, loads a
// library, calls the CRT or joins the thread.
inline void Schedule() {
#if !GK_BOOTSTRAP_ENABLED
    gkearly::Log("bootstrap_disabled", "gc_regression_quarantine");
    return;
#endif
    if (InterlockedCompareExchange(&g_scheduleState, 1, 0) != 0) return;

    gkearly::Log("bootstrap_schedule");

#if GK_BOOTSTRAP_TEST_FAIL_CREATE
    // Test-only: behave exactly like a failed CreateThread so the unchanged
    // lazy path is the fallback. Production never takes this branch.
    gkearly::Log("bootstrap_schedule_failed", "test_forced", 0);
#else
    HANDLE thread = CreateThread(nullptr, 0, &BootstrapThread, nullptr, 0, nullptr);
    if (!thread) {
        gkearly::Log("bootstrap_schedule_failed", nullptr, GetLastError());
        // The guarded flag stays set; the legacy lazy Initialize still runs on
        // the first forwarded call, so automatic startup is not lost.
        return;
    }
    CloseHandle(thread);
#endif
}

} // namespace gkbootstrap

#endif // GK_BOOTSTRAP_H
