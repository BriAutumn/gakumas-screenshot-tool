"""User-run Win32 adapter. No Codex, browser, network, or game memory access."""
import ctypes
import sys
import time
from ctypes import wintypes as W


class CapturePaused(RuntimeError):
    pass


def dpi_aware():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        ctypes.windll.user32.SetProcessDPIAware()


def api():
    if sys.platform != "win32":
        raise ValueError("采集器目前只支持 Windows PC 客户端。")
    user = ctypes.WinDLL("user32", use_last_error=True)
    for name, result, arguments in (
        ("GetForegroundWindow", W.HWND, []), ("IsWindow", W.BOOL, [W.HWND]),
        ("IsWindowVisible", W.BOOL, [W.HWND]), ("IsIconic", W.BOOL, [W.HWND]),
        ("GetClientRect", W.BOOL, [W.HWND, ctypes.POINTER(W.RECT)]),
        ("ClientToScreen", W.BOOL, [W.HWND, ctypes.POINTER(W.POINT)]),
        ("SetForegroundWindow", W.BOOL, [W.HWND]), ("WindowFromPoint", W.HWND, [W.POINT]),
        ("GetAncestor", W.HWND, [W.HWND, W.UINT]),
        ("GetWindowTextW", ctypes.c_int, [W.HWND, W.LPWSTR, ctypes.c_int]),
        ("GetWindowThreadProcessId", W.DWORD, [W.HWND, ctypes.POINTER(W.DWORD)]),
        ("SetCursorPos", W.BOOL, [ctypes.c_int, ctypes.c_int]),
        ("GetCursorPos", W.BOOL, [ctypes.POINTER(W.POINT)]),
        ("GetAsyncKeyState", ctypes.c_short, [ctypes.c_int]),
    ):
        function = getattr(user, name)
        function.restype, function.argtypes = result, arguments
    return user


def list_game_windows(title="gakumas"):
    user = api()
    windows = []
    callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def inspect(handle, _):
        text = ctypes.create_unicode_buffer(512)
        user.GetWindowTextW(handle, text, 512)
        if user.IsWindowVisible(handle) and text.value.casefold() == title.casefold():
            pid = W.DWORD()
            user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            windows.append({"hwnd": int(handle), "pid": pid.value, "title": text.value})
        return True
    callback = callback_type(inspect)
    user.EnumWindows.argtypes = [callback_type, W.LPARAM]
    user.EnumWindows(callback, 0)
    return windows


class WindowsBackend:
    def __init__(self, window):
        from PIL import ImageGrab
        self.window, self.user, self.grab = dict(window), api(), ImageGrab.grab
        self.geometry = None

    def activate(self):
        self.verify(foreground=False)
        self.preflight()
        if self.user.GetForegroundWindow() == self.window["hwnd"]:
            return
        # Invoked by the Start button on the UI thread, while it owns the last input.
        # Windows may still deny foreground activation; never fake an Alt/key input.
        self.user.SetForegroundWindow(self.window["hwnd"])
        for _ in range(20):
            if self.user.GetForegroundWindow() == self.window["hwnd"]:
                self.verify()
                return
            time.sleep(0.025)
        raise CapturePaused("Windows 未允许自动切到游戏。请确认游戏未最小化；也可勾选『手动切换到游戏』，点击开始后在 3 秒倒计时内点击游戏。")

    def preflight(self):
        from gkshot.capture.permissions import input_permissions
        try:
            permissions = input_permissions(self.window["pid"])
        except OSError as error:
            raise CapturePaused(f"无法读取游戏权限（Windows {error.winerror}），未发送鼠标输入。请重开游戏后刷新窗口。") from error
        if permissions["collector"] < permissions["game"]:
            raise CapturePaused(permissions["description"] + "。Windows 会阻止向更高权限的游戏发送鼠标输入。请关闭当前采集器，在文件资源管理器里双击 launch-capture-admin.cmd，并在 Windows 提示时确认；原会话可以继续使用。")
        return permissions

    def input_error(self, operation, code):
        from gkshot.capture.permissions import desktop_names
        desktops = desktop_names()
        reason = ctypes.FormatError(code).strip() if code else "系统未返回具体错误码"
        detail = f"{operation}失败（Windows {code}：{reason}；线程桌面：{desktops['thread']}；输入桌面：{desktops['input']}）。"
        if desktops["thread"] != desktops["input"]:
            detail += "请退出后从 Windows 文件资源管理器重新启动，并保持普通桌面已解锁。"
        else:
            detail += "请确认采集器与游戏权限一致，且游戏仍在前台。"
        return detail

    def keys(self):
        return {"pause": bool(self.user.GetAsyncKeyState(0x77) & 0x8000),
                "stop": bool(self.user.GetAsyncKeyState(0x78) & 0x8000)}

    def verify(self, foreground=True):
        h = self.window["hwnd"]
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(h, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(512)
        self.user.GetWindowTextW(h, title, 512)
        if not self.user.IsWindow(h) or pid.value != self.window["pid"] or title.value != self.window["title"] or self.user.IsIconic(h):
            raise CapturePaused("游戏窗口已关闭、最小化或发生变化。")
        if foreground and self.user.GetForegroundWindow() != h:
            raise CapturePaused("游戏失去前台焦点，已暂停，不会向其他窗口点击。")
        rect, origin = W.RECT(), W.POINT(0, 0)
        if not self.user.GetClientRect(h, ctypes.byref(rect)) or not self.user.ClientToScreen(h, ctypes.byref(origin)):
            raise CapturePaused("无法读取游戏窗口位置。")
        geometry = (origin.x, origin.y, rect.right, rect.bottom)
        if rect.right < 400 or not 1.6 < rect.bottom / rect.right < 2.0:
            raise CapturePaused("需要宽度至少 400 像素的 PC 竖版游戏窗口。")
        if self.geometry is None:
            self.geometry = geometry
        elif self.geometry != geometry:
            raise CapturePaused("游戏窗口位置或大小改变，已暂停；重新开始时会重新定位。")
        return geometry

    def unobscured(self, x, y):
        target = self.user.WindowFromPoint(W.POINT(x, y))
        if self.user.GetAncestor(target, 2) != self.window["hwnd"]:
            raise CapturePaused("游戏画面被其他窗口遮挡，已暂停。")

    def screenshot(self):
        x, y, width, height = self.verify()
        for dx, dy in ((8, 8), (width-8, 8), (8, height-8), (width-8, height-8), (width//2, height//2)):
            self.unobscured(x+dx, y+dy)
        image = self.grab(bbox=(x, y, x+width, y+height), all_screens=True).convert("RGB")
        self.verify()
        return image

    def point(self, point):
        if len(point) != 2 or not all(0 < value < 1 for value in point):
            raise ValueError("点击位置必须在游戏客户区内部。")
        x, y, width, height = self.verify()
        x, y = x+round(point[0]*width), y+round(point[1]*height)
        self.unobscured(x, y)
        ctypes.set_last_error(0)
        if not self.user.SetCursorPos(x, y):
            raise CapturePaused(self.input_error("移动鼠标", ctypes.get_last_error()))
        # A successful SetCursorPos can still be clipped by another program. Never
        # click until the actual position has reached the requested game point.
        actual = W.POINT()
        ctypes.set_last_error(0)
        if not self.user.GetCursorPos(ctypes.byref(actual)):
            raise CapturePaused(self.input_error("读取鼠标位置", ctypes.get_last_error()))
        if abs(actual.x-x)>2 or abs(actual.y-y)>2:
            raise CapturePaused(f"鼠标未到达目标位置（目标 {x},{y}，实际 {actual.x},{actual.y}），已暂停，未点击。")
        self.verify()
        self.unobscured(actual.x, actual.y)

    def mouse(self, flags, data=0):
        # Win32 INPUT includes a union with MOUSEINPUT; c_size_t preserves pointer width.
        class Mouse(ctypes.Structure):
            _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
        class Payload(ctypes.Union):
            _fields_ = [("mi", Mouse)]
        class Input(ctypes.Structure):
            _fields_ = [("type", W.DWORD), ("payload", Payload)]
        event = Input(0, Payload(Mouse(0, 0, data & 0xffffffff, flags, 0, 0)))
        self.user.SendInput.argtypes = [W.UINT, ctypes.POINTER(Input), ctypes.c_int]
        ctypes.set_last_error(0)
        if self.user.SendInput(1, ctypes.byref(event), ctypes.sizeof(Input)) != 1:
            raise CapturePaused(self.input_error("发送鼠标输入", ctypes.get_last_error()))

    def click(self, point):
        self.point(point)
        # Give the game's input loop a frame to observe the new cursor position.
        time.sleep(0.08)
        self.point(point)
        self.verify()
        self.mouse(0x0002)
        time.sleep(0.05)
        self.mouse(0x0004)

    def scroll(self, point, notches):
        self.point(point)
        self.verify()
        self.mouse(0x0800, round(notches*120))
