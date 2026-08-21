#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Общий клиент для скриптов формы «Заявка на материалы» (Pyrus, форма 2455896).

Используется всеми скриптами в этом наборе:
  - catalogs_seed.py
  - create_request_example.py
  - permissions_setup.py
  - verify_form_codes.py
  - budget_remainder_bot.py
  - overdue_reminder_bot.py
  - upd_reconciliation_bot.py

Требует переменные окружения:
  PYRUS_LOGIN           — логин (например, pyrus_demo@outlook.com)
  PYRUS_SECURITY_KEY    — секретный ключ из настроек аккаунта

Секретный ключ нигде не хранится в файлах скриптов — только в переменной окружения.
"""

import os
import sys
import requests

FORM_ID = 2455896


class PyrusClient:
    def __init__(self):
        self.login = os.environ.get("PYRUS_LOGIN", "pyrus_demo@outlook.com")
        self.security_key = os.environ.get("PYRUS_SECURITY_KEY")
        if not self.security_key:
            print("Ошибка: задайте переменную окружения PYRUS_SECURITY_KEY", file=sys.stderr)
            sys.exit(1)
        self.token = None
        self.api_url = None
        self._auth()

    def _auth(self):
        resp = requests.post(
            "https://accounts.pyrus.com/api/v4/auth",
            json={"login": self.login, "security_key": self.security_key},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access_token"]
        self.api_url = data["api_url"]

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def get(self, path, params=None):
        resp = requests.get(f"{self.api_url}{path}", headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def post(self, path, payload):
        resp = requests.post(f"{self.api_url}{path}", headers=self._headers(), json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def put(self, path, payload):
        resp = requests.put(f"{self.api_url}{path}", headers=self._headers(), json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def upload_file(self, file_bytes, filename):
        """Загружает файл через POST /files/upload (единственный метод API,
        который принимает multipart/form-data, а не JSON — поэтому не
        через self.post(), у него другой Content-Type). Возвращает guid,
        который потом передаётся в POST /tasks/{id}/comments в поле
        attachments — так к задаче прикрепляется файл, сгенерированный
        ботом (используется supplier_documents_bot.py для документов на
        поставщиков)."""
        resp = requests.post(
            f"{self.api_url}files/upload",
            headers={"Authorization": f"Bearer {self.token}"},  # Content-Type не указываем — requests сам поставит multipart с нужной границей
            files={"file": (filename, file_bytes)},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    # ---- Вспомогательные методы ----

    def get_form(self, form_id=FORM_ID):
        return self.get(f"forms/{form_id}")

    def get_register(self, form_id=FORM_ID, steps=None, include_archived=False):
        """Возвращает список задач по форме. Фильтрация по шагам (steps) —
        по номерам шагов, а не по коду поля (в API фильтр по значению поля
        работает только через числовой id поля, поэтому здесь мы намеренно
        получаем задачи без фильтра по полям и ищем нужные поля в ответе
        по коду (field['code']) на стороне скрипта — так во всех скриптах
        набора мы работаем с полями только по code, а не по id/name."""
        params = {}
        if steps:
            params["steps"] = ",".join(str(s) for s in steps)
        if include_archived:
            params["include_archived"] = "y"
        return self.get(f"forms/{form_id}/register", params=params)

    def find_catalog_id(self, name):
        catalogs = self.get("catalogs")["catalogs"]
        for c in catalogs:
            if c["name"] == name and not c.get("deleted"):
                return c["catalog_id"]
        return None

    def create_catalog(self, name, catalog_headers, items=None):
        """Создаёт новый справочник (PUT /catalogs). items — необязательный
        список начальных строк вида [{"values": [...]}]; можно передать
        пустой список и наполнить справочник отдельно через diff."""
        return self.put(
            "catalogs",
            {"name": name, "catalog_headers": catalog_headers, "items": items or []},
        )

    def find_or_create_catalog(self, name, catalog_headers):
        """Находит справочник по имени, а если его ещё нет — создаёт с
        заданными колонками. Возвращает catalog_id. Используется скриптами
        импорта, которые должны быть безопасны для повторного запуска —
        при первом запуске создают справочник сами, при последующих просто
        находят уже существующий."""
        catalog_id = self.find_catalog_id(name)
        if catalog_id:
            return catalog_id
        result = self.create_catalog(name, catalog_headers)
        return result["catalog_id"]

    def get_members(self):
        return self.get("members")["members"]

    def find_member_id(self, email):
        for m in self.get_members():
            if m.get("email", "").lower() == email.lower():
                return m["id"]
        return None

    def get_form_field_defs(self, form_id=FORM_ID):
        """Плоский список всех полей формы (включая вложенные колонки таблиц
        и поля внутри опций multiple_choice), как они приходят из
        GET /forms/{id}. У каждого поля код (code), если задан, лежит
        в field['info']['code']."""
        form = self.get_form(form_id)
        out = []
        collect_form_fields(form.get("fields", []), out)
        return out

    def find_field_def_by_code(self, code, form_id=FORM_ID):
        for f in self.get_form_field_defs(form_id):
            if (f.get("info") or {}).get("code") == code:
                return f
        return None

    def find_choice_id(self, field_code, choice_value, form_id=FORM_ID):
        """Находит числовой choice_id варианта поля типа multiple_choice
        по его тексту — при создании/обновлении задачи multiple_choice
        принимает только choice_ids, choice_names API не поддерживает."""
        field = self.find_field_def_by_code(field_code, form_id)
        if not field:
            return None
        for opt in (field.get("info") or {}).get("options", []):
            if opt.get("choice_value") == choice_value:
                return opt.get("choice_id")
        return None


def collect_form_fields(fields, out):
    """Рекурсивно собирает все поля формы (в т.ч. колонки таблиц и поля
    внутри опций) в плоский список out. Используется get_form_field_defs
    и напрямую в verify_form_codes.py."""
    for f in fields or []:
        out.append(f)
        info = f.get("info") or {}
        for key in ("columns", "fields"):
            if key in info:
                collect_form_fields(info[key], out)
        for opt in info.get("options", []):
            if "fields" in opt:
                collect_form_fields(opt["fields"], out)


def field_by_code(fields, code):
    """Найти значение поля задачи по его коду (а не по id/name).
    Код может быть как на верхнем уровне поля (f['code']), так и внутри
    f['info']['code'] — в определении формы (GET /forms/{id}) код лежит
    во втором варианте, а как именно отдаётся в задачах (GET /tasks/{id})
    проверяется эмпирически в create_request_example.py, поэтому здесь
    проверяем оба варианта для надёжности."""
    for f in fields or []:
        if f.get("code") == code or (f.get("info") or {}).get("code") == code:
            return f
    return None
