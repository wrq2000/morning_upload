"""Подготовка плоской таблицы, перевод и расчёт стоимости. Python 3.8+."""
import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import pandas as pd

from common.errors import SourceError
from common.text import clean_text
from .config import Settings
from .translations import TEXT_FIELDS_RU, translate_text
from .sources import document_url, result_url

ROW_COLUMNS = (
    ("position_number", "Позиция", "Number", "int"),
    ("position_id", "ID позиции", "id", "id"),
    ("company", "Компания", "OwnerName", "text"),
    ("company_id", "ID компании", "idOwner", "id"),
    ("direction", "Вид операции", "DirectionName", "text"),
    ("direction_id", "ID вида операции", "idDirection", "id"),
    ("product", "Товар", "Goods", "text"),
    ("product_id", "ID товара", "idGoods", "id"),
    ("resource", "Марка / сорт / ресурс", "GoodsComment", "text"),
    ("unit", "Единица объёма", "Unit", "text"),
    ("delivery_point", "Точка поставки / базис", "Base_point", "text"),
    ("delivery_place", "Пункт поставки", "DeliveryPlace", "text"),
    ("delivery_terms", "Условия транспортировки", "Delivery_type", "text"),
    ("delivery_from", "Поставка с", "LoadDateFrom", "date"),
    ("delivery_to", "Поставка по", "LoadDateTo", "date"),
    ("sold_lots", "Продано лотов", "ResultAmount", "decimal"),
    ("sold_volume", "Проданный объём", "ResultFullSize", "decimal"),
    ("price_min", "Цена минимальная, валюта/единица", "MinPrice", "decimal"),
    ("price_avg", "Цена средневзвешенная, валюта/единица", "AvgPrice", "decimal"),
    ("price_max", "Цена максимальная, валюта/единица", "MaxPrice", "decimal"),
    ("vat", "НДС (как в источнике)", "WithVAT", "text"),
    ("agreements", "Количество сделок", "AgreeCount", "int"),
    ("offered_lots", "Выставлено лотов", "Amount", "decimal"),
    ("lot_volume", "Объём одного лота", "LotSize", "decimal"),
    ("offered_volume", "Выставленный объём", "FullSize", "decimal"),
    ("initial_price", "Начальная цена, валюта/единица", "Init_Price", "decimal"),
    ("collateral_price", "Цена для расчёта залога, валюта/единица", "Price", "decimal"),
    ("offered_position_cost", "Стоимость выставленной позиции, валюта (Cost)", "Cost", "decimal"),
    ("initial_lot_cost", "Начальная стоимость лота, валюта", "LotCost", "decimal"),
    ("price_step", "Шаг цены", "Step", "decimal"),
    ("position_state", "Состояние заявки", "StateName", "text"),
    ("position_state_id", "ID состояния заявки", "idState", "id"),
    ("trade_type_id", "ID вида торгов позиции", "idTrade_type", "id"),
    ("payment_name", "Вид оплаты", "PaymentName", "text"),
    ("payment_id", "ID вида оплаты", "Payment", "id"),
    ("payment_conditions", "Условия оплаты", "PaymentCondition", "text"),
    ("comments", "Дополнительные условия", "Comments", "text"),
)
# Состав и порядок CSV фиксированы. Служебные ID продолжают использоваться
# внутри сборщика и в состоянии, но не добавляются в выгрузку автоматически.
OUTPUT_COLUMNS = (
    ("auction_date", "Дата торгов"),
    ("auction_time", "Время начала торгов"),
    ("auction_number", "Номер торгов"),
    ("auction_id", "ID торгов"),
    ("company_role", "Роль компании"),
    ("currency", "Валюта цены и стоимости"),
    ("trade_section", "Секция"),
    ("trade_type", "Вид торгов"),
    ("auction_state_id", "ID состояния торгов"),
    ("position_number", "Позиция"),
    ("company", "Компания"),
    ("direction", "Вид операции"),
    ("product", "Товар"),
    ("unit", "Единица объёма"),
    ("delivery_point", "Точка поставки / базис"),
    ("delivery_place", "Пункт поставки"),
    ("delivery_terms", "Условия транспортировки"),
    ("delivery_from", "Поставка с"),
    ("delivery_to", "Поставка по"),
    ("sold_lots", "Продано лотов"),
    ("sold_volume", "Проданный объём"),
    ("price_min", "Цена минимальная, валюта/единица"),
    ("price_avg", "Цена средневзвешенная, валюта/единица"),
    ("actual_cost", "Фактическая стоимость"),
    ("price_max", "Цена максимальная, валюта/единица"),
    ("vat", "НДС (как в источнике)"),
    ("agreements", "Количество сделок"),
    ("offered_lots", "Выставлено лотов"),
    ("lot_volume", "Объём одного лота"),
    ("offered_volume", "Выставленный объём"),
    ("initial_price", "Начальная цена, валюта/единица"),
    ("collateral_price", "Цена для расчёта залога, валюта/единица"),
    ("offered_position_cost", "Стоимость выставленной позиции, валюта (Cost)"),
    ("initial_lot_cost", "Начальная стоимость лота, валюта"),
    ("price_step", "Шаг цены"),
    ("position_state", "Состояние заявки"),
    ("payment_name", "Вид оплаты"),
    ("payment_conditions", "Условия оплаты"),
    ("comments", "Дополнительные условия"),
    ("document_names", "Документы"),
    ("document_urls", "Ссылки на документы"),
    ("document_changed", "Изменения документов (как на сайте)"),
    ("result_url", "Страница результатов"),
    ("announcement_url", "Объявление"),
)



def number(value: Any) -> Optional[Decimal]:
    text = re.sub(r"\s+", "", str(value if value is not None else ""))
    if text.lower() in {"", "-", "—", "null", "none"}:
        return None
    if not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", text):
        raise SourceError("Неизвестный числовой формат: {!r}".format(value))
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation as exc:
        raise SourceError("Некорректное число: " + text) from exc



def convert(value: Any, kind: str) -> Any:
    if kind in {"decimal", "int"}:
        parsed = number(value)
        if kind == "int" and parsed is not None:
            if parsed != parsed.to_integral_value():
                raise SourceError("Вместо целого числа получено {!r}".format(value))
            return int(parsed)
        return parsed
    if kind == "date":
        return datetime.strptime(clean_text(value), "%d.%m.%Y").date().isoformat() if value else ""
    if isinstance(value, (dict, list)):
        raise SourceError("Вместо скалярного поля получен вложенный объект: {!r}".format(value))
    return clean_text(value)



def flatten(auction_id: str, payload: Dict[str, Any], settings, announcement_url: str = "") -> List[Dict[str, Any]]:
    info = payload["info"]
    required_info = {"Num", "TradeDate", "TradeTimeFrom", "Currency", "Trade_type", "Section_Type"}
    if not required_info.issubset(info):
        raise SourceError("В метаданных отсутствуют поля: " + str(required_info - set(info)))
    auction_number = clean_text(info["Num"]).upper()
    if not settings.number_pattern.fullmatch(auction_number):
        raise SourceError("Это не экспортная сессия GASEDP: " + auction_number)
    auction_date = convert(info["TradeDate"], "date")
    number_date = datetime.strptime(auction_number.split("-")[1], "%d%m%y").date().isoformat()
    if auction_date != number_date:
        raise SourceError("Дата торгов API не совпала с номером сессии: " + auction_number)
    rows = []
    seen = set()
    for source in payload["rows"]:
        required = {"id", "Number", "OwnerName", "idOwner", "idDirection", "Goods", "Unit",
                    "LoadDateFrom", "LoadDateTo", "ResultFullSize", "ResultAmount",
                    "MinPrice", "MaxPrice", "AvgPrice"}
        if not isinstance(source, dict) or not required.issubset(source):
            raise SourceError("В позиции отсутствуют обязательные поля: " + auction_id)
        position_id = clean_text(source["id"])
        if not position_id or position_id in seen:
            raise SourceError("Пустой или повторный ID позиции: " + position_id)
        seen.add(position_id)
        direction_id = str(source["idDirection"])
        direction = clean_text(source.get("DirectionName")).casefold()
        if direction_id == "2401" or direction == "продаж":
            role = "Продавец"
        elif direction in {"купівля", "покупка"}:
            role = "Покупатель"
        else:
            role = ""  # Не приписываем компании неизвестную роль.
        row = {
            "auction_date": auction_date,
            "auction_time": clean_text(info["TradeTimeFrom"]),
            "auction_number": auction_number, "auction_id": str(auction_id),
            "company_role": role, "currency": clean_text(info["Currency"]),
            "currency_id": clean_text(info.get("idCurrency")),
            "trade_section": clean_text(info["Section_Type"]),
            "trade_type": clean_text(info["Trade_type"]),
            "auction_state_id": clean_text(info.get("idState")),
        }
        for name, _, key, kind in ROW_COLUMNS:
            row[name] = convert(source.get(key), kind)
        if not row["company"]:
            raise SourceError("Отсутствует название компании: " + position_id)
        if (row["delivery_from"] and row["delivery_to"]
                and row["delivery_from"] > row["delivery_to"]):
            raise SourceError("Перепутаны даты поставки: " + position_id)
        if row["sold_volume"] is None or row["sold_lots"] is None:
            raise SourceError("Отсутствуют результаты по объёму: " + position_id)
        if row["sold_volume"] < 0 or row["sold_lots"] < 0:
            raise SourceError("Отрицательный результат торгов: " + position_id)
        prices = [row[key] for key in ("price_min", "price_avg", "price_max")]
        if row["sold_volume"] > 0 and (any(p is None for p in prices) or prices != sorted(prices)):
            raise SourceError("Некорректные цены результата: " + position_id)
        # Указанная пользователем методика: объём * опубликованная средняя цена.
        # Decimal сохраняет точность источника; пропущенная цена даёт пропуск.
        row["actual_cost"] = (row["sold_volume"] * row["price_avg"]
                              if row["price_avg"] is not None else None)
        files = source.get("files", [])
        if not isinstance(files, list) or not all(isinstance(f, dict) and "id" in f for f in files):
            raise SourceError("Изменилась структура документов: " + position_id)
        row.update({
            "document_names": " | ".join(clean_text(f.get("Num")) for f in files),
            "document_urls": " | ".join(document_url(settings, f["id"]) for f in files),
            "document_changed": " | ".join(clean_text(f.get("changed")) for f in files),
            "result_url": result_url(settings, auction_id), "announcement_url": announcement_url,
        })
        known = {column[2] for column in ROW_COLUMNS} | {"files"}
        # Новые скалярные поля сохраняются во внутренних данных; состав CSV
        # определяется только OUTPUT_COLUMNS и не расширяется автоматически.
        for key in sorted(set(source) - known):
            row["source_" + key] = convert(source[key], "text")
        for name in TEXT_FIELDS_RU:
            row[name] = translate_text(row[name], name)
        rows.append(row)
    return rows



def csv_content(rows: List[Dict[str, Any]], settings: Settings) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=settings.csv_delimiter, lineterminator="\n")
    # Английские имена совпадают с ключами подготовленного DataFrame.
    # Русские подписи в OUTPUT_COLUMNS остаются пояснениями для разработчика.
    writer.writerow([name for name, _ in OUTPUT_COLUMNS])
    for row in rows:
        values = []
        for name, _ in OUTPUT_COLUMNS:
            value = row.get(name)
            if isinstance(value, Decimal):
                value = format(value, "f").replace(".", settings.csv_decimal_separator)
            elif value is None:
                value = ""
            elif isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                value = "'" + value  # Текст источника не должен стать формулой в Excel.
            values.append(value)
        writer.writerow(values)
    return buffer.getvalue()



def prepare_positions(auction_id, frame, settings, announcement_url=""):
    """Обработать исходный DataFrame одного DataGateway, сохраняя Decimal."""
    payload = {"info": frame.attrs["auction_info"], "rows": frame.to_dict("records")}
    rows = flatten(auction_id, payload, settings, announcement_url)
    return pd.DataFrame(rows, dtype=object)
