"""Среднесуточная температура, согласованный формат для загрузки в БД."""
import math
import os
from datetime import date, datetime, time, timedelta
from functools import partial

import pandas as pd

from common.csv_process import CsvMailProcess
from common.auto_http import AutoHttpClient
from common.data_gateway import DataGateway
from common.errors import SourceError, HttpSourceError
from common.process_config import env_bool, env_float, env_int

FIELDS = ("region_name", "parent_name", "date", "fcst_date", "temperature", "value_type")
VARIABLE = "temperature_2m_mean"


def read_daily(response, start, end, today, settings, forecast):
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("error"):
        raise SourceError("Open-Meteo: некорректный ответ API")
    if payload.get("daily_units", {}).get(VARIABLE) != "°C":
        raise SourceError("Open-Meteo: ожидалась температура в °C")
    if payload.get("utc_offset_seconds") != settings["utc_offset"]:
        raise SourceError("Open-Meteo: неверный часовой пояс ответа")
    daily = payload.get("daily", {})
    dates, values = daily.get("time"), daily.get(VARIABLE)
    if not isinstance(dates, list) or not isinstance(values, list) or len(dates) != len(values):
        raise SourceError("Open-Meteo: некорректные массивы суточных данных")
    found = {}
    for day_text, value in zip(dates, values):
        day = date.fromisoformat(day_text)
        if not start <= day <= end:
            continue
        if day in found:
            raise SourceError("Open-Meteo: повтор даты " + day_text)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SourceError("Open-Meteo: отсутствует температура за " + day_text)
        found[day] = value
    expected = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    missing = [day.isoformat() for day in expected if day not in found]
    if missing:
        raise SourceError("Open-Meteo: отсутствуют сутки " + ", ".join(missing))
    return pd.DataFrame([{
        "region_name": settings["region_name"], "parent_name": settings["parent_name"],
        "date": today.isoformat() if forecast else day.isoformat(),
        "fcst_date": day.isoformat() if forecast else None, "temperature": found[day],
        "value_type": "Прогноз" if forecast else "Факт",
    } for day in expected], columns=FIELDS)


def validate_json_access(response):
    """Страница блокировки с HTTP 200 должна переключать маршрут до DataGateway-парсера."""
    try:
        response.json()
    except ValueError as exc:
        raise HttpSourceError("Open-Meteo: вместо JSON получен другой ответ", status_code=200) from exc


class TskhinvalProcess(CsvMailProcess):
    def __init__(self):
        super().__init__("TSKHINVAL", "Температура Цхинвала", "temperature_tskhinval.csv")
        self.past_days = env_int("TSKHINVAL_PAST_DAYS", 15)
        self.days_ahead = env_int("TSKHINVAL_FORECAST_DAYS_AHEAD", 7, maximum=7)
        self.include_today = env_bool("TSKHINVAL_INCLUDE_TODAY", True)
        self.region_name = os.getenv("TSKHINVAL_REGION_NAME", "Цхинвал")
        self.parent_name = os.getenv("TSKHINVAL_PARENT_NAME", "Южная Осетия")
        self.latitude = env_float("TSKHINVAL_LATITUDE", 42.22764)
        self.longitude = env_float("TSKHINVAL_LONGITUDE", 43.96861)
        self.elevation = env_float("TSKHINVAL_ELEVATION_METRES", 874)
        if not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180:
            raise ValueError("Недопустимые координаты Цхинвала")
        self.model = os.getenv("TSKHINVAL_MODEL", "ecmwf_ifs")
        self.api_key = os.getenv("TSKHINVAL_API_KEY", "").strip()
        self.archive_url = os.getenv(
            "TSKHINVAL_ARCHIVE_URL", "https://archive-api.open-meteo.com/v1/archive"
        )
        self.forecast_url = os.getenv(
            "TSKHINVAL_FORECAST_URL", "https://api.open-meteo.com/v1/forecast"
        )
        if self.api_key:
            self.archive_url = self.archive_url.replace(
                "https://archive-api.open-meteo.com/", "https://customer-archive-api.open-meteo.com/", 1
            )
            self.forecast_url = self.forecast_url.replace(
                "https://api.open-meteo.com/", "https://customer-api.open-meteo.com/", 1
            )

    def http_client(self):
        return AutoHttpClient(self.http_settings(), self.prefix,
                              response_validator=validate_json_access)

    async def collect(self, client, today):
        settings = {
            "region_name": self.region_name, "parent_name": self.parent_name,
            "utc_offset": int(datetime.combine(today, time(), tzinfo=self.timezone).utcoffset().total_seconds()),
        }
        params = {
            "latitude": self.latitude, "longitude": self.longitude, "elevation": self.elevation,
            "daily": VARIABLE, "models": self.model, "temperature_unit": "celsius",
            "timezone": str(self.timezone), "timeformat": "iso8601", "cell_selection": "land",
        }
        if self.api_key:
            params["apikey"] = self.api_key
        fact_start, fact_end = today - timedelta(days=self.past_days), today - timedelta(days=1)
        forecast_start = today if self.include_today else today + timedelta(days=1)
        forecast_end = today + timedelta(days=self.days_ahead)
        archive = DataGateway.from_http(
            "tskhinval_fact", self.archive_url, client,
            partial(read_daily, start=fact_start, end=fact_end, today=today,
                    settings=settings, forecast=False),
            params=dict(params, start_date=fact_start.isoformat(), end_date=fact_end.isoformat()),
            required_columns=FIELDS,
        )
        forecast = DataGateway.from_http(
            "tskhinval_forecast", self.forecast_url, client,
            partial(read_daily, start=forecast_start, end=forecast_end, today=today,
                    settings=settings, forecast=True),
            params=dict(params, forecast_days=self.days_ahead + 1), required_columns=FIELDS,
        )
        facts = await archive.fetch()
        predictions = await forecast.fetch()
        frame = pd.concat([facts, predictions], ignore_index=True)
        body = (
            f"Среднесуточная температура воздуха на высоте 2 м, °C. {self.region_name}.\n"
            f"Факт {fact_start} — {fact_end}, {len(facts)} суток.\n"
            f"Прогноз {forecast_start} — {forecast_end}, {len(predictions)} суток.\n"
            f"Дата выгрузки {today}, часовой пояс {self.timezone}.\n"
            "Факт — архивные расчётные значения ECMWF IFS за завершённые сутки.\n"
            "Данные Open-Meteo / ECMWF, CC BY 4.0. https://open-meteo.com/\n"
        )
        return frame, body
