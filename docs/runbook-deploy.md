# Рунбук: деплой на стенд (волна за волной)

**Статус документа.** Это ПРОЦЕДУРА, а не код. Выполнение фиксируется
чек-листом §6, а не зелёным прогоном. Правило «деплой в конце каждой волны,
а не эпика» — обязательство №7 ретро эпика 11 (`W-11.10-g`).

**Стенд:** `nsk-1-vm-fj33` (`129.101.113.246`, `/opt/karamel`), домен
`www.xn--j1accp.xn--p1ai` (`крмл.рф`). SSH:
`ssh -i /root/.ssh/id_ed25519_karamel_host root@129.101.113.246`.
**Стенд НЕ git-репозиторий** — деплой `rsync` + `docker compose build`.

---

## 1. Ломающие миграции: одновременный деплой

**Стори 12.1 (`0035`, `community_slug` → `city_slug`) — ломающая.** После
`ALTER TABLE ... RENAME COLUMN` старый код получает
`ProgrammingError: column "community_slug" does not exist` на каждом запросе
ленты. Это доказано тестом `test_old_code_fails_on_new_schema`
(`tests/integration/test_city_slug_migration_cycle.py`), а не описано словами.

**Правило.** Для ломающих миграций `migrate` и перезапуск `api`/`worker`
происходят в одном окне. Пауза между ними — окно `500` для всех запросов.
На стенде без трафика окно допустимо; на живом трафике — только через
`blue/green` (в пилоте не требуется, зафиксировано решением).

**Длительность блокировки.** `ALTER TABLE ... RENAME COLUMN` берёт
`ACCESS EXCLUSIVE` на таблицу, но не переписывает данные: 40 000 строк
проходят за секунды. Индексы переименовываются метаданными
(`ALTER INDEX ... RENAME TO`), данные не трогаются.

**Порядок для ломающей миграции:**

```bash
# 1. Код на стенд (образы ещё старые, трафика нет — не страшно)
rsync -az --delete --exclude node_modules --exclude .venv \
  ./ root@129.101.113.246:/opt/karamel/

# 2. Собрать ВСЕ образы без аргументов (ловушка W-11.2-o: `build api`
#    оставляет migrate со старым кодом и миграция «проходит» молча)
docker compose build

# 3. Миграция
docker compose run --rm migrate
#    Ожидаемо: `Running upgrade 0034 -> 0035, rename community_slug to city_slug`

# 4. Перезапуск кода в том же окне
docker compose up -d --force-recreate api worker worker-cards
#    НЕ `docker compose start web`: он тянет `migrate` и падает
#    (`Can't locate revision`, W-11.2-o). Для web:
docker compose up -d --no-deps web
```

**Ловушка «exit 0 без применения».** Первый `migrate` после `build api`
вернул exit 0, ничего не применив: сервис `migrate` собирается из своего
образа. Правило: **`docker compose build` без аргументов**, затем `migrate`;
и всегда проверять `alembic_version` после (§5).

---

## 2. Обычные (не ломающие) миграции

Порядок тот же, но окна нет: `build` → `migrate` → `up -d` можно разносить
по времени. Код обязан быть совместим с обеими схемами (`expand/contract`).

---

## 3. Откат

**Ломающая миграция откатывается только вместе с кодом.** Обратное
переименование без отката кода оставляет новый код на старой схеме — те же
`500`, зеркально.

```bash
# 1. Откат схемы на одну ревизию
docker compose run --rm migrate alembic downgrade -1

# 2. Откат кода на предыдущий образ (тег/дайджест фиксируется до деплоя)
docker compose up -d --force-recreate api worker worker-cards
```

### ⚠️ `0037` — откат останавливается, если карточки потеряют смысл

`0037` (стори 12.4) добавила `event_cards.kind`/`activity`. `downgrade`
удаляет колонки, `upgrade` создаёт заново с бэкфиллом `company`/`other`.
Значит цикл «откат → повторный накат» **переписывает** тип и активность
у всех карточек, где они отличались от этой пары: «свидание, кино» станет
«компания, активность неизвестна».

Поэтому `downgrade 0037` **сам останавливается** ДО изменения схемы:

```
Откат 0037 остановлен: у 2 карточек тип встречи или активность не совпадают
с тем, что вернёт повторный накат (`company`/`other`). ...
Если потеря смысла допустима, повторите откат с `ALEMBIC_ALLOW_MEANING_LOSS=1`.
```

Оператор узнаёт о затирании **до** операции, а не после повторного наката.
Порядок действий:

1. **Штатный откат (смысл сохранить):** выгрузить `kind`/`activity` заранее
   (`psql -c 'COPY (SELECT uuid, kind, activity FROM event_cards) TO STDOUT CSV'`),
   откатиться, а после возврата на `0037` — восстановить значения из выгрузки.
2. **Осознанная потеря смысла:** `ALEMBIC_ALLOW_MEANING_LOSS=1 docker compose run --rm migrate alembic downgrade -1`.
   В лог уйдёт предупреждение `осознанный откат 0037`.
3. **Offline-режим (`--sql`) запрещён** для `0037`: без соединения строки
   не посчитать, а печатать разрушительный скрипт без проверки нельзя.

Строки, уже равные `company`/`other` (все карточки до 12.4), откат не
блокируют — гард считает ЗНАЧИМЫЕ строки, а не все.

**Обратимость `0035` проверена замером:** `alembic downgrade -1` возвращает
`community_slug` и точные имена `ck_users_community_slug`,
`uq_places_community_name`, `ix_event_cards_community_starts`,
`idx_places_community_active`, `idx_places_community_cursor`.
Двойной откат до `0033` не падает на именах ограничений — проверяется
`test_double_downgrade_survives_0032`.

---

## 4. Проверка после деплоя

```bash
# Схема: колонка переименована, старых объектов нет
docker compose exec postgres psql -U karamel -d karamel_db -c '\d places' | grep city_slug
docker compose exec postgres psql -U karamel -d karamel_db -c "
  SELECT indexname FROM pg_indexes WHERE indexname LIKE '%community%'
  UNION ALL SELECT conname FROM pg_constraint WHERE conname LIKE '%community%';"
# Ожидаемо: пусто

# Версия
docker compose exec postgres psql -U karamel -d karamel_db -tAc 'SELECT version_num FROM alembic_version'
# Ожидаемо: 0035

# Живой сервис
curl -fsS https://www.xn--j1accp.xn--p1ai/api/health
# Ожидаемо: {"status":"ok"}
```

**Логи:** за 10 минут после деплоя — ноль `ERROR`, ноль `Traceback`, ноль
`ProgrammingError`.

---

## 5. Почему проверка — запрос к БД, а не «команда прошла»

`migrate` вернул exit 0 дважды, когда ничего не применил (`W-11.2-o`,
деплой 2026-10-27). Единственная проверка, которую нельзя обмануть, —
чтение `alembic_version` и каталога схемы. Зелёный код возврата — не факт.

---

## 6. Чек-лист деплоя волны

| # | Шаг | Признак выполнения |
|---|---|---|
| 1 | `rsync` кода на стенд | `git`-лога нет — сверять по `md5sum` ключевого файла |
| 2 | `docker compose build` (без аргументов) | все шесть образов пересобраны |
| 3 | `docker compose run --rm migrate` | `Running upgrade <old> -> <new>` в выводе |
| 4 | `alembic_version` = ожидаемая голова | число из запроса, не из вывода команды |
| 5 | `up -d --force-recreate` затронутых сервисов | `docker compose ps` → `Up` |
| 6 | `curl /api/health` | `{"status":"ok"}` |
| 7 | Логи за 10 минут | 0 `ERROR`/`Traceback` |
| 8 | Для ломающей: старых имён в каталоге нет | запрос §4 вернул пусто |

---

## 7. Связанные документы

- `docs/runbook-backup-restore.md` — бэкап и восстановление БД (перед
  ломающей миграцией делается внеплановый дамп: откат кода не откатит данные).
- `docs/runbook-uptime.md` — внешний монитор (`/api/health`, метод `HEAD`).
- `docs/architecture-overview.md` — схема и сервисы.
