"""
processes/argus.py
Парсер для скачивания и отправки отчетов с Argus Media
"""
import os
import time
import logging
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from common.logfile import setup_logging
from common.mailer import send_mail


# Настройка логирования
logger = logging.getLogger(__name__)


class ArgusProcess:
    """
    Парсер для скачивания и отправки отчетов с Argus Media
    Полностью автономный: скачивает + отправляет свои отчеты
    Стратегия отправки:
      - Все отчеты скачаны → отправка файлов на основной email
      - Есть ошибки → отправка уведомления об ошибке на другой email (без файлов)
    """

    def __init__(self, email=None, password=None, report_ids=None,
                 base_url=None, download_dir="./downloads"):
        """
        Инициализация парсера

        Args:
            email: Email для входа в Argus (если не указан — берется из .env)
            password: Пароль Argus (если не указан — берется из .env)
            report_ids: Список ID отчетов для скачивания
            base_url: Базовый URL для отчетов
            download_dir: Папка для сохранения файлов
        """

        # Настройки Argus (КРИТИЧЕСКИ ВАЖНО: убраны ВСЕ пробелы из URL!)
        self.email = email or os.getenv("ARGUS_EMAIL")
        self.password = password or os.getenv("ARGUS_PASSWORD")

        # ИСПРАВЛЕНО: убраны пробелы из базового URL (включая конец строки!)
        self.base_url = (base_url or "https://direct.argusmedia.com/integration/excel/publicationpricereport/").strip()
        self.login_url = "https://myaccount.argusmedia.com/login?ReturnUrl=https://direct.argusmedia.com/".strip()

        self.report_ids = report_ids or [1000027, 1000023, 1000005, 1000086, 38]
        self.download_dir = download_dir

        # Email получатели
        self.success_recipients = os.getenv("ARGUS_SUCCESS_RECIPIENTS") or os.getenv("RECIPIENTS")
        self.error_recipients = os.getenv("ARGUS_ERROR_RECIPIENTS") or os.getenv("ADMIN_RECIPIENTS") or self.success_recipients

        # Валидация
        if not self.email or not self.password:
            raise ValueError(
                "Email и пароль для Argus Media должны быть указаны "
                "(через параметры или .env: ARGUS_EMAIL, ARGUS_PASSWORD)"
            )

        if not self.success_recipients:
            raise ValueError(
                "Не указаны получатели для успешных отчетов "
                "(ARGUS_SUCCESS_RECIPIENTS или RECIPIENTS в .env)"
            )

        # Создаем папку для скачивания
        os.makedirs(self.download_dir, exist_ok=True)

    # def _login(self, page):
    #     """
    #     Авторизация на сайте Argus Media
    #
    #     Args:
    #         page: Playwright Page объект
    #
    #     Returns:
    #         str: URL домашней страницы после входа
    #     """
    #     logger.info("➡️  Переход на страницу логина...")
    #     page.goto(self.login_url)
    #     page.fill('input[type="email"]', self.email)
    #     page.click('button:has-text("Next")')
    #     page.wait_for_selector('input[type="password"]', timeout=10000)
    #
    #     logger.info("🔑 Ввод пароля...")
    #     page.fill('input[type="password"]', self.password)
    #     page.click('button:has-text("Войти")')
    #
    #     # ДОПОЛНИТЕЛЬНАЯ ПАУЗА после логина для инициализации сессии
    #     logger.info("⏳ Ожидание полной инициализации сессии...")
    #     page.wait_for_timeout(3000)  # 3 секунды для стабилизации сессии
    #
    #     page.wait_for_load_state('networkidle', timeout=25000)
    #     logger.info("✅ Авторизация успешна!")
    #     return page.url
    def _login(self, page):
        """
        Авторизация на сайте Argus Media с обходом bot-защиты
        """
        # === Шаг 1: Переход с мягким ожиданием ===
        logger.info(f"➡️ Переход на страницу логина: {self.login_url}")
        try:
            # Используем 'commit' вместо 'load' — ждём только HTML, без всех ресурсов
            page.goto(self.login_url, wait_until="commit", timeout=60000)
            logger.info("✅ Страница начата загрузку")
        except Exception as e:
            logger.error(f"❌ Ошибка при переходе на страницу логина: {str(e)[:200]}")
            # Сохраняем скриншот и HTML для диагностики
            self._save_debug_artifacts(page, "login_timeout")
            raise

        # === Шаг 2: Обработка Cloudflare / bot-защиты ===
        logger.info("🛡️ Проверка наличия Cloudflare/bot-защиты...")
        if page.query_selector("div.cf-turnstile") or page.query_selector("iframe[title*='challenge']"):
            logger.warning("⚠️ Обнаружена Cloudflare Turnstile! Ждём автоматического решения...")
            # Cloudflare обычно решается автоматически в течение 5-15 сек в не-headless режиме
            page.wait_for_timeout(15000)

        # Альтернативная проверка: сообщение о включении JS/cookies
        if "enable javascript" in page.content().lower() or "checking your browser" in page.content().lower():
            logger.warning("⚠️ Сайт проверяет браузер (Cloudflare). Дополнительное ожидание...")
            page.wait_for_timeout(20000)

        # === Шаг 3: Ожидание поля ввода email ===
        logger.info("⏳ Ожидание поля ввода email...")
        try:
            page.wait_for_selector('input[type="email"]', timeout=45000)
        except Exception as e:
            logger.error("❌ Поле ввода email не появилось. Сохраняем артефакты для отладки...")
            self._save_debug_artifacts(page, "no_email_field")
            raise

        # === Шаг 4: Стандартный логин ===
        logger.info("📧 Ввод email...")
        page.fill('input[type="email"]', self.email)
        page.click('button:has-text("Next")')

        logger.info("⏳ Ожидание поля пароля...")
        page.wait_for_selector('input[type="password"]', timeout=20000)

        logger.info("🔑 Ввод пароля...")
        page.fill('input[type="password"]', self.password)
        page.click('button:has-text("Войти")')

        # === Шаг 5: Ожидание успешного входа ===
        logger.info("⏳ Ожидание завершения авторизации...")
        # Ждём появления элемента, характерного для залогиненной страницы
        try:
            page.wait_for_selector('text=Logout', timeout=30000)
        except:
            # Или проверяем URL
            if "direct.argusmedia.com" not in page.url:
                logger.warning("⚠️ Стандартные признаки логина не найдены, но продолжаем...")
                page.wait_for_timeout(5000)  # Доп. пауза на случай медленной загрузки

        logger.info("✅ Авторизация успешна!")
        return page.url

    def _save_debug_artifacts(self, page, prefix="debug"):
        """Сохранение скриншота и HTML для диагностики ошибок"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.join(self.download_dir, f"{prefix}_{timestamp}.png")
            html_path = os.path.join(self.download_dir, f"{prefix}_{timestamp}.html")

            page.screenshot(path=screenshot_path, full_page=True)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(page.content())

            logger.info(f"📸 Скриншот сохранён: {screenshot_path}")
            logger.info(f"📄 HTML сохранён: {html_path}")

            # Логируем видимый текст для быстрой диагностики
            visible_text = page.text_content("body")[:500] if page.query_selector("body") else "N/A"
            logger.info(f"👀 Видимый текст на странице (первые 500 символов):\n{visible_text}")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения артефактов: {str(e)}")

    def _download_report(self, page, report_id, home_url):
        """
        Скачивание одного отчета

        Args:
            page: Playwright Page объект
            report_id: ID отчета
            home_url: URL домашней страницы для возврата при ошибках

        Returns:
            str: Путь к скачанному файлу или None при ошибке
        """
        # ФОРМИРУЕМ ЧИСТЫЙ URL без пробелов
        report_url = f"{self.base_url}{report_id}".strip()
        logger.info(f"📥 Скачивание отчета ID {report_id}: {report_url}")

        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Возвращаемся на стабильную страницу перед скачиванием
                if attempt > 0:
                    logger.warning(f"🔄 Попытка {attempt + 1}/{max_retries} для отчета {report_id}...")
                    page.goto(home_url, timeout=30000)
                    page.wait_for_load_state('networkidle', timeout=30000)
                else:
                    # Первая попытка: даем странице полностью стабилизироваться
                    page.wait_for_timeout(1500)  # 1.5 сек для стабилизации после предыдущего действия

                # Инициируем скачивание через JavaScript
                with page.expect_download(timeout=25000) as download_info:
                    page.evaluate("""url => {
                        const maxWait = 5000;
                        const startTime = Date.now();
                        
                        return new Promise((resolve) => {
                            const checkBody = () => {
                                if (document.body) {
                                    const a = document.createElement('a');
                                    a.href = url;
                                    document.body.appendChild(a);
                                    a.click();
                                    setTimeout(() => {
                                        if (document.body && a.parentNode) {
                                            document.body.removeChild(a);
                                        }
                                    }, 100);
                                    resolve();
                                } else if (Date.now() - startTime < maxWait) {
                                    setTimeout(checkBody, 100);
                                } else {
                                    throw new Error('document.body is null');
                                }
                            };
                            checkBody();
                        });
                    }""", report_url)

                # Сохраняем файл С ПЕРЕЗАПИСЬЮ существующего файла
                download = download_info.value
                filename = download.suggested_filename
                filepath = os.path.join(self.download_dir, filename)

                # 🔑 КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: перезаписываем существующий файл вместо добавления суффикса
                if os.path.exists(filepath):
                    logger.warning(f"⚠️  Файл уже существует, перезаписываем: {filename}")
                    try:
                        os.remove(filepath)
                        logger.info(f"✅ Старый файл удален: {filename}")
                    except Exception as e:
                        logger.error(f"❌ Ошибка удаления старого файла {filename}: {str(e)}")
                        # Продолжаем попытку сохранения — download.save_as() перезапишет файл

                download.save_as(filepath)

                # Проверка успешного сохранения
                if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                    raise Exception(f"Файл не сохранен или пустой: {filepath}")

                size_kb = os.path.getsize(filepath) / 1024
                logger.info(f"✅ Успешно! {size_kb:.2f} КБ → {filename}")

                # 🔑 ДОПОЛНИТЕЛЬНАЯ СТАБИЛИЗАЦИЯ: пауза после скачивания
                page.wait_for_timeout(2000)  # 2 секунды для стабилизации сессии

                return filepath

            except Exception as e:
                error_msg = str(e)[:150]
                if attempt < max_retries - 1:
                    logger.warning(f"⚠️  Попытка {attempt + 1} не удалась для отчета {report_id}: {error_msg}")
                    time.sleep(2)
                else:
                    logger.error(f"❌ Ошибка после {max_retries} попыток для отчета {report_id}: {error_msg}")
                    return None

    def _send_success_email(self, file_paths):
        """Отправка письма с успешно скачанными отчетами"""
        if not file_paths:
            logger.warning("📭 Нет файлов для отправки успешного письма")
            return False

        subject = f"Argus Media Reports - {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        file_list = '\n'.join([
            f'• {os.path.basename(f)} ({os.path.getsize(f) / 1024:.2f} КБ)'
            for f in file_paths
        ])

        body = f"""
Добрый день!

Успешно скачаны отчеты Argus Media.

Список файлов:
{file_list}

Всего файлов: {len(file_paths)}
Общий размер: {sum(os.path.getsize(f) for f in file_paths) / 1024:.2f} КБ

Дата скачивания: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

--
С уважением,
Автоматизированная система сбора данных
"""

        logger.info(f"📤 Отправка {len(file_paths)} файлов на {self.success_recipients}")

        try:
            success = send_mail(
                file_paths=file_paths,
                subject=subject,
                body=body,
                recipients=self.success_recipients
            )
            return success
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке успешного письма: {str(e)}", exc_info=True)
            return False

    def _send_error_email(self, failed_reports, total_reports, error_details=None):
        """Отправка уведомления об ошибке (без файлов)"""
        subject = f"⚠️ Argus Media: Ошибка при скачивании отчетов - {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        failed_list = '\n'.join([f'• ID {rid}' for rid in failed_reports])

        body = f"""
Внимание!

При скачивании отчетов Argus Media возникли ошибки.

Статистика:
  Успешно: {total_reports - len(failed_reports)}/{total_reports}
  Ошибки: {len(failed_reports)}/{total_reports}

Отчеты с ошибками:
{failed_list}

{f"Детали ошибок:\n{error_details}" if error_details else ""}

Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

--
С уважением,
Автоматизированная система сбора данных
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
        Выполняет полный цикл: авторизация → скачивание → отправка

        Returns:
            dict: Результат выполнения {
                'all_success': bool,          # Все отчеты скачаны успешно
                'downloaded_files': list,     # Список путей к файлам
                'failed_reports': list,       # Список ID с ошибками
                'email_sent': bool            # Отправлено ли письмо
            }
        """
        logger.info(f"🚀 Запуск парсера Argus Media")
        logger.info(f"📊 Отчетов для скачивания: {len(self.report_ids)}")

        result = {
            'all_success': False,
            'downloaded_files': [],
            'failed_reports': [],
            'email_sent': False
        }
        with sync_playwright() as p:
            logger.info("🌐 Запуск браузера...")
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            try:
                # === Шаг 1: Авторизация ===
                home_url = self._login(page)

                # === Шаг 2: Скачивание отчетов ===
                total = len(self.report_ids)
                downloaded_files = []
                failed_reports = []

                for idx, report_id in enumerate(self.report_ids, 1):
                    logger.info(f"[{idx}/{total}] Обработка отчета ID: {report_id}")
                    filepath = self._download_report(page, report_id, home_url)

                    if filepath:
                        downloaded_files.append(filepath)
                    else:
                        failed_reports.append(report_id)

                result['downloaded_files'] = downloaded_files
                result['failed_reports'] = failed_reports
                result['all_success'] = len(failed_reports) == 0

                # === Шаг 3: Отправка письма в зависимости от результата ===
                logger.info("\n" + "=" * 60)
                logger.info(f"✅ Скачано {len(downloaded_files)}/{total} отчетов")
                if failed_reports:
                    logger.info(f"❌ Ошибки при скачивании: {', '.join(map(str, failed_reports))}")
                logger.info("=" * 60)

                if result['all_success'] and downloaded_files:
                    # Все успешно → отправляем файлы
                    logger.info("📧 Отправка отчетов на основной email...")
                    email_success = self._send_success_email(downloaded_files)
                    result['email_sent'] = email_success
                else:
                    # Есть ошибки → отправляем уведомление без файлов
                    logger.info("📧 Отправка уведомления об ошибке на администратора...")
                    email_success = self._send_error_email(
                        failed_reports=failed_reports,
                        total_reports=total
                    )
                    result['email_sent'] = email_success

                # Итоги
                logger.info("\n" + "=" * 60)
                logger.info("📊 ИТОГИ РАБОТЫ ARGUS PARSER")
                logger.info("=" * 60)
                logger.info(f"Скачано файлов: {len(downloaded_files)}/{total}")
                logger.info(f"Ошибки: {len(failed_reports)} отчетов")
                logger.info(f"Отправлено письмо: {'✅ Да' if result['email_sent'] else '❌ Нет'}")
                logger.info("=" * 60)

                return result

            finally:
                logger.info("🔚 Закрытие браузера...")
                browser.close()
                logger.info("✅ Браузер закрыт")

    @classmethod
    def get_name(cls):
        """Возвращает имя парсера для логирования и идентификации"""
        return "argus"


# Для тестирования напрямую
if __name__ == "__main__":
    load_dotenv()
    parser = ArgusProcess()
    result = parser.run()

    if result['all_success']:
        logger.info(f"\n🎉 Парсер завершил работу успешно!")
        logger.info(f"   Файлов скачано: {len(result['downloaded_files'])}")
        logger.info(f"   Письмо отправлено: {'✅' if result['email_sent'] else '⚠️'}")
    else:
        logger.error(f"\n❌ Парсер завершил работу с ошибками")
        logger.error(f"   Успешно: {len(result['downloaded_files'])}/{len(result['downloaded_files']) + len(result['failed_reports'])}")
        logger.error(f"   Ошибки: {result['failed_reports']}")
        logger.error(f"   Уведомление отправлено: {'✅' if result['email_sent'] else '❌'}")