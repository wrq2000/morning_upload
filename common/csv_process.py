"""Общий запуск HTTP → DataGateway → CSV → основной/резервный SMTP."""
import asyncio
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .data_gateway import DataGateway
from .csv_delivery import csv_attachment
from .http_client import HttpClient, HttpSettings
from .mailer import send_mail
from .process_config import env_float, env_int, env_path
from .storage import run_lock

logger = logging.getLogger(__name__)


class CsvMailProcess:
    """Конкретный процесс задаёт collect(client, today) и настройки с префиксом."""

    def __init__(self, prefix, label, default_filename):
        self.prefix = prefix
        self.label = label
        self.output_dir = env_path(prefix + "_OUTPUT_DIR", "output/" + prefix.lower())
        self.filename = os.getenv(prefix + "_CSV_FILENAME", default_filename)
        # Каждый CSV-процесс по умолчанию отправляет датированную копию.
        self.attachment_name = self.filename
        if not self.filename or self.filename != os.path.basename(self.filename):
            raise ValueError(prefix + "_CSV_FILENAME должен содержать имя файла")
        self.timezone = ZoneInfo(os.getenv(prefix + "_TIMEZONE", "Europe/Moscow"))
        self.recipients = os.getenv(prefix + "_SUCCESS_RECIPIENTS") or os.getenv("RECIPIENTS", "")
        self.error_recipients = (os.getenv(prefix + "_ERROR_RECIPIENTS")
                                 or os.getenv("ADMIN_RECIPIENTS") or self.recipients)

    def http_settings(self):
        """Общие HTTP-настройки; конкретный процесс может выбрать клиент с AUTO."""
        proxy = os.getenv(self.prefix + "_PROXY_URL", "").strip()
        connect_timeout = env_float(self.prefix + "_CONNECT_TIMEOUT", 10)
        read_timeout = env_float(self.prefix + "_READ_TIMEOUT", 60)
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("HTTP-тайм-ауты должны быть положительными")
        return HttpSettings(
            proxy_enabled=bool(proxy), proxy_url=proxy,
            connect_timeout=connect_timeout, read_timeout=read_timeout,
            attempts=env_int(self.prefix + "_HTTP_ATTEMPTS", 3),
            request_pause=1, retry_backoff=5, retry_max_delay=60,
            retry_statuses=(408, 425, 429, 500, 502, 503, 504),
            verify_tls=True, headers={"User-Agent": "MorningUpload/1.0"},
        )

    def http_client(self):
        return HttpClient(self.http_settings())

    async def collect(self, client, today):
        raise NotImplementedError

    def run(self):
        started = time.monotonic()
        csv_path = self.output_dir / self.filename
        try:
            if not self.recipients:
                raise ValueError("Укажите " + self.prefix + "_SUCCESS_RECIPIENTS или RECIPIENTS")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            with run_lock(self.output_dir / "process.lock"):
                today = datetime.now(self.timezone).date()
                with self.http_client() as client:
                    frame, body = asyncio.run(self.collect(client, today))
                DataGateway.write_csv_atomic(
                    frame, csv_path, sep=";", encoding="utf-8-sig", index=False,
                    lineterminator="\n",
                )
                csv_path.chmod(0o644)
                logger.info("%s: CSV сохранён, строк %s, %s", self.label, len(frame), csv_path)
                with csv_attachment(csv_path, self.attachment_name) as attachment:
                    sent = send_mail(
                        file_paths=[attachment], recipients=self.recipients,
                        subject=self.label + " — " + today.isoformat(), body=body,
                    )
                return {
                    "success": sent, "all_success": sent, "rows": len(frame),
                    "csv_file": str(csv_path), "duration_sec": round(time.monotonic() - started, 2),
                    "errors": [] if sent else ["CSV сохранён, отправка подтверждена не всем получателям"],
                }
        except Exception as exc:
            logger.warning("%s: источник завершён с предупреждением: %s", self.label, exc, exc_info=True)
            if self.error_recipients:
                try:
                    send_mail(
                        recipients=self.error_recipients, subject=self.label + " — ошибка",
                        body="Процесс сбора данных завершился с ошибкой.\n" + str(exc),
                    )
                except Exception:
                    logger.warning("%s: уведомление об ошибке не отправлено", self.label, exc_info=True)
            return {
                "success": False, "all_success": False, "csv_file": str(csv_path),
                "duration_sec": round(time.monotonic() - started, 2), "errors": [str(exc)],
            }
