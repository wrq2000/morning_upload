"""DataGateway → обработка отдельных источников → полная/частичная выгрузка. Python 3.8+."""
import asyncio
import json
import logging
from contextlib import nullcontext
from datetime import datetime, timedelta

import pandas as pd

from .business import BusinessData as B
from common.checkpoint import RunCheckpoint
from common.errors import SourceError
from common.http_client import HttpClient
from common.storage import load_state, run_lock, write_text_atomic
from common.text import clean_text
from .parsers import AuctionApiError
from .processing import OUTPUT_COLUMNS, convert, csv_content, prepare_positions
from .reporting import check_snapshot
from .sources import announcement_gateway, calendar_gateway, results_gateway

logger = logging.getLogger(__name__)


def discovery_start(settings, end):
    """Фиксированное окно; ошибки и длительность перерыва его не расширяют."""
    return max(settings.start_date, end - timedelta(days=settings.recheck_days - 1))


async def fetch_results(checkpoint, settings, http, auction_id):
    """HTTP-повторы принадлежат HttpClient; здесь повторяем только JSON Message."""
    for attempt in range(settings.api_error_attempts):
        try:
            # Новый DataGateway обязателен: предыдущий сохраняет состояние ошибки.
            return await checkpoint.fetch(results_gateway(settings, http, auction_id), "results", auction_id)
        except AuctionApiError:
            if attempt + 1 >= settings.api_error_attempts:
                raise
            logger.warning("API УЭБ, аукцион %s: внутренняя ошибка; повтор %s/%s через %.1f с",
                           auction_id, attempt + 2, settings.api_error_attempts, settings.api_error_backoff)
            await asyncio.sleep(settings.api_error_backoff)


async def run_pipeline(settings, client=None):
    """Ошибка даты, объявления или аукциона не отменяет доступные результаты."""
    end = settings.effective_end_date
    output = settings.output_dir
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / settings.csv_file
    state_path = output / settings.state_file
    with run_lock(output / settings.lock_file):
        state = load_state(state_path)
        previous_check = check_snapshot(state)
        start = discovery_start(settings, end)
        checkpoint = RunCheckpoint(
            output / settings.checkpoint_file,
            context={
                "collection_mode": "rolling-window-v2-isolated",
                "start": settings.start_date.isoformat(), "end": end.isoformat(),
                "scan_from": start.isoformat(), "calendar_url": settings.calendar_url,
                "public_base": settings.public_base, "results_page": settings.results_page,
                "api_handler": settings.api_handler, "results_command": settings.results_command,
                "number_pattern": settings.number_pattern.pattern,
                "base_state_success": state.get("last_success_msk", ""),
            }, enabled=settings.resume_enabled, max_age=settings.resume_max_age,
        )
        errors = []
        excluded = set()

        def issue(stage, key, exc, number="", cached=False):
            errors.append({"stage": stage, "key": str(key), "number": number,
                           "kind": type(exc).__name__, "message": str(exc), "cached": cached})
            logger.warning("УЭБ %s %s (%s): %s; продолжаем остальные источники", stage, key, number, exc)

        logger.info("Окно загрузки: %s — %s (%s дней). История до окна — из сохранённого состояния",
                    start, end, settings.recheck_days)
        context = nullcontext(client) if client is not None else HttpClient(settings.http)
        with context as http:
            day = start
            while day <= end:
                try:
                    gateway = calendar_gateway(settings, http, day)
                    frame = await checkpoint.fetch(gateway, "calendar", day.isoformat())
                    for row in frame.to_dict("records"):
                        state["announcements"][row["announcement_url"]] = {
                            "number": row["number"], "date": row["date"]}
                    logger.info("Календарь %s: экспортных сессий %s (%.2f с)", day, len(frame), gateway.load_time)
                except Exception as exc:
                    issue("calendar", day.isoformat(), exc)
                day += timedelta(days=1)

            results = {}
            pending = 0
            for url, metadata in sorted(state["announcements"].items(), key=lambda item: (item[1]["date"], item[0])):
                if not start.isoformat() <= metadata["date"] <= end.isoformat():
                    continue
                try:
                    frame = await checkpoint.fetch(
                        announcement_gateway(settings, http, url, metadata), "announcements", url)
                    if frame.empty:
                        pending += 1
                        logger.warning("Результаты ещё не опубликованы: %s (%s)", metadata["number"], url)
                    for row in frame.to_dict("records"):
                        auction_id = row.pop("auction_id")
                        if auction_id in results and results[auction_id]["number"] != row["number"]:
                            raise SourceError("Один ID аукциона указан для разных сессий: " + auction_id)
                        results[auction_id] = row
                except Exception as exc:
                    issue("announcement", url, exc, metadata["number"])

            snapshots = {}
            for auction_id, previous in state["auctions"].items():
                if settings.start_date.isoformat() <= previous["info"]["iso_date"] <= end.isoformat():
                    snapshots[auction_id] = previous
                    if previous["info"]["iso_date"] >= start.isoformat():
                        results.setdefault(auction_id, {"number": clean_text(previous["info"]["Num"]).upper(),
                                                        "announcement_url": previous.get("announcement_url", "")})
            prepared = {}
            for auction_id, metadata in sorted(results.items()):
                try:
                    raw = await fetch_results(checkpoint, settings, http, auction_id)
                    info = raw.attrs["auction_info"]
                    iso_date = convert(info["TradeDate"], "date")
                    if not start.isoformat() <= iso_date <= end.isoformat():
                        raise SourceError("Дата результатов за пределами окна загрузки: " + auction_id)
                    if metadata.get("number") and clean_text(info["Num"]).upper() != metadata["number"]:
                        raise SourceError("Номер торгов API не совпал с объявлением: " + auction_id)
                    # Проверяем преобразование до замены последнего исправного снимка.
                    prepared[auction_id] = prepare_positions(
                        auction_id, raw, settings, metadata.get("announcement_url", ""))
                    snapshots[auction_id] = {
                        "info": dict(info, iso_date=iso_date), "rows": raw.attrs["raw_rows"],
                        "announcement_url": metadata.get("announcement_url", ""),
                        "last_verified_msk": datetime.now(settings.timezone).isoformat(timespec="seconds"),
                    }
                    logger.info("%s: %s позиций с сайта", info["Num"], len(raw))
                except Exception as exc:
                    checkpoint.discard("results", auction_id)
                    excluded.add(auction_id)
                    issue("results", auction_id, exc, metadata.get("number", ""), auction_id in snapshots)

            frames = []
            for auction_id, snapshot in sorted(snapshots.items()):
                if auction_id in excluded:
                    # Старый снимок остаётся в state, но не выдаётся за свежие строки CSV.
                    continue
                try:
                    if auction_id in prepared:
                        frame = prepared[auction_id]
                    else:
                        raw = pd.DataFrame(snapshot["rows"], dtype=object)
                        raw = raw.where(raw.notna(), None)
                        raw.attrs["auction_info"] = snapshot["info"]
                        frame = prepare_positions(auction_id, raw, settings, snapshot.get("announcement_url", ""))
                    frames.append(frame)
                except Exception as exc:
                    excluded.add(auction_id)
                    issue("processing", auction_id, exc, clean_text(snapshot["info"].get("Num", "")), True)
            rows = [row for frame in frames for row in frame.to_dict("records")]
            rows.sort(key=lambda r: (r["auction_date"], r["auction_time"], r["auction_number"],
                                     r["position_number"], r["position_id"]))
            if len({(r["auction_id"], r["position_id"]) for r in rows}) != len(rows):
                raise SourceError("Повторный ключ ID торгов + ID позиции")

            complete = not errors
            checked_at = datetime.now(settings.timezone).isoformat(timespec="seconds")
            state.update({"start_date": settings.start_date.isoformat(), "last_window_from": start.isoformat(),
                          "last_attempt_through": end.isoformat(), "last_attempt_msk": checked_at,
                          "collection_complete": complete, "collection_errors": errors,
                          "excluded_auction_ids": sorted(excluded), "auctions": snapshots,
                          "row_count": len(rows), "pending_announcements": pending})
            if complete:
                state.update(last_discovery_through=end.isoformat(), last_success_msk=checked_at)
            state.pop("last_full_scan", None)
            target = csv_path if complete else csv_path.with_name(csv_path.stem + "_partial" + csv_path.suffix)
            if not complete and target in {state_path, output / settings.lock_file, output / settings.checkpoint_file,
                                           settings.http.cooldown_file, settings.log_file}:
                raise SourceError("Имя частичного CSV совпало со служебным файлом")
            csv_text = csv_content(rows, settings)
            state_text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
            # При неполном проходе основной CSV последнего полного сбора не меняется.
            write_text_atomic(target, csv_text, settings.csv_encoding)
            write_text_atomic(state_path, state_text)
            if complete:
                checkpoint.complete()
            result = pd.DataFrame(rows, dtype=object) if rows else pd.DataFrame(
                columns=[name for name, _ in OUTPUT_COLUMNS], dtype=object)
            result.attrs.update(source=B.Processed.RESULTS, comment="Результаты экспорта газа УЭБ",
                                check_snapshot=check_snapshot(state), previous_check_snapshot=previous_check,
                                collection_complete=complete, collection_errors=errors,
                                excluded_auction_ids=sorted(excluded), csv_path=str(target))
            logger.info("%s: %s позиций в CSV, %s сохранённых сессий; ошибок %s; исключены ID %s; "
                        "HTTP-запросов %s; из промежуточного файла %s. %s",
                        "Готово" if complete else "Частичный результат", len(rows), len(snapshots), len(errors),
                        ", ".join(sorted(excluded)) or "нет", http.request_count, checkpoint.hits, target)
            return result
