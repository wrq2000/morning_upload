"""Ручная проверка ставок текущего и следующего месяца из PyCharm.
Без SMTP, БД и изменения состояния ежемесячного процесса.
"""
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env', override=False)
from common.data_gateway import DataGateway
from processes.export_duty.process import ExportDutyProcess, collect_months, next_month

# Необязательная дата проверки (ISO), None — сегодня по МСК.
CHECK_DATE = None
# True — проверять только официальный источник, без Альта-Софт.
OFFICIAL_ONLY = False


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    process = ExportDutyProcess()
    today = datetime.fromisoformat(CHECK_DATE).date() if CHECK_DATE else datetime.now(process.timezone).date()
    current = today.replace(day=1)
    months = (current, next_month(current))
    print('Проверяем:', ', '.join(m.strftime('%Y-%m') for m in months), flush=True)
    with process.http_client() as client:
        frame, pending, errors = asyncio.run(collect_months(client, months, today, official_only=OFFICIAL_ONLY))
    if not frame.empty:
        path = BASE / 'output' / 'export_duty' / 'export_duty_check.csv'
        path.parent.mkdir(parents=True, exist_ok=True)
        DataGateway.write_csv_atomic(frame, path, sep=';', encoding='utf-8-sig', index=False, lineterminator='\n')
        print(frame.to_string(index=False))
        print('Проверочный CSV:', path)
    else:
        print('Нет подтверждённых ставок; новый CSV не сформирован.')
    for message in pending:
        print('ОЖИДАНИЕ:', message)
    for message in errors:
        print('ОШИБКА:', message)
    print('Получено строк:', len(frame), '| SMTP, БД и state не изменялись.')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
