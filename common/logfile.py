"""
common/logfile.py
Настройка логирования с цветами в консоли и ротацией файлов
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
import colorlog
from pathlib import Path


def setup_logging(log_dir: str = "logs", max_bytes: int = 5 * 1024 * 1024, backup_count: int = 3) -> str:
    """
    Настраивает логирование с цветами в консоли и ротацией файлов.

    ВАЖНО: Вызывать ТОЛЬКО ОДИН РАЗ в точке входа (main.py) до импорта других модулей!

    Args:
        log_dir: Директория для логов (по умолчанию "logs")
        max_bytes: Максимальный размер файла перед ротацией (5 МБ по умолчанию)
        backup_count: Количество резервных файлов (3 по умолчанию)

    Returns:
        Путь к основному файлу лога
    """
    # Защита от повторной конфигурации
    if getattr(setup_logging, "_configured", False):
        logging.warning("⚠️ setup_logging() уже вызывался ранее. Пропускаем повторную настройку.")
        return getattr(setup_logging, "_log_file", "unknown.log")

    # Создаём директорию для логов
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Фиксированное имя файла для корректной работы ротации
    # (временная метка в имени ломает механизм ротации!)
    log_file = log_path / "morning-upload.log"

    # Очищаем существующие обработчики (защита от дублирования при многократном вызове)
    root_logger = logging.getLogger()
    if root_logger.handlers:
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

    # === Файловый обработчик (без цветов, UTF-8, ротация) ===
    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)

    # === Консольный обработчик (цвета через colorlog) ===
    console_handler = logging.StreamHandler(sys.stdout)

    console_formatter = colorlog.ColoredFormatter(
        fmt='%(log_color)s%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'bold_red',
        },
        secondary_log_colors={},
        style='%')


    console_handler.setFormatter(console_formatter)

    # === Настраиваем корневой логгер ===
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Помечаем как настроенный
    setup_logging._configured = True
    setup_logging._log_file = str(log_file)

    logging.info(
        f"✅ Логирование настроено. Файл: {log_file} "
        f"(ротация: {max_bytes / 1024 / 1024:.0f} МБ × {backup_count} файлов)"
    )

    return str(log_file)