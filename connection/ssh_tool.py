import os

import paramiko
import socket
import threading
import time
from typing import Optional, Tuple
import requests
# Настройки
SSH_HOST = os.getenv("SSH_HOST")
SSH_PORT = os.getenv("SSH_PORT")
SSH_USER = os.getenv("SSH_USER")
SSH_KEY = os.getenv("SSH_KEY")

class SSHSOCKSProxy:
    def __init__(
        self,
        ssh_host: str,
        ssh_port: int = 22,
        ssh_username: str = None,
        ssh_password: str = None,
        ssh_private_key: Optional[str] = None,
        local_bind_address: Tuple[str, int] = ("127.0.0.1", 1080),
        timeout: int = 30,
    ):
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_username = ssh_username
        self.ssh_password = ssh_password
        self.ssh_private_key = ssh_private_key
        self.local_host, self.local_port = local_bind_address
        self.timeout = timeout

        self._ssh_client = paramiko.SSHClient()
        self._ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._transport: Optional[paramiko.Transport] = None
        self._socks_server_socket: Optional[socket.socket] = None
        self._server_thread: Optional[threading.Thread] = None
        self._running = False

    def _handle_socks_connection(self, client_socket: socket.socket):
        """Обрабатывает одно SOCKS5-соединение (упрощённая реализация)."""
        try:
            # SOCKS5 handshake
            ver = client_socket.recv(1)
            if ver != b"\x05":
                client_socket.close()
                return

            nmethods = client_socket.recv(1)[0]
            client_socket.recv(nmethods)  # skip methods
            client_socket.sendall(b"\x05\x00")  # no auth

            # Request
            header = client_socket.recv(4)
            if len(header) < 4:
                return
            ver, cmd, rsv, atyp = header
            if cmd != 1:  # CONNECT only
                client_socket.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return

            # Parse address
            if atyp == 1:  # IPv4
                addr = socket.inet_ntoa(client_socket.recv(4))
            elif atyp == 3:  # Domain
                addr_len = client_socket.recv(1)[0]
                addr = client_socket.recv(addr_len).decode()
            elif atyp == 4:  # IPv6
                addr = socket.inet_ntop(socket.AF_INET6, client_socket.recv(16))
            else:
                return

            port_raw = client_socket.recv(2)
            port = int.from_bytes(port_raw, "big")

            # Open channel to target via SSH
            try:
                chan = self._transport.open_channel(
                    kind="direct-tcpip",
                    dest_addr=(addr, port),
                    src_addr=(self.local_host, self.local_port),
                )
            except Exception as e:
                client_socket.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")  # host unreachable
                client_socket.close()
                return

            # SOCKS5 response: success
            client_socket.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")

            # Relay data
            def forward(src, dst):
                try:
                    while self._running:
                        data = src.recv(4096)
                        if not data:
                            break
                        dst.sendall(data)
                except:
                    pass
                finally:
                    src.close()
                    dst.close()

            threading.Thread(target=forward, args=(client_socket, chan), daemon=True).start()
            threading.Thread(target=forward, args=(chan, client_socket), daemon=True).start()

        except Exception:
            if client_socket:
                client_socket.close()

    def _socks_server_loop(self):
        """Основной цикл SOCKS5-сервера."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.local_host, self.local_port))
        sock.listen(5)
        self._socks_server_socket = sock

        while self._running:
            try:
                sock.settimeout(1.0)
                client, addr = sock.accept()
                threading.Thread(
                    target=self._handle_socks_connection,
                    args=(client,),
                    daemon=True
                ).start()
            except socket.timeout:
                continue
            except:
                break

    def start(self):
        """Запускает SSH-соединение и локальный SOCKS5-прокси."""
        if self._running:
            raise RuntimeError("SOCKS proxy already running")

        # Подключаемся по SSH
        try:
            if self.ssh_private_key:
                pkey = paramiko.RSAKey.from_private_key_file(self.ssh_private_key)
                self._ssh_client.connect(
                    hostname=self.ssh_host,
                    port=self.ssh_port,
                    username=self.ssh_username,
                    pkey=pkey,
                    timeout=self.timeout,
                    look_for_keys=False,
                    allow_agent=False,
                    banner_timeout=30
                )
            else:
                self._ssh_client.connect(
                    hostname=self.ssh_host,
                    port=self.ssh_port,
                    username=self.ssh_username,
                    password=self.ssh_password,
                    timeout=self.timeout,
                    look_for_keys=False,
                    allow_agent=False,
                )
            self._transport = self._ssh_client.get_transport()
            assert self._transport is not None
        except Exception as e:
            self._ssh_client.close()
            raise ConnectionError(f"SSH connection failed: {e}")

        # Запускаем SOCKS5-сервер в потоке
        self._running = True
        self._server_thread = threading.Thread(
            target=self._socks_server_loop, daemon=True, name="SOCKS-Server"
        )
        self._server_thread.start()

        # Ждём, пока сокет начнёт слушать
        time.sleep(0.2)
        if self._socks_server_socket is None:
            self.stop()
            raise RuntimeError("Failed to start SOCKS server")

        proxy_url = f"socks5://{self.local_host}:{self.local_port}"
        return proxy_url

    def stop(self):
        """Останавливает прокси и закрывает SSH."""
        self._running = False
        if self._socks_server_socket:
            self._socks_server_socket.close()
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=1)
        if self._ssh_client:
            self._ssh_client.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


# Удобная функция-обёртка
def create_proxy() -> Tuple[str, SSHSOCKSProxy]:
    proxy = SSHSOCKSProxy(
        ssh_host=SSH_HOST,
        ssh_port=SSH_PORT,
        ssh_username=SSH_USER,
        ssh_private_key=SSH_KEY,
        local_bind_address=("127.0.0.1", SSH_PORT),
    )
    url = proxy.start()
    return url, proxy  # возвращаем URL и объект для ручного .stop()



if __name__ == '__main__':
    proxy_dict, proxy = create_proxy()
    print(proxy_dict)

    try:
        print(f"SOCKS5 proxy ready")

        # Проверка
        session = requests.Session()
        session.proxies = proxy_dict
        resp = session.get("https://gas.rftoday.ru", timeout=10)
        print(resp.text)

    finally:
        proxy.stop()
        print("SSH SOCKS proxy stopped.")