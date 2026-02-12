"""
common/mailer.py
Модуль для отправки почты с поддержкой динамических получателей
"""
import smtplib
import os
import logging
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from dotenv import load_dotenv

# Настройка логирования (без вызова setup_logging — это делает точка входа)
logger = logging.getLogger(__name__)
#

class MailSender:
    """Класс для отправки почты с вложениями"""

    def __init__(self):
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.yandex.ru")
        self.smtp_port = int(os.getenv("SMTP_PORT", 587))
        self.smtp_email = os.getenv("SMTP_EMAIL")
        self.smtp_password = os.getenv("SMTP_PASSWORD")

        # 🔑 ДИАГНОСТИКА: временно для отладки (удалить в продакшене!)
        if not self.smtp_email or not self.smtp_password:
            logger.error("❌ SMTP_EMAIL или SMTP_PASSWORD не загружены из .env!")
            logger.error(f"SMTP_EMAIL: {self.smtp_email}")
            logger.error(f"SMTP_PASSWORD: {'*' * len(self.smtp_password) if self.smtp_password else 'NOT SET'}")
            logger.error(f"Текущая директория: {os.getcwd()}")
            logger.error(f"Путь к .env: {Path('.env').resolve()}")
            raise ValueError("SMTP_EMAIL и SMTP_PASSWORD должны быть указаны в .env файле")
        else:
            logger.debug(f"✅ SMTP настройки загружены: {self.smtp_email}@{self.smtp_server}")

    def send_mail(self, file_paths=None, subject=None, body=None, recipients=None):
        """
        Отправка письма с вложениями или без

        Args:
            file_paths: Список путей к файлам для отправки (опционально)
            subject: Тема письма
            body: Текст письма
            recipients: Список получателей или строка через запятую (опционально, берется из .env если не указано)

        Returns:
            bool: Успешность отправки
        """
        # Определяем получателей
        if recipients is None:
            recipients_str = os.getenv("RECIPIENTS", "")
        elif isinstance(recipients, list):
            recipients_str = ",".join(recipients)
        else:
            recipients_str = str(recipients)

        recipient_list = [r.strip() for r in recipients_str.split(",") if r.strip()]

        if not recipient_list:
            logger.error("Не указаны получатели для отправки письма")
            return False

        try:
            # Создаем сообщение
            msg = MIMEMultipart()
            msg['From'] = self.smtp_email
            msg['To'] = ", ".join(recipient_list)
            msg['Subject'] = subject or "Отчет системы сбора данных"

            # Добавляем тело письма
            msg.attach(MIMEText(body or "", 'plain', 'utf-8'))

            # Добавляем вложения, если есть
            if file_paths:
                logger.info(f"Добавление {len(file_paths)} вложений...")
                for file_path in file_paths:
                    if not os.path.exists(file_path):
                        logger.warning(f"Файл не найден: {file_path}")
                        continue

                    try:
                        file_size = os.path.getsize(file_path)
                        file_name = os.path.basename(file_path)
                        logger.info(f"Добавление вложения: {file_name} ({file_size / 1024:.2f} КБ)")

                        with open(file_path, "rb") as attachment:
                            part = MIMEBase('application', 'octet-stream')
                            part.set_payload(attachment.read())

                        encoders.encode_base64(part)
                        # 🔑 ИСПРАВЛЕНИЕ: используем правильный синтаксис заголовка
                        part.add_header(
                            'Content-Disposition',
                            f'attachment; filename="{file_name}"'
                        )
                        msg.attach(part)

                    except Exception as e:
                        logger.error(f"Ошибка при добавлении файла {file_path}: {str(e)}")
                        continue

            # Отправка
            logger.info(f"Подключение к SMTP серверу: {self.smtp_server}:{self.smtp_port}")
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.smtp_email, self.smtp_password)

            logger.info(f"Отправка письма на {len(recipient_list)} получателей...")
            server.send_message(msg)
            server.quit()

            logger.info("✅ Письмо успешно отправлено!")
            return True

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"❌ Ошибка аутентификации SMTP: {str(e)}")
            logger.error("Проверьте:")
            logger.error("  1. Правильность SMTP_EMAIL и SMTP_PASSWORD в .env")
            logger.error("  2. Для Яндекса: включена ли 'Пароли приложений' в настройках почты")
            logger.error("  3. Для Gmail: включена ли 'Двухфакторная аутентификация' и создан ли 'Пароль приложения'")
            return False
        except smtplib.SMTPConnectError:
            logger.error(f"❌ Ошибка подключения к серверу {self.smtp_server}:{self.smtp_port}")
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка отправки письма: {str(e)}", exc_info=True)
            return False

    def test_connection(self):
        """Тестирование подключения к почтовому серверу"""
        try:
            logger.info("Тестирование подключения к SMTP серверу...")
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.smtp_email, self.smtp_password)
            server.quit()
            logger.info("✅ Подключение успешно!")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка подключения: {str(e)}")
            return False


def send_mail(file_paths=None, subject=None, body=None, recipients=None):
    """
    Удобная функция для отправки письма

    Args:
        file_paths: Список путей к файлам (опционально)
        subject: Тема письма
        body: Текст письма
        recipients: Получатели (строка или список)

    Returns:
        bool: Успешность отправки
    """
    sender = MailSender()
    return sender.send_mail(
        file_paths=file_paths,
        subject=subject,
        body=body,
        recipients=recipients
    )


if __name__ == "__main__":
    load_dotenv()
    # Настройка логирования для тестового запуска
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s'
    )

    # Тест отправки без вложений
    success = send_mail(
        subject="Тестовое письмо из mailer.py",
        body="Это тестовое письмо для проверки работы модуля.",
        recipients="vologzhaninov@gmail.com"
    )
    print(f"\n{'✅ УСПЕШНО' if success else '❌ ОШИБКА'}: Результат отправки: {success}")