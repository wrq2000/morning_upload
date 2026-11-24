"""Промежуточный прогресс незавершённого запуска. Python 3.8+."""
import copy
import json
import logging
import time
from pathlib import Path

import pandas as pd

from .storage import write_text_atomic

logger = logging.getLogger(__name__)


class RunCheckpoint:
    """Хранит только успешно загруженные и проверенные источники текущего прохода.

    После успешной публикации checkpoint удаляется. Результаты завершённых
    запусков не подменяют новые ежедневные загрузки. Контекст и срок хранения
    не позволяют использовать другой период или слишком старые ответы.
    """

    def __init__(self, path, context, enabled=True, max_age=21600):
        self.path = Path(path)
        self.context = context
        self.enabled = enabled
        self.max_age = max_age
        self.data = {"version": 1, "context": context, "sources": {}}
        self.hits = 0
        if enabled and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if (data.get("version") == 1 and data.get("context") == context
                        and isinstance(data.get("sources"), dict)):
                    self.data = data
            except (OSError, ValueError, AttributeError):
                logger.warning("Промежуточный файл не прочитан; источники будут загружены заново: %s", self.path)

    async def fetch(self, gateway, stage, key):
        entries = self.data["sources"].setdefault(stage, {})
        entry = entries.get(key) if self.enabled else None
        if (isinstance(entry, dict) and 0 <= time.time() - entry.get("saved_at", 0) <= self.max_age):
            payload = copy.deepcopy(entry["payload"])
            frame = pd.DataFrame(payload["records"], columns=payload["columns"], dtype=object)
            frame.attrs.update(payload["attrs"])
            self.hits += 1
            logger.info("%s: из промежуточного файла, без HTTP-запроса", gateway.name)
            return frame
        # При ошибке fetch запись не создаётся: 429 и другие неуспешные
        # ответы никогда не сохраняются как успешно загруженные данные.
        frame = await gateway.fetch()
        if self.enabled:
            entries[key] = {"saved_at": time.time(), "payload": {
                "columns": list(frame.columns), "records": frame.to_dict("records"),
                "attrs": frame.attrs,
            }}
            write_text_atomic(self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")
        return frame

    def discard(self, stage, key):
        """Ответ, не прошедший последующую обработку, надо запросить заново."""
        entries = self.data["sources"].get(stage, {})
        if self.enabled and key in entries:
            del entries[key]
            write_text_atomic(self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")

    def complete(self):
        if self.enabled and self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                logger.warning("Не удалось удалить завершённый промежуточный файл: %s", self.path)
