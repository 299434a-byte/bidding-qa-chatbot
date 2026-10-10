# ============================================================================
# 文件：logging_setup.py —— 全项目统一的「日志」配置
#
# 【这个文件是干什么的】
#   让程序把「发生了什么」按统一格式记下来：屏幕上看得见、同时存进 data/app.log 文件。
#   以前代码里到处 print(...)，问题是：①没法按级别过滤（调试信息和生产信息混在一起）；
#   ②没有时间戳、不知道是哪一行、哪个模块说的；③服务跑成后台进程时屏幕看不到。
#   换成标准库 logging 后，这三点都解决了。
#
# 【先懂两个概念（大白话）】
#   1) 日志级别：从低到高 Debug < Info < Warning < Error < Critical。
#      设成 INFO 时，DEBUG 的不显示；设成 DEBUG 时全都显示。用 LOG_LEVEL 配置控制。
#   2) Handler（处理器）：日志「往哪儿送」。本项目配两个：
#      · StreamHandler —— 送到屏幕（控制台）；
#      · FileHandler   —— 送到文件（data/app.log）。
#      同一条日志会同时走两个处理器，所以屏幕和文件里都能看到。
#
# 【为什么日志名要用 "app" 开头】
#   各模块里用 logging.getLogger(__name__) 拿日志器，名字形如 "app.main"、"app.ingest"。
#   本文件统一配置名为 "app" 的日志器，所有 "app.*" 的日志都会「上报」到它这里处理，
#   格式与去向就统一了。
# ============================================================================
"""日志配置：按 LOG_LEVEL 输出到控制台与本地文件。

用法：应用启动时调用一次 ``setup_logging()`` 即可；
各模块内用 ``logging.getLogger(__name__)`` 记录日志。
"""

from __future__ import annotations

# logging：Python 标准库的日志工具。
import logging
# threading：锁，保证「只配置一次」，避免并发启动时重复添加处理器。
import threading

# Settings/get_settings：读取 .env 配置（日志级别、日志文件路径）。
from .config import Settings, get_settings

# 日志「格式模板」。各占位符含义（大白话）：
#   %(asctime)s  —— 时间；%(levelname)s —— 级别（INFO/WARNING…）；
#   %(name)s     —— 模块/日志器名；%(message)s —— 日志正文。
# 级别用 -7s 左对齐补足 7 位，让各行日志的竖线对齐、更好看。
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
# 时间格式：年-月-日 时:分:秒。
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# 本项目的日志器名（各模块的 "app.xxx" 都会归到它名下）。
_LOGGER_NAME = "app"

# 「是否已配置过」的开关 + 锁，保证 setup_logging 幂等（调用多次也只配一次）。
_configured = False
_lock = threading.Lock()


def setup_logging(settings: Settings | None = None) -> None:
    """配置日志（幂等：重复调用只会生效一次）。

    【参数】settings —— 可传入配置对象；不传就从 .env 读（默认）。
    【行为】
      · 按 ``LOG_LEVEL``（默认 INFO）设定级别；
      · 同时输出到「控制台」和「LOG_FILE_PATH 指定的文件」（默认 backend/data/app.log）；
      · 格式为「时间 | 级别 | 模块 | 消息」。
    【为什么文件处理器失败要容错】日志文件写不了（比如磁盘只读）不该让服务起不来，
      此时仅保留「控制台输出」，程序照常运行。
    """
    global _configured
    # 快速路径：已经配置过就直接返回（不加锁，省开销）。
    if _configured:
        return
    with _lock:
        # 【双重检查】拿到锁后再确认一次：可能刚才别的线程已经配好了。
        if _configured:
            return
        s = settings or get_settings()
        # 把配置里的级别字符串（如 "INFO"）转成 logging 模块认识的对象；
        # 认不出来就回落 INFO（不让手滑写错的级别导致起不来）。
        level = getattr(logging, str(s.log_level).upper(), logging.INFO)

        # 取到名为 "app" 的日志器并设定级别。
        logger = logging.getLogger(_LOGGER_NAME)
        logger.setLevel(level)
        # propagate=False：不再把日志往上交给「根日志器」处理，避免被 uvicorn 等
        # 的默认配置重复打印一遍（否则同一条日志会出现两次）。
        logger.propagate = False

        # 若还没有处理器，就挂上控制台 + 文件两个处理器。
        if not logger.handlers:
            formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)

            # ① 控制台处理器：日志直接打印到屏幕。
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            console.setLevel(level)
            logger.addHandler(console)

            # ② 文件处理器：日志追加写入本地文件。
            try:
                # 确保日志文件所在目录存在（例如 backend/data/）。
                s.log_file_path.parent.mkdir(parents=True, exist_ok=True)
                # encoding="utf-8"：保证中文日志不乱码。
                file_handler = logging.FileHandler(s.log_file_path, encoding="utf-8")
                file_handler.setFormatter(formatter)
                file_handler.setLevel(level)
                logger.addHandler(file_handler)
            except Exception:
                # 【静默降级】文件写不了就放弃文件日志，仅保留控制台输出。
                pass

        _configured = True


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """取一个日志器。默认取项目主日志器 "app"；各模块可传 ``__name__`` 细分来源。"""
    return logging.getLogger(name)