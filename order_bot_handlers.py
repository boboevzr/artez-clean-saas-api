"""Хендлеры бота заказов для клиентов компании — общий Router на всех компаний.

Два потока, перенесённые из старого монолитного artez_bot/bot.py:
- "быстрый заказ" (QuickForm) — язык → меню → услуга → телефон → имя → лид создан.
- "полный заказ" (OrderForm) — язык → меню → имя → телефон → филиал → адрес →
  услуга → тип услуги → дата → время → лид создан. Реальный порядок шагов взят
  из старого bot.py (class OrderForm, ~L674 и его хендлеры ~L1941-2253) — там
  этот порядок именно такой (имя и телефон запрашиваются раньше филиала/адреса).
  Упрощение относительно старого бота: без гео-точки/карты (location/web_app) —
  адрес только текстом; филиал берётся динамически из db.get_branches(company_id)
  вместо хардкода "zarafshan"/"navoi".

В обоих случаях — сотрудник обрабатывает лид и конвертирует его в заказ вручную
через CRM, бот заказ напрямую не создаёт. После создания лида сотрудники получают
push + сообщение в TG-группу филиала с кнопкой «Взять лид» (см. cb_take_lead —
использует db.take_lead(), уже company_id-aware).

НЕ перенесено (следующий этап): калькулятор площади/суммы, скидки, долги,
водительские колбэки, admin-команды, autodial, live-chat.
См. artez_bot/artez_bot/bot.py (только чтение, не редактировать).

Лид создаётся через db.create_lead() — ту же функцию, что использует остальной API
(admin.html, /api/bot/lead и т.д.), с явным company_id (вебхук общий на все компании,
request-scoped contextvar _cid() здесь не работает).
"""
from __future__ import annotations

import asyncio
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


class CalcForm(StatesGroup):
    """Калькулятор стоимости — анонимный, ничего не сохраняет (не создаёт лид).
    Порядок шагов перенесён из старого bot.py (class CalcForm, ~L2282-2361):
    услуга → тип услуги → ширина (см) → длина (см) → результат."""
    service      = State()
    service_type = State()
    width        = State()
    length       = State()


class OrderForm(StatesGroup):
    """Полный заказ — реальный порядок шагов из старого bot.py (class OrderForm):
    имя → телефон → филиал → адрес → услуга → тип услуги → дата → время → лид.
    (Старый бот дополнительно спрашивал гео-точку между адресом и услугой —
    здесь опущено, см. docstring модуля.)"""
    name         = State()
    phone        = State()
    branch       = State()
    address      = State()
    service      = State()
    service_type = State()
    date         = State()
    time         = State()
    time_from    = State()   # выбор начала (grid) после «Указать время»
    time_to      = State()   # выбор конца (grid)


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

        # ── Полный заказ (OrderForm) ──
        "btn_order_full": "📅 Заказать с выездом",
        "ask_branch":     "🏢 Выберите филиал:",
        "ask_address":    "📍 Введите адрес (город, улица, дом):",
        "ask_service_type": "⏱ Выберите тип услуги:",
        "btn_type_standard": "🕓 Стандарт",
        "btn_type_express":  "⚡ Экспресс",
        "ask_date":       "📅 Выберите дату самовывоза:",
        "btn_today":      "Сегодня",
        "btn_tomorrow":   "Завтра",
        "btn_pick_date":  "✏️ Указать дату",
        "ask_date_manual": "✏️ Введите дату в формате ДД.ММ.ГГГГ\n\nПример: {example}",
        "date_invalid":   "⚠️ Неверная дата!\n\nВведите дату в формате ДД.ММ.ГГГГ (не раньше сегодняшнего дня).",
        "ask_time":       "🕐 Выберите время самовывоза:",
        "btn_morning":    "🌅 08:00 — 13:00",
        "btn_evening":    "🌆 13:00 — 20:00",
        "btn_custom_time": "✏️ Указать время",
        "ask_time_from":  "🕐 Выберите время начала:",
        "ask_time_to":    "🕐 Выберите время окончания:",
        "full_order_done": "✅ Заявка №{num} принята!\n\nМы свяжемся с вами в ближайшее время.",
        "full_order_failed": "⚠️ Не удалось сохранить заявку. Попробуйте ещё раз чуть позже.",

        # ── Калькулятор стоимости (CalcForm) ──
        "btn_calc":       "🧮 Калькулятор",
        "calc_ask_svc":   "🧮 Калькулятор стоимости\n\nВыберите услугу:",
        "calc_selected_header": "🧮 Калькулятор стоимости\n\nУслуга: {svc}",
        "calc_ask_w":     "Введите ширину в сантиметрах:\n\nПример: 200 (= 2 метра)",
        "calc_ask_l":     "Теперь введите длину в сантиметрах:\n\nПример: 300 (= 3 метра)",
        "calc_result_below_min": "🧮 Расчёт стоимости\n\n📐 Размер: {w} × {l} см = {sqm} {unit}\n{svc}\n💰 {price} сум/{unit}\n\n⚠️ Ваш размер {sqm} {unit} — меньше мин. заказа ({min_order} {unit})\n💵 Итого: {total} сум (за {min_order} {unit})",
        "calc_result_no_min": "🧮 Расчёт стоимости\n\n📐 Размер: {w} × {l} см = {sqm} {unit}\n{svc}\n💰 {price} сум/{unit}\n\n💵 Итого: {total} сум",
        "calc_price_missing": "⚠️ Цена для этой услуги ещё не настроена. Обратитесь в компанию.",
        "invalid_num":    "⚠️ Пожалуйста, введите число. Например: 200",
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

        # ── To'liq buyurtma (OrderForm) ──
        "btn_order_full": "📅 Chiqib olib ketish bilan buyurtma",
        "ask_branch":     "🏢 Filialni tanlang:",
        "ask_address":    "📍 Manzilni kiriting (shahar, ko'cha, uy):",
        "ask_service_type": "⏱ Xizmat turini tanlang:",
        "btn_type_standard": "🕓 Standart",
        "btn_type_express":  "⚡ Ekspress",
        "ask_date":       "📅 Olib ketish sanasini tanlang:",
        "btn_today":      "Bugun",
        "btn_tomorrow":   "Ertaga",
        "btn_pick_date":  "✏️ Sanani kiritish",
        "ask_date_manual": "✏️ Sanani KK.OO.YYYY formatida kiriting\n\nMisol: {example}",
        "date_invalid":   "⚠️ Noto'g'ri sana!\n\nSanani KK.OO.YYYY formatida kiriting (bugungidan oldin bo'lmasin).",
        "ask_time":       "🕐 Olib ketish vaqtini tanlang:",
        "btn_morning":    "🌅 08:00 — 13:00",
        "btn_evening":    "🌆 13:00 — 20:00",
        "btn_custom_time": "✏️ Vaqtni kiritish",
        "ask_time_from":  "🕐 Boshlanish vaqtini tanlang:",
        "ask_time_to":    "🕐 Tugash vaqtini tanlang:",
        "full_order_done": "✅ Ariza №{num} qabul qilindi!\n\nTez orada siz bilan bog'lanamiz.",
        "full_order_failed": "⚠️ Arizani saqlab bo'lmadi. Birozdan keyin qayta urinib ko'ring.",

        # ── Narx kalkulyatori (CalcForm) ──
        "btn_calc":       "🧮 Kalkulyator",
        "calc_ask_svc":   "🧮 Narx kalkulyatori\n\nXizmatni tanlang:",
        "calc_selected_header": "🧮 Narx kalkulyatori\n\nXizmat: {svc}",
        "calc_ask_w":     "Enini santimetrda kiriting:\n\nMisol: 200 (= 2 metr)",
        "calc_ask_l":     "Endi bo'yini santimetrda kiriting:\n\nMisol: 300 (= 3 metr)",
        "calc_result_below_min": "🧮 Narx hisobi\n\n📐 O'lcham: {w} × {l} sm = {sqm} {unit}\n{svc}\n💰 {price} so'm/{unit}\n\n⚠️ Sizning o'lchamingiz {sqm} {unit} — minimal buyurtmadan kam ({min_order} {unit})\n💵 Jami: {total} so'm ({min_order} {unit} uchun)",
        "calc_result_no_min": "🧮 Narx hisobi\n\n📐 O'lcham: {w} × {l} sm = {sqm} {unit}\n{svc}\n💰 {price} so'm/{unit}\n\n💵 Jami: {total} so'm",
        "calc_price_missing": "⚠️ Bu xizmat uchun narx hali sozlanmagan. Kompaniyaga murojaat qiling.",
        "invalid_num":    "⚠️ Iltimos, son kiriting. Masalan: 200",
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
        [InlineKeyboardButton(text=t(lang, "btn_order_full"), callback_data="menu_order_full")],
        [InlineKeyboardButton(text=t(lang, "btn_calc"), callback_data="menu_calc")],
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


# ── Клавиатуры полного заказа (OrderForm) ──
def branch_kb(lang: str, branches: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for b in branches:
        name = b.get(f"name_{lang}") or b.get("name_ru") or b.get("slug", "")
        rows.append([InlineKeyboardButton(text=name, callback_data=f"of_branch_{b['slug']}")])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_cancel"), callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_type_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_type_standard"), callback_data="of_svctype_standard")],
        [InlineKeyboardButton(text=t(lang, "btn_type_express"),  callback_data="of_svctype_express")],
        [InlineKeyboardButton(text=t(lang, "btn_cancel"), callback_data="cancel_order")],
    ])


_WD_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_WD_UZ = ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]


def date_kb(lang: str) -> InlineKeyboardMarkup:
    from datetime import date, timedelta
    today = date.today()
    rows, row = [], []
    for i in range(7):
        d = today + timedelta(days=i)
        date_str = d.strftime("%d.%m.%Y")
        if i == 0:
            label = t(lang, "btn_today") + f" ({d.strftime('%d.%m')})"
        elif i == 1:
            label = t(lang, "btn_tomorrow") + f" ({d.strftime('%d.%m')})"
        else:
            wd = (_WD_UZ if lang == "uz" else _WD_RU)[d.weekday()]
            label = f"{wd} {d.strftime('%d.%m')}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"of_date_{date_str}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=t(lang, "btn_pick_date"), callback_data="of_date_pick")])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_cancel"),    callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def time_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_morning"),     callback_data="of_time_morning")],
        [InlineKeyboardButton(text=t(lang, "btn_evening"),     callback_data="of_time_evening")],
        [InlineKeyboardButton(text=t(lang, "btn_custom_time"), callback_data="of_time_custom")],
        [InlineKeyboardButton(text=t(lang, "btn_cancel"),      callback_data="cancel_order")],
    ])


_TIME_SLOTS = [f"{h:02d}:00" for h in range(8, 20)]  # 08:00..19:00


def time_from_kb(lang: str) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, 12, 3):
        rows.append([InlineKeyboardButton(text=_TIME_SLOTS[j], callback_data=f"of_tslot_from_{j+8}")
                     for j in range(i, i + 3)])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_cancel"), callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def time_to_kb(lang: str, from_h: int) -> InlineKeyboardMarkup:
    slots = [h for h in range(8, 20) if h > from_h]
    rows, row = [], []
    for h in slots:
        row.append(InlineKeyboardButton(text=f"{h:02d}:00", callback_data=f"of_tslot_to_{h}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=t(lang, "btn_cancel"), callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def _notify_staff_new_lead(lead: dict) -> None:
    """Уведомляет сотрудников (веб-пуш + TG-группа филиала) о новом лиде из бота —
    та же функция, что и для лидов с сайта/CRM (admin.html, /api/bot/lead). Ленивый
    импорт из main — на момент вызова (внутри уже обработанного вебхука) main.py
    полностью загружен, обратный импорт на уровне модуля не нужен."""
    if not lead:
        return
    try:
        from main import _notify_new_lead
        bot_staff = {"role": "bot", "first_name": "Telegram", "last_name": "", "login": "bot"}
        asyncio.create_task(_notify_new_lead(lead, bot_staff))
    except Exception as e:
        logging.warning(f"_notify_staff_new_lead error: {e}")


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

    if saved:
        _notify_staff_new_lead(lead)

    await state.clear()
    await state.update_data(lang=lang)
    if saved:
        await message.answer(t(lang, "order_done"), reply_markup=back_kb(lang))
    else:
        await message.answer(t(lang, "order_failed"), reply_markup=back_kb(lang))


# ══════════════════════════════════════
#  ПОЛНЫЙ ЗАКАЗ: имя → телефон → филиал → адрес → услуга → тип → дата → время → сохранение
#  (реальный порядок шагов из старого bot.py, см. docstring модуля и класса OrderForm)
# ══════════════════════════════════════
@router.callback_query(F.data == "menu_order_full")
async def menu_order_full(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await state.set_state(OrderForm.name)
    await call.message.answer(t(lang, "ask_name"), reply_markup=cancel_kb(lang))


@router.message(OrderForm.name)
async def full_name(message: Message, company_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    name = (message.text or "").strip()
    if not name:
        await message.answer(t(lang, "ask_name"), reply_markup=cancel_kb(lang))
        return
    await state.update_data(name=name)
    await state.set_state(OrderForm.phone)
    await message.answer(t(lang, "ask_phone"), reply_markup=phone_kb(lang))


async def _advance_after_phone(message: Message, company_id: int, state: FSMContext, phone: str) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await state.update_data(phone=phone)
    try:
        branches = await db.get_branches(company_id)
    except Exception as e:
        logging.warning(f"get_branches error: {e}")
        branches = []
    await message.answer("✅", reply_markup=ReplyKeyboardRemove())
    if branches:
        await state.set_state(OrderForm.branch)
        await message.answer(t(lang, "ask_branch"), reply_markup=branch_kb(lang, [dict(b) for b in branches]))
    else:
        # У компании ещё не заведены филиалы — пропускаем шаг, branch останется пустым
        await state.set_state(OrderForm.address)
        await message.answer(t(lang, "ask_address"), reply_markup=cancel_kb(lang))


@router.message(OrderForm.phone, F.contact)
async def full_phone_contact(message: Message, company_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    norm = normalize_phone_bot(message.contact.phone_number or "")
    if not norm:
        await message.answer(t(lang, "phone_invalid"), reply_markup=phone_kb(lang))
        return
    await _advance_after_phone(message, company_id, state, norm)


@router.message(OrderForm.phone, F.text)
async def full_phone_text(message: Message, company_id: int, state: FSMContext) -> None:
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
    await _advance_after_phone(message, company_id, state, norm)


@router.callback_query(OrderForm.branch, F.data.startswith("of_branch_"))
async def full_branch(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    slug = call.data[len("of_branch_"):]
    await state.update_data(branch=slug)
    await state.set_state(OrderForm.address)
    await call.message.answer(t(lang, "ask_address"), reply_markup=cancel_kb(lang))


@router.message(OrderForm.address)
async def full_address(message: Message, company_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    address = (message.text or "").strip()
    if not address:
        await message.answer(t(lang, "ask_address"), reply_markup=cancel_kb(lang))
        return
    await state.update_data(address=address)
    try:
        services = await db.get_services_for_company(company_id)
    except Exception as e:
        logging.warning(f"get_services_for_company error: {e}")
        services = []
    await state.set_state(OrderForm.service)
    await message.answer(t(lang, "ask_service"), reply_markup=service_kb(lang, services))


@router.callback_query(OrderForm.service, F.data.startswith("svc_"))
async def full_service(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    svc_key = call.data.replace("svc_", "")
    await state.update_data(service=svc_key)
    await state.set_state(OrderForm.service_type)
    await call.message.answer(t(lang, "ask_service_type"), reply_markup=service_type_kb(lang))


@router.callback_query(OrderForm.service_type, F.data.startswith("of_svctype_"))
async def full_service_type(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    kind = call.data.replace("of_svctype_", "")
    label = t(lang, "btn_type_standard") if kind == "standard" else t(lang, "btn_type_express")
    await state.update_data(service_type=label)
    await state.set_state(OrderForm.date)
    await call.message.answer(t(lang, "ask_date"), reply_markup=date_kb(lang))


@router.callback_query(OrderForm.date, F.data.startswith("of_date_") & (F.data != "of_date_pick"))
async def full_date_btn(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    date_val = call.data[len("of_date_"):]
    await state.update_data(date=date_val)
    await state.set_state(OrderForm.time)
    await call.message.answer(t(lang, "ask_time"), reply_markup=time_kb(lang))


@router.callback_query(OrderForm.date, F.data == "of_date_pick")
async def full_date_pick(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    from datetime import date as _dt, timedelta
    example = (_dt.today() + timedelta(days=7)).strftime("%d.%m.%Y")
    await call.message.answer(t(lang, "ask_date_manual").format(example=example), reply_markup=cancel_kb(lang))


@router.message(OrderForm.date)
async def full_date_manual(message: Message, company_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    text = (message.text or "").strip()
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    valid = False
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            from datetime import date as dt_date
            d = dt_date(year, month, day)
            if d >= dt_date.today():
                valid = True
        except ValueError:
            valid = False
    if not valid:
        await message.answer(t(lang, "date_invalid"), reply_markup=cancel_kb(lang))
        return
    await state.update_data(date=text)
    await state.set_state(OrderForm.time)
    await message.answer(t(lang, "ask_time"), reply_markup=time_kb(lang))


async def _finish_full_order(message: Message, company_id: int, state: FSMContext,
                              time_txt: str, tg_user) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    uid = tg_user.id if tg_user else None

    note_parts = ["Полная заявка (бот)"]
    if data.get("service_type"):
        note_parts.append(f"Тип: {data['service_type']}")

    saved = False
    lead = None
    try:
        lead = await db.create_lead({
            "client_name":  data.get("name", ""),
            "client_phone": data.get("phone", ""),
            "service":      data.get("service", ""),
            "branch":       data.get("branch", ""),
            "address":      data.get("address", ""),
            "note":         " · ".join(note_parts),
            "status":       "new",
            "source":       "bot",
            "client_tg_id": uid,
            "pickup_date":  data.get("date", ""),
            "pickup_time":  time_txt,
        }, company_id)
        saved = bool(lead)
    except Exception as e:
        logging.error(f"create_lead error (full order): {e}")

    if saved:
        _notify_staff_new_lead(lead)

    await state.clear()
    await state.update_data(lang=lang)
    if saved:
        await message.answer(
            t(lang, "full_order_done").format(num=lead.get("lead_num", "")),
            reply_markup=back_kb(lang))
    else:
        await message.answer(t(lang, "full_order_failed"), reply_markup=back_kb(lang))


@router.callback_query(OrderForm.time, F.data.in_({"of_time_morning", "of_time_evening", "of_time_custom"}))
async def full_time_choice(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    if call.data == "of_time_morning":
        await _finish_full_order(call.message, company_id, state, "08:00 — 13:00", call.from_user)
    elif call.data == "of_time_evening":
        await _finish_full_order(call.message, company_id, state, "13:00 — 20:00", call.from_user)
    else:
        await state.set_state(OrderForm.time_from)
        await call.message.answer(t(lang, "ask_time_from"), reply_markup=time_from_kb(lang))


@router.callback_query(OrderForm.time_from, F.data.startswith("of_tslot_from_"))
async def full_time_from(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    from_h = int(call.data.split("_")[-1])
    await state.update_data(time_from_h=from_h)
    await state.set_state(OrderForm.time_to)
    await call.message.answer(t(lang, "ask_time_to"), reply_markup=time_to_kb(lang, from_h))


@router.callback_query(OrderForm.time_to, F.data.startswith("of_tslot_to_"))
async def full_time_to(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    from_h = data.get("time_from_h", 8)
    to_h = int(call.data.split("_")[-1])
    time_txt = f"{from_h:02d}:00 — {to_h:02d}:00"
    await _finish_full_order(call.message, company_id, state, time_txt, call.from_user)


# ══════════════════════════════════════
#  КАЛЬКУЛЯТОР СТОИМОСТИ (CalcForm) — услуга → тип услуги → ширина → длина →
#  результат. Анонимный, ничего не сохраняет, никакого лида не создаёт —
#  чисто информационный расчёт (площадь × цена). Перенесено из старого
#  bot.py (class CalcForm, ~L2282-2361), без module-level кэша цен: бот общий
#  на все компании в одном процессе, поэтому цены/юниты берутся заново на
#  шаге показа результата через db.get_all_prices()/db.get_all_units()
#  (contextvar company_id уже резолвится корректно к моменту вызова).
# ══════════════════════════════════════

# Фолбек символов единиц измерения — только для отображения, если в таблице
# units почему-то нет нужной записи (в норме она сидируется при init_db).
_UNIT_SYMBOL_FALLBACK = {
    "m2":  {"symbol_ru": "м²", "symbol_uz": "m²"},
    "m":   {"symbol_ru": "м",  "symbol_uz": "m"},
    "pcs": {"symbol_ru": "шт", "symbol_uz": "dona"},
    "cm":  {"symbol_ru": "см", "symbol_uz": "sm"},
    "cm2": {"symbol_ru": "см²", "symbol_uz": "sm²"},
    "kg":  {"symbol_ru": "кг", "symbol_uz": "kg"},
}


def _svc_display_name(lang: str, services: list[dict], svc_key: str) -> str:
    """Название услуги (эмодзи + имя на нужном языке) по её ключу, из уже
    полученного списка услуг компании. Фоллбек — сам ключ, если услугу
    почему-то не нашли (например, каталог поменялся между шагами)."""
    for s in services:
        if s.get("key") == svc_key:
            name = s.get(f"name_{lang}") or s.get("name_ru") or svc_key
            emoji = s.get("emoji") or ""
            return f"{emoji} {name}".strip()
    return svc_key


async def _unit_symbol(unit_key: str, lang: str) -> str:
    try:
        units = await db.get_all_units()
    except Exception as e:
        logging.warning(f"get_all_units error: {e}")
        units = []
    for u in units:
        if u["key"] == unit_key:
            return u["symbol_uz"] if lang == "uz" else u["symbol_ru"]
    fallback = _UNIT_SYMBOL_FALLBACK.get(unit_key) or _UNIT_SYMBOL_FALLBACK["m2"]
    return fallback["symbol_uz"] if lang == "uz" else fallback["symbol_ru"]


@router.callback_query(F.data == "menu_calc")
async def menu_calc(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    try:
        services = await db.get_services_for_company(company_id)
    except Exception as e:
        logging.warning(f"get_services_for_company error: {e}")
        services = []
    await state.set_state(CalcForm.service)
    await call.message.answer(t(lang, "calc_ask_svc"), reply_markup=service_kb(lang, services))


@router.callback_query(CalcForm.service, F.data.startswith("svc_"))
async def calc_service(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    svc_key = call.data.replace("svc_", "")
    try:
        services = await db.get_services_for_company(company_id)
    except Exception as e:
        logging.warning(f"get_services_for_company error: {e}")
        services = []
    svc_name = _svc_display_name(lang, services, svc_key)
    await state.update_data(calc_svc=svc_key, calc_svc_name=svc_name)
    await state.set_state(CalcForm.service_type)
    await call.message.answer(t(lang, "ask_service_type"), reply_markup=service_type_kb(lang))


@router.callback_query(CalcForm.service_type, F.data.startswith("of_svctype_"))
async def calc_service_type(call: CallbackQuery, company_id: int, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    lang = data.get("lang", "ru")
    svctype = call.data.replace("of_svctype_", "")
    type_label = t(lang, "btn_type_standard") if svctype == "standard" else t(lang, "btn_type_express")
    await state.update_data(calc_svctype=svctype, calc_svctype_label=type_label)
    await state.set_state(CalcForm.width)
    svc_display = f"{data.get('calc_svc_name', '')} ({type_label})".strip()
    header = t(lang, "calc_selected_header").format(svc=svc_display)
    await call.message.answer(header + "\n\n" + t(lang, "calc_ask_w"), reply_markup=cancel_kb(lang))


@router.message(CalcForm.width)
async def calc_width(message: Message, company_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    raw = (message.text or "").strip()
    try:
        width = float(raw.replace(",", "."))
    except ValueError:
        await message.answer(t(lang, "invalid_num"))
        return
    await state.update_data(calc_w=width)
    await state.set_state(CalcForm.length)
    svc_display = f"{data.get('calc_svc_name', '')} ({data.get('calc_svctype_label', '')})".strip()
    header = t(lang, "calc_selected_header").format(svc=svc_display)
    await message.answer(header + "\n\n" + t(lang, "calc_ask_l"), reply_markup=cancel_kb(lang))


@router.message(CalcForm.length)
async def calc_length(message: Message, company_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang", "ru")
    raw = (message.text or "").strip()
    try:
        length = float(raw.replace(",", "."))
    except ValueError:
        await message.answer(t(lang, "invalid_num"))
        return

    svc = data.get("calc_svc", "")
    svctype = data.get("calc_svctype", "standard")
    width = data.get("calc_w", 0.0)

    try:
        prices = await db.get_all_prices()
    except Exception as e:
        logging.warning(f"get_all_prices error: {e}")
        prices = {}
    entry = prices.get(svc, {}).get(svctype)

    if not entry:
        await state.clear()
        await state.update_data(lang=lang)
        await message.answer(t(lang, "calc_price_missing"), reply_markup=back_kb(lang))
        return

    sqm_real = (width / 100) * (length / 100)
    min_order = entry.get("min_order")
    sqm_bill = max(sqm_real, min_order) if min_order else sqm_real
    price = entry["price"]
    total = int(sqm_bill * price)
    unit_sym = await _unit_symbol(entry.get("unit_key") or "m2", lang)
    svc_display = f"{data.get('calc_svc_name', '')} ({data.get('calc_svctype_label', '')})".strip()

    fmt_args = dict(
        w=int(width), l=int(length), sqm=round(sqm_real, 2), unit=unit_sym,
        svc=svc_display,
        price=f"{price:,}".replace(",", " "),
        total=f"{total:,}".replace(",", " "),
        min_order=min_order,
    )
    if min_order and sqm_real < min_order:
        result = t(lang, "calc_result_below_min").format(**fmt_args)
    else:
        result = t(lang, "calc_result_no_min").format(**fmt_args)

    await state.clear()
    await state.update_data(lang=lang)
    await message.answer(result, reply_markup=back_kb(lang))


# ══════════════════════════════════════
#  СОТРУДНИКИ: «Взять лид» — кнопка в уведомлении о новом лиде (_notify_new_lead,
#  main.py). Логика зеркалит /api/tg/webhook (легаси, single-tenant), но здесь —
#  через db.take_lead(), уже принимающий явный company_id.
# ══════════════════════════════════════
@router.callback_query(F.data.startswith("take_lead_"))
async def cb_take_lead(call: CallbackQuery, company_id: int) -> None:
    try:
        lead_id = int(call.data.replace("take_lead_", ""))
    except ValueError:
        await call.answer("Ошибка", show_alert=True)
        return

    staff = await db.get_staff_by_tg_id_and_company(call.from_user.id, company_id)
    if not staff:
        await call.answer("Ваш Telegram не привязан к аккаунту сотрудника.", show_alert=True)
        return
    if staff.get("role") == "agent":
        await call.answer("Агенты не могут брать лиды через Telegram.", show_alert=True)
        return

    staff_id = staff["id"]
    staff_name = f"{staff.get('first_name','')} {staff.get('last_name','')}".strip() or staff.get("login", "")

    try:
        status, taker_name, taker_verb = await db.take_lead(lead_id, staff_id, staff_name, company_id)
    except Exception as e:
        logging.error(f"take_lead error: {e}")
        await call.answer("Ошибка сервера. Попробуйте ещё раз.", show_alert=True)
        return

    orig_text = call.message.text or call.message.caption or ""

    if status == "not_found":
        await call.answer("Лид не найден", show_alert=True)
    elif status == "taken":
        await call.answer(f"Лид уже взят: {taker_name or 'другой сотрудник'}", show_alert=True)
        new_text = orig_text.rstrip("━").rstrip() + f"\n━━━━━━━━━━\n✅ {taker_verb}: {taker_name or 'другой сотрудник'}"
        try:
            await call.message.edit_text(new_text)
        except Exception:
            pass
    elif status == "already_mine":
        await call.answer("Этот лид уже ваш!")
    elif status == "ok":
        took_verb = "Взяла" if staff.get("gender") == "F" else "Взял"
        await call.answer("Лид взят! Откройте приложение.")
        new_text = orig_text.rstrip("━").rstrip() + f"\n━━━━━━━━━━\n✅ {took_verb}: {staff_name}"
        try:
            await call.message.edit_text(new_text)
        except Exception:
            pass
    else:
        await call.answer("Ошибка сервера. Попробуйте ещё раз.", show_alert=True)


# ══════════════════════════════════════
#  ФОЛЛБЕК: любой другой текст вне известных состояний
# ══════════════════════════════════════
@router.message(F.text)
async def echo_fallback(message: Message, company_id: int, state: FSMContext) -> None:
    """Ловит текст вне известных шагов формы (например, если ждали нажатия кнопки) —
    просто возвращает клиента в главное меню."""
    data = await state.get_data()
    lang = data.get("lang") or await _resolve_lang(message.from_user.id, company_id, state) or "ru"
    await state.clear()
    await state.update_data(lang=lang)
    await message.answer(t(lang, "menu_title"), reply_markup=menu_kb(lang))
