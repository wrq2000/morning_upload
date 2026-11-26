"""Физические перетоки ENTSO-E File Library, локальные сутки. Python 3.10+.
Обновлено: 25.11.2026, МСК.
"""
import asyncio
import logging
import math
import os
import time as clock
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from common.csv_process import CsvMailProcess
from common.errors import SourceError
from common.process_config import env_float, env_int
from .config import EIC, TIMEZONES, get_directions

logger = logging.getLogger(__name__)
FOLDER = 'PhysicalFlows_12.1.G_r3'
COLUMNS = ('DateTime(UTC)', 'ResolutionCode', 'OutAreaCode', 'InAreaCode',
           'Flow[MW]', 'UpdateTime(UTC)')
FIELDS = ('date', 'country_from', 'country_to', 'flow_mw', 'gas_ecv',
          'coverage_minutes', 'expected_minutes', 'resolution_min', 'resolution_max',
          'source_points_count')
UTC = timezone.utc
STEP = timedelta(minutes=15)
RESOLUTIONS = {'PT15M': 15, 'PT30M': 30, 'PT60M': 60, 'PT1H': 60}
CHUNK_SIZE = 100_000


@dataclass(frozen=True)
class Slot:
    value: float
    resolution: int
    source_time: datetime


def bounds(day, tz):
    return datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)


def window_bounds(start, end, directions):
    if start >= end or not directions:
        raise ValueError('ENTSO-E: пустой период или список направлений')
    zones = {ZoneInfo(TIMEZONES[a]) for a, _ in directions}
    return min(bounds(start, z) for z in zones), max(bounds(end, z) for z in zones)


def required_months(start, end, directions):
    lower, upper = window_bounds(start, end, directions)
    last = upper - timedelta(microseconds=1)
    return pd.period_range(lower.replace(tzinfo=None), last.replace(tzinfo=None), freq='M').astype(str).tolist()


def parse_utc(values):
    """ISO-8601 или DD/MM/YYYY (согласно спецификации File Library)."""
    values = values.astype('string').str.strip()
    iso = values.str.match(r'^\d{4}-', na=False)
    parsed = pd.to_datetime(values.where(iso), format='ISO8601', utc=True, errors='raise')
    parsed.loc[~iso] = pd.to_datetime(values.loc[~iso], format='mixed', dayfirst=True, utc=True, errors='raise')
    return parsed


def read_month(content, filename, directions, start, end):
    """CSV читается из ZIP по частям; сохраняем лишь нужные EIC и даты."""
    lower, upper = window_bounds(start, end, directions)
    eic_to_country = {EIC[c]: c for pair in directions for c in pair}
    parts = []
    total = 0
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            members = [i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith('.csv')]
            if len(members) != 1 or PurePosixPath(members[0].filename).name != PurePosixPath(filename).name:
                raise SourceError('ENTSO-E: ZIP не содержит ровно один ожидаемый CSV: ' + filename)
            logger.info('ENTSO-E: %s, ZIP %.1f МБ, CSV %.1f МБ', filename, len(content)/1e6, members[0].file_size/1e6)
            with archive.open(members[0]) as stream:
                with pd.read_csv(stream, sep='\t', encoding='utf-8-sig', usecols=list(COLUMNS),
                                 dtype='string', chunksize=CHUNK_SIZE) as reader:
                    for raw in reader:
                        total += len(raw)
                        raw['country_from'] = raw['OutAreaCode'].map(eic_to_country)
                        raw['country_to'] = raw['InAreaCode'].map(eic_to_country)
                        mask = pd.MultiIndex.from_frame(raw[['country_from', 'country_to']]).isin(directions)
                        selected = raw.loc[mask, ['DateTime(UTC)', 'ResolutionCode', 'Flow[MW]', 'UpdateTime(UTC)', 'country_from', 'country_to']].copy()
                        if selected.empty:
                            continue
                        selected['utc'] = parse_utc(selected['DateTime(UTC)'])
                        if selected['utc'].isna().any():
                            raise SourceError('ENTSO-E: отсутствует дата исходной точки')
                        selected = selected.loc[(selected['utc'] >= lower) & (selected['utc'] < upper)].copy()
                        if selected.empty:
                            continue
                        selected['updated'] = parse_utc(selected['UpdateTime(UTC)'])
                        selected['resolution'] = selected['ResolutionCode'].str.strip().map(RESOLUTIONS)
                        selected['value'] = pd.to_numeric(selected['Flow[MW]'].str.strip(), errors='raise')
                        parts.append(selected[['country_from', 'country_to', 'utc', 'updated', 'resolution', 'value']])
    except (zipfile.BadZipFile, ValueError, KeyError, UnicodeError) as exc:
        raise SourceError('ENTSO-E: неверная структура месячного файла {} ({})'.format(filename, type(exc).__name__)) from None
    logger.info('ENTSO-E: прочитано %s строк, отобрано %s', total, sum(map(len, parts)))
    return parts


def latest_points(data):
    """Последняя публикация точки; равные ревизии с конфликтом не выбираем произвольно."""
    keys = ['country_from', 'country_to', 'utc']
    data = data.sort_values('updated', na_position='first', kind='stable')
    # Включая NaT: если у всех версий нет UpdateTime, конфликт тоже неоднозначен.
    newest = data.drop_duplicates(keys, keep='last')
    candidates = data.merge(newest[keys + ['updated']], on=keys + ['updated'], how='inner')
    conflicts = candidates.groupby(keys, dropna=False)[['resolution', 'value']].nunique(dropna=False).gt(1).any(axis=1)
    bad = set(conflicts[conflicts].index)
    return newest, bad


def daily_frame(data, bad, country_from, country_to, start, end):
    """Явная длительность каждой строки CSV. Не заполняем отсутствующие точки."""
    tz = ZoneInfo(TIMEZONES[country_from])
    lower, upper = bounds(start, tz), bounds(end, tz)
    slots = {}
    invalid = set()
    for row in data.itertuples(index=False):
        stamp = row.utc.to_pydatetime()
        if not lower <= stamp < upper:
            continue
        if (pd.isna(row.resolution) or stamp.second or stamp.microsecond
                or stamp.minute % 15):
            # Без однозначной длительности нельзя подтвердить полноту этих суток.
            day = stamp.astimezone(tz).date()
            invalid.add(day)
            logger.warning('ENTSO-E %s → %s %s: неизвестная/некорректная ResolutionCode или дата', country_from, country_to, day)
            continue
        minutes = int(row.resolution)
        if (country_from, country_to, row.utc) in bad or pd.isna(row.value) or not math.isfinite(float(row.value)):
            # Не заменяем пропуск или неоднозначную ревизию нулём/старой версией.
            invalid.add(stamp.astimezone(tz).date())
            continue
        slot = Slot(float(row.value), minutes, stamp)
        stop = min(upper, stamp + timedelta(minutes=minutes))
        t = stamp
        while t < stop:
            old = slots.get(t)
            if old is not None and old != slot:
                # Накладывающиеся интервалы разных исходных точек — неоднозначность.
                invalid.add(t.astimezone(tz).date())
            else:
                slots[t] = slot
            t += STEP
    rows = []
    day = start
    previous = None
    while day < end:
        first, last = bounds(day, tz), bounds(day + timedelta(days=1), tz)
        count = (last - first) // STEP
        points = [slots.get(first + i * STEP) for i in range(count)]
        if day not in invalid and all(slot is not None for slot in points):
            mw = math.fsum(slot.value for slot in points) / count
            resolutions = frozenset(slot.resolution for slot in points)
            rows.append(dict(date=day.isoformat(), country_from=country_from, country_to=country_to,
                             flow_mw=mw, gas_ecv=mw * 24 * 2 / 10750,
                             coverage_minutes=count * 15, expected_minutes=count * 15,
                             resolution_min=min(resolutions), resolution_max=max(resolutions),
                             source_points_count=len({slot.source_time for slot in points})))
            if len(resolutions) > 1:
                logger.warning('ENTSO-E %s → %s, %s: смешанная гранулярность %s мин', country_from, country_to, day, sorted(resolutions))
            if previous is not None and previous[1] != resolutions:
                logger.warning('ENTSO-E %s → %s: изменение гранулярности %s мин (%s) → %s мин (%s)',
                               country_from, country_to, sorted(previous[1]), previous[0], sorted(resolutions), day)
            previous = (day, resolutions)
        day += timedelta(days=1)
    return pd.DataFrame(rows, columns=FIELDS)


def retry_call(fn, attempts, label):
    """Повторы временных сетевых ошибок; не выводим тело запроса/учётные данные."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except requests.RequestException as exc:
            status = exc.response.status_code if exc.response is not None else None
            retry = status in (408, 425, 429, 500, 502, 503, 504) or (status is None and isinstance(exc, (requests.Timeout, requests.ConnectionError)) and not isinstance(exc, requests.exceptions.SSLError))
            message = 'ENTSO-E: {}: {}'.format(label, 'HTTP ' + str(status) if status else type(exc).__name__)
            if not retry or attempt == attempts:
                raise SourceError(message) from None
            delay = min(60, 5 * 2**(attempt - 1))
            logger.warning('%s; повтор через %s с (%s/%s)', message, delay, attempt, attempts)
            clock.sleep(delay)
        except Exception as exc:
            raise SourceError('ENTSO-E: {}: {}'.format(label, type(exc).__name__)) from None


class EntsoeFlowsProcess(CsvMailProcess):
    def __init__(self):
        super().__init__('ENTSOE', 'ENTSO-E: физические перетоки', 'entsoe_flows.csv')
        self.days = env_int('ENTSOE_LOOKBACK_DAYS', 10, maximum=31)
        self.attempts = env_int('ENTSOE_HTTP_ATTEMPTS', 3)
        self.missing = []

    @contextmanager
    def http_client(self):
        """Используем File Library как источник вместо отдельного HTTP GET на каждый EIC."""
        username = os.getenv('ENTSOE_USERNAME', '').strip()
        pwd = os.getenv('ENTSOE_PWD', '')
        if not username or not pwd:
            raise SourceError('Укажите ENTSOE_USERNAME и ENTSOE_PWD в .env (File Library)')
        try:
            from entsoe.files import EntsoeFileClient
        except ImportError:
            raise SourceError('Установите entsoe-py==0.8.1 в интерпретатор проекта morning_upload') from None
        proxy = os.getenv('ENTSOE_PROXY_URL', '').strip()
        connect = env_float('ENTSOE_CONNECT_TIMEOUT', 10)
        read = env_float('ENTSOE_READ_TIMEOUT', 180)
        if min(connect, read) <= 0:
            raise ValueError('ENTSO-E: тайм-ауты должны быть положительными')
        with requests.Session() as session:
            client = retry_call(lambda: EntsoeFileClient(username=username, pwd=pwd, session=session,
                                proxies={'http': proxy, 'https': proxy} if proxy else None,
                                timeout=(connect, read)), self.attempts, 'авторизация File Library')
            yield client

    def collect_months(self, client, today):
        self.missing = []
        start, end = today - timedelta(days=self.days), today
        directions = get_directions()
        months = required_months(start, end, directions)
        logger.info('ENTSO-E: период %s — %s (не включительно), месяцы UTC: %s, направлений %s', start, end, ', '.join(months), len(directions))
        available = retry_call(lambda: client.list_folder(FOLDER), self.attempts, 'список файлов')
        parts = []
        # По одному ZIP в памяти; месяцы обновляются при каждом запуске.
        for i, month in enumerate(months, 1):
            prefix = month.replace('-', '_') + '_'
            names = [name for name in available if name.startswith(prefix) and name.lower().endswith('.csv')]
            if len(names) != 1:
                raise SourceError('ENTSO-E: за {} ожидался один CSV, найдено {}'.format(month, len(names)))
            filename = names[0]
            logger.info('ENTSO-E [%s/%s]: скачиваем %s', i, len(months), filename)
            t = clock.monotonic()
            content = retry_call(lambda: client.download_multiple_files_raw([available[filename]]), self.attempts, 'скачивание ' + filename)
            logger.info('ENTSO-E: скачивание завершено за %.1f с', clock.monotonic() - t)
            parts.extend(read_month(content, filename, directions, start, end))
            del content
        if not parts:
            raise SourceError('ENTSO-E: в месячных файлах нет точек за нужный период')
        data, bad = latest_points(pd.concat(parts, ignore_index=True))
        del parts
        groups = dict(tuple(data.groupby(['country_from', 'country_to'], sort=False)))
        frames = []
        for a, b in directions:
            group = groups.get((a, b), data.iloc[:0])
            frame = daily_frame(group, bad, a, b, start, end)
            if len(frame) < self.days:
                got = set(frame['date'])
                missing = [(start + timedelta(days=j)).isoformat() for j in range(self.days) if (start + timedelta(days=j)).isoformat() not in got]
                self.missing.append('{} → {}: {}'.format(a, b, ', '.join(missing)))
                logger.warning('ENTSO-E: неполные/отсутствующие сутки %s', self.missing[-1])
            if not frame.empty:
                frames.append(frame)
        if not frames:
            raise SourceError('ENTSO-E: ни одних полных суток по заданным направлениям')
        result = pd.concat(frames, ignore_index=True).sort_values(['date', 'country_from', 'country_to'])
        if result.duplicated(['date', 'country_from', 'country_to']).any():
            raise SourceError('ENTSO-E: дубли в суточном результате')
        body = ('Физические перетоки ENTSO-E File Library (12.1.G).\n'
                f'Период: {start} — {end - timedelta(days=1)} включительно.\n'
                f'Месячные файлы UTC: {", ".join(months)}.\n'
                f'Строк: {len(result)} из максимум {self.days * len(directions)}.\n'
                'Локальные сутки по стране отправления. MW — среднее по времени; '
                'gas_ecv = flow_mw * 24 * 2 / 10750.\n'
                'Неполные/неоднозначные сутки исключены; нули не подставляются.\n'
                'resolution_min/max — исходная гранулярность ResolutionCode; '
                'source_points_count — число уникальных исходных точек.\n'
                'Источник: https://transparency.entsoe.eu/\n')
        if self.missing:
            body += '\nПропуски/ошибки направлений:\n' + '\n'.join(self.missing)
        return result, body

    async def collect(self, client, today):
        return await asyncio.to_thread(self.collect_months, client, today)

    def run(self):
        result = super().run()
        if result.get('success') and self.missing:
            result.update(partial=True, all_success=False, errors=[
                'ENTSO-E: {} направлений с пропусками/ошибками; подробности в журнале и письме'.format(len(self.missing))])
        return result
