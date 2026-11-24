"""Разбор календаря, объявлений и JSON УЭБ. Python 3.8+. Обновлено 17.11.2026."""
import json
from datetime import datetime
from urllib.parse import unquote, urljoin, urlsplit

import pandas as pd

from common.errors import SourceError
from common.html_tools import parse_page


class AuctionApiError(SourceError):
    """API вернул собственную ошибку для конкретного аукциона (в том числе HTTP 200)."""


def parse_calendar(response, settings, day):
    page = parse_page(response.text, element_attrs={"data-slot": "day-auctions"},
                      fallbacks=({"element_class": "auctions-info"},))
    if "Торги за " + day.strftime("%d.%m.%Y") not in page.text:
        raise SourceError("Календарь вернул другую дату: " + day.isoformat())
    found = {}
    for link in page.links:
        number = settings.number_pattern.search(link["text"] + " " + unquote(link["href"]))
        if not number:
            continue
        value = number.group().upper()
        if datetime.strptime(value.split("-")[1], "%d%m%y").date() != day:
            raise SourceError("Дата экспортной сессии не совпала с календарём: " + value)
        url = urljoin(settings.calendar_url, link["href"])
        if urlsplit(url).hostname != urlsplit(settings.calendar_url).hostname:
            raise SourceError("Неожиданный адрес объявления: " + url)
        found[url] = {"announcement_url": url, "number": value, "date": day.isoformat()}
    return pd.DataFrame(list(found.values()), columns=["announcement_url", "number", "date"], dtype=object)


def parse_announcement(response, settings, url, metadata):
    # Импорт внутри функции исключает цикл регистрации источников и парсеров.
    from .sources import auction_id_from_url
    page = parse_page(response.text, element_attrs={"data-slot": "auction-detail"},
                      fallbacks=({"element_id": "main-content"},))
    if metadata["number"] not in page.text.upper():
        raise SourceError("В объявлении отсутствует ожидаемый номер сессии: " + url)
    found = {}
    for link in page.links:
        auction_id = auction_id_from_url(settings, urljoin(url, link["href"]))
        if auction_id:
            found[auction_id] = dict(metadata, auction_id=auction_id, announcement_url=url)
    return pd.DataFrame(list(found.values()), columns=["auction_id", "number", "date", "announcement_url"], dtype=object)


def parse_results(response, auction_id):
    try:
        payload = json.loads(response.content)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise SourceError("Получен не JSON для аукциона " + auction_id) from exc
    if not isinstance(payload, dict):
        raise SourceError("Ожидался объект JSON для аукциона " + auction_id)
    if "Message" in payload:
        raise AuctionApiError("Ошибка API {}: {}".format(auction_id, payload["Message"]))
    if not isinstance(payload.get("info"), dict) or not isinstance(payload.get("rows"), list):
        raise SourceError("Изменилась структура ответа API: " + auction_id)
    if not payload["info"].get("TradeDate") or not payload["info"].get("Num"):
        raise SourceError("API не вернул дату или номер торгов: " + auction_id)
    if not all(isinstance(row, dict) for row in payload["rows"]):
        raise SourceError("JSON позиций должен содержать объекты: " + auction_id)
    frame = (pd.DataFrame(payload["rows"], dtype=object) if payload["rows"]
             else pd.DataFrame(columns=["id", "OwnerName"], dtype=object))
    frame = frame.where(frame.notna(), None)
    # Данные таблицы используются в обработке. Оригиналы в attrs предназначены
    # для сохранения исходного состояния без замены отсутствующих ключей на null.
    frame.attrs["auction_info"] = payload["info"]
    frame.attrs["raw_rows"] = payload["rows"]
    return frame
