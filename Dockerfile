# ===== БАЗОВЫЙ ОБРАЗ =====
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

# ===== ШАГ 1: Настройка репозиториев (меняется крайне редко) =====
# Вынесено в отдельный слой — кэшируется, пока не изменится базовый образ
# Поддерживаются и sources.list, и формат DEB822 (*.sources) Ubuntu Noble.
RUN set -eu; for apt_source in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do \
      if [ -f "$apt_source" ]; then \
        sed -E -i 's#https?://(archive|security)\.ubuntu\.com/ubuntu#http://mirror.yandex.ru/ubuntu#g' "$apt_source"; \
      fi; \
    done

# ===== ШАГ 2: Системные зависимости — локаль (лёгкий слой) =====
# Кэшируется отдельно от тяжёлых пакетов вроде tesseract
RUN apt-get update --allow-releaseinfo-change && \
    apt-get install -y --no-install-recommends locales openssh-client && \
    locale-gen en_US.UTF-8 ru_RU.UTF-8 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ===== ШАГ 3: Системные зависимости — tesseract (тяжёлый, но стабильный слой) =====
# Если список пакетов не меняется — этот слой не пересобирается
RUN apt-get update --allow-releaseinfo-change && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libtesseract-dev \
        libleptonica-dev \
        tesseract-ocr-eng \
        tesseract-ocr-tur \
        tesseract-ocr-rus && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ===== ШАГ 4: Переменные окружения =====
# Не влияют на кэш, но важны для работы приложения
ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    NO_COLOR=1

# ===== ШАГ 5: Python-зависимости (ключевой слой для кэша!) =====
# Копируем ТОЛЬКО requirements.txt — при изменении кода этот слой НЕ инвалидируется
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ===== ШАГ 6: Установка браузеров Playwright =====
# Зависит от установленных Python-пакетов, поэтому после pip install
RUN playwright install chromium

# ===== ШАГ 7: Исходный код приложения (меняется чаще всего — должен быть последним) =====
# При изменении кода пересобирается только этот слой и CMD
COPY . .

# ===== ЗАПУСК =====
CMD ["python", "main.py"]
