#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <string>
#include "early-diagnostics.h"
#include "bootstrap.h"
extern "C" void StartMemoryExportOnce();
extern "C" { void* versionFunctions[17] = {}; }
static INIT_ONCE once = INIT_ONCE_STATIC_INIT;
static const char* names[] = {
 "GetFileVersionInfoA", "GetFileVersionInfoByHandle", "GetFileVersionInfoExA",
 "GetFileVersionInfoExW", "GetFileVersionInfoSizeA", "GetFileVersionInfoSizeExA",
 "GetFileVersionInfoSizeExW", "GetFileVersionInfoSizeW", "GetFileVersionInfoW",
 "VerFindFileA", "VerFindFileW", "VerInstallFileA", "VerInstallFileW",
 "VerLanguageNameA", "VerLanguageNameW", "VerQueryValueA", "VerQueryValueW"
};
static BOOL CALLBACK Initialize(PINIT_ONCE, PVOID, PVOID*) {
 gkearly::Log("initialize_begin");
 wchar_t system[MAX_PATH];
 if (!GetSystemDirectoryW(system, MAX_PATH)) {
  gkearly::Log("initialize_system_directory_failed", nullptr, GetLastError());
  return FALSE;
 }
 gkearly::Log("initialize_system_directory_ok");
 std::wstring path = std::wstring(system) + L"\\version.dll";
 gkearly::Log("initialize_load_begin", "version.dll");
 HMODULE real = LoadLibraryExW(path.c_str(), nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
 if (!real) {
  gkearly::Log("initialize_load_failed", "version.dll", GetLastError());
  return FALSE;
 }
 gkearly::Log("initialize_load_ok", "version.dll");
 gkearly::Log("initialize_getprocaddress_begin");
 for (unsigned i = 0; i < 17; ++i) {
  versionFunctions[i] = reinterpret_cast<void*>(GetProcAddress(real, names[i]));
  if (!versionFunctions[i]) {
   gkearly::Log("initialize_getprocaddress_failed", names[i], GetLastError());
   return FALSE;
  }
 }
 gkearly::Log("initialize_getprocaddress_ok");
 HMODULE pinned = nullptr;
 GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
  reinterpret_cast<LPCWSTR>(&Initialize), &pinned);
 gkearly::Log("initialize_call_start_memory_export");
 StartMemoryExportOnce();
 return TRUE;
}
// Preserves the caller's LastError across the one-time init so a forwarded call
// keeps its original GetLastError semantics exactly like the WinHTTP proxy.
extern "C" void EnsureVersionProxy() {
 const DWORD saved = GetLastError();
 if (!InitOnceExecuteOnce(&once, Initialize, nullptr, nullptr))
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
  // Automatic startup: only for the real game, and only a target gate plus one
  // plain CreateThread handled inside the header. The lazy Initialize path
  // above stays unchanged as the automatic fallback when this is skipped or
  // fails; forwarded calls keep their semantics via Ensure*Proxy.
  if (gkearly::IsTargetProcessEarly()) gkbootstrap::Schedule();
 }
 return TRUE;
}
