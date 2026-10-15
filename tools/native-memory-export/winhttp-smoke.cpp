// tools/native-memory-export/winhttp-smoke.cpp
//
// Offline validation for the winhttp.dll coexistence proxy.
//
// Loads the built proxy by full path, confirms the four imports UnityPlayer.dll
// uses explicitly plus the full advertised baseline resolve, then compares the
// observable behaviour of the proxy against a directly loaded
// System32\winhttp.dll. Every call either succeeds locally (session open,
// option query, default/IE proxy configuration, platform/time helpers) or fails
// on parameter validation before any I/O, so no network traffic is produced.
// LastError is seeded before each call so the comparisons are deterministic.
//
// Usage: winhttp-smoke.exe <path-to-built-winhttp.dll>

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winhttp.h>
#include <cstdio>
#include <cwchar>
#include <string>

namespace {

int g_failures = 0;

void Check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) ++g_failures;
}

// Seeded before every forwarded call so LastError comparisons are deterministic.
constexpr DWORD kSeed = 0x5EED;

typedef HINTERNET (WINAPI* tOpen)(LPCWSTR, DWORD, LPCWSTR, LPCWSTR, DWORD);
typedef BOOL (WINAPI* tCloseHandle)(HINTERNET);
typedef BOOL (WINAPI* tQueryOption)(HINTERNET, DWORD, LPVOID, LPDWORD);
typedef BOOL (WINAPI* tGetDefaultProxyConfiguration)(WINHTTP_PROXY_INFO*);
typedef BOOL (WINAPI* tGetIEProxyConfigForCurrentUser)(WINHTTP_CURRENT_USER_IE_PROXY_CONFIG*);
typedef BOOL (WINAPI* tGetProxyForUrl)(HINTERNET, LPCWSTR, WINHTTP_AUTOPROXY_OPTIONS*, WINHTTP_PROXY_INFO*);
typedef BOOL (WINAPI* tCheckPlatform)();
typedef BOOL (WINAPI* tTimeFromSystemTime)(const SYSTEMTIME*, LPWSTR);

struct Api {
    tOpen Open = nullptr;
    tCloseHandle CloseHandle = nullptr;
    tQueryOption QueryOption = nullptr;
    tGetDefaultProxyConfiguration GetDefaultProxyConfiguration = nullptr;
    tGetIEProxyConfigForCurrentUser GetIEProxyConfigForCurrentUser = nullptr;
    tGetProxyForUrl GetProxyForUrl = nullptr;
    tCheckPlatform CheckPlatform = nullptr;
    tTimeFromSystemTime TimeFromSystemTime = nullptr;

    bool Complete() const {
        return Open && CloseHandle && QueryOption && GetDefaultProxyConfiguration &&
               GetIEProxyConfigForCurrentUser && GetProxyForUrl && CheckPlatform && TimeFromSystemTime;
    }
};

template <typename T>
T Bind(HMODULE module, const char* name) {
    return reinterpret_cast<T>(GetProcAddress(module, name));
}

bool Resolve(HMODULE module, Api& api) {
    api.Open = Bind<tOpen>(module, "WinHttpOpen");
    api.CloseHandle = Bind<tCloseHandle>(module, "WinHttpCloseHandle");
    api.QueryOption = Bind<tQueryOption>(module, "WinHttpQueryOption");
    api.GetDefaultProxyConfiguration =
        Bind<tGetDefaultProxyConfiguration>(module, "WinHttpGetDefaultProxyConfiguration");
    api.GetIEProxyConfigForCurrentUser = Bind<tGetIEProxyConfigForCurrentUser>(
        module, "WinHttpGetIEProxyConfigForCurrentUser");
    api.GetProxyForUrl = Bind<tGetProxyForUrl>(module, "WinHttpGetProxyForUrl");
    api.CheckPlatform = Bind<tCheckPlatform>(module, "WinHttpCheckPlatform");
    api.TimeFromSystemTime = Bind<tTimeFromSystemTime>(module, "WinHttpTimeFromSystemTime");
    return api.Complete();
}

void FreeProxyInfo(WINHTTP_PROXY_INFO& info) {
    if (info.lpszProxy) GlobalFree(info.lpszProxy);
    if (info.lpszProxyBypass) GlobalFree(info.lpszProxyBypass);
    info.lpszProxy = nullptr;
    info.lpszProxyBypass = nullptr;
}

void FreeIeConfig(WINHTTP_CURRENT_USER_IE_PROXY_CONFIG& cfg) {
    if (cfg.lpszAutoConfigUrl) GlobalFree(cfg.lpszAutoConfigUrl);
    if (cfg.lpszProxy) GlobalFree(cfg.lpszProxy);
    if (cfg.lpszProxyBypass) GlobalFree(cfg.lpszProxyBypass);
    cfg.lpszAutoConfigUrl = nullptr;
    cfg.lpszProxy = nullptr;
    cfg.lpszProxyBypass = nullptr;
}

bool SameString(const wchar_t* a, const wchar_t* b) {
    if (!a || !b) return a == b;
    return std::wcscmp(a, b) == 0;
}

// Every entry point advertised by winhttp.def.
const char* const kExpectedExports[] = {
    "WinHttpAddRequestHeaders",
    "WinHttpCheckPlatform",
    "WinHttpCloseHandle",
    "WinHttpConnect",
    "WinHttpCrackUrl",
    "WinHttpCreateProxyResolver",
    "WinHttpCreateUrl",
    "WinHttpDetectAutoProxyConfigUrl",
    "WinHttpFreeProxyResult",
    "WinHttpGetDefaultProxyConfiguration",
    "WinHttpGetIEProxyConfigForCurrentUser",
    "WinHttpGetProxyForUrl",
    "WinHttpGetProxyForUrlEx",
    "WinHttpGetProxyResult",
    "WinHttpOpen",
    "WinHttpOpenRequest",
    "WinHttpQueryAuthSchemes",
    "WinHttpQueryDataAvailable",
    "WinHttpQueryHeaders",
    "WinHttpQueryOption",
    "WinHttpReadData",
    "WinHttpReceiveResponse",
    "WinHttpResetAutoProxy",
    "WinHttpSendRequest",
    "WinHttpSetCredentials",
    "WinHttpSetDefaultProxyConfiguration",
    "WinHttpSetOption",
    "WinHttpSetStatusCallback",
    "WinHttpSetTimeouts",
    "WinHttpTimeFromSystemTime",
    "WinHttpTimeToSystemTime",
    "WinHttpWriteData",
};

// The four symbols UnityPlayer.dll actually imports.
const char* const kUnityImports[] = {
    "WinHttpOpen",
    "WinHttpGetIEProxyConfigForCurrentUser",
    "WinHttpGetProxyForUrl",
    "WinHttpCloseHandle",
};

} // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc != 2) {
        std::printf("usage: winhttp-smoke <path-to-built-winhttp.dll>\n");
        return 2;
    }

    wchar_t full[MAX_PATH * 4] = {};
    if (!GetFullPathNameW(argv[1], static_cast<DWORD>(sizeof(full) / sizeof(full[0])), full, nullptr)) {
        std::printf("GetFullPathNameW failed: %lu\n", GetLastError());
        return 2;
    }

    HMODULE proxy = LoadLibraryW(full);
    if (!proxy) {
        std::printf("LoadLibraryW(proxy) failed: %lu\n", GetLastError());
        return 3;
    }

    wchar_t systemDir[MAX_PATH] = {};
    if (!GetSystemDirectoryW(systemDir, MAX_PATH)) return 3;
    std::wstring systemPath = std::wstring(systemDir) + L"\\winhttp.dll";
    HMODULE real = LoadLibraryExW(systemPath.c_str(), nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!real) {
        std::printf("LoadLibraryExW(system winhttp) failed: %lu\n", GetLastError());
        return 3;
    }
    if (real == proxy) {
        std::printf("proxy and system resolved to the same module\n");
        return 3;
    }

    // --- export surface ----------------------------------------------------
    bool exportsOk = true;
    for (const char* name : kExpectedExports) {
        if (!GetProcAddress(proxy, name)) {
            std::printf("missing export: %s\n", name);
            exportsOk = false;
        }
    }
    Check(exportsOk, "proxy exports every advertised WinHTTP entry point");

    bool unityOk = true;
    for (const char* name : kUnityImports) {
        if (!GetProcAddress(proxy, name)) {
            std::printf("missing Unity import: %s\n", name);
            unityOk = false;
        }
    }
    Check(unityOk, "proxy resolves the four UnityPlayer WinHTTP imports");

    Api p;
    Api s;
    if (!Resolve(proxy, p) || !Resolve(real, s)) {
        std::printf("failed to resolve smoke APIs\n");
        return 5;
    }

    // --- WinHttpOpen (Unity import #1) -------------------------------------
    SetLastError(kSeed);
    HINTERNET hp = p.Open(L"gk-smoke", WINHTTP_ACCESS_TYPE_NO_PROXY, nullptr, nullptr, 0);
    DWORD openErrProxy = GetLastError();
    SetLastError(kSeed);
    HINTERNET hs = s.Open(L"gk-smoke", WINHTTP_ACCESS_TYPE_NO_PROXY, nullptr, nullptr, 0);
    DWORD openErrSystem = GetLastError();
    Check(hp != nullptr && hs != nullptr, "WinHttpOpen(NO_PROXY) succeeds on proxy and system");
    Check(openErrProxy == openErrSystem, "WinHttpOpen LastError semantics match");
    if (!hp || !hs) return 6;

    // --- WinHttpQueryOption (session option compare) -----------------------
    DWORD timeoutProxy = 0;
    DWORD timeoutSystem = 0;
    DWORD lenProxy = sizeof(timeoutProxy);
    DWORD lenSystem = sizeof(timeoutSystem);
    SetLastError(kSeed);
    BOOL qp = p.QueryOption(hp, WINHTTP_OPTION_CONNECT_TIMEOUT, &timeoutProxy, &lenProxy);
    DWORD qpErr = GetLastError();
    SetLastError(kSeed);
    BOOL qs = s.QueryOption(hs, WINHTTP_OPTION_CONNECT_TIMEOUT, &timeoutSystem, &lenSystem);
    DWORD qsErr = GetLastError();
    Check(qp == qs, "WinHttpQueryOption result matches system");
    Check(qpErr == qsErr, "WinHttpQueryOption LastError semantics match");
    Check(qp && timeoutProxy == timeoutSystem, "WinHttpQueryOption connect timeout matches system");

    // --- WinHttpGetDefaultProxyConfiguration -------------------------------
    WINHTTP_PROXY_INFO dp = {};
    WINHTTP_PROXY_INFO ds = {};
    SetLastError(kSeed);
    BOOL gdp = p.GetDefaultProxyConfiguration(&dp);
    DWORD gdpErr = GetLastError();
    SetLastError(kSeed);
    BOOL gds = s.GetDefaultProxyConfiguration(&ds);
    DWORD gdsErr = GetLastError();
    Check(gdp == gds, "WinHttpGetDefaultProxyConfiguration result matches system");
    Check(gdpErr == gdsErr, "WinHttpGetDefaultProxyConfiguration LastError matches system");
    if (gdp && gds) Check(dp.dwAccessType == ds.dwAccessType, "default proxy access type matches system");
    FreeProxyInfo(dp);
    FreeProxyInfo(ds);

    // --- WinHttpGetIEProxyConfigForCurrentUser (Unity import #2) -----------
    WINHTTP_CURRENT_USER_IE_PROXY_CONFIG ieProxy = {};
    WINHTTP_CURRENT_USER_IE_PROXY_CONFIG ieSystem = {};
    SetLastError(kSeed);
    BOOL gip = p.GetIEProxyConfigForCurrentUser(&ieProxy);
    DWORD gipErr = GetLastError();
    SetLastError(kSeed);
    BOOL gis = s.GetIEProxyConfigForCurrentUser(&ieSystem);
    DWORD gisErr = GetLastError();
    Check(gip == gis, "WinHttpGetIEProxyConfigForCurrentUser result matches system");
    Check(gipErr == gisErr, "WinHttpGetIEProxyConfigForCurrentUser LastError matches system");
    if (gip && gis) {
        const bool same = (ieProxy.fAutoDetect == ieSystem.fAutoDetect) &&
                          SameString(ieProxy.lpszAutoConfigUrl, ieSystem.lpszAutoConfigUrl) &&
                          SameString(ieProxy.lpszProxy, ieSystem.lpszProxy) &&
                          SameString(ieProxy.lpszProxyBypass, ieSystem.lpszProxyBypass);
        Check(same, "IE proxy configuration matches system");
    }
    FreeIeConfig(ieProxy);
    FreeIeConfig(ieSystem);

    // --- WinHttpGetProxyForUrl (Unity import #3): invalid handle -----------
    WINHTTP_AUTOPROXY_OPTIONS autoOptions = {};
    autoOptions.dwFlags = WINHTTP_AUTOPROXY_CONFIG_URL;
    autoOptions.dwAutoDetectFlags = 0;
    autoOptions.lpszAutoConfigUrl = nullptr; // no config URL -> cannot reach the network
    autoOptions.fAutoLogonIfChallenged = FALSE;

    WINHTTP_PROXY_INFO invProxy = {};
    WINHTTP_PROXY_INFO invSystem = {};
    HINTERNET badSession = reinterpret_cast<HINTERNET>(static_cast<ULONG_PTR>(0x1234));
    SetLastError(kSeed);
    BOOL invP = p.GetProxyForUrl(badSession, L"http://example.invalid/wpad.dat", &autoOptions, &invProxy);
    DWORD invErrP = GetLastError();
    SetLastError(kSeed);
    BOOL invS = s.GetProxyForUrl(badSession, L"http://example.invalid/wpad.dat", &autoOptions, &invSystem);
    DWORD invErrS = GetLastError();
    Check(!invP && !invS, "WinHttpGetProxyForUrl rejects an invalid session handle");
    Check(invErrP == invErrS, "WinHttpGetProxyForUrl invalid-handle LastError matches system");
    FreeProxyInfo(invProxy);
    FreeProxyInfo(invSystem);

    // --- WinHttpGetProxyForUrl: valid session, unusable options ------------
    WINHTTP_PROXY_INFO optProxy = {};
    WINHTTP_PROXY_INFO optSystem = {};
    SetLastError(kSeed);
    BOOL optP = p.GetProxyForUrl(hp, L"http://example.invalid/", &autoOptions, &optProxy);
    DWORD optErrP = GetLastError();
    SetLastError(kSeed);
    BOOL optS = s.GetProxyForUrl(hs, L"http://example.invalid/", &autoOptions, &optSystem);
    DWORD optErrS = GetLastError();
    Check(optP == optS, "WinHttpGetProxyForUrl invalid-options result matches system");
    Check(optErrP == optErrS, "WinHttpGetProxyForUrl invalid-options LastError matches system");
    FreeProxyInfo(optProxy);
    FreeProxyInfo(optSystem);

    // --- WinHttpCheckPlatform ---------------------------------------------
    SetLastError(kSeed);
    BOOL cp = p.CheckPlatform();
    DWORD cpErr = GetLastError();
    SetLastError(kSeed);
    BOOL cs = s.CheckPlatform();
    DWORD csErr = GetLastError();
    Check(cp == cs, "WinHttpCheckPlatform matches system");
    Check(cpErr == csErr, "WinHttpCheckPlatform LastError matches system");

    // --- WinHttpTimeFromSystemTime (buffer-out forwarding) -----------------
    SYSTEMTIME st = {};
    st.wYear = 2020;
    st.wMonth = 1;
    st.wDay = 15;
    st.wHour = 12;
    st.wMinute = 0;
    st.wSecond = 0;
    st.wMilliseconds = 0;
    wchar_t timeProxy[64] = {};
    wchar_t timeSystem[64] = {};
    SetLastError(kSeed);
    BOOL tp = p.TimeFromSystemTime(&st, timeProxy);
    DWORD tpErr = GetLastError();
    SetLastError(kSeed);
    BOOL ts = s.TimeFromSystemTime(&st, timeSystem);
    DWORD tsErr = GetLastError();
    Check(tp && ts, "WinHttpTimeFromSystemTime succeeds on proxy and system");
    Check(tp == ts && std::wcscmp(timeProxy, timeSystem) == 0, "WinHttpTimeFromSystemTime output matches system");
    Check(tpErr == tsErr, "WinHttpTimeFromSystemTime LastError matches system");

    // --- repeated invocations exercise the forward path --------------------
    int opened = 0;
    for (int i = 0; i < 8; ++i) {
        SetLastError(kSeed);
        HINTERNET h = p.Open(L"gk-smoke", WINHTTP_ACCESS_TYPE_NO_PROXY, nullptr, nullptr, 0);
        if (h && p.CloseHandle(h)) ++opened;
    }
    Check(opened == 8, "repeated WinHttpOpen/WinHttpCloseHandle forwarding succeeds");

    // --- WinHttpCloseHandle (Unity import #4) ------------------------------
    Check(p.CloseHandle(hp) && s.CloseHandle(hs), "WinHttpCloseHandle succeeds on both");

    std::printf(g_failures ? "SMOKE FAILED (%d)\n" : "SMOKE OK\n", g_failures);
    return g_failures ? 1 : 0;
}
