"""NRG_CB_GASM через официальный SDMX-CSV API Eurostat."""
import io
import logging
import os
from decimal import Decimal, InvalidOperation

import pandas as pd

from common.data_gateway import DataGateway
from common.errors import SourceError
from common.monthly_process import MonthlyCsvMailProcess
from common.process_config import env_int

logger = logging.getLogger(__name__)
DIMENSIONS = ("freq", "nrg_bal", "siec", "unit", "geo", "TIME_PERIOD")
REQUIRED_COLUMNS = DIMENSIONS + ("OBS_VALUE", "OBS_FLAG")
DEFAULT_URL = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/nrg_cb_gasm"


def period_start(today, months):
    month_index = today.year * 12 + today.month - 1 - months
    year, month = divmod(month_index, 12)
    return "{:04d}-{:02d}".format(year, month + 1)


def parse_balance(response, unit, start_period):
    """Сохраняет поля SDMX-CSV, включая пустые значения и флаги Eurostat."""
    try:
        frame = pd.read_csv(
            io.StringIO(response.content.decode("utf-8-sig")),
            dtype=str, keep_default_na=False,
        )
    except (UnicodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise SourceError("Eurostat: ответ не является корректным SDMX-CSV") from exc
    if not set(REQUIRED_COLUMNS).issubset(frame.columns):
        raise SourceError("Eurostat: в ответе отсутствуют обязательные колонки SDMX-CSV")
    if frame.empty:
        raise SourceError("Eurostat: получена пустая таблица")
    if not (frame["freq"].eq("M") & frame["siec"].eq("G3000") & frame["unit"].eq(unit)).all():
        raise SourceError("Eurostat: неверные частота, топливо или единица измерения")
    if not frame["TIME_PERIOD"].str.fullmatch(r"\d{4}-\d{2}").all():
        raise SourceError("Eurostat: ожидался период YYYY-MM")
    pd.to_datetime(frame["TIME_PERIOD"] + "-01", format="%Y-%m-%d", errors="raise")
    if frame["TIME_PERIOD"].lt(start_period).any():
        raise SourceError("Eurostat: ответ содержит периоды раньше заданного окна")

    # Страновые коды Eurostat двухсимвольные. ЕС и еврозона имеют отдельные коды.
    frame = frame.loc[frame["geo"].str.fullmatch(r"[A-Z]{2}")].copy()
    if frame.empty or not frame["OBS_VALUE"].ne("").any():
        raise SourceError("Eurostat: нет наблюдений по странам")
    for value in frame.loc[frame["OBS_VALUE"].ne(""), "OBS_VALUE"]:
        try:
            valid = Decimal(value).is_finite()
        except InvalidOperation:
            valid = False
        if not valid:
            raise SourceError("Eurostat: некорректное числовое наблюдение")
    if frame[list(DIMENSIONS)].eq("").any().any() or frame.duplicated(list(DIMENSIONS)).any():
        raise SourceError("Eurostat: пустые или повторяющиеся ключи наблюдений")
    return frame.sort_values(["geo", "TIME_PERIOD", "nrg_bal"]).reset_index(drop=True)


class EurostatGasProcess(MonthlyCsvMailProcess):
    def __init__(self):
        super().__init__("EUROSTAT", "Eurostat — газовый баланс", "eurostat_gas_balance.csv")
        self.attachment_name = self.filename
        self.url = os.getenv("EUROSTAT_URL", DEFAULT_URL).rstrip("/")
        self.unit = os.getenv("EUROSTAT_UNIT", "MIO_M3").strip()
        if self.unit not in ("MIO_M3", "TJ_GCV"):
            raise ValueError("EUROSTAT_UNIT должен быть MIO_M3 или TJ_GCV")
        self.lookback_months = env_int("EUROSTAT_LOOKBACK_MONTHS", 24)

    async def collect(self, client, today):
        start = period_start(today, self.lookback_months)
        gateway = DataGateway.from_http(
            "eurostat_gas_balance",
            self.url + "/M..G3000." + self.unit + ".",
            client, lambda response: parse_balance(response, self.unit, start),
            params={"format": "SDMX-CSV", "startPeriod": start},
            required_columns=REQUIRED_COLUMNS, unique_columns=DIMENSIONS,
        )
        frame = await gateway.fetch()
        latest = frame.loc[frame["OBS_VALUE"].ne("")].groupby("geo")["TIME_PERIOD"].max()
        latest = latest.reindex(sorted(frame["geo"].unique()), fill_value="нет числовых данных")
        coverage = "\n".join("{} — {}".format(country, month) for country, month in latest.items())
        units = "млн м³" if self.unit == "MIO_M3" else "ТДж по высшей теплоте сгорания"
        logger.info("Eurostat: строк %s, стран %s, период %s — %s",
                    len(frame), frame["geo"].nunique(),
                    frame["TIME_PERIOD"].min(), frame["TIME_PERIOD"].max())
        body = (
            f"Eurostat, месячный газовый баланс NRG_CB_GASM. Проверка {today}.\n"
            f"Единица измерения — {units}.\n"
            f"Период {frame['TIME_PERIOD'].min()} — {frame['TIME_PERIOD'].max()}.\n"
            f"Стран и территорий {frame['geo'].nunique()}, строк {len(frame)}.\n"
            "Во вложении все доступные статьи баланса за выбранное окно.\n"
            "Пустые значения и исходные флаги Eurostat сохранены.\n\n"
            "Последний месяц с числовыми данными по каждой стране\n" + coverage + "\n\n"
            "Источник https://ec.europa.eu/eurostat/databrowser/view/nrg_cb_gasm/default/table\n"
        )
        return frame, body
