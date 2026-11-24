"""Ежемесячный CSV-процесс с повторными попытками до успешной отправки."""
import json
import logging
import time
from calendar import monthrange
from datetime import date, datetime, timedelta

from .csv_process import CsvMailProcess
from .process_config import env_int, env_path
from .storage import run_lock, write_text_atomic

logger = logging.getLogger(__name__)


def latest_cycle(today, run_day):
    """Последняя наступившая дата запуска, для коротких месяцев — последний день."""
    month = today.replace(day=1)
    scheduled = month.replace(day=min(run_day, monthrange(month.year, month.month)[1]))
    if today < scheduled:
        month = (month - timedelta(days=1)).replace(day=1)
        scheduled = month.replace(day=min(run_day, monthrange(month.year, month.month)[1]))
    return scheduled


class MonthlyCsvMailProcess(CsvMailProcess):
    def __init__(self, prefix, label, default_filename):
        super().__init__(prefix, label, default_filename)
        self.run_day = env_int(prefix + "_RUN_DAY", 25, maximum=31)
        self.state_file = env_path(prefix + "_STATE_FILE", "state/" + prefix.lower() + "_monthly.json")

    def run(self):
        started = time.monotonic()
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            lock_file = self.state_file.with_name(self.state_file.name + ".lock")
            # Проверка флага, скачивание, письмо и запись успеха под одной блокировкой.
            with run_lock(lock_file):
                today = datetime.now(self.timezone).date()
                cycle = latest_cycle(today, self.run_day)
                previous = None
                if self.state_file.exists():
                    state = json.loads(self.state_file.read_text(encoding="utf-8"))
                    if not isinstance(state, dict) or state.get("version") != 1:
                        raise ValueError("Неверный формат состояния " + str(self.state_file))
                    previous = date.fromisoformat(state["last_successful_cycle"])
                if previous is not None and previous >= cycle:
                    logger.info("%s: цикл %s уже завершён, пропуск", self.label, cycle)
                    return {
                        "success": True, "all_success": True, "skipped": True,
                        "cycle": cycle.isoformat(), "errors": [],
                        "duration_sec": round(time.monotonic() - started, 2),
                    }

                logger.info("%s: выполняется цикл %s, дата попытки %s", self.label, cycle, today)
                result = super().run()
                result["cycle"] = cycle.isoformat()
                if result.get("all_success", result.get("success", False)):
                    state = {
                        "version": 1, "last_successful_cycle": cycle.isoformat(),
                        "completed_at": datetime.now(self.timezone).isoformat(timespec="seconds"),
                        "csv_file": result["csv_file"], "rows": result["rows"],
                    }
                    write_text_atomic(
                        self.state_file, json.dumps(state, ensure_ascii=False, indent=2) + "\n"
                    )
                    self.state_file.chmod(0o644)
                    logger.info("%s: успешный цикл %s сохранён в %s",
                                self.label, cycle, self.state_file)
                result["duration_sec"] = round(time.monotonic() - started, 2)
                return result
        except Exception as exc:
            logger.exception("%s: ежемесячный процесс не завершён", self.label)
            return {
                "success": False, "all_success": False, "errors": [str(exc)],
                "duration_sec": round(time.monotonic() - started, 2),
            }
