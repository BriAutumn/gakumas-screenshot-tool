import sys
import time
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from gkshot.simulation_gui import SimulationWindow


class SimulationGuiTests(unittest.TestCase):
    def test_window_runs_worker_and_displays_sorted_statistics(self):
        root = tk.Tk()
        root.withdraw()
        window = SimulationWindow(root)
        try:
            window.paths = [ROOT / 'examples/simulation/original.loadout.json',
                            ROOT / 'examples/simulation/without-customization.loadout.json']
            window.trials.set('3')
            window.start()
            self.assertTrue(window.running)
            deadline = time.monotonic() + 15
            while window.running and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertFalse(window.running)
            self.assertEqual(window.report['status'], 'complete')
            self.assertEqual(len(window.table.get_children()), 2)
            for row in window.table.get_children():
                self.assertEqual(str(window.table.item(row)['values'][3]), '3')
            self.assertEqual(str(window.run_button['state']), 'normal')
            self.assertEqual(str(window.cancel_button['state']), 'disabled')
        finally:
            window.cancel.set()
            root.destroy()


if __name__ == '__main__':
    unittest.main()
