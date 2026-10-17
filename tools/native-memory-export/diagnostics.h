// tools/native-memory-export/diagnostics.h
//
// Bounded, thread-safe diagnostic journal for the readonly IL2CPP memory
// exporter. It records only startup/phase/elapsed/PID/OS error signal strings;
// it must never receive memory ids, card data, photo metadata, URLs, tokens,
// raw pointer contents or account state. Callers pass fixed lifetime-safe
// literals (or public API symbol names).
//
// Design constraints honored here:
//   * Fixed, bounded log size per startup (< GK_EXPORT_MAX_LOG_BYTES).
//   * Repeat capping: identical phase/detail pairs are logged only once, then
//     sparsely, so a polling loop cannot exhaust the budget.
//   * The latest observed phase is always retained in memory even after the
//     on-disk cap is reached, so a watchdog can report where the worker stalled.
//   * The lock is only ever held across a short file append; game APIs are
//     never invoked while the lock is held.
//
// This header is self-contained (header-only) so exporter.cpp needs no extra
// build inputs.

#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif

#include <windows.h>

#include <cstddef>
#include <cstdio>
#include <mutex>
#include <string>

#ifndef GK_EXPORT_MAX_LOG_BYTES
#define GK_EXPORT_MAX_LOG_BYTES (256u * 1024u)
#endif

namespace gkdiag {

class DiagnosticLog {
public:
    // Opens (truncating) export-diagnostic.log beside status.json. Safe to call
    // from the worker thread before any potentially blocking startup call.
    void Start(const std::wstring& directory, unsigned long processId) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (file_ != INVALID_HANDLE_VALUE) {
            CloseHandle(file_);
            file_ = INVALID_HANDLE_VALUE;
        }
        processId_ = processId;
        startTick_ = GetTickCount();
        started_ = true;
        capped_ = false;
        bytesWritten_ = 0;
        lastKey_.clear();
        repeat_ = 0;
        lastPhase_.clear();
        lastDetail_.clear();
        if (directory.empty()) return;
        const std::wstring path = directory + L"\\export-diagnostic.log";
        file_ = CreateFileW(path.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file_ == INVALID_HANDLE_VALUE) return;
        WriteLineLocked_("startup", nullptr);
    }

    // Records a checkpoint. `phase` and `detail` must be process-lifetime
    // constants. Identical (phase, detail) pairs are capped.
    void Checkpoint(const char* phase, const char* detail = nullptr) {
        if (!phase) return;
        std::lock_guard<std::mutex> lock(mutex_);
        lastPhase_.assign(phase);
        if (detail && *detail) lastDetail_.assign(detail); else lastDetail_.clear();
        std::string key(phase);
        key.push_back('|');
        if (detail) key.append(detail);
        if (key == lastKey_) {
            ++repeat_;
            // Log the first occurrence, then every 64th repeat.
            if ((repeat_ % 64) != 1) return;
        } else {
            lastKey_ = key;
            repeat_ = 1;
        }
        WriteLineLocked_(phase, detail);
    }

    // Unconditional (still size-bounded) event marker. Used for one-off signals
    // such as the watchdog deadline.
    void Event(const char* phase, const char* detail = nullptr) {
        if (!phase) return;
        std::lock_guard<std::mutex> lock(mutex_);
        lastPhase_.assign(phase);
        if (detail && *detail) lastDetail_.assign(detail); else lastDetail_.clear();
        lastKey_.assign(phase);
        lastKey_.push_back('|');
        if (detail) lastKey_.append(detail);
        repeat_ = 1;
        WriteLineLocked_(phase, detail);
    }

    // Latest phase (optionally with detail) retained in memory regardless of the
    // on-disk cap. Safe to call from the watchdog thread.
    std::string LastPhaseSummary() const {
        std::lock_guard<std::mutex> lock(mutex_);
        std::string summary = lastPhase_;
        if (!lastDetail_.empty()) {
            summary += " (";
            summary += lastDetail_;
            summary += ')';
        }
        return summary;
    }

    bool Capped() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return capped_;
    }

private:
    void WriteLineLocked_(const char* phase, const char* detail) {
        if (file_ == INVALID_HANDLE_VALUE) return;
        if (bytesWritten_ >= static_cast<long long>(GK_EXPORT_MAX_LOG_BYTES)) {
            capped_ = true;
            return;
        }
        const long long elapsed = started_ ? static_cast<long long>(GetTickCount() - startTick_) : 0;
        const bool hasDetail = detail && *detail;
        char buffer[512];
        const int n = std::snprintf(buffer, sizeof(buffer), "elapsed_ms=%lld pid=%lu phase=%s%s%s\n",
                                    elapsed, processId_, phase,
                                    hasDetail ? " detail=" : "",
                                    hasDetail ? detail : "");
        if (n <= 0) return;
        std::size_t length = static_cast<std::size_t>(n);
        if (length >= sizeof(buffer)) length = sizeof(buffer) - 1;
        const auto remaining = static_cast<std::size_t>(GK_EXPORT_MAX_LOG_BYTES - bytesWritten_);
        if (length > remaining) length = remaining;
        DWORD written = 0;
        if (WriteFile(file_, buffer, static_cast<DWORD>(length), &written, nullptr)) {
            bytesWritten_ += static_cast<long long>(written);
            if (bytesWritten_ >= static_cast<long long>(GK_EXPORT_MAX_LOG_BYTES)) capped_ = true;
        }
    }

    mutable std::mutex mutex_;
    HANDLE             file_ = INVALID_HANDLE_VALUE;
    unsigned long      processId_ = 0;
    DWORD              startTick_ = 0;
    bool               started_ = false;
    bool               capped_ = false;
    long long          bytesWritten_ = 0;
    std::string        lastKey_;
    unsigned long long repeat_ = 0;
    std::string        lastPhase_;
    std::string        lastDetail_;
};

inline DiagnosticLog& Log() {
    static DiagnosticLog instance;
    return instance;
}

} // namespace gkdiag
