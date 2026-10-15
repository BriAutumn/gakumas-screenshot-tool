#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <iostream>
#include <vector>
int wmain(int argc, wchar_t** argv) {
 if (argc != 2) return 2;
 auto dll = LoadLibraryW(argv[1]);
 if (!dll) { std::cerr << "LoadLibrary: " << GetLastError(); return 3; }
 using Size = DWORD(WINAPI*)(LPCWSTR, LPDWORD);
 using Info = BOOL(WINAPI*)(LPCWSTR, DWORD, DWORD, LPVOID);
 auto size = reinterpret_cast<Size>(GetProcAddress(dll, "GetFileVersionInfoSizeW"));
 auto info = reinterpret_cast<Info>(GetProcAddress(dll, "GetFileVersionInfoW"));
 if (!size || !info) return 5;
 wchar_t system[MAX_PATH]; GetSystemDirectoryW(system, MAX_PATH);
 std::wstring path = std::wstring(system) + L"\\kernel32.dll";
 DWORD ignored = 0, bytes = size(path.c_str(), &ignored);
 std::vector<char> data(bytes);
 if (!bytes || !info(path.c_str(), 0, bytes, data.data())) return 4;
 std::cout << "Version proxy forwards successfully, bytes=" << bytes << "\n";
 Sleep(1000);
 return 0;
}
