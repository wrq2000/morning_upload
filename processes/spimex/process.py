"""Текстовая выгрузка СПбМТСБ, история и последние N строк для письма."""
import csv
import io
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

import pandas as pd

from common.csv_process import CsvMailProcess
from common.data_gateway import DataGateway
from common.errors import SourceError
from common.process_config import env_int

logger = logging.getLogger(__name__)
SERVICE_FIELDS = ["date", "value", "source_value_3", "source_value_4"]
DEFAULT_URL = (
    "https://spimex.com/markets/oil_products/instruments/list/service.php"
    "?code=PCX1UGU036F&rand=960&pageId=1e7e0c0&token=___"
)


def source_date(value):
    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            pass
    return None


def decode_source(content, encoding):
    if encoding != "auto":
        return content.decode(encoding).lstrip("\ufeff")
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1251")


def parse_table(text, delimiter="auto", date_column=""):
    """Распознаёт CSV/TSV с датой, сохраняет исходные названия и значения полей."""
    text = text.lstrip("\ufeff").strip()
    if re.search(r"<(?:!doctype|html|head|body)\b", text, flags=re.I):
        raise SourceError("СПбМТСБ вернула HTML вместо текстовой таблицы")
    # service.php отдаёт записи без заголовка, в том числе одной длинной строкой.
    service_format = bool(re.match(r"\d{4}-\d{2}-\d{2}\s*;", text))
    if service_format:
        text = re.sub(r"\s+(?=\d{4}-\d{2}-\d{2}\s*;)", "\n", text)
    separators = (";", "\t", ",", "|") if delimiter == "auto" else (delimiter,)
    if any(len(separator) != 1 for separator in separators):
        raise ValueError("SPIMEX_SOURCE_DELIMITER должен быть auto или одним символом")
    candidates = []
    for separator in separators:
        try:
            rows = [[cell.strip() for cell in row] for row in csv.reader(
                io.StringIO(text), delimiter=separator, strict=True
            ) if any(cell.strip() for cell in row)]
        except csv.Error:
            continue
        for index, row in enumerate(rows[:50]):
            if len(row) < 2:
                continue
            date_indexes = [i for i, value in enumerate(row) if source_date(value)]
            if not date_indexes:
                continue
            header = rows[index - 1] if index and len(rows[index - 1]) == len(row) else []
            if not header or any(source_date(value) for value in header):
                if service_format and len(row) == 4 and date_indexes == [0]:
                    header = SERVICE_FIELDS.copy()
                else:
                    header = ["column_" + str(i + 1) for i in range(len(row))]
            if date_column:
                date_indexes = [i for i in date_indexes if header[i] == date_column]
            if len(date_indexes) != 1:
                continue
            candidates.append((rows, index, header, date_indexes[0], separator))
            break
    if len(candidates) != 1:
        raise SourceError(
            "Не удалось однозначно распознать текст СПбМТСБ. "
            "Проверьте файл, SPIMEX_SOURCE_ENCODING/DELIMITER/DATE_COLUMN"
        )
    rows, first, columns, date_index, separator = candidates[0]
    if service_format and columns != SERVICE_FIELDS:
        raise SourceError("СПбМТСБ: ожидались четыре поля в записи service.php")
    if not all(columns) or len(set(columns)) != len(columns):
        raise SourceError("СПбМТСБ: пустые или повторяющиеся заголовки")
    records = []
    for row in rows[first:]:
        if len(row) != len(columns) or not source_date(row[date_index]):
            raise SourceError("СПбМТСБ: некорректная строка таблицы")
        row[date_index] = source_date(row[date_index])
        if service_format:
            for value in row[1:]:
                if value == "null":
                    continue
                try:
                    valid = bool(value) and Decimal(value).is_finite()
                except InvalidOperation:
                    valid = False
                if not valid:
                    raise SourceError("СПбМТСБ: ожидалось числовое значение или null")
        records.append(row)
    frame = pd.DataFrame(records, columns=columns).drop_duplicates()
    date_name = columns[date_index]
    if frame[date_name].duplicated().any():
        raise SourceError("СПбМТСБ: несколько разных строк за одну дату")
    frame.attrs["date_column"] = date_name
    logger.info("СПбМТСБ: разделитель %r, колонка даты %s, строк %s",
                separator, date_name, len(frame))
    return frame


class SpimexProcess(CsvMailProcess):
    def __init__(self):
        super().__init__("SPIMEX", "СПбМТСБ", "spimex_latest.csv")
        self.url = os.getenv("SPIMEX_URL", DEFAULT_URL)
        self.last_rows = env_int("SPIMEX_LAST_ROWS", 30)
        self.encoding = os.getenv("SPIMEX_SOURCE_ENCODING", "auto")
        self.delimiter = os.getenv("SPIMEX_SOURCE_DELIMITER", "auto")
        if self.delimiter == "tab":
            self.delimiter = "\t"
        self.date_column = os.getenv("SPIMEX_DATE_COLUMN", "")
        self.code = parse_qs(urlsplit(self.url).query).get("code", ["instrument"])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.code):
            raise ValueError("Недопустимый code в SPIMEX_URL")
        self.attachment_name = "spimex_" + self.code + ".csv"

    def parse_response(self, response):
        return parse_table(
            decode_source(response.content, self.encoding), self.delimiter, self.date_column
        )

    async def collect(self, client, today):
        gateway = DataGateway.from_http(
            "spimex", self.url, client, self.parse_response,
            headers={"Referer": "https://spimex.com/markets/oil_products/instruments/list/"},
        )
        current = await gateway.fetch()
        date_column = current.attrs["date_column"]
        history_path = self.output_dir / ("spimex_" + self.code + "_history.csv")
        if history_path.exists():
            history = DataGateway.read_csv_sync(
                "spimex_history", history_path, sep=";", encoding="utf-8-sig",
                dtype=str, keep_default_na=False,
            )
            if (list(history.columns) == ["column_1", "column_2", "column_3", "column_4"]
                    and list(current.columns) == SERVICE_FIELDS):
                history.columns = SERVICE_FIELDS
            if list(history.columns) != list(current.columns):
                raise SourceError("СПбМТСБ: состав колонок изменился, проверьте накопленный CSV")
            if history[date_column].duplicated().any():
                raise SourceError("СПбМТСБ: повтор дат в накопленном CSV")
            if not history[date_column].map(source_date).eq(history[date_column]).all():
                raise SourceError("СПбМТСБ: некорректные даты в накопленном CSV")
            current = pd.concat([history, current], ignore_index=True).drop_duplicates(
                subset=[date_column], keep="last"
            )
        current = current.sort_values(date_column).reset_index(drop=True)
        DataGateway.write_csv_atomic(
            current, history_path, sep=";", encoding="utf-8-sig", index=False,
            lineterminator="\n",
        )
        history_path.chmod(0o644)
        latest = current.tail(self.last_rows).copy()
        body = (
            f"СПбМТСБ, инструмент {self.code}. Проверка {today}.\n"
            f"Во вложении последние {len(latest)} строк по дате торгов.\n"
            f"Период {latest[date_column].iloc[0]} — {latest[date_column].iloc[-1]}.\n"
            f"Накопленная история содержит {len(current)} строк.\n"
            "Значения полей сохранены, даты приведены к YYYY-MM-DD.\n"
        )
        return latest, body
