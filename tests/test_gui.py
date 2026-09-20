import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
AVAILABLE = all(importlib.util.find_spec(name) for name in ("tkinter", "PIL", "numpy", "onnxruntime", "rapidocr_onnxruntime"))


@unittest.skipUnless(AVAILABLE, "GUI recognition dependencies are optional")
class GuiTests(unittest.TestCase):
    def test_review_flow_and_stale_link_invalidation(self):
        import tkinter as tk
        from gkshot.gui import ReviewWindow
        from gkshot.recognition.automatic import ScreenshotScanner
        root = tk.Tk()
        root.withdraw()
        try:
            app = ReviewWindow(root, "https://gktools.ris.moe/zh-Hans/simulator")
            app.scanner = ScreenshotScanner(catalog=app.catalog)
            image = ROOT / "tests/fixtures/original-screenshot.png"
            app.display(image, app.scanner.scan(image))
            root.update_idletasks()
            self.assertEqual(app.param_vars["vocal"].get(), "1375")
            self.assertEqual(app.card_vars[9].get().split(" · ")[0], "295")
            with patch("gkshot.gui.messagebox.showerror") as error:
                app.export()
                self.assertTrue(error.called)
            self.assertEqual(app.link_var.get(), "")
            app.stage_var.set("160")
            app.bonus_var.set("4.72")
            app.clear_unreviewed_custom()
            app.set_custom(1, 3, "52x2")
            app.confirmed.set(True)
            with tempfile.TemporaryDirectory() as directory, patch("gkshot.gui.PROJECT_ROOT", Path(directory)):
                app.export()
                saved = next(Path(directory).glob("work/exports/*/loadout.json"))
                loadout = json.loads(saved.read_text(encoding="utf-8"))
                self.assertEqual(loadout["support_bonus"], 0.0472)
                self.assertEqual(loadout["customizations"][1][3], "52x2")
                self.assertIn("customizations=-----_---52x2--", app.link_var.get())
            app.param_vars["vocal"].set("1400")
            self.assertFalse(app.confirmed.get())
            self.assertEqual(app.link_var.get(), "")
            self.assertEqual(str(app.open_button.cget("state")), "disabled")
            app.card_vars[9].set("294")
            self.assertIsNone(app.custom_tokens[1][3])
        finally:
            root.destroy()
