#!/usr/bin/env bash
# Удаление внешнего uptime-монитора одной командой (стори 11.2, AC-5.2).
#
# **Зачем отдельный скрипт, а не строка curl в рунбуке.** Требование
# исключения из NFR65 — «монитор удаляется одной командой». Строка в markdown
# не проверяема: CR 2026-10-27 показал, что документ описывал `editMonitor`
# со `status:0` (это ПАУЗА, а не удаление), и собственная проверка рунбука
# («id исчез из getMonitors») не могла пройти. Скрипт исполняется тестом
# против стаб-сервера, поэтому «одна команда» доказывается эффектом.
#
# **Ключ.** Только из окружения (`UPTIME_API_KEY`) — в репозиторий не попадает
# (решение D-11.2-5).
#
# **Использование.**
#   read -rs UPTIME_API_KEY && export UPTIME_API_KEY
#   ./scripts/uptime/delete-monitor.sh <ID>
#
# Коды выхода: 0 — монитор удалён и проверено, что id исчез из аккаунта;
#              1 — удаление не подтвердилось (id всё ещё в getMonitors);
#              2 — нет ключа или id, вызов API не удался.
set -euo pipefail

API_BASE="${UPTIME_API_BASE:-https://api.uptimerobot.com}"

if [ -z "${UPTIME_API_KEY:-}" ]; then
  echo "delete-monitor: не задан UPTIME_API_KEY (панель -> My Settings -> API Settings)" >&2
  exit 2
fi

if [ $# -lt 1 ] || [ -z "${1:-}" ]; then
  echo "delete-monitor: укажите id монитора: $0 <ID>" >&2
  exit 2
fi

MONITOR_ID="$1"

# 1. Удаление. Именно deleteMonitor: editMonitor со status=0 ставит на паузу,
#    запись остаётся в аккаунте и возвращается в getMonitors.
if ! curl -s -X POST "$API_BASE/v2/deleteMonitor" \
  -H 'Content-Type: application/json' \
  -d "{\"api_key\":\"$UPTIME_API_KEY\",\"id\":$MONITOR_ID}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("stat")=="ok" else 1)'; then
  echo "delete-monitor: API не подтвердил удаление монитора $MONITOR_ID" >&2
  exit 2
fi

# 2. Проверка эффекта: id больше не должен встречаться в аккаунте.
if curl -s -X POST "$API_BASE/v2/getMonitors" \
  -H 'Content-Type: application/json' \
  -d "{\"api_key\":\"$UPTIME_API_KEY\",\"format\":\"json\"}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); ids={str(m.get('id')) for m in d.get('monitors',[])}; sys.exit(1 if '$MONITOR_ID' in ids else 0)"; then
  echo "delete-monitor: монитор $MONITOR_ID удалён, в getMonitors его нет"
  exit 0
fi

echo "delete-monitor: монитор $MONITOR_ID всё ещё в аккаунте — удаление не подтверждено" >&2
exit 1
