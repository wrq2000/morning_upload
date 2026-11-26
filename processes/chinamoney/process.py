"""CFETS: центральный паритет USD/CNY; суточная выгрузка без браузера."""
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial

import pandas as pd
from common.csv_process import CsvMailProcess
from common.auto_http import AutoHttpClient
from common.data_gateway import DataGateway
from common.errors import SourceError, HttpSourceError
from common.process_config import env_int

logger = logging.getLogger(__name__)
BASE = "https://www.chinamoney.com.cn"
LATEST = BASE + "/r/cms/www/chinamoney/data/fx/ccpr.json"
HISTORY = BASE + "/ags/ms/cm-u-bk-ccpr/CcprHisNew"
PAIR = "USD/CNY"
FIELDS = ("date", "currency_pair", "value", "unit", "rate_type", "source_url")
HEADERS = {"Accept": "application/json", "Referer": BASE + "/english/bmkcpr/"}


def payload_from(response):
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError("ChinaMoney: ожидался JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise SourceError("ChinaMoney: неизвестная структура JSON")
    head = payload.get("head", {})
    if not isinstance(head, dict) or str(head.get("rep_code", "200")) != "200":
        raise SourceError("ChinaMoney: ошибка источника")
    if not isinstance(payload.get("records"), list):
        raise SourceError("ChinaMoney: отсутствует records")
    return payload


def rate(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise SourceError("ChinaMoney: некорректный курс") from exc
    if not result.is_finite() or result <= 0:
        raise SourceError("ChinaMoney: курс должен быть положительным конечным числом")
    return result


def row(day, value, url):
    return dict(date=day.isoformat(), currency_pair=PAIR, value=rate(value),
                unit="CNY/USD", rate_type="CENTRAL_PARITY", source_url=url)


def read_latest(response, start, end):
    payload = payload_from(response)
    try:
        day = date.fromisoformat(payload["data"]["lastDate"].split()[0])
        records = [r for r in payload["records"] if r.get("vrtEName") == PAIR]
        if len(records) != 1 or not start <= day <= end:
            raise ValueError("дата вне окна или отсутствует USD/CNY")
        return pd.DataFrame([row(day, records[0]["price"], LATEST)], columns=FIELDS)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SourceError("ChinaMoney: некорректная текущая публикация") from exc


def read_history(response, start, end):
    payload = payload_from(response)
    data = payload["data"]
    columns = data.get("searchlist") or data.get("head")
    if not isinstance(columns, list) or columns.count(PAIR) != 1:
        raise SourceError("ChinaMoney: не найден однозначный столбец USD/CNY")
    index = columns.index(PAIR)
    rows, seen = [], set()
    for record in payload["records"]:
        try:
            day = date.fromisoformat(record["date"])
            values = record["values"]
            if not isinstance(values, list) or len(values) != len(columns):
                raise ValueError("неверное число столбцов")
            if not start <= day <= end:
                raise ValueError("дата вне запрошенного периода")
            if day in seen:
                raise ValueError("повтор даты")
            seen.add(day)
            rows.append(row(day, values[index], HISTORY))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SourceError("ChinaMoney: некорректная историческая строка") from exc
    if not rows:
        raise SourceError("ChinaMoney: история пуста")
    return pd.DataFrame(rows, columns=FIELDS)


def validate_json_access(response):
    """В AUTO переключаем маршрут при HTML вместо JSON."""
    try:
        response.json()
    except ValueError as exc:
        raise HttpSourceError("ChinaMoney: вместо JSON получен другой ответ", status_code=200) from exc


class ChinaMoneyProcess(CsvMailProcess):
    def __init__(self):
        super().__init__("CHINAMONEY", "ChinaMoney — курс USD/CNY", "chinamoney_usdcny.csv")
        self.lookback = env_int("CHINAMONEY_LOOKBACK_DAYS", 10, maximum=30)

    def http_client(self):
        return AutoHttpClient(self.http_settings(), self.prefix,
                              response_validator=validate_json_access,
                              default_mode="proxy")

    async def collect(self, client, today):
        start = today - timedelta(days=self.lookback - 1)
        latest = DataGateway.from_http("chinamoney_latest", LATEST, client,
            partial(read_latest, start=start, end=today), headers=HEADERS,
            required_columns=FIELDS, unique_columns=("date",))
        history = DataGateway.from_http("chinamoney_history", HISTORY, client,
            partial(read_history, start=start, end=today), headers=HEADERS,
            params={"startDate": start.isoformat(), "endDate": today.isoformat(),
                    "currency": PAIR, "pageNum": 1, "pageSize": 30},
            required_columns=FIELDS, unique_columns=("date",))
        frames, warnings = [], []
        for gateway in (history, latest):
            try:
                frames.append(await gateway.fetch())
            except Exception as exc:
                warnings.append(str(exc))
                logger.warning("ChinaMoney: %s: %s", gateway.name, exc)
        if not frames:
            raise SourceError("ChinaMoney: ни один интерфейс не вернул корректных данных: " + "; ".join(warnings))
        frame = pd.concat(frames, ignore_index=True)
        # Текущая публикация приоритетна при совпадении даты.
        frame = frame.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
        last = frame.iloc[-1]
        if last["date"] != today.isoformat():
            logger.info("ChinaMoney: последняя публикация %s; за %s не подтверждена", last["date"], today)
        body = (f"Официальный центральный паритет CFETS, CNY за 1 USD.\n"
                f"Запрошено: {start} — {today}; получено {len(frame)} торговых дат.\n"
                f"Последняя публикация: {last['date']}, {last['value']}.\n"
                "Нет искусственных значений за выходные и праздники.\n"
                "https://www.chinamoney.org.cn/english/bmkcpr/\n")
        if warnings:
            body += "Предупреждения (частичная выгрузка):\n" + "\n".join(warnings) + "\n"
        return frame, body
