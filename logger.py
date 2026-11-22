from __future__ import annotations
from typing import (
    override,
    TypedDict,
    TYPE_CHECKING,
)
if TYPE_CHECKING:
    from logging import (
        LogRecord,
    )
    class LogLevelColorMap(TypedDict, total=False):
        DEBUG: str      # 10
        INFO: str       # 20
        WARNING: str    # 30
        ERROR: str      # 40
        CRITICAL: str   # 50
        NOTSET: str     # 0

import logging
import sys

from config import settings

DEV_MODE = settings.dev_mode      # 开发模式
RESET_COLOR = "\033[0m"           # 默认颜色
LEVEL_COLORS: "LogLevelColorMap" = {
    "DEBUG": "\033[36m",       # 青色
    "INFO": "\033[32m",        # 绿色
    "WARNING": "\033[33m",     # 黄色
    "ERROR": "\033[31m",       # 红色
    "CRITICAL": "\033[91m",    # 亮红色
    "NOTSET": RESET_COLOR,     # 默认颜色
}
# Err 输出端口是否为终端
IS_ERR_TTY = sys.stderr.isatty()

class LevelPrefixFormatter(logging.Formatter):
    @override
    def format(
        self,
        record: "LogRecord",
    ):
        level_name = record.levelname
        level_color = LEVEL_COLORS.get(level_name)

        # 冒号后补空格对齐
        # 最长级别名 CRITICAL (8 个字符)
        separator = " " * (8 - len(level_name))
        if IS_ERR_TTY and level_color is not None:
            level_name = f"{level_color}{level_name}{RESET_COLOR}"

        log_time = self.formatTime(record, self.datefmt) if DEV_MODE else ""
        log_time_message = f"{log_time} " if log_time else ""
        # 继续使用父类的格式化方法获取原始日志消息
        message = super().format(record)
        return f"{log_time_message}{level_name}:{separator} {message}"

log_handler = logging.StreamHandler()
log_handler.setFormatter(LevelPrefixFormatter(
    datefmt="%H:%M:%S" # Hour:Minute:Second
))

logger = logging.getLogger("custom-logger")
logger.addHandler(log_handler)
logger.setLevel(
    logging.DEBUG if DEV_MODE else logging.INFO
)
# 不交给 root logger 处理
logger.propagate = False

if __name__ == "__main__":
    logger.debug("debug message")
    logger.info("info message")
    logger.warning("warning message")
    logger.error("error message")
    logger.critical("critical message")
