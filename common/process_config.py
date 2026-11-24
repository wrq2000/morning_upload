"""Настройки независимых CSV-процессов из окружения, загруженного main.py."""
import math
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_int(name, default, minimum=1, maximum=None):
    value = int(os.getenv(name, str(default)))
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError("Недопустимое значение " + name)
    return value


def env_float(name, default):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value):
        raise ValueError(name + " должен содержать конечное число")
    return value


def env_bool(name, default=True):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in ("true", "false"):
        raise ValueError(name + " должен быть true или false")
    return value == "true"


def env_path(name, default):
    path = Path(os.getenv(name, default)).expanduser()
    return path if path.is_absolute() else BASE_DIR / path
