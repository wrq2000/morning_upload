import subprocess
import os
import atexit
import time
import sys
import shlex
import requests


def start_ssh_socks_proxy(
    ssh_host: str,
    ssh_port: int = 20222,
    ssh_username: str = None,
    ssh_private_key: str = None,
    local_port: int = 20222
) -> tuple[subprocess.Popen, str]:
    """
    Запускает SSH-туннель с динамическим SOCKS-прокси (ssh -D).
    Кроссплатформенная версия (Windows/Linux/macOS).
    """
    # Подготавливаем команду
    cmd = ["ssh"]

    if ssh_port != 22:
        cmd += ["-p", str(ssh_port)]

    if ssh_private_key:
        key_path = os.path.expanduser(ssh_private_key)
        if not os.path.isfile(key_path):
            raise FileNotFoundError(f"SSH-ключ не найден: {key_path}")
        cmd += ["-i", key_path]

    # Отключаем интерактивные проверки
    cmd += [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=NUL" if sys.platform == "win32" else "/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
    ]

    # Ключевые флаги:
    # -N  — не выполнять команду (только туннель)
    # -D  — динамическое пробрасывание (SOCKS5)
    cmd += ["-N", "-D", f"127.0.0.1:{local_port}"]

    # Целевой хост
    target = f"{ssh_username}@{ssh_host}" if ssh_username else ssh_host
    cmd.append(target)

    print(f"→ Запуск SSH SOCKS-прокси: {' '.join(shlex.quote(c) for c in cmd)}")

    # Параметры запуска — кроссплатформенно
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # для корректного завершения через Ctrl+C / terminate()

    # Запускаем ssh
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creationflags,
    )

    # Ждём, пока туннель инициализируется (обычно 1–3 сек)
    time.sleep(3)

    # Проверяем, не завершился ли процесс аварийно
    if proc.poll() is not None:
        out, _ = proc.communicate()
        raise RuntimeError(f"SSH завершился с ошибкой (код {proc.returncode}):\n{out}")

    # Регистрируем аварийное завершение при выходе
    def cleanup():
        if proc.poll() is None:
            if sys.platform == "win32":
                # На Windows используем taskkill, если terminate() не сработал
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            else:
                proc.terminate()
                proc.wait(timeout=5)
    atexit.register(cleanup)

    proxy_url = f"socks5://127.0.0.1:{local_port}"
    return proc, proxy_url


def get_requests_session_with_socks(proxy_url: str) -> requests.Session:
    session = requests.Session()
    session.proxies = {"http": proxy_url, "https": proxy_url}
    return session
