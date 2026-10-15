#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <string>
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
 wchar_t system[MAX_PATH];
 if (!GetSystemDirectoryW(system, MAX_PATH)) return FALSE;
 std::wstring path = std::wstring(system) + L"\\version.dll";
 HMODULE real = LoadLibraryExW(path.c_str(), nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
 if (!real) return FALSE;
 for (unsigned i = 0; i < 17; ++i) {
  versionFunctions[i] = reinterpret_cast<void*>(GetProcAddress(real, names[i]));
  if (!versionFunctions[i]) return FALSE;
 }
 HMODULE pinned = nullptr;
 GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
  reinterpret_cast<LPCWSTR>(&Initialize), &pinned);
 StartMemoryExportOnce();
 return TRUE;
}
extern "C" void EnsureVersionProxy() {
 if (!InitOnceExecuteOnce(&once, Initialize, nullptr, nullptr))
  RaiseFailFastException(nullptr, nullptr, 0);
}
BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID) {
 if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(module);
 return TRUE;
}
