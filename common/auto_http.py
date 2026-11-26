"""HTTP AUTO: прямой доступ, затем явный прокси или собственный SSH/VPN. Python 3.8+."""
import logging
import os
import threading
import time
from contextlib import ExitStack
from dataclasses import replace
from urllib.parse import urlsplit

from .errors import HttpSourceError
from .http_client import HttpClient
from .process_config import env_int

logger = logging.getLogger(__name__)


class AutoHttpClient:
    """Один маршрут на время задачи; после успешного переключения остаёмся на прокси.

    Реализует контракт get/close/context manager для DataGateway. HTTP-повторы,
    таймауты, проверка TLS и маскирование секретов остаются в HttpClient.
    """

    def __init__(self, settings, prefix, response_validator=None, default_mode="auto"):
        self.settings = settings
        self.prefix = prefix
        self.response_validator = response_validator
        mode = os.getenv(prefix + "_NETWORK_MODE", default_mode).strip().casefold()
        if mode not in {"auto", "direct", "proxy", "vpn"}:
            raise ValueError(prefix + "_NETWORK_MODE: требуется auto, direct, proxy или vpn")
        self.mode = mode
        self.direct_attempts = env_int(prefix + "_DIRECT_ATTEMPTS", 1)
        self._stack = ExitStack()
        self._direct = self._proxy = None
        self._using_proxy = mode in {"proxy", "vpn"}
        self._closed = False
        self._lock = threading.RLock()

    @property
    def request_count(self):
        return sum(client.request_count for client in (self._direct, self._proxy) if client is not None)

    def _direct_client(self):
        if self._direct is None:
            config = replace(self.settings, proxy_enabled=False, proxy_url="", attempts=self.direct_attempts)
            self._direct = self._stack.enter_context(HttpClient(config))
        return self._direct

    def _proxy_client(self):
        if self._proxy is None:
            proxy_url = self.settings.proxy_url.strip()
            if not proxy_url:
                # SSH не импортируется/не запускается при исправном прямом доступе.
                from .ssh_tunnel import SSHTunnel, tunnel_settings_from_env
                mode = os.getenv(self.prefix + "_TUNNEL_MODE", "dynamic").strip().casefold()
                config = tunnel_settings_from_env(mode)
                proxy_url = self._stack.enter_context(SSHTunnel(config))
                logger.info("%s: резервный маршрут SSH/VPN готов", self.prefix)
            else:
                parts = urlsplit(proxy_url)
                if parts.scheme not in {"http", "https", "socks5", "socks5h", "socks4", "socks4a"} or not parts.hostname:
                    raise ValueError(self.prefix + "_PROXY_URL: требуется полный HTTP/SOCKS-адрес")
                logger.info("%s: используется настроенный резервный прокси", self.prefix)
            config = replace(self.settings, proxy_enabled=True, proxy_url=proxy_url)
            self._proxy = self._stack.enter_context(HttpClient(config))
        return self._proxy

    def _request(self, client, url, params, headers):
        response = client.get(url, params=params, headers=headers)
        try:
            if self.response_validator is not None:
                self.response_validator(response)
            return response
        except Exception:
            response.close()
            raise

    @staticmethod
    def _can_switch(error):
        # 400/401/404/422 — параметры, ключ или адрес; 429 — лимит сервиса, его обрабатываем без смены маршрута.
        return error.status_code is None or error.status_code in {200, 403, 408, 425, 500, 502, 503, 504}

    def get(self, url, params=None, headers=None):
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTP-клиент уже закрыт")
            if self._using_proxy:
                logger.info("%s: запрос через прокси к %s", self.prefix, urlsplit(url).hostname)
                return self._request(self._proxy_client(), url, params, headers)
            try:
                logger.info("%s: прямой запрос к %s", self.prefix, urlsplit(url).hostname)
                return self._request(self._direct_client(), url, params, headers)
            except HttpSourceError as exc:
                if self.mode == "direct" or not self._can_switch(exc):
                    raise
                logger.warning("%s: прямой доступ не удался (%s); переключаемся на прокси", self.prefix, exc)
                # Смена маршрута не отменяет явную паузу Retry-After от сервиса.
                while exc.retry_not_before and exc.retry_not_before > time.time():
                    remaining = exc.retry_not_before - time.time()
                    logger.info("%s: перед резервным запросом ждём Retry-After %.1f с", self.prefix, remaining)
                    time.sleep(min(remaining, 30))
                self._using_proxy = True
                return self._request(self._proxy_client(), url, params, headers)

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
