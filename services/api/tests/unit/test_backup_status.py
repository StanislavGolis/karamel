"""Свежесть бэкапа в `/health/deep` — границы 26 ч и режимы отказа (стори 11.10, AC-4).

**Почему unit, а не integration.** `backup_freshness` не трогает БД и не пишет
в S3: она вызывает `list_objects_v2` и сравнивает `LastModified` с порогом.
Живой S3 в тесте проверял бы провайдера, а не логику порога.

**Что доказывается обеими сторонами границы (P-25).** 25 ч → `ok`, 27 ч →
`stale`. Односторонний тест (`27 ч → stale`) зеленел бы и при пороге 26
минут, и при пороге `ok`. Порог берётся из константы модуля, а не из
литерала в тесте: иначе тест и код могли бы «сойтись» на 26 при разных
значениях в будущем.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.services import backup_status as module


def _fake_s3(monkeypatch: pytest.MonkeyPatch, ages_hours: list[float]) -> None:
    """Подменяет `boto3.client` объектом, отдающим объекты заданного возраста.

    Возраст считается от `datetime.now(UTC)` в момент вызова — те же часы, что
    у боевого пути, поэтому проверяется НАСТОЯЩАЯ арифметика разницы, а не
    подставленное число.
    """

    class _Paginator:
        def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
            # Префикс обязан быть `db/`: проверка свежести, смотрящая не туда,
            # всегда говорила бы `stale` на исправном канале (AC-2).
            assert Prefix == module.BACKUP_PREFIX
            now = datetime.datetime.now(datetime.UTC)
            return [
                {
                    "Contents": [
                        {
                            "Key": f"db/karamel-{index}.dump",
                            "LastModified": now - datetime.timedelta(hours=age),
                        }
                        for index, age in enumerate(ages_hours)
                    ]
                }
            ]

    class _Client:
        def get_paginator(self, name: str) -> _Paginator:
            assert name == "list_objects_v2"
            return _Paginator()

    # Реквизиты задаются подменой настроек: без них функция выходит раньше
    # (`fail:NotConfigured`), и тест порога не дошёл бы до сравнения.
    monkeypatch.setattr(module.settings, "s3_backup_endpoint", "https://s3.example")
    monkeypatch.setattr(module.settings, "s3_backup_region", "ru-1")
    monkeypatch.setattr(module.settings, "s3_backup_bucket", "karamel-backup")
    monkeypatch.setattr(module.settings, "s3_backup_access_key", "key")
    monkeypatch.setattr(module.settings, "s3_backup_secret_key", "secret")
    monkeypatch.setattr(module.boto3, "client", lambda *args, **kwargs: _Client())


async def test_fresh_backup_within_threshold_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-8 (AC-4): свежий бэкап (25 ч) → `ok` — сторона «ещё не устарел».

    Без этой стороны порог можно было бы поставить в 1 час, и все проверки
    «устарел» остались бы зелёными.
    """
    _fake_s3(monkeypatch, ages_hours=[25.0])

    assert await module.backup_freshness() == module.OK


async def test_stale_backup_beyond_threshold_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-7 (AC-4): бэкап старше порога (27 ч) → `stale` — сторона «устарел».

    27, а не 26: ровно 26 ч — граница, и сравнение `<=` считает её свежей.
    Проверять саму границу обеими сторонами значит взять 26 и 26+эпсилон;
    целые часы 25/27 дают по часу запаса с каждой стороны и не зависят от
    точности исполнения.
    """
    _fake_s3(monkeypatch, ages_hours=[27.0])

    assert await module.backup_freshness() == module.STALE


async def test_newest_object_wins_over_older_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-4: возраст считается по САМОМУ СВЕЖЕМУ объекту, а не по любому.

    Ретенция S3 держит 30 дней (Б-2), значит в бакете одновременно лежат
    старые дампы. Если бы проверка брала первый попавшийся (или последний),
    исправный канал краснел бы из-за собственной ретенции.
    """
    _fake_s3(monkeypatch, ages_hours=[27.0, 200.0, 2.0])

    assert await module.backup_freshness() == module.OK


async def test_no_objects_means_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-4: бакет доступен, но объектов нет → `stale`, а не `fail` и не `ok`.

    Это различие решает, кого звать: `stale` — «cron не отработал» (данных
    бэкапа нет), `fail` — «легло хранилище». Слить их значило бы послать
    дежурного не туда.
    """

    class _EmptyClient:
        def get_paginator(self, name: str) -> Any:
            class _P:
                def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
                    return [{}]

            return _P()

    monkeypatch.setattr(module.settings, "s3_backup_endpoint", "https://s3.example")
    monkeypatch.setattr(module.settings, "s3_backup_region", "ru-1")
    monkeypatch.setattr(module.settings, "s3_backup_bucket", "karamel-backup")
    monkeypatch.setattr(module.settings, "s3_backup_access_key", "key")
    monkeypatch.setattr(module.settings, "s3_backup_secret_key", "secret")
    monkeypatch.setattr(module.boto3, "client", lambda *args, **kwargs: _EmptyClient())

    assert await module.backup_freshness() == module.STALE


async def test_storage_error_is_fail_not_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-4: недоступность S3 → `fail:<Тип>`, НЕ `stale`.

    `stale` означает «бэкапа нет дольше суток» — инцидент данных с действием
    «проверь cron». Отказ хранилища — другой инцидент и другой дежурный.
    """
    _fake_s3(monkeypatch, ages_hours=[1.0])

    def _broken(*args: Any, **kwargs: Any) -> Any:
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")

    monkeypatch.setattr(module.boto3, "client", _broken)

    assert await module.backup_freshness() == "fail:ClientError"


async def test_missing_credentials_is_fail_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-4: пустые реквизиты в dev → `fail:NotConfigured`, а не `stale` и не `ok`.

    `ok` был бы ложью («всё хорошо», хотя бэкап не проверялся), `stale` —
    ложным инцидентом в каждом локальном прогоне и в CI.
    """
    monkeypatch.setattr(module.settings, "s3_backup_endpoint", "")
    monkeypatch.setattr(module.settings, "s3_backup_region", "")
    monkeypatch.setattr(module.settings, "s3_backup_bucket", "")
    monkeypatch.setattr(module.settings, "s3_backup_access_key", "")
    monkeypatch.setattr(module.settings, "s3_backup_secret_key", "")

    assert await module.backup_freshness() == "fail:NotConfigured"


def test_threshold_is_twenty_six_hours() -> None:
    """AC-4: порог объявлен ОДНОЙ константой и равен 26 ч.

    Литерал на месте сравнения не проверялся бы ничем: тест порога брал бы
    значение оттуда же, откуда код, и «сошёлся» бы на любом числе.
    """
    assert module.STALE_AFTER_HOURS == 26


def test_backup_prefix_matches_upload_script() -> None:
    """AC-2: префикс в проверке совпадает с префиксом в скрипте загрузки.

    Разойдись они — проверка смотрела бы в пустоту и всегда говорила `stale`
    на исправном канале. Тест читает ОБА файла: это единственный способ
    поймать рассинхронизацию двух источников без общего модуля.
    """
    from pathlib import Path

    script = Path(__file__).resolve().parents[4] / "scripts" / "backup" / "pg-backup.sh"
    source = script.read_text(encoding="utf-8")

    assert module.BACKUP_PREFIX == "db/"
    assert 'S3_PREFIX="${S3_PREFIX:-db/}"' in source


async def test_exactly_at_threshold_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-8 (AC-4): РОВНО 26 ч — ещё `ok`, граница включительная.

    **Что ловится (CR 11.10, M6).** Прежние тесты держали 25 и 27 часов, а
    саму границу 26.0 не проверял никто: мутация `age <= timedelta` →
    `age < timedelta` выжила. Докстринг соседнего теста утверждал «граница
    проверяется ОБЕИМИ сторонами», но обе стороны стояли на расстоянии часа.

    **Почему подменяется `_last_backup_age`, а не `LastModified`.** Возраст
    объекта в поддельном S3 отсчитывается от собственного `now`, и между ним
    и `now` теста проходят микросекунды: «ровно 26 ч» физически недостижимо —
    получается 26 ч + ε или 26 ч − ε, и мутация `<=`/`<` неразличима. Здесь
    подменяется ИСТОЧНИК возраста, а решает по-прежнему `backup_freshness`:
    проверяется её сравнение, а не арифметика подделки.
    """
    import datetime as dt

    _fake_s3(monkeypatch, ages_hours=[1.0])
    monkeypatch.setattr(
        module, "_last_backup_age", lambda now: dt.timedelta(hours=module.STALE_AFTER_HOURS)
    )

    assert await module.backup_freshness() == module.OK


async def test_just_over_threshold_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-7 (AC-4): 26 ч + 1 с — уже `stale`. Парная сторона границы.

    Пара к тесту выше: вместе они фиксируют границу как `<=`, а не `<` или
    `>`. Односторонний тест мутацию направления не отличает.
    """
    import datetime as dt

    _fake_s3(monkeypatch, ages_hours=[1.0])
    monkeypatch.setattr(
        module,
        "_last_backup_age",
        lambda now: dt.timedelta(hours=module.STALE_AFTER_HOURS, seconds=1),
    )

    assert await module.backup_freshness() == module.STALE


@pytest.mark.parametrize(
    "empty_field",
    [
        "s3_backup_endpoint",
        "s3_backup_region",
        "s3_backup_bucket",
        "s3_backup_access_key",
        "s3_backup_secret_key",
    ],
)
async def test_partial_credentials_is_not_configured(
    monkeypatch: pytest.MonkeyPatch, empty_field: str
) -> None:
    """A-13 (AC-4): ЛЮБОЙ пустой реквизит → `fail:NotConfigured`, а не попытка.

    **Что ловится (CR 11.10, M7).** `_credentials_configured` использует
    `all(...)`, но тест опустошал ВСЕ пять полей сразу — мутация `all` →
    `any` выжила. С `any` четыре заполненных реквизита из пяти считались бы
    достаточными, и клиент пошёл бы в S3 с пустым секретом: не «не настроено»,
    а `fail:...` от хранилища, то есть неверный диагноз инцидента.
    """
    _fake_s3(monkeypatch, ages_hours=[1.0])
    monkeypatch.setattr(module.settings, empty_field, "")

    assert await module.backup_freshness() == "fail:NotConfigured"
