"""Регистрация HTTP-источников DataGateway. Python 3.8+."""
from functools import partial
from urllib.parse import parse_qs, urlencode, urlsplit

from .business import BusinessData as B
from common.data_gateway import DataGateway
from .parsers import parse_calendar, parse_announcement, parse_results


def result_url(settings, auction_id):
    return settings.public_base + settings.results_page + "?" + urlencode({
        "id_auc": auction_id, "view_type": "results", "lan": "ua"})


def auction_id_from_url(settings, url):
    parsed = urlsplit(url)
    target = urlsplit(settings.public_base + settings.results_page)
    if parsed.hostname != target.hostname or parsed.path.lower() != target.path.lower():
        return None
    params = parse_qs(parsed.query)
    if params.get("view_type") != ["results"]:
        return None
    value = params.get("id_auc", [""])[0]
    return value if value.isdigit() else None


def calendar_gateway(settings, client, day):
    return DataGateway.from_http(
        name=B.Raw.CALENDAR + "." + day.isoformat(), url=settings.calendar_url,
        http_client=client, response_parser=partial(parse_calendar, settings=settings, day=day),
        params={"y": day.year, "m": day.month, "d": day.day},
        headers={"Referer": settings.calendar_url}, timeout=settings.gateway_timeout,
        required_columns=("announcement_url", "number", "date"),
        unique_columns=("announcement_url",), allow_empty=True,
    )


def announcement_gateway(settings, client, url, metadata):
    return DataGateway.from_http(
        name=B.Raw.ANNOUNCEMENT + "." + metadata["number"], url=url,
        http_client=client,
        response_parser=partial(parse_announcement, settings=settings, url=url, metadata=metadata),
        headers={"Referer": settings.calendar_url}, timeout=settings.gateway_timeout,
        required_columns=("auction_id", "number", "date", "announcement_url"),
        unique_columns=("auction_id",), allow_empty=True,
    )


def results_gateway(settings, client, auction_id):
    # fl_* передаются даже пустыми: без них сервер возвращает ошибку вместо JSON.
    command_parameter = "name" if settings.api_handler.casefold() == "handler/action" else "CommandName"
    params = {command_parameter: settings.results_command, "id_auc": auction_id,
              "info": "Y", "lan": "ua", "fl_FullNumber": "", "fl_Direction": "",
              "fl_Owner": "", "fl_Goods": "", "fl_Payment": "", "empty_filter": "Y"}
    return DataGateway.from_http(
        name=B.Raw.POSITIONS + "." + auction_id, url=settings.public_base + settings.api_handler,
        http_client=client, response_parser=partial(parse_results, auction_id=auction_id),
        params=params, headers={"Referer": result_url(settings, auction_id)},
        timeout=settings.gateway_timeout, required_columns=("id", "OwnerName"),
        unique_columns=("id",), allow_empty=True,
    )


def document_url(settings, document_id):
    """Адрес документа из текущего шаблона PositionList.aspx."""
    if settings.api_handler.casefold() == "handler/action":
        return settings.public_base + "Download?" + urlencode({"id": document_id})
    return settings.public_base + settings.api_handler + "?" + urlencode({
        "CommandName": "Download", "id": document_id})
