import logging
from logging.handlers import RotatingFileHandler
import colorlog

def setup_logging():
    # Очищаем существующие обработчики корневого логгера
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # Обработчик для файла
    file_handler = RotatingFileHandler(
        'app.log',
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding='utf-8'
    )

    # Обработчик для консоли
    console_handler = logging.StreamHandler()

    # Форматтер с поддержкой цвета (работает в консоли, а в файл — без цвета — см. ниже)
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
    file_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Применяем форматтер ко всем обработчикам
    file_handler.setFormatter(file_formatter)
    console_handler.setFormatter(formatter)

    # Настраиваем корневой логгер
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)