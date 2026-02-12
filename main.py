"""
Точка входа для запуска всех процессов сбора данных
"""
import random
import sys
import asyncio
import time
from datetime import datetime
from dotenv import load_dotenv
from common.logfile import setup_logging
from processes.argus import ArgusProcess
from processes.desfa_lng_plan import DesfaUnloadingPlanProcess
from processes.epias import EpiasProcess, processing_config, url_dict
import logging
# Настройка логирования (ТОЛЬКО ОДИН РАЗ!) и загрузка переменных окружения
setup_logging()  # Сохраняем логгер для использования в этом модуле
logger = logging.getLogger(__name__)

async def run_argus() -> dict:
    """Асинхронная обёртка для Argus с случайной задержкой (0-600 сек)"""
    # Генерируем случайную задержку от 0 до 600 секунд
    delay = random.uniform(0, 600)
    logger.info(f"[1/3] Запуск Argus Media с задержкой {delay:.1f} секунд...")

    # Асинхронное ожидание (не блокирует другие задачи)
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


async def run_desfa() -> dict:
    """Асинхронная обёртка для DESFA"""
    logger.info("[2/3] Запуск DESFA...")
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
    logger.info("[3/3] Запуск EPİAŞ...")
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


async def main_async() -> int:
    """Основная асинхронная функция"""
    start_time = datetime.now()
    logger.info("\n" + "=" * 70)
    logger.info(f"🚀 Запуск системы сбора данных: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    # Запуск всех процессов параллельно
    results = await asyncio.gather(
        run_argus(),
        run_desfa(),
        # run_epias(),
        return_exceptions=False
    )

    argus_result, desfa_result = results

    # Итоги
    total_duration = (datetime.now() - start_time).total_seconds()

    # Определение общего успеха с учётом различий в структуре результатов
    argus_success = argus_result.get('all_success', False) or argus_result.get('success', False)
    desfa_success = desfa_result.get('success', False)
    # epias_success = epias_result.get('success', False)
    all_success = argus_success and desfa_success #and epias_success

    logger.info("\n" + "=" * 70)
    logger.info("🏁 ИТОГИ РАБОТЫ")
    logger.info("=" * 70)
    logger.info(f"Argus Media:   {'✅ УСПЕШНО' if argus_success else '❌ ОШИБКА'} "
                f"({argus_result.get('duration_sec', 0):.1f} сек)")
    logger.info(f"DESFA:         {'✅ УСПЕШНО' if desfa_success else '❌ ОШИБКА'} "
                f"({desfa_result.get('duration_sec', 0):.1f} сек)")
    # logger.info(f"EPİAŞ:         {'✅ УСПЕШНО' if epias_success else '❌ ОШИБКА'} "
    #             f"({epias_result.get('duration_sec', 0):.1f} сек)")
    logger.info(f"Общее время:   {total_duration:.1f} сек")
    logger.info("=" * 70)

    if not all_success:
        logger.warning("\n⚠️  Некоторые процессы завершились с ошибками:")
        if not argus_success:
            for err in argus_result.get('errors', []):
                logger.warning(f"  • Argus: {err}")
        if not desfa_success:
            for err in desfa_result.get('errors', []):
                logger.warning(f"  • DESFA: {err}")
        # if not epias_success:
        #     for err in epias_result.get('errors', []):
        #         logger.warning(f"  • EPİAŞ: {err}")

    return 0 if all_success else 1


def main() -> int:
    """Точка входа"""
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        if 'logger' in locals():
            logger.info("\n⚠️  Выполнение прервано пользователем")
        else:
            print("\n⚠️  Выполнение прервано пользователем")
        return 130  # Код выхода для KeyboardInterrupt
    except Exception as e:
        if 'logger' in locals():
            logger.error(f"❌ Критическая ошибка выполнения: {e}", exc_info=True)
        else:
            print(f"❌ Критическая ошибка выполнения: {e}")
        return 1


if __name__ == "__main__":
    load_dotenv()
    sys.exit(main())