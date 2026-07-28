"""Хендлеры бота заказов для клиентов компании — общий Router на всех компаний.

Первая, минимальная версия: подтверждает, что вся цепочка работает
(вебхук → company_id по секрету → нужный Bot → FSM-хранилище → ответ).
Полный перенос формы заказа/скидок/долгов из старого bot.py — следующий этап.
"""
from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

router = Router(name="order_bot")


@router.message(CommandStart())
async def start(message: Message, company_id: int) -> None:
    await message.answer(
        f"👋 Добро пожаловать! (company_id={company_id})\n\n"
        f"Это тестовый бот заказов — форма заказа появится на следующем этапе."
    )


@router.message(F.text)
async def echo_fallback(message: Message, company_id: int, state: FSMContext) -> None:
    current = await state.get_state()
    await message.answer(f"Получено: {message.text!r} (company_id={company_id}, state={current})")
