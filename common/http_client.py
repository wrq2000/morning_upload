"""HTTP-клиент: явный прокси, пауза после ответа и уважение Retry-After. Python 3.8+."""
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Mapping, Tuple, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from .errors import SourceError, HttpSourceError
from .storage import write_text_atomic

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpSettings:
    proxy_enabled: bool
    proxy_url: str
    connect_timeout: float
    read_timeout: float
    attempts: int
    request_pause: float
    retry_backoff: float
    retry_max_delay: float
    retry_statuses: Tuple[int, ...]
    verify_tls: bool
    headers: Mapping[str, str]
    rate_limit_backoff: float = 30.0
    rate_limit_max_delay: float = 300.0
    wait_log_interval: float = 30.0
    cooldown_file: Optional[Path] = None


def retry_after_seconds(value, now=None):
    """Retry-After: неотрицательные секунды либо HTTP-дата; None при ошибке."""
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        try:
            seconds = float(value)
            return seconds if math.isfinite(seconds) else None
        except ValueError:
            return None
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (deadline - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class HttpClient:
    """Сессия используется последовательно; ожидание общее для обоих доменов.

    Пауза начинается после полного получения ответа или сетевой ошибки.
    Retry-After не сокращается локальным пределом backoff. Время разрешённого
    следующего запроса сохраняется между запусками, если указан cooldown_file.
    Прокси не меняется, автоматического прямого подключения нет.
    """

    def __init__(self, settings: HttpSettings):
        self.settings = settings
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = settings.verify_tls
        self.session.headers.update(settings.headers)
        if settings.proxy_enabled:
            self.session.proxies.update({"http": settings.proxy_url, "https": settings.proxy_url})
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._wait_reason = "пауза между ответами"
        self.request_count = 0
        self._restore_cooldown()

    def _restore_cooldown(self):
        path = self.settings.cooldown_file
        if path is None or not Path(path).exists():
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            deadline = float(data["not_before"])
            if data.get("version") != 1 or not math.isfinite(deadline):
                raise ValueError("неверный формат")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise SourceError("Не удалось прочитать время следующего HTTP-запроса: " + str(path)) from exc
        if deadline > self._next_request_at:
            self._next_request_at = deadline
            self._wait_reason = str(data.get("reason", "ожидание после предыдущего запуска"))

    def _defer(self, seconds, reason):
        deadline = time.time() + max(0.0, seconds)
        if deadline >= self._next_request_at:
            self._next_request_at = deadline
            self._wait_reason = reason
        if self.settings.cooldown_file is not None:
            payload = {"version": 1, "not_before": self._next_request_at, "reason": self._wait_reason}
            write_text_atomic(Path(self.settings.cooldown_file), json.dumps(payload) + "\n")

    def _wait_for_slot(self):
        self._restore_cooldown()
        while True:
            remaining = self._next_request_at - time.time()
            if remaining <= 0:
                return
            if "HTTP" in self._wait_reason or "Retry-After" in self._wait_reason:
                logger.info("%s: до следующего запроса %.1f с", self._wait_reason, remaining)
            else:
                logger.debug("Пауза после предыдущего ответа: %.1f с", remaining)
            # Длинное ожидание разбито на части с сообщениями о ходе работы.
            time.sleep(min(remaining, self.settings.wait_log_interval))

    def get(self, url, params=None, headers=None, accepted_statuses=()):
        if urlsplit(url).scheme not in {"http", "https"}:
            raise SourceError("HTTP-источник должен иметь схему http или https")
        full_url = requests.Request("GET", url, params=params).prepare().url
        # Платный Open-Meteo передаёт ключ в query. Скрываем его в сообщениях.
        parts = urlsplit(full_url)
        query = [(key, "***" if key.lower() in {"apikey", "api_key", "token", "password", "securitytoken"} else value)
                 for key, value in parse_qsl(parts.query, keep_blank_values=True)]
        full_url = urlunsplit(parts._replace(query=urlencode(query)))
        with self._lock:
            rate_failures = 0
            last_status = None
            for attempt in range(self.settings.attempts):
                self._wait_for_slot()
                self.request_count += 1
                server_delay = None
                try:
                    response = self.session.get(
                        url, params=params, headers=headers,
                        timeout=(self.settings.connect_timeout, self.settings.read_timeout),
                    )
                except (requests.ConnectionError, requests.Timeout) as exc:
                    last_status = None
                    failure = type(exc).__name__
                else:
                    last_status = response.status_code
                    if last_status in self.settings.retry_statuses or last_status == 429:
                        failure = "HTTP {}".format(last_status)
                        server_delay = retry_after_seconds(response.headers.get("Retry-After"))
                        response.close()
                    elif not 200 <= last_status < 300 and last_status not in accepted_statuses:
                        server_delay = retry_after_seconds(response.headers.get("Retry-After"))
                        response.close()
                        self._defer(max(self.settings.request_pause, server_delay or 0.0), "пауза между ответами")
                        raise HttpSourceError("HTTP {}: {}".format(last_status, full_url), status_code=last_status,
                                              retry_not_before=self._next_request_at if server_delay is not None else None)
                    else:
                        if "charset=" not in response.headers.get("Content-Type", "").lower():
                            response.encoding = ("utf-16" if response.content[:2] in
                                                 (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig")
                        try:
                            self._defer(self.settings.request_pause, "пауза между ответами")
                        except Exception:
                            response.close()
                            raise
                        return response
                if last_status == 429:
                    local_delay = min(self.settings.rate_limit_backoff * 2 ** rate_failures,
                                      self.settings.rate_limit_max_delay)
                    rate_failures += 1
                else:
                    local_delay = min(self.settings.retry_backoff * 2 ** attempt,
                                      self.settings.retry_max_delay)
                delay = max(self.settings.request_pause, local_delay, server_delay or 0.0)
                reason = failure + (" / Retry-After" if server_delay is not None else "")
                # Сохраняем запрет до следующего запроса и на последней попытке:
                # немедленный перезапуск программы не отменяет паузу сервера.
                self._defer(delay, reason)
                if attempt + 1 < self.settings.attempts:
                    logger.warning("%s; пауза %.1f с, повтор %s/%s: %s", reason, delay,
                                   attempt + 2, self.settings.attempts, full_url)
            remaining = max(0.0, self._next_request_at - time.time())
            hint = ("Источник ограничил частоту запросов" if last_status == 429
                    else "Запрос не выполнен; проверьте доступность источника, сети и прокси")
            raise HttpSourceError("{}: {} после {} попыток. Следующий запрос не ранее чем через {:.1f} с. {}".format(
                hint, failure, self.settings.attempts, remaining, full_url), status_code=last_status,
                retry_not_before=self._next_request_at if server_delay is not None else None)

    def close(self):
        with self._lock:
            self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
