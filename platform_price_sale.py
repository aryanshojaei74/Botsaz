"""فروش افزونهٔ «🔎 قیمت‌یاب» به ربات‌های مستأجر، از ربات اصلی (PATCH_PRICE_LOOKUP_SALE_V1).

مالک: ربات‌های من ← انتخاب ربات ← «🔎 قیمت‌یاب» ← پرداخت از کیف‌پول بات‌ساز (پیش‌فرض ۱۵۰٬۰۰۰ تومان، یک‌بار و دائمی) ← فعال.
بعد از خرید، داخل ربات خودش (دکمه‌ها و تنظیمات ← 🔎 قیمت‌یاب) روشن/خاموش و دکمه‌های زیر پیام را تنظیم می‌کند.
مدیر اصلی: پنل ← ⚙️ تنظیمات و پلن‌ها ← «🔎 فروش قیمت‌یاب»: قیمت، روشن/خاموش فروش، لیست ربات‌های خریدار با
فعال/غیرفعال‌کردن هر کدام، و فعال‌سازی دستی (رایگان) برای یک ربات.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import bot_addons
from config import PLATFORM_ADMIN_ID, Session
from logging_setup import get_logger as _get_logger
from models import Bot as BotRow, BotAddon, PlatformTxn
from premium_emoji import hx_button

logger = _get_logger(__name__)
platform_price_sale_router = Router(name="platform_price_sale")

ADDON = bot_addons.PRICE_ADDON
DEFAULT_PRICE = 150_000
PAGE = 15


class PriceSaleFSM(StatesGroup):
    price = State()
    grant = State()


def _rial(n) -> str:
    return f"{int(n):,} تومان"


async def sale_price() -> int:
    from platform_bot import get_pset
    raw = (await get_pset("pq_sale_price", str(DEFAULT_PRICE))).replace(",", "").replace("،", "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_PRICE


async def sale_enabled() -> bool:
    from platform_bot import get_pset
    return (await get_pset("pq_sale_enabled", "on")) == "on"


# ───────────────────────── مالک ربات ─────────────────────────
async def mybot_button(bot_id: int):
    st = await bot_addons.addon_state(bot_id, ADDON)
    if st == "none" and not await sale_enabled():
        return None
    mark = {"on": " ✅", "off": " ⛔"}.get(st, "")
    return hx_button("🔎 قیمت‌یاب" + mark, callback_data=f"ppq:menu:{bot_id}")


def _bid(cb: CallbackQuery) -> int:
    try:
        return int((cb.data or "").split(":")[2])
    except (IndexError, ValueError):
        return 0


async def _owned_bot(cb: CallbackQuery, bot_id: int):
    async with Session() as s:
        r = await s.get(BotRow, bot_id)
    if not r or r.owner_id != cb.from_user.id:
        await cb.answer("ربات یافت نشد", show_alert=True)
        return None
    if r.archived_at:
        await cb.answer("این ربات در بایگانی است.", show_alert=True)
        return None
    return r


async def menu_content(r: BotRow, uid: int):
    st = await bot_addons.addon_state(r.id, ADDON)
    price = 0 if uid == PLATFORM_ADMIN_ID else await sale_price()
    head = (f"🔎 <b>قیمت‌یاب برای @{r.username}</b>\n\n"
            "کاربران ربات تو در پیوی یا گروه می‌نویسند «تون»، «5 تون»، «btc»، «قیمت pepe»، آدرس قرارداد یک توکن، "
            "«2 گرم طلا»، «5 دلار» یا «سکه امامی» و ربات جواب می‌دهد:\n"
            "• قیمت دلاری و تومانی، درصد تغییر و سود و ضرر 24 ساعته\n"
            "• رتبه، سقف و کف، حجم معاملات، ساپلای و مارکت‌کپ\n"
            "• تصویر نمودار کندلی با آیدی ربات خودت\n"
            "• همهٔ ارزهای معروف و توکن‌های تازه (TON، اتریوم، BSC، سولانا، ترون …)\n"
            "• دکمه‌های شیشه‌ای دلخواه زیر هر پیام (متن، لینک و چیدمان دست خودت)\n\n")
    back = [hx_button("⬅️ بازگشت", callback_data=f"mb:{r.id}", style=ButtonStyle.DANGER)]
    if st == "on":
        return head + ("✅ <b>برای این ربات فعال است.</b>\n\n"
                       "روشن/خاموش و دکمه‌ها: داخل ربات خودت ← دکمه‌ها و تنظیمات ← 🔎 قیمت‌یاب."), [back]
    if st == "off":
        return head + "⛔ این قابلیت برای این ربات توسط مدیر بات‌ساز غیرفعال شده است؛ با پشتیبانی در تماس باش.", [back]
    if not await sale_enabled():
        return head + "فروش این قابلیت موقتا غیرفعال است.", [back]
    line = ("💰 قیمت: <b>رایگان</b> (مدیر اصلی)" if price == 0
            else f"💰 قیمت: <b>{_rial(price)}</b> — یک‌بار، دائمی برای همین ربات؛ از کیف‌پول بات‌ساز تو کسر می‌شود.")
    label = "✅ پرداخت و فعال‌سازی" + (f" ({_rial(price)})" if price else "")
    return head + line + "\n\nوضعیت: ❌ فعال نشده", [
        [hx_button(label, callback_data=f"ppq:buy:{r.id}", style=ButtonStyle.SUCCESS)], back]


async def _edit(cb: CallbackQuery, text: str, rows):
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@platform_price_sale_router.callback_query(F.data.startswith("ppq:menu:"))
async def ppq_menu(cb: CallbackQuery):
    r = await _owned_bot(cb, _bid(cb))
    if not r:
        return
    text, rows = await menu_content(r, cb.from_user.id)
    await _edit(cb, text, rows)
    await cb.answer()


@platform_price_sale_router.callback_query(F.data.startswith("ppq:buy:"))
async def ppq_buy(cb: CallbackQuery):
    from platform_bot import _insufficient_balance, get_user
    r = await _owned_bot(cb, _bid(cb))
    if not r:
        return
    uid = cb.from_user.id
    is_admin = uid == PLATFORM_ADMIN_ID
    if not is_admin and not await sale_enabled():
        await cb.answer("فروش این قابلیت موقتا غیرفعال است.", show_alert=True)
        return
    price = 0 if is_admin else await sale_price()
    async with Session() as s:
        u = await get_user(s, uid, cb.from_user.full_name, for_update=True, username=cb.from_user.username)
        # بعد از قفل کیف‌پول: کلیک هم‌زمان دوم هرگز دوبار پول کم نمی‌کند
        if await s.scalar(select(BotAddon.id).where(BotAddon.bot_id == r.id, BotAddon.addon == ADDON)):
            await s.commit()
            bot_addons.invalidate(r.id)
            await cb.answer("برای این ربات قبلا خریداری شده است.", show_alert=True)
            return
        if price and float(u.balance) < price:
            await s.commit()
            await _insufficient_balance(cb, price, note="برای فعال‌سازی قیمت‌یاب لازم است.")
            return
        if price:
            u.balance = float(u.balance) - price
            s.add(PlatformTxn(user_telegram_id=uid, amount=-price, kind="purchase",
                              note=f"فعال‌سازی قیمت‌یاب برای @{r.username}"[:200]))
        s.add(BotAddon(bot_id=r.id, addon=ADDON, amount=price, paid_by=uid, source="wallet" if price else "free"))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            await cb.answer("برای این ربات قبلا خریداری شده است.", show_alert=True)
            return
    bot_addons.invalidate(r.id)
    await cb.answer("✅ فعال شد")
    paid = f"\n💳 {_rial(price)} از کیف‌پول بات‌ساز تو کسر شد." if price else ""
    await cb.message.answer(
        f"✅ قیمت‌یاب برای @{r.username} فعال شد (حداکثر تا 15 ثانیه دیگر در ربات اعمال می‌شود).{paid}\n\n"
        "مرحلهٔ بعد: داخل ربات خودت ← دکمه‌ها و تنظیمات ← 🔎 قیمت‌یاب ← «🟢 روشن کردن»؛ "
        "از همان‌جا دکمه‌های زیر پیام قیمت را هم بچین.")
    if not is_admin:
        try:
            await cb.bot.send_message(PLATFORM_ADMIN_ID, f"🔎 خرید قیمت‌یاب: @{r.username} (مالک {uid}) — {_rial(price)}")
        except Exception:  # noqa: BLE001
            logger.info("price sale: admin notice skipped")


# ───────────────────────── مدیر اصلی ─────────────────────────
def _admin(ev) -> bool:
    return ev.from_user.id == PLATFORM_ADMIN_ID


async def hub_content():
    async with Session() as s:
        total = int(await s.scalar(select(func.count(BotAddon.id)).where(BotAddon.addon == ADDON)) or 0)
        off = int(await s.scalar(select(func.count(BotAddon.id)).where(BotAddon.addon == ADDON, BotAddon.source == "off")) or 0)
        income = float(await s.scalar(select(func.coalesce(func.sum(BotAddon.amount), 0)).where(BotAddon.addon == ADDON)) or 0)
    price, on = await sale_price(), await sale_enabled()
    text = ("🔎 <b>فروش قیمت‌یاب به ربات‌ها</b>\n\n"
            "مالک هر ربات از «ربات‌های من ← همان ربات ← 🔎 قیمت‌یاب» می‌خرد؛ مبلغ از کیف‌پول بات‌ساز کم می‌شود و "
            "قابلیت فقط برای همان ربات فعال می‌شود.\n\n"
            f"فروش: <b>{'✅ روشن' if on else '❌ خاموش'}</b>\n"
            f"قیمت: <b>{_rial(price)}</b>\n"
            f"ربات‌های خریدار: <b>{total}</b> (فعال {total - off}، غیرفعال {off})\n"
            f"جمع درآمد: <b>{_rial(income)}</b>")
    rows = [[hx_button(f"💵 تعیین قیمت ({_rial(price)})", callback_data="ppq:price", style=ButtonStyle.PRIMARY)],
            [hx_button("🔴 خاموش‌کردن فروش" if on else "🟢 روشن‌کردن فروش", callback_data="ppq:toggle",
                       style=ButtonStyle.DANGER if on else ButtonStyle.SUCCESS)],
            [hx_button("📋 ربات‌های خریدار (فعال/غیرفعال)", callback_data="ppq:list:0", style=ButtonStyle.PRIMARY)],
            [hx_button("➕ فعال‌سازی دستی برای یک ربات", callback_data="ppq:grant", style=ButtonStyle.PRIMARY)],
            [hx_button("⬅️ بازگشت", callback_data="adm:cat:settings", style=ButtonStyle.DANGER)]]
    return text, rows


@platform_price_sale_router.callback_query(F.data.in_({"ppq:hub", "ppq:toggle"}))
async def ppq_hub(cb: CallbackQuery):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    if cb.data == "ppq:toggle":
        from platform_bot import set_pset
        await set_pset("pq_sale_enabled", "off" if await sale_enabled() else "on")
    text, rows = await hub_content()
    await _edit(cb, text, rows)
    await cb.answer()


async def list_content(page: int):
    async with Session() as s:
        rows = (await s.execute(
            select(BotAddon, BotRow.username).join(BotRow, BotRow.id == BotAddon.bot_id, isouter=True)
            .where(BotAddon.addon == ADDON).order_by(BotAddon.id.desc()).offset(page * PAGE).limit(PAGE + 1))).all()
    more = len(rows) > PAGE
    rows = rows[:PAGE]
    text = ("📋 <b>ربات‌های خریدار قیمت‌یاب</b>\n"
            "روی هر ربات بزن تا فعال/غیرفعال شود (✅ فعال، ⛔ غیرفعال). پول برگشت داده نمی‌شود؛ فقط قابلیت قطع/وصل می‌شود.")
    if not rows:
        text += "\n\nهنوز هیچ رباتی نخریده است."
    kb = []
    for a, uname in rows:
        mark = "⛔" if a.source == "off" else "✅"
        paid = "رایگان" if not a.amount else _rial(a.amount)
        kb.append([hx_button(f"{mark} @{uname or a.bot_id} — {paid}", callback_data=f"ppq:t:{a.bot_id}:{page}",
                             style=ButtonStyle.DANGER if a.source == "off" else ButtonStyle.SUCCESS)])
    nav = []
    if page:
        nav.append(hx_button("◀️ قبلی", callback_data=f"ppq:list:{page - 1}"))
    if more:
        nav.append(hx_button("بعدی ▶️", callback_data=f"ppq:list:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([hx_button("⬅️ بازگشت", callback_data="ppq:hub", style=ButtonStyle.DANGER)])
    return text, kb


@platform_price_sale_router.callback_query(F.data.startswith("ppq:list:") | F.data.startswith("ppq:t:"))
async def ppq_list(cb: CallbackQuery):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    parts = cb.data.split(":")
    if parts[1] == "t":
        bot_id, page = int(parts[2]), int(parts[3])
        async with Session() as s:
            a = await s.scalar(select(BotAddon).where(BotAddon.bot_id == bot_id, BotAddon.addon == ADDON))
            if a is not None:
                a.source = ("wallet" if a.amount else "admin") if a.source == "off" else "off"
                await s.commit()
        bot_addons.invalidate(bot_id)
    else:
        page = int(parts[2])
    text, kb = await list_content(max(0, page))
    await _edit(cb, text, kb)
    await cb.answer()


@platform_price_sale_router.callback_query(F.data.in_({"ppq:price", "ppq:grant"}))
async def ppq_ask(cb: CallbackQuery, state: FSMContext):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    if cb.data == "ppq:price":
        await state.set_state(PriceSaleFSM.price)
        await cb.message.answer("💵 قیمت فعال‌سازی قیمت‌یاب برای هر ربات را به تومان بفرست (مثلا 150000؛ صفر = رایگان):")
    else:
        await state.set_state(PriceSaleFSM.grant)
        await cb.message.answer("آیدی ربات (مثل @mybot) یا شمارهٔ ربات را بفرست تا قیمت‌یاب رایگان برایش فعال شود:")
    await cb.answer()


@platform_price_sale_router.message(PriceSaleFSM.price, F.text)
async def ppq_price_save(msg: Message, state: FSMContext):
    if not _admin(msg):
        await state.clear()
        return
    raw = (msg.text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).replace(",", "").replace("،", "").strip()
    if not raw.isdigit() or int(raw) > 10 ** 9:
        await msg.answer("❌ یک عدد معتبر (تومان) بفرست.")
        return
    await state.clear()
    from platform_bot import set_pset
    await set_pset("pq_sale_price", str(int(raw)))
    text, rows = await hub_content()
    await msg.answer(f"✅ قیمت قیمت‌یاب: {_rial(int(raw))}")
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@platform_price_sale_router.message(PriceSaleFSM.grant, F.text)
async def ppq_grant_save(msg: Message, state: FSMContext):
    if not _admin(msg):
        await state.clear()
        return
    raw = (msg.text or "").strip().lstrip("@")
    async with Session() as s:
        if raw.isdigit():
            r = await s.get(BotRow, int(raw))
        else:
            r = await s.scalar(select(BotRow).where(func.lower(BotRow.username) == raw.lower()))
        if r is None:
            await msg.answer("❌ ربات پیدا نشد. دوباره بفرست یا /cancel.")
            return
        a = await s.scalar(select(BotAddon).where(BotAddon.bot_id == r.id, BotAddon.addon == ADDON))
        if a is None:
            s.add(BotAddon(bot_id=r.id, addon=ADDON, amount=0, paid_by=msg.from_user.id, source="admin"))
        elif a.source == "off":
            a.source = "wallet" if a.amount else "admin"
        await s.commit()
        uname, owner = r.username, r.owner_id
    await state.clear()
    bot_addons.invalidate(r.id)
    await msg.answer(f"✅ قیمت‌یاب برای @{uname} فعال شد.")
    try:
        await msg.bot.send_message(owner, f"🎁 قیمت‌یاب برای ربات @{uname} فعال شد.\n"
                                          "داخل ربات ← دکمه‌ها و تنظیمات ← 🔎 قیمت‌یاب ← روشن کردن.")
    except Exception:  # noqa: BLE001
        logger.info("price grant: owner notice skipped")
