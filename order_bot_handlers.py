"""Хендлеры бота заказов для клиентов компании — общий Router на всех компаний.

Первый реальный поток, перенесённый из старого монолитного artez_bot/bot.py:
"быстрый заказ" (QuickForm) — язык → меню → услуга → телефон → имя → лид создан
(как и в старом прод-боте — сотрудник обрабатывает лид и конвертирует в заказ вручную
через CRM, бот заказ напрямую не создаёт).

НЕ перенесено (следующий этап): полный заказ (OrderForm) с филиалом/адресом/датой/
временем/замерами, калькулятор, скидки, долги, водительские колбэки, admin-команды,
autodial, live-chat. См. artez_bot/artez_bot/bot.py (только чтение, не редактировать).

Лид создаётся через db.create_lead() — ту же функцию, что использует остальной API
(admin.html, /api/bot/lead и т.д.), с явным company_id (вебхук общий на все компании,
request-scoped contextvar _cid() здесь не работает).
"""
from __future__ import annotations

import logging
import re

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db

router = Router(name="order_bot")


# ══════════════════════════════════════
#  FSM
# ══════════════════════════════════════
class QuickForm(StatesGroup):
    service = State()
    phone   = State()
    name    = State()


# ══════════════════════════════════════
#  ТЕКСТЫ (только ключи, нужные быстрому заказу — не весь словарь прод-бота)
# ══════════════════════════════════════
T = {
    "ru": {
        "hello":          "👋",
        "lang_set":       "🇷🇺 Выбран русский язык",
        "menu_title":     "🏠 Главное меню",
        "btn_order":      "📋 Оформить заказ",
        "ask_service":    "🧺 Выберите услугу:",
        "ask_phone":      "📞 Поделитесь номером или введите вручную:\n\nФормат: +998XXXXXXXXX",
        "btn_share_phone": "📱 Поделиться номером",
        "btn_enter_phone": "⌨️ Ввести другой номер",
        "ask_phone_manual": "✏️ Введите номер в формате:\n+998XXXXXXXXX\n\nПример: +998901234567",
        "phone_invalid":  "⚠️ Неверный формат!\n\nВведите номер строго в формате:\n+998XXXXXXXXX",
        "ask_name":       "👤 Введите ваше имя:",
        "order_done":     "✅ Заказ принят!\n\nМы свяжемся с вами в ближайшее время.",
        "order_failed":   "⚠️ Не удалось сохранить заказ. Попробуйте ещё раз чуть позже.",
        "btn_cancel":     "❌ Отмена",
        "btn_menu":       "🏠 Меню",
        "cancelled":      "❌ Отменено.",
    },
    "uz": {
        "hello":          "👋",
        "lang_set":       "🇺🇿 O'zbek tili tanlandi",
        "menu_title":     "🏠 Asosiy menyu",
        "btn_order":      "📋 Buyurtma berish",
        "ask_service":    "🧺 Xizmatni tanlang:",
        "ask_phone":      "📞 Raqamingizni ulashing yoki qo'lda kiriting:\n\nFormat: +998XXXXXXXXX",
        "btn_share_phone": "📱 Raqamni ulashish",
        "btn_enter_phone": "⌨️ Boshqa raqam kiritish",
        "ask_phone_manual": "✏️ Raqamni quyidagi formatda kiriting:\n+998XXXXXXXXX\n\nMisol: +998901234567",
        "phone_invalid":  "⚠️ Noto'g'ri format!\n\nRaqamni qat'iy formatda kiriting:\n+998XXXXXXXXX",
        "ask_name":       "👤 Ismingizni kiriting:",
        "order_done":     "✅ Buyurtma qabul qilindi!\n\nTez orada siz bilan bog'lanamiz.",
        "order_failed":   "⚠️ Buyurtmani saqlab bo'lmadi. Birozdan keyin qayta urinib ko'ring.",
        "btn_cancel":     "❌ Bekor qilish",
        "btn_menu":       "🏠 Menyu",
        "cancelled":      "❌ Bekor qilindi.",
    },
}


def t(lang: str, key: str) -> str:
    return T.get(lang, T["ru"]).get(key, key)


# ══════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════
def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 Русский язык", callback_data="lang_ru"),
        InlineKeyboardButton(text="🇺🇿 O'zbek tili",  callback_data="lang_uz"),
    ]])


def menu_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_order"), callback_data="menu_order")],
    ])


def cancel_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn_cancel"), callback_data="cancel_order"),
    ]])


def back_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn_menu"), callback_data="go_menu"),
    ]])


def service_kb(lang: str, services: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    if services:
        for s in services:
            name = s.get(f"name_{lang}") or s.get("name_ru") or s["key"]
            emoji = s.get("emoji") or ""
            label = f"{emoji} {name}".strip()
            rows.append([InlineKeyboardButton(text=label, callback_data=f"svc_{s['key']}")])
    else:
        # Фоллбек, если у компании ещё не заполнен каталог услуг
        rows.append([InlineKeyboardButton(
            text=("🧺 Химчистка" if lang == "ru" else "🧺 Kimyoviy tozalash"),
            callback_data="svc_default")])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_cancel"), callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def phone_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text=t(lang, "btn_share_phone"), request_contact=True),
            KeyboardButton(text=t(lang, "btn_enter_phone")),
        ]],
        resize_keyboard=True, one_time_keyboard=True,
    )


# ══════════════════════════════════════
#  ХЕЛПЕРЫ
# ══════════════════════════════════════
_PHONE_RE = re.compile(r"^\+998\d{9}$")


def normalize_phone_bot(raw: str) -> str:
    v = raw.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if v.startswith("998") and not v.startswith("+"):
        v = "+" + v
    return v if _PHONE_RE.match(v) else ""


async def _resolve_lang(tg_id: int, company_id: int, state: FSMContext) -> str | None:
    """Язык из FSM-данных (быстрее) или из БД (после рестарта FSM ещё жива, но на
    всякий случай проверяем и БД — переживает даже сброс состояния)."""
    data = await state.get_data()
    lang = data.get("lang")
    if lang in ("ru", "uz"):
        return lang
    try:
        lang = await db.get_bot_client_lang(tg_id, company_id)
    except Exception as e:
        logging.warning(f"get_bot_client_lang error: {e}")
        lang = None
    return lang if lang in ("ru", "uz") else None


# ══════════════════════════════════════
#  /start и язык
# ══════════════════════════════════════
@router.message(CommandStart())
async def start(message: Message, company_id: int, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    lang = await _resolve_lang(uid, company_id, state)

    try:
        await db.upsert_bot_client(
            tg_id=uid, company_id=company_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            lang=lang or "ru",
        )
    except Exception as e:
        logging.warning(f"upsert_bot_client error: {e}")

    if lang:
        await state.update_data(lang=lang)
        await message.answer(t(lang, "menu_title"), reply_markup=menu_kb(lang))
    else:
        await message.answer(t("ru", "hello"), reply_markup=lang_kb())


@router.callback_query(F.data.in_({"lang_ru", "lang_uz"}))
async def set_language(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    uid = call.from_user.id
    lang = "ru" if call.data == "lang_ru" else "uz"
    await state.update_data(lang=lang)
    try:
        await db.set_bot_client_lang(uid, lang, company_id)
    except Exception as e:
        logging.warning(f"set_bot_client_lang error: {e}")
    await call.message.edit_text(t(lang, "lang_set"))
    await call.message.answer(t(lang, "menu_title"), reply_markup=menu_kb(lang))


@router.callback_query(F.data == "go_menu")
async def go_menu(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    uid = call.from_user.id
    lang = await _resolve_lang(uid, company_id, state)
    if not lang:
        await call.message.answer(t("ru", "hello"), reply_markup=lang_kb())
        return
    await state.update_data(lang=lang)
    await call.message.answer(t(lang, "menu_title"), reply_markup=menu_kb(lang))


@router.callback_query(F.data == "cancel_order")
async def cancel_order(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await state.clear()
    await state.update_data(lang=lang)
    await call.message.answer(t(lang, "cancelled"), reply_markup=menu_kb(lang))


# ══════════════════════════════════════
#  БЫСТРЫЙ ЗАКАЗ: услуга → телефон → имя → сохранение
# ══════════════════════════════════════
@router.callback_query(F.data == "menu_order")
async def menu_order(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    try:
        services = await db.get_services_for_company(company_id)
    except Exception as e:
        logging.warning(f"get_services_for_company error: {e}")
        services = []
    await state.set_state(QuickForm.service)
    await call.message.answer(t(lang, "ask_service"), reply_markup=service_kb(lang, services))


@router.callback_query(QuickForm.service, F.data.startswith("svc_"))
async def quick_service(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    svc_key = call.data.replace("svc_", "")
    await state.update_data(service=svc_key)
    await state.set_state(QuickForm.phone)
    await call.message.answer(t(lang, "ask_phone"), reply_markup=phone_kb(lang))


async def _finish_phone_step(message: Message, company_id: int, state: FSMContext, phone: str) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await state.update_data(phone=phone)
    await state.set_state(QuickForm.name)
    await message.answer("✅", reply_markup=ReplyKeyboardRemove())
    await message.answer(t(lang, "ask_name"), reply_markup=cancel_kb(lang))


@router.message(QuickForm.phone, F.contact)
async def quick_phone_contact(message: Message, company_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    norm = normalize_phone_bot(message.contact.phone_number or "")
    if not norm:
        await message.answer(t(lang, "phone_invalid"), reply_markup=phone_kb(lang))
        return
    await _finish_phone_step(message, company_id, state, norm)


@router.message(QuickForm.phone, F.text)
async def quick_phone_text(message: Message, company_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    raw = (message.text or "").strip()
    if raw == t(lang, "btn_enter_phone"):
        await message.answer(t(lang, "ask_phone_manual"), reply_markup=cancel_kb(lang))
        return
    norm = normalize_phone_bot(raw)
    if not norm:
        await message.answer(t(lang, "phone_invalid"), reply_markup=phone_kb(lang))
        return
    await _finish_phone_step(message, company_id, state, norm)


@router.message(QuickForm.name)
async def quick_name(message: Message, company_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    name = (message.text or "").strip()
    if not name:
        await message.answer(t(lang, "ask_name"), reply_markup=cancel_kb(lang))
        return

    uid = message.from_user.id
    saved = False
    try:
        lead = await db.create_lead({
            "client_name":  name,
            "client_phone": data.get("phone", ""),
            "service":      data.get("service", ""),
            "note":         "Быстрая заявка (бот)",
            "status":       "new",
            "source":       "bot",
            "client_tg_id": uid,
        }, company_id)
        saved = bool(lead)
    except Exception as e:
        logging.error(f"create_lead error: {e}")

    await state.clear()
    await state.update_data(lang=lang)
    if saved:
        await message.answer(t(lang, "order_done"), reply_markup=back_kb(lang))
    else:
        await message.answer(t(lang, "order_failed"), reply_markup=back_kb(lang))


# ══════════════════════════════════════
#  ФОЛЛБЕК: любой другой текст вне известных состояний
# ══════════════════════════════════════
@router.message(F.text)
async def echo_fallback(message: Message, company_id: int, state: FSMContext) -> None:
    """Ловит текст вне известных шагов формы (например, если ждали нажатия кнопки) —
    просто возвращает клиента в главное меню, без ветки полного заказа (см. docstring
    модуля — OrderForm/расширенное меню будет на следующем этапе)."""
    data = await state.get_data()
    lang = data.get("lang") or await _resolve_lang(message.from_user.id, company_id, state) or "ru"
    await state.clear()
    await state.update_data(lang=lang)
    await message.answer(t(lang, "menu_title"), reply_markup=menu_kb(lang))
