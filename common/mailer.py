"""Письма с вложениями: основной SMTP и резервный сервис; прежний интерфейс сохранён."""
import logging
import math
import mimetypes
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from pathlib import Path

logger = logging.getLogger(__name__)


def recipient_list(value):
    if value is None:
        value = os.getenv("RECIPIENTS", "")
    items = value if isinstance(value, (list, tuple)) else [str(value)]
    addresses = [address.strip() for _, address in getaddresses(items) if address.strip()]
    if any("@" not in address or "\n" in address or "\r" in address for address in addresses):
        raise ValueError("Некорректный список получателей")
    return list(dict.fromkeys(addresses))


@dataclass(frozen=True)
class SMTPProfile:
    name: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    sender: str
    security: str
    timeout: float


def _profile(prefix, name):
    host = os.getenv(prefix + "SERVER", "smtp.yandex.ru" if name == "primary" else "").strip()
    email = os.getenv(prefix + "EMAIL", "").strip()
    password = os.getenv(prefix + "PASSWORD", "")
    if not host or not email or not password:
        raise ValueError(prefix + "SERVER/EMAIL/PASSWORD не заполнены")
    port = int(os.getenv(prefix + "PORT", "587"))
    security = os.getenv(prefix + "SECURITY", "ssl" if port == 465 else "starttls").strip().casefold()
    timeout = float(os.getenv(prefix + "TIMEOUT", "30"))
    if not 1 <= port <= 65535 or not math.isfinite(timeout) or timeout <= 0 or security not in {"ssl", "starttls"}:
        raise ValueError(prefix + "PORT/SECURITY/TIMEOUT: неверные настройки")
    return SMTPProfile(name, host, port, os.getenv(prefix + "USERNAME", email).strip(), password,
                       os.getenv(prefix + "FROM", email).strip(), security, timeout)


class MailSender:
    """Основной сервис → резервный; уже принятые получатели повторно не отправляются."""

    def __init__(self):
        self.profiles = []
        for prefix, name in (("SMTP_", "primary"), ("SMTP_BACKUP_", "backup")):
            if name == "backup" and not any(os.getenv(prefix + key) for key in ("SERVER", "EMAIL", "PASSWORD")):
                continue
            try:
                self.profiles.append(_profile(prefix, name))
            except ValueError as exc:
                logger.warning("Настройки почты %s: %s", name, exc)
        if not self.profiles:
            raise ValueError("Не настроен ни основной SMTP_*, ни резервный SMTP_BACKUP_* сервис")
        first = self.profiles[0]
        # Совместимые атрибуты прежнего MailSender.
        self.smtp_server, self.smtp_port = first.host, first.port
        self.smtp_email, self.smtp_password = first.sender, first.password
        self.last_delivery = {"accepted": [], "pending": [], "providers": []}

    @staticmethod
    def _connect(profile):
        context = ssl.create_default_context()
        server = None
        try:
            if profile.security == "ssl":
                server = smtplib.SMTP_SSL(profile.host, profile.port, timeout=profile.timeout, context=context)
            else:
                server = smtplib.SMTP(profile.host, profile.port, timeout=profile.timeout)
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            server.login(profile.username, profile.password)
            return server
        except Exception:
            if server is not None:
                server.close()
            raise

    @staticmethod
    def _close(server):
        if server is not None:
            try:
                server.quit()
            except Exception:
                # Ошибка QUIT после принятого DATA не отменяет подтверждённую отправку.
                try:
                    server.close()
                except Exception:
                    pass

    def send_mail(self, file_paths=None, subject=None, body=None, recipients=None):
        """Прежние аргументы; False означает, что остались неподтверждённые получатели."""
        self.last_delivery = {"accepted": [], "pending": [], "providers": []}
        try:
            pending = recipient_list(recipients)
            if not pending:
                raise ValueError("Не указаны получатели для отправки письма")
            attachments = []
            for file_path in file_paths or []:
                path = Path(file_path)
                if not path.is_file():
                    raise FileNotFoundError("Не найдено вложение: " + str(path))
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                payload = path.read_bytes()
                attachments.append((path.name, payload, mime.split("/", 1)))
                logger.info("Вложение: %s (%.2f КБ)", path.name, len(payload) / 1024)
        except Exception as exc:
            logger.error("Письмо не подготовлено: %s", exc)
            return False

        self.last_delivery = {"accepted": [], "pending": list(pending), "providers": []}
        message_id = make_msgid()
        for profile in self.profiles:
            server = None
            try:
                msg = EmailMessage()
                msg["From"], msg["To"] = profile.sender, ", ".join(pending)
                msg["Subject"] = subject or "Отчет системы сбора данных"
                msg["Date"], msg["Message-ID"] = formatdate(localtime=True), message_id
                msg.set_content(body or "", charset="utf-8")
                for name, payload, (maintype, subtype) in attachments:
                    msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=name)
                logger.info("SMTP %s: %s:%s; получателей %s", profile.name, profile.host, profile.port, len(pending))
                server = self._connect(profile)
                refused = server.send_message(msg, from_addr=profile.sender, to_addrs=pending) or {}
                refused_keys = {address.casefold() for address in refused}
                accepted = [address for address in pending if address.casefold() not in refused_keys]
                self.last_delivery["accepted"].extend(accepted)
                self.last_delivery["providers"].append({"name": profile.name, "accepted": len(accepted)})
                pending = [address for address in pending if address.casefold() in refused_keys]
                self.last_delivery["pending"] = list(pending)
                if not pending:
                    logger.info("Письмо принято SMTP-сервисом %s", profile.name)
                    return True
                logger.warning("SMTP %s отклонил %s получателей; пробуем резервный сервис", profile.name, len(pending))
            except Exception as exc:
                logger.warning("SMTP %s: %s; код %s", profile.name, type(exc).__name__, getattr(exc, "smtp_code", "нет"))
            finally:
                self._close(server)
        logger.error("Отправка не подтверждена для %s получателей", len(pending))
        return False

def send_mail(file_paths=None, subject=None, body=None, recipients=None):
    """Совместимая функция отправки для всех прежних процессов."""
    return MailSender().send_mail(file_paths=file_paths, subject=subject, body=body, recipients=recipients)
