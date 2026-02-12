"""
processes/desfa.py
Парсер для мониторинга и скачивания годового плана СПГ с сайта DESFA (Греция)
"""
import os
from pathlib import Path
from urllib.parse import urlparse, unquote
from datetime import datetime
import json
from bs4 import BeautifulSoup
import requests
from dotenv import load_dotenv
from common.mailer import send_mail
from connection.ssh_tool import create_proxy
from settings import desfa_unloading_plan
import logging
logger = logging.getLogger(__name__)

class DesfaUnloadingPlanProcess:
    """
    Парсер для мониторинга годового плана СПГ на сайте DESFA

    Функциональность:
      - Проверяет наличие файла "annual-lng-plan-for-2026"
      - Отслеживает изменения версии файла
      - Скачивает файл при обнаружении изменений (через прокси)
      - Отправляет уведомления на почту
      - Сохраняет состояние между запусками
    """

    def __init__(self, url=None, state_dir="./state", download_dir="./downloads", use_proxy=True):
        """
        Инициализация парсера

        Args:
            url: URL страницы с информацией о плане СПГ
            state_dir: Папка для хранения состояния
            download_dir: Папка для временного сохранения файлов
            use_proxy: Использовать прокси для запросов
        """
        # Настройки
        self.url = (url or desfa_unloading_plan).strip()
        self.state_dir = Path(state_dir)
        self.download_dir = Path(download_dir)
        self.state_file = self.state_dir / "desfa_last_state.json"
        self.use_proxy = use_proxy

        # Создаем необходимые директории
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # Email настройки
        self.success_recipients = os.getenv("DESFA_SUCCESS_RECIPIENTS") or os.getenv("RECIPIENTS")
        self.error_recipients = os.getenv("DESFA_ERROR_RECIPIENTS") or os.getenv("ADMIN_RECIPIENTS") or self.success_recipients

        # Настройки поведения
        self.send_notification_if_not_found = os.getenv("DESFA_SEND_NOTIFICATION_IF_NOT_FOUND", "true").lower() == "true"

        # Валидация
        if not self.success_recipients:
            raise ValueError(
                "Не указаны получатели для уведомлений DESFA "
                "(DESFA_SUCCESS_RECIPIENTS или RECIPIENTS в .env)"
            )

    def _extract_filename_from_url(self, url):
        """Извлекает имя файла из URL"""
        parsed = urlparse(url)
        path = unquote(parsed.path)
        filename = Path(path).name

        if '?' in filename:
            filename = filename.split('?')[0]

        if not filename or len(filename) < 3:
            ext = os.path.splitext(parsed.path)[1] or '.pdf'
            filename = f"lng_plan_2026{ext}"

        return filename

    def _download_file(self, url, proxy_url=None):
        """
        Скачивает файл по ссылке с использованием прокси

        Args:
            url: URL файла
            proxy_url: URL прокси сервера

        Returns:
            Path: Путь к скачанному файлу или None при ошибке
        """
        try:
            logger.info(f"📥 Скачивание файла: {url}")

            proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None

            response = requests.get(url, proxies=proxies, timeout=60)
            response.raise_for_status()

            # Определяем имя файла
            filename = self._extract_filename_from_url(url)
            save_path = self.download_dir / filename

            # Сохраняем файл
            with open(save_path, 'wb') as f:
                f.write(response.content)

            logger.info(f"✅ Файл сохранён: {save_path}")
            return save_path

        except Exception as e:
            logger.error(f"❌ Ошибка при скачивании файла {url}: {str(e)}")
            return None

    def _load_state(self):
        """
        Загружает последнее состояние из файла

        Returns:
            dict: Состояние или None если файл не существует
        """
        if not self.state_file.exists():
            logger.info("📄 Файл состояния не найден — первый запуск")
            return None

        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
                logger.info(
                    f"📂 Загружено состояние: версия='{state.get('last_version')}', "
                    f"url='{state.get('last_url')}'"
                )
                return state
        except Exception as e:
            logger.warning(f"⚠️  Ошибка загрузки состояния: {str(e)}. Будет создано новое состояние.")
            return None

    def _save_state(self, version, url):
        """
        Сохраняет текущее состояние в файл

        Args:
            version: Версия файла
            url: URL файла
        """
        try:
            state = {
                'last_version': version,
                'last_url': url,
                'last_check': datetime.now().isoformat(),
                'last_success': datetime.now().isoformat()
            }
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 Состояние сохранено: версия='{version}', url='{url}'")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения состояния: {str(e)}")

    def _send_success_email(self, version, url, file_path, reason):
        """Отправка письма с успешно скачанным файлом"""
        subject = f"Annual LNG Plan for 2026 — {version}"

        body = f"""Добрый день!

Обнаружено обновление годового плана поставок СПГ на 2026 год от DESFA.

Причина отправки: {reason}

Версия: {version}
Источник: {self.url}
Файл: {url}
Дата проверки: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}

С уважением,
Автоматизированная система мониторинга
"""

        logger.info(f"📤 Отправка файла на {self.success_recipients}")

        try:
            success = send_mail(
                file_paths=[file_path] if file_path else None,
                subject=subject,
                body=body,
                recipients=self.success_recipients
            )
            return success
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке успешного письма: {str(e)}", exc_info=True)
            return False

    def _send_error_email(self, error_type, error_message, version=None, url=None):
        """
        Отправка уведомления об ошибке

        Args:
            error_type: Тип ошибки ("not_found", "download_error", "critical_error")
            error_message: Описание ошибки
            version: Версия файла (опционально)
            url: URL файла (опционально)
        """
        if error_type == "not_found":
            subject = "⚠️ Annual LNG Plan for 2026 — Файл не найден"
            body = f"""Добрый день!

⚠️ Внимание: Файл с годовым планом поставок СПГ на 2026 год НЕ НАЙДЕН на сайте DESFA.

Причина отправки: {error_message}

Источник: {self.url}
Дата проверки: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}

Возможные причины:
• Файл ещё не опубликован
• Изменилась структура сайта
• URL файла изменился

Рекомендуется проверить сайт вручную: {self.url}

С уважением,
Автоматизированная система мониторинга
"""
        elif error_type == "download_error":
            subject = "❌ Ошибка при обработке Annual LNG Plan for 2026"
            body = f"""Добрый день!

Произошла ошибка при обработке годового плана СПГ:

Версия: {version or 'N/A'}
URL: {url or 'N/A'}
Ошибка: {error_message}
Дата: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}

С уважением,
Автоматизированная система мониторинга
"""
        else:  # critical_error
            subject = "❌ Критическая ошибка при получении Annual LNG Plan for 2026"
            body = f"""Добрый день!

❌ Произошла критическая ошибка при выполнении скрипта получения годового плана СПГ.

Источник: {self.url}
Дата ошибки: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}
Ошибка: {error_message}

Пожалуйста, проверьте логи и настройки скрипта.

С уважением,
Автоматизированная система мониторинга
"""

        logger.info(f"⚠️  Отправка уведомления об ошибке на {self.error_recipients}")

        try:
            success = send_mail(
                subject=subject,
                body=body,
                recipients=self.error_recipients
            )
            return success
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке уведомления об ошибке: {str(e)}", exc_info=True)
            return False

    def run(self):
        """
        Основной метод запуска парсера

        Returns:
            dict: Результат выполнения {
                'success': bool,              # Успешное выполнение
                'file_downloaded': bool,      # Файл был скачан
                'version_changed': bool,      # Версия изменилась
                'version': str,               # Текущая версия
                'url': str,                   # URL файла
                'email_sent': bool            # Отправлено ли письмо
            }
        """
        logger.info(f"🚀 Запуск парсера DESFA")
        logger.info(f"🌐 URL: {self.url}")
        logger.info(f"🔗 Использование прокси: {'Да' if self.use_proxy else 'Нет'}")

        result = {
            'success': False,
            'file_downloaded': False,
            'version_changed': False,
            'version': None,
            'url': None,
            'email_sent': False
        }

        # Инициализация прокси
        proxy_url = None
        proxy = None

        try:
            # === Создание прокси (если требуется) ===
            if self.use_proxy:
                try:
                    proxy_url, proxy = create_proxy()
                    logger.info(f"✅ Прокси создан: {proxy_url}")
                except Exception as e:
                    logger.error(f"❌ Ошибка создания прокси: {str(e)}")
                    # Продолжаем без прокси
                    proxy_url = None
                    proxy = None

            # === Шаг 1: Получение страницы ===
            logger.info("➡️  Получение страницы...")
            proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None

            response = requests.get(self.url, proxies=proxies, timeout=30)
            logger.info(f"✅ Страница получена. Статус: {response.status_code}")

            # === Шаг 2: Парсинг HTML ===
            soup = BeautifulSoup(response.content, 'html.parser')

            # Ищем все ссылки с текстом "annual-lng-plan-for-2026"
            target_text = "annual-lng-plan-for-2026"
            matching_links = []

            for link in soup.find_all('a'):
                href = link.get('href', '')
                if target_text in href.lower():
                    link_text = link.get_text(strip=True)
                    full_url = requests.compat.urljoin(self.url, href)
                    matching_links.append({
                        'text': link_text,
                        'href': href,
                        'full_url': full_url
                    })

            # === Шаг 3: Загрузка предыдущего состояния ===
            prev_state = self._load_state()

            # === Шаг 4: Определение необходимости отправки ===
            should_send = False
            reason = ""
            current_version = None
            current_url = None

            if not matching_links:
                logger.warning("❌ Ссылки не найдены")
                reason = "Ссылка не найдена на странице"
                should_send = self.send_notification_if_not_found

                # Отправляем уведомление о ненахождении
                if should_send:
                    email_success = self._send_error_email(
                        error_type="not_found",
                        error_message=reason
                    )
                    result['email_sent'] = email_success

                result['success'] = True  # Это не ошибка, просто файл не найден
                return result

            # Берем первую найденную ссылку
            current_version = matching_links[0]['text']
            current_url = matching_links[0]['full_url']

            result['version'] = current_version
            result['url'] = current_url

            # Логика определения необходимости отправки
            if prev_state is None:
                logger.info("🆕 Первый запуск — отправляем файл")
                should_send = True
                reason = "Первый запуск системы"
            elif prev_state.get('last_version') != current_version or prev_state.get('last_url') != current_url:
                logger.info(
                    f"🔄 Обнаружено изменение: "
                    f"версия='{prev_state.get('last_version')}' → '{current_version}'"
                )
                logger.info(
                    f"URL: '{prev_state.get('last_url')}' → '{current_url}'"
                )
                should_send = True
                result['version_changed'] = True
                reason = f"Изменена версия плана: {prev_state.get('last_version')} → {current_version}"
            else:
                logger.info("✅ Версия и ссылка не изменились — отправка не требуется")
                result['success'] = True
                return result

            # === Шаг 5: Скачивание и отправка файла ===
            if should_send:
                logger.info(f"📄 Текст ссылки: {current_version}")
                logger.info(f"🔗 Полный URL: {current_url}")

                # Скачиваем файл (с прокси)
                file_path = self._download_file(current_url, proxy_url)

                if file_path:
                    result['file_downloaded'] = True

                    # Отправляем письмо с файлом
                    email_success = self._send_success_email(
                        version=current_version,
                        url=current_url,
                        file_path=file_path,
                        reason=reason
                    )
                    result['email_sent'] = email_success

                    # Сохраняем новое состояние
                    self._save_state(current_version, current_url)

                    # Удаляем временный файл
                    try:
                        os.remove(file_path)
                        logger.info(f"🗑️  Временный файл удалён: {file_path}")
                    except Exception as e:
                        logger.warning(f"⚠️  Не удалось удалить временный файл {file_path}: {str(e)}")
                else:
                    # Ошибка скачивания — отправляем уведомление
                    logger.error("❌ Не удалось скачать файл")
                    email_success = self._send_error_email(
                        error_type="download_error",
                        error_message="Ошибка при скачивании файла",
                        version=current_version,
                        url=current_url
                    )
                    result['email_sent'] = email_success
                    result['success'] = False
                    return result

            result['success'] = True
            return result

        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ Критическая ошибка при выполнении скрипта: {error_msg}", exc_info=True)

            # Отправляем уведомление о критической ошибке
            self._send_error_email(
                error_type="critical_error",
                error_message=error_msg
            )

            result['success'] = False
            return result

        finally:
            # === Остановка прокси ===
            if proxy:
                try:
                    proxy.stop()
                    logger.info("🔌 Прокси остановлен")
                except Exception as e:
                    logger.error(f"❌ Ошибка при остановке прокси: {str(e)}")

    @classmethod
    def get_name(cls):
        """Возвращает имя парсера для логирования и идентификации"""
        return "desfa"


# Для тестирования напрямую
if __name__ == "__main__":
    load_dotenv()
    parser = DesfaUnloadingPlanProcess()
    result = parser.run()

    logger.info("\n" + "=" * 60)
    logger.info("📊 ИТОГИ РАБОТЫ DESFA PARSER")
    logger.info("=" * 60)
    logger.info(f"Успешно: {'✅ Да' if result['success'] else '❌ Нет'}")
    logger.info(f"Файл скачан: {'✅ Да' if result['file_downloaded'] else '❌ Нет'}")
    logger.info(f"Версия изменилась: {'✅ Да' if result['version_changed'] else '❌ Нет'}")
    if result['version']:
        logger.info(f"Текущая версия: {result['version']}")
    logger.info(f"Письмо отправлено: {'✅ Да' if result['email_sent'] else '❌ Нет'}")
    logger.info("=" * 60)

    if result['success']:
        logger.info("\n🎉 Парсер завершил работу успешно!")
    else:
        logger.error("\n❌ Парсер завершил работу с ошибками")