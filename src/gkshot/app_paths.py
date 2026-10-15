"""Read-only shipped resources and per-user mutable data (no migration)."""
import os
import sys
from pathlib import Path

RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def user_root():
    override = os.environ.get("GKTOOL_USER_HOME")
    if override:
        if not Path(override).is_absolute():
            raise ValueError("GKTOOL_USER_HOME 必须是绝对路径")
        return Path(override)
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        raise RuntimeError('无法确定用户数据目录：LOCALAPPDATA 未设置')
    return Path(local) / 'GKMemoryTool'


USER_ROOT = user_root()
