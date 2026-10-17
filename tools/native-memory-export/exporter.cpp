// tools/native-memory-export/exporter.cpp
//
// Standalone IL2CPP memory exporter for Gakumas (gakumas.exe).
// Voluntarily loaded at game startup via version.dll proxy; read-only.
// No hooks, no injection, no collection mutation.
//
// Public entrypoint: void StartMemoryExportOnce() (C++ linkage).
//
// DIAGNOSTIC PATCH (plugin_version 1.0.7-diagnostic)
// --------------------------------------------------
// The shipped exporter could remain "running" past its 180s cooperative timeout
// when a single native IL2CPP call blocked. This patch adds:
//   * a bounded export-diagnostic.log (phase/elapsed/PID/OS signal only) with
//     checkpoints placed *before* each potentially blocking startup call, and
//   * an independent watchdog thread that never calls IL2CPP, never terminates
//     threads and never mutates game state, but publishes a terminal
//     status="timeout" if the worker has not finished by the deadline.
// Serialization of records, record ids and successful-snapshot handling are
// unchanged.
//
// Native runtime readiness gate
// -----------------------------
// Local investigation of GakumasLocalify shows src/windowsPlatform.cpp patches
// the game assembly only while VuplexWebViewWindows.dll is loaded; the IL2CPP
// runtime Init then runs domain_get -> thread_attach -> foreach assembly. The
// original crash was a first domain_get at ~2.6s, i.e. while the runtime was
// still starting. Observing GameAssembly.dll alone is therefore NOT sufficient
// evidence that IL2CPP may be touched from this worker. This is an
// application-specific readiness marker, not a universal IL2CPP claim: the
// Windows-only gate below waits (bounded by the existing overall budget) for
// that late module before the first ResolveApi/IL2CPP call.
//
// Automatic startup: this shared entrypoint is invoked either by the proxy
// bootstrap thread scheduled from DLL_PROCESS_ATTACH (see bootstrap.h) or by
// the unchanged lazy Initialize path on the first forwarded call. Both funnel
// through the single g_started atomic below, so at most one exporter runs.

#include "diagnostics.h"
#include "early-diagnostics.h"

#include <windows.h>
#include <bcrypt.h>
#include <process.h>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cwctype>
#include <fstream>
#include <filesystem>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include <unordered_set>

extern "C" __declspec(dllexport) void StartMemoryExportOnce();

// Test override: shorten the independent watchdog deadline. Defaults to the
// production 180s (3 minute) overall budget. The worker's cooperative polling
// budget is intentionally left unchanged (kMaxWaitMs) so the watchdog can be
// exercised against a blocking native call that the cooperative checks can
// never observe. A test override such as 250 (ms) keeps working.
#ifndef GK_EXPORT_TEST_TIMEOUT_MS
#define GK_EXPORT_TEST_TIMEOUT_MS 180000
#endif

// Test override: allow StartMemoryExportOnce to run outside gakumas.exe so an
// isolated fake-GameAssembly harness can exercise the worker/watchdog. Defaults
// to production behavior.
#ifndef GK_EXPORT_TEST_ALLOW_NON_TARGET
#define GK_EXPORT_TEST_ALLOW_NON_TARGET 0
#endif

namespace {

std::wstring OutputDirectory() {
    wchar_t exe[32768] = {}, output[32768] = {}, local[32768] = {};
    GetModuleFileNameW(nullptr, exe, 32768);
    const auto config = std::filesystem::path(exe).parent_path() / L"GKMemoryExport.ini";
    GetPrivateProfileStringW(L"GKMemoryExport", L"ExportDirectory", L"", output, 32768, config.c_str());
    std::filesystem::path directory(output);
    if (!directory.is_absolute()) {
        if (!GetEnvironmentVariableW(L"LOCALAPPDATA", local, 32768)) return L"";
        directory = std::filesystem::path(local) / L"GKMemoryTool" / L"native-export";
    }
    std::error_code error;
    std::filesystem::create_directories(directory, error);
    return error ? L"" : directory.wstring();
}

std::string SnapshotHash(const std::string& value) {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    unsigned char digest[32] = {};
    if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0) return "";
    const auto status = BCryptHash(algorithm, nullptr, 0,
        reinterpret_cast<PUCHAR>(const_cast<char*>(value.data())), static_cast<ULONG>(value.size()), digest, 32);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    if (status < 0) return "";
    std::string hex;
    for (auto b : digest) { hex += "0123456789abcdef"[b >> 4]; hex += "0123456789abcdef"[b & 15]; }
    return hex;
}

std::string PhotoCacheHash(const std::string& value) {
    if (value.empty() || value.size() > 16384) return "";
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    unsigned char digest[16] = {};
    if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_MD5_ALGORITHM, nullptr, 0) < 0) return "";
    const auto status = BCryptHash(algorithm, nullptr, 0,
        reinterpret_cast<PUCHAR>(const_cast<char*>(value.data())), static_cast<ULONG>(value.size()), digest, 16);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    if (status < 0) return "";
    std::string hex;
    for (auto b : digest) { hex += "0123456789ABCDEF"[b >> 4]; hex += "0123456789ABCDEF"[b & 15]; }
    return hex;
}
constexpr int   kMaxRecords    = 5000;
// Cooperative readiness budget. The bootstrap can start the exporter before the
// game has finished loading its assemblies, so the worker polls for up to three
// minutes (the same cap the independent watchdog enforces) before it gives up.
constexpr DWORD kMaxWaitMs     = 180000;
constexpr DWORD kPollMs        = 500;
constexpr int   kMaxExcMsg     = 500;
constexpr DWORD kWatchdogMs    = static_cast<DWORD>(GK_EXPORT_TEST_TIMEOUT_MS);

// Application-specific native readiness signal. The gate below is deliberately
// conservative: GakumasLocalify only patches the game assembly (and thus only
// reaches UnityResolve Init) while this late native module is loaded, so we wait
// for it before touching any IL2CPP symbol. This is not a universal claim that
// IL2CPP is otherwise unusable; it is this title's observed startup ordering.
const char* const    kRuntimeSignalModule  = "VuplexWebViewWindows.dll";
const wchar_t* const kRuntimeSignalModuleW = L"VuplexWebViewWindows.dll";

bool RuntimeSignalPresent() {
    return GetModuleHandleW(kRuntimeSignalModuleW) != nullptr;
}

// ---- IL2CPP exports (resolved dynamically from GameAssembly.dll) ------------
typedef void*         (__cdecl *t_domain_get)();
typedef void**        (__cdecl *t_domain_get_assemblies)(void*, size_t*);
typedef void*         (__cdecl *t_assembly_get_image)(void*);
typedef const char*   (__cdecl *t_image_get_name)(void*);
typedef void*         (__cdecl *t_class_from_name)(void*, const char*, const char*);
typedef void*         (__cdecl *t_object_get_class)(void*);
typedef void*         (__cdecl *t_class_get_parent)(void*);
typedef void*         (__cdecl *t_class_get_method_from_name)(void*, const char*, int);
typedef void*         (__cdecl *t_runtime_invoke)(void*, void*, void**, void**);
typedef void*         (__cdecl *t_object_unbox)(void*);
typedef void*         (__cdecl *t_thread_attach)(void*);
typedef void          (__cdecl *t_thread_detach)(void*);
typedef int32_t       (__cdecl *t_string_length)(void*);
typedef uint16_t*     (__cdecl *t_string_chars)(void*);
typedef void*         (__cdecl *t_method_get_return_type)(void*);
typedef int           (__cdecl *t_type_get_type)(void*);
typedef void*         (__cdecl *t_class_from_type)(void*);
typedef bool          (__cdecl *t_class_is_enum)(void*);
typedef void*         (__cdecl *t_class_enum_basetype)(void*);
typedef void          (__cdecl *t_format_exception)(void*, char*, int);

struct Api {
    t_domain_get                 domain_get                 = nullptr;
    t_domain_get_assemblies      domain_get_assemblies      = nullptr;
    t_assembly_get_image         assembly_get_image         = nullptr;
    t_image_get_name             image_get_name             = nullptr;
    t_class_from_name            class_from_name            = nullptr;
    t_object_get_class           object_get_class           = nullptr;
    t_class_get_parent           class_get_parent           = nullptr;
    t_class_get_method_from_name class_get_method_from_name = nullptr;
    t_runtime_invoke             runtime_invoke             = nullptr;
    t_object_unbox               object_unbox               = nullptr;
    t_thread_attach              thread_attach              = nullptr;
    t_thread_detach              thread_detach              = nullptr;
    t_string_length              string_length              = nullptr;
    t_string_chars               string_chars               = nullptr;
    t_method_get_return_type     method_get_return_type     = nullptr;
    t_type_get_type              type_get_type              = nullptr;
    t_class_from_type            class_from_type            = nullptr;
    t_class_is_enum              class_is_enum              = nullptr;
    t_class_enum_basetype        class_enum_basetype        = nullptr;
    t_format_exception           format_exception           = nullptr;
};

Api g_api;
std::atomic<bool>  g_started{false};
std::atomic<DWORD> g_workerStartMs{0};

// ---- publication / watchdog synchronization ---------------------------------
std::mutex        g_publishMutex;
std::atomic<bool> g_terminalPublished{false};
std::atomic<bool> g_workerFinished{false};
HANDLE            g_workerDoneEvent = nullptr;

long long ElapsedSince() {
    const DWORD start = g_workerStartMs.load(std::memory_order_relaxed);
    if (!start) return 0;
    return static_cast<long long>(GetTickCount() - start);
}

// Emits a single OS error code (never handles/pointers/account data) for
// lifecycle failures. The code is formatted into a stack buffer and copied
// under the log lock, so its lifetime is safe.
void LogOsError(const char* phase, DWORD code) {
    char buffer[32];
    std::snprintf(buffer, sizeof(buffer), "os_error=%lu", code);
    gkdiag::Log().Event(phase, buffer);
}

// Reports one missing API by its public IL2CPP symbol name (no pointer values).
#define GK_RESOLVE(Field, Symbol)                                                        \
    do {                                                                                 \
        gkdiag::Log().Checkpoint("resolve_api", Symbol);                                 \
        g_api.Field = reinterpret_cast<decltype(g_api.Field)>(GetProcAddress(ga, Symbol));\
        if (!g_api.Field) gkdiag::Log().Checkpoint("resolve_api_missing", Symbol);       \
    } while (0)

bool ResolveApi(HMODULE ga) {
    GK_RESOLVE(domain_get,                 "il2cpp_domain_get");
    GK_RESOLVE(domain_get_assemblies,      "il2cpp_domain_get_assemblies");
    GK_RESOLVE(assembly_get_image,         "il2cpp_assembly_get_image");
    GK_RESOLVE(image_get_name,             "il2cpp_image_get_name");
    GK_RESOLVE(class_from_name,            "il2cpp_class_from_name");
    GK_RESOLVE(object_get_class,           "il2cpp_object_get_class");
    GK_RESOLVE(class_get_parent,           "il2cpp_class_get_parent");
    GK_RESOLVE(class_get_method_from_name, "il2cpp_class_get_method_from_name");
    GK_RESOLVE(runtime_invoke,             "il2cpp_runtime_invoke");
    GK_RESOLVE(object_unbox,               "il2cpp_object_unbox");
    GK_RESOLVE(thread_attach,              "il2cpp_thread_attach");
    GK_RESOLVE(thread_detach,              "il2cpp_thread_detach");
    GK_RESOLVE(string_length,              "il2cpp_string_length");
    GK_RESOLVE(string_chars,               "il2cpp_string_chars");
    GK_RESOLVE(method_get_return_type,     "il2cpp_method_get_return_type");
    GK_RESOLVE(type_get_type,              "il2cpp_type_get_type");
    GK_RESOLVE(class_from_type,            "il2cpp_class_from_type");
    GK_RESOLVE(class_is_enum,              "il2cpp_class_is_enum");
    GK_RESOLVE(class_enum_basetype,        "il2cpp_class_enum_basetype");
    GK_RESOLVE(format_exception,           "il2cpp_format_exception");
    return g_api.domain_get && g_api.domain_get_assemblies && g_api.assembly_get_image &&
           g_api.image_get_name && g_api.class_from_name && g_api.object_get_class &&
           g_api.class_get_parent && g_api.class_get_method_from_name &&
           g_api.runtime_invoke && g_api.object_unbox && g_api.thread_attach &&
           g_api.thread_detach && g_api.string_length && g_api.string_chars &&
           g_api.method_get_return_type && g_api.type_get_type &&
           g_api.class_from_type && g_api.class_is_enum && g_api.class_enum_basetype &&
           g_api.format_exception;
}

#undef GK_RESOLVE

// RAII holder for the single per-worker IL2CPP thread attachment.
//
// The worker attaches once, as soon as a domain is available, *before* any
// metadata/assembly API, and preserves that attached thread across assembly
// polling. This guard owns the attach handle so its destructor records the
// detach checkpoint and calls il2cpp_thread_detach exactly once on every
// non-fatal return or exception path. A never-attached (null) thread is a no-op,
// so thread_detach is never invoked with a null thread. There is intentionally
// no explicit detach call anywhere else, which avoids a double detach.
struct AttachedThread {
    void* thread = nullptr;
    ~AttachedThread() { Reset(); }
    void Reset() {
        if (!thread) return;
        gkdiag::Log().Checkpoint("thread_detach");
        g_api.thread_detach(thread);
        thread = nullptr;
    }
};

// ---- small utilities --------------------------------------------------------

std::string Utf16ToUtf8(const uint16_t* chars, int len) {
    if (!chars || len <= 0) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, (LPCWCH)chars, len, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return std::string();
    std::string out;
    out.resize((size_t)n);
    WideCharToMultiByte(CP_UTF8, 0, (LPCWCH)chars, len, &out[0], n, nullptr, nullptr);
    return out;
}

std::string JsonEscape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (unsigned char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b";  break;
            case '\f': out += "\\f";  break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += (char)c;
                }
        }
    }
    return out;
}

// ---- reflection helpers -----------------------------------------------------

void* FindMethodInHierarchy(void* klass, const char* name, int argc) {
    void* k = klass;
    while (k) {
        void* m = g_api.class_get_method_from_name(k, name, argc);
        if (m) return m;
        k = g_api.class_get_parent(k);
    }
    return nullptr;
}

struct Field {
    void* method = nullptr;
    void* retType = nullptr;
    int   tt     = -1;   // il2cpp_type_get_type() result, or -1
};

Field GetField(void* klass, const char* name, int argc = 0) {
    Field f;
    void* m = FindMethodInHierarchy(klass, name, argc);
    if (m) {
        f.method = m;
        void* rt = g_api.method_get_return_type(m);
        f.retType = rt;
        if (rt) f.tt = g_api.type_get_type(rt);
    }
    return f;
}

struct InvokeResult {
    void*       value = nullptr;
    std::string exc;
};

InvokeResult Invoke(void* method, void* self, void** args) {
    InvokeResult r;
    const DWORD start = g_workerStartMs.load(std::memory_order_relaxed);
    if (start && (GetTickCount() - start) > kMaxWaitMs) {
        r.exc = "timeout";
        return r;
    }
    void* exc = nullptr;
    r.value = g_api.runtime_invoke(method, self, args, &exc);
    if (exc) {
        char buf[kMaxExcMsg] = {0};
        g_api.format_exception(exc, buf, kMaxExcMsg);
        r.exc = buf;
    }
    return r;
}

bool ReadStringField(void* self, const Field& f, std::string& out, std::string& err) {
    if (!f.method)  { err = "missing"; return false; }
    if (f.tt != 0x0e) { err = "not string"; return false; }
    auto r = Invoke(f.method, self, nullptr);
    if (!r.exc.empty()) { err = r.exc; return false; }
    if (!r.value) { out.clear(); return true; }   // null string -> empty
    int len = g_api.string_length(r.value);
    uint16_t* chars = g_api.string_chars(r.value);
    out = Utf16ToUtf8(chars, len);
    return true;
}

bool ReadIntField(void* self, const Field& f, int64_t& out, std::string& err) {
    if (!f.method) { err = "missing"; return false; }
    int tt = f.tt;
    if (tt == 0x11) {
        if (!f.retType) { err = "no type"; return false; }
        void* klass = g_api.class_from_type(f.retType);
        if (!klass) { err = "class_from_type failed"; return false; }
        if (!g_api.class_is_enum(klass)) { err = "not enum"; return false; }
        void* base = g_api.class_enum_basetype(klass);
        if (!base) { err = "no enum base"; return false; }
        tt = g_api.type_get_type(base);
    }
    if (tt != 0x08 && tt != 0x09 && tt != 0x0a && tt != 0x0b) {
        err = "unsupported type";
        return false;
    }
    auto r = Invoke(f.method, self, nullptr);
    if (!r.exc.empty()) { err = r.exc; return false; }
    if (!r.value) { err = "null"; return false; }
    void* u = g_api.object_unbox(r.value);
    if (!u) { err = "unbox failed"; return false; }
    if (tt == 0x08) { out = (int64_t)*(int32_t*)u;  return true; }  // I4
    if (tt == 0x09) { out = (int64_t)*(uint32_t*)u; return true; }  // U4
    if (tt == 0x0a) { out = *(int64_t*)u;           return true; }  // I8
    uint64_t v = *(uint64_t*)u;                                     // U8
    if (v > (uint64_t)INT64_MAX) { err = "U8 overflow"; return false; }
    out = (int64_t)v;
    return true;
}

void* FindAssemblyImage(void* domain, const char* name) {
    size_t n = 0;
    gkdiag::Log().Checkpoint("domain_get_assemblies");
    void** assemblies = g_api.domain_get_assemblies(domain, &n);
    if (!assemblies) return nullptr;
    for (size_t i = 0; i < n; i++) {
        gkdiag::Log().Checkpoint("assembly_get_image");
        void* img = g_api.assembly_get_image(assemblies[i]);
        if (!img) continue;
        gkdiag::Log().Checkpoint("image_get_name", name);
        const char* n2 = g_api.image_get_name(img);
        if (n2 && std::strcmp(n2, name) == 0) return img;
    }
    return nullptr;
}

bool WriteFileBytes(const std::wstring& path, const std::string& data) {
    HANDLE h = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr,
                           CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    DWORD written = 0;
    BOOL ok = WriteFile(h, data.data(), (DWORD)data.size(), &written, nullptr);
    CloseHandle(h);
    return ok && written == (DWORD)data.size();
}

struct ExportResult {
    std::string status;
    std::string message;
    int         records      = 0;
    bool        dataComplete = false;
    std::string memoriesJson;
};

// Serialized publication. Only the first terminal publication wins; a late
// worker can never overwrite a watchdog timeout (and vice versa) because both
// paths take g_publishMutex and honour g_terminalPublished. A failed/timed-out
// run never writes memories.json, so a prior successful snapshot is preserved.
void WriteResults(const ExportResult& r, const std::string& phase, bool terminal) {
    std::lock_guard<std::mutex> publishLock(g_publishMutex);
    if (g_terminalPublished.load(std::memory_order_acquire)) return;

    const auto directory = OutputDirectory();
    if (directory.empty()) {
        if (terminal) g_terminalPublished.store(true, std::memory_order_release);
        return;
    }
    const bool lateSuccess = r.dataComplete && ElapsedSince() >= kWatchdogMs;
    const bool finish = terminal || lateSuccess;
    std::string status    = lateSuccess ? "timeout" : r.status;
    bool        completed = r.dataComplete && !lateSuccess;
    const std::string message = lateSuccess ? "export completed after diagnostic deadline; snapshot not published" : r.message;

    if (completed) {
        std::wstring mtmp = directory + L"\\memories.json.tmp";
        std::wstring mfin = directory + L"\\memories.json";
        bool ok = false;
        if (WriteFileBytes(mtmp, r.memoriesJson)) {
            if (MoveFileExW(mtmp.c_str(), mfin.c_str(), MOVEFILE_REPLACE_EXISTING)) {
                ok = true;
            }
        }
        if (!ok) {
            status    = "io_error";
            completed = false;
            // Old memories.json intentionally left in place; status makes it clear it is stale.
        }
    }

    const auto hash = completed ? SnapshotHash(r.memoriesJson) : "";
    if (completed && hash.empty()) { status = "io_error"; completed = false; }

    std::string statusJson =
        std::string("{\"schema_version\":2,\"plugin_version\":\"1.0.7-diagnostic\",\"snapshot_sha256\":\"") + hash +
        "\",\"status\":\"" + JsonEscape(status) +
        "\",\"message\":\"" + JsonEscape(message) +
        "\",\"records\":" + std::to_string(lateSuccess ? 0 : r.records) +
        ",\"completed\":" + (completed ? "true" : "false") +
        ",\"phase\":\"" + JsonEscape(phase) +
        "\",\"elapsed_ms\":" + std::to_string(ElapsedSince()) +
        ",\"pid\":" + std::to_string(GetCurrentProcessId()) + "}";

    std::wstring tmp = directory + L"\\status.json.tmp";
    std::wstring fin = directory + L"\\status.json";
    if (WriteFileBytes(tmp, statusJson)) {
        MoveFileExW(tmp.c_str(), fin.c_str(), MOVEFILE_REPLACE_EXISTING);
    }

    if (finish) g_terminalPublished.store(true, std::memory_order_release);
}

// ---- per-record serialization ----------------------------------------------

struct RecBuf {
    std::ostringstream oss;
};

void SerializeCustomizes(void* custColl, RecBuf& rb);
void SerializeCard(void* card, RecBuf& rb);
void SerializeCards(void* cardsColl, RecBuf& rb);

void SerializeCards(void* cardsColl, RecBuf& rb) {
    if (!cardsColl) { rb.oss << "[]"; return; }
    void* cls = g_api.object_get_class(cardsColl);
    if (!cls) throw std::runtime_error("cards class missing");
    void* mCount = FindMethodInHierarchy(cls, "get_Count", 0);
    void* mItem  = FindMethodInHierarchy(cls, "get_Item", 1);
    if (!mCount || !mItem) throw std::runtime_error("cards accessors missing");
    auto rC = Invoke(mCount, cardsColl, nullptr);
    if (!rC.exc.empty()) throw std::runtime_error("cards count exc: " + rC.exc);
    if (!rC.value)       throw std::runtime_error("cards count null");
    void* u = g_api.object_unbox(rC.value);
    if (!u) throw std::runtime_error("cards count unbox failed");
    int32_t n = *(int32_t*)u;
    if (n < 0 || n > kMaxRecords) throw std::runtime_error("cards count out of range");
    rb.oss << "[";
    for (int32_t i = 0; i < n; i++) {
        int32_t idx = i;
        void* args[1] = { &idx };
        auto rIt = Invoke(mItem, cardsColl, args);
        if (!rIt.exc.empty()) throw std::runtime_error("card get_Item exc: " + rIt.exc);
        if (!rIt.value)       throw std::runtime_error("card get_Item null");
        rb.oss << (i ? "," : "") << "{";
        SerializeCard(rIt.value, rb);
        rb.oss << "}";
    }
    rb.oss << "]";
}

void SerializeCard(void* card, RecBuf& rb) {
    void* cls = g_api.object_get_class(card);
    if (!cls) throw std::runtime_error("card class missing");
    Field fId   = GetField(cls, "get_Id");
    Field fUpg  = GetField(cls, "get_UpgradeCount");
    Field fCust = GetField(cls, "get_Customizes");

    std::string sId, err;
    int64_t upg = 0;
    if (!ReadStringField(card, fId, sId, err)) throw std::runtime_error("card Id: " + err);
    if (sId.empty()) throw std::runtime_error("card Id empty");
    if (!ReadIntField(card, fUpg, upg, err))  throw std::runtime_error("card UpgradeCount: " + err);

    rb.oss << "\"Id\":\"" << JsonEscape(sId) << "\",";
    rb.oss << "\"UpgradeCount\":" << upg << ",";
    rb.oss << "\"Customizes\":";
    if (!fCust.method) throw std::runtime_error("card get_Customizes missing");
    auto r = Invoke(fCust.method, card, nullptr);
    if (!r.exc.empty()) throw std::runtime_error("card Customizes exc: " + r.exc);
    if (!r.value)       throw std::runtime_error("card Customizes null");
    SerializeCustomizes(r.value, rb);
}

void SerializeCustomizes(void* custColl, RecBuf& rb) {
    if (!custColl) { rb.oss << "[]"; return; }
    void* cls = g_api.object_get_class(custColl);
    if (!cls) throw std::runtime_error("customizes class missing");
    void* mCount = FindMethodInHierarchy(cls, "get_Count", 0);
    void* mItem  = FindMethodInHierarchy(cls, "get_Item", 1);
    if (!mCount || !mItem) throw std::runtime_error("customizes accessors missing");
    auto rC = Invoke(mCount, custColl, nullptr);
    if (!rC.exc.empty()) throw std::runtime_error("customizes count exc: " + rC.exc);
    if (!rC.value)       throw std::runtime_error("customizes count null");
    void* u = g_api.object_unbox(rC.value);
    if (!u) throw std::runtime_error("customizes count unbox failed");
    int32_t n = *(int32_t*)u;
    if (n < 0 || n > kMaxRecords) throw std::runtime_error("customizes count out of range");
    rb.oss << "[";
    for (int32_t i = 0; i < n; i++) {
        int32_t idx = i;
        void* args[1] = { &idx };
        auto rIt = Invoke(mItem, custColl, args);
        if (!rIt.exc.empty()) throw std::runtime_error("customize get_Item exc: " + rIt.exc);
        if (!rIt.value)       throw std::runtime_error("customize get_Item null");
        rb.oss << (i ? "," : "") << "{";
        void* ccls = g_api.object_get_class(rIt.value);
        if (!ccls) throw std::runtime_error("customize class missing");
        Field fId = GetField(ccls, "get_Id");
        Field fCn = GetField(ccls, "get_CustomizeCount");
        std::string sid, e;
        int64_t cnt = 0;
        if (!ReadStringField(rIt.value, fId, sid, e)) throw std::runtime_error("customize Id: " + e);
        if (sid.empty()) throw std::runtime_error("customize Id empty");
        if (!ReadIntField(rIt.value, fCn, cnt, e))   throw std::runtime_error("customize count: " + e);
        rb.oss << "\"Id\":\"" << JsonEscape(sid) << "\",\"CustomizeCount\":" << cnt;
        rb.oss << "}";
    }
    rb.oss << "]";
}

void SerializeItemIds(void* item, const Field& fids, RecBuf& rb) {
    rb.oss << "\"ExamBattleProduceItemIds\":";
    if (!fids.method) { rb.oss << "null"; return; }
    rb.oss << "[";
    auto r = Invoke(fids.method, item, nullptr);
    if (!r.exc.empty()) throw std::runtime_error("ItemIds exc: " + r.exc);
    if (!r.value)       throw std::runtime_error("ItemIds null");
    void* coll = r.value;
    void* cls = g_api.object_get_class(coll);
    if (!cls) throw std::runtime_error("ItemIds class missing");
    void* mCount = FindMethodInHierarchy(cls, "get_Count", 0);
    void* mItem  = FindMethodInHierarchy(cls, "get_Item", 1);
    if (!mCount || !mItem) throw std::runtime_error("ItemIds accessors missing");
    auto rC = Invoke(mCount, coll, nullptr);
    if (!rC.exc.empty()) throw std::runtime_error("ItemIds count exc: " + rC.exc);
    if (!rC.value)       throw std::runtime_error("ItemIds count null");
    void* u = g_api.object_unbox(rC.value);
    if (!u) throw std::runtime_error("ItemIds count unbox failed");
    int32_t n = *(int32_t*)u;
    if (n < 0 || n > kMaxRecords) throw std::runtime_error("ItemIds count out of range");
    bool first = true;
    for (int32_t i = 0; i < n; i++) {
        int32_t idx = i;
        void* args[1] = { &idx };
        auto rIt = Invoke(mItem, coll, args);
        if (!rIt.exc.empty()) throw std::runtime_error("ItemIds get_Item exc: " + rIt.exc);
        if (!rIt.value)       throw std::runtime_error("ItemIds get_Item null");
        void* rt = g_api.method_get_return_type(mItem);
        int tt = rt ? g_api.type_get_type(rt) : -1;
        if (tt != 0x0e) throw std::runtime_error("ItemIds item not string");
        int len = g_api.string_length(rIt.value);
        uint16_t* chars = g_api.string_chars(rIt.value);
        std::string s = Utf16ToUtf8(chars, len);
        rb.oss << (first ? "" : ",") << "\"" << JsonEscape(s) << "\"";
        first = false;
    }
    rb.oss << "]";
}

// ---- worker -----------------------------------------------------------------

unsigned __stdcall ExportWorker(void*) {
    // Earliest possible worker marker: recorded before any OutputDirectory(),
    // filesystem or C++ logging call so a stalled worker is distinguishable
    // from one that never entered.
    gkearly::Log("worker_entered");
    const DWORD t0 = GetTickCount();
    g_workerStartMs.store(t0, std::memory_order_relaxed);
    gkdiag::Log().Start(OutputDirectory(), GetCurrentProcessId());
    gkdiag::Log().Checkpoint("startup");

    // Marks the worker finished and wakes the watchdog on every return path.
    struct DoneGuard {
        ~DoneGuard() {
            g_workerFinished.store(true, std::memory_order_release);
            if (g_workerDoneEvent) SetEvent(g_workerDoneEvent);
        }
    } doneGuard;

    ExportResult res;
    res.status  = "error";
    res.message = "not initialized";
    res.status = "running";
    res.message = "waiting for game data (maximum 180 seconds)";
    WriteResults(res, gkdiag::Log().LastPhaseSummary(), false);

    // Phase 1: native readiness gate. No IL2CPP API is touched until the
    // application-specific late module is observed, bounded by the same overall
    // budget as the watchdog (no separate retries, no extra sleeps as a
    // readiness substitute). A missing signal is a controlled, terminal
    // "not_loaded" outcome: ResolveApi/domain_get/thread_attach/enumeration are
    // never called in that case.
    bool signalSeen = false;
    while (GetTickCount() - t0 < kMaxWaitMs) {
        gkdiag::Log().Checkpoint("native_runtime_wait", kRuntimeSignalModule);
        if (RuntimeSignalPresent()) { signalSeen = true; break; }
        Sleep(kPollMs);
    }
    if (!signalSeen) {
        res.status       = "not_loaded";
        res.message      = "runtime readiness signal not observed";
        res.records      = 0;
        res.dataComplete = false;
        WriteResults(res, gkdiag::Log().LastPhaseSummary(), true);
        return 0;
    }
    gkdiag::Log().Checkpoint("native_runtime_signal_seen");

    HMODULE ga = nullptr;
    void* domain = nullptr;
    void* img    = nullptr;

    // Owns the single attach handle; detaches exactly once on every non-fatal
    // exit path (declared outside try so the destructor also runs after a catch).
    AttachedThread attached;

    // The startup poll now lives inside try/catch so a controlled failure
    // (e.g. a null thread_attach) is published through the same result path.
    // Only normal C++ exceptions are caught; SEH is left unhandled so a GC
    // fatal fault is never suppressed.
    try {
        while (GetTickCount() - t0 < kMaxWaitMs) {
            // Gate re-checked before domain_get on every loop iteration.
            if (RuntimeSignalPresent()) {
                gkdiag::Log().Checkpoint("get_module_handle", "GameAssembly.dll");
                ga = GetModuleHandleW(L"GameAssembly.dll");
                if (ga && ResolveApi(ga)) {
                    gkdiag::Log().Checkpoint("domain_get");
                    void* d = g_api.domain_get();
                    if (d) {
                        // Attach before FindAssemblyImage or ANY metadata API,
                        // once per worker; preserve the thread across polling.
                        if (!attached.thread) {
                            gkdiag::Log().Checkpoint("thread_attach");
                            attached.thread = g_api.thread_attach(d);
                            if (!attached.thread)
                                throw std::runtime_error("thread_attach failed");
                        }
                        void* i = FindAssemblyImage(d, "Assembly-CSharp.dll");
                        if (i) { domain = d; img = i; break; }
                    }
                }
            }
            Sleep(kPollMs);
        }

        if (!domain || !img) {
            res.status  = "not_loaded";
            res.message = "GameAssembly/Assembly-CSharp not ready within timeout";
            WriteResults(res, gkdiag::Log().LastPhaseSummary(), true);
            return 0;
        }
        gkdiag::Log().Checkpoint("assembly_image_ready");

        gkdiag::Log().Checkpoint("class_lookup_userdatamanager");
        void* udmClass = g_api.class_from_name(img, "Campus.Common.User", "UserDataManager");
        if (!udmClass)
            udmClass = g_api.class_from_name(img, "", "Campus.Common.User.UserDataManager");
        if (!udmClass) throw std::runtime_error("UserDataManager class not found");
        gkdiag::Log().Checkpoint("userdatamanager_ready");

        void* getHasInstance = FindMethodInHierarchy(udmClass, "get_HasInstance", 0);
        if (!getHasInstance) throw std::runtime_error("get_HasInstance not found");

        bool hasInstance = false;
        while (GetTickCount() - t0 < kMaxWaitMs) {
            gkdiag::Log().Checkpoint("getter_has_instance");
            auto rH = Invoke(getHasInstance, nullptr, nullptr);
            if (!rH.exc.empty()) throw std::runtime_error("get_HasInstance exc: " + rH.exc);
            if (rH.value) {
                void* hu = g_api.object_unbox(rH.value);
                if (hu && *(bool*)hu) { hasInstance = true; break; }
            }
            Sleep(kPollMs);
        }
        if (!hasInstance) {
            res.status       = "not_loaded";
            res.message      = "get_HasInstance not true within timeout";
            res.records      = 0;
            res.dataComplete = false;
            WriteResults(res, gkdiag::Log().LastPhaseSummary(), true);
            return 0;
        }

        void* getInstance = FindMethodInHierarchy(udmClass, "get_Instance", 0);
        if (!getInstance) throw std::runtime_error("get_Instance not found");

        gkdiag::Log().Checkpoint("getter_instance");
        auto rInst = Invoke(getInstance, nullptr, nullptr);
        if (!rInst.exc.empty()) throw std::runtime_error("get_Instance exc: " + rInst.exc);
        if (!rInst.value)       throw std::runtime_error("get_Instance returned null");

        void* manager      = rInst.value;
        void* managerClass = g_api.object_get_class(manager);
        if (!managerClass) throw std::runtime_error("manager class missing");

        void* getUserMemoryList = FindMethodInHierarchy(managerClass, "get_UserMemoryList", 0);
        if (!getUserMemoryList)
            getUserMemoryList = FindMethodInHierarchy(managerClass, "get__userMemoryList", 0);
        if (!getUserMemoryList) throw std::runtime_error("UserMemoryList getter not found");

        void*   coll  = nullptr;
        void*   list  = nullptr;
        int32_t total = 0;

        while (GetTickCount() - t0 < kMaxWaitMs) {
            gkdiag::Log().Checkpoint("getter_user_memory_list");
            auto rColl = Invoke(getUserMemoryList, manager, nullptr);
            if (!rColl.exc.empty()) throw std::runtime_error("UserMemoryList exc: " + rColl.exc);
            if (!rColl.value) { Sleep(kPollMs); continue; }
            void* c = rColl.value;
            void* collClass = g_api.object_get_class(c);
            if (!collClass) { Sleep(kPollMs); continue; }
            void* getAllList = FindMethodInHierarchy(collClass, "GetAllList", 1);
            if (!getAllList) throw std::runtime_error("GetAllList not found");
            void* argsAll[1] = { nullptr };
            gkdiag::Log().Checkpoint("getter_get_all_list");
            auto rList = Invoke(getAllList, c, argsAll);
            if (!rList.exc.empty()) throw std::runtime_error("GetAllList exc: " + rList.exc);
            if (!rList.value) { Sleep(kPollMs); continue; }
            void* l = rList.value;
            void* listClass = g_api.object_get_class(l);
            if (!listClass) { Sleep(kPollMs); continue; }
            void* getCount = FindMethodInHierarchy(listClass, "get_Count", 0);
            if (!getCount) throw std::runtime_error("list get_Count missing");
            gkdiag::Log().Checkpoint("getter_count");
            auto rCount = Invoke(getCount, l, nullptr);
            if (!rCount.exc.empty()) throw std::runtime_error("Count exc: " + rCount.exc);
            if (!rCount.value) { Sleep(kPollMs); continue; }
            void* u = g_api.object_unbox(rCount.value);
            if (!u) { Sleep(kPollMs); continue; }
            int32_t n = *(int32_t*)u;
            if (n < 0) throw std::runtime_error("memory count negative");
            if (n > 0) { coll = c; list = l; total = n; break; }
            Sleep(kPollMs);
        }

        if (total <= 0) {
            res.status       = "not_loaded";
            res.message      = "memory list empty within timeout";
            res.records      = 0;
            res.dataComplete = false;
            WriteResults(res, gkdiag::Log().LastPhaseSummary(), true);
            return 0;
        }
        (void)coll;

        void* listClass = g_api.object_get_class(list);
        if (!listClass) throw std::runtime_error("list class missing");
        void* getItem = FindMethodInHierarchy(listClass, "get_Item", 1);
        if (!getItem) throw std::runtime_error("list get_Item missing");

        if (total > kMaxRecords) throw std::runtime_error("memory list exceeds max records");

        std::ostringstream mem;
        mem << "[";
        int  nOut    = 0;
        std::unordered_set<std::string> memoryIds;
        // Pure URL formatting only: no image downloads or game cache writes.
        gkdiag::Log().Checkpoint("class_lookup_photo");
        void* photoClass = g_api.class_from_name(img, "Campus.Common.Photo", "PhotoImageManager");
        Field photoUrl = photoClass ? GetField(photoClass, "CreateDownloadTextureUrl", 1) : Field{};

        for (int32_t i = 0; i < total; i++) {
            if (GetTickCount() - t0 > kMaxWaitMs) throw std::runtime_error("timeout during enumeration");
            gkdiag::Log().Checkpoint("enumeration");
            int32_t idx = i;
            void* args[1] = { &idx };
            auto rIt = Invoke(getItem, list, args);
            if (!rIt.exc.empty()) throw std::runtime_error("get_Item exc: " + rIt.exc);
            if (!rIt.value)       throw std::runtime_error("get_Item null");

            void* item      = rIt.value;
            void* itemClass = g_api.object_get_class(item);
            if (!itemClass) throw std::runtime_error("item class missing");

            Field fId      = GetField(itemClass, "get_UserMemoryId");
            Field fChar    = GetField(itemClass, "get_CharacterId");
            Field fCard    = GetField(itemClass, "get_IdolCardId");
            Field fSkin    = GetField(itemClass, "get_IdolCardSkinId");   // optional
            Field fTag     = GetField(itemClass, "get_MemoryTagId");      // optional
            Field fImage   = GetField(itemClass, "get_ImagePath");        // optional photo metadata
            Field fAsset   = GetField(itemClass, "get_AssetId");          // optional photo metadata
            Field fVcl     = GetField(itemClass, "get_Vocal");
            Field fDnc     = GetField(itemClass, "get_Dance");
            Field fVis     = GetField(itemClass, "get_Visual");
            Field fStm     = GetField(itemClass, "get_Stamina");
            Field fGrd     = GetField(itemClass, "get_Grade");
            Field fPwr     = GetField(itemClass, "get_Power");
            Field fCards   = GetField(itemClass, "get_ExamBattleProduceCards");
            Field fItemIds = GetField(itemClass, "get_ExamBattleProduceItemIds"); // optional

            std::string sId, sChar, sCard, sSkin, sTag, sImage, sAsset, sPhotoKey, err;
            int64_t nVcl = 0, nDnc = 0, nVis = 0, nStm = 0, nGrd = 0, nPwr = 0;

            if (!ReadStringField(item, fId,   sId,   err)) throw std::runtime_error("UserMemoryId: " + err);
            if (sId.empty()) throw std::runtime_error("UserMemoryId empty");
            if (!memoryIds.insert(sId).second) throw std::runtime_error("duplicate UserMemoryId");
            if (!ReadStringField(item, fChar, sChar, err)) throw std::runtime_error("CharacterId: "  + err);
            if (sChar.empty()) throw std::runtime_error("CharacterId empty");
            if (!ReadStringField(item, fCard, sCard, err)) throw std::runtime_error("IdolCardId: "   + err);
            if (sCard.empty()) throw std::runtime_error("IdolCardId empty");
            if (fSkin.method) { if (!ReadStringField(item, fSkin, sSkin, err)) throw std::runtime_error("IdolCardSkinId: " + err); }
            if (fTag.method)  { if (!ReadStringField(item, fTag,  sTag,  err)) throw std::runtime_error("MemoryTagId: " + err); }
            // A missing/failed optional photo getter must not invalidate simulation data.
            if (fImage.method && !ReadStringField(item, fImage, sImage, err)) sImage.clear();
            if (fAsset.method && !ReadStringField(item, fAsset, sAsset, err)) sAsset.clear();
            if (!sImage.empty() && photoUrl.method && photoUrl.tt == 0x0e) {
                gkdiag::Log().Checkpoint("getter_photo_url");
                auto image = Invoke(fImage.method, item, nullptr);
                if (image.exc.empty() && image.value) {
                    void* photoArgs[1] = { image.value };
                    auto url = Invoke(photoUrl.method, nullptr, photoArgs);
                    if (url.exc.empty() && url.value) {
                        const int length = g_api.string_length(url.value);
                        if (length > 0 && length <= 4096)
                            sPhotoKey = PhotoCacheHash(Utf16ToUtf8(g_api.string_chars(url.value), length));
                    }
                }
            }

            if (!ReadIntField(item, fVcl, nVcl, err)) throw std::runtime_error("Vocal: "   + err);
            if (!ReadIntField(item, fDnc, nDnc, err)) throw std::runtime_error("Dance: "   + err);
            if (!ReadIntField(item, fVis, nVis, err)) throw std::runtime_error("Visual: "  + err);
            if (!ReadIntField(item, fStm, nStm, err)) throw std::runtime_error("Stamina: " + err);
            if (!ReadIntField(item, fGrd, nGrd, err)) throw std::runtime_error("Grade: "   + err);
            if (!ReadIntField(item, fPwr, nPwr, err)) throw std::runtime_error("Power: "   + err);

            RecBuf rb;
            rb.oss << "{";
            rb.oss << "\"UserMemoryId\":\""   << JsonEscape(sId)   << "\",";
            rb.oss << "\"CharacterId\":\""    << JsonEscape(sChar) << "\",";
            rb.oss << "\"IdolCardId\":\""     << JsonEscape(sCard) << "\",";
            rb.oss << "\"IdolCardSkinId\":\"" << JsonEscape(sSkin) << "\",";
            rb.oss << "\"MemoryTagId\":\""    << JsonEscape(sTag)  << "\",";
            rb.oss << "\"ImagePath\":\""      << JsonEscape(sImage) << "\",";
            rb.oss << "\"AssetId\":\""        << JsonEscape(sAsset) << "\",";
            rb.oss << "\"PhotoCacheKey\":\""  << JsonEscape(sPhotoKey) << "\",";
            rb.oss << "\"Vocal\":"   << nVcl << ",";
            rb.oss << "\"Dance\":"   << nDnc << ",";
            rb.oss << "\"Visual\":"  << nVis << ",";
            rb.oss << "\"Stamina\":" << nStm << ",";
            rb.oss << "\"Grade\":"   << nGrd << ",";
            rb.oss << "\"Power\":"   << nPwr << ",";
            rb.oss << "\"ExamBattleProduceCards\":";
            if (!fCards.method) throw std::runtime_error("ExamBattleProduceCards getter missing");
            auto rCards = Invoke(fCards.method, item, nullptr);
            if (!rCards.exc.empty()) throw std::runtime_error("ExamBattleProduceCards exc: " + rCards.exc);
            if (!rCards.value)       throw std::runtime_error("ExamBattleProduceCards null");
            SerializeCards(rCards.value, rb);
            rb.oss << ",";
            SerializeItemIds(item, fItemIds, rb);
            rb.oss << "}";

            mem << (nOut ? "," : "") << rb.oss.str();
            nOut++;
        }

        mem << "]";
        res.records      = nOut;
        res.memoriesJson = mem.str();

        res.status       = "completed";
        res.message.clear();
        res.dataComplete = true;
    } catch (const std::exception& e) {
        res.status       = "incomplete";
        res.message      = std::string("exception: ") + e.what();
        res.dataComplete = false;
    } catch (...) {
        res.status       = "incomplete";
        res.message      = "unknown exception";
        res.dataComplete = false;
    }

    // The attached thread is released exactly once by ~AttachedThread on the way
    // out (including the catch paths above); there is no explicit detach here.
    gkdiag::Log().Checkpoint("result_write", res.status.c_str());
    WriteResults(res, gkdiag::Log().LastPhaseSummary(), true);
    return 0;
}

// ---- watchdog ---------------------------------------------------------------
//
// Independent of the worker: performs no IL2CPP calls, never waits on the
// (possibly blocked) worker thread, never terminates it and never changes game
// state. It only waits on a completion event and, on deadline, publishes a
// terminal timeout status. The deadline is the overall 3 minute budget.
unsigned __stdcall WatchdogWorker(void*) {
    if (g_workerDoneEvent) {
        const DWORD wait = WaitForSingleObject(g_workerDoneEvent, kWatchdogMs);
        if (wait == WAIT_OBJECT_0) return 0;   // worker finished cleanly; stop early
    }
    if (g_workerFinished.load(std::memory_order_acquire)) return 0;

    // Capture where the worker stalled before recording the deadline event.
    const std::string lastPhase = gkdiag::Log().LastPhaseSummary();
    gkdiag::Log().Event("watchdog", "deadline_reached");

    ExportResult res;
    res.status       = "timeout";
    res.message      = std::string("export watchdog deadline reached; last phase: ") +
                       (lastPhase.empty() ? std::string("unknown") : lastPhase);
    res.records      = 0;
    res.dataComplete = false;
    WriteResults(res, lastPhase, true);
    return 0;
}

bool IsTargetProcess() {
#if GK_EXPORT_TEST_ALLOW_NON_TARGET
    return true;
#else
    wchar_t path[MAX_PATH] = {0};
    DWORD n = GetModuleFileNameW(nullptr, path, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return false;
    std::wstring full(path, n);
    size_t pos = full.find_last_of(L"\\/");
    std::wstring base = (pos == std::wstring::npos) ? full : full.substr(pos + 1);
    for (auto& c : base) c = (wchar_t)towlower(c);
    return base == L"gakumas.exe";
#endif
}

} // namespace

extern "C" __declspec(dllexport) void StartMemoryExportOnce() {
    // Entry marker precedes the target gate so a non-target host still proves
    // the shared entrypoint ran (the helper itself stays silent off-target).
    gkearly::Log("start_memory_export_entry");
    bool expected = false;
    // g_started is never cleared: whether the first start came from the
    // bootstrap thread or the lazy Initialize path, a concurrent legacy trigger
    // finds it set and returns, so exactly one exporter ever runs.
    if (!g_started.compare_exchange_strong(expected, true)) return;
    gkearly::Log("start_memory_export_started");

    if (!IsTargetProcess()) {
        gkearly::Log("start_memory_export_non_target");
        return;
    }
    gkearly::Log("start_memory_export_target");

    if (!g_workerDoneEvent) {
        gkearly::Log("event_create_begin");
        g_workerDoneEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (!g_workerDoneEvent) {
            const DWORD eventError = GetLastError();
            LogOsError("event_create_failed", eventError);
            gkearly::Log("event_create_failed", nullptr, eventError);
        } else {
            gkearly::Log("event_create_ok");
        }
    }

    // Bounded worker creation: a transient _beginthreadex failure is retried at
    // most kMaxWorkerCreateAttempts times, with a short Sleep *between*
    // failures only (never inside DllMain). A successfully created worker is
    // never retried and g_started is never cleared, so a partial start can never
    // spawn a second exporter.
    constexpr int kMaxWorkerCreateAttempts = 3;
    static const char* const kCreateAttemptDetail[kMaxWorkerCreateAttempts] = {
        "attempt=1", "attempt=2", "attempt=3",
    };

    uintptr_t h = 0;
    for (int attempt = 0; attempt < kMaxWorkerCreateAttempts; ++attempt) {
        gkearly::Log("worker_beginthreadex_begin");
        gkearly::Log("worker_beginthreadex_attempt", kCreateAttemptDetail[attempt]);
        h = _beginthreadex(nullptr, 0, &ExportWorker, nullptr, 0, nullptr);
        if (h) break;
        // Capture the OS error immediately, before any other Win32 call.
        const DWORD threadError = GetLastError();
        LogOsError("worker_thread_failed", threadError);
        gkearly::Log("worker_beginthreadex_failed", nullptr, threadError);
        if (attempt + 1 < kMaxWorkerCreateAttempts) {
            gkearly::Log("worker_beginthreadex_retry");
            Sleep(100);
        }
    }
    if (!h) {
        // Every bounded attempt failed: publish one terminal error. The failed
        // run never writes memories.json, so a prior successful snapshot is
        // preserved.
        ExportResult res;
        res.status       = "error";
        res.message      = "beginthread failed";
        res.records      = 0;
        res.dataComplete = false;
        WriteResults(res, gkdiag::Log().LastPhaseSummary(), true);
        return;
    }
    gkearly::Log("worker_beginthreadex_ok");
    CloseHandle((HANDLE)h);

    // The watchdog is optional only in the sense that it is skipped if the
    // completion event could not be created (nothing else to synchronize on).
    if (g_workerDoneEvent) {
        gkearly::Log("watchdog_beginthreadex_begin");
        uintptr_t w = _beginthreadex(nullptr, 0, &WatchdogWorker, nullptr, 0, nullptr);
        if (!w) {
            const DWORD watchdogError = GetLastError();
            LogOsError("watchdog_thread_failed", watchdogError);
            gkearly::Log("watchdog_beginthreadex_failed", nullptr, watchdogError);
        } else {
            gkearly::Log("watchdog_beginthreadex_ok");
            CloseHandle((HANDLE)w);
        }
    }
}
