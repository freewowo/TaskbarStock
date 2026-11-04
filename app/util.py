"""日志与小工具。"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime

from .config import LOG_PATH


def log(message: str, exc: bool = False) -> None:
    """写日志到 %APPDATA%\\TaskbarStock\\stockbar.log，永不抛出异常。"""
    try:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
        if exc:
            line += "\n" + traceback.format_exc()
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512 * 1024:
                LOG_PATH.replace(LOG_PATH.with_suffix(".log.old"))
        except OSError:
            pass
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    if sys.stderr:
        try:
            print(line, file=sys.stderr)
        except Exception:
            pass
