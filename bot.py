"""Telegram-бот: меню для пользователей (подключение, тарифы, промокоды) + админка по /admin."""
import asyncio, html, io, logging, time, traceback, uuid
from urllib.parse import quote
import qrcode
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, BotCommandScopeChat, BotCommandScopeDefault, BufferedInputFile,
                           CallbackQuery, ErrorEvent, InlineKeyboardButton as Btn,
                           InlineKeyboardMarkup as Kb, KeyboardButton as RBtn, Message, ReplyKeyboardMarkup)
import core as c
import rollypay as rp

bot = Bot(c.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
dp = Dispatcher()
router = Router()
dp.include_router(router)
admin = F.from_user.id.in_(c.ADMINS)

APP_STORE = "https://apps.apple.com/app/happ-proxy-utility/id6504287215"
PLAY = "https://play.google.com/store/apps/developer?id=HappDev"
ALL_VERSIONS = "https://github.com/Happ-proxy/happ-desktop"  # в README — ПК, APK, App Store RU
ICON = {"new": "🆕", "active": "🟢", "expired": "⚪️", "banned": "⛔"}
PAGE = 8
PROMO_COUNTS = (1, 10, 25)   # сколько кодов можно создать за раз в админке

class PromoSt(StatesGroup):
    code = State()

class AdmSt(StatesGroup):
    search = State()
    broadcast = State()
    bc_confirm = State()
    pc_days = State()
    pc_devices = State()
    pc_discount = State()
    pc_code = State()
    pc_qty = State()

# ---------- хелперы ----------
def fmt(b): return f"{b / c.GB:.2f} ГБ"

def bar(used, total, n=10):
    k = min(n, round(n * used / total)) if total else 0
    return "▰" * k + "▱" * (n - k)

def dur(sec):
    sec = max(0, int(sec)); d, r = divmod(sec, 86400); h, r = divmod(r, 3600)
    return " ".join(x for x in (f"{d} д" if d else "", f"{h} ч" if d or h else "", f"{r // 60} мин") if x)

def date(ts) -> str: return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))

async def safe_answer(q: CallbackQuery, *args, **kw):
    """q.answer() без падения: если панель отвечала долго, Telegram вернёт «query is too old» — это не ошибка."""
    try: await q.answer(*args, **kw)
    except TelegramBadRequest as e:
        logging.info("callback answer пропущен: %s", e)

async def safe_delete(msg: Message):
    """Удаление без падения (двойной клик, сообщение уже удалено / старше 48 ч)."""
    try: await msg.delete()
    except TelegramBadRequest: pass

async def show(q: CallbackQuery, text: str, kb: Kb):
    """Правит сообщение на месте; если это фото (QR) или правка невозможна — удаляет и шлёт новое."""
    try:
        await q.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" in str(e): return
        await safe_delete(q.message)
        await q.message.answer(text, reply_markup=kb)

# ---------- экраны пользователя ----------
def ch_footer() -> str:
    return (f"\n\n📢 Наш канал — розыгрыши промокодов, новости и обновления: <a href=\"{c.CHANNEL_URL}\">@hutzvpn</a>"
            if c.CHANNEL_URL else "")

def welcome() -> str:
    return (f"<b>{c.BRAND}</b>\n{c.TAGLINE}\n\n"
            "🔒 Защищённое VPN-соединение: шифрование трафика, в том числе в публичных Wi-Fi сетях.\n"
            "⚡ Стабильный доступ к высокоскоростным серверам в Европе.\n\n"
            f"🎁 Бесплатный тест: <b>{c.TRIAL_HOURS} ч · {c.TRIAL_GB:g} ГБ</b>\n"
            "Нажмите «Подключить», чтобы начать.\n"
            "Есть промокод? Нажмите «Активировать промокод».\n\n"
            f"🌐 Сайт сервиса: {c.PUBLIC_URL}{ch_footer()}")

def kb_main() -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text="🚀 Подключить", callback_data="connect")],
        [Btn(text="📊 Статус", callback_data="status"), Btn(text="💳 Тарифы", callback_data="plans")],
        [Btn(text="🎟 Активировать промокод", callback_data="promo")],
        [Btn(text="📄 Документы и поддержка", callback_data="docs")],
    ])

def docs_view() -> tuple[str, Kb]:
    text = ("📄 <b>Документы и поддержка</b>\n\n"
            "Правила сервиса и связь с нами. Если возникли любые вопросы по оплате или подключению — пишите в поддержку.\n\n"
            f"🌐 Официальный сайт: {c.PUBLIC_URL}"
            f"{ch_footer()}")
    kb = Kb(inline_keyboard=[
        [Btn(text="📄 Политика", url=c.PRIVACY_URL), Btn(text="📜 Оферта", url=c.TERMS_URL)],
        [Btn(text="🆘 Поддержка", url=c.SUPPORT_URL)],
        [BACK],
    ])
    return text, kb

# нижняя постоянная клавиатура (Меню, Подключить, Тарифы)
RB_MENU, RB_CONNECT, RB_PLANS = "🏠 Меню", "🚀 Подключить", "💳 Тарифы"
KB_REPLY = ReplyKeyboardMarkup(
    keyboard=[[RBtn(text=RB_MENU), RBtn(text=RB_CONNECT), RBtn(text=RB_PLANS)]],
    resize_keyboard=True, is_persistent=True)

KB_SUPPORT = Kb(inline_keyboard=[[Btn(text="🆘 Поддержка", url=c.SUPPORT_URL)]])

BACK = Btn(text="◀️ Назад", callback_data="menu")

async def _ensure_synced(tg_id: int, username: str | None = None):
    u = c.ensure_user(tg_id, username)
    if u["status"] == "new":
        try:
            u = await c.sync_user_from_h1(tg_id)
        except Exception:
            logging.exception("sync_user_from_h1 failed for %s", tg_id)
    return u

@router.message(Command("start"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    await _ensure_synced(m.from_user.id, m.from_user.username)
    await m.answer("👇 Кнопки быстрого доступа — внизу экрана", reply_markup=KB_REPLY)
    await m.answer(welcome(), reply_markup=kb_main())

@router.callback_query(F.data == "menu")
async def menu(q: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(q, welcome(), kb_main()); await safe_answer(q)

@router.callback_query(F.data == "docs")
async def docs(q: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = docs_view()
    await show(q, text, kb); await safe_answer(q)

def connect_view(u) -> tuple[str, Kb]:
    if u["status"] == "banned":
        return (f"⛔ <b>Доступ заблокирован.</b>\n\nЕсли считаете, что это ошибка — напишите в поддержку.{ch_footer()}",
                Kb(inline_keyboard=[[Btn(text="🆘 Поддержка", url=c.SUPPORT_URL)], [BACK]]))
    if u["status"] != "active":
        title = "⚪️ <b>Срок подписки истёк</b>" if u["status"] == "expired" else "🔒 <b>Подписка не активна</b>"
        text = (f"{title}\n\n"
                "Чтобы подключиться к VPN, сначала оформите подписку в разделе «Тарифы» или активируйте промокод.\n\n"
                f"🆘 Возникли вопросы или не прошла оплата? Напишите в поддержку.{ch_footer()}")
        kb = Kb(inline_keyboard=[
            [Btn(text="💳 Тарифы", callback_data="plans")],
            [Btn(text="🎟 Активировать промокод", callback_data="promo")],
            [Btn(text="🆘 Поддержка", url=c.SUPPORT_URL)],
            [BACK],
        ])
        return text, kb

    text = ("<b>Подключение за 3 шага</b>\n\n"
            "1️⃣ Установите <b>Happ</b>\n"
            "2️⃣ Нажмите «🚀 Открыть в Happ» — подписка добавится сама\n"
            "3️⃣ Выберите сервер и включите VPN\n\n"
            "💡 На мобильном интернете выберите локацию «<b>NL · Мобильная связь</b>» — она работает стабильнее.\n\n"
            f"Ссылка для ручного добавления (тап — скопировать):\n<code>{c.sub_url(u)}</code>\n\n"
            f"📱 Устройств в подписке: <b>{u['device_limit']}</b>. Перед подключением выключите другие VPN.\n"
            f"🆘 Если сервер не отвечает или не получается подключиться — напишите в поддержку.{ch_footer()}")
    kb = Kb(inline_keyboard=[
        [Btn(text="🍎 App Store", url=APP_STORE), Btn(text="🤖 Google Play", url=PLAY)],
        [Btn(text="💻 ПК и другие версии", url=ALL_VERSIONS)],
        [Btn(text="🚀 Открыть в Happ", url=f"{c.PUBLIC_URL}/open/{u['token']}")],
        [Btn(text="📷 QR-код", callback_data="qr")],
        [Btn(text="💳 Тарифы", callback_data="plans")],
        [BACK]])
    return text, kb

@router.callback_query(F.data == "connect")
async def connect(q: CallbackQuery):
    text, kb = connect_view(await _ensure_synced(q.from_user.id, q.from_user.username))
    await show(q, text, kb); await safe_answer(q)

@router.callback_query(F.data == "qr")
async def qr(q: CallbackQuery):
    u = c.ensure_user(q.from_user.id, q.from_user.username)
    buf = io.BytesIO(); qrcode.make(c.sub_url(u)).save(buf, "PNG")
    await safe_delete(q.message)  # текст → фото нельзя через edit, поэтому удаляем и шлём заново
    await q.message.answer_photo(
        BufferedInputFile(buf.getvalue(), "qr.png"),
        caption="📷 В Happ нажмите «+» → «Сканировать QR».",
        reply_markup=Kb(inline_keyboard=[[BACK]]))
    await safe_answer(q)

async def status_view(u) -> tuple[str, Kb]:
    s = u["status"]
    ch_btn = [[Btn(text="📢 Наш канал · Розыгрыши и новости", url=c.CHANNEL_URL)]] if c.CHANNEL_URL else []
    if s == "new":
        return (f"🆕 <b>Подписка ещё не оформлена</b>\n\nОформите тариф или активируйте промокод, чтобы начать пользоваться VPN.{ch_footer()}",
                Kb(inline_keyboard=[[Btn(text="💳 Тарифы", callback_data="plans"),
                                     Btn(text="🎟 Промокод", callback_data="promo")], *ch_btn, [BACK]]))
    if s == "banned":
        return (f"⛔ <b>Доступ заблокирован.</b>\n\nПо всем вопросам пишите в поддержку.{ch_footer()}",
                Kb(inline_keyboard=[[Btn(text="🆘 Поддержка", url=c.SUPPORT_URL)], [BACK]]))
    if s == "expired":
        return (f"⚪️ <b>Срок доступа истёк</b>\n\nПродлить можно в разделе «Тарифы» или промокодом.{ch_footer()}",
                Kb(inline_keyboard=[[Btn(text="💳 Тарифы", callback_data="plans"),
                                     Btn(text="🎟 Промокод", callback_data="promo")], *ch_btn, [BACK]]))
    try: used = await c.usage(u["key_id"])
    except Exception: used = 0
    plan = "тест" if u["plan"] == "trial" else "оплачено"
    disc_line = f"\n🎁 Скидка на след. оплату: <b>-{u['discount_pct']}%</b>" if (u["discount_pct"] or 0) > 0 else ""
    text = (f"🟢 <b>Подписка активна</b> · {plan}\n\n"
            f"{bar(used, u['limit_bytes'])}\n{fmt(used)} / {fmt(u['limit_bytes'])}\n\n"
            f"⏳ Осталось: <b>{dur(u['expires_at'] - c.now())}</b>\n"
            f"📱 Устройства: <b>{len(c.devices_of(u))}/{u['device_limit']}</b>{disc_line}\n\n"
            "💡 <i>На мобильном интернете выберите локацию «NL · Мобильная связь» — она работает стабильнее.</i>\n"
            f"🆘 <i>Не работает сервер или возникли сложности? Напишите в поддержку.</i>{ch_footer()}")
    kb_rows = [
        [Btn(text="🔄 Обновить", callback_data="status"), Btn(text="💳 Продлить", callback_data="plans")],
        *ch_btn,
        [BACK],
    ]
    return text, Kb(inline_keyboard=kb_rows)

@router.callback_query(F.data == "status")
async def status(q: CallbackQuery):
    text, kb = await status_view(await _ensure_synced(q.from_user.id, q.from_user.username))
    await show(q, text, kb); await safe_answer(q)

@router.message(Command("status"))
async def status_cmd(m: Message):
    text, kb = await status_view(await _ensure_synced(m.from_user.id, m.from_user.username))
    await m.answer(text, reply_markup=kb)

# ---------- витрина: Тарифы / Купить (3 шага) ----------
BTN_PROMO = Btn(text="🎟 У меня есть промокод", callback_data="promo")

def item_of(key: str):
    """(название, цена ₽, дней) по ключу тарифа; 'x' — доп. устройство."""
    if key == "x":
        return "➕ Доп. устройство (+1 слот)", c.EXTRA_DEVICE_RUB, c.EXTRA_DEVICE_DAYS
    pl = c.PLANS.get(key)
    return (f"{c.PACK_TITLE[pl['devices']]} · {c.months_text(pl['months'])}", pl["price"], pl["days"]) if pl else None

def includes() -> str:
    return (f"🔒 Защищённое VPN-соединение · 🌍 Локация: {c.LOCATION} · ⚡ {c.traffic_text()} · "
            "📱 устройства — по выбранному пакету")

def support_link(msg: str) -> str:
    """Ссылка на поддержку с подставленным текстом. Учитывает, что в SUPPORT_URL уже может быть «?».
    Telegram игнорирует ?text= в чате с ботом — поэтому в тексте экрана есть копируемый <code>-шаблон."""
    sep = "&" if "?" in c.SUPPORT_URL else "?"
    return f"{c.SUPPORT_URL}{sep}text={quote(msg, safe='')}"

def _fmt_price(base: int, disc: int) -> str:
    if disc > 0:
        return f"<s>{base} ₽</s> <b>{c.apply_discount(base, disc)} ₽</b>"
    return f"<b>{base} ₽</b>"

def plans_view(disc: int = 0) -> tuple[str, Kb]:
    blocks = []
    for d, ms in c.PACKS.items():
        rows = "\n".join(f"  • {c.months_text(m)} ({m * c.MONTH_DAYS} дн.) — {_fmt_price(p, disc)}" for m, p in ms.items())
        blocks.append(f"<b>{c.PACK_TITLE[d]}</b>\n{rows}")
    banner = f"🎁 <b>Ваша скидка -{disc}%</b> применится к оплате!\n\n" if disc > 0 else ""
    text = (f"💳 <b>Тарифы {c.BRAND}</b>\n<i>Шаг 1 из 3 — выберите пакет устройств</i>\n\n"
            f"{banner}В каждом тарифе: {includes()}\n\n" + "\n\n".join(blocks) + "\n\n"
            f"➕ Доп. устройство (+1 слот): {_fmt_price(c.EXTRA_DEVICE_RUB, disc)}/мес за каждое.\n\n"
            "⚡ <i>Активация происходит автоматически сразу после оплаты. При продлении оставшиеся дни суммируются.</i>\n"
            f"🆘 <i>Если оплата не прошла — напишите в поддержку.</i>{ch_footer()}")
    kb = [[Btn(text=c.PACK_TITLE[d], callback_data=f"pk:{d}")] for d in c.PACKS]
    kb += [[BTN_PROMO], [BACK]]
    return text, Kb(inline_keyboard=kb)

def pack_view(d: int, disc: int = 0) -> tuple[str, Kb]:
    banner = f"🎁 Скидка <b>-{disc}%</b> уже учтена в ценах!\n\n" if disc > 0 else ""
    extra_p = c.apply_discount(c.EXTRA_DEVICE_RUB, disc)
    text = (f"<b>{c.PACK_TITLE[d]}</b>\n<i>Шаг 2 из 3 — срок подписки</i>\n\n"
            f"{banner}📱 Устройств: <b>{d}</b> · 🌍 {c.LOCATION} · ⚡ {c.traffic_text()}\n\n"
            f"Нужны ещё устройства? Добавьте слот на {c.EXTRA_DEVICE_DAYS} дней за {extra_p} ₽.")
    kb = [[Btn(text=f"🗓 {c.months_text(m)} — {c.apply_discount(p, disc)} ₽" + (f" (-{disc}%)" if disc > 0 else ""),
               callback_data=f"buy:d{d}m{m}")] for m, p in c.PACKS[d].items()]
    kb.append([Btn(text=f"➕ Добавить +1 устройство ({c.EXTRA_DEVICE_DAYS} дн) — {extra_p} ₽", callback_data="buy:x")])
    kb.append([Btn(text="◀️ Назад", callback_data="plans")])
    return text, Kb(inline_keyboard=kb)

def checkout_view(key: str, disc: int = 0) -> tuple[str, Kb] | None:
    it = item_of(key)
    if not it: return None
    title, base_price, days = it
    price_line = f"{_fmt_price(base_price, disc)} (скидка -{disc}%)" if disc > 0 else f"<b>{base_price} ₽</b>"
    text = (f"🧾 <b>Оформление</b>\n<i>Шаг 3 из 3 — способ оплаты</i>\n\n"
            f"Тариф: <b>{title}</b>\nСрок: <b>{days} дн.</b>\n"
            + ("" if key == "x" else f"Устройств: <b>{c.PLANS[key]['devices']}</b> · Локация: {c.LOCATION} · {c.traffic_text()}\n")
            + f"К оплате: {price_line}")
    if key == "x": text += "\n\n<i>Слот добавляется к вашей активной подписке после оплаты.</i>"
    back = "plans" if key == "x" else f"pk:{c.PLANS[key]['devices']}"
    text += "\n\n🆘 <i>Если оплата не пройдёт или возникнет ошибка — напишите в поддержку.</i>"
    return text, Kb(inline_keyboard=[
        [Btn(text="🏦 Оплатить через СБП", callback_data=f"pay:sbp:{key}")],
        [Btn(text="₿ Оплатить криптой", callback_data=f"pay:crypto:{key}")],
        [BTN_PROMO],
        [Btn(text="◀️ Назад", callback_data=back)]])

@router.callback_query(F.data == "plans")
async def plans(q: CallbackQuery, state: FSMContext):
    await state.clear()
    u = c.ensure_user(q.from_user.id, q.from_user.username)
    text, kb = plans_view(u["discount_pct"] or 0); await show(q, text, kb); await safe_answer(q)

@router.callback_query(F.data.regexp(r"^pk:\d+$"))
async def pack(q: CallbackQuery):
    d = int(q.data.split(":")[1])
    if d not in c.PACKS: return await safe_answer(q, "Тариф не найден", show_alert=True)
    u = c.ensure_user(q.from_user.id, q.from_user.username)
    text, kb = pack_view(d, u["discount_pct"] or 0); await show(q, text, kb); await safe_answer(q)

@router.callback_query(F.data.startswith("buy:"))
async def buy(q: CallbackQuery):
    u = c.ensure_user(q.from_user.id, q.from_user.username)
    v = checkout_view(q.data.split(":")[1], u["discount_pct"] or 0)
    if not v: return await safe_answer(q, "Тариф не найден", show_alert=True)
    await show(q, *v); await safe_answer(q)

@router.callback_query(F.data.startswith("pay:"))
async def pay(q: CallbackQuery):
    _, how, key = q.data.split(":")
    it = item_of(key)
    if not it or how not in rp.METHODS:
        return await safe_answer(q, "Тариф не найден", show_alert=True)
    title, base_price, days = it
    back = Btn(text="◀️ Назад", callback_data=f"buy:{key}")
    fallback = ([[Btn(text="🛒 Купить код", url=c.SHOP_URL)]] if c.SHOP_URL else []) + \
               [[Btn(text="🎟 Ввести промокод", callback_data="promo")], [back]]
    if not rp.ENABLED:
        await show(q, "💳 Автооплата временно недоступна. Можно активировать промокод.", Kb(inline_keyboard=fallback))
        return await safe_answer(q)
    u = c.ensure_user(q.from_user.id, q.from_user.username)
    if u["status"] == "banned": return await safe_answer(q, "Доступ заблокирован", show_alert=True)
    disc = u["discount_pct"] or 0
    price = c.apply_discount(base_price, disc)
    devices = 1 if key == "x" else c.PLANS[key]["devices"]
    order_id = uuid.uuid4().hex
    c.create_payment_row(order_id, q.from_user.id, key, days, devices, price, how)
    try:
        d = await rp.create_payment(order_id, price, how, f"{c.BRAND}: {title}", q.from_user.id)
    except Exception as e:
        logging.exception("rollypay create")
        c.mark_payment_failed(order_id)
        c.alert("rp_create", f"🔴 RollyPay: не удалось создать счёт: {e!r}")
        await show(q, "⚠️ Не удалось создать счёт, попробуйте через минуту.", Kb(inline_keyboard=fallback))
        return await safe_answer(q)
    c.set_payment_link(order_id, d["payment_id"], d["pay_url"])
    text = (f"🧾 <b>Счёт создан</b>\n\nТариф: <b>{title}</b>\nСрок: <b>{days} дн.</b>\nК оплате: <b>{price} ₽</b>\n\n"
            "Нажмите «Оплатить», после оплаты подписка активируется автоматически. "
            "Если оплата не прошла или подписка не выдалась — нажмите «Проверить оплату» или напишите в поддержку.")
    await show(q, text, Kb(inline_keyboard=[[Btn(text="💳 Оплатить", url=d["pay_url"])],
                                            [Btn(text="🔄 Проверить оплату", callback_data=f"chk:{order_id}")],
                                            [back]]))
    await safe_answer(q)

async def finalize_payment(order_id: str) -> str:
    """Зачисляет оплаченный заказ (идемпотентно) и пишет пользователю. Возвращает ok | already | unknown."""
    st, p = await c.fulfill_payment(order_id)
    if st == "ok":
        if p["plan_key"] == "test1":
            kb = Kb(inline_keyboard=[[Btn(text="◀️ В админку", callback_data="adm:l:0")]])
            text = "✅ Тестовый платёж на <b>1 ₽</b> прошёл успешно! RollyPay и вебхук работают."
        else:
            kb = Kb(inline_keyboard=[[Btn(text="🚀 Подключить", callback_data="connect")]])
            text = ((f"✅ Оплата прошла, подписка активирована на <b>{p['days']} дней</b>." if p["plan_key"] != "x"
                     else "✅ Оплата прошла, добавлен +1 слот устройства.") + ch_footer())
        try: await bot.send_message(p["tg_id"], text, reply_markup=kb)
        except Exception: logging.warning("не удалось уведомить %s об оплате %s", p["tg_id"], order_id)
        await notify_admins(f"💰 Оплата {p['amount']} ₽ · {p['plan_key']} · tg {p['tg_id']} · {p['method']}")
    return st

@router.callback_query(F.data.startswith("chk:"))
async def check_pay(q: CallbackQuery):
    p = c.get_payment_row(q.data.split(":", 1)[1])
    if not p or p["tg_id"] != q.from_user.id: return await safe_answer(q, "Платёж не найден", show_alert=True)
    if p["status"] == "paid": return await safe_answer(q, "✅ Оплата уже получена, подписка активна", show_alert=True)
    try: info = await rp.get_payment(p["rp_payment_id"])
    except Exception:
        logging.exception("rollypay status")
        return await safe_answer(q, "Не удалось проверить, попробуйте позже", show_alert=True)
    if info.get("status") == "paid" and str(info.get("amount")) and float(info["amount"]) + 0.001 >= float(p["amount"]):
        try: await finalize_payment(p["order_id"])
        except Exception:
            logging.exception("finalize")
            return await safe_answer(q, "Оплата получена, но выдача задерживается — повторите через минуту", show_alert=True)
        return await safe_answer(q)
    msg = {"expired": "Срок счёта истёк, создайте новый", "canceled": "Платёж отменён"}.get(
        info.get("status"), "Оплата пока не поступила. Если уже оплатили — подождите минуту")
    await safe_answer(q, msg, show_alert=True)

# ---------- нижняя клавиатура (хендлеры стоят выше промокодов: кнопка работает и в режиме ввода кода) ----------
@router.message(F.text == RB_MENU)
async def rb_menu(m: Message, state: FSMContext):
    await state.clear()
    await _ensure_synced(m.from_user.id, m.from_user.username)
    await m.answer(welcome(), reply_markup=kb_main())

@router.message(F.text == RB_CONNECT)
async def rb_connect(m: Message, state: FSMContext):
    await state.clear()
    text, kb = connect_view(await _ensure_synced(m.from_user.id, m.from_user.username))
    await m.answer(text, reply_markup=kb)

@router.message(F.text == RB_PLANS)
async def rb_plans(m: Message, state: FSMContext):
    await state.clear()
    u = await _ensure_synced(m.from_user.id, m.from_user.username)
    text, kb = plans_view(u["discount_pct"] or 0)
    await m.answer(text, reply_markup=kb)

# ---------- промокоды ----------
KB_CANCEL = Kb(inline_keyboard=[[Btn(text="✖️ Отмена", callback_data="menu")]])
PROMO_PROMPT = ("🎟 <b>Активация промокода</b>\n\nОтправьте код одним сообщением, например:\n"
                "<code>HUTZ-XXXX-XXXX</code>")

@router.callback_query(F.data == "promo")
async def promo_start(q: CallbackQuery, state: FSMContext):
    await state.set_state(PromoSt.code)
    await show(q, PROMO_PROMPT, KB_CANCEL); await safe_answer(q)

async def do_redeem(m: Message, raw: str) -> bool:
    """Активирует код и отвечает пользователю. True — диалог ввода кода можно закрывать."""
    c.ensure_user(m.from_user.id, m.from_user.username)
    try:
        st, u, p = await c.redeem_promo(m.from_user.id, raw)
    except Exception as e:
        logging.exception("promo redeem")
        await notify_admins(f"❗ Не удалось активировать промокод у {m.from_user.id}:\n<pre>{html.escape(repr(e))[:500]}</pre>")
        await m.answer("⚠️ Не удалось активировать: сервер временно недоступен. "
                       "Код <b>не потрачен</b> — попробуйте через пару минут или напишите в поддержку.",
                       reply_markup=Kb(inline_keyboard=[[Btn(text="🆘 Поддержка", url=c.SUPPORT_URL)],
                                                        [Btn(text="✖️ Отмена", callback_data="menu")]]))
        return False
    if st == "used_self":
        await m.answer("✅ Этот код уже активирован на вашем аккаунте.", reply_markup=Kb(inline_keyboard=[
            [Btn(text="📊 Статус", callback_data="status")], [BACK]]))
        return True
    if st == "ok_discount":
        await m.answer(f"🎁 <b>Промокод на скидку -{p['discount_pct']}% активирован!</b>\n\n"
                       "Скидка автоматически применится к вашей следующей оплате в разделе «Тарифы».",
                       reply_markup=Kb(inline_keyboard=[[Btn(text="💳 Перейти к тарифам", callback_data="plans")], [BACK]]))
        return True
    if st == "discount_better":
        await m.answer(f"ℹ️ У вас уже активна скидка <b>-{u['discount_pct']}%</b> — она не меньше скидки по этому коду. "
                       "Код не потрачен.",
                       reply_markup=Kb(inline_keyboard=[[Btn(text="💳 Перейти к тарифам", callback_data="plans")], [BACK]]))
        return True
    if st == "ok":
        await m.answer(f"✅ <b>Промокод активирован!</b>\n\n➕ Добавлено: <b>{p['days']} дн.</b>\n"
                       f"📱 Устройств в подписке: <b>{u['device_limit']}</b>\n"
                       f"⏳ Действует до: <b>{date(u['expires_at'])}</b>\n\n"
                       f"Откройте «Подключить», чтобы добавить подписку в Happ.{ch_footer()}",
                       reply_markup=Kb(inline_keyboard=[[Btn(text="🚀 Подключить", callback_data="connect")],
                                                        [Btn(text="📊 Статус", callback_data="status")]]))
        return True
    if st == "banned":
        await m.answer("⛔ Доступ заблокирован.", reply_markup=Kb(inline_keyboard=[[Btn(text="🆘 Поддержка", url=c.SUPPORT_URL)]]))
        return True
    if st == "throttled":
        await m.answer("⏳ Слишком много неверных попыток. Попробуйте через 10 минут "
                       f"или напишите в поддержку.", reply_markup=Kb(inline_keyboard=[[BACK]]))
        return True
    await m.answer("⚠️ Этот код уже использован." if st == "used"
                   else "❌ Код не найден. Проверьте его и отправьте ещё раз.", reply_markup=KB_CANCEL)
    return False

@router.message(Command("promo"))
async def promo_cmd(m: Message, command: CommandObject, state: FSMContext):
    if command.args:
        if await do_redeem(m, command.args): await state.clear()
        else: await state.set_state(PromoSt.code)
        return
    await state.set_state(PromoSt.code)
    await m.answer(PROMO_PROMPT, reply_markup=KB_CANCEL)

@router.message(PromoSt.code, F.text, ~F.text.startswith("/"))
async def promo_text(m: Message, state: FSMContext):
    if await do_redeem(m, m.text): await state.clear()

# ---------- уведомления (вызываются из app.py) ----------
def kb_renew() -> Kb:
    return Kb(inline_keyboard=[[Btn(text="💳 Продлить", callback_data="plans")]])

async def send_notice(tg_id: int, text: str):
    try: await bot.send_message(tg_id, text + ch_footer(), reply_markup=kb_renew())
    except Exception: logging.info("не удалось отправить уведомление %s", tg_id)

async def notify_admins(text: str):
    """Сообщение всем админам (сбои панели, ошибки). Сам никогда не падает."""
    for aid in c.ADMINS:
        try: await bot.send_message(aid, text[:3900])
        except Exception: logging.warning("не удалось уведомить админа %s", aid)

c.alert_hook = notify_admins   # core.alert() шлёт сюда сообщения о недоступности панели

async def send_db_backup(chat_id: int | None = None, reason: str = "Автобэкап") -> bool:
    target = chat_id or c.BACKUP_CHAT_ID
    if not target: return False
    data = c.backup_db_bytes()
    ts = time.strftime("%Y%m%d_%H%M", time.localtime())
    users_n = c.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active_n = c.db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
    caption = (f"💾 <b>{reason} БД</b>\n"
               f"Пользователей: <b>{users_n}</b> (активных: <b>{active_n}</b>)\n"
               "<i>Чтобы восстановить после пересборки — ответьте на этот файл командой /restore</i>")
    await bot.send_document(target, BufferedInputFile(data, f"hutz_vpn_{ts}.db"), caption=caption)
    return True

# ---------- админка ----------
def label(u) -> str:
    name = f"@{u['username']}" if u["username"] else str(u["tg_id"])
    tail = f" · {dur(u['expires_at'] - c.now())}" if u["status"] == "active" else ""
    return f"{ICON[u['status']]} {name}{tail}"

def list_view(page: int) -> tuple[str, Kb]:
    rows = c.db.execute("SELECT * FROM users ORDER BY (status='active') DESC, created_at DESC").fetchall()
    pages = max(1, -(-len(rows) // PAGE)); page = min(max(page, 0), pages - 1)
    kb = [[Btn(text=label(u), callback_data=f"adm:u:{u['tg_id']}")] for u in rows[page * PAGE:(page + 1) * PAGE]]
    nav = ([Btn(text="◀️", callback_data=f"adm:l:{page - 1}")] if page else []) + \
          [Btn(text=f"{page + 1}/{pages}", callback_data="adm:noop")] + \
          ([Btn(text="▶️", callback_data=f"adm:l:{page + 1}")] if page < pages - 1 else [])
    kb.append(nav)
    kb.append([Btn(text="🔍 Найти пользователя", callback_data="adm:find")])
    kb.append([Btn(text="🎟 Генератор промокодов", callback_data="adm:pg")])
    kb.append([Btn(text="📣 Рассылка", callback_data="adm:bc"), Btn(text="🧪 Тест (1 ₽)", callback_data="adm:testpay")])
    kb.append([Btn(text="🔄 Синхр. H1", callback_data="adm:sync"), Btn(text="💾 Бэкап БД", callback_data="adm:backup")])
    active = sum(r["status"] == "active" for r in rows)
    return f"👥 <b>Пользователи</b>: {len(rows)}, активных: {active}", Kb(inline_keyboard=kb)

def dev_name(d: dict) -> str:
    """Человекочитаемое имя устройства: «iPhone 15 (iOS)»; если Happ не прислал модель — «Устройство»."""
    name = d["model"] or "Устройство"
    return f"{name} ({d['os']})" if d["os"] else name

async def card_view(tg_id: int) -> tuple[str, Kb]:
    u = c.get_user(tg_id)
    to_list = [Btn(text="◀️ К списку", callback_data="adm:l:0")]
    if not u: return "Пользователь не найден.", Kb(inline_keyboard=[to_list])
    used = "—"
    if u["status"] == "active":
        try: used = fmt(await c.usage(u["key_id"]))
        except Exception: pass
    left = dur(u["expires_at"] - c.now()) if u["status"] == "active" else "—"
    text = (f"👤 <b>{html.escape(label(u).split(' · ')[0])}</b> · <code>{tg_id}</code>\n"
            f"Статус: {u['status']} / {u['plan']}\nТрафик: {used} / {fmt(u['limit_bytes'])}\n"
            f"Осталось: {left}\nУстройства: {len(c.devices_of(u))}/{u['device_limit']}\n"
            f"Создан: {date(u['created_at'])}")
    devs = c.device_list(u)
    if devs:
        text += "\n\n📱 <b>Устройства:</b>\n" + "\n".join(
            f"{i}. {html.escape(dev_name(d))} · <code>{html.escape(d['hwid'][:8])}</code>" for i, d in enumerate(devs, 1))
    if u["status"] == "banned":
        rows = [[Btn(text="✅ Разбанить", callback_data=f"adm:unban:{tg_id}")]]
    else:
        rows = [[Btn(text="+1 ч", callback_data=f"adm:ext:{tg_id}:1"),
                 Btn(text="-1 ч", callback_data=f"adm:ext:{tg_id}:-1"),
                 Btn(text="+24 ч", callback_data=f"adm:ext:{tg_id}:24")],
                [Btn(text="+7 дн", callback_data=f"adm:ext:{tg_id}:168"),
                 Btn(text="+30 дн", callback_data=f"adm:ext:{tg_id}:720")],
                [Btn(text="➕ 1 устройство", callback_data=f"adm:dev:{tg_id}"),
                 Btn(text="➖ 1 устройство", callback_data=f"adm:dec:{tg_id}")],
                *[[Btn(text=f"❌ Отвязать #{i}: {dev_name(d)}"[:60], callback_data=f"adm:ud:{tg_id}:{d['h']}")]
                  for i, d in enumerate(devs, 1)],
                [Btn(text="♻️ Сбросить HWID", callback_data=f"adm:rst:{tg_id}"),
                 Btn(text="🗑 Обнулить подписку", callback_data=f"adm:rev:{tg_id}")],
                [Btn(text="⛔ Бан", callback_data=f"adm:ban:{tg_id}")]]
    return text, Kb(inline_keyboard=rows + [to_list])

def promo_menu_view() -> tuple[str, Kb]:
    kb = [
        [Btn(text="⚙️ Свой промокод (дни / скидка)", callback_data="adm:pc")],
        [Btn(text="📋 Активные промокоды", callback_data="adm:pl")],
    ]
    kb += [[Btn(text=f"{c.PACK_TITLE[p['devices']].split()[0]} {p['devices']} устр. · {c.months_text(p['months'])} — {p['price']} ₽",
                callback_data=f"adm:pt:{p['key']}")] for p in c.PLANS.values()]
    kb.append([Btn(text="◀️ К списку", callback_data="adm:l:0")])
    return (f"🎟 <b>Генератор промокодов</b>\n\nВыберите быстрый тариф или создайте свой промокод с любыми параметрами.\n"
            f"Активных кодов в базе: <b>{c.promos_left()}</b>",
            Kb(inline_keyboard=kb))

def active_promos_view() -> tuple[str, Kb]:
    rows = c.list_active_promos(15)
    if not rows:
        return ("📋 <b>Активные промокоды</b>\n\nСейчас нет свободных промокодов.",
                Kb(inline_keyboard=[[Btn(text="⚙️ Создать промокод", callback_data="adm:pc")],
                                    [Btn(text="◀️ Назад", callback_data="adm:pg")]]))
    lines, kb = [], []
    for r in rows:
        kind = r["kind"] or "days"
        desc = f"-{r['discount_pct']}%" if kind == "discount" else f"{r['days']} дн. · {r['devices']} устр."
        uses = f"{r['uses_count'] or 0}/{r['max_uses'] or 1}"
        lines.append(f"• <code>{r['code']}</code> — {desc} ({uses})")
        kb.append([Btn(text=f"🗑 {r['code']} ({desc}, {uses})", callback_data=f"adm:pd:{r['code']}")])
    kb.append([Btn(text="◀️ Назад", callback_data="adm:pg")])
    return "📋 <b>Активные промокоды (последние 15)</b>\n<i>Нажмите на код, чтобы удалить его:</i>\n\n" + "\n".join(lines), Kb(inline_keyboard=kb)

@router.message(Command("admin", "users"), admin)
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    text, kb = list_view(0)
    await m.answer(text, reply_markup=kb)

@router.message(Command("user"), admin)
async def user_cmd(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if not command.args: return await m.answer("/user <tg_id или @username>")
    await open_card(m, command.args)

async def open_card(m: Message, query: str) -> bool:
    u = c.find_user(query)
    if not u:
        await m.answer("Пользователь не найден. Он должен хотя бы раз написать боту /start.")
        return False
    text, kb = await card_view(u["tg_id"])
    await m.answer(text, reply_markup=kb)
    return True

@router.message(AdmSt.search, admin, F.text, ~F.text.startswith("/"))
async def search_text(m: Message, state: FSMContext):
    if await open_card(m, m.text): await state.clear()

_bc_running = False

@router.message(AdmSt.broadcast, admin)
async def bc_receive(m: Message, state: FSMContext):
    if m.text and m.text.startswith("/"):
        return
    await state.update_data(bc_chat_id=m.chat.id, bc_msg_id=m.message_id)
    await state.set_state(AdmSt.bc_confirm)
    all_cnt = c.db.execute("SELECT COUNT(*) FROM users WHERE status!='banned'").fetchone()[0]
    act_cnt = c.db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
    kb = Kb(inline_keyboard=[
        [Btn(text=f"📢 Всем ({all_cnt})", callback_data="adm:bcgo:all")],
        [Btn(text=f"🟢 Только активным ({act_cnt})", callback_data="adm:bcgo:active")],
        [Btn(text="✖️ Отмена", callback_data="adm:l:0")]])
    await m.answer("📣 <b>Подтверждение рассылки</b>\n\nСообщение выше готово к отправке. Кому разослать?",
                   reply_markup=kb)

async def _run_broadcast(admin_chat_id: int, status_msg_id: int, src_chat_id: int, src_msg_id: int, target: str):
    global _bc_running
    _bc_running = True
    try:
        sql = ("SELECT tg_id FROM users WHERE status='active'" if target == "active"
               else "SELECT tg_id FROM users WHERE status!='banned'")
        ids = [r[0] for r in c.db.execute(sql).fetchall()]
        sent = blocked = failed = 0
        for uid in ids:
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_msg_id)
                sent += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.5)
                try:
                    await bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_msg_id)
                    sent += 1
                except TelegramForbiddenError:
                    blocked += 1
                except Exception:
                    failed += 1
            except TelegramForbiddenError:
                blocked += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        summary = (f"✅ <b>Рассылка завершена</b>\n\n"
                   f"Всего получателей: <b>{len(ids)}</b>\n"
                   f"Доставлено: <b>{sent}</b>\n"
                   f"Заблокировали бота: <b>{blocked}</b>\n"
                   f"Ошибок: <b>{failed}</b>")
        kb = Kb(inline_keyboard=[[Btn(text="◀️ В админку", callback_data="adm:l:0")]])
        try:
            await bot.edit_message_text(summary, chat_id=admin_chat_id, message_id=status_msg_id, reply_markup=kb)
        except Exception:
            await bot.send_message(admin_chat_id, summary, reply_markup=kb)
    finally:
        _bc_running = False

# ---------- конструктор кастомных промокодов ----------
async def _pc_ask_devices(msg: Message, state: FSMContext, days: int):
    await state.update_data(pc_days=days)
    await state.set_state(AdmSt.pc_devices)
    kb = Kb(inline_keyboard=[
        [Btn(text="1", callback_data="adm:pcdv:1"), Btn(text="2", callback_data="adm:pcdv:2"),
         Btn(text="3", callback_data="adm:pcdv:3"), Btn(text="5", callback_data="adm:pcdv:5")],
        [Btn(text="✖️ Отмена", callback_data="adm:pg")]])
    await msg.answer(f"📅 Дней: <b>{days}</b>\n\n<b>Шаг 2:</b> Сколько устройств дать по этому промокоду? "
                     "Нажмите кнопку или введите своё число (1–20):", reply_markup=kb)

async def _pc_ask_code(msg: Message, state: FSMContext):
    await state.set_state(AdmSt.pc_code)
    d = await state.get_data()
    info = (f"💸 Скидка: <b>-{d['pc_discount']}%</b>" if d.get("pc_kind") == "discount"
            else f"📅 Подписка: <b>{d['pc_days']} дн. · {d['pc_devices']} устр.</b>")
    kb = Kb(inline_keyboard=[
        [Btn(text="🎲 Сгенерировать случайно", callback_data="adm:pcrnd")],
        [Btn(text="✖️ Отмена", callback_data="adm:pg")]])
    await msg.answer(f"{info}\n\n<b>Название промокода:</b> отправьте свой текст (например <code>SALE20</code> или <code>HUTZ7</code>) "
                     "или нажмите «Сгенерировать случайно»:", reply_markup=kb)

async def _pc_ask_qty(msg: Message, state: FSMContext, custom_code: str | None):
    await state.update_data(pc_code=custom_code)
    await state.set_state(AdmSt.pc_qty)
    if custom_code:
        kb = Kb(inline_keyboard=[
            [Btn(text="1 (одноразовый)", callback_data="adm:pcq:1"), Btn(text="10", callback_data="adm:pcq:10")],
            [Btn(text="50", callback_data="adm:pcq:50"), Btn(text="100", callback_data="adm:pcq:100"),
             Btn(text="1000", callback_data="adm:pcq:1000")],
            [Btn(text="✖️ Отмена", callback_data="adm:pg")]])
        await msg.answer(f"🏷 Код: <code>{custom_code}</code>\n\n"
                         "<b>Лимит активаций:</b> сколько человек смогут применить этот код? "
                         "Нажмите кнопку или введите любое число:", reply_markup=kb)
    else:
        kb = Kb(inline_keyboard=[
            [Btn(text="1", callback_data="adm:pcq:1"), Btn(text="5", callback_data="adm:pcq:5"),
             Btn(text="10", callback_data="adm:pcq:10"), Btn(text="25", callback_data="adm:pcq:25")],
            [Btn(text="✖️ Отмена", callback_data="adm:pg")]])
        await msg.answer("🎲 <b>Случайные одноразовые коды</b>\n\n"
                         "Сколько уникальных кодов создать? Нажмите кнопку или введите число (1–100):", reply_markup=kb)

async def _pc_finish(target_msg: Message, state: FSMContext, qty: int, edit: bool = False):
    d = await state.get_data()
    await state.clear()
    kind = d.get("pc_kind", "days")
    custom_code = d.get("pc_code")
    days = d.get("pc_days", 0)
    devices = d.get("pc_devices", 1)
    disc = d.get("pc_discount", 0)
    max_uses = qty if custom_code else 1
    count = 1 if custom_code else qty
    try:
        codes = c.create_custom_promos(kind=kind, days=days, devices=devices, discount_pct=disc,
                                       max_uses=max_uses, count=count, custom_code=custom_code)
    except ValueError as e:
        kb = Kb(inline_keyboard=[[Btn(text="⚙️ Попробовать снова", callback_data="adm:pc")],
                                 [Btn(text="◀️ К промокодам", callback_data="adm:pg")]])
        text = f"❌ {html.escape(str(e))}"
        if edit: await target_msg.edit_text(text, reply_markup=kb)
        else: await target_msg.answer(text, reply_markup=kb)
        return
    desc = f"Скидка -{disc}%" if kind == "discount" else f"{days} дн. · {devices} устр."
    extra = f"Лимит активаций: <b>{max_uses}</b>" if custom_code else f"Создано одноразовых кодов: <b>{len(codes)}</b>"
    text = (f"✅ <b>Промокод создан!</b>\n{desc} · {extra}\n"
            f"<i>Тапните по блоку, чтобы скопировать:</i>\n\n"
            f"<pre>{chr(10).join(codes)}</pre>")
    kb = Kb(inline_keyboard=[
        [Btn(text="⚙️ Создать ещё", callback_data="adm:pc")],
        [Btn(text="📋 Активные коды", callback_data="adm:pl")],
        [Btn(text="◀️ В админку", callback_data="adm:l:0")]])
    if edit:
        try: await target_msg.edit_text(text, reply_markup=kb)
        except TelegramBadRequest: await target_msg.answer(text, reply_markup=kb)
    else:
        await target_msg.answer(text, reply_markup=kb)

@router.message(AdmSt.pc_days, admin, F.text, ~F.text.startswith("/"))
async def pc_days_text(m: Message, state: FSMContext):
    try: days = int(m.text.strip())
    except ValueError: return await m.answer("❌ Введите целое число дней (например <code>7</code> или <code>45</code>).")
    if not (1 <= days <= 3650): return await m.answer("❌ Укажите число дней от 1 до 3650.")
    await _pc_ask_devices(m, state, days)

@router.message(AdmSt.pc_devices, admin, F.text, ~F.text.startswith("/"))
async def pc_devices_text(m: Message, state: FSMContext):
    try: devs = int(m.text.strip())
    except ValueError: return await m.answer("❌ Введите целое число устройств (например <code>1</code> или <code>3</code>).")
    if not (1 <= devs <= 20): return await m.answer("❌ Укажите от 1 до 20 устройств.")
    await state.update_data(pc_devices=devs)
    await _pc_ask_code(m, state)

@router.message(AdmSt.pc_discount, admin, F.text, ~F.text.startswith("/"))
async def pc_discount_text(m: Message, state: FSMContext):
    raw = m.text.strip().rstrip("%").strip()
    try: pct = int(raw)
    except ValueError: return await m.answer("❌ Введите размер скидки в процентах (например <code>20</code>).")
    if not (1 <= pct <= 95): return await m.answer("❌ Скидка должна быть от 1 до 95%.")
    await state.update_data(pc_discount=pct)
    await _pc_ask_code(m, state)

@router.message(AdmSt.pc_code, admin, F.text, ~F.text.startswith("/"))
async def pc_code_text(m: Message, state: FSMContext):
    code = c.norm_code(m.text)
    if len(code) < 3 or len(code) > 32:
        return await m.answer("❌ Код должен содержать от 3 до 32 латинских букв/цифр (например <code>SALE20</code>).")
    await _pc_ask_qty(m, state, code)

@router.message(AdmSt.pc_qty, admin, F.text, ~F.text.startswith("/"))
async def pc_qty_text(m: Message, state: FSMContext):
    try: qty = int(m.text.strip())
    except ValueError: return await m.answer("❌ Введите целое число.")
    d = await state.get_data()
    max_allowed = 100000 if d.get("pc_code") else 100
    if not (1 <= qty <= max_allowed):
        return await m.answer(f"❌ Укажите число от 1 до {max_allowed}.")
    await _pc_finish(m, state, qty, edit=False)

@router.callback_query(F.data.startswith("adm:"), admin)
async def adm(q: CallbackQuery, state: FSMContext):
    p = q.data.split(":"); act = p[1]; note = None
    if act == "noop": return await safe_answer(q)
    if act == "find":
        await state.set_state(AdmSt.search)
        await show(q, "🔍 Отправьте числовой <b>TG ID</b> или <b>@username</b> пользователя.",
                   Kb(inline_keyboard=[[Btn(text="◀️ К списку", callback_data="adm:l:0")]]))
        return await safe_answer(q)
    if act == "bc":
        if _bc_running:
            return await safe_answer(q, "⏳ Предыдущая рассылка ещё идёт, дождитесь завершения.", show_alert=True)
        await state.set_state(AdmSt.broadcast)
        await show(q, "📣 <b>Рассылка пользователям</b>\n\n"
                      "Отправьте сообщение, которое нужно разослать (текст, фото с подписью или файл — форматирование сохранится).",
                   Kb(inline_keyboard=[[Btn(text="✖️ Отмена", callback_data="adm:l:0")]]))
        return await safe_answer(q)
    if act == "bcgo":
        if _bc_running:
            return await safe_answer(q, "⏳ Рассылка уже запущена.", show_alert=True)
        data = await state.get_data()
        src_chat, src_msg = data.get("bc_chat_id"), data.get("bc_msg_id")
        await state.clear()
        if not (src_chat and src_msg):
            return await safe_answer(q, "Сообщение для рассылки устарело, создайте заново.", show_alert=True)
        target = p[2] if len(p) > 2 and p[2] in ("all", "active") else "all"
        label_t = "активным пользователям" if target == "active" else "всем пользователям"
        await show(q, f"⏳ <b>Рассылка запущена ({label_t})…</b>\nОтчёт появится здесь по завершении.",
                   Kb(inline_keyboard=[[Btn(text="◀️ В админку", callback_data="adm:l:0")]]))
        asyncio.create_task(_run_broadcast(q.message.chat.id, q.message.message_id, src_chat, src_msg, target))
        return await safe_answer(q, "Рассылка запущена")
    if act == "pc":
        await state.clear()
        kb = Kb(inline_keyboard=[
            [Btn(text="📅 На дни (подписка)", callback_data="adm:pck:days")],
            [Btn(text="💸 Скидка на оплату (%)", callback_data="adm:pck:discount")],
            [Btn(text="◀️ Назад", callback_data="adm:pg")]])
        await show(q, "⚙️ <b>Создание своего промокода</b>\n\nВыберите тип промокода:", kb)
        return await safe_answer(q)
    if act == "pck":
        kind = p[2]
        await state.update_data(pc_kind=kind)
        if kind == "discount":
            await state.set_state(AdmSt.pc_discount)
            kb = Kb(inline_keyboard=[
                [Btn(text="10%", callback_data="adm:pcds:10"), Btn(text="20%", callback_data="adm:pcds:20"),
                 Btn(text="30%", callback_data="adm:pcds:30"), Btn(text="50%", callback_data="adm:pcds:50")],
                [Btn(text="✖️ Отмена", callback_data="adm:pg")]])
            await show(q, "💸 <b>Промокод на скидку</b>\n\nВыберите размер скидки или введите число от 1 до 95:", kb)
        else:
            await state.set_state(AdmSt.pc_days)
            kb = Kb(inline_keyboard=[
                [Btn(text="3 дн", callback_data="adm:pcd:3"), Btn(text="7 дн", callback_data="adm:pcd:7"),
                 Btn(text="14 дн", callback_data="adm:pcd:14"), Btn(text="30 дн", callback_data="adm:pcd:30")],
                [Btn(text="✖️ Отмена", callback_data="adm:pg")]])
            await show(q, "📅 <b>Промокод на дни</b>\n\n<b>Шаг 1:</b> Сколько дней подписки? Нажмите кнопку или введите своё число:", kb)
        return await safe_answer(q)
    if act == "pcd":
        await _pc_ask_devices(q.message, state, int(p[2]))
        return await safe_answer(q)
    if act == "pcdv":
        await state.update_data(pc_devices=int(p[2]))
        await _pc_ask_code(q.message, state)
        return await safe_answer(q)
    if act == "pcds":
        await state.update_data(pc_discount=int(p[2]))
        await _pc_ask_code(q.message, state)
        return await safe_answer(q)
    if act == "pcrnd":
        await _pc_ask_qty(q.message, state, None)
        return await safe_answer(q)
    if act == "pcq":
        await _pc_finish(q.message, state, int(p[2]), edit=True)
        return await safe_answer(q, "Готово")
    await state.clear()
    if act == "pl":
        text, kb = active_promos_view(); await show(q, text, kb); return await safe_answer(q)
    if act == "pd":
        code = p[2]
        c.delete_promo(code)
        text, kb = active_promos_view(); await show(q, text, kb); return await safe_answer(q, f"Удалён: {code}")
    if act == "l":
        text, kb = list_view(int(p[2])); await show(q, text, kb); return await safe_answer(q)
    if act == "sync":
        try:
            st = await c.sync_from_h1()
            note = f"Синхронизировано: +{st['created']} новых, {st['updated']} обновл. (всего в H1: {st['total']})"
        except Exception as e:
            logging.exception("adm sync")
            note = f"Ошибка синхронизации: {e}"
        text, kb = list_view(0); await show(q, text, kb); return await safe_answer(q, note[:190], show_alert=True)
    if act == "backup":
        try:
            await send_db_backup(q.from_user.id, reason="Ручной бэкап")
            return await safe_answer(q, "💾 Файл БД отправлен в чат")
        except Exception as e:
            logging.exception("adm backup")
            return await safe_answer(q, f"Ошибка бэкапа: {e}"[:190], show_alert=True)
    if act == "testpay":
        mode = "SANDBOX (ROLLYPAY_TEST=1)" if rp.TEST_MODE else "БОЕВОЙ (реальный 1 ₽)"
        kb = Kb(inline_keyboard=[
            [Btn(text="🏦 Тест СБП — 1 ₽", callback_data="adm:tp:sbp")],
            [Btn(text="₿ Тест Крипта — 1 ₽", callback_data="adm:tp:crypto")],
            [Btn(text="◀️ К списку", callback_data="adm:l:0")]])
        await show(q, f"🧪 <b>Тест оплаты (1 ₽)</b>\nРежим RollyPay: <b>{mode}</b>\n\n"
                      "Подписка при тестовом платеже не изменяется. Выберите способ:", kb)
        return await safe_answer(q)
    if act == "tp":
        how = p[2]
        if how not in rp.METHODS: return await safe_answer(q, "Неизвестный способ", show_alert=True)
        if not rp.ENABLED:
            return await safe_answer(q, "RollyPay не настроен: проверьте ROLLYPAY_API_KEY и ROLLYPAY_SIGNING_SECRET", show_alert=True)
        c.ensure_user(q.from_user.id, q.from_user.username)
        order_id = uuid.uuid4().hex
        c.create_payment_row(order_id, q.from_user.id, "test1", 0, 0, 1, how)
        try:
            d = await rp.create_payment(order_id, 1, how, f"{c.BRAND}: Тестовый платёж 1 ₽", q.from_user.id)
        except Exception as e:
            logging.exception("rollypay test create")
            c.mark_payment_failed(order_id)
            return await safe_answer(q, f"Ошибка RollyPay: {e}"[:190], show_alert=True)
        c.set_payment_link(order_id, d["payment_id"], d["pay_url"])
        text = (f"🧪 <b>Тестовый счёт создан</b>\n\nСумма: <b>1 ₽</b>\nСпособ: <b>{how.upper()}</b>\n"
                f"Order ID: <code>{order_id}</code>\n\nОплатите по кнопке ниже и проверьте приход вебхука.")
        kb = Kb(inline_keyboard=[
            [Btn(text="💳 Оплатить 1 ₽", url=d["pay_url"])],
            [Btn(text="🔄 Проверить оплату", callback_data=f"chk:{order_id}")],
            [Btn(text="◀️ К списку", callback_data="adm:l:0")]])
        await show(q, text, kb); return await safe_answer(q)
    if act == "pg":   # генератор промокодов: выбор тарифа
        text, kb = promo_menu_view(); await show(q, text, kb); return await safe_answer(q)
    if act == "pt":   # ... выбор количества
        pl = c.PLANS.get(p[2])
        if not pl: return await safe_answer(q, "Тариф не найден", show_alert=True)
        kb = Kb(inline_keyboard=[[Btn(text=str(n), callback_data=f"adm:pn:{pl['key']}:{n}") for n in PROMO_COUNTS],
                                 [Btn(text="◀️ Назад", callback_data="adm:pg")]])
        await show(q, f"🎟 Тариф: <b>{pl['label']}</b> ({pl['days']} дн.)\n\nСколько кодов создать?", kb)
        return await safe_answer(q)
    if act == "pn":   # ... генерация
        pl, n = c.PLANS.get(p[2]), int(p[3])
        if not pl or n not in PROMO_COUNTS: return await safe_answer(q, "Неверные параметры", show_alert=True)
        codes = c.gen_promos(pl["days"], pl["devices"], n)
        text = (f"🎟 <b>Создано кодов: {n}</b> · {pl['label']} ({pl['days']} дн.)\n"
                f"<i>Каждый код одноразовый. Тапните по блоку, чтобы скопировать всё.</i>\n\n"
                f"<pre>{chr(10).join(codes)}</pre>")
        kb = Kb(inline_keyboard=[[Btn(text="🎟 Ещё коды", callback_data="adm:pg")],
                                 [Btn(text="◀️ К списку", callback_data="adm:l:0")]])
        await show(q, text, kb); return await safe_answer(q, f"Создано: {n}")
    tg = int(p[2]); u = c.get_user(tg)
    if u and act == "ext":
        hours = int(p[3])
        r = await c.shift_time(tg, hours)
        note = {"ok": f"{hours:+d} ч", "expired": "Срок вышел: клиент удалён, статус expired",
                "noop": "Нет активной подписки"}[r]
    elif u and act == "rev":
        note = "Подписка обнулена, устройства очищены" if await c.revoke(tg) else "Недоступно для этого статуса"
    elif u and act == "dev":
        c.add_device_slot(tg); note = "+1 устройство"
    elif u and act == "dec":
        limit, cut = c.remove_device_slot(tg)
        note = f"Лимит устройств: {limit}" + (f" (отвязано: {cut})" if cut else "")
    elif u and act == "ud":
        d = c.unbind_device(tg, p[3])
        note = f"Отвязано: {dev_name(d)}" if d else "Устройство уже отвязано"
    elif u and act == "rst":
        c.reset_devices(tg); note = "Список устройств очищен"
    elif u and act == "ban":
        await c.ban(tg); note = "Заблокирован"
    elif u and act == "unban":
        c.db.execute("UPDATE users SET status='expired' WHERE tg_id=? AND status='banned'", (tg,))
        c.db.commit(); note = "Разблокирован — выдайте доступ кнопками +…"
    text, kb = await card_view(tg)
    await show(q, text, kb); await safe_answer(q, note)

# Текстовая команда для нестандартных значений
def args(cmd: CommandObject, n: int):
    p = (cmd.args or "").split()
    try: return [float(x) for x in p[:n]] if len(p) >= n else None
    except ValueError: return None

@router.message(Command("grant"), admin)
async def grant(m: Message, command: CommandObject):
    a = args(command, 3)
    if not a: return await m.answer("/grant tg_id дней гб [устройств]")
    p = command.args.split()
    try: devices = max(1, int(p[3])) if len(p) > 3 else None
    except ValueError: return await m.answer("/grant tg_id дней гб [устройств]")
    await c.grant(int(a[0]), a[1], a[2], devices); await m.answer("Готово")

@router.message(Command("backup"), admin)
async def backup_cmd(m: Message):
    await send_db_backup(m.chat.id, reason="Ручной бэкап")

async def _do_restore(m: Message, doc):
    if not doc or not (doc.file_name or "").endswith((".db", ".sqlite", ".sqlite3")):
        return await m.answer("❌ Пришлите файл базы данных (<code>.db</code>) с подписью <code>/restore</code> "
                              "или ответьте командой <code>/restore</code> на ранее присланный бэкап.")
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    try:
        st = await c.restore_db_bytes(buf.getvalue())
    except Exception as e:
        logging.exception("restore_db_bytes")
        return await m.answer(f"❌ Не удалось восстановить БД: <code>{html.escape(str(e))}</code>")
    await m.answer(f"✅ <b>БД восстановлена!</b>\n"
                   f"Пользователей: <b>{st['users']}</b> (активных: <b>{st['active']}</b>)\n"
                   f"Оплат: <b>{st['payments']}</b> · Свободных промокодов: <b>{st['promos']}</b>")

@router.message(Command("restore"), admin)
async def restore_cmd(m: Message):
    doc = m.document or (m.reply_to_message.document if m.reply_to_message else None)
    await _do_restore(m, doc)

@router.message(admin, F.document, F.caption.startswith("/restore"))
async def restore_doc_caption(m: Message):
    await _do_restore(m, m.document)

# ---------- сервис ----------
async def setup_commands():
    user_cmds = [BotCommand(command="start", description="Главное меню"),
                 BotCommand(command="status", description="Мой статус"),
                 BotCommand(command="promo", description="Активировать промокод")]
    await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
    for aid in c.ADMINS:
        try:
            await bot.set_my_commands(user_cmds + [BotCommand(command="admin", description="Панель администратора"), BotCommand(command="backup", description="Скачать бэкап БД")],
                                      scope=BotCommandScopeChat(chat_id=aid))
        except Exception:
            logging.warning("не удалось задать команды админу %s (он должен сначала написать боту)", aid)

@dp.errors()
async def on_error(event: ErrorEvent):
    """Любой сбой хендлера: лог + стектрейс всем админам + аккуратный ответ пользователю. Бот продолжает работать."""
    exc = event.exception
    logging.exception("handler failed", exc_info=exc)
    upd = event.update
    src = upd.message or upd.callback_query
    who = f"{src.from_user.id} (@{src.from_user.username})" if src and src.from_user else "—"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-3000:]
    await notify_admins(f"❗ Ошибка в боте, пользователь {who}\n<pre>{html.escape(tb)}</pre>")
    if not src: return True
    try:
        if upd.callback_query: await safe_answer(upd.callback_query)
        chat = upd.message or upd.callback_query.message
        await chat.answer("⚠️ Сервис временно недоступен, попробуйте позже или напишите в поддержку.",
                          reply_markup=KB_SUPPORT)
    except Exception:
        logging.exception("не удалось ответить пользователю после ошибки")
    return True
