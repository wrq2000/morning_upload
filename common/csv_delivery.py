"""Датированные имена вложений без переименования рабочих CSV."""
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))


def dated_filename(filename, generated_at=None):
    """Дата выгрузки в MSK; время и микросекунды различают повторные выгрузки."""
    path = Path(filename)
    if path.name != str(filename) or path.suffix.lower() != ".csv":
        raise ValueError("Ожидалось имя CSV без пути")
    generated_at = generated_at or datetime.now(MSK)
    if generated_at.tzinfo is None:
        raise ValueError("Время выгрузки должно содержать часовой пояс")
    return path.stem + "_" + generated_at.astimezone(MSK).strftime("%Y-%m-%d_%H%M%S_%f") + path.suffix


@contextmanager
def csv_attachment(source, filename=None):
    source = Path(source)
    if filename is None:
        yield source
        return
    # mtime относится к записи CSV, а не ко времени повторной попытки доставки.
    generated_at = datetime.fromtimestamp(source.stat().st_mtime, MSK)
    with tempfile.TemporaryDirectory(prefix="morning-csv-") as directory:
        target = Path(directory) / dated_filename(filename, generated_at)
        shutil.copyfile(source, target)
        yield target
