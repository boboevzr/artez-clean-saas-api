"""Кастомное aiogram FSM-хранилище поверх Postgres (таблица bot_fsm_state).

Нужно, потому что один процесс (этот же FastAPI-сервис) обслуживает вебхуки
ботов ВСЕХ компаний одновременно через общий Dispatcher — состояние диалога
должно переживать рестарт/деплой процесса, а не жить только в памяти
(как aiogram MemoryStorage по умолчанию). Redis в проекте нет, поэтому
состояние храним там же, где всё остальное — в Postgres.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

import database as db


def _key_to_str(key: StorageKey) -> str:
    return ":".join([
        str(key.bot_id), str(key.chat_id), str(key.user_id),
        str(key.thread_id or 0), key.business_connection_id or "", key.destiny,
    ])


class PostgresStorage(BaseStorage):
    """StorageKey.bot_id уникален на компанию (у каждой компании свой бот-токен),
    поэтому company_id отдельно в ключе хранить не нужно — bot_id уже разделяет
    состояния между компаниями."""

    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        if not db.pool:
            return
        state_str = state.state if isinstance(state, State) else state
        async with db.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bot_fsm_state (key, state, updated_at) VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET state = EXCLUDED.state, updated_at = NOW()
            """, _key_to_str(key), state_str)

    async def get_state(self, key: StorageKey) -> str | None:
        if not db.pool:
            return None
        async with db.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT state FROM bot_fsm_state WHERE key=$1", _key_to_str(key))

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not db.pool:
            return
        async with db.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bot_fsm_state (key, data, updated_at) VALUES ($1, $2::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
            """, _key_to_str(key), json.dumps(dict(data)))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        if not db.pool:
            return {}
        async with db.pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT data FROM bot_fsm_state WHERE key=$1", _key_to_str(key))
        if not raw:
            return {}
        return json.loads(raw) if isinstance(raw, str) else dict(raw)

    async def close(self) -> None:
        pass
