#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Настраивает права доступа к форме 2455896 согласно разделу 2 спецификации
(«Роли и права доступа»): подставьте реальные email сотрудников в словарь
EMPLOYEES ниже — скрипт сам найдёт их person_id через GET /members и
выставит права методом POST /forms/2455896/permissions.

Уровни доступа Pyrus: administrator, manager, restricted_manager, member, none.
Менеджеров "по условию" (restricted_manager) через API добавить нельзя —
такие правила настраиваются только в конструкторе формы (это уже сделано
вами вручную для этапа согласования у руководителя).

Запуск:
    export PYRUS_LOGIN="pyrus_demo@outlook.com"
    export PYRUS_SECURITY_KEY="ваш_секретный_ключ"
    python3 permissions_setup.py
"""

from pyrus_client import PyrusClient, FORM_ID

# Заполните реальными email из вашей организации Pyrus.
EMPLOYEES = {
    "administrator": [
        # "admin@company.ru",
    ],
    "manager": [
        # "snabzhenets@company.ru",
    ],
    "member": [
        # "prorab1@company.ru",
        # "prorab2@company.ru",
        # "kladovshik@company.ru",
        # "buhgalter@company.ru",
    ],
}


def main():
    client = PyrusClient()

    permissions = {}
    not_found = []

    for level, emails in EMPLOYEES.items():
        for email in emails:
            person_id = client.find_member_id(email)
            if person_id is None:
                not_found.append(email)
                continue
            permissions[str(person_id)] = level

    if not_found:
        print("Не найдены в организации (проверьте email):")
        for e in not_found:
            print(f"  - {e}")

    if not permissions:
        print("Нечего применять: заполните словарь EMPLOYEES реальными email и запустите снова.")
        return

    result = client.post(f"forms/{FORM_ID}/permissions", {"permissions": permissions})
    print("Права обновлены:")
    for person_id, level in result["permissions"].items():
        print(f"  {person_id}: {level}")


if __name__ == "__main__":
    main()
