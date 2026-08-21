#!/bin/sh
# Простой цикл вместо системного cron/GitHub Actions — на своём сервере
# (сессия 2026-08-18) прогоняет ботов по расписанию раз в CRON_INTERVAL_SECONDS
# секунд (по умолчанию 900 = 15 минут, как было в GitHub Actions).
#
# Ловит то, что вебхук (webhook_server.py) не видит: правки уже созданных
# заявок (Pyrus не переиспускает вебхук на повторные правки, только на
# новые "ожидающие действия" — см. докстринг webhook_server.py), а также
# служит подстраховкой на случай, если вебхук временно недоступен.
#
# Используется в docker-compose.yml как command сервиса "cron" (тот же
# Docker-образ, что и у вебхука — код и зависимости одни и те же, разница
# только в том, что запускается).

INTERVAL="${CRON_INTERVAL_SECONDS:-900}"

echo "cron_loop.sh: старт, интервал ${INTERVAL} сек."

while true; do
  echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') : запуск ботов ==="
  python3 smeta_remainder_bot.py
  python3 overdue_reminder_bot.py
  python3 upd_reconciliation_bot.py
  python3 supplier_documents_bot.py
  echo "=== готово, следующий запуск через ${INTERVAL} сек. ==="
  sleep "$INTERVAL"
done
