"""Изоляция независимых задач приложения. Python 3.8+."""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)


async def run_processes(processes, registry):
    """Дождаться всех задач; ошибка одной возвращается как её результат."""
    async def invoke(function_name):
        started = time.monotonic()
        function = registry[function_name]
        if not callable(function):
            raise TypeError("Процесс не является функцией: " + function_name)
        result = await function()
        if not isinstance(result, dict):
            raise TypeError("Процесс должен вернуть словарь: " + function_name)
        result.setdefault("duration_sec", round(time.monotonic() - started, 2))
        return result

    names = [name for name, _ in processes]
    if len(set(names)) != len(names):
        raise ValueError("Имена процессов в ENABLED_PROCESSES должны быть уникальными")
    started = time.monotonic()
    results = await asyncio.gather(*(invoke(function_name) for _, function_name in processes),
                                   return_exceptions=True)
    collected = {}
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            logger.warning("%s: предупреждение независимой задачи: %s", name, result,
                         exc_info=(type(result), result, result.__traceback__))
            result = {"success": False, "all_success": False,
                      "duration_sec": round(time.monotonic() - started, 2), "errors": [str(result)]}
        collected[name] = result
    return collected
