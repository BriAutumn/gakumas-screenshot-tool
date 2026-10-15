"""Explicit, DPI-correct Windows window icons for the Tk launcher.

Tk 8.6.15 reads PNG-encoded ICO headers as DIB headers. In the tested launcher
that selects the first 16-pixel frame, enlarged to 32/64 pixels. Merely checking
the resulting HICON dimensions misses this loss of detail.

This helper bypasses Tk: it loads the small and big frames straight from the ICO
with ``LoadImageW`` using the exact ``GetSystemMetricsForDpi`` dimensions for the
window's actual DPI (``GetDpiForWindow``), then posts them to the real top-level
wrapper HWND with ``WM_SETICON``.  The HICONs are owned here, replaced only when
the wrapper HWND or its DPI changes, and destroyed when the window goes away.
Only this process' own window is touched; class icons are never changed.
"""
from __future__ import annotations

import ctypes
import sys
import tkinter

try:  # pragma: no cover - wintypes needs Windows, this keeps review imports cheap
    from ctypes import wintypes
except Exception:  # pragma: no cover
    wintypes = None

if wintypes is not None:
    BOOL = wintypes.BOOL
    HANDLE = wintypes.HANDLE
    HINSTANCE = wintypes.HINSTANCE
    HWND = wintypes.HWND
    LPCWSTR = wintypes.LPCWSTR
    UINT = wintypes.UINT
    HICON = getattr(wintypes, 'HICON', wintypes.HANDLE)
else:  # pragma: no cover
    BOOL = ctypes.c_int
    HANDLE = ctypes.c_void_p
    HINSTANCE = ctypes.c_void_p
    HWND = ctypes.c_void_p
    LPCWSTR = ctypes.c_wchar_p
    UINT = ctypes.c_uint
    HICON = ctypes.c_void_p

# Pointer-sized message types: c_long / c_ulong are 32 bit on 64-bit Windows and
# would truncate handles passed through WPARAM / LPARAM.
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

SM_CXICON = 11
SM_CYICON = 12
SM_CXSMICON = 49
SM_CYSMICON = 50

WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1

GA_ROOT = 2

_DPI_FALLBACK = 96


class WindowIcons:
    """Own the small/big window icons of one Tk toplevel at its real DPI."""

    def __init__(self, root, ico_path):
        self.root = root
        self.ico_path = str(ico_path)
        self._user32 = None
        self._set_dpi = None
        self._get_metric = None
        self._icons = {}
        self._hwnd = 0
        self._dpi = 0
        self._pending = False
        self._destroyed = False
        if sys.platform != 'win32' or not self.ico_path:
            return
        try:
            self._setup()
        except Exception:
            self._user32 = None
            return
        try:
            root.bind('<Map>', self._on_map, add='+')
            root.bind('<Configure>', self._on_configure, add='+')
            root.bind('<Destroy>', self._on_destroy, add='+')
        except Exception:
            self._user32 = None

    # -- setup -----------------------------------------------------------
    def _setup(self):
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.LoadImageW.argtypes = [HINSTANCE, LPCWSTR, UINT, ctypes.c_int, ctypes.c_int, UINT]
        user32.LoadImageW.restype = HANDLE
        user32.SendMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
        user32.SendMessageW.restype = LRESULT
        user32.GetAncestor.argtypes = [HWND, UINT]
        user32.GetAncestor.restype = HWND
        user32.DestroyIcon.argtypes = [HICON]
        user32.DestroyIcon.restype = BOOL
        set_dpi = user32.GetDpiForWindow
        set_dpi.argtypes = [HWND]
        set_dpi.restype = UINT
        get_metric = user32.GetSystemMetricsForDpi
        get_metric.argtypes = [ctypes.c_int, UINT]
        get_metric.restype = ctypes.c_int
        self._user32 = user32
        self._set_dpi = set_dpi
        self._get_metric = get_metric

    # -- Tk events -------------------------------------------------------
    def _on_map(self, _event):
        self._schedule()

    def _on_configure(self, _event):
        self._schedule()

    def _on_destroy(self, event):
        if getattr(event, 'widget', None) is not self.root:
            return
        self.destroy()

    def _schedule(self):
        if self._destroyed or self._pending or self._user32 is None:
            return
        self._pending = True
        try:
            self.root.after_idle(self._run_pending)
        except Exception:
            self._pending = False

    def _run_pending(self):
        self._pending = False
        if self._destroyed or self._user32 is None:
            return
        try:
            self._refresh()
        except Exception:
            pass

    # -- internals -------------------------------------------------------
    @staticmethod
    def _handle(value):
        if value is None:
            return 0
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if not text:
            return 0
        try:
            return int(text, 16) if text[:2].lower() == '0x' else int(text, 10)
        except ValueError:
            return 0

    def _wrapper_hwnd(self):
        hwnd = 0
        try:
            hwnd = self._handle(self.root.wm_frame())
        except (tkinter.TclError, AttributeError):
            hwnd = 0
        if not hwnd:
            try:
                hwnd = self._handle(self.root.winfo_id())
            except (tkinter.TclError, AttributeError):
                return 0
        if not hwnd:
            return 0
        ancestor = self._user32.GetAncestor(HWND(hwnd), GA_ROOT)
        return int(ancestor) if ancestor else hwnd

    def _dpi_for(self, hwnd):
        try:
            dpi = int(self._set_dpi(HWND(hwnd)))
        except Exception:
            dpi = 0
        return dpi if dpi > 0 else _DPI_FALLBACK

    def _load_frame(self, cx, cy):
        cx = max(1, int(cx))
        cy = max(1, int(cy))
        try:
            handle = self._user32.LoadImageW(None, self.ico_path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
        except Exception:
            return 0
        return int(handle) if handle else 0

    def _send_icon(self, hwnd, index, icon):
        self._user32.SendMessageW(HWND(hwnd), WM_SETICON, index, int(icon or 0))

    def _destroy_icon(self, handle):
        if not handle:
            return
        try:
            self._user32.DestroyIcon(HICON(handle))
        except Exception:
            pass

    def _refresh(self):
        if self._destroyed or self._user32 is None:
            return False
        hwnd = self._wrapper_hwnd()
        if not hwnd:
            return False
        dpi = self._dpi_for(hwnd)
        if hwnd == self._hwnd and dpi == self._dpi and self._icons:
            return True
        sizes = (int(self._get_metric(SM_CXSMICON, dpi)), int(self._get_metric(SM_CYSMICON, dpi)),
                 int(self._get_metric(SM_CXICON, dpi)), int(self._get_metric(SM_CYICON, dpi)))
        if min(sizes) <= 0:
            return False
        small = self._load_frame(sizes[0], sizes[1])
        if not small:
            return False
        big = self._load_frame(sizes[2], sizes[3])
        if not big:
            self._destroy_icon(small)
            return False
        previous = self._icons
        try:
            self._send_icon(hwnd, ICON_SMALL, small)
            self._send_icon(hwnd, ICON_BIG, big)
        except Exception:
            # Restore whatever the window had before so no destroyed handle stays set.
            try:
                self._send_icon(hwnd, ICON_SMALL, previous.get('small'))
                self._send_icon(hwnd, ICON_BIG, previous.get('big'))
            except Exception:
                pass
            self._destroy_icon(small)
            self._destroy_icon(big)
            return False
        self._icons = {'small': small, 'big': big}
        self._hwnd = hwnd
        self._dpi = dpi
        self._destroy_icon(previous.get('small'))
        self._destroy_icon(previous.get('big'))
        return True

    # -- teardown --------------------------------------------------------
    def destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        self._pending = False
        icons = self._icons
        self._icons = {}
        hwnd = self._hwnd
        self._hwnd = 0
        if hwnd and self._user32 is not None:
            for index in (ICON_SMALL, ICON_BIG):
                try:
                    self._send_icon(hwnd, index, 0)
                except Exception:
                    pass
        self._destroy_icon(icons.get('small'))
        self._destroy_icon(icons.get('big'))
