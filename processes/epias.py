"""
Модуль для работы с данными портала EPİAŞ (Турция)
Объединяет все источники в один Excel-файл с разными листами
"""
import logging
import os
import tempfile
import time
from pathlib import Path
from io import StringIO
import requests
from datetime import datetime as dt, timedelta
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field
import pandas as pd
import numpy as np
import re
from common.logfile import setup_logging

logger = logging.getLogger(__name__)


@dataclass
class EpiasData:
    """Обработчик данных EPİAŞ с поддержкой турецкой локали"""

    rename_columns: List[str] = field(default_factory=list)
    delete_columns: List[str] = field(default_factory=list)
    agg_func: Dict[str, Any] = field(default_factory=dict)
    numeric_columns: Union[List[str], str] = 'all_except_date'
    datetime_column: str = 'date'
    dayfirst: bool = True
    add_columns: Dict[str, Any] = field(default_factory=dict)

    def process(self, df: pd.DataFrame) -> pd.DataFrame:
        """Полный цикл обработки данных"""
        df = df.copy()

        # 1. Очистка имён колонок
        df.columns = df.columns.str.strip()

        # 2. Переименование колонок
        if self.rename_columns:
            if len(self.rename_columns) != len(df.columns):
                raise ValueError(
                    f"Несоответствие количества колонок: ожидается {len(df.columns)}, "
                    f"получено {len(self.rename_columns)}"
                )
            df.columns = self.rename_columns

        # 3. Валидация колонки даты
        if self.datetime_column not in df.columns:
            raise ValueError(
                f"Колонка даты '{self.datetime_column}' отсутствует. "
                f"Доступные колонки: {list(df.columns)}"
            )

        # 4. Парсинг даты
        df[self.datetime_column] = pd.to_datetime(
            df[self.datetime_column],
            errors='coerce',
            dayfirst=self.dayfirst
        ).dt.tz_localize(None)

        # 5. Парсинг турецких чисел
        cols_to_parse = self._get_numeric_columns(df)
        if cols_to_parse:
            df = self._parse_turkish_numbers(df, cols_to_parse)

        # 6. Агрегация по дням
        if self.agg_func:
            df = self._apply_aggregation(df)

        # 7. Добавление статических колонок (после агрегации!)
        if self.add_columns:
            for col_name, col_value in self.add_columns.items():
                df[col_name] = col_value
                logger.debug(f"Добавлена колонка '{col_name}' со значением '{col_value}'")

        # 8. Удаление ненужных колонок
        cols_to_drop = [c for c in self.delete_columns if c in df.columns]
        if cols_to_drop:
            df = df.drop(columns=cols_to_drop)

        return df

    def _get_numeric_columns(self, df: pd.DataFrame) -> List[str]:
        """Определяет колонки для числового парсинга"""
        if self.numeric_columns == 'all_except_date':
            candidates = [col for col in df.columns if col != self.datetime_column]
            return [col for col in candidates if not pd.api.types.is_numeric_dtype(df[col])]
        elif isinstance(self.numeric_columns, list):
            return [col for col in self.numeric_columns if col in df.columns]
        return []

    def _parse_turkish_numbers(self, df: pd.DataFrame, columns: List[str]) -> pd.DataFrame:
        """Парсит турецкие числа в указанных колонках"""
        for col in columns:
            if col in df.columns:
                df[col] = df[col].apply(self._parse_turkish_number)
        return df

    @staticmethod
    def _parse_turkish_number(val: Any) -> float:
        """
        Парсит число из турецкой локали с правильной обработкой всех форматов:
        - 1.234,56 → 1234.56 (точка = разделитель тысяч, запятая = десятичный разделитель)
        - 1234,56  → 1234.56 (только запятая = десятичный разделитель)
        - 1.234    → 1234    (только точка = разделитель тысяч)
        - 1234     → 1234    (целое число)
        - 1,5      → 1.5     (дробное без тысяч)

        Важно: В турецкой локали запятая ВСЕГДА десятичный разделитель.
        Точка без запятой — всегда разделитель тысяч.
        """
        if pd.isna(val) or val is None or val == '':
            return np.nan

        # Если уже число — возвращаем как есть
        if isinstance(val, (int, float)):
            return float(val)

        # Приводим к строке и очищаем от всего кроме цифр, точки, запятой и минуса
        val_str = str(val).strip()
        val_str = re.sub(r'[^\d.,-]', '', val_str)

        # Турецкая локаль: запятая = десятичный разделитель, точка = разделитель тысяч
        if ',' in val_str:
            # Есть запятая → это десятичный разделитель
            if '.' in val_str:
                # Оба разделителя: удаляем точки (тысячи), заменяем запятую на точку
                val_str = val_str.replace('.', '').replace(',', '.')
            else:
                # Только запятая: заменяем на точку
                val_str = val_str.replace(',', '.')
        else:
            # Нет запятой → все точки являются разделителями тысяч (не десятичным разделителем!)
            val_str = val_str.replace('.', '')

        try:
            return float(val_str)
        except ValueError:
            logger.warning(f"Не удалось распарсить число '{val}' → '{val_str}'")
            return np.nan

    def _apply_aggregation(self, df: pd.DataFrame) -> pd.DataFrame:
        """Агрегация данных по дням с поддержкой 'all'"""
        agg_spec = self.agg_func.copy()

        if 'all' in agg_spec:
            func = agg_spec.pop('all')
            numeric_cols = [
                col for col in df.select_dtypes(include=['number']).columns
                if col != self.datetime_column
            ]
            for col in numeric_cols:
                agg_spec[col] = func

        return (df.groupby(pd.Grouper(key=self.datetime_column, freq='D'))
                  .agg(agg_spec)
                  .reset_index())


class EpiasProcess:
    """Оркестратор получения, обработки и отправки данных EPİAŞ"""

    # Максимальная длина имени листа в Excel (лимит pandas/openpyxl)
    _MAX_SHEET_NAME_LEN = 31

    def __init__(
        self,
        processing_config: Dict[str, Any],
        url_dict: Dict[str, str],
        login_url: str = 'https://giris.epias.com.tr/cas/v1/tickets'
    ):
        """
        Инициализация процессора EPİAŞ.

        Получатели писем автоматически загружаются из переменной окружения RECIPIENTS.
        """
        self.processing_config = processing_config
        self.url_dict = url_dict
        self.login_url = login_url.rstrip()
        self.temp_dir = tempfile.mkdtemp()
        self.tgt = None

        # Загрузка получателей из .env (инкапсулировано внутри класса)
        recipients_raw = os.getenv("RECIPIENTS", "")
        self.recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

        if not self.recipients:
            logger.warning(
                "⚠️  Переменная окружения RECIPIENTS не задана или пуста. "
                "Отправка email будет пропущена."
            )

        # Инициализация отправщика почты
        from common.mailer import MailSender
        self.mailer = MailSender()
        logger.debug("MailSender успешно инициализирован")

        # Валидация конфигурации
        invalid_sources = set(processing_config.keys()) - set(url_dict.keys())
        if invalid_sources:
            raise ValueError(
                f"Обработчики настроены для несуществующих источников: {invalid_sources}. "
                f"Доступные источники: {list(url_dict.keys())}"
            )

    @staticmethod
    def _sanitize_sheet_name(name: str) -> str:
        """
        Очищает имя источника для использования в качестве имени листа Excel.
        - Заменяет недопустимые символы на '_'
        - Обрезает до 31 символа (лимит Excel)
        """
        # Заменяем недопустимые символы: \ / ? * [ ]
        sanitized = re.sub(r'[\\/?*\[\]]', '_', name)
        # Обрезаем до лимита Excel
        return sanitized[:EpiasProcess._MAX_SHEET_NAME_LEN]

    def login(self) -> str:
        """Авторизация в системе EPİAŞ"""
        logger.info("Начинаю авторизацию в EPİAŞ...")

        username = os.getenv("EPIAS_LOGIN")
        password = os.getenv("EPIAS_PASSWORD")

        if not username or not password:
            raise ValueError("Необходимо указать EPIAS_LOGIN и EPIAS_PASSWORD в .env файле")

        data = {"username": username, "password": password}
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/plain"}

        try:
            response = requests.post(self.login_url, headers=headers, data=data, timeout=30)
            response.raise_for_status()
            self.tgt = response.text.strip()
            logger.info(f"✅ Тикет успешно получен (первые 50 символов): {self.tgt[:50]}...")
            return self.tgt
        except Exception as e:
            logger.error(f"❌ Ошибка авторизации в EPİAŞ: {e}", exc_info=True)
            raise

    def fetch_data(self, url: str, name: str) -> pd.DataFrame:
        """Получение сырых данных из источника"""
        if self.tgt is None:
            self.login()

        headers = {
            "Content-Type": "application/json",
            "Accept-Language": "EN",
            "Accept": "application/json",
            "TGT": self.tgt
        }

        end_date = dt.today().strftime('%Y-%m-%d')
        start_date = (dt.today() - timedelta(days=30)).strftime('%Y-%m-%d')

        call_body = {
            'startDate': f'{start_date}T00:00:00+03:00',
            'endDate': f'{end_date}T00:00:00+03:00',
            'exportType': 'CSV'
        }

        try:
            response = requests.post(url.rstrip(), headers=headers, json=call_body, timeout=60)
            response.raise_for_status()

            if name == 'tr_gen_epias':
                parsed = response.json()
                items = parsed.get('items', [])
                df = pd.DataFrame(items)
            else:
                df = pd.read_csv(StringIO(response.text), delimiter=';', skipinitialspace=True)
                df.columns = df.columns.str.strip()

            logger.info(f"✅ Успешно получено {len(df)} строк для источника '{name}'")
            return df

        except Exception as e:
            logger.error(f"❌ Ошибка получения данных для '{name}': {e}", exc_info=True)
            raise

    def _save_combined_excel(self, dataframes: Dict[str, pd.DataFrame], filename: str) -> Optional[Path]:
        """
        Сохраняет несколько датафреймов в один Excel-файл на разных листах.
        Имена листов создаются на основе ключей словаря (с санитизацией).
        """
        if not dataframes:
            logger.warning("⚠️  Нет данных для сохранения в Excel")
            return None

        filepath = Path(self.temp_dir) / filename

        try:
            with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
                for source_name, df in dataframes.items():
                    if df.empty:
                        logger.warning(f"⚠️  Пропускаю пустой датафрейм для листа '{source_name}'")
                        continue

                    sheet_name = self._sanitize_sheet_name(source_name)
                    df.to_excel(writer, sheet_name=sheet_name, index=False)
                    logger.debug(f"✅ Сохранён лист '{sheet_name}' ({len(df)} строк)")

            logger.info(f"✅ Excel-файл создан: {filepath}")
            return filepath

        except Exception as e:
            logger.error(f"❌ Ошибка сохранения Excel-файла: {e}", exc_info=True)
            return None

    def _prepare_email_body(self, processed_sources: List[str], excel_filename: str) -> str:
        """Подготавливает тело письма"""
        body = f"""
Добрый день!

Во вложении — объединённый Excel-файл с данными с портала EPİAŞ за период (последние 30 дней).

📁 Файл: {excel_filename}

📊 Источники данных (отдельные листы):
"""
        for i, source in enumerate(processed_sources, 1):
            sheet_name = self._sanitize_sheet_name(source)
            body += f"{i}. {sheet_name} ← {source}\n"

        body += f"\n🕐 Дата формирования: {dt.now().strftime('%Y-%m-%d %H:%M:%S')}"
        body += "\n\n--\nС уважением,\nСистема автоматического сбора данных EPİAŞ"

        return body

    def _send_email(self, file_path: Path, processed_sources: List[str], excel_filename: str) -> bool:
        """Отправляет email с вложением (один Excel-файл)"""
        # Проверка наличия получателей
        if not self.recipients:
            logger.info("ℹ️  Отправка email пропущена: нет получателей (RECIPIENTS не задан)")
            return True  # Не считаем ошибкой отсутствие получателей

        if not self.mailer:
            logger.warning("⚠️  Отправка email пропущена: MailSender не инициализирован")
            return False

        if not file_path or not file_path.exists():
            logger.warning("⚠️  Файл для отправки не найден")
            return False

        try:
            subject = f"EPİAŞ данные за {dt.now().strftime('%Y-%m-%d')}"
            body = self._prepare_email_body(processed_sources, excel_filename)

            self.mailer.send_mail(
                recipients=self.recipients,
                subject=subject,
                body=body,
                file_paths=[str(file_path)]
            )

            logger.info(f"✅ Письмо успешно отправлено на: {', '.join(self.recipients)}")
            logger.info(f"✅ Вложен файл: {excel_filename}")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка отправки email: {e}", exc_info=True)
            return False

    def _cleanup_temp_files(self):
        """Удаляет все временные файлы из директории"""
        try:
            files_deleted = 0
            for ext in ["*.csv", "*.xlsx"]:
                for file in Path(self.temp_dir).glob(ext):
                    file.unlink()
                    files_deleted += 1
                    logger.debug(f"Временный файл удалён: {file}")

            if files_deleted > 0:
                logger.info(f"✅ Удалено временных файлов: {files_deleted}")

            Path(self.temp_dir).rmdir()
            logger.info(f"✅ Временная директория очищена: {self.temp_dir}")
        except Exception as e:
            logger.warning(f"⚠️  Ошибка при очистке временных файлов: {e}")

    def run(self) -> Dict[str, Any]:
        """
        Основной метод запуска процесса получения и отправки данных.

        Возвращает структурированный результат:
        {
            'success': bool,           # Общий успех (обработка + отправка)
            'sources_processed': int,  # Количество успешно обработанных источников
            'sources_failed': int,     # Количество неудачных источников
            'sources': List[str],      # Список успешно обработанных источников
            'errors': List[str],       # Список ошибок
            'duration_sec': float,     # Время выполнения в секундах
            'excel_file': str          # Имя созданного Excel-файла (если есть)
        }
        """
        start_time = time.time()
        logger.info("=" * 70)
        logger.info("🚀 Запуск процесса получения и отправки данных EPİAŞ")
        logger.info("=" * 70)

        processed_dataframes: Dict[str, pd.DataFrame] = {}
        processed_sources: List[str] = []
        errors: List[str] = []
        excel_file_path: Optional[Path] = None
        excel_filename: Optional[str] = None

        try:
            self.login()

            # Обработка каждого источника
            for name, url in self.url_dict.items():
                logger.info(f"\n{'-' * 70}")
                logger.info(f"Обработка источника: {name}")
                logger.info(f"{'-' * 70}")

                try:
                    # Получение сырых данных
                    raw_df = self.fetch_data(url, name)
                    logger.info(f"📥 Получено {len(raw_df)} строк (сырые данные)")

                    # Проверка наличия обработчика
                    if name not in self.processing_config:
                        err_msg = f"Нет обработчика для источника '{name}'"
                        logger.warning(f"⚠️  {err_msg}")
                        errors.append(err_msg)
                        continue

                    # Применение обработчика
                    processor = self.processing_config[name]
                    processed_df = processor.process(raw_df)
                    logger.info(f"✅ Обработано {len(processed_df)} строк после применения обработчика")

                    # Сохраняем датафрейм в словарь для последующего объединения
                    if not processed_df.empty:
                        processed_dataframes[name] = processed_df
                        processed_sources.append(name)
                        logger.info(f"✅ Источник '{name}' успешно обработан")
                    else:
                        logger.warning(f"⚠️  Датафрейм для '{name}' пуст после обработки")

                except Exception as e:
                    err_msg = f"Ошибка обработки источника '{name}': {str(e)}"
                    logger.error(f"❌ {err_msg}", exc_info=True)
                    errors.append(err_msg)
                    continue

            # Создание объединённого Excel-файла
            if processed_dataframes:
                timestamp = dt.now().strftime('%Y%m%d_%H%M')
                excel_filename = f"epias_combined_{timestamp}.xlsx"
                excel_file_path = self._save_combined_excel(processed_dataframes, excel_filename)

                if excel_file_path:
                    logger.info(f"✅ Создан объединённый Excel-файл: {excel_filename}")
                else:
                    errors.append("Не удалось создать Excel-файл")
            else:
                logger.warning("⚠️  Нет данных для создания Excel-файла")

            # Отправка email
            email_sent = False
            if excel_file_path and processed_sources:
                email_sent = self._send_email(excel_file_path, processed_sources, excel_filename or "")
                if email_sent:
                    logger.info("✅ Данные EPİAŞ успешно отправлены по почте")
                else:
                    if self.recipients:  # Ошибка отправки, а не отсутствие получателей
                        logger.warning("⚠️  Данные обработаны, но отправка email не удалась")
                        errors.append("Отправка email не удалась")
            else:
                if not self.recipients:
                    logger.info("ℹ️  Отправка email пропущена (нет получателей в RECIPIENTS)")
                elif processed_sources:
                    logger.info("ℹ️  Данные обработаны, но отправка не требуется (нет получателей)")
                else:
                    logger.warning("⚠️  Не удалось обработать ни один источник")
                    if not errors:
                        errors.append("Нет обработанных источников")

            # Формирование результата
            duration = time.time() - start_time
            # Успех = есть обработанные источники И (отправлено ИЛИ нет получателей)
            success = len(processed_sources) > 0 and (email_sent or not self.recipients)

            result = {
                'success': success,
                'sources_processed': len(processed_sources),
                'sources_failed': len(self.url_dict) - len(processed_sources),
                'sources': processed_sources,
                'errors': errors,
                'duration_sec': round(duration, 2),
                'excel_file': excel_filename
            }

            # Логирование итогов
            logger.info("\n" + "=" * 70)
            logger.info("🏁 ИТОГИ ОБРАБОТКИ EPİAŞ")
            logger.info("=" * 70)
            logger.info(f"Успешно обработано источников: {result['sources_processed']}")
            logger.info(f"Ошибок обработки: {result['sources_failed']}")
            logger.info(f"Общее время выполнения: {result['duration_sec']} сек")
            if excel_filename:
                logger.info(f"Создан Excel-файл: {excel_filename}")

            if errors:
                logger.info("\nОшибки:")
                for err in errors:
                    logger.info(f"  ⚠️  {err}")

            logger.info("=" * 70)

            return result

        except Exception as e:
            duration = time.time() - start_time
            err_msg = f"Критическая ошибка выполнения EPİAŞ: {str(e)}"
            logger.error(f"❌ {err_msg}", exc_info=True)
            return {
                'success': False,
                'sources_processed': 0,
                'sources_failed': len(self.url_dict),
                'sources': [],
                'errors': [err_msg],
                'duration_sec': round(duration, 2),
                'excel_file': None
            }

        finally:
            # Обязательная очистка временных файлов
            self._cleanup_temp_files()


# === КОНФИГУРАЦИЯ ОБРАБОТЧИКОВ ===
processing_config = {
    'turk_ugs': EpiasData(
        rename_columns=['date', 'inj', 'wth'],
        numeric_columns='all_except_date',
        dayfirst=True,
        add_columns={'ugs': 'Silivri'}
    ),
    'entry_amount': EpiasData(
        rename_columns=['date', 'consumption'],
        numeric_columns=['consumption'],
        dayfirst=True
    ),
    'tr_gen_epias': EpiasData(
        delete_columns=['hour'],
        agg_func={'all': 'mean'},
        dayfirst=False
    )
}

# === URL ИСТОЧНИКОВ ===
url_dict = {
    'turk_ugs': 'https://seffaflik.epias.com.tr/natural-gas-service/v1/transmission/export/daily-actualization-amount',
    'entry_amount': 'https://seffaflik.epias.com.tr/natural-gas-service/v1/transmission/export/realization-entry-amount',
    'tr_gen_epias': 'https://seffaflik.epias.com.tr/electricity-service/v1/generation/data/realtime-generation'
}