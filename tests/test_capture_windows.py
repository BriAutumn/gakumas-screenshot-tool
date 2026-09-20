import ctypes
import sys
import unittest
from ctypes import wintypes as W
from pathlib import Path
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from gkshot.capture.windows import WindowsBackend, CapturePaused


@unittest.skipUnless(sys.platform=="win32","Win32 adapter tests")
class WindowsInputTests(unittest.TestCase):
    def test_fractional_wheel_delta_is_not_truncated_to_zero(self):
        backend=self.backend()
        backend.point=Mock();backend.mouse=Mock()
        for notches,delta in ((-.25,-30),(.5,60),(-1,-120),(3,360)):
            backend.scroll([.91,.53],notches)
            backend.mouse.assert_called_with(0x0800,delta)

    def backend(self):
        backend=WindowsBackend.__new__(WindowsBackend)
        backend.window={"hwnd":123,"pid":456,"title":"gakumas"}
        backend.user=Mock();backend.geometry=None
        backend.verify=Mock(return_value=(100,200,540,960))
        backend.unobscured=Mock()
        return backend

    def test_higher_integrity_game_blocks_before_foreground_or_mouse(self):
        backend=self.backend()
        with patch("gkshot.capture.permissions.input_permissions",return_value={"collector":0x2000,"game":0x3000,"description":"普通 / 管理员"}):
            with self.assertRaisesRegex(CapturePaused,"launch-capture-admin.cmd"):
                backend.activate()
        backend.user.SetForegroundWindow.assert_not_called()
        backend.user.SetCursorPos.assert_not_called()

    def test_equal_integrity_accepts_and_waits_for_delayed_activation(self):
        backend=self.backend()
        backend.user.GetForegroundWindow.side_effect=[999,999,123]
        permissions={"collector":0x3000,"game":0x3000,"description":"管理员 / 管理员"}
        with patch("gkshot.capture.permissions.input_permissions",return_value=permissions), patch("gkshot.capture.windows.time.sleep") as wait:
            backend.activate()
        backend.user.SetForegroundWindow.assert_called_once_with(123)
        self.assertEqual(wait.call_count,1)
        self.assertEqual(backend.verify.call_count,2)

    def test_already_foreground_does_not_steal_focus_again(self):
        backend=self.backend()
        backend.user.GetForegroundWindow.return_value=123
        with patch.object(backend,"preflight"):
            backend.activate()
        backend.user.SetForegroundWindow.assert_not_called()

    def test_foreground_denial_gives_manual_switch_route(self):
        backend=self.backend();backend.user.GetForegroundWindow.return_value=999
        with patch.object(backend,"preflight"),patch("gkshot.capture.windows.time.sleep"):
            with self.assertRaisesRegex(CapturePaused,"手动切换到游戏"):backend.activate()

    def test_cursor_move_checks_actual_destination(self):
        backend=self.backend()
        backend.user.SetCursorPos.return_value=1
        def read(point):
            actual=ctypes.cast(point,ctypes.POINTER(W.POINT)).contents
            actual.x,actual.y=370,680
            return 1
        backend.user.GetCursorPos.side_effect=read
        backend.point([0.5,0.5])
        backend.user.SetCursorPos.assert_called_once_with(370,680)
        self.assertEqual(backend.verify.call_count,2)
        self.assertEqual(backend.unobscured.call_count,2)

    def test_clipped_cursor_never_clicks(self):
        backend=self.backend()
        backend.user.SetCursorPos.return_value=1
        def read(point):
            actual=ctypes.cast(point,ctypes.POINTER(W.POINT)).contents
            actual.x,actual.y=1,1
            return 1
        backend.user.GetCursorPos.side_effect=read
        backend.mouse=Mock()
        with self.assertRaisesRegex(CapturePaused,"未到达目标"):backend.click([0.5,0.5])
        backend.mouse.assert_not_called()

    def test_cursor_failure_preserves_windows_error_and_desktop(self):
        backend=self.backend()
        def failed(x,y):ctypes.set_last_error(5);return 0
        backend.user.SetCursorPos.side_effect=failed
        backend.mouse=Mock()
        with patch("gkshot.capture.permissions.desktop_names",return_value={"thread":"Default","input":"Default"}):
            with self.assertRaisesRegex(CapturePaused,"Windows 5") as error:backend.click([0.5,0.5])
        self.assertIn("Default",str(error.exception))
        backend.mouse.assert_not_called()

    def test_read_only_integrity_and_desktop_queries(self):
        import os
        from gkshot.capture.permissions import input_permissions,desktop_names
        result=input_permissions(os.getpid())
        self.assertEqual(result["collector"],result["game"])
        self.assertIsInstance(result["collector"],int)
        self.assertNotEqual(desktop_names()["thread"],"未知")
