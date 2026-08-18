#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
УСТАРЕЛ (сессия 2026-08-18) — заменён на smeta_remainder_bot.py.

Эта версия считала остаток по паре (объект, категория материала) из
справочника «Лимиты по объектам». После перехода на модель «смета по
позициям» поля object/material_category/quantity/budget_remainder,
на которых завязана вся логика ниже, УДАЛЕНЫ с формы — скрипт больше не
делает ничего полезного (field_by_code просто не находит эти коды и тихо
пропускает все задачи), но и не ломается. Оставлен в репозитории для
истории, из расписания GitHub Actions убран (см. .github/workflows/pyrus-bots.yml).

Бот «Остаток по смете». Pyrus не умеет сам вычислять произвольные формулы
между полем формы и внешним справочником лимитов, поэтому остаток считает
этот бот и записывает его обратно в поле budget_remainder. Дальше уже
условное форматирование в самой форме (настроенное вами в конструкторе)
подсвечивает поле красным, если оно ушло в минус.

Логика:
  1. Берём лимит по паре (объект, категория) из справочника «Лимиты по объектам».
  2. Суммируем quantity уже поданных активных заявок по этой же паре
     (объект, категория) — читаем реестр формы без фильтра по полю
     (фильтрация по числовому id поля здесь не нужна: перебираем поля
     каждой задачи и ищем нужные по code).
  3. remainder = лимит - уже использовано в других заявках - количество в этой заявке.
  4. Обновляем поле budget_remainder через field_updates по code
     (POST /tasks/{id}/comments) — БЕЗ текста комментария (silent update),
     чтобы не засорять историю задачи при регулярном перезапуске по расписанию.

Два режима запуска:
  - без аргумента — обходит ВСЕ активные заявки формы разом (для cron /
    GitHub Actions по расписанию) и обновляет только те, где остаток
    реально изменился;
  - с task_id — пересчитывает только одну заявку (для ручного вызова
    или вебхука на конкретное событие).

Запуск:
    export PYRUS_LOGIN="pyrus_demo@outlook.com"
    export PYRUS_SECURITY_KEY="ваш_секретный_ключ"
    python3 budget_remainder_bot.py            # все активные заявки
    python3 budget_remainder_bot.py <task_id>  # одна заявка
"""

import sys
from pyrus_client import PyrusClient, FORM_ID, field_by_code


def get_limits_map(client):
    """{(object_name, category_name): limit} по всем строкам справочника."""
    catalog_id = client.find_catalog_id("Лимиты по объектам")
    if not catalog_id:
        raise RuntimeError("Справочник «Лимиты по объектам» не найден")
    catalog = client.get(f"catalogs/{catalog_id}")
    limits = {}
    for item in catalog["items"]:
        # Первая колонка называется "Объект", но переименовать её нельзя
        # (ограничение API), поэтому в ней хранится составной ключ
        # синхронизации "Объект / Категория", а реальное имя объекта — в
        # добавленной 5-й колонке. Порядок values:
        # [Ключ, Категория, Лимит, Период, Объект (полное имя)]
        values = item["values"]
        if len(values) >= 5:
            limits[(values[4], values[1])] = float(values[2])
    return limits


def get_limit(client, object_name, category_name):
    return get_limits_map(client).get((object_name, category_name))


def _obj_cat_qty(fields):
    """Возвращает (объект, категория, количество) из полей задачи или None,
    если какого-то из полей нет."""
    obj_field = field_by_code(fields, "object")
    cat_field = field_by_code(fields, "material_category")
    qty_field = field_by_code(fields, "quantity")
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


def sum_used_budget(client, object_name, category_name, exclude_task_id=None):
    """Суммирует количество по уже поданным заявкам с тем же объектом
    и категорией (кроме текущей задачи)."""
    register = client.get_register(FORM_ID, include_archived=False)
    total = 0.0
    for task in register.get("tasks", []):
        if exclude_task_id and task["id"] == exclude_task_id:
            continue
        parsed = _obj_cat_qty(task.get("fields", []))
        if parsed and parsed[0] == object_name and parsed[1] == category_name:
            total += parsed[2]
    return total


def update_budget_remainder(task_id):
    """Пересчитывает остаток по смете для одной задачи (ручной вызов/вебхук)."""
    client = PyrusClient()
    task = client.get(f"tasks/{task_id}")["task"]
    fields = task.get("fields", [])

    parsed = _obj_cat_qty(fields)
    if not parsed:
        print("В задаче не найдены поля object/material_category/quantity по code — проверьте verify_form_codes.py")
        return
    object_name, category_name, quantity = parsed

    limit = get_limit(client, object_name, category_name)
    if limit is None:
        print(f"Лимит для «{object_name}» / «{category_name}» не найден в справочнике")
        return

    already_used = sum_used_budget(client, object_name, category_name, exclude_task_id=task_id)
    remainder = limit - already_used - quantity

    client.post(
        f"tasks/{task_id}/comments",
        {"field_updates": [{"code": "budget_remainder", "value": remainder}]},
    )
    print(f"Задача {task_id}: остаток по смете обновлён = {remainder:.2f}")


def sweep_all_tasks():
    """Пересчитывает остаток по смете для всех активных заявок формы за
    один проход (для запуска по расписанию). Обновляет задачу, только если
    посчитанное значение реально отличается от того, что уже записано в
    поле — иначе на каждый прогон мы бы дёргали API и создавали лишнюю
    активность в задачах, которые никто не менял."""
    client = PyrusClient()
    limits = get_limits_map(client)
    register = client.get_register(FORM_ID, include_archived=False)

    parsed_by_task = {}
    current_remainder_by_task = {}
    for task in register.get("tasks", []):
        parsed = _obj_cat_qty(task.get("fields", []))
        if parsed:
            parsed_by_task[task["id"]] = parsed
        remainder_field = field_by_code(task.get("fields", []), "budget_remainder")
        current_remainder_by_task[task["id"]] = remainder_field.get("value") if remainder_field else None

    updated = 0
    for task_id, (object_name, category_name, quantity) in parsed_by_task.items():
        limit = limits.get((object_name, category_name))
        if limit is None:
            continue

        used_by_others = sum(
            q for tid, (o, c, q) in parsed_by_task.items()
            if tid != task_id and o == object_name and c == category_name
        )
        remainder = limit - used_by_others - quantity

        current_value = current_remainder_by_task.get(task_id)
        if current_value is not None:
            try:
                if abs(float(current_value) - remainder) < 0.01:
                    continue  # не изменилось — задачу не трогаем
            except (TypeError, ValueError):
                pass

        client.post(
            f"tasks/{task_id}/comments",
            {"field_updates": [{"code": "budget_remainder", "value": remainder}]},
        )
        updated += 1
        print(f"Задача {task_id}: остаток по смете обновлён = {remainder:.2f}")

    print(f"\nВсего обновлено задач: {updated} из {len(parsed_by_task)} рассмотренных")


def main():
    if len(sys.argv) >= 2:
        update_budget_remainder(int(sys.argv[1]))
    else:
        sweep_all_tasks()


if __name__ == "__main__":
    main()
