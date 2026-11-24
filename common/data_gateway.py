"""
Модуль: common/data_gateway.py

Описание:
    Единый двунаправленный шлюз DataGateway для PostgreSQL, SQLite, Excel, CSV, JSON и HTTP. Читает
    DataFrame, выполняет транзакционные записи через DB-клиент, публикует файлы атомарно через
    временный путь и поддерживает резервные копии. Файловое чтение выполняется в executor;
    набор Excel-файлов объединяется в заданном порядке. Зашифрованные книги требуют
    msoffcrypto-tool только при использовании; одно-листовая книга допускает автоматический
    выбор листа. Синхронные и асинхронные способы чтения используют одинаковые настройки.
    Экземпляр можно повторно запускать после завершения; конкурентный повторный запуск
    запрещён. gather_complete дожидается всех задач, прежде чем передать ошибку, чтобы фоновые
    потоки не работали после закрытия клиента или event loop.
    from_db принимает SQL-текст либо Path к файлу; sql_query разрешает шаблоны
    имен через table_resolver при выполнении выбранного плана. required_columns,
    unique_columns, expected_date и stale_days задают проверки конкретного шлюза.

Python: 3.8 (https://docs.python.org/3.8/)
Обновлено: 2026-10-23 16:01:45 МСК (UTC+03:00)
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

import pandas as pd

from .constants import DATA_SOURCE_TYPES, DB_SOURCE_TYPES

PathSource = Union[str, Path]
logger = logging.getLogger(__name__)


async def gather_complete(*awaitables):
    """Дожидается всех операций и после этого поднимает первое исключение."""
    # Дожидаемся всех чтений; при ошибке не оставляем фоновые операции незавершёнными.
    results = await asyncio.gather(*awaitables, return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise errors[0]
    return tuple(results)


@dataclass
class DataGateway:
    name: str
    source_type: str
    source: Union[str, Path, List[str]]
    query_params: Dict[str, Any] = field(default_factory=dict)
    read_params: Dict[str, Any] = field(default_factory=dict)
    date_columns: Sequence[str] = field(default_factory=tuple)
    index_col: Optional[str] = None
    excel_password: Optional[str] = None
    timeout: Optional[float] = None
    validator: Optional[Callable] = None
    table_resolver: Optional[Callable] = None
    required_columns: Sequence[str] = field(default_factory=tuple)
    unique_columns: Sequence[str] = field(default_factory=tuple)
    expected_date: Optional[Any] = None
    stale_days: Optional[int] = None
    http_client: Optional[Any] = field(default=None, repr=False)
    response_parser: Optional[Callable] = field(default=None, repr=False)
    allow_empty: bool = False
    _data: Optional[pd.DataFrame] = field(default=None, init=False, repr=False)
    _error: Optional[Exception] = field(default=None, init=False, repr=False)
    _load_time: float = field(default=0.0, init=False, repr=False)
    _task: Optional[asyncio.Task] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Проверяем контракт объекта сразу после создания, до выполнения операций.
        self.name = str(self.name).strip()
        if not self.name:
            raise ValueError("DataGateway.name не может быть пустым")
        if self.validator is not None and not callable(self.validator):
            raise TypeError("validator должен быть функцией")
        if self.table_resolver is not None and not callable(self.table_resolver):
            raise TypeError("table_resolver должен быть функцией")
        if self.stale_days is not None and self.stale_days <= 0:
            raise ValueError("stale_days должен быть положительным числом")
        if self.source_type not in DATA_SOURCE_TYPES:
            raise ValueError(f"Неизвестный source_type: {self.source_type!r}")
        if self.timeout is not None and (isinstance(self.timeout, bool) or self.timeout <= 0):
            raise ValueError("timeout должен быть положительным числом")
        if self.source_type == "excel_multi" and not isinstance(self.source, list):
            raise TypeError("Для excel_multi source должен быть списком путей")
        if self.source_type != "excel_multi" and not str(self.source).strip():
            raise ValueError("source не может быть пустым")
        if self.source_type == "http":
            if self.http_client is None or not callable(getattr(self.http_client, "get", None)):
                raise TypeError("Для HTTP требуется http_client с методом get")
            if not callable(self.response_parser):
                raise TypeError("Для HTTP требуется response_parser, возвращающий DataFrame")

    @classmethod
    def from_http(
        cls, name, url, http_client, response_parser, params=None, headers=None,
        date_columns=(), index_col=None, timeout=None, required_columns=(),
        unique_columns=(), allow_empty=False, validator=None,
    ):
        """Описывает HTTP-источник с общим клиентом и отдельным разбором ответа.

        Сетевые настройки принадлежат HttpClient, правила разбора — проекту.
        Пустой календарь допустим, если allow_empty=True; контракт колонок
        и уникальность проверяются так же, как для остальных источников.
        """
        return cls(
            name=name, source_type="http", source=str(url),
            query_params=dict(params or {}), read_params={"headers": dict(headers or {})},
            date_columns=date_columns, index_col=index_col, timeout=timeout,
            required_columns=required_columns, unique_columns=unique_columns,
            http_client=http_client, response_parser=response_parser,
            allow_empty=allow_empty, validator=validator,
        )

    @staticmethod
    async def read_db_session(name, session, query, params=None):
        """Читает DataFrame внутри уже открытой транзакции DB-клиента."""
        # Чтение использует уже открытую сессию, новую транзакцию не создаём.
        result = await session.fetch_df(query, params=params)
        if not isinstance(result, pd.DataFrame):
            raise TypeError("DataGateway[{}]: DB session должна вернуть DataFrame".format(name))
        return result

    @classmethod
    def from_db(
        cls,
        name: str,
        source_type: str,
        query: Union[str, Path],
        date_columns=(),
        index_col=None,
        timeout=None,
        table_resolver=None,
        required_columns=(),
        unique_columns=(),
        expected_date=None,
        stale_days=None,
        **query_params,
    ):
        # query — SQL-текст или Path к .sql. Файл читается только при запросе,
        # поэтому регистрация источников не требует SQL в offline-режиме.
        if source_type not in DB_SOURCE_TYPES:
            raise ValueError("source_type должен быть postgresql или sqlite")
        return cls(
            name,
            source_type,
            query,
            query_params=query_params,
            date_columns=date_columns,
            index_col=index_col,
            timeout=timeout,
            table_resolver=table_resolver,
            required_columns=required_columns,
            unique_columns=unique_columns,
            expected_date=expected_date,
            stale_days=stale_days,
        )

    def sql_query(self) -> str:
        """Прочитать SQL-файл при необходимости и разрешить имена таблиц."""
        # Даты и прочие значения остаются именованными параметрами драйвера.
        query = self.source.read_text(encoding="utf-8") if isinstance(self.source, Path) else self.source
        if self.table_resolver is not None:
            query = re.sub(r"\{\{(\w+)\.([\w]+)\}\}",
                           lambda match: self.table_resolver(match[1], match[2]), query)
        if isinstance(self.source, Path) or self.table_resolver is not None:
            if "{{" in query or "}}" in query:
                raise ValueError("Неизвестный SQL-шаблон: " + self.name)
        return query

    @classmethod
    def from_excel(
        cls,
        name: str,
        file_path: PathSource,
        sheet_name=None,
        date_columns=(),
        index_col=None,
        excel_password=None,
        timeout=None,
        required_columns=(),
        unique_columns=(),
        expected_date=None,
        stale_days=None,
        **read_params,
    ):
        # Чтение Excel описываем отдельно от выполнения операции.
        params = {"sheet_name": sheet_name, **read_params}
        password = (
            excel_password if excel_password is not None else os.getenv("EXCEL_PASSWORD") or None
        )
        return cls(
            name,
            "excel",
            str(file_path),
            read_params=params,
            date_columns=date_columns,
            index_col=index_col,
            excel_password=password,
            timeout=timeout,
            required_columns=required_columns,
            unique_columns=unique_columns,
            expected_date=expected_date,
            stale_days=stale_days,
        )

    @classmethod
    def read_excel_sync(
        cls, name: str, file_path: PathSource, sheet_name=None, excel_password=None, **read_params
    ) -> pd.DataFrame:
        """Синхронно читает Excel тем же транспортом, что и from_excel()."""
        # Синхронное чтение используется для небольших входных файлов.
        fetcher = cls.from_excel(
            name, file_path, sheet_name=sheet_name, excel_password=excel_password, **read_params
        )
        result = fetcher._read_excel_sync(str(file_path), fetcher.read_params)
        fetcher._data = result
        fetcher._normalize()
        return fetcher.get_data()

    @classmethod
    def read_excel_sheets_sync(
        cls, name: str, file_path: PathSource, excel_password=None, **read_params
    ) -> Dict[str, pd.DataFrame]:
        """Читает все листы Excel с общей поддержкой пароля."""
        # Сохраняем имена листов при чтении всей книги.
        fetcher = cls.from_excel(name, file_path, sheet_name=None, excel_password=excel_password)
        params = {"sheet_name": None, **read_params}
        path = str(file_path)
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        if zipfile.is_zipfile(path):
            return pd.read_excel(path, **params)
        if not fetcher.excel_password:
            raise ValueError(
                f"DataGateway[{name}]: Excel зашифрован, но EXCEL_PASSWORD не задан: {path}"
            )
        try:
            import msoffcrypto
        except ImportError as error:
            raise RuntimeError(
                "Для чтения зашифрованного Excel установите msoffcrypto-tool"
            ) from error
        with open(path, "rb") as source:
            office = msoffcrypto.OfficeFile(source)
            office.load_key(password=fetcher.excel_password)
            decrypted = BytesIO()
            office.decrypt(decrypted)
            decrypted.seek(0)
            return pd.read_excel(decrypted, **params)

    @staticmethod
    def publish_file_atomic(
        path: PathSource, writer: Callable[[Path], None], backup: bool = False
    ) -> Path:
        """Пишет временный файл и только после успеха заменяет целевой."""
        # Писатель создаёт временный файл рядом с целевым; заменяем результат после успеха.
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{target.stem}.", suffix=target.suffix, dir=str(target.parent)
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            writer(temporary)
            if backup and target.exists():
                backup_path = target.with_name(f"{target.stem}.backup{target.suffix}")
                shutil.copy2(str(target), str(backup_path))
            temporary.replace(target)
            return target
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def write_excel_atomic(
        cls, frame: pd.DataFrame, path: PathSource, backup: bool = False, **params
    ) -> Path:
        """Атомарно сохраняет один DataFrame в Excel."""
        # Запись DataFrame делегируем общему механизму атомарной публикации.
        return cls.publish_file_atomic(
            path, lambda temporary: frame.to_excel(temporary, **params), backup
        )

    @classmethod
    def from_multiple_excel(
        cls,
        name: str,
        file_paths,
        sheet_name=None,
        date_columns=(),
        index_col=None,
        excel_password=None,
        timeout=None,
        concat_kwargs=None,
        **read_params,
    ):
        # Параметры чтения и объединения сохраняем до выполнения задачи.
        params = {"sheet_name": sheet_name, "concat_kwargs": concat_kwargs or {}, **read_params}
        password = (
            excel_password if excel_password is not None else os.getenv("EXCEL_PASSWORD") or None
        )
        return cls(
            name,
            "excel_multi",
            [str(path) for path in file_paths],
            read_params=params,
            date_columns=date_columns,
            index_col=index_col,
            excel_password=password,
            timeout=timeout,
        )

    @classmethod
    def read_csv_sync(cls, name: str, file_path: PathSource, **read_params) -> pd.DataFrame:
        """Синхронное чтение CSV через общий контракт источников."""
        # Проверяем наличие файла перед передачей параметров pandas.
        fetcher = cls.from_csv(name, file_path, **read_params)
        if not Path(file_path).is_file():
            raise FileNotFoundError(file_path)
        fetcher._data = pd.read_csv(file_path, **fetcher.read_params)
        fetcher._normalize()
        return fetcher.get_data()

    @classmethod
    def write_csv_atomic(cls, frame, path, **params):
        # Экспорт CSV тоже выполняется через временный файл.
        return cls.publish_file_atomic(path, lambda temporary: frame.to_csv(temporary, **params))

    @classmethod
    def from_csv(
        cls,
        name: str,
        file_path: PathSource,
        date_columns=(),
        index_col=None,
        timeout=None,
        **read_params,
    ):
        # Даты и индекс нормализуются после чтения источника.
        return cls(
            name,
            "csv",
            str(file_path),
            read_params=read_params,
            date_columns=date_columns,
            index_col=index_col,
            timeout=timeout,
        )

    def create_task(self, db_client=None) -> asyncio.Task:
        # Один объект DataGateway не должен запускаться повторно одновременно.
        if self._task is not None and not self._task.done():
            raise RuntimeError(f"DataGateway[{self.name}] уже выполняется")
        if self.source_type in DB_SOURCE_TYPES and db_client is None:
            raise ValueError(f"DataGateway[{self.name}]: требуется db_client")
        self._task = asyncio.create_task(self._execute(db_client))
        return self._task

    @classmethod
    def from_json(
        cls, name, file_path, date_columns=(), index_col=None, timeout=None, **read_params
    ):
        # JSON с прогнозами тоже входит в граф зависимостей как обычный DataFrame.
        return cls(
            name,
            "json",
            str(file_path),
            read_params=read_params,
            date_columns=date_columns,
            index_col=index_col,
            timeout=timeout,
        )

    async def fetch(self, db_client=None) -> pd.DataFrame:
        """Загружает источник и возвращает готовый DataFrame."""
        # Ожидаем собственную задачу чтения и возвращаем подготовленные данные.
        await self.create_task(db_client)
        return self.get_data()

    async def _execute(self, db_client=None) -> None:
        # Фиксируем время загрузки и сохраняем ошибку для последующего доступа.
        started = time.perf_counter()
        self._data = None
        self._error = None
        try:
            if self.source_type in DB_SOURCE_TYPES:
                operation = db_client.fetch_df(self.sql_query(), params=self.query_params)
            elif self.source_type == "excel":
                operation = self._read_excel(self.source)
            elif self.source_type == "excel_multi":
                operation = self._read_multiple_excel()
            elif self.source_type == "json":
                operation = self._read_json()
            elif self.source_type == "http":
                operation = self._read_http()
            else:
                operation = self._read_csv()
            self._data = (
                await asyncio.wait_for(operation, self.timeout) if self.timeout else await operation
            )
            self._normalize()
            if self.validator is not None:
                self._data = self.validator(self._data)
                if not isinstance(self._data, pd.DataFrame):
                    raise TypeError("validator должен вернуть DataFrame")
        except Exception as error:
            self._error = error
            self._data = None
            raise
        finally:
            self._load_time = time.perf_counter() - started

    @staticmethod
    async def _run(func, *args, **kwargs):
        # Блокирующее чтение файла выполняем вне основного цикла asyncio.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    def _read_excel_sync(self, path: str, params: Dict[str, Any]) -> pd.DataFrame:
        # При необходимости расшифровываем книгу до передачи pandas.
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        if zipfile.is_zipfile(path):
            return self._read_excel_source(path, params)
        if not self.excel_password:
            raise ValueError(
                f"DataGateway[{self.name}]: Excel зашифрован, но EXCEL_PASSWORD не задан: {path}"
            )
        try:
            import msoffcrypto
        except ImportError as error:
            raise RuntimeError(
                "Для чтения зашифрованного Excel установите msoffcrypto-tool"
            ) from error
        with open(path, "rb") as source:
            office = msoffcrypto.OfficeFile(source)
            office.load_key(password=self.excel_password)
            decrypted = BytesIO()
            office.decrypt(decrypted)
            decrypted.seek(0)
            return self._read_excel_source(decrypted, params)

    def _read_excel_source(self, source, params: Dict[str, Any]) -> pd.DataFrame:
        # Единый путь чтения используется для одиночного и многолистового источника.
        params = dict(params)
        sheet_name = params.pop("sheet_name", None)
        with pd.ExcelFile(source) as workbook:
            sheets = workbook.sheet_names
            if sheet_name is None:
                if len(sheets) != 1:
                    raise ValueError(
                        f"DataGateway[{self.name}]: в Excel {len(sheets)} листов; "
                        f"укажите sheet_name, доступно: {sheets}"
                    )
                sheet_name = sheets[0]
            elif isinstance(sheet_name, str) and sheet_name not in sheets:
                raise ValueError(
                    f"DataGateway[{self.name}]: лист {sheet_name!r} не найден; "
                    f"доступно: {sheets}"
                )
            return pd.read_excel(workbook, sheet_name=sheet_name, **params)

    async def _read_excel(self, path: str) -> pd.DataFrame:
        # Параметры конкретного листа передаём синхронному читателю в отдельном потоке.
        return await self._run(self._read_excel_sync, path, self.read_params)

    async def _read_multiple_excel(self) -> pd.DataFrame:
        # Каждую книгу читаем независимо, затем объединяем готовые кадры.
        params = dict(self.read_params)
        concat_kwargs = params.pop("concat_kwargs", {})
        paths = [path for path in self.source if Path(path).is_file()]
        if not paths:
            raise FileNotFoundError(f"DataGateway[{self.name}]: файлы не найдены")
        frames = await gather_complete(
            *(self._run(self._read_excel_sync, path, params) for path in paths)
        )
        return pd.concat(frames, **concat_kwargs)

    async def _read_csv(self) -> pd.DataFrame:
        # Проверяем путь и читаем CSV вне основного цикла событий.
        path = str(self.source)
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        return await self._run(pd.read_csv, path, **self.read_params)

    async def _read_json(self) -> pd.DataFrame:
        # Для словаря имя → значение используйте typ="series"; табличный JSON читается напрямую.
        path = str(self.source)
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        result = await self._run(pd.read_json, path, **self.read_params)
        if isinstance(result, pd.Series):
            result = result.rename_axis("name").reset_index(name="value")
        if not isinstance(result, pd.DataFrame):
            raise TypeError(f"DataGateway[{self.name}]: JSON должен содержать таблицу или словарь")
        return result

    async def _read_http(self) -> pd.DataFrame:
        # Вызов клиента и разбор ответа выполняются в executor; результат
        # проходит стандартные проверки, журнал времени и хранение ошибок.
        def read():
            response = self.http_client.get(self.source, params=self.query_params, **self.read_params)
            try:
                frame = self.response_parser(response)
                if not isinstance(frame, pd.DataFrame):
                    raise TypeError("response_parser должен вернуть DataFrame")
                return frame
            finally:
                response.close()
        return await self._run(read)

    def _normalize(self) -> None:
        # Приводим объявленные даты и индекс только после успешной загрузки.
        if self._data is None:
            return
        # Контракт принадлежит экземпляру шлюза, отдельный реестр не нужен.
        missing = set(self.required_columns) | set(self.unique_columns)
        missing -= set(self._data.columns)
        if missing:
            raise ValueError("{}: отсутствуют поля {}".format(self.name, sorted(missing)))
        if (self.required_columns or self.unique_columns) and self._data.empty and not self.allow_empty:
            raise ValueError("{}: нет данных за выбранный период".format(self.name))
        for column in self.date_columns:
            if column not in self._data.columns:
                raise ValueError(f"DataGateway[{self.name}]: нет колонки даты {column!r}")
            self._data[column] = pd.to_datetime(self._data[column], errors="raise")
        if self.unique_columns:
            keys = list(self.unique_columns)
            if self._data[keys].isna().any().any() or self._data.duplicated(keys).any():
                raise ValueError(self.name + ": пустые или повторяющиеся значения ключа " + str(tuple(keys)))
        if "date" in self._data and self.expected_date is not None and self.stale_days is not None:
            last = self._data["date"].max()
            if (pd.Timestamp(self.expected_date) - last).days > self.stale_days:
                logger.warning("Источник %s: последнее наблюдение %s", self.name, last.date())
        self._data.attrs.setdefault("source", self.name)
        self._data.attrs.setdefault("comment", "Исходные данные: " + self.name)
        if self.index_col:
            if self.index_col not in self._data.columns:
                raise ValueError(f"DataGateway[{self.name}]: нет index_col={self.index_col!r}")
            self._data = self._data.set_index(self.index_col)

    def get_data(self) -> pd.DataFrame:
        # Ошибку и незавершённую загрузку не маскируем пустым DataFrame.
        if self._error:
            raise RuntimeError(f"DataGateway[{self.name}] завершился с ошибкой") from self._error
        if self._data is None:
            raise RuntimeError(f"DataGateway[{self.name}]: данные еще не загружены")
        return self._data

    @property
    def is_ready(self) -> bool:
        # Готовность означает завершённое чтение без ошибки.
        return self._data is not None and self._error is None

    @property
    def error(self) -> Optional[Exception]:
        # Возвращаем сохранённую ошибку, не выполняя повторное чтение.
        return self._error

    @property
    def load_time(self) -> float:
        # Длительность относится к последней загрузке этого объекта.
        return self._load_time

    @staticmethod
    async def write_db_batch(db_client, query, records):
        """Write mappings atomically through the same gateway; rollback on failure."""
        # Вся партия записывается через одну транзакционную сессию.
        records = list(records)
        async with db_client.transaction() as session:
            return await session.executemany(query, records)

    @staticmethod
    async def write_db_session(session, query, params=None):
        """Execute a write inside an already open transaction."""
        # Используем сессию вызывающего кода, чтобы сохранить общую транзакцию.
        return await session.execute(query, params=params)
