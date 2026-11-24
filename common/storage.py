"""Состояние, атомарная запись и блокировка повторного запуска. Python 3.8+."""
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict
from .errors import SourceError
from .data_gateway import DataGateway

def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "announcements": {}, "auctions": {}}
    with path.open("r", encoding="utf-8") as stream:
        state = json.load(stream)
    if (not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("announcements"), dict)
            or not isinstance(state.get("auctions"), dict)):
        raise SourceError("Неверный формат состояния: " + str(path))
    return state



@contextmanager
def run_lock(path: Path):
    # При случайном пересечении двух запусков на Ubuntu второй не перепишет первый.
    with path.open("a") as stream:
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Сборщик уже выполняется: " + str(path)) from exc
        yield



def write_text_atomic(path, content, encoding="utf-8"):
    def writer(temporary):
        with temporary.open("w", encoding=encoding, newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    return DataGateway.publish_file_atomic(path, writer)
