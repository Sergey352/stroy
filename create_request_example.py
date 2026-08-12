#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пример создания заявки на материалы через POST /tasks, где каждое поле
формы указывается через его "code" (а не id или name) — так, как просил
пользователь, и как это сделано во всех остальных скриптах набора.

Функция create_material_request(...) — переиспользуемая: вызывайте её
из вашей интеграции (1С, Telegram-бот прораба и т.п.), передавая уже
готовые значения.

Запуск примера:
    export PYRUS_LOGIN="pyrus_demo@outlook.com"
    export PYRUS_SECURITY_KEY="ваш_секретный_ключ"
    python3 create_request_example.py
"""

from pyrus_client import PyrusClient, FORM_ID


def create_material_request(
    client,
    object_name,             # code: object            — название объекта из справочника «Объекты стройки»
    area,                    # code: area               — участок/этаж/зона (текст)
    material_category_name,  # code: material_category  — категория из справочника «Категории материалов»
    items,                   # code: items_table        — список строк таблицы позиций, см. ниже
    item_description,        # code: item_description   — номенклатура/описание
    unit,                    # code: unit               — единица измерения (multiple_choice: "шт." | "литр" | "кг.")
    quantity,                # code: quantity           — количество
    delivery_deadline,       # code: delivery_deadline  — срок поставки, "YYYY-MM-DD" (тип поля date, без времени)
    priority_choice,         # code: priority           — "Срочный" или "Плановый"
    responsible_email=None,  # ответственный (снабженец) — необязательно, если назначение уже в маршруте формы
):
    """
    items — список словарей вида:
        {"name": "Цемент М500", "unit": "меш.", "qty": 40, "in_budget": True}
    Ячейки таблицы (items_table) в этом примере используют числовые id
    колонок, т.к. у столбцов вложенной таблицы код (code) в конструкторе
    Pyrus обычно не задаётся отдельно — их идентификация всё равно идёт
    через родительское поле items_table по code. Если вы проставили code
    и самим колонкам таблицы — замените "id" на "code" в cells ниже.
    """

    table_rows = []
    for i, row in enumerate(items):
        table_rows.append({
            "row_id": i,
            "cells": [
                {"id": row["col_name_id"], "value": row["name"]},
                {"id": row["col_unit_id"], "value": row["unit"]},
                {"id": row["col_qty_id"], "value": row["qty"]},
            ],
        })

    # multiple_choice принимает только choice_ids (числа), choice_names
    # API не поддерживает — находим id варианта по его тексту в опциях поля.
    def resolve_choice(field_code, choice_value):
        choice_id = client.find_choice_id(field_code, choice_value)
        if choice_id is None:
            raise ValueError(
                f"Вариант «{choice_value}» не найден в опциях поля {field_code} — "
                f"проверьте написание (сверьте с конструктором формы)"
            )
        return choice_id

    priority_choice_id = resolve_choice("priority", priority_choice)
    unit_choice_id = resolve_choice("unit", unit)

    fields = [
        {"code": "object", "value": {"item_name": object_name}},
        {"code": "area", "value": area},
        {"code": "material_category", "value": {"item_name": material_category_name}},
        {"code": "item_description", "value": item_description},
        {"code": "unit", "value": {"choice_ids": [unit_choice_id]}},
        {"code": "quantity", "value": quantity},
        {"code": "delivery_deadline", "value": delivery_deadline},
        {"code": "priority", "value": {"choice_ids": [priority_choice_id]}},
    ]

    if table_rows:
        fields.append({"code": "items_table", "value": table_rows})

    payload = {"form_id": FORM_ID, "fields": fields}
    if responsible_email:
        payload["responsible"] = {"email": responsible_email}

    return client.post("tasks", payload)


if __name__ == "__main__":
    client = PyrusClient()

    result = create_material_request(
        client,
        object_name="ЖК «Северный», корп. 1",
        area="3 этаж, секция Б",
        material_category_name="Бетон и растворы",
        items=[],  # заполните при необходимости с id колонок таблицы вашей формы
        item_description="Цемент М500, мешок 50 кг",
        unit="шт.",  # поле unit — multiple_choice с фиксированными вариантами: "шт." | "литр" | "кг."
        quantity=40,
        delivery_deadline="2026-08-20",
        priority_choice="Плановый",
    )

    print("Создана задача:", result["task"]["id"])
