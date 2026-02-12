# Образ с Python 3.12 (Ubuntu 22.04)
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

COPY requirements.txt .
# Устанавливаем без --no-cache-dir для отладки (потом можно вернуть)
RUN pip install -r requirements.txt

COPY . .

CMD ["python", "main.py"]