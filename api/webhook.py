#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Вебхук-бот «Остаток по смете» для Vercel (serverless).

Даёт мгновенный (секунды, а не 15 минут как у cron-версии в
../budget_remainder_bot.py) пересчёт budget_remainder сразу как только
задача формы попадает во входящие бота — то есть сразу после того, как
заявка сохранена.

Настройка на стороне Pyrus (делается один раз в вебе, вручную, API этого
не поддерживает):
  1. pyrus.com/t#bots → создать бота → скопировать сгенерированный Pyrus
     secret key (X-Pyrus-Sig подписывается им) и URL этого эндпоинта
     (https://<ваш-проект>.vercel.app/api/webhook) в настройки бота.
  2. В конструкторе формы 2455896 добавить этого бота участником шага 1
     «Заполнение заявки» — ВАЖНО: как наблюдателя/неблокирующего
     участника, если в конструкторе такой вариант есть, а не как
     обязательного согласующего — иначе сбой вебхука может застопорить
     реальную заявку. Уточните в UI конструктора, там это явно подписано.

Настройка на стороне Vercel (Project Settings → Environment Variables):
  PYRUS_LOGIN            — pyrus_demo@outlook.com
  PYRUS_SECURITY_KEY     — секретный ключ Pyrus API (тот же, что в GitHub Secrets)
  PYRUS_WEBHOOK_SECRET   — secret key бота из шага 1 выше (НЕ security_key API)

Файл специально самодостаточный (не импортирует pyrus_client.py /
budget_remainder_bot.py из корня репозитория) — раздельная сборка
Vercel-функций по умолчанию не подхватывает файлы вне api/, а тащить их
через vercel.json/includeFiles менее надёжно без возможности живого теста
на этой платформе. Логика расчёта продублирована из budget_remainder_bot.py
намеренно — при изменении формулы остатка поправьте оба места.
"""

import hashlib
import hmac
import json
import os

import requests
from flask import Flask, request

FORM_ID = 2455896
API_TIMEOUT = 25  # вебхук должен уложиться в 60 сек, оставляем запас

app = Flask(__name__)


def _pyrus_auth():
    resp = requests.post(
        "https://accounts.pyrus.com/api/v4/auth",
        json={
            "login": os.environ["PYRUS_LOGIN"],
            "security_key": os.environ["PYRUS_SECURITY_KEY"],
        },
        timeout=API_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], data["api_url"]


def _pyrus_get(api_url, token, path, params=None):
    resp = requests.get(
        f"{api_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=API_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _pyrus_post(api_url, token, path, payload):
    resp = requests.post(
        f"{api_url}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=API_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _field_by_code(fields, code):
    for f in fields or []:
        if f.get("code") == code or (f.get("info") or {}).get("code") == code:
            return f
    return None


def _obj_cat_qty(fields):
    obj_field = _field_by_code(fields, "object")
    cat_field = _field_by_code(fields, "material_category")
    qty_field = _field_by_code(fields, "quantity")
    if not obj_field or not cat_field or not qty_field:
        return None
    obj_value = (obj_field.get("value") or {}).get("values", [None])[0] if isinstance(obj_field.get("value"), dict) else None
    cat_value = (cat_field.get("value") or {}).get("values", [None])[0] if isinstance(cat_field.get("value"), dict) else None
    if not obj_value or not cat_value:
        return None
    try:
        qty = float(qty_field.get("value") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    return obj_value, cat_value, qty


def _get_limit(api_url, token, object_name, category_name):
    catalogs = _pyrus_get(api_url, token, "catalogs")["catalogs"]
    catalog_id = next(
        (c["catalog_id"] for c in catalogs if c["name"] == "Лимиты по объектам" and not c.get("deleted")),
        None,
    )
    if not catalog_id:
        return None
    catalog = _pyrus_get(api_url, token, f"catalogs/{catalog_id}")
    for item in catalog["items"]:
        # Порядок values: [Ключ, Категория, Лимит, Период, Объект (полное имя)] —
        # см. докстринг budget_remainder_bot.py про ограничение can_not_modify_first_column
        values = item["values"]
        if len(values) >= 5 and values[4] == object_name and values[1] == category_name:
            return float(values[2])
    return None


def _sum_used_budget(api_url, token, object_name, category_name, exclude_task_id):
    register = _pyrus_get(api_url, token, f"forms/{FORM_ID}/register", params={"include_archived": "n"})
    total = 0.0
    for task in register.get("tasks", []):
        if task["id"] == exclude_task_id:
            continue
        parsed = _obj_cat_qty(task.get("fields", []))
        if parsed and parsed[0] == object_name and parsed[1] == category_name:
            total += parsed[2]
    return total


def recalc_budget_remainder(task_id):
    token, api_url = _pyrus_auth()
    task = _pyrus_get(api_url, token, f"tasks/{task_id}")["task"]
    fields = task.get("fields", [])

    parsed = _obj_cat_qty(fields)
    if not parsed:
        return  # заявка ещё не заполнена настолько, чтобы считать остаток
    object_name, category_name, quantity = parsed

    limit = _get_limit(api_url, token, object_name, category_name)
    if limit is None:
        return

    used = _sum_used_budget(api_url, token, object_name, category_name, exclude_task_id=task_id)
    remainder = limit - used - quantity

    _pyrus_post(
        api_url, token, f"tasks/{task_id}/comments",
        {"field_updates": [{"code": "budget_remainder", "value": remainder}]},
    )


def _signature_is_valid(raw_body, signature_header):
    secret = os.environ.get("PYRUS_WEBHOOK_SECRET", "")
    if not secret or not signature_header:
        return False
    digest = hmac.new(secret.encode("utf-8"), msg=raw_body, digestmod=hashlib.sha1).hexdigest()
    return hmac.compare_digest(digest, signature_header.lower())


@app.route("/api/webhook", methods=["POST"])
@app.route("/", methods=["POST"])
def pyrus_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Pyrus-Sig", "")

    if not _signature_is_valid(raw_body, signature):
        return ("invalid signature", 403)

    payload = json.loads(raw_body.decode("utf-8"))
    task_id = payload.get("task_id") or (payload.get("task") or {}).get("id")
    if not task_id:
        return ("", 200)

    try:
        recalc_budget_remainder(task_id)
    except Exception as e:
        # Намеренно не роняем вебхук с ошибкой — если бот добавлен
        # обязательным участником шага, 4xx/5xx-ответ мог бы застопорить
        # реальную заявку из-за сбоя в нашем расчёте. Ошибку просто логируем.
        print(f"budget remainder webhook error for task {task_id}: {e}")

    return ("", 200)
