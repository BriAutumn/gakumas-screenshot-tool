"""UI boundaries use temporary libraries and never drive the game."""
import copy
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class DesktopUiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def test_offline_entry_and_stop_do_not_require_game(self):
        from gkshot.capture.gui import CaptureWindow
        with patch("gkshot.capture.gui.list_game_windows", return_value=[]):
            app = CaptureWindow(self.root, initial_tab="offline")
        self.assertEqual(app.notebook.index(app.notebook.select()), 1)
        with patch("gkshot.capture.gui.messagebox.showinfo") as dialog:
            app.process()
            self.assertTrue(dialog.called)
        self.assertFalse(app.busy)
        app.set_busy(True)
        self.assertIn("disabled", app.window_box.state())
        app.request_stop(True)
        self.assertTrue(app.pause_event.is_set())
        self.assertFalse(app.stop_event.is_set())
        app.request_stop(False)
        self.assertTrue(app.stop_event.is_set())
        app.set_busy(False)
        self.assertIn("disabled", app.stop_button.state())
        self.assertIn("readonly", app.window_box.state())

    def test_library_filters_preserve_records_and_selection(self):
        from gkshot.memory_gui import MemoryLibraryWindow
        from gkshot.memory_library import MemoryLibrary
        with tempfile.TemporaryDirectory() as directory:
            app = MemoryLibraryWindow(self.root, MemoryLibrary(Path(directory)))
            self.assertIn("还没有记忆", app.status.get())
            report = {"identity": {"title": "测试", "idol_name": "花海咲季", "power": 123},
                      "draft": {"parameters": dict(vocal=1, dance=2, visual=3, stamina=4)},
                      "missing_fields": ["card_ids.0"]}
            records = [dict(memory_id="a", status="reviewed", review=None, report=copy.deepcopy(report)),
                       dict(memory_id="b", status="needs-review", review=None, report=copy.deepcopy(report))]
            before = copy.deepcopy(records)
            with patch.object(app.library, "list", return_value=records):
                app.refresh()
                app.table.selection_set("a")
                app.refresh()
                self.assertEqual(app.selected(), "a")
                app.filter_status.set("待解决")
                app.refresh()
                self.assertEqual(app.table.get_children(), ("b",))
                app.filter_status.set("可用于模拟")
                app.refresh()
                self.assertEqual(app.table.get_children(), ("a",))
                app.query.set("不存在")
                self.assertFalse(app.table.get_children())
                self.assertIn("显示 0 / 2", app.count_text.get())
                self.assertEqual(records, before)


if __name__ == "__main__":
    unittest.main()
