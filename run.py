"""Run from a checkout without installation or third-party dependencies."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from gkshot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
