#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Скрипт настройки Pyrus под ТЗ "Автоматизация снабжения в строительной компании".

ВАЖНО: Pyrus API v4 НЕ поддерживает создание/редактирование полей, шагов
и маршрутизации формы программным способом (нет методов POST/PUT для
структуры формы — см. https://pyrus.com/ru/help/api/forms). Поля формы,
шаги и правила маршрутизации создаются ТОЛЬКО вручную в конструкторе форм
Pyrus (веб-интерфейс). Кроме того, аккаунт-бот (технический пользователь,
работающий по security_key) не может входить в веб-интерфейс — см.
https://pyrus.com/ru/help/api/authorization.

Через API можно и нужно сделать программно:
  1. Создать справочники (catalogs), которые форма будет использовать
     (объекты, категории материалов, лимиты, поставщики) — методом PUT /catalogs.
  2. Прочитать текущую структуру формы (GET /forms/{id}), чтобы свериться
     с уже существующими полями перед ручной настройкой.
  3. В будущем — реализовать бота, который сверяет УПД со счётом и
     подсвечивает позиции (через задачи/комментарии/поля, POST /tasks/{id}/comments,
     обновление полей задачи).

Запуск:
    export PYRUS_LOGIN="pyrus_demo@outlook.com"
    export PYRUS_SECURITY_KEY="ваш_секретный_ключ"
    pip install requests
    python3 pyrus_setup.py

Секретный ключ НЕ хранится в этом файле — передавайте его через переменную
окружения, чтобы не оставлять его в открытом виде.
"""

import os
import sys
import json
import requests

FORM_ID = 2455896

LOGIN = os.environ.get("PYRUS_LOGIN", "pyrus_demo@outlook.com")
SECURITY_KEY = os.environ.get("PYRUS_SECURITY_KEY")

if not SECURITY_KEY:
    print("Ошибка: задайте переменную окружения PYRUS_SECURITY_KEY", file=sys.stderr)
    sys.exit(1)


def auth():
    resp = requests.post(
        "https://accounts.pyrus.com/api/v4/auth",
        json={"login": LOGIN, "security_key": SECURITY_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], data["api_url"]


def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def get_form(api_url, token, form_id):
    resp = requests.get(f"{api_url}forms/{form_id}", headers=headers(token), timeout=30)
    resp.raise_for_status()
    return resp.json()


def create_catalog(api_url, token, name, catalog_headers, items):
    resp = requests.put(
        f"{api_url}catalogs",
        headers=headers(token),
        json={"name": name, "catalog_headers": catalog_headers, "items": items},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


CATALOGS = [
    {
        "name": "Объекты стройки",
        "catalog_headers": ["Объект", "Адрес"],
        "items": [
            # {"values": ["ЖК Северный, корп. 1", "г. Москва, ул. Примерная, 1"]},
        ],
    },
    {
        "name": "Категории материалов",
        "catalog_headers": ["Категория"],
        "items": [
            {"values": ["Бетон и растворы"]},
            {"values": ["Арматура и металлопрокат"]},
            {"values": ["Электрика"]},
            {"values": ["Сантехника"]},
            {"values": ["Отделочные материалы"]},
            {"values": ["Инструмент и оснастка"]},
            {"values": ["Прочее"]},
        ],
    },
    {
        "name": "Лимиты по объектам",
        "catalog_headers": ["Объект", "Категория", "Лимит", "Период"],
        "items": [
            # {"values": ["ЖК Северный, корп. 1", "Бетон и растворы", "500000", "2026 Q3"]},
        ],
    },
    {
        "name": "Поставщики",
        "catalog_headers": ["Название", "Контакты", "Условия", "Рейтинг"],
        "items": [
            # {"values": ["ООО Стройснаб", "+7 900 000-00-00", "Отсрочка 14 дней", "5"]},
        ],
    },
]


def main():
    print("Авторизация в Pyrus...")
    token, api_url = auth()
    print(f"OK, api_url = {api_url}")

    print(f"\nТекущая структура формы {FORM_ID}:")
    form = get_form(api_url, token, FORM_ID)
    print(json.dumps(form, ensure_ascii=False, indent=2))

    print("\nСоздание справочников...")
    created = {}
    for cat in CATALOGS:
        result = create_catalog(api_url, token, cat["name"], cat["catalog_headers"], cat["items"])
        created[cat["name"]] = result["catalog_id"]
        print(f"  Справочник «{cat['name']}» создан, catalog_id = {result['catalog_id']}")

    print("\nГотово. Идентификаторы справочников (понадобятся при ручной настройке полей формы):")
    for name, cid in created.items():
        print(f"  {name}: {cid}")


if __name__ == "__main__":
    main()
