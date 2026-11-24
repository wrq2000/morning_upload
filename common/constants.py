"""
Модуль: common/constants.py

Описание:
    Общие допустимые типы источников данных: SQL-транспорты и файловые форматы. Константы
    используются DataGateway и клиентами для проверки и выбора операции. Модуль не содержит
    проектных путей, бизнес-ключей, подключений или настроек; его можно переносить вместе с
    общей инфраструктурой в другой проект.

Python: 3.8 (https://docs.python.org/3.8/)
Обновлено: 2026-10-15 10:34:12 МСК (UTC+03:00)
"""

# Файлы
XLSX_SUFFIXES = frozenset({".xlsx", ".xls"})
CSV_SUFFIX = ".csv"
DB_SOURCE_TYPES = frozenset({"postgresql", "sqlite"})
DATA_SOURCE_TYPES = DB_SOURCE_TYPES | {"excel", "excel_multi", "csv", "json", "http"}
DB_TYPE_ENV = "DB_TYPE"
SQLITE_DB_PATH_ENV = "SQLITE_DB_PATH"
SQLITE_TIMEOUT_ENV = "SQLITE_TIMEOUT"
DEFAULT_DB_TYPE = "postgresql"
DEFAULT_SQLITE_TIMEOUT = 30.0
