import logging
import sys
from logging.handlers import RotatingFileHandler
import colorlog
from pathlib import Path
from datetime import datetime


def setup_logging(log_dir='/app/logs'):
    """
    Настраивает логирование:
    - Файл: всегда без цветов, UTF-8, ротация
    - Консоль: цвета только при выводе в терминал (не в cron/pipe)
    """
    # Создаём директорию для логов
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # Имя файла с временной меткой для каждого запуска
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    log_file = f"{log_dir}/morning-upload_{timestamp}.log"

    # Очищаем существующие обработчики
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # === Файловый обработчик (гарантированно UTF-8, без цветов) ===
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding='utf-8'
    )
    file_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)

    # === Консольный обработчик (цвета только в терминале) ===
    console_handler = logging.StreamHandler()

    # Проверяем, выводим ли мы в терминал (не в cron/pipe)
    if sys.stdout.isatty() and not sys.stdout.closed:
        formatter = colorlog.ColoredFormatter(
            fmt='%(log_color)s%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'bold_red',
            }
        )
    else:
        # Без цветов при перенаправлении (cron, pipe)
        formatter = logging.Formatter(
            fmt='%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    console_handler.setFormatter(formatter)

    # Настраиваем корневой логгер
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logging.info(f"Логирование настроено. Логи: {log_file}")
    return log_file  # Возвращаем путь для информации