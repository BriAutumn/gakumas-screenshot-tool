"""Read-only Win32 input-permission diagnostics. Never elevates automatically."""
import ctypes
import os
from ctypes import wintypes as W


def process_integrity(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    security.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)]
    security.GetTokenInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)]
    security.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    security.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    security.GetSidSubAuthority.argtypes = [ctypes.c_void_p, W.DWORD]
    security.GetSidSubAuthority.restype = ctypes.POINTER(W.DWORD)
    process = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    token = W.HANDLE()
    try:
        if not security.OpenProcessToken(process, 8, ctypes.byref(token)):  # TOKEN_QUERY
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = W.DWORD()
            security.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
            if not size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(size.value)
            if not security.GetTokenInformation(token, 25, buffer, size.value, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            count = security.GetSidSubAuthorityCount(sid)[0]
            return int(security.GetSidSubAuthority(sid, count-1)[0])
        finally:
            kernel.CloseHandle(token)
    finally:
        kernel.CloseHandle(process)


def integrity_label(value):
    if value >= 0x4000: return "系统权限"
    if value >= 0x3000: return "管理员权限"
    if value >= 0x2000: return "普通权限"
    return "受限权限"


def input_permissions(target_pid):
    current, target = process_integrity(os.getpid()), process_integrity(target_pid)
    return {"collector": current, "game": target,
            "description": f"采集器：{integrity_label(current)}；游戏：{integrity_label(target)}"}


def desktop_names():
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentThreadId.restype = W.DWORD
    user.GetThreadDesktop.argtypes = [W.DWORD]; user.GetThreadDesktop.restype = W.HANDLE
    user.OpenInputDesktop.argtypes = [W.DWORD, W.BOOL, W.DWORD]; user.OpenInputDesktop.restype = W.HANDLE
    user.CloseDesktop.argtypes = [W.HANDLE]
    user.GetUserObjectInformationW.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)]
    def name(handle):
        text, length = ctypes.create_unicode_buffer(256), W.DWORD()
        if not handle or not user.GetUserObjectInformationW(handle, 2, text, ctypes.sizeof(text), ctypes.byref(length)):
            return "未知"
        return text.value
    current = name(user.GetThreadDesktop(kernel.GetCurrentThreadId()))
    handle = user.OpenInputDesktop(0, False, 1)
    try:
        return {"thread": current, "input": name(handle)}
    finally:
        if handle: user.CloseDesktop(handle)
