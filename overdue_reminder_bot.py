#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-напоминание о просрочке поставки (см. ТЗ п.4: «Если в назначенную дату
не приехал товар – в задаче идет просрочка и напоминание снабженцу»).

Логика:
  1. Получаем все открытые задачи формы (без фильтра по полю — фильтруем
     на стороне скрипта по code, как и остальные скрипты набора).
  2. Для каждой задачи смотрим поле delivery_deadline (code) и
     receipt_confirmed (code).
  3. Если срок поставки прошёл, а получение ещё не подтверждено —
     оставляем комментарий с напоминанием и переназначаем на снабженца
     (email снабженца берите из справочника «Поставщики»/оргструктуры;
     здесь используется поле "responsible" текущей задачи — бот просто
     комментирует, не меняя ответственного, чтобы не сломать вашу
     маршрутизацию).

Запускайте по расписанию (например, каждый час) — через cron / планировщик
задач Windows / внешний scheduler.

Запуск:
    export PYRUS_LOGIN="pyrus_demo@outlook.com"
    export PYRUS_SECURITY_KEY="ваш_секретный_ключ"
    python3 overdue_reminder_bot.py
"""

from datetime import datetime, timezone
from pyrus_client import PyrusClient, FORM_ID, field_by_code

REMINDER_MARKER = "⚠️ Просрочка поставки"


def already_reminded(client, task_id):
    """Проверяет по комментариям задачи, отправляли ли уже напоминание —
    без этого при запуске по расписанию бот слал бы одно и то же
    напоминание на каждый прогон, пока просрочка не устранена."""
    task = client.get(f"tasks/{task_id}")["task"]
    return any(REMINDER_MARKER in (c.get("text") or "") for c in task.get("comments", []))


def parse_pyrus_datetime(value):
    if not value:
        return None
    # due_date_time: "2026-08-20T09:00:00Z"; due_date: "2026-08-20"
    if "T" in value:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main():
    client = PyrusClient()
    now = datetime.now(timezone.utc)

    register = client.get_register(FORM_ID, include_archived=False)
    reminded = 0

    for task in register.get("tasks", []):
        fields = task.get("fields", [])
        deadline_field = field_by_code(fields, "delivery_deadline")
        receipt_field = field_by_code(fields, "receipt_confirmed")

        if not deadline_field:
            continue

        deadline_value = deadline_field.get("value")
        deadline = parse_pyrus_datetime(deadline_value)
        if not deadline or deadline >= now:
            continue

        already_confirmed = receipt_field and receipt_field.get("value") == "checked"
        if already_confirmed:
            continue

        if already_reminded(client, task["id"]):
            continue

        client.post(
            f"tasks/{task['id']}/comments",
            {
                "text": (
                    f"{REMINDER_MARKER}: срок был {deadline_value}, "
                    f"а получение материалов ещё не подтверждено. Проверьте статус у поставщика."
                ),
            },
        )
        reminded += 1
        print(f"Задача {task['id']}: напоминание о просрочке отправлено (срок был {deadline_value})")

    print(f"\nВсего напоминаний отправлено: {reminded}")


if __name__ == "__main__":
    main()
