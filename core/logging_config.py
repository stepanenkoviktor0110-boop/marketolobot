import logging
import os
import sys
from logging.handlers import RotatingFileHandler


LOG_FORMAT = "%(asctime)s [%(name)s] %(message)s"


def setup_logging(log_file="logs/bot.log", level=logging.INFO):
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setFormatter(formatter)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    logging.getLogger("aiogram").setLevel(logging.WARNING)
