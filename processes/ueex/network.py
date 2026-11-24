"""Маршрут УЭБ: заданный прокси либо SSH-туннель к отдельному серверу."""
import os
from contextlib import contextmanager
from dataclasses import replace

from common.ssh_tunnel import SSHTunnel, tunnel_settings_from_env


@contextmanager
def network_settings(settings):
    if not settings.http.proxy_enabled:
        raise ValueError("Для УЭБ обязателен VPN-прокси")
    mode = os.getenv("UEEX_TUNNEL_MODE", "none").strip().casefold()
    if mode == "none":
        yield settings
    elif mode in {"dynamic", "forward"}:
        with SSHTunnel(tunnel_settings_from_env(mode)) as url:
            yield replace(settings, http=replace(settings.http, proxy_url=url))
    else:
        raise ValueError("UEEX_TUNNEL_MODE: требуется none, dynamic или forward")
