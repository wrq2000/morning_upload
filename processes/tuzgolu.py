import os
import re
import cv2
import logging
import tempfile
import pytesseract
import requests
import pandas as pd
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path

# ==============================
# ТОЧНАЯ СТРУКТУРА ТАБЛИЦ В ПИКСЕЛЯХ
# ==============================

TABLE_TEMPLATES = {
    "1": {
        "name": "Depolama",
        "x_bounds": [8, 132, 343, 567],
        "y_bounds": [73, 97, 121, 145],
        "headers": ["date", "inj", "wth"]
    },
    "2": {
        "name": "Inj/Wth Fact",
        "x_bounds": [8, 80, 214, 350, 567],
        "y_bounds": [81, 143],
        "headers": ["date", "inj", "wth", "total_wth"]
    },
    "3": {
        "name": "Inj/Wth Capacity",
        "x_bounds": [9, 98, 250, 403, 567],
        "y_bounds": [81, 113, 146],
        "headers": ["period", 'inj_cap', 'wth_cap', 'total_cap']
    }
}

# Импорт mailer (предполагается, что он есть)
try:
    from common.mailer import send_mail
except ImportError:
    def send_mail(**kwargs):
        raise RuntimeError("Mailer not available. Install common.mailer.")

logger = logging.getLogger(__name__)


class TuzGoluFixedTemplateParser:
    def __init__(
            self,
            urls=None,
            state_dir="./state",
            download_dir="./downloads",
            debug_roi=False,
            timeout=10
    ):
        # URLs to process
        self.urls = urls or [
            "https://tuzgoluebt.botas.gov.tr/ws/Data/1.png",
            "https://tuzgoluebt.botas.gov.tr/ws/Data/2.png",
            "https://tuzgoluebt.botas.gov.tr/ws/Data/3.png",
        ]
        self.state_dir = Path(state_dir)
        self.download_dir = Path(download_dir)
        self.debug_roi = debug_roi
        self.timeout = timeout

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # Email settings
        self.success_recipients = os.getenv("TUZGOLU_SUCCESS_RECIPIENTS") or os.getenv("RECIPIENTS")
        self.error_recipients = os.getenv("TUZGOLU_ERROR_RECIPIENTS") or os.getenv("ADMIN_RECIPIENTS")

        if not self.success_recipients:
            raise ValueError("Не указаны получатели (TUZGOLU_SUCCESS_RECIPIENTS или RECIPIENTS)")

        # Session
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def _download_image(self, url: str) -> bytes:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        if 'image' not in response.headers.get('content-type', ''):
            raise ValueError(f"URL не содержит изображение: {url}")
        return response.content

    def _save_temp_image(self, image_bytes: bytes, suffix=".png") -> str:
        """Сохраняет изображение во временный файл со случайным именем (для обратной совместимости)"""
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(image_bytes)
            return tmp.name

    def _save_image_with_name(self, image_bytes: bytes, filename: str, suffix=".png") -> str:
        """Сохраняет изображение с указанным именем в download_dir"""
        filepath = self.download_dir / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(image_bytes)
        return str(filepath)

    def _detect_type_from_url(self, url: str) -> str:
        path = urlparse(url).path
        for t in ["1", "2", "3"]:
            if f"/{t}.png" in path.lower():
                return t
        raise ValueError(f"Не удалось определить тип из URL: {url}")

    def _parse_with_precise_template(self, img, template, img_type="unknown"):
        x_bounds = template["x_bounds"]
        y_bounds = template["y_bounds"]
        headers = template["headers"]

        data = []
        num_rows = len(y_bounds) - 1
        num_cols = len(x_bounds) - 1

        for r in range(num_rows):
            row = []
            for c in range(num_cols):
                x1 = x_bounds[c]
                y1 = y_bounds[r]
                x2 = x_bounds[c + 1]
                y2 = y_bounds[r + 1]

                roi = img[y1:y2, x1:x2]
                if roi.size == 0:
                    text = ""
                else:
                    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

                    # Распознавание текста
                    text = pytesseract.image_to_string(
                        binary,
                        lang='eng',
                        config='--psm 6 --oem 3 -c tessedit_char_whitelist=0123456789.,-'
                    ).strip()

                    if self.debug_roi:
                        debug_dir = self.download_dir / "debug_rois" / f"type_{img_type}"
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        cv2.imwrite(str(debug_dir / f"roi_r{r}_c{c}.png"), binary)

                row.append(text)
            data.append(row)

        return pd.DataFrame(data, columns=headers)

    def _postprocess_dataframe(self, df: pd.DataFrame, img_type: str) -> pd.DataFrame:
        """
        Применяет бизнес-логику к данным:
        - 'date': сохраняется как дата (строка)
        - 'period': разбивается на period_min/period_max как целые числа (годы)
        - 'inj_cap', 'wth_cap': "X-Y Milyon" → разбивка на min/max с правилом лидирующего нуля → float
        - 'total_cap': число с разделителями тысяч → очищается в целое число
        - для типа 3: добавляется колонка fetch_date (сегодняшняя дата) как первая колонка
        - остальные колонки: стандартная числовая обработка с правилом лидирующего нуля
        """
        df = df.copy()

        # Добавляем fetch_date ДО обработки колонок для типа 3
        if img_type == "3":
            fetch_date = datetime.now().strftime('%Y-%m-%d')
            df.insert(0, 'fetch_date', fetch_date)  # Вставляем как первую колонку

        for col in df.columns:
            # Пропускаем уже добавленную колонку fetch_date
            if col == 'fetch_date':
                continue

            # ===== Обработка колонки 'date' =====
            if col == 'date':
                processed = []
                for val in df[col]:
                    if pd.isna(val) or not str(val).strip():
                        processed.append(None)
                        continue
                    text = str(val).strip()
                    cleaned = re.sub(r'[^\d.\-/]', '', text)
                    if not cleaned:
                        processed.append(None)
                        continue
                    # Пытаемся распознать как дату
                    for fmt in ['%d.%m.%Y', '%d/%m/%Y', '%Y-%m-%d', '%d.%m.%y', '%d/%m/%y']:
                        try:
                            dt = datetime.strptime(cleaned, fmt)
                            processed.append(dt.strftime('%Y-%m-%d'))
                            break
                        except (ValueError, TypeError):
                            continue
                    else:
                        processed.append(cleaned)
                df[col] = processed
                continue

            # ===== Обработка колонки 'period' (годы) =====
            if col == 'period':
                min_vals, max_vals = [], []
                for val in df[col]:
                    if pd.isna(val) or not str(val).strip():
                        min_vals.append(None)
                        max_vals.append(None)
                        continue
                    text = str(val).strip()
                    cleaned = re.sub(r'[^\d-]', '', text)
                    if '-' in cleaned and cleaned.count('-') == 1:
                        parts = cleaned.split('-')
                        try:
                            min_vals.append(int(parts[0]) if parts[0] else None)
                            max_vals.append(int(parts[1]) if parts[1] else None)
                        except (ValueError, IndexError):
                            min_vals.append(None)
                            max_vals.append(None)
                    else:
                        try:
                            min_vals.append(int(cleaned))
                            max_vals.append(None)
                        except ValueError:
                            min_vals.append(None)
                            max_vals.append(None)
                df.drop(col, axis=1, inplace=True)
                df[f"{col}_min"] = min_vals
                df[f"{col}_max"] = max_vals
                continue

            # ===== Обработка колонок ёмкостей с правилом лидирующего нуля =====
            if col in ['inj_cap', 'wth_cap']:
                min_vals, max_vals = [], []
                for val in df[col]:
                    if pd.isna(val) or not str(val).strip():
                        min_vals.append(None)
                        max_vals.append(None)
                        continue
                    text = str(val).strip()
                    # Извлекаем цифры, дефис и точки
                    cleaned = re.sub(r'[^\d.-]', '', text)

                    # Разбиваем по дефису
                    if '-' in cleaned and cleaned.count('-') == 1:
                        parts = cleaned.split('-')
                        part_min = parts[0].strip()
                        part_max = parts[1].strip()
                    else:
                        part_min = cleaned
                        part_max = None

                    # Обрабатываем каждую часть по правилу лидирующего нуля
                    def parse_with_leading_zero(s):
                        if not s or not s.strip():
                            return None
                        s = s.strip()

                        # Правило: "05" → 0.5
                        if re.match(r'^0\d+$', s):
                            return float(f"0.{s[1:]}")

                        # Стандартная очистка
                        num_clean = re.sub(r'[^\d.,]', '', s)
                        if not num_clean:
                            return None

                        num_clean = num_clean.replace(',', '.')

                        try:
                            return float(num_clean)
                        except ValueError:
                            return None

                    min_vals.append(parse_with_leading_zero(part_min))
                    max_vals.append(parse_with_leading_zero(part_max) if part_max else None)

                df.drop(col, axis=1, inplace=True)
                df[f"{col}_min"] = min_vals
                df[f"{col}_max"] = max_vals
                continue

            # ===== Обработка total_cap (убираем разделители тысяч) =====
            if col == 'total_cap':
                processed = []
                for val in df[col]:
                    if pd.isna(val) or not str(val).strip():
                        processed.append(None)
                        continue
                    text = str(val).strip()
                    cleaned = re.sub(r'[^\d]', '', text)
                    try:
                        processed.append(int(cleaned))
                    except ValueError:
                        processed.append(None)
                df[col] = processed
                continue

            # ===== Стандартная числовая обработка для остальных колонок =====
            processed = []
            for val in df[col]:
                if pd.isna(val) or not str(val).strip():
                    processed.append(None)
                    continue

                text = str(val).strip()

                # Правило лидирующего нуля: "05" → 0.5
                if re.match(r'^0\d+$', text):
                    cleaned = f"0.{text[1:]}"
                    try:
                        processed.append(float(cleaned))
                        continue
                    except ValueError:
                        pass

                # Стандартная очистка
                cleaned = re.sub(r'[^\d.,]', '', text)
                if not cleaned:
                    processed.append(None)
                    continue

                cleaned = cleaned.replace('.', '').replace(',', '.')
                try:
                    processed.append(float(cleaned))
                except ValueError:
                    processed.append(None)

            df[col] = processed

        return df

    def _send_success_email(self, csv_paths, image_paths):
        # Формируем список прикреплённых файлов для отображения в письме
        attachments_info = []
        for p in csv_paths:
            attachments_info.append(f"📊 {Path(p).name}")
        for p in image_paths:
            attachments_info.append(f"🖼️ {Path(p).name}")

        subject = f"Tuz Gölü Data — {datetime.now().strftime('%d.%m.%Y')}"
        body = f"""Добрый день!

Автоматически распознанные данные с сайта Tuz Gölü EBT.

Дата: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}

Прикреплённые файлы:
{chr(10).join(attachments_info)}

С уважением,
Автоматизированная система мониторинга
"""
        return send_mail(
            file_paths=csv_paths + image_paths,
            subject=subject,
            body=body,
            recipients=self.success_recipients
        )

    def _send_error_email(self, error_msg):
        subject = "❌ Ошибка при обработке Tuz Gölü EBT"
        body = f"""Произошла ошибка при обработке данных Tuz Gölü:

Ошибка: {error_msg}
Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}

Проверьте логи.
"""
        return send_mail(
            subject=subject,
            body=body,
            recipients=self.error_recipients
        )

    def run(self):
        """
        Основной метод. Возвращает dict с метриками.
        """
        logger.info("🚀 Запуск TuzGölü парсера")
        result = {
            'success': False,
            'processed': 0,
            'failed': 0,
            'email_sent': False,
            'details': []
        }

        all_csv_paths = []
        all_image_paths = []

        try:
            for url in self.urls:
                logger.info(f"📥 Обработка: {url}")
                try:
                    img_type = self._detect_type_from_url(url)
                    template = TABLE_TEMPLATES[img_type]

                    # === Генерируем единое базовое имя для CSV и изображения ===
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    base_filename = f"tuzgolu_{img_type}_{timestamp}"

                    # Скачиваем и сохраняем изображение с осмысленным именем
                    image_bytes = self._download_image(url)
                    image_path = self._save_image_with_name(image_bytes, f"{base_filename}.png")
                    all_image_paths.append(image_path)

                    img = cv2.imread(image_path)
                    if img is None:
                        raise ValueError("Не удалось загрузить изображение")

                    raw_df = self._parse_with_precise_template(img, template, img_type)
                    clean_df = self._postprocess_dataframe(raw_df, img_type)

                    # Сохраняем CSV с тем же базовым именем
                    csv_path = self.download_dir / f"{base_filename}.csv"
                    clean_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
                    all_csv_paths.append(csv_path)

                    result['processed'] += 1
                    result['details'].append({
                        'url': url,
                        'type': img_type,
                        'rows': len(clean_df),
                        'status': 'success'
                    })

                except Exception as e:
                    logger.error(f"❌ Ошибка при обработке {url}: {e}")
                    result['failed'] += 1
                    result['details'].append({
                        'url': url,
                        'error': str(e),
                        'status': 'failed'
                    })

            # Отправка email
            if result['processed'] > 0:
                email_success = self._send_success_email(all_csv_paths, all_image_paths)
                result['email_sent'] = email_success
                result['success'] = True
            else:
                # Только ошибки — отправляем уведомление
                if result['failed'] > 0:
                    error_summary = "; ".join([d['error'] for d in result['details'] if 'error' in d])
                    self._send_error_email(error_summary)
                result['success'] = False

            return result

        finally:
            # Удаляем временные файлы (изображения и CSV)
            for file_path in all_image_paths + all_csv_paths:
                try:
                    if os.path.exists(file_path):
                        os.unlink(file_path)
                        logger.debug(f"🗑️ Удалён: {file_path}")
                except Exception as e:
                    logger.warning(f"Не удалось удалить {file_path}: {e}")

    @classmethod
    def get_name(cls):
        return "tuzgolu"
