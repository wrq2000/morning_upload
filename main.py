"""
Точка входа для запуска всех процессов сбора данных
"""
import logging
from pathlib import Path
from dotenv import load_dotenv

# .env рядом с main.py; Docker-переменные имеют приоритет.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
from common.logfile import setup_logging

# Настройка логирования (ТОЛЬКО ОДИН РАЗ!) и загрузка переменных окружения
setup_logging()
logger = logging.getLogger(__name__)
import random
import sys
import asyncio
import time
from datetime import datetime


# === УПРАВЛЕНИЕ ПРОЦЕССАМИ: для отключения достаточно закомментировать строку ===
ENABLED_PROCESSES = [
    # ("Argus Media", "run_argus"),
    # ("DESFA", "run_desfa"),
    ("БелАЭС", "run_belaes"),
    # ("Tuz Gölü", "run_tuzgolu"),
    # ("EPİAŞ", "run_epias"),
    ("УЭБ", "run_ueex"),
    ("СПбМТСБ", "run_spimex"),
    ("Цхинвал", "run_tskhinval"),
    ("Eurostat", "run_eurostat"),
    ("Вывозные пошлины", "run_export_duty"),
    ("ENTSO-E", "run_entsoe_flows"),
    ("ChinaMoney", "run_chinamoney"),
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
        from processes.argus import ArgusProcess
        argus = ArgusProcess()
        result = await asyncio.to_thread(argus.run)
        duration = time.time() - start
        result['duration_sec'] = round(duration, 2)
        logger.info(f"✅ Argus Media завершён за {duration:.1f} сек")
        return result
    except Exception as e:
        logger.warning(f"❌ Ошибка выполнения Argus: {e}", exc_info=True)
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
        from processes.tuzgolu import TuzGoluFixedTemplateParser
        tuzgolu = TuzGoluFixedTemplateParser()
        result = await asyncio.to_thread(tuzgolu.run)
        duration = time.time() - start
        result['duration_sec'] = round(duration, 2)
        logger.info(f"✅ Tuz Gölü завершён за {duration:.1f} сек")
        return result
    except Exception as e:
        logger.warning(f"❌ Ошибка выполнения Tuz Gölü: {e}", exc_info=True)
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
        from processes.desfa_lng_plan import DesfaUnloadingPlanProcess
        desfa = DesfaUnloadingPlanProcess()
        result = await asyncio.to_thread(desfa.run)
        duration = time.time() - start
        result['duration_sec'] = round(duration, 2)
        logger.info(f"✅ DESFA завершён за {duration:.1f} сек")
        return result
    except Exception as e:
        logger.warning(f"❌ Ошибка выполнения DESFA: {e}", exc_info=True)
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
        from processes.epias import EpiasProcess, processing_config, url_dict
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
        logger.warning(f"❌ Ошибка выполнения EPİAŞ: {e}", exc_info=True)
        return {
            'success': False,
            'sources_processed': 0,
            'sources_failed': len(locals().get('url_dict', {})),
            'duration_sec': round(time.time() - start, 2),
            'errors': [str(e)]
        }


async def run_belaes() -> dict:
    """Асинхронная обёртка для БелАЭС"""
    logger.info(f"[{len([p for p in ENABLED_PROCESSES if 'belaes' in p[1].lower()]) + 1}/{len(ENABLED_PROCESSES)}] Запуск БелАЭС...")
    start = time.time()

    try:
        from processes.belaes import BelaesProcess
        belaes = BelaesProcess()
        result = await asyncio.to_thread(belaes.run)
        duration = time.time() - start
        result['duration_sec'] = round(duration, 2)
        logger.info(f"✅ БелАЭС завершён за {duration:.1f} сек")
        return result
    except Exception as e:
        logger.warning(f"❌ Ошибка выполнения БелАЭС: {e}", exc_info=True)
        return {
            'success': False,
            'all_success': False,
            'duration_sec': round(time.time() - start, 2),
            'errors': [str(e)]
        }

async def run_ueex() -> dict:
    """Независимая задача УЭБ: CSV и отправка через основной/резервный SMTP."""
    start = time.time()
    try:
        from processes.ueex import UeexProcess
        result = await asyncio.to_thread(UeexProcess().run)
        result["duration_sec"] = round(time.time() - start, 2)
        return result
    except Exception as exc:
        logger.warning("Ошибка процесса УЭБ")
        return {"success": False, "duration_sec": round(time.time() - start, 2), "errors": [str(exc)]}


async def run_spimex() -> dict:
    """Текстовая выгрузка СПбМТСБ и ежедневное письмо с последними 30 строками."""
    try:
        from processes.spimex import SpimexProcess
        return await asyncio.to_thread(lambda: SpimexProcess().run())
    except Exception as exc:
        logger.warning("Ошибка процесса СПбМТСБ")
        return {"success": False, "errors": [str(exc)]}


async def run_tskhinval() -> dict:
    """Среднесуточная температура Цхинвала, CSV и ежедневная отправка."""
    try:
        from processes.tskhinval import TskhinvalProcess
        return await asyncio.to_thread(lambda: TskhinvalProcess().run())
    except Exception as exc:
        logger.warning("Ошибка процесса температуры Цхинвала")
        return {"success": False, "errors": [str(exc)]}


async def run_eurostat() -> dict:
    """Месячный газовый баланс, повторные попытки до успешного завершения цикла."""
    try:
        from processes.eurostat import EurostatGasProcess
        return await asyncio.to_thread(lambda: EurostatGasProcess().run())
    except Exception as exc:
        logger.warning("Ошибка процесса Eurostat")
        return {"success": False, "errors": [str(exc)]}


async def run_export_duty() -> dict:
    """Ежемесячные ставки на нефть и СУГ; повтор до появления документа."""
    try:
        from processes.export_duty import ExportDutyProcess
        return await asyncio.to_thread(lambda: ExportDutyProcess().run())
    except Exception as exc:
        logger.warning("Ошибка процесса вывозных пошлин", exc_info=True)
        return {"success": False, "errors": [str(exc)]}


async def run_chinamoney() -> dict:
    """Ежедневный официальный курс USD/CNY."""
    try:
        from processes.chinamoney import ChinaMoneyProcess
        return await asyncio.to_thread(lambda: ChinaMoneyProcess().run())
    except Exception as exc:
        logger.warning("Ошибка процесса ChinaMoney", exc_info=True)
        return {"success": False, "errors": [str(exc)]}


async def run_entsoe_flows() -> dict:
    """Ежедневные физические перетоки ENTSO-E за завершённые сутки."""
    try:
        from processes.entsoe_flows import EntsoeFlowsProcess
        return await asyncio.to_thread(lambda: EntsoeFlowsProcess().run())
    except Exception as exc:
        logger.warning("Ошибка процесса ENTSO-E", exc_info=True)
        return {"success": False, "errors": [str(exc)]}


async def main_async() -> int:
    """Основная асинхронная функция"""
    start_time = datetime.now()
    logger.info("\n" + "=" * 70)
    logger.info(f"🚀 Запуск системы сбора данных: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    # Изоляция источников действует даже при необработанном исключении в обёртке.
    from common.process_runner import run_processes
    process_results = await run_processes(ENABLED_PROCESSES, globals())
    if not process_results:
        logger.warning("⚠️  Нет активных процессов для запуска")

    # Итоги
    total_duration = (datetime.now() - start_time).total_seconds()
    all_success = True
    errors_summary = []

    logger.info("\n" + "=" * 70)
    logger.info("🏁 ИТОГИ РАБОТЫ")
    logger.info("=" * 70)

    for name, result in process_results.items():
        success = bool(result.get('all_success', result.get('success', False)))
        duration = result.get('duration_sec', 0)
        status = '✅ УСПЕШНО' if success else '⚠️ ПРЕДУПРЕЖДЕНИЕ'
        if result.get('partial'):
            status = '⚠️ ЧАСТИЧНО'
        if success and result.get('skipped'):
            status = 'ПРОПУЩЕНО'
        logger.info(f"{name:15}: {status} ({duration:.1f} сек)")

        if not success:
            all_success = False
            for err in result.get('errors', []):
                errors_summary.append(f"  • {name}: {err}")

    logger.info(f"Общее время:   {total_duration:.1f} сек")
    logger.info("=" * 70)

    if not all_success:
        logger.warning("\n⚠️  Завершено с предупреждениями по отдельным источникам:")
        for err in errors_summary:
            logger.warning(err)

    # Обработанные сбои источников отражаются в WARNING и результатах задач.
    # Завершённый общий проход всегда возвращает 0.
    return 0


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
