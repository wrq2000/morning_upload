"""
Точка входа для запуска всех процессов сбора данных
"""
import logging
from common.logfile import setup_logging
from processes.belaes import BelaesProcess

# Настройка логирования (ТОЛЬКО ОДИН РАЗ!) и загрузка переменных окружения
setup_logging()
logger = logging.getLogger(__name__)
from dotenv import load_dotenv
load_dotenv()
import random
import sys
import asyncio
import time
from datetime import datetime
from processes.argus import ArgusProcess
from processes.desfa_lng_plan import DesfaUnloadingPlanProcess
from processes.epias import EpiasProcess, processing_config, url_dict
from processes.tuzgolu import TuzGoluFixedTemplateParser  # ← ДОБАВЛЕНО


# === УПРАВЛЕНИЕ ПРОЦЕССАМИ: для отключения достаточно закомментировать строку ===
ENABLED_PROCESSES = [
    ("Argus Media", "run_argus"),
    # ("DESFA", "run_desfa"),
    # ("БелАЭС", "run_belaes"),
    # ("Tuz Gölü", "run_tuzgolu"),  # ← ДОБАВЛЕНО
    # ("EPİAŞ", "run_epias"),  # Закомментировать эту строку для полного отключения процесса
]
# ================================================================================


async def run_argus() -> dict:
    """Асинхронная обёртка для Argus с случайной задержкой (0-600 сек)"""
    delay = random.uniform(0, 600)
    logger.info(f"[1/{len(ENABLED_PROCESSES)}] Запуск Argus Media с задержкой {delay:.1f} секунд...")

    await asyncio.sleep(delay)
    logger.info(f"✅ Задержка завершена, начинаю обработку Argus Media")
    start = time.time()

    try:
        argus = ArgusProcess()
        result = await asyncio.to_thread(argus.run)
        duration = time.time() - start
        result['duration_sec'] = round(duration, 2)
        logger.info(f"✅ Argus Media завершён за {duration:.1f} сек")
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка выполнения Argus: {e}", exc_info=True)
        return {
            'success': False,
            'all_success': False,
            'duration_sec': round(time.time() - start, 2),
            'errors': [str(e)]
        }





async def run_tuzgolu() -> dict:  # ← ДОБАВЛЕНО
    """Асинхронная обёртка для Tuz Gölü EBT"""
    logger.info(f"[{len([p for p in ENABLED_PROCESSES if 'tuzgolu' in p[1].lower()]) + 1}/{len(ENABLED_PROCESSES)}] Запуск Tuz Gölü...")
    start = time.time()

    try:
        tuzgolu = TuzGoluFixedTemplateParser()
        result = await asyncio.to_thread(tuzgolu.run)
        duration = time.time() - start
        result['duration_sec'] = round(duration, 2)
        logger.info(f"✅ Tuz Gölü завершён за {duration:.1f} сек")
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка выполнения Tuz Gölü: {e}", exc_info=True)
        return {
            'success': False,
            'processed': 0,
            'failed': 1,
            'duration_sec': round(time.time() - start, 2),
            'errors': [str(e)]
        }

...  # остальной код


async def run_desfa() -> dict:
    """Асинхронная обёртка для DESFA"""
    # logger.info(f"[2/{len(ENABLED_PROCESSES)}] Запуск DESFA...")
    logger.info(
        f"[{len([p for p in ENABLED_PROCESSES if 'desfa' in p[1].lower()]) + 1}/{len(ENABLED_PROCESSES)}] Запуск DESFA...")
    start = time.time()

    try:
        desfa = DesfaUnloadingPlanProcess()
        result = await asyncio.to_thread(desfa.run)
        duration = time.time() - start
        result['duration_sec'] = round(duration, 2)
        logger.info(f"✅ DESFA завершён за {duration:.1f} сек")
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка выполнения DESFA: {e}", exc_info=True)
        return {
            'success': False,
            'duration_sec': round(time.time() - start, 2),
            'errors': [str(e)]
        }


async def run_epias() -> dict:
    """Асинхронная обёртка для EPİAŞ"""
    logger.info(f"[3/{len(ENABLED_PROCESSES)}] Запуск EPİAŞ...")
    start = time.time()

    try:
        processor = EpiasProcess(
            processing_config=processing_config,
            url_dict=url_dict
        )
        result = await asyncio.to_thread(processor.run)
        duration = time.time() - start
        result['duration_sec'] = round(duration, 2)
        logger.info(f"✅ EPİAŞ завершён за {duration:.1f} сек")
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка выполнения EPİAŞ: {e}", exc_info=True)
        return {
            'success': False,
            'sources_processed': 0,
            'sources_failed': len(url_dict),
            'duration_sec': round(time.time() - start, 2),
            'errors': [str(e)]
        }


async def run_belaes() -> dict:
    """Асинхронная обёртка для БелАЭС"""
    logger.info(f"[{len([p for p in ENABLED_PROCESSES if 'belaes' in p[1].lower()]) + 1}/{len(ENABLED_PROCESSES)}] Запуск БелАЭС...")
    start = time.time()

    try:
        belaes = BelaesProcess()
        result = await asyncio.to_thread(belaes.run)
        duration = time.time() - start
        result['duration_sec'] = round(duration, 2)
        logger.info(f"✅ БелАЭС завершён за {duration:.1f} сек")
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка выполнения БелАЭС: {e}", exc_info=True)
        return {
            'success': False,
            'all_success': False,
            'duration_sec': round(time.time() - start, 2),
            'errors': [str(e)]
        }

async def main_async() -> int:
    """Основная асинхронная функция"""
    start_time = datetime.now()
    logger.info("\n" + "=" * 70)
    logger.info(f"🚀 Запуск системы сбора данных: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    # Динамическое формирование задач на основе списка ENABLED_PROCESSES
    tasks = []
    for idx, (name, func_name) in enumerate(ENABLED_PROCESSES, 1):
        func = globals()[func_name]
        # Обновляем номер этапа в логах для корректной нумерации
        tasks.append((name, func()))

    # Запуск всех активных процессов параллельно
    if tasks:
        results = await asyncio.gather(*[task for _, task in tasks], return_exceptions=False)
        process_results = dict(zip([name for name, _ in tasks], results))
    else:
        process_results = {}
        logger.warning("⚠️  Нет активных процессов для запуска")

    # Итоги
    total_duration = (datetime.now() - start_time).total_seconds()
    all_success = True
    errors_summary = []

    logger.info("\n" + "=" * 70)
    logger.info("🏁 ИТОГИ РАБОТЫ")
    logger.info("=" * 70)

    for name, result in process_results.items():
        success = result.get('all_success', False) or result.get('success', False)
        duration = result.get('duration_sec', 0)
        logger.info(f"{name:15}: {'✅ УСПЕШНО' if success else '❌ ОШИБКА'} ({duration:.1f} сек)")

        if not success:
            all_success = False
            for err in result.get('errors', []):
                errors_summary.append(f"  • {name}: {err}")

    logger.info(f"Общее время:   {total_duration:.1f} сек")
    logger.info("=" * 70)

    if not all_success:
        logger.warning("\n⚠️  Некоторые процессы завершились с ошибками:")
        for err in errors_summary:
            logger.warning(err)

    return 0 if all_success else 1


def main() -> int:
    """Точка входа"""
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("\n⚠️  Выполнение прервано пользователем")
        return 130
    except Exception as e:
        logger.error(f"❌ Критическая ошибка выполнения: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())