"""Локальный SOCKS5/port forwarding поверх SSH Paramiko. Без внешнего ssh-процесса."""
import base64
import hashlib
import hmac
import logging
import math
import os
import re
import select
import socket
import socketserver
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import paramiko

logger = logging.getLogger(__name__)


def key_fingerprint(key):
    value = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
    return "SHA256:" + value


class PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected):
        self.expected = expected.rstrip("=")

    def missing_host_key(self, client, hostname, key):
        actual = key_fingerprint(key)
        if not hmac.compare_digest(self.expected, actual):
            raise paramiko.SSHException("SSH host key {}: ожидался {}, получен {}".format(hostname, self.expected, actual))


@dataclass(frozen=True)
class TunnelSettings:
    host: str
    user: str = ""
    port: Optional[int] = None
    key: str = ""
    known_hosts: str = ""
    mode: str = "dynamic"
    remote_proxy_port: int = 0
    proxy_scheme: str = "socks5h"
    connect_timeout: int = 10
    startup_timeout: float = 20
    host_key_fingerprint: str = ""

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", self.host):
            raise ValueError("Укажите VPN_SSH_HOST: IP, адрес или простой SSH-профиль")
        if self.user and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.-]*", self.user):
            raise ValueError("Некорректный VPN_SSH_USER")
        if self.mode not in {"dynamic", "forward"} or (self.port is not None and not 1 <= self.port <= 65535):
            raise ValueError("Некорректный режим туннеля или порт SSH")
        if self.mode == "forward" and not 1 <= self.remote_proxy_port <= 65535:
            raise ValueError("Для forward требуется VPN_REMOTE_PROXY_PORT")
        if self.proxy_scheme not in {"socks5h", "http"} or (self.mode == "dynamic" and self.proxy_scheme != "socks5h"):
            raise ValueError("Неверный протокол туннеля; dynamic использует socks5h")
        if self.connect_timeout <= 0 or not math.isfinite(self.startup_timeout) or self.startup_timeout <= 0:
            raise ValueError("Таймауты SSH должны быть положительными")
        if self.host_key_fingerprint and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}=?", self.host_key_fingerprint):
            raise ValueError("VPN_SSH_HOST_KEY_SHA256: требуется отпечаток SHA256 из ssh-keygen")


def _receive_exact(stream, size):
    parts = []
    while size:
        data = stream.recv(size)
        if not data:
            raise EOFError("Соединение закрыто во время SOCKS5 handshake")
        parts.append(data)
        size -= len(data)
    return b"".join(parts)


def _socks_reply(stream, code):
    stream.sendall(bytes([5, code, 0, 1]) + b"\0" * 6)


def _socks_destination(stream):
    version, count = _receive_exact(stream, 2)
    if version != 5:
        return None
    methods = _receive_exact(stream, count)
    if 0 not in methods:
        stream.sendall(b"\x05\xff")
        return None
    stream.sendall(b"\x05\x00")
    version, command, reserved, address_type = _receive_exact(stream, 4)
    if version != 5 or reserved != 0 or command != 1:
        _socks_reply(stream, 7)
        return None
    if address_type == 1:
        host = socket.inet_ntop(socket.AF_INET, _receive_exact(stream, 4))
    elif address_type == 4:
        host = socket.inet_ntop(socket.AF_INET6, _receive_exact(stream, 16))
    elif address_type == 3:
        length = _receive_exact(stream, 1)[0]
        host = _receive_exact(stream, length).decode("ascii")
        if not host:
            raise ValueError("Пустое имя SOCKS5")
    else:
        _socks_reply(stream, 8)
        return None
    port = struct.unpack("!H", _receive_exact(stream, 2))[0]
    if not port:
        raise ValueError("Нулевой порт SOCKS5")
    return host, port


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        owner = self.server.owner
        client, channel = self.request, None
        awaiting_reply = False
        owner._track(client, True)
        try:
            client.settimeout(owner.settings.connect_timeout)
            if owner.settings.mode == "dynamic":
                destination = _socks_destination(client)
                if destination is None:
                    return
                awaiting_reply = True
            else:
                destination = ("127.0.0.1", owner.settings.remote_proxy_port)
            transport = owner.transport
            if transport is None or not transport.is_active():
                raise ConnectionError("SSH transport закрыт")
            # При socks5h имя передаётся SSH-серверу; локальный DNS биржи не используется.
            channel = transport.open_channel("direct-tcpip", destination, self.client_address,
                                             timeout=owner.settings.connect_timeout)
            if channel is None:
                raise ConnectionError("SSH-сервер не открыл TCP-канал")
            owner._track(channel, True)
            if awaiting_reply:
                _socks_reply(client, 0)
                awaiting_reply = False
            owner._relay(client, channel)
        except Exception as exc:
            if awaiting_reply:
                try:
                    _socks_reply(client, 5)
                except OSError:
                    pass
            if not owner.stopped.is_set():
                logger.debug("SSH proxy: %s", type(exc).__name__)
        finally:
            for stream in (channel, client):
                if stream is not None:
                    owner._track(stream, False)
                    try:
                        stream.close()
                    except Exception:
                        pass


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, owner):
        self.owner = owner
        # Порт назначает ядро атомарно; конфликтов с другими процессами нет.
        super().__init__(("127.0.0.1", 0), _ProxyHandler)


class SSHTunnel:
    """Один SSH-клиент и локальный прокси на время выполнения задачи."""

    def __init__(self, settings):
        self.settings = settings
        self.client = self.transport = self.server = self.thread = None
        self.stopped = threading.Event()
        self._connections = set()
        self._lock = threading.Lock()

    def _track(self, stream, add):
        with self._lock:
            if add:
                self._connections.add(stream)
            else:
                self._connections.discard(stream)

    def _connection_options(self):
        cfg = self.settings
        options = {}
        config_path = Path("~/.ssh/config").expanduser()
        if config_path.is_file():
            config = paramiko.SSHConfig()
            with config_path.open(encoding="utf-8") as stream:
                config.parse(stream)
            options = config.lookup(cfg.host)
        if any(options.get(name) not in (None, "none") for name in ("proxycommand", "proxyjump")):
            raise ValueError("Для этого туннеля задайте прямой IP без ProxyCommand/ProxyJump")
        keys = [cfg.key] if cfg.key else options.get("identityfile", [])
        if not keys:
            keys = ["~/.ssh/id_ed25519", "~/.ssh/id_rsa", "~/.ssh/id_ecdsa"]
        key = next((str(Path(name).expanduser()) for name in keys if Path(name).expanduser().is_file()), None)
        if key is None:
            raise FileNotFoundError("Укажите доступный VPN_SSH_KEY")
        return {"hostname": options.get("hostname", cfg.host),
                "port": cfg.port if cfg.port is not None else int(options.get("port", 22)),
                "username": cfg.user or options.get("user"), "key_filename": key,
                "allow_agent": False, "look_for_keys": False,
                "timeout": cfg.connect_timeout, "banner_timeout": cfg.startup_timeout,
                "auth_timeout": cfg.startup_timeout, "channel_timeout": cfg.connect_timeout}

    def __enter__(self):
        self.stopped.clear()
        self.client = paramiko.SSHClient()
        try:
            if self.settings.host_key_fingerprint:
                # Явно доверенный отпечаток проверяется до аутентификации ключом.
                self.client.set_missing_host_key_policy(PinnedHostKeyPolicy(self.settings.host_key_fingerprint))
            else:
                if self.settings.known_hosts:
                    self.client.load_system_host_keys(str(Path(self.settings.known_hosts).expanduser()))
                else:
                    self.client.load_system_host_keys()
                self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
            self.client.connect(**self._connection_options())
            self.transport = self.client.get_transport()
            if self.transport is None or not self.transport.is_active() or not self.transport.is_authenticated():
                raise ConnectionError("SSH-аутентификация не завершена")
            self.transport.set_keepalive(20)
            self.server = _ProxyServer(self)
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           kwargs={"poll_interval": 0.1}, name="SSH-Proxy", daemon=True)
            self.thread.start()
            port = self.server.server_address[1]
            logger.info("Paramiko SSH proxy %s к %s готов", self.settings.mode, self.settings.host)
            return "{}://127.0.0.1:{}".format(self.settings.proxy_scheme, port)
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _relay(self, client, channel):
        readers = [client, channel]
        while readers and not self.stopped.is_set():
            ready, _, _ = select.select(readers, [], [], 0.5)
            for source in ready:
                target = channel if source is client else client
                data = source.recv(65536)
                if data:
                    target.sendall(data)
                else:
                    readers.remove(source)
                    try:
                        if target is channel:
                            target.shutdown_write()
                        else:
                            target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

    def __exit__(self, *_):
        self.stopped.set()
        if self.server is not None:
            if self.thread is not None and self.thread.is_alive():
                self.server.shutdown()
            self.server.server_close()
        with self._lock:
            streams = list(self._connections)
        for stream in streams:
            try:
                stream.close()
            except Exception:
                pass
        if self.client is not None:
            self.client.close()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)
        self.client = self.transport = self.server = self.thread = None


def tunnel_settings_from_env(mode):
    """Единственная конфигурация SSH/VPN для всех источников."""
    def setting(name, default=""):
        return os.getenv("VPN_" + name, "").strip() or default

    port = setting("SSH_PORT")
    return TunnelSettings(
        host=setting("SSH_HOST"), user=setting("SSH_USER"),
        port=int(port) if port else None,
        key=setting("SSH_KEY"), known_hosts=setting("SSH_KNOWN_HOSTS"),
        mode=mode, remote_proxy_port=int(setting("REMOTE_PROXY_PORT", "0")),
        proxy_scheme=setting("REMOTE_PROXY_SCHEME", "socks5h") if mode == "forward" else "socks5h",
        connect_timeout=int(setting("SSH_CONNECT_TIMEOUT", "10")),
        startup_timeout=float(setting("SSH_STARTUP_TIMEOUT", "20")),
        host_key_fingerprint=setting("SSH_HOST_KEY_SHA256"),
    )
