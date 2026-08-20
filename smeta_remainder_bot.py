#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот «Остаток по позиции сметы» — новая модель остатка (сессия 2026-08-18),
заменяет старую логику budget_remainder_bot.py (та работала по паре
объект+категория материала — этих полей на форме больше нет, они удалены
при переходе на модель «смета по позициям»).

КАК СЧИТАЕТСЯ ОСТАТОК (важно — не «списание», а «пересчёт с нуля»):
  Для каждой позиции справочника «Смета» (306906):
      Остаток = Кол-во по смете (неизменный ориджинал, из последнего
                импорта Excel через smeta_catalog_import.py)
                − сумма «Заказанное количество» по ВСЕМ заявкам формы,
                  где эта позиция выбрана хоть в одной строке таблицы
                  «Позиции заявки», КРОМЕ заявок, отклонённых на любом
                  шаге согласования.

  Это НЕ накопительное вычитание (не «было 10, заказали 3, стало 7,
  запомнили 7»), а полный пересчёт при каждом запуске бота — так же, как
  была устроена самая первая версия budget_remainder_bot.py. Плюсы этого
  подхода: результат не может «разъехаться» из-за пропущенного вебхука или
  повторного запуска (идемпотентно), а отклонённая заявка автоматически
  перестаёт учитываться при первом же следующем прогоне бота — без
  отдельного кода «откатить остаток» (это и есть требование ТЗ «списание
  сразу при подаче, с возвратом остатка при отклонении», сессия
  2026-08-18 — просто реализовано через пересчёт, а не через ручной
  +/- остатка).

  Закрытые (is_closed=true), но НЕ отклонённые заявки — по-прежнему
  учитываются в сумме (материал реально заказан и потрачен, закрытие
  заявки не должно освобождать бюджет обратно).

КАК ОПРЕДЕЛЯЕТСЯ «ОТКЛОНЕНО»:
  В API Pyrus нет отдельного флага «заявка отклонена» — статус
  отклонения виден только через approvals[шаг][участник].approval_choice
  == "rejected" (проверено по справке API, сессия 2026-08-18). Поэтому
  функция is_rejected() ищет "rejected" по всем шагам согласования задачи.

СВЯЗЬ ЗАЯВКИ СО СТРОКОЙ СПРАВОЧНИКА:
  В ячейке «Номенклатура» (code=item_catalog) хранится item_id элемента
  справочника «Смета» — тот же item_id, что отдаётся в GET /catalogs/{id}
  для каждой строки. Поэтому сопоставление идёт по item_id, а не по
  нашему собственному синтетическому ключу (первая колонка справочника) —
  так надёжнее и не нужно ничего парсить руками.

Запуск:
    export PYRUS_LOGIN="pyrus_demo@outlook.com"
    export PYRUS_SECURITY_KEY="ваш_секретный_ключ"
    python3 smeta_remainder_bot.py             # пересчитать всё
    python3 smeta_remainder_bot.py --dry-run   # только показать, что изменится

ВТОРАЯ ЗАДАЧА ЭТОГО БОТА — «позиции нет в справочнике» (см. функцию
get_missing_descriptions ниже): т.к. catalog-поле в Pyrus не поддерживает
свободный ввод (только строгий выбор из существующих элементов, проверено
по официальной документации в сессии 2026-08-18), пользователь завёл в
конструкторе ОТДЕЛЬНУЮ таблицу «Позиции, отсутствующие в смете»
(code=missing_items_table, поля not_item_name/not_item_unit/
not_item_qty_ordered/not_item_price) — заполняется вместо строки в
основной таблице «Позиции заявки», когда нужного материала нет в
справочнике «Смета». Бот такие строки посчитать не может (нет item_id) —
вместо этого оставляет заявителю/снабженцу комментарий-напоминание со
списком, чтобы снабженец добавил позиции в справочник вручную.

Запуск (тот же скрипт, оба действия — пересчёт остатка и проверка
недостающих позиций — выполняются вместе за один проход по задачам):
    export PYRUS_LOGIN="pyrus_demo@outlook.com"
    export PYRUS_SECURITY_KEY="ваш_секретный_ключ"
    python3 smeta_remainder_bot.py             # пересчитать всё
    python3 smeta_remainder_bot.py --dry-run   # только показать, что изменится
"""

import sys

from pyrus_client import PyrusClient, FORM_ID, field_by_code

SMETA_CATALOG_NAME = "Смета"

# Индексы колонок в справочнике «Смета» (см. CATALOG_HEADERS в
# smeta_catalog_import.py) — работаем по позиции в списке values, так как
# у колонок справочника (в отличие от полей формы) кодов не бывает.
COL_KEY = 0
COL_NAME = 1
COL_BUDGET_QTY = 6
COL_REMAINDER = 7

# Маркер в тексте комментария — чтобы не напоминать про одну и ту же
# недостающую позицию на каждом прогоне бота (та же идея, что и в
# overdue_reminder_bot.py: ищем этот маркер среди уже оставленных
# комментариев задачи, прежде чем оставлять новый).
MISSING_ITEM_MARKER = "⚠️ Позиция не из справочника «Смета»"


def is_rejected(task):
    """Проверяет, отклонена ли задача хоть на одном шаге согласования.
    В Pyrus нет отдельного флага «отклонено» — смотрим approval_choice
    каждого участника каждого шага (см. докстринг модуля)."""
    for step in task.get("approvals", []):
        for approver in step:
            if approver.get("approval_choice") == "rejected":
                return True
    return False


def get_ordered_quantities(task):
    """Возвращает {item_id справочника «Смета»: заказанное количество}
    по всем строкам таблицы «Позиции заявки» этой задачи. Строки без
    выбранной номенклатуры или без указанного количества пропускаются
    (черновик, ещё не заполнен до конца)."""
    result = {}
    table_field = field_by_code(task.get("fields", []), "items_table")
    if not table_field:
        return result

    for row in table_field.get("value") or []:
        cells = row.get("cells", [])
        catalog_cell = field_by_code(cells, "item_catalog")
        qty_cell = field_by_code(cells, "item_qty_ordered")
        if not catalog_cell or not qty_cell:
            continue

        catalog_value = catalog_cell.get("value") or {}
        item_id = catalog_value.get("item_id")
        qty = qty_cell.get("value")
        if item_id is None or qty is None:
            continue

        try:
            qty = float(qty)
        except (TypeError, ValueError):
            continue

        result[item_id] = result.get(item_id, 0.0) + qty

    return result


def get_missing_descriptions(task):
    """Возвращает список описаний недостающих позиций из отдельной
    таблицы «Позиции, отсутствующие в смете» (code=missing_items_table,
    добавлена пользователем в конструкторе в сессии 2026-08-18 — своя
    таблица, а не колонка внутри «Позиции заявки», как предполагалось
    изначально: у неё сразу 4 поля — Название/Ед.изм./Заказанное
    количество/Цена, снабженцу этого достаточно, чтобы завести новую
    позицию в справочнике «Смета» без дополнительных вопросов автору
    заявки). Пустые строки (ничего не заполнено) пропускаются."""
    descriptions = []
    table_field = field_by_code(task.get("fields", []), "missing_items_table")
    if not table_field:
        return descriptions

    for row in table_field.get("value") or []:
        cells = row.get("cells", [])
        name = (field_by_code(cells, "not_item_name") or {}).get("value")
        unit = (field_by_code(cells, "not_item_unit") or {}).get("value")
        qty = (field_by_code(cells, "not_item_qty_ordered") or {}).get("value")
        price = (field_by_code(cells, "not_item_price") or {}).get("value")

        name = (name or "").strip()
        if not name:
            continue  # пустая строка (черновик) — пропускаем

        parts = [name]
        if qty is not None:
            parts.append(f"{qty} {unit or ''}".strip())
        if price is not None:
            parts.append(f"цена {price}")
        descriptions.append(", ".join(parts))

    return descriptions


def already_notified_about_missing(task):
    """Проверяет, оставлял ли бот уже комментарий-напоминание об этой же
    задаче (см. MISSING_ITEM_MARKER) — чтобы не дублировать при каждом
    прогоне. Не различает, какие именно позиции уже упоминались: если
    список недостающих позиций в заявке поменялся, снабженец всё равно
    увидит актуальный список в предыдущем комментарии бота глазами — это
    сознательное упрощение, не отслеживаем это тонко."""
    for comment in task.get("comments", []):
        if MISSING_ITEM_MARKER in (comment.get("text") or ""):
            return True
    return False


def notify_missing_items(client, task):
    """Если в заявке есть строки с описанием отсутствующей позиции —
    оставляет один комментарий со списком, чтобы снабженец добавил их в
    справочник «Смета» (вручную в Pyrus или через smeta_catalog_import.py)."""
    descriptions = get_missing_descriptions(task)
    if not descriptions or already_notified_about_missing(task):
        return False

    lines = "\n".join(f"- {d}" for d in descriptions)
    text = (
        f"{MISSING_ITEM_MARKER}\n"
        f"В таблице «Позиции, отсутствующие в смете» указаны позиции, которых "
        f"нет в справочнике «Смета»:\n{lines}\n\n"
        f"Снабженцу нужно добавить их в справочник вручную в Pyrus — после этого "
        f"их можно будет выбрать в поле «Номенклатура» основной таблицы «Позиции "
        f"заявки»."
    )
    client.post(f"tasks/{task['id']}/comments", {"text": text})
    return True


def sweep(dry_run=False):
    client = PyrusClient()

    catalog_id = client.find_catalog_id(SMETA_CATALOG_NAME)
    if not catalog_id:
        print(f"Справочник «{SMETA_CATALOG_NAME}» не найден — сначала запустите smeta_catalog_import.py")
        return

    catalog = client.get(f"catalogs/{catalog_id}")
    # {item_id: values} — values в порядке CATALOG_HEADERS.
    rows_by_item_id = {item["item_id"]: item["values"] for item in catalog.get("items", [])}

    # Суммарный спрос по всем НЕотклонённым заявкам формы. include_archived=True —
    # закрытые заявки тоже должны учитываться (см. докстринг модуля).
    #
    # ВАЖНО: реестр (GET /forms/{id}/register) НЕ отдаёт approvals — там
    # есть только fields/current_step. Чтобы узнать, отклонена ли заявка,
    # приходится дозапрашивать каждую задачу отдельно через GET /tasks/{id}
    # (там approvals уже есть) — проверено эмпирически в сессии 2026-08-18
    # (без этого is_rejected() всегда возвращал False, потому что просто
    # не находил поле approvals в данных из реестра).
    register = client.get_register(FORM_ID, include_archived=True)

    demand = {}  # {item_id: суммарное заказанное количество}
    considered_tasks = 0
    rejected_tasks = 0
    missing_notified = 0
    for stub in register.get("tasks", []):
        task = client.get(f"tasks/{stub['id']}")["task"]
        if is_rejected(task):
            rejected_tasks += 1
            continue
        considered_tasks += 1
        for item_id, qty in get_ordered_quantities(task).items():
            demand[item_id] = demand.get(item_id, 0.0) + qty

        if not dry_run and notify_missing_items(client, task):
            missing_notified += 1

    # Считаем новый остаток для каждой строки справочника и собираем те,
    # что реально изменились, в один запрос diff (не отправляем то, что
    # не поменялось — не создаём лишней активности).
    upsert_rows = []
    for item_id, values in rows_by_item_id.items():
        try:
            budget = float(values[COL_BUDGET_QTY])
            current_remainder = float(values[COL_REMAINDER])
        except (ValueError, IndexError):
            continue

        new_remainder = budget - demand.get(item_id, 0.0)
        if abs(new_remainder - current_remainder) < 0.01:
            continue  # не изменилось — строку не трогаем

        new_values = list(values)
        new_values[COL_REMAINDER] = f"{new_remainder:g}"
        upsert_rows.append({"values": new_values})

    print(f"Заявок учтено: {considered_tasks}, отклонённых (пропущены): {rejected_tasks}")
    print(f"Строк справочника «Смета» к обновлению: {len(upsert_rows)} из {len(rows_by_item_id)}")

    if dry_run:
        for row in upsert_rows[:20]:
            v = row["values"]
            print(f"  [{v[COL_KEY]}] {v[COL_NAME][:60]!r}: остаток -> {v[COL_REMAINDER]}")
        print("\n--dry-run: ничего не отправлено в Pyrus.")
        return

    if upsert_rows:
        result = client.post(f"catalogs/{catalog_id}/diff", {"upsert": upsert_rows})
        print(f"Обновлено строк: {len(result.get('updated', []))}")
    else:
        print("Изменений нет.")

    print(f"Оставлено новых напоминаний о недостающих позициях: {missing_notified}")


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    sweep(dry_run=dry_run)


if __name__ == "__main__":
    main()
