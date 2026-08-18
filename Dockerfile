# Один образ на оба сервиса (вебхук и крон, см. docker-compose.yml) — код
# и зависимости у них общие, отличается только команда запуска. Так проще
# поддерживать: обновили requirements.txt или код бота — пересобрался один
# образ, а не два по отдельности.

FROM python:3.12-slim

WORKDIR /app

# Сначала только requirements.txt — чтобы Docker кэшировал слой установки
# зависимостей и не переустанавливал их при каждом изменении .py-файлов
# (кэш инвалидируется только когда меняется сам requirements.txt).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# По умолчанию — вебхук через gunicorn (готовый к нагрузке WSGI-сервер,
# а не встроенный dev-сервер Flask, который для реальной работы не
# предназначен). Для сервиса "cron" в docker-compose.yml команда
# переопределяется на cron_loop.sh.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "webhook_server:app"]
