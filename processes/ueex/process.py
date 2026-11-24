"""Отдельная задача приложения: сбор УЭБ, CSV и отправка через два SMTP-сервиса."""
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from common.mailer import MailSender, recipient_list
from common.csv_delivery import csv_attachment
from common.storage import run_lock, write_text_atomic
from .config import load_settings
from .pipeline import run_pipeline
from .network import network_settings
from .reporting import changes_text, snapshot_signature

logger = logging.getLogger(__name__)


class UeexProcess:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else load_settings()
        if not self.settings.http.proxy_enabled:
            raise ValueError("УЭБ требует VPN-прокси: установите UEEX_PROXY_ENABLED=true")

    def get_name(self):
        return "УЭБ — экспорт природного газа"

    def _send_report(self, frame, csv_path):
        settings = self.settings
        recipients = recipient_list(os.getenv("UEEX_SUCCESS_RECIPIENTS") or os.getenv("RECIPIENTS", ""))
        if not recipients:
            raise ValueError("Укажите UEEX_SUCCESS_RECIPIENTS или RECIPIENTS")
        report_date = datetime.now(settings.timezone).date().isoformat()
        current_check = frame.attrs["check_snapshot"]
        fingerprint = hashlib.sha256(
            b"ueex-mail-v2\0" + csv_path.read_bytes() + report_date.encode("utf-8")
            + json.dumps(recipients).encode("utf-8") + snapshot_signature(current_check).encode("ascii")
        ).hexdigest()
        state_path = settings.output_dir / "ueex_mail_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if not isinstance(state, dict):
            raise ValueError("Некорректное состояние отправки УЭБ")
        previous_check = state.get("last_reported_check")
        if state.get("version", 1) == 1 and not state.get("pending"):
            # Миграция действующего состояния: история не становится новыми торгами.
            previous_check = frame.attrs.get("previous_check_snapshot")
        if previous_check is not None and not previous_check.get("complete", True):
            previous_check = None
        delivered = set(state.get("accepted", [])) if state.get("fingerprint") == fingerprint else set()
        pending = [address for address in recipients if address not in delivered]
        if not pending:
            logger.info("УЭБ: эта версия CSV уже отправлена всем получателям сегодня")
            return True, True
        complete = current_check.get("complete", True)
        subject = os.getenv("UEEX_EMAIL_SUBJECT", "УЭБ — экспорт природного газа — {date}").format(date=report_date)
        if not complete:
            subject += " [НЕПОЛНЫЕ ДАННЫЕ]"
        body = ("Результаты торгов природным газом на экспорт.\n\n{}\n"
                "Позиций: {}. Компаний: {}.\n"
                "Во вложении таблица CSV с детализацией по компаниям.\n"
                "actual_cost = sold_volume × price_avg.\n").format(
                    changes_text(current_check, previous_check), len(frame), frame["company"].nunique())
        if state.get("fingerprint") == fingerprint and state.get("body"):
            # После частичного отказа/сбоя повторяем тот же отчёт оставшимся адресатам.
            subject, body = state["subject"], state["body"]
        mail_state = {
            "version": 2, "date": report_date, "fingerprint": fingerprint,
            "accepted": sorted(delivered), "pending": pending,
            "subject": subject, "body": body, "last_reported_check": previous_check,
        }
        # Сохраняем исходную точку сравнения даже при ошибке настройки SMTP.
        write_text_atomic(state_path, json.dumps(mail_state, ensure_ascii=False, indent=2) + "\n")
        sender = MailSender()
        with csv_attachment(csv_path, csv_path.name) as attachment:
            sent = sender.send_mail(file_paths=[attachment], subject=subject, body=body, recipients=pending)
        delivered.update(sender.last_delivery["accepted"])
        mail_state.update({
            "accepted": sorted(delivered), "pending": [r for r in recipients if r not in delivered],
            "last_reported_check": current_check if sent and complete else previous_check,
        })
        write_text_atomic(state_path, json.dumps(mail_state, ensure_ascii=False, indent=2) + "\n")
        return sent, False

    def run(self):
        settings = self.settings
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = settings.output_dir / settings.csv_file
        try:
            # Охватывает и сбор, и отправку; внутренний lock сборщика имеет другое имя.
            with run_lock(settings.output_dir / "ueex_process.lock"):
                with network_settings(settings) as effective:
                    frame = asyncio.run(run_pipeline(effective))
                csv_path = Path(frame.attrs.get("csv_path", csv_path))
                # Частичный CSV отправляется отдельно; последний полный файл сохраняется.
                csv_path.chmod(0o644)
                sent, skipped = self._send_report(frame, csv_path)
                complete = frame.attrs.get("collection_complete", True)
                errors = ["{stage} {key}: {message}".format(**item)
                          for item in frame.attrs.get("collection_errors", [])]
                if not sent:
                    errors.append("CSV сохранён, отправка подтверждена не всем получателям")
                return {"success": sent and complete, "all_success": sent and complete,
                        "partial": not complete, "mail_sent": sent, "rows": len(frame),
                        "csv_file": str(csv_path), "mail_skipped": skipped, "errors": errors}
        except Exception as exc:
            logger.warning("УЭБ: источник завершён с предупреждением: %s", exc, exc_info=True)
            return {"success": False, "csv_file": str(csv_path), "errors": [str(exc)]}
