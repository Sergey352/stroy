#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверяет, что в форме 2455896 у полей проставлены коды (code) под ТЕКУЩУЮ
модель «смета по позициям» (сессия 2026-08-18) — не под исходную
спецификацию из docx (та была под лимиты по категориям, устарела вместе
с полями object/material_category/quantity/budget_remainder/budget_cost,
которые пользователь удалил с формы при переходе на новую модель).
Без корректных code остальные скрипты (боты, вебхук) работать не смогут —
они обращаются к полям по коду, а не по id/названию.

Запуск:
    export PYRUS_LOGIN="pyrus_demo@outlook.com"
    export PYRUS_SECURITY_KEY="ваш_секретный_ключ"
    python3 verify_form_codes.py
"""

from pyrus_client import PyrusClient, FORM_ID, collect_form_fields

EXPECTED_CODES = {
    # Поле «Объект» (id=43) — code "Object" (с большой буквы) Pyrus
    # выставил САМ, автоматически, без ручной настройки в конструкторе
    # (обнаружено эмпирически в сессии 2026-08-18, см. CLAUDE.md) — не
    # опечатка, именно так и должно быть.
    "Object": "Объект",
    "items_table": "Позиции заявки",
    # Колонки таблицы «Позиции заявки» — коды проставлены пользователем
    # вручную в сессии 2026-08-18, используются smeta_remainder_bot.py
    # и pyrus_form_script.js.
    "item_catalog": "Номенклатура",
    "item_name": "Название",
    "item_unit": "Ед. изм.",
    "item_qty_ordered": "Заказанное количество",
    "item_qty_budget": "Кол-во по смете",
    "item_remainder": "Остаток",
    "item_price": "Цена",
    "delivery_deadline": "Срок поставки",
    "priority": "Приоритет",
    "overrun_reason": "Обоснование перерасхода",
    "supplier": "Поставщик",
    "supplier_price": "Цена поставщика",
    "order_number": "Номер заказа",
    "invoice_file": "Счёт поставщика (файл)",
    "invoice_confirmed": "Подтверждение счёта",
    "payment_confirmed": "Подтверждение оплаты",
    "payment_status": "Статус оплаты",
    "payment_deferral_date": "Срок отсрочки",
    "payment_order_file": "Платёжное поручение (ПП)",
    "payment_done": "Отметка «Исполнено» (оплата)",
    "delivery_date": "Дата доставки",
    "upd_photo": "Фото УПД",
    "receipt_confirmed": "Подтверждение получения",
    "rejection_comment": "Комментарий по отказу от товара",
    "upd_match_status": "Статус сверки УПД со счётом",
    "partial_delivery_close": "Закрытие с неполной поставкой",
}


def main():
    client = PyrusClient()
    form = client.get_form(FORM_ID)

    all_fields = []
    collect_form_fields(form.get("fields", []), all_fields)

    by_code = {
        f["info"]["code"]: f
        for f in all_fields
        if (f.get("info") or {}).get("code")
    }

    print(f"Форма: «{form.get('name')}» (id={form.get('id')})")
    print(f"Всего полей в форме: {len(all_fields)}, из них с заполненным code: {len(by_code)}\n")

    missing = []
    mismatched_name = []
    for code, expected_name in EXPECTED_CODES.items():
        f = by_code.get(code)
        if not f:
            missing.append(code)
        elif f.get("name") != expected_name:
            mismatched_name.append((code, f.get("name"), expected_name))

    if not missing and not mismatched_name:
        print("Все ожидаемые коды найдены и совпадают с названиями из спецификации. Можно запускать остальные скрипты.")
        return

    if missing:
        print(f"Не найдены поля с кодом (проставьте code в конструкторе формы), {len(missing)} шт.:")
        for code in missing:
            print(f"  - {code}  (ожидалось поле «{EXPECTED_CODES[code]}»)")

    if mismatched_name:
        print(f"\nКод найден, но название поля отличается от спецификации (не критично, просто для сверки), {len(mismatched_name)} шт.:")
        for code, actual, expected in mismatched_name:
            print(f"  - {code}: в форме «{actual}», в спецификации «{expected}»")


if __name__ == "__main__":
    main()
