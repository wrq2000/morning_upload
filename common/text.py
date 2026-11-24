"""Нормализация текста без изменения названий компаний. Python 3.8+."""
import html
from typing import Any


def clean_text(value: Any) -> str:
    return " ".join(html.unescape(str(value if value is not None else "")).split())

