"""Windows extended paths for file I/O without changing system settings."""
import os
from pathlib import Path


def io_path(path):
    path = Path(path).absolute()
    value = str(path)
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return path
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)
