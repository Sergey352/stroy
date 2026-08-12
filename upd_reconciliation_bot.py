#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот сверки УПД со счётом (ТЗ п.4: «Бот – сверяет УПД со счётом, в случае
недопоставки по счету БОТ подсвечивает товары в заявке зеленым что
отгружено, красным не отгружено»).

ВАЖНО (ограничение): в текущем составе полей формы УПД прикладывается как
фото (upd_photo, тип file) — это скан документа, а не структурированные
данные. Автоматически считать построчное сравнение "счёт vs УПД" по фото
без OCR/распознавания нельзя. Есть два пути:
  1. Добавить в форму дополнительное поле с построчным вводом фактически
     отгруженного количества (например, отдельная таблица "Факт по УПД"),
     которое кладовщик/прораб заполняют вручную при приёмке — тогда бот
     ниже сравнивает их с items_table и расставляет статус по-позиционно.
  2. Подключить OCR-сервис для распознавания УПД из upd_photo — тогда бот
     дополнительно вызывает распознавание и подставляет результат в то же
     поле "Факт по УПД", после чего работает по сценарию (1).

Ниже реализован вариант (1) в виде готового бота: он ожидает, что рядом
с items_table у вас есть поле-таблица с кодом "delivered_table" (колонки:
name, qty_delivered) — если вы создали такое поле в конструкторе, задайте
ему этот код, и бот заработает. Если поля нет — бот использует упрощённое
правило: полностью подтверждённая приёмка (receipt_confirmed=checked) без
комментария по отказу (rejection_comment пусто) считается «Отгружено»,
любой отказ/комментарий — «Не отгружено», и записывает это в
upd_match_status.

Два режима запуска:
  - без аргумента — обходит все активные заявки, у которых уже отмечена
    приёмка (receipt_confirmed) или есть комментарий по отказу, и
    обновляет upd_match_status только там, где посчитанный статус
    отличается от уже записанного (для cron / GitHub Actions);
  - с task_id — пересчитывает одну заявку (ручной вызов).

Запуск:
    export PYRUS_LOGIN="pyrus_demo@outlook.com"
    export PYRUS_SECURITY_KEY="ваш_секретный_ключ"
    python3 upd_reconciliation_bot.py            # все подходящие заявки
    python3 upd_reconciliation_bot.py <task_id>  # одна заявка
"""

import sys
from pyrus_client import PyrusClient, FORM_ID, field_by_code


def compute_status(fields):
    """Возвращает (overall, result_text) по полям задачи — без побочных
    эффектов, чтобы использовать и в одиночном, и в пакетном режиме."""
    delivered_table = field_by_code(fields, "delivered_table")  # опциональное поле, см. докстринг
    items_table = field_by_code(fields, "items_table")
    receipt_field = field_by_code(fields, "receipt_confirmed")
    rejection_field = field_by_code(fields, "rejection_comment")

    if delivered_table and items_table:
        # Построчное сравнение: сопоставляем строки по названию позиции.
        delivered_by_name = {}
        for row in delivered_table.get("value", []):
            cells = {c.get("id"): c.get("value") for c in row.get("cells", [])}
            name = cells.get(next(iter(cells), None))  # первая колонка — наименование
            delivered_by_name[name] = cells

        statuses = []
        for row in items_table.get("value", []):
            cells = {c.get("id"): c.get("value") for c in row.get("cells", [])}
            name = next(iter(cells.values()), None)
            status = "Отгружено" if name in delivered_by_name else "Не отгружено"
            statuses.append(f"{name}: {status}")

        result_text = "; ".join(statuses)
        overall = "Не отгружено" if "Не отгружено" in result_text else "Отгружено"
    else:
        # Упрощённый вариант без построчных данных
        has_rejection = bool(rejection_field and rejection_field.get("value"))
        confirmed = bool(receipt_field and receipt_field.get("value") == "checked")
        overall = "Не отгружено" if (has_rejection or not confirmed) else "Отгружено"
        result_text = f"Упрощённая проверка (нет поля delivered_table): confirmed={confirmed}, rejection={has_rejection}"

    return overall, result_text


def is_relevant(fields):
    """Задачу вообще имеет смысл сверять только если приёмка отмечена
    (receipt_confirmed) или есть отказ (rejection_comment) — до этого
    момента сверять нечего."""
    receipt_field = field_by_code(fields, "receipt_confirmed")
    rejection_field = field_by_code(fields, "rejection_comment")
    return bool((receipt_field and receipt_field.get("value") == "checked") or
                (rejection_field and rejection_field.get("value")))


def current_match_status(fields):
    status_field = field_by_code(fields, "upd_match_status")
    value = status_field.get("value") if status_field else None
    if isinstance(value, dict):
        names = value.get("choice_names") or []
        return names[0] if names else None
    return value


def reconcile(task_id):
    """Пересчитывает статус сверки для одной задачи (ручной вызов)."""
    client = PyrusClient()
    task = client.get(f"tasks/{task_id}")["task"]
    fields = task.get("fields", [])

    overall, result_text = compute_status(fields)

    client.post(
        f"tasks/{task_id}/comments",
        {
            "field_updates": [
                {"code": "upd_match_status", "value": {"choice_names": [overall]}},
            ],
            "text": f"Бот сверки УПД со счётом: {overall}. Детали: {result_text}",
        },
    )
    print(f"Задача {task_id}: статус сверки УПД = {overall}")


def sweep_all_tasks():
    """Обходит все активные заявки формы и обновляет upd_match_status у
    тех, где приёмка уже отмечена и посчитанный статус отличается от уже
    записанного — чтобы не постить повторно один и тот же результат на
    каждый прогон по расписанию."""
    client = PyrusClient()
    register = client.get_register(FORM_ID, include_archived=False)

    updated = 0
    considered = 0
    for task in register.get("tasks", []):
        fields = task.get("fields", [])
        if not is_relevant(fields):
            continue
        considered += 1

        overall, result_text = compute_status(fields)
        if current_match_status(fields) == overall:
            continue  # уже записан тот же статус — задачу не трогаем

        client.post(
            f"tasks/{task['id']}/comments",
            {
                "field_updates": [
                    {"code": "upd_match_status", "value": {"choice_names": [overall]}},
                ],
                "text": f"Бот сверки УПД со счётом: {overall}. Детали: {result_text}",
            },
        )
        updated += 1
        print(f"Задача {task['id']}: статус сверки УПД = {overall}")

    print(f"\nВсего обновлено задач: {updated} из {considered} рассмотренных")


def main():
    if len(sys.argv) >= 2:
        reconcile(int(sys.argv[1]))
    else:
        sweep_all_tasks()


if __name__ == "__main__":
    main()
