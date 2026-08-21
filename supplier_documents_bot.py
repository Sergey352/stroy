#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот «Документы на поставщиков» (сессия 2026-08-19).

ЗАДАЧА: после того как снабженец прошёл этап «Снабжение» (шаг 3 маршрута)
и проставил поставщика в каждой строке таблицы «Позиции заявки», нужно
сформировать ОТДЕЛЬНЫЙ документ (Excel) на КАЖДОГО поставщика,
встретившегося в заявке — со списком именно его позиций. Снабженец сам
скачивает готовый файл из задачи и отправляет поставщику (автоматическая
отправка на почту — задача следующего этапа, см. CLAUDE.md).

ПОЧЕМУ ПОСЛЕ ШАГА 3, А НЕ РАНЬШЕ: до завершения «Снабжения» поставщик у
позиций мог быть ещё не проставлен (либо проставлен не до конца) —
дёргать генерацию документов раньше означало бы либо ловить пустые/
неполные списки, либо генерировать документы повторно после каждой
правки. current_step > 3 — надёжный признак, что шаг 3 пройден, вне
зависимости от того, срабатывал ли условный шаг 2 (согласование
перерасхода) — нумерация шагов фиксированная, current_step просто
перепрыгивает через шаг 2, если он не нужен.

ПОСТАВЩИК НА ПОЗИЦИЮ: колонка «Поставщик», тип «Справочник» →
«Поставщики» (306329), проставляет снабженец вручную (решение сессии
2026-08-19 — автоподстановки пока нет, т.к. нет данных «какой материал у
какого поставщика»). Такая колонка нужна В ОБЕИХ таблицах заявки:
  - «Позиции заявки» — code=item_supplier
  - «Позиции, отсутствующие в смете» — code=not_item_supplier
Вторая таблица тоже участвует специально: то, что снабженец ещё не успел
добавить в справочник «Смета», всё равно нужно закупить в рамках ЭТОЙ
заявки — иначе такие позиции никогда не попали бы ни в один документ
поставщику.

ИДЕМПОТЕНТНОСТЬ: на каждого поставщика бот оставляет отдельный
комментарий с прикреплённым файлом и с маркером в тексте (SUPPLIER_DOC_MARKER
+ имя поставщика) — при повторном прогоне для этого же поставщика
находит такой комментарий и пропускает, генерирует только для новых/ещё
не охваченных поставщиков. Так частичный сбой (одна доставка удалась,
другая нет) не приводит к дублям при следующем прогоне.

Запуск:
    export PYRUS_LOGIN="pyrus_demo@outlook.com"
    export PYRUS_SECURITY_KEY="ваш_секретный_ключ"
    python3 supplier_documents_bot.py             # обойти все заявки
    python3 supplier_documents_bot.py --dry-run    # только показать, что было бы сгенерировано
"""

import io
import sys

from openpyxl import Workbook

from pyrus_client import PyrusClient, FORM_ID, field_by_code

SUPPLIER_DOC_MARKER = "📦 Документ поставщику"
MIN_STEP_AFTER_SNABZHENIE = 4  # current_step >= 4 значит шаг 3 «Снабжение» пройден


def _collect_rows_into_groups(task, groups, table_code, name_code, unit_code, qty_code, price_code, supplier_code):
    """Общая логика сбора строк одной таблицы в {поставщик: [строки]} —
    используется дважды: для «Позиции заявки» (обычные позиции по
    справочнику «Смета») и для «Позиции, отсутствующие в смете» (снабженец
    их ещё не добавил в справочник, но закупать всё равно нужно — иначе
    эти материалы вообще никогда не попали бы ни в один документ
    поставщику). Названия колонок в двух таблицах разные
    (item_name/not_item_name и т.д.), поэтому код параметризован."""
    table_field = field_by_code(task.get("fields", []), table_code)
    if not table_field:
        return

    for row in table_field.get("value") or []:
        cells = row.get("cells", [])
        supplier_cell = field_by_code(cells, supplier_code)
        if not supplier_cell:
            continue  # колонка ещё не добавлена в форму — тихо пропускаем

        supplier_value = supplier_cell.get("value") or {}
        supplier_names = supplier_value.get("values") or []
        supplier_name = supplier_names[0] if supplier_names else None
        if not supplier_name:
            continue  # поставщик в этой строке не выбран

        name = (field_by_code(cells, name_code) or {}).get("value")
        unit = (field_by_code(cells, unit_code) or {}).get("value")
        qty = (field_by_code(cells, qty_code) or {}).get("value")
        price = (field_by_code(cells, price_code) or {}).get("value")
        if not name:
            continue

        groups.setdefault(supplier_name, []).append(
            {"name": name, "unit": unit, "qty": qty, "price": price}
        )


def get_rows_by_supplier(task):
    """Группирует по поставщику строки ОБЕИХ таблиц заявки: «Позиции
    заявки» (item_supplier) и «Позиции, отсутствующие в смете»
    (not_item_supplier) — снабженец должен закупить материал независимо
    от того, успел ли он уже добавить его в справочник «Смета». Возвращает
    {имя_поставщика: [строка, строка, ...]}, строка — словарь
    {name, unit, qty, price}."""
    groups = {}
    _collect_rows_into_groups(
        task, groups,
        table_code="items_table", name_code="item_name", unit_code="item_unit",
        qty_code="item_qty_ordered", price_code="item_price", supplier_code="item_supplier",
    )
    _collect_rows_into_groups(
        task, groups,
        table_code="missing_items_table", name_code="not_item_name", unit_code="not_item_unit",
        qty_code="not_item_qty_ordered", price_code="not_item_price", supplier_code="not_item_supplier",
    )
    return groups


def build_supplier_workbook(object_name, task_id, supplier_name, rows):
    """Собирает простую Excel-таблицу (в памяти, без временных файлов на
    диске) — заголовок с объектом/номером заявки и таблица позиций.
    Возвращает bytes, готовые к загрузке через PyrusClient.upload_file."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Заказ"

    ws.append([f"Заказ поставщику: {supplier_name}"])
    ws.append([f"Объект: {object_name or '—'}"])
    ws.append([f"Заявка № {task_id}"])
    ws.append([])
    ws.append(["Наименование", "Ед. изм.", "Количество", "Цена", "Сумма"])

    for row in rows:
        qty = row["qty"] or 0
        price = row["price"] or 0
        try:
            total = float(qty) * float(price)
        except (TypeError, ValueError):
            total = None
        ws.append([row["name"], row["unit"], qty, price, total])

    # Ширина колонок по содержимому — просто чтобы файл было удобно
    # открыть и сразу прочитать, без ручной подгонки снабженцем.
    widths = [50, 10, 12, 12, 14]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def already_sent_to_supplier(task, supplier_name):
    marker = f"{SUPPLIER_DOC_MARKER} «{supplier_name}»"
    for comment in task.get("comments", []):
        if marker in (comment.get("text") or ""):
            return True
    return False


def generate_for_task(client, task, dry_run=False):
    """Генерирует и прикрепляет документы для ещё не охваченных
    поставщиков этой задачи. Возвращает число реально сгенерированных
    документов (0, если генерировать было нечего или всё уже сделано)."""
    groups = get_rows_by_supplier(task)
    if not groups:
        return 0

    object_field = field_by_code(task.get("fields", []), "Object")
    object_value = object_field.get("value") if object_field else None
    object_name = (object_value or {}).get("values", [None])[0] if isinstance(object_value, dict) else None

    generated = 0
    for supplier_name, rows in groups.items():
        if already_sent_to_supplier(task, supplier_name):
            continue

        if dry_run:
            print(f"    [{task['id']}] поставщик «{supplier_name}»: {len(rows)} позиций (--dry-run, не отправляю)")
            generated += 1
            continue

        file_bytes = build_supplier_workbook(object_name, task["id"], supplier_name, rows)
        filename = f"Заказ {supplier_name} (заявка {task['id']}).xlsx"
        upload_result = client.upload_file(file_bytes, filename)

        client.post(
            f"tasks/{task['id']}/comments",
            {
                "text": f"{SUPPLIER_DOC_MARKER} «{supplier_name}»: сформирован файл заказа, {len(rows)} позиций.",
                "attachments": [upload_result["guid"]],
            },
        )
        generated += 1
        print(f"    [{task['id']}] поставщик «{supplier_name}»: документ сформирован и прикреплён")

    return generated


def sweep(dry_run=False):
    client = PyrusClient()
    register = client.get_register(FORM_ID, include_archived=True)

    considered = 0
    total_generated = 0
    for stub in register.get("tasks", []):
        task = client.get(f"tasks/{stub['id']}")["task"]

        if task.get("current_step", 0) < MIN_STEP_AFTER_SNABZHENIE:
            continue  # шаг «Снабжение» ещё не пройден — рано генерировать документы

        considered += 1
        total_generated += generate_for_task(client, task, dry_run=dry_run)

    print(f"Заявок после шага «Снабжение»: {considered}")
    print(f"Сгенерировано документов на поставщиков: {total_generated}")


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    sweep(dry_run=dry_run)


if __name__ == "__main__":
    main()
