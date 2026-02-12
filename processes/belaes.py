"""
belaes_collector.py
Модуль для сбора данных о производственных показателях Белорусской АЭС
"""
import os
import requests
import re
import csv
import io
import tempfile
from datetime import datetime
from typing import Dict, Optional
from common.mailer import send_mail
import logging
logger = logging.getLogger(__name__)

class BelaesProcess:
    """
    Класс для сбора и отправки данных о производственных показателях БелАЭС
    """

    def __init__(self):
        """Инициализация коллектора"""
        self.url = "https://belaes.by/ru/proizvodstvennye-pokazateli.html"

        # Получатели для успешной отправки
        self.success_recipients = (
            os.getenv("BELAES_SUCCESS_RECIPIENTS") or
            os.getenv("RECIPIENTS")
        )

        # Получатели для ошибок
        self.error_recipients = (
            os.getenv("BELAES_ERROR_RECIPIENTS") or
            os.getenv("ADMIN_RECIPIENTS") or
            self.success_recipients
        )

        logger.info(f"✅ Инициализирован сборщик данных БелАЭС")
        logger.info(f"📧 Получатели успеха: {self.success_recipients}")
        logger.info(f"📧 Получатели ошибок: {self.error_recipients}")

    def parse_production(self, html: str) -> Optional[Dict]:
        """
        Парсит производственные показатели Белорусской АЭС

        Извлекает данные о выработке электроэнергии двух энергоблоков

        Args:
            html: HTML-код страницы

        Returns:
            Словарь с данными или None при ошибке
        """
        try:
            # Извлекаем дату отчёта
            date_pattern = r'По состоянию на (\d{2}\.\d{2}\.\d{4})'
            date_match = re.search(date_pattern, html)
            report_date = date_match.group(1) if date_match else None

            # Извлекаем значения выработки из таблицы
            # Формат: "37073,28 млн кВт*ч" и "19472,83 млн кВт*ч"
            value_pattern = r'(\d+[\.,]\d+)\s*млн\s*кВт\*ч'
            values = re.findall(value_pattern, html)

            if len(values) < 2:
                logger.warning("Не найдены данные о выработке на странице")
                return None

            # Извлекаем значения для каждого блока
            block1_value = values[0].replace(',', '.')
            block2_value = values[1].replace(',', '.')

            result = {
                'report_date': report_date,
                'block1_mwh': float(block1_value),
                'block2_mwh': float(block2_value),
                'parsed_at': datetime.now()
            }

            logger.info(
                f"Успешно извлечены данные БелАЭС: "
                f"Дата={report_date}, "
                f"Блок1={block1_value} млн кВт*ч, "
                f"Блок2={block2_value} млн кВт*ч"
            )
            return result

        except Exception as e:
            logger.error(f"Ошибка при парсинге данных БелАЭС: {e}", exc_info=True)
            return None

    def format_as_csv(self, data: Dict) -> str:
        """
        Форматирует данные в CSV формат

        Структура файла:
        - date: Дата отчёта
        - fetch_date: Дата сбора
        - unit_1: Энергоблок №1 (млн кВт*ч)
        - unit_2: Энергоблок №2 (млн кВт*ч)

        Args:
            data: Словарь с данными

        Returns:
            CSV-строка
        """
        output = io.StringIO()

        # Используем специальный writer с правильными настройками
        writer = csv.writer(
            output,
            delimiter=';',  # Точка с запятой как разделитель
            quoting=csv.QUOTE_NONE,  # Без кавычек
            lineterminator='\n'  # Используем только \n для переносов
        )

        # Заголовки на английском
        writer.writerow([
            'date',
            'fetch_date',
            'unit_1',
            'unit_2'
        ])

        # Данные
        writer.writerow([
            data.get('report_date', ''),
            data['parsed_at'].strftime('%d.%m.%Y %H:%M:%S'),
            f"{data.get('block1_mwh', 0):.2f}",
            f"{data.get('block2_mwh', 0):.2f}"
        ])

        return output.getvalue()

    def fetch_html(self) -> Optional[str]:
        """
        Получает HTML-код страницы с данными БелАЭС

        Returns:
            HTML-код страницы или None при ошибке
        """
        try:
            logger.info(f"Загрузка страницы: {self.url}")

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                             '(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'
            }

            response = requests.get(self.url, headers=headers, timeout=30)
            response.raise_for_status()

            # Устанавливаем кодировку (сайт использует utf-8)
            response.encoding = 'utf-8'
            html = response.text

            logger.info(f"✅ Страница успешно загружена, размер: {len(html)} байт")
            return html

        except requests.RequestException as e:
            logger.error(f"❌ Ошибка при загрузке страницы: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Неожиданная ошибка при загрузке страницы: {e}", exc_info=True)
            return None

    def save_csv_to_tempfile(self, csv_content: str) -> Optional[str]:
        """
        Сохраняет CSV контент во временный файл

        Args:
            csv_content: CSV строка

        Returns:
            Путь к временному файлу или None при ошибке
        """
        try:
            temp_dir = tempfile.gettempdir()
            filename = f"belaes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            temp_path = os.path.join(temp_dir, filename)

            # Сохраняем с BOM для корректного открытия в Excel
            with open(temp_path, 'w', encoding='utf-8-sig', newline='') as f:
                f.write(csv_content)

            logger.info(f"📁 Временный файл сохранен: {temp_path}")
            return temp_path

        except Exception as e:
            logger.error(f"❌ Ошибка при сохранении CSV файла: {e}", exc_info=True)
            return None

    def send_success_email(self, data: Dict, csv_path: str) -> bool:
        """
        Отправляет письмо с успешным результатом

        Args:
            data: Словарь с данными
            csv_path: Путь к CSV файлу

        Returns:
            Успешность отправки
        """
        try:
            subject = f"✅ БелАЭС - Выработка на {data.get('report_date', 'N/A')}"
            body = f"""
Данные о выработке электроэнергии Белорусской АЭС успешно собраны.

Дата отчёта: {data.get('report_date', 'N/A')}
Дата сбора: {data['parsed_at'].strftime('%d.%m.%Y %H:%M')}

Энергоблок №1: {data['block1_mwh']:,.2f} млн кВт*ч
Энергоблок №2: {data['block2_mwh']:,.2f} млн кВт*ч

Источник: {self.url}
            """

            success = send_mail(
                file_paths=[csv_path],
                subject=subject,
                body=body,
                recipients=self.success_recipients
            )

            if success:
                logger.info(f"✅ Письмо успешно отправлено на {self.success_recipients}")
            else:
                logger.error(f"❌ Ошибка при отправке письма на {self.success_recipients}")

            return success

        except Exception as e:
            logger.error(f"❌ Ошибка при отправке успешного письма: {e}", exc_info=True)
            return False

    def send_error_email(self, error_message: str) -> bool:
        """
        Отправляет письмо с уведомлением об ошибке

        Args:
            error_message: Сообщение об ошибке

        Returns:
            Успешность отправки
        """
        try:
            subject = "❌ Ошибка при сборе данных БелАЭС"
            body = f"""
Произошла ошибка при сборе данных о выработке электроэнергии Белорусской АЭС.

Дата и время ошибки: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}
Ошибка: {error_message}

Источник: {self.url}

Пожалуйста, проверьте работу системы.
            """

            success = send_mail(
                subject=subject,
                body=body,
                recipients=self.error_recipients
            )

            if success:
                logger.info(f"✅ Уведомление об ошибке отправлено на {self.error_recipients}")
            else:
                logger.error(f"❌ Ошибка при отправке уведомления об ошибке на {self.error_recipients}")

            return success

        except Exception as e:
            logger.error(f"❌ Ошибка при отправке уведомления об ошибке: {e}", exc_info=True)
            return False

    def run(self) -> Dict:
        """
        Основной метод запуска процесса сбора данных БелАЭС

        Выполняет:
        1. Загрузку страницы
        2. Парсинг данных
        3. Форматирование в CSV
        4. Отправку на соответствующих получателей

        Returns:
            Словарь с результатами выполнения (совместимый с другими процессами)
        """
        logger.info("=" * 60)
        logger.info("🚀 Запуск сбора данных БелАЭС")
        logger.info("=" * 60)

        start_time = datetime.now()
        result = {
            'process': 'belaes',
            'success': False,
            'all_success': False,
            'errors': [],
            'duration_sec': 0.0
        }

        try:
            # Шаг 1: Загрузка HTML
            html = self.fetch_html()
            if not html:
                error_msg = "Не удалось загрузить страницу с данными БелАЭС"
                result['errors'].append(error_msg)
                self.send_error_email(error_msg)
                return result

            # Шаг 2: Парсинг данных
            data = self.parse_production(html)
            if not data:
                error_msg = "Не удалось распарсить данные с страницы БелАЭС"
                result['errors'].append(error_msg)
                self.send_error_email(error_msg)
                return result

            # Шаг 3: Форматирование в CSV
            csv_content = self.format_as_csv(data)

            # Шаг 4: Сохранение во временный файл
            csv_path = self.save_csv_to_tempfile(csv_content)
            if not csv_path:
                error_msg = "Не удалось сохранить данные в CSV файл"
                result['errors'].append(error_msg)
                self.send_error_email(error_msg)
                return result

            try:
                # Шаг 5: Отправка успешного письма
                email_success = self.send_success_email(data, csv_path)

                # Формируем результат
                result['success'] = True
                result['all_success'] = email_success
                result['statistics'] = {
                    'report_date': data.get('report_date', ''),
                    'block1_mwh': data['block1_mwh'],
                    'block2_mwh': data['block2_mwh']
                }

                if email_success:
                    logger.info("=" * 60)
                    logger.info("✅ Сбор данных БелАЭС завершен успешно!")
                    logger.info("=" * 60)
                else:
                    logger.warning("=" * 60)
                    logger.warning("⚠️ Сбор данных выполнен, но отправка email не удалась")
                    logger.warning("=" * 60)

                return result

            finally:
                # Очистка временного файла
                if os.path.exists(csv_path):
                    os.remove(csv_path)
                    logger.info(f"🗑️ Временный файл удален: {csv_path}")

        except Exception as e:
            error_msg = f"Неожиданная ошибка при сборе данных: {str(e)}"
            logger.error(f"❌ {error_msg}", exc_info=True)
            result['errors'].append(error_msg)
            self.send_error_email(error_msg)
            return result
        finally:
            # Записываем длительность выполнения
            result['duration_sec'] = round((datetime.now() - start_time).total_seconds(), 2)


# ==============================================================================
# Точка входа для тестового запуска
# ==============================================================================

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    # Создаем экземпляр и запускаем сбор
    collector = BelaesProcess()
    result = collector.run()

    logger.info(f"\n{'=' * 60}")
    logger.info(f"Результат: {'✅ УСПЕШНО' if result['success'] else '❌ ОШИБКА'}")
    logger.info(f"Длительность: {result['duration_sec']:.2f} сек")
    if result['errors']:
        logger.info(f"Ошибки: {result['errors']}")
    logger.info(f"{'=' * 60}\n")