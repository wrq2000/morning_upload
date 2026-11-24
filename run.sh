#!/bin/bash
set -eo pipefail

# Фиксируем директорию скрипта.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Используем локаль хоста; контейнер сохраняет собственную UTF-8 локаль.
export LANG=ru_RU.utf8
export LC_ALL=ru_RU.utf8
export TZ=Europe/Moscow
cd "$SCRIPT_DIR"

morning_output_dir="${MORNING_OUTPUT_DIR:-$SCRIPT_DIR/output}"
mkdir -p "$SCRIPT_DIR/logs" "$SCRIPT_DIR/state" "$morning_output_dir/ueex"

# Для проверки кандидата можно задать MORNING_IMAGE, не меняя cron и latest.
morning_image="${MORNING_IMAGE:-morning_upload:latest}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Запуск: $morning_image"

if docker run --rm \
  --env TZ=Europe/Moscow \
  --env-file .env \
  -v "$HOME/.ssh:/root/.ssh:ro" \
  -v "$SCRIPT_DIR/state:/app/state" \
  -v "$SCRIPT_DIR/logs:/app/logs" \
  -v "$morning_output_dir:/app/output" \
  "$morning_image"; then
  morning_status=0
else
  morning_status=$?
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Завершено; EXIT_CODE=$morning_status"
echo "Логи: $SCRIPT_DIR/logs/; данные УЭБ: $morning_output_dir/ueex/"
exit "$morning_status"
