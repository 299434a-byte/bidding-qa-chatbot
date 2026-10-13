"""logging_setup：日志初始化（控制台 + 按天轮转的本地文件）。

职责：
  - 按 LOG_LEVEL 配置全局日志等级与格式
  - TimedRotatingFileHandler 按 UTC 午夜轮转
  - 幂等：重复调用只生效一次

依赖：config
被谁调用：main（应用启动）、ingest（建库任务）
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
import threading

from .config import Settings, get_settings

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_LOGGER_NAME = "app"

_configured = False
_lock = threading.Lock()


def setup_logging(settings: Settings | None = None) -> None:
    """配置日志（幂等：重复调用只会生效一次）。"""
    global _configured
    if _configured:
        return
    with _lock:
        if _configured:
            return
        s = settings or get_settings()
        level = getattr(logging, str(s.log_level).upper(), logging.INFO)

        logger = logging.getLogger(_LOGGER_NAME)
        logger.setLevel(level)
        logger.propagate = False

        if not logger.handlers:
            formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)

            console = logging.StreamHandler()
            console.setFormatter(formatter)
            console.setLevel(level)
            logger.addHandler(console)

            try:
                s.log_file_path.parent.mkdir(parents=True, exist_ok=True)
                file_handler = TimedRotatingFileHandler(
                    s.log_file_path,
                    when="midnight",
                    backupCount=7,
                    encoding="utf-8",
                    utc=True,
                )
                file_handler.setFormatter(formatter)
                file_handler.setLevel(level)
                logger.addHandler(file_handler)
            except Exception:
                pass

        _configured = True


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """取一个日志器。默认取项目主日志器 "app"；各模块可传 ``__name__`` 细分来源。"""
    return logging.getLogger(name)