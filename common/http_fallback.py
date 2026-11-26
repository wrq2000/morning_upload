"""Прямой HTTP с резервным SSH/VPN-маршрутом. Обновлено 23.11.2026 МСК."""
import logging
import os
from urllib.parse import urlsplit

import requests

from .errors import SourceError
from .process_config import env_float

logger = logging.getLogger(__name__)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)


def _request_text(url, prefix, proxy_url, timeout, user_agent, validator):
    route = "VPN/SSH" if proxy_url else "прямое соединение"
    logger.info("%s: %s; загрузка %s", prefix, route, url)
    with requests.Session() as session:
        # AUTO действительно пробует прямой доступ: системные proxy env не влияют.
        session.trust_env = False
        session.headers.update({"User-Agent": user_agent})
        session.proxies.clear()
        if proxy_url:
            session.proxies.update({"http": proxy_url, "https": proxy_url})
        response = session.get(url, timeout=timeout)
        try:
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise SourceError("Ожидался успешный HTTP-ответ")
            response.encoding = "utf-8-sig"
            html = response.text
            if validator is not None:
                validator(html)
            logger.info("%s: %s, HTTP %s; данные страницы проверены", prefix, route, response.status_code)
            return html
        finally:
            response.close()


def _reason(exc):
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return "HTTP {}".format(exc.response.status_code)
    if isinstance(exc, SourceError):
        return str(exc)
    return type(exc).__name__


def fetch_text_with_fallback(url, prefix, validator=None):
    """auto: прямой запрос → при ошибке один запрос через собственный SSH-туннель.

    direct/vpn принудительно выбирают маршрут. SSH-конфигурация читается только
    при необходимости; исправный прямой доступ не требует SSH-ключей.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(prefix + "_URL: требуется полный HTTP(S)-адрес страницы данных")
    mode = os.getenv(prefix + "_NETWORK_MODE", "auto").strip().casefold()
    if mode not in {"auto", "direct", "vpn"}:
        raise ValueError(prefix + "_NETWORK_MODE: требуется auto, direct или vpn")
    timeout = (env_float(prefix + "_CONNECT_TIMEOUT", 10), env_float(prefix + "_READ_TIMEOUT", 30))
    if min(timeout) <= 0:
        raise ValueError(prefix + ": HTTP-таймауты должны быть положительными")
    user_agent = os.getenv(prefix + "_USER_AGENT", DEFAULT_USER_AGENT).strip()
    if not user_agent:
        raise ValueError(prefix + "_USER_AGENT не должен быть пустым")

    direct_failure = None
    if mode != "vpn":
        try:
            return _request_text(url, prefix, None, timeout, user_agent, validator)
        except (requests.RequestException, SourceError) as exc:
            if mode == "direct":
                raise
            direct_failure = _reason(exc)
            logger.warning("%s: прямой доступ не удался (%s); пробуем VPN через SSH", prefix, direct_failure)

    # Импорт и установка соединения выполняются только для резервного маршрута.
    from .ssh_tunnel import SSHTunnel, tunnel_settings_from_env
    tunnel_mode = os.getenv(prefix + "_TUNNEL_MODE", "dynamic").strip().casefold()
    settings = tunnel_settings_from_env(tunnel_mode)
    try:
        with SSHTunnel(settings) as proxy_url:
            return _request_text(url, prefix, proxy_url, timeout, user_agent, validator)
    except Exception as exc:
        direct_note = "Прямой доступ: {}. ".format(direct_failure) if direct_failure else ""
        raise SourceError(prefix + ": " + direct_note + "VPN/SSH не сработал: " + _reason(exc)) from exc
