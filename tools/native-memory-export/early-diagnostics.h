// tools/native-memory-export/early-diagnostics.h
//
// Loader-lock-safe startup journal shared by the version.dll / winhttp.dll
// coexistence proxies and the readonly memory exporter.
//
// Why a second journal:
//   diagnostics.h records what the *worker* does once it is running, but it is
//   compiled with STL (std::mutex/string/ostringstream) and therefore may not be
//   touched from DllMain. A restart could still leave no export-diagnostic.log
//   because the failure happened earlier: the DLL never attached, the lazy
//   Initialize was never reached, StartMemoryExportOnce was never called, or the
//   worker never entered. This header records those earliest phases into a
//   single fixed file so the first failing stage is visible.
//
// Hard constraints honoured here (this runs under the loader lock from DllMain):
//   * Win32 and plain fixed stack buffers only.
//   * NO STL, NO heap, NO mutex, NO CRT formatting (no snprintf), NO
//     LoadLibrary, NO threads/waits and NO INI/profile parsing.
//   * The file is a single shared location, bounded to 64 KiB. It is opened for
//     append with FILE_SHARE_READ|FILE_SHARE_WRITE and each writer atomically
//     reserves a clipped byte count, so the cap still holds when the bootstrap
//     thread and the lazy Initialize path append concurrently.
//   * Every helper preserves the caller's LastError.
//   * Helper failures are silently ignored so a broken diagnostic can never
//     crash or destabilise the game.
//
// Privacy: only fixed phase literals plus numeric pid/tick/Win32 error are
// written. No memory ids, paths, URLs, tokens or account state ever appear.

#pragma once
#ifndef GK_EARLY_DIAGNOSTICS_H
#define GK_EARLY_DIAGNOSTICS_H

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif

#include <windows.h>

namespace gkearly {

// ---- fixed, constant configuration -----------------------------------------

inline const unsigned long long kMaxLogBytes = 64ull * 1024ull;
inline const unsigned kPathChars = 1024;

inline const wchar_t kLocalAppData[] = L"LOCALAPPDATA";
inline const wchar_t kDirSuffix[]    = L"\\GKMemoryTool";
inline const wchar_t kFileSuffix[]   = L"\\loader-diagnostic.log";
inline const wchar_t kTargetExe[]    = L"gakumas.exe";

// ---- single shared logfile state (one instance per process image) ----------
// `inline` gives these definitions external linkage, so proxy.cpp, the
// winhttp proxy and exporter.cpp that share one DLL all resolve the same
// path rather than each caching their own copy.

inline wchar_t g_logPath[kPathChars] = {};
inline bool    g_pathResolved = false;
inline bool    g_pathUsable   = false;

// Per-DLL reservation cursor for the 64 KiB cap. Writers run concurrently (the
// bootstrap thread and the lazy Initialize path can append at the same time),
// so the on-disk file size alone cannot bound the append: each writer first
// atomically reserves a clipped byte count here and only then appends. A failed
// write keeps its reservation, so the budget is only ever approached, never
// crossed, and ResetLog zeroes it for the next game start.
inline volatile LONG g_reservedBytes = 0;

// ---- tiny, allocation-free formatting --------------------------------------

inline unsigned AppendAscii(char* out, unsigned cap, unsigned pos, const char* text) {
    if (!text) return pos;
    while (*text && pos + 1 < cap) out[pos++] = *text++;
    return pos;
}

inline unsigned AppendUnsigned(char* out, unsigned cap, unsigned pos, unsigned long long value) {
    char digits[24];
    unsigned count = 0;
    if (value == 0) digits[count++] = '0';
    while (value > 0 && count < sizeof(digits)) {
        digits[count++] = static_cast<char>('0' + static_cast<int>(value % 10));
        value /= 10;
    }
    while (count > 0 && pos + 1 < cap) out[pos++] = digits[--count];
    return pos;
}

// Fixed one-line record: phase literal, optional fixed detail, pid, monotonic
// GetTickCount64 timestamp and the numeric Win32 error.
inline unsigned BuildLine(char* out, unsigned cap, const char* phase,
                          const char* detail, unsigned long error) {
    unsigned pos = 0;
    pos = AppendAscii(out, cap, pos, "phase=");
    pos = AppendAscii(out, cap, pos, phase);
    if (detail && *detail) {
        pos = AppendAscii(out, cap, pos, " detail=");
        pos = AppendAscii(out, cap, pos, detail);
    }
    pos = AppendAscii(out, cap, pos, " pid=");
    pos = AppendUnsigned(out, cap, pos, static_cast<unsigned long long>(GetCurrentProcessId()));
    pos = AppendAscii(out, cap, pos, " tick=");
    pos = AppendUnsigned(out, cap, pos, static_cast<unsigned long long>(GetTickCount64()));
    pos = AppendAscii(out, cap, pos, " error=");
    pos = AppendUnsigned(out, cap, pos, static_cast<unsigned long long>(error));
    pos = AppendAscii(out, cap, pos, "\r\n");
    if (cap > 0) out[pos < cap ? pos : cap - 1] = 0;
    return pos;
}

// ---- target detection (fixed ASCII casefold, no CRT) -----------------------
// Only the real game records anything, so the proxy-smoke harnesses and any
// other host that happens to load the DLL never touch a user profile.

inline bool IsTargetProcessEarly() {
    wchar_t path[kPathChars];
    const DWORD n = GetModuleFileNameW(nullptr, path, kPathChars);
    if (n == 0 || n >= kPathChars) return false;

    DWORD start = 0;
    for (DWORD i = 0; i < n; ++i) {
        if (path[i] == L'\\' || path[i] == L'/') start = i + 1;
    }

    DWORD j = 0;
    for (; start + j < n && kTargetExe[j]; ++j) {
        wchar_t c = path[start + j];
        if (c >= L'A' && c <= L'Z') c = static_cast<wchar_t>(c + 32);
        if (c != kTargetExe[j]) return false;
    }
    return kTargetExe[j] == 0 && (start + j == n);
}

// ---- path resolution -------------------------------------------------------
// LOCALAPPDATA\GKMemoryTool\loader-diagnostic.log. The installer already
// creates the user root; only the single GKMemoryTool level may be missing, so
// a single non-recursive CreateDirectoryW is attempted and its result ignored.

inline bool ResolvePath() {
    if (g_pathResolved) return g_pathUsable;
    const DWORD saved = GetLastError();

    wchar_t local[kPathChars];
    const DWORD n = GetEnvironmentVariableW(kLocalAppData, local, kPathChars);
    bool usable = false;
    // Reject long paths rather than opening an accidentally truncated filename.
    if (n > 0 && n + (sizeof(kDirSuffix) / sizeof(wchar_t) - 1) +
                       (sizeof(kFileSuffix) / sizeof(wchar_t)) <= kPathChars) {
        wchar_t directory[kPathChars + 32];
        unsigned d = 0;
        for (DWORD i = 0; i < n && d + 1 < (kPathChars + 32); ++i) directory[d++] = local[i];
        for (const wchar_t* p = kDirSuffix; *p && d + 1 < (kPathChars + 32); ++p) directory[d++] = *p;
        directory[d] = 0;

        CreateDirectoryW(directory, nullptr);

        unsigned f = 0;
        for (unsigned i = 0; i < d && f + 1 < kPathChars; ++i) g_logPath[f++] = directory[i];
        for (const wchar_t* p = kFileSuffix; *p && f + 1 < kPathChars; ++p) g_logPath[f++] = *p;
        g_logPath[f] = 0;
        usable = true;
    }

    g_pathResolved = true;
    g_pathUsable = usable;
    SetLastError(saved);
    return usable;
}

// ---- journal operations ----------------------------------------------------

// Truncates any stale journal. Called once from DllMain attach of the proxy
// that loaded first, so each game start begins with a clean file. The
// reservation cursor is reset alongside the truncation.
inline void ResetLog() {
    const DWORD saved = GetLastError();
    g_reservedBytes = 0;
    if (IsTargetProcessEarly() && ResolvePath()) {
        HANDLE file = CreateFileW(g_logPath, GENERIC_WRITE,
                                  FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                                  CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
    }
    SetLastError(saved);
}

// Appends one record. `phase` (and optional `detail`) must be process-lifetime
// literals. The handle is shared for read+write so a concurrent writer no longer
// fails the open, and the 64 KiB cap is enforced by atomically reserving the
// clipped line byte count before appending. Never blocks: a lost
// InterlockedCompareExchange race simply re-reads the cursor and retries, which
// is bounded because the contending writers are few. Drops the record once the
// cap is reached.
inline void Log(const char* phase, const char* detail = nullptr, unsigned long error = 0) {
    if (!phase) return;
    const DWORD saved = GetLastError();
    if (IsTargetProcessEarly() && ResolvePath()) {
        HANDLE file = CreateFileW(g_logPath, FILE_APPEND_DATA | FILE_READ_ATTRIBUTES,
                                  FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_ALWAYS,
                                  FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file != INVALID_HANDLE_VALUE) {
            LARGE_INTEGER size;
            size.QuadPart = 0;
            if (GetFileSizeEx(file, &size)) {
                char line[512];
                const unsigned length = BuildLine(line, sizeof(line), phase, detail, error);

                // Existing on-disk bytes are the floor for a new reservation.
                unsigned long long existing = static_cast<unsigned long long>(size.QuadPart);
                if (existing > kMaxLogBytes) existing = kMaxLogBytes;

                // Reserve a clipped byte count. The reservation may run ahead of
                // the real file size while writers are in flight, so the base is
                // the larger of the on-disk size and the current reservation.
                unsigned toWrite = 0;
                for (;;) {
                    toWrite = 0;
                    const LONG reserved = g_reservedBytes;
                    unsigned long long base =
                        (reserved > 0) ? static_cast<unsigned long long>(reserved) : 0ull;
                    if (existing > base) base = existing;
                    if (base >= kMaxLogBytes) break;   // cap reached: drop record
                    const unsigned long long available = kMaxLogBytes - base;
                    unsigned candidate = length;
                    if (static_cast<unsigned long long>(candidate) > available)
                        candidate = static_cast<unsigned>(available);
                    if (candidate == 0) break;
                    const LONG next = static_cast<LONG>(base + candidate);
                    if (InterlockedCompareExchange(&g_reservedBytes, next, reserved) == reserved) {
                        toWrite = candidate;           // reservation committed
                        break;
                    }
                    // Contention: retry with the freshly observed reservation.
                }

                if (toWrite > 0) {
                    DWORD written = 0;
                    WriteFile(file, line, toWrite, &written, nullptr);
                }
            }
            CloseHandle(file);
        }
    }
    SetLastError(saved);
}

} // namespace gkearly

#endif // GK_EARLY_DIAGNOSTICS_H
