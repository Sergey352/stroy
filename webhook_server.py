#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Вебхук-бот «Остаток по позициям сметы» — замена src/index.js (Cloudflare
Workers) в сессии 2026-08-18. Переехали на свой VDS пользователя вместо
Cloudflare: та же самая логика, что и в smeta_remainder_bot.py (запускаем
её прямо отсюда через import, а не переписываем на JS второй раз — раньше
на Cloudflare код приходилось дублировать на JS, потому что Workers — не
Python и не Node, а V8-изоляты; на своём сервере такого ограничения нет,
поэтому логика расчёта остатка теперь только в ОДНОМ месте:
smeta_remainder_bot.py, и .py, и вебхук здесь его просто вызывают).

ЧТО ДЕЛАЕТ:
  Pyrus присылает сюда HTTP POST при каждом новом «ожидающем действии» для
  бота-участника формы (создание заявки; правки уже созданной заявки вебхук
  повторно НЕ шлёт — этот пробел закрывает крон, см. ниже). Мы:
    1. Проверяем подпись запроса (HMAC-SHA1 от тела запроса секретом бота
       PYRUS_WEBHOOK_SECRET, приходит в заголовке X-Pyrus-Sig) — без этого
       кто угодно мог бы слать сюда поддельные запросы.
    2. Запускаем ПОЛНЫЙ пересчёт остатка по всем позициям сметы (функция
       sweep() из smeta_remainder_bot.py) — не только по этой задаче,
       потому что остаток зависит от суммы по ВСЕМ активным заявкам, а не
       только от той, что вызвала вебхук.
    3. Отвечаем Pyrus {"approval_choice": "approved"} — ОБЯЗАТЕЛЬНО, даже
       если расчёт упал с ошибкой: бот в маршруте формы — участник группы
       согласования шага 1 вместе с ролью Строй_Прораб, группа ждёт ответа
       ОТ ВСЕХ участников. Если не ответить approved — заявка застрянет на
       шаге 1 навсегда, даже когда прораб её согласует (это уже случалось
       и было исправлено на Cloudflare-версии, см. CLAUDE.md, история
       сессии 2026-08-12 — тот же риск актуален и здесь).

КРОН (обновления УЖЕ созданных заявок, которые вебхук не ловит):
  Отдельный процесс (см. docker-compose.yml, сервис "cron") просто вызывает
  smeta_remainder_bot.py по расписанию — на своём сервере это обычный
  цикл с sleep, Cloudflare Cron Trigger больше не нужен.

СТРАНИЦА ЗАГРУЗКИ СМЕТЫ (/upload, добавлено в сессии 2026-08-18):
  Тот же самый Flask-процесс (тот же контейнер, тот же порт) обслуживает
  ещё и простую HTML-страницу для снабженца/сметчика — залить .xlsx со
  сметой объекта, не открывая терминал. Логика разбора и загрузки та же,
  что и в CLI-версии (smeta_catalog_import.py, функция run_import) —
  никакого дублирования, страница просто вызывает ту же функцию и
  показывает результат в браузере вместо консоли. Доступ к /upload
  защищён Basic Auth на уровне Caddy (см. Caddyfile) — путь "/" (сам
  вебхук для Pyrus) без пароля, потому что Pyrus не умеет посылать
  Basic Auth, а подлинность запроса там и так проверяется подписью.

ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ (см. .env.example):
  PYRUS_LOGIN            — pyrus_demo@outlook.com
  PYRUS_SECURITY_KEY     — секретный ключ Pyrus API
  PYRUS_WEBHOOK_SECRET   — secret key бота (pyrus.com/t#bots), НЕ security_key API

ЗАПУСК ЛОКАЛЬНО (без Docker, для проверки):
    export PYRUS_LOGIN=... PYRUS_SECURITY_KEY=... PYRUS_WEBHOOK_SECRET=...
    python3 webhook_server.py
    # слушает на 0.0.0.0:8000
"""

import hashlib
import hmac
import logging
import os
import tempfile

from flask import Flask, request, jsonify, render_template
from werkzeug.utils import secure_filename

from smeta_catalog_import import run_import
from smeta_remainder_bot import sweep

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webhook")

app = Flask(__name__)
# Ограничение на размер загружаемого файла — 20 МБ с большим запасом
# (реальная смета «Акварель ОВВК» — меньше 200 КБ); защита от случайной
# заливки не-того файла или обрыва соединения посреди огромной загрузки.
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

WEBHOOK_SECRET = os.environ.get("PYRUS_WEBHOOK_SECRET", "")


def is_signature_valid(raw_body: bytes, signature_hex: str) -> bool:
    """Сверяет подпись запроса Pyrus. HMAC-SHA1 от тела запроса секретом
    бота, сравнение — hmac.compare_digest (защита от timing-атак, обычное
    сравнение строк вида a == b теоретически позволяет подобрать подпись
    по времени ответа)."""
    if not WEBHOOK_SECRET or not signature_hex:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, signature_hex.lower())


@app.route("/", methods=["POST"])
def pyrus_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Pyrus-Sig", "")

    if not is_signature_valid(raw_body, signature):
        # Здесь можно отвечать 403 — в отличие от ответа боту-участнику
        # шага (см. ниже), это ещё не дошло до реальной логики согласования,
        # просто отказ в обслуживании поддельного запроса.
        return jsonify({"error": "invalid signature"}), 403

    payload = request.get_json(silent=True) or {}
    task_id = payload.get("task_id") or (payload.get("task") or {}).get("id")

    try:
        sweep(dry_run=False)
    except Exception:
        # Намеренно не роняем вебхук ошибкой — см. докстринг модуля про то,
        # почему approval_choice: approved должен уйти в любом случае.
        logger.exception("sweep() упал при обработке вебхука для задачи %s", task_id)

    return jsonify({"approval_choice": "approved"}), 200


@app.route("/upload", methods=["GET"])
def upload_form():
    """Показывает пустую форму загрузки — без файла и без результата."""
    return render_template("upload.html", result=None, error=None)


@app.route("/upload", methods=["POST"])
def upload_submit():
    """Обрабатывает загруженный файл: сохраняет во временную папку (она
    удаляется автоматически по выходу из `with`, файл не остаётся на
    диске сервера), разбирает и загружает через run_import() —
    ту же функцию, что использует и smeta_catalog_import.py из
    командной строки.

    Кнопок на странице две — «Проверить» и «Загрузить в Pyrus» — обе
    отправляют один и тот же form, различаются только скрытым полем
    `mode` (dry_run/import), которое выставляет нажатая кнопка (см.
    templates/upload.html, атрибут value у <button>)."""
    uploaded = request.files.get("smeta_file")
    mode = request.form.get("mode", "dry_run")

    if not uploaded or uploaded.filename == "":
        return render_template("upload.html", result=None, error="Файл не выбран.")

    filename = secure_filename(uploaded.filename)
    if not filename.lower().endswith(".xlsx"):
        return render_template("upload.html", result=None, error="Нужен файл в формате .xlsx.")

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, filename)
        uploaded.save(path)
        try:
            summary = run_import(path, dry_run=(mode == "dry_run"))
        except Exception as e:
            logger.exception("Ошибка обработки загруженной сметы %s", filename)
            return render_template("upload.html", result=None, error=f"Ошибка при разборе файла: {e}")

    return render_template("upload.html", result=summary, error=None, filename=filename)


@app.route("/healthz", methods=["GET"])
def healthz():
    """Для проверки живости контейнера (docker healthcheck / Caddy) —
    ничего не считает, просто отвечает 200."""
    return "ok", 200


if __name__ == "__main__":
    # host=0.0.0.0 обязательно внутри Docker-контейнера — иначе сервер
    # слушает только localhost контейнера и снаружи недоступен.
    app.run(host="0.0.0.0", port=8000)
