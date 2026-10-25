"""Заглушка воркера 'messages'.

Реальные задачи появляются в эпиках 4–8; в стори 1.1 воркер только присутствует
в структуре и в docker-compose (AR33–AR35).
"""

import asyncio


async def main() -> None:
    """No-op цикл: контейнер остаётся живым, задач пока нет."""
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
