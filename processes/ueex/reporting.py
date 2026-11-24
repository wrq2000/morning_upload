"""Снимок торгов и описание изменений между успешными отчётами УЭБ."""
import hashlib
import json
from datetime import datetime

from .processing import number


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def check_snapshot(state):
    """Считать сессии по номеру торгов, в том числе с нулевыми/пустыми результатами."""
    checked_at = state.get("last_attempt_msk") or state.get("last_success_msk")
    if not checked_at:
        return None
    through = state.get("last_attempt_through") or state.get("last_discovery_through")
    sessions = {}
    contents = {}
    for auction_id, auction in sorted(state["auctions"].items()):
        info = auction["info"]
        name = str(info["Num"]).strip().upper()
        session = sessions.setdefault(name, {"date": info["iso_date"], "has_sales": False})
        session["has_sales"] = session["has_sales"] or any(
            (number(row.get("ResultFullSize")) or 0) > 0 for row in auction["rows"])
        contents.setdefault(name, []).append({"id": auction_id, "info": info, "rows": auction["rows"]})
    for name, payload in contents.items():
        sessions[name]["result_hash"] = _digest(payload)
    pending = {}
    window_from = state.get("last_window_from", state["start_date"])
    for item in state["announcements"].values():
        name = item["number"].upper()
        if (window_from <= item["date"] <= through
                and name not in sessions):
            pending[name] = {"date": item["date"]}
    return {"checked_at": checked_at, "start_date": state["start_date"],
            "window_from": window_from,
            "end_date": through, "sessions": sessions, "pending": pending,
            "complete": state.get("collection_complete", True),
            "errors": state.get("collection_errors", []),
            "excluded_auction_ids": state.get("excluded_auction_ids", [])}


def snapshot_signature(snapshot):
    # Полный отчёт сохраняет прежний fingerprint; повторный запуск не дублирует письмо.
    payload = {"start_date": snapshot["start_date"], "end_date": snapshot["end_date"],
               "window_from": snapshot.get("window_from", snapshot["start_date"]),
               "sessions": snapshot["sessions"], "pending": snapshot["pending"]}
    if not snapshot.get("complete", True):
        payload.update(complete=False, excluded_auction_ids=snapshot.get("excluded_auction_ids", []),
                       errors=[{key: item.get(key) for key in ("stage", "key", "kind", "cached")}
                               for item in snapshot.get("errors", [])])
    return _digest(payload)


def _stamp(value):
    return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M:%S %z")


def _date(value):
    return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")


def changes_text(current, previous):
    """Не выводить отсутствие торгов из отсутствия проданного объёма."""
    if not current.get("complete", True):
        lines = ["СБОР УЭБ ЗАВЕРШЁН ЧАСТИЧНО.",
                 "Попытка проверки: {}.".format(_stamp(current["checked_at"])),
                 "Полнота календаря и результатов не подтверждена. Нельзя подтвердить отсутствие новых торгов.",
                 "Во вложении доступные результаты и история вне обновляемого окна.",
                 "Сбойные аукционы исключены из этой выгрузки; они не считаются торгами без продаж."]
        if previous is not None:
            lines.append("Последний полный отправленный отчёт: {}.".format(_stamp(previous["checked_at"])))
        excluded = current.get("excluded_auction_ids", [])
        if excluded:
            lines.append("Исключённые ID торгов: {}.".format(", ".join(excluded)))
        for item in current.get("errors", []):
            lines.append("- {stage}: {key} {number}: {message}".format(number=item.get("number", ""),
                         stage=item["stage"], key=item["key"], message=item["message"]))
            if item.get("cached"):
                lines.append("  Предыдущий исправный снимок сохранён в состоянии; его строки не выданы за свежие.")
        return "\n".join(lines) + "\n"
    sessions = current["sessions"]
    window_from = current.get("window_from", current["start_date"])
    window_sessions = {name for name, value in sessions.items()
                       if window_from <= value["date"] <= current["end_date"]}
    lines = ["Проверка: {}.".format(_stamp(current["checked_at"])),
             "Проверенное окно: {} — {}.".format(_date(window_from), _date(current["end_date"]))]
    if previous is None or previous.get("start_date") != current["start_date"]:
        new = window_sessions
        changed = set()
        lines += ["Первый отчёт для этого периода.",
                  "Сессий с опубликованными результатами в окне: {}.".format(len(new))]
    else:
        lines.append("Предыдущая успешная проверка с отправленным отчётом: {}.".format(
            _stamp(previous["checked_at"])))
        new = window_sessions - set(previous["sessions"])
        recent_pending = {name for name, value in current["pending"].items()
                          if name not in previous["pending"] or value["date"] >= previous["checked_at"][:10]}
        changed = {name for name in window_sessions & set(previous["sessions"])
                   if sessions[name]["result_hash"] != previous["sessions"][name]["result_hash"]}
        if new:
            lines.append("С момента предыдущей проверки обнаружены новые торговые сессии "
                         "с опубликованными результатами: {}.".format(len(new)))
        elif recent_pending:
            lines.append("Новых опубликованных результатов нет; в календаре есть сессии "
                         "без опубликованных результатов. Отсутствие торгов не подтверждено.")
        else:
            lines.append("В проверенном окне новых торгов не обнаружено "
                         "(по опубликованным данным УЭБ).")
    if new:
        without_sales = sum(not sessions[name]["has_sales"] for name in new)
        lines.append("Из этих сессий с продажами: {}; без продаж: {}. "
                     "Сессии без продаж также учитываются как торги.".format(len(new) - without_sales, without_sales))
        days = sorted({sessions[name]["date"] for name in new})
        lines.append("Дней с этими торгами: {}. Даты: {}.".format(len(days), ", ".join(_date(day) for day in days)))
        for name in sorted(new, key=lambda item: (sessions[item]["date"], item)):
            lines.append("- {}: {}.".format(name, "были продажи" if sessions[name]["has_sales"] else "без продаж"))
        if previous:
            old = [name for name in new if sessions[name]["date"] < previous["checked_at"][:10]]
            if old:
                lines.append("Сессии датой до предыдущей проверки, результаты которых обнаружены сейчас: {}. "
                             "Это поздняя публикация или позднее обнаружение исторических данных.".format(
                                 ", ".join(sorted(old))))
    if changed:
        lines.append("Обновлены результаты ранее известных сессий: {} ({}).".format(
            len(changed), ", ".join(sorted(changed))))
    if current["pending"]:
        lines.append("Результаты ещё не опубликованы для {} сессий календаря: {}.".format(
            len(current["pending"]), ", ".join(sorted(current["pending"]))))
    return "\n".join(lines) + "\n"
