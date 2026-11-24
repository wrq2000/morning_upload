# Обновление сервера

Подтверждён запуск: gpu-server, пользователь vologzhaninov_as, каталог
/home/vologzhaninov_as/projects/morning_upload, cron ежедневно в 08:00 МСК.
Cron вызывает run.sh, который запускает одноразовый контейнер morning_upload:latest.
VPN и отправка через Яндекс/Google проверены пользователем.

## Подготовить рабочий кандидат

Загрузите содержимое обновлённого архива через SFTP в существующий каталог
/home/vologzhaninov_as/projects/morning_upload_update. Сохраните его рабочий
.env с реквизитами и прежний постоянный output рабочего проекта.
Удалённые из архива файлы могли остаться после копирования поверх старой папки:

```bash


cd /home/vologzhaninov_as/projects/morning_upload_update

rm -f processes/ueex/preflight.py deployment/server_audit.py test_unicode.sh


```

В .env оставьте UEEX_RECHECK_DAYS=14. Удалите старые строки
UEEX_FULL_RESCAN_DAYS, UEEX_FORCE_FULL_SCAN и UEEX_EXTRA_RESULT_URLS, если есть.
Они больше не используются. Все действующие реквизиты сохраняются.
Состав задач задаётся только списком ENABLED_PROCESSES в main.py.
Удалите прежние MORNING_PROCESSES и UEEX_ENABLED из .env, если они есть:
они больше не управляют запуском. По умолчанию явно включены БелАЭС,
Tuz Gölü, EPİAŞ и УЭБ. Для отдельного прогона УЭБ закомментируйте остальные
строки списка до сборки образа. Для ежедневной общей рассылки включите
нужные процессы в этом же списке и пересоберите образ ещё раз.

## Собрать и запустить выбранные процессы

Dockerfile и зависимости не менялись; установка пакетов использует кэш.

```bash


cd /home/vologzhaninov_as/projects/morning_upload_update

docker build --progress=plain -t morning_upload:ueex-candidate .

MORNING_IMAGE=morning_upload:ueex-candidate \
MORNING_OUTPUT_DIR=/home/vologzhaninov_as/projects/morning_upload/output \
bash ./run.sh


```

Это обычный рабочий прогон с сохранением CSV и отправкой письма. Ожидается
EXIT_CODE=0. По сайту запрашиваются только последние 14 дней, старые результаты
остаются из output. Обычный повтор неизменившегося отчёта в тот же день
может быть пропущен журналом рассылки. Диагностических команд в приложении нет.

## Сохранить резервную копию и переключить образ

Выполняется после успешного прогона, когда прежний cron-запуск завершён.

```bash


docker ps --filter ancestor=morning_upload:latest

morning_stamp=$(date +%Y%m%d_%H%M%S)
morning_backup="/home/vologzhaninov_as/projects/morning_upload_backups/$morning_stamp"
umask 077
mkdir -p "$morning_backup"

crontab -l > "$morning_backup/crontab.txt"

tar --exclude='./logs' --exclude='./state' --exclude='./output' \
  --exclude='*/state' --exclude='./venv' --exclude='./.venv' \
  --exclude='./.git' --exclude='*/__pycache__' \
  -czf "$morning_backup/source-before.tar.gz" \
  -C /home/vologzhaninov_as/projects/morning_upload .

docker image tag morning_upload:latest "morning_upload:backup-$morning_stamp"
printf '%s\n' "morning_upload:backup-$morning_stamp" > "$morning_backup/image-before.txt"
printf '%s\n' "$morning_backup"


```

Резервная копия включает .env и остаётся на сервере с правами владельца.
Скопируйте подготовленные файлы, сохранив runtime-каталоги:

```bash


python3 - <<'PYCODE'
from pathlib import Path
import shutil
source = Path('/home/vologzhaninov_as/projects/morning_upload_update')
target = Path('/home/vologzhaninov_as/projects/morning_upload')
skip = {'logs', 'state', 'output', 'venv', '.venv', '.git', '__pycache__'}
for path in source.rglob('*'):
    relative = path.relative_to(source)
    if any(part in skip for part in relative.parts):
        continue
    if not path.is_file() or path.is_symlink() or path.suffix in {'.log', '.pyc'}:
        continue
    destination = target / relative
    if target.resolve() not in destination.resolve().parents:
        raise RuntimeError('Путь вышел за пределы проекта: ' + str(relative))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(path), str(destination))
for relative in ('processes/ueex/preflight.py', 'deployment/server_audit.py', 'test_unicode.sh'):
    obsolete = target / relative
    if obsolete.exists():
        obsolete.unlink()
print('Файлы обновлены; runtime сохранён')
PYCODE

chmod +x /home/vologzhaninov_as/projects/morning_upload/run.sh

docker image tag morning_upload:ueex-candidate morning_upload:latest


```

## Существующий cron

Через crontab -e сохраните запуск по прежнему расписанию и пути:

```cron
0 08 * * * /home/vologzhaninov_as/projects/morning_upload/run.sh >> /tmp/morning_upload.log 2>&1
```

run.sh сам сохраняет и выводит точный EXIT_CODE Docker. Прежний хвост с
echo и $(date) не нужен. Остальные задания crontab сохраните.
Следующий cron-запуск использует новый образ тем же способом.

## Откат

После завершения активного запуска используйте напечатанный путь копии:

```bash


morning_backup=/home/vologzhaninov_as/projects/morning_upload_backups/НУЖНАЯ_МЕТКА

docker image tag "$(cat "$morning_backup/image-before.txt")" morning_upload:latest

tar -xzf "$morning_backup/source-before.tar.gz" \
  -C /home/vologzhaninov_as/projects/morning_upload


```

Каталоги output/state/logs при откате сохраняются. Старые dangling-образы
не требуется удалять в ходе обновления.
