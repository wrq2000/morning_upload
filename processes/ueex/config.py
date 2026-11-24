"""Чтение всех параметров запуска из .env. Python 3.8+."""
import codecs
import os
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import pytz
from dotenv import dotenv_values

from common.http_client import HttpSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    start_date: date
    end_date: Optional[date]
    timezone: Any
    calendar_url: str
    public_base: str
    results_page: str
    api_handler: str
    results_command: str
    number_pattern: Any
    output_dir: Path
    csv_file: str
    state_file: str
    lock_file: str
    csv_delimiter: str
    csv_decimal_separator: str
    csv_encoding: str
    recheck_days: int
    gateway_timeout: Optional[float]
    http: HttpSettings
    log_level: int
    log_file: Path
    log_max_bytes: int
    log_backup_count: int
    log_format: str
    log_date_format: str
    log_console_color: bool
    log_colors: Any
    checkpoint_file: str
    resume_enabled: bool
    resume_max_age: float
    api_error_attempts: int = 2
    api_error_backoff: float = 5.0

    @property
    def effective_end_date(self):
        return self.end_date or datetime.now(self.timezone).date()


def load_settings(env_path=None) -> Settings:
    path = Path(env_path) if env_path is not None else PROJECT_ROOT / ".env"
    defaults_path = Path(__file__).with_name("defaults.env")
    values = dict(dotenv_values(defaults_path, encoding="utf-8-sig", interpolate=False))
    file_values = dotenv_values(path, encoding="utf-8-sig", interpolate=False) if path.is_file() else {}
    # Docker --env-file передаёт переменные, самого .env в образе нет.
    # UEEX_* отделяет настройки этого процесса от остальных задач приложения.
    for key in values:
        external_key = "UEEX_" + key
        if external_key in os.environ:
            values[key] = os.environ[external_key]
        elif external_key in file_values:
            values[key] = file_values[external_key]

    def text(key, allow_empty=False):
        if key not in values or values[key] is None:
            raise ValueError("В .env отсутствует параметр " + key)
        value = values[key].strip()
        if not value and not allow_empty:
            raise ValueError("В .env пустой параметр " + key)
        return value

    def boolean(key):
        value = text(key).casefold()
        if value not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError(key + ": требуется true или false")
        return value in {"true", "1", "yes"}

    def numeric(key, kind=float, zero=False):
        try:
            value = kind(text(key))
        except ValueError as exc:
            raise ValueError(key + ": неверное число") from exc
        if value < 0 or (not zero and value == 0):
            raise ValueError(key + ": требуется " + ("неотрицательное" if zero else "положительное") + " число")
        return value

    def json_value(key, kind):
        try:
            value = json.loads(text(key))
        except json.JSONDecodeError as exc:
            raise ValueError(key + ": неверный JSON") from exc
        if not isinstance(value, kind):
            raise ValueError(key + ": неверный тип JSON")
        return value

    def file_path(key):
        value = Path(text(key)).expanduser()
        return (value if value.is_absolute() else PROJECT_ROOT / value).resolve()

    proxy_enabled = boolean("PROXY_ENABLED")
    proxy_url = text("PROXY_URL", allow_empty=not proxy_enabled)
    if proxy_enabled:
        parsed = urlsplit(proxy_url)
        if parsed.scheme not in {"http", "https", "socks5", "socks5h", "socks4", "socks4a"}:
            raise ValueError("PROXY_URL: требуется адрес HTTP или SOCKS-прокси со схемой")
        if not parsed.hostname or parsed.port is None:
            raise ValueError("PROXY_URL: укажите адрес и порт")
    headers = json_value("HTTP_HEADERS", dict)
    colors = json_value("LOG_COLORS", dict)
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError("HTTP_HEADERS должен содержать пары строк")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in colors.items()):
        raise ValueError("LOG_COLORS должен содержать пары строк")
    retry_statuses = tuple(json_value("HTTP_RETRY_STATUSES", list))
    if not all(type(v) is int and 100 <= v <= 599 for v in retry_statuses):
        raise ValueError("HTTP_RETRY_STATUSES должен содержать HTTP-коды")
    level_name = text("LOG_LEVEL").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise ValueError("LOG_LEVEL: неизвестный уровень логирования")
    timezone = pytz.timezone(text("TIMEZONE"))
    end_text = text("END_DATE", allow_empty=True)
    gateway_timeout = numeric("GATEWAY_TIMEOUT", zero=True)
    api_handler = text("API_HANDLER")
    if api_handler.casefold() == "publichandler.ashx":
        logging.getLogger(__name__).warning(
            "УЭБ: адрес PublicHandler.ashx заменён биржей на Handler/Action; используется новый адрес"
        )
        api_handler = "Handler/Action"
    settings = Settings(
        start_date=date.fromisoformat(text("START_DATE")),
        end_date=date.fromisoformat(end_text) if end_text else None,
        timezone=timezone, calendar_url=text("CALENDAR_URL"),
        public_base=text("PUBLIC_BASE").rstrip("/") + "/",
        results_page=text("RESULTS_PAGE"), api_handler=api_handler,
        results_command=text("RESULTS_COMMAND"),
        number_pattern=re.compile(text("AUCTION_NUMBER_PATTERN"), re.I),
        output_dir=file_path("OUTPUT_DIR"),
        csv_file=text("CSV_FILE"), state_file=text("STATE_FILE"), lock_file=text("LOCK_FILE"),
        csv_delimiter=text("CSV_DELIMITER"), csv_decimal_separator=text("CSV_DECIMAL_SEPARATOR"),
        csv_encoding=text("CSV_ENCODING"), recheck_days=numeric("RECHECK_DAYS", int),
        gateway_timeout=gateway_timeout or None,
        http=HttpSettings(
            proxy_enabled=proxy_enabled, proxy_url=proxy_url,
            connect_timeout=numeric("HTTP_CONNECT_TIMEOUT"), read_timeout=numeric("HTTP_READ_TIMEOUT"),
            attempts=numeric("HTTP_ATTEMPTS", int), request_pause=numeric("HTTP_REQUEST_PAUSE", zero=True),
            retry_backoff=numeric("HTTP_RETRY_BACKOFF", zero=True),
            retry_max_delay=numeric("HTTP_RETRY_MAX_DELAY", zero=True),
            retry_statuses=retry_statuses, verify_tls=boolean("HTTP_VERIFY_TLS"), headers=headers,
            rate_limit_backoff=numeric("HTTP_RATE_LIMIT_BACKOFF"),
            rate_limit_max_delay=numeric("HTTP_RATE_LIMIT_MAX_DELAY"),
            wait_log_interval=numeric("HTTP_WAIT_LOG_INTERVAL"),
            cooldown_file=file_path("HTTP_COOLDOWN_FILE"),
        ),
        log_level=level, log_file=file_path("LOG_FILE"), log_max_bytes=numeric("LOG_MAX_BYTES", int),
        log_backup_count=numeric("LOG_BACKUP_COUNT", int, zero=True), log_format=text("LOG_FORMAT"),
        log_date_format=text("LOG_DATE_FORMAT"), log_console_color=boolean("LOG_CONSOLE_COLOR"),
        log_colors=colors,
        checkpoint_file=text("CHECKPOINT_FILE"), resume_enabled=boolean("RESUME_ENABLED"),
        resume_max_age=numeric("RESUME_MAX_AGE_SECONDS"),
        api_error_attempts=numeric("API_ERROR_ATTEMPTS", int),
        api_error_backoff=numeric("API_ERROR_BACKOFF", zero=True),
    )
    if not math.isfinite(settings.api_error_backoff):
        raise ValueError("API_ERROR_BACKOFF должен быть конечным числом")
    if settings.start_date > settings.effective_end_date:
        raise ValueError("END_DATE раньше START_DATE")
    if len(settings.csv_delimiter) != 1 or settings.csv_decimal_separator not in {".", ","}:
        raise ValueError("Неверные разделители CSV")
    if settings.csv_delimiter == settings.csv_decimal_separator:
        raise ValueError("Разделитель CSV не должен совпадать с десятичным разделителем")
    codecs.lookup(settings.csv_encoding)
    for url in (settings.calendar_url, settings.public_base):
        if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).hostname:
            raise ValueError("Неверный адрес источника: " + url)
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", settings.api_handler)
            or any(part in {".", ".."} for part in settings.api_handler.split("/"))):
        raise ValueError("API_HANDLER должен содержать относительный путь обработчика")
    for name in (settings.results_page, settings.csv_file,
                 settings.state_file, settings.lock_file, settings.checkpoint_file):
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("В имени файла не должен содержаться путь: " + name)
    if len({settings.csv_file, settings.state_file, settings.lock_file, settings.checkpoint_file}) != 4:
        raise ValueError("Имена выходных файлов должны различаться")
    paths = {settings.output_dir / name for name in (
        settings.csv_file, settings.state_file, settings.lock_file, settings.checkpoint_file)}
    if settings.http.cooldown_file in paths or settings.http.cooldown_file == settings.log_file:
        raise ValueError("HTTP_COOLDOWN_FILE должен отличаться от остальных выходных файлов")
    return settings
