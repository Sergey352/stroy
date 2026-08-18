#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Загружает смету объекта (Excel-файл) в общий справочник Pyrus «Смета».

ОБЩАЯ ИДЕЯ АРХИТЕКТУРЫ (решено в сессии 2026-08-18):
  Справочник «Смета» — ОДИН на все объекты (не по одному справочнику на
  объект — так поле формы «Номенклатура» можно один раз привязать к этому
  справочнику в конструкторе Pyrus и больше никогда не трогать конструктор,
  даже когда появляются новые объекты). Разные объекты различаются внутри
  справочника колонкой «Объект» — её значение читается автоматически из
  самого файла сметы (ячейка A4 листа), вручную ничего вводить не нужно.

  Когда снабженец загружает файл сметы нового объекта — его позиции
  ДОБАВЛЯЮТСЯ в справочник (остальные объекты не трогаются).
  Когда загружается ОБНОВЛЁННЫЙ файл сметы уже существующего объекта —
  существующие строки этого объекта ОБНОВЛЯЮТСЯ, а не дублируются (см.
  ниже про ключ строки и про сохранение остатка).

КЛЮЧ СТРОКИ СПРАВОЧНИКА:
  Первая (ключевая) колонка справочника — это не человекочитаемое имя, а
  технический хэш от (Объект, Наименование, Категория 2-го уровня). Это
  сделано специально: если снабженец загрузит тот же файл (или его
  исправленную версию с теми же позициями) повторно, для каждой позиции
  посчитается ТОТ ЖЕ хэш — и Pyrus API (POST /catalogs/{id}/diff, upsert)
  обновит существующую строку вместо создания дубликата. Человек в справке
  Pyrus видит не хэш, а обычные колонки «Объект» и «Наименование».

СОХРАНЕНИЕ ОСТАТКА ПРИ ПОВТОРНОЙ ЗАГРУЗКЕ:
  Колонка «Остаток» — это не просто копия «Кол-во по смете», а живой
  счётчик, который уменьшают боты при подаче заявок (эта логика — в
  budget_remainder_bot.py, там ещё не подключено, см. TODO в CLAUDE.md).
  Если слепо перезаписывать «Остаток» = «Кол-во по смете» при каждой
  повторной загрузке файла, мы бы стирали историю уже поданных заявок.
  Поэтому при повторной загрузке скрипт:
    1. Считывает текущее состояние справочника (что там уже есть).
    2. Для позиций, которые уже существуют (тот же ключ) — считает
       разницу (дельту) между новым и старым «Кол-во по смете» и
       прибавляет эту дельту к текущему «Остатку» (а не переписывает
       остаток заново). Так исправление сметы (например, увеличили
       количество на 10) увеличит и остаток на 10, а не обнулит историю.
    3. Для новых позиций — остаток = полное количество по смете (пока
       ничего не заказано).

ЛИМИТ РАЗМЕРА СТРОКИ СПРАВОЧНИКА:
  У Pyrus есть недокументированный лимит — не больше 500 символов суммарно
  на все колонки одной строки справочника (обнаружено эмпирически в
  сессии 2026-08-17). В проверенном файле 95% наименований короче 152
  символов, но есть единичные (7 из ~2065) длиной 400+ символов — с ними
  полная строка может не влезть в лимит. Скрипт такие строки обрезает
  (с пометкой "…") и отдельно печатает предупреждение, чтобы вы могли
  проверить их вручную при необходимости.

Запуск:
    export PYRUS_LOGIN="pyrus_demo@outlook.com"
    export PYRUS_SECURITY_KEY="ваш_секретный_ключ"
    python3 smeta_catalog_import.py <путь_к_файлу.xlsx> [--dry-run]

    --dry-run — только посчитать и напечатать, что было бы загружено,
                ничего реально не отправляя в Pyrus (полезно для проверки
                нового файла перед реальной загрузкой).
"""

import hashlib
import sys

from pyrus_client import PyrusClient
from smeta_parser import aggregate_positions, parse_smeta_file

CATALOG_NAME = "Смета"
CATALOG_HEADERS = [
    "Ключ",
    "Наименование",
    "Категория 1",
    "Категория 2",
    "Объект",
    "Ед. изм.",
    "Кол-во по смете",
    "Остаток",
    "Цена",
]
MAX_ROW_CHARS = 500  # см. пояснение в докстринге модуля


def make_key(object_name, name, cat2):
    """Технический ключ строки — короткий хэш от (Объект, Наименование,
    Категория 2). Стабилен между запусками: тот же набор данных всегда
    даёт тот же ключ, поэтому повторная загрузка той же позиции обновит
    существующую строку, а не создаст дубликат."""
    raw = f"{object_name}|{name}|{cat2}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:16]


def build_row_values(key, pos, remaining):
    """Собирает список значений строки в порядке CATALOG_HEADERS.
    Если суммарная длина превышает лимит Pyrus (500 символов на строку),
    обрезает Наименование (самую длинную колонку) и возвращает флаг
    truncated=True, чтобы вызывающий код мог предупредить пользователя."""
    name = pos["name"] or ""
    cat1 = pos["cat1"] or ""
    cat2 = pos["cat2"] or ""
    obj = pos["object"] or ""
    unit = pos["unit"] or ""
    qty = f"{pos['qty']:g}"
    remaining_str = f"{remaining:g}"
    price = f"{pos['price']:g}" if pos["price"] is not None else ""

    def total_len(nm):
        return len(key) + len(nm) + len(cat1) + len(cat2) + len(obj) + len(unit) + len(qty) + len(remaining_str) + len(price)

    truncated = False
    if total_len(name) > MAX_ROW_CHARS:
        truncated = True
        overflow = total_len(name) - MAX_ROW_CHARS + 1  # +1 запас на символ "…"
        name = name[: max(0, len(name) - overflow)].rstrip() + "…"

    values = [key, name, cat1, cat2, obj, unit, qty, remaining_str, price]
    return values, truncated


OBJECTS_CATALOG_NAME = "Объекты стройки"


def ensure_objects_registered(client, object_names):
    """Добавляет в справочник «Объекты стройки» те объекты из загружаемой
    сметы, которых там ещё нет — чтобы поле «Объект» формы сразу знало про
    новый объект, не дожидаясь, пока кто-то вручную впишет его в
    справочник. Колонка «Адрес» у автодобавленных объектов остаётся
    пустой (в файле сметы адреса нет — см. CLAUDE.md) — снабженец может
    дозаполнить вручную в самом справочнике Pyrus при необходимости, на
    работу формы пустой адрес не влияет."""
    catalog_id = client.find_catalog_id(OBJECTS_CATALOG_NAME)
    if not catalog_id:
        print(f"  Справочник «{OBJECTS_CATALOG_NAME}» не найден — новые объекты не добавлены")
        return

    catalog = client.get(f"catalogs/{catalog_id}")
    existing_names = {item["values"][0] for item in catalog.get("items", []) if item.get("values")}

    to_add = [name for name in object_names if name and name not in existing_names]
    if not to_add:
        return

    client.post(
        f"catalogs/{catalog_id}/diff",
        {"upsert": [{"values": [name, ""]} for name in to_add]},
    )
    print(f"  Добавлены новые объекты в «{OBJECTS_CATALOG_NAME}»: {to_add}")


def load_existing_rows(client, catalog_id):
    """Читает текущее содержимое справочника «Смета» и возвращает словарь
    {ключ: словарь_с_колонками_по_именам} — удобно для сверки при
    повторной загрузке (см. докстринг модуля про сохранение остатка)."""
    catalog = client.get(f"catalogs/{catalog_id}")
    existing = {}
    for item in catalog.get("items", []):
        values = item.get("values", [])
        if not values:
            continue
        row = dict(zip(CATALOG_HEADERS, values))
        existing[row["Ключ"]] = row
    return existing


def import_file(path, dry_run=False):
    print(f"Разбираю файл: {path}")
    raw_positions = parse_smeta_file(path)
    positions = aggregate_positions(raw_positions)
    objects = sorted(set(p["object"] for p in positions))
    print(f"  сырых позиций: {len(raw_positions)}, после агрегации: {len(positions)}")
    print(f"  объекты в файле: {objects}")

    client = PyrusClient()

    if not dry_run:
        ensure_objects_registered(client, objects)

    catalog_id = client.find_or_create_catalog(CATALOG_NAME, CATALOG_HEADERS)
    print(f"Справочник «{CATALOG_NAME}»: catalog_id={catalog_id}")

    existing = load_existing_rows(client, catalog_id)

    upsert_rows = []
    truncated_names = []
    new_count = 0
    updated_count = 0
    new_keys_this_object = set()

    for pos in positions:
        key = make_key(pos["object"], pos["name"], pos["cat2"])
        new_keys_this_object.add(key)
        old_row = existing.get(key)

        if old_row is None:
            # Новая позиция — остаток = полное количество по смете.
            remaining = pos["qty"]
            new_count += 1
        else:
            # Позиция уже была загружена раньше — сохраняем историю
            # потраченного остатка, применяя только дельту количества
            # по смете (см. докстринг модуля).
            old_qty = float(old_row.get("Кол-во по смете") or 0)
            old_remaining = float(old_row.get("Остаток") or 0)
            delta = pos["qty"] - old_qty
            remaining = old_remaining + delta
            updated_count += 1

        values, truncated = build_row_values(key, pos, remaining)
        if truncated:
            truncated_names.append(pos["name"])
        upsert_rows.append({"values": values})

    # Позиции, которые раньше были в справочнике для ЭТИХ ЖЕ объектов, но
    # в новом файле уже не встречаются — не удаляем автоматически (могут
    # быть уже частично заказаны), только предупреждаем, чтобы вы могли
    # проверить и удалить вручную при необходимости.
    stale = [
        row for key, row in existing.items()
        if row.get("Объект") in objects and key not in new_keys_this_object
    ]

    print(f"\nК загрузке: {len(upsert_rows)} строк (новых: {new_count}, обновляемых: {updated_count})")
    if truncated_names:
        print(f"  ВНИМАНИЕ: {len(truncated_names)} наименований обрезано (превышали лимит в 500 символов на строку):")
        for n in truncated_names:
            print(f"    - {n[:80]}...")
    if stale:
        print(f"  ВНИМАНИЕ: {len(stale)} прежних позиций этих объектов отсутствуют в новом файле (не удалены автоматически):")
        for row in stale[:20]:
            print(f"    - [{row.get('Объект')}] {row.get('Наименование')}")

    if dry_run:
        print("\n--dry-run: ничего не отправлено в Pyrus.")
        return

    result = client.post(f"catalogs/{catalog_id}/diff", {"upsert": upsert_rows})
    added = len(result.get("added", []))
    updated = len(result.get("updated", []))
    print(f"\nГотово. Добавлено строк: {added}, обновлено: {updated}")


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 smeta_catalog_import.py <файл.xlsx> [--dry-run]")
        sys.exit(1)
    path = sys.argv[1]
    dry_run = "--dry-run" in sys.argv[2:]
    import_file(path, dry_run=dry_run)


if __name__ == "__main__":
    main()
