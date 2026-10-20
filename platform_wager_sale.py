"""فروش افزونهٔ «بازی‌های گروهی» (ایکس‌او / سنگ‌کاغذ‌قیچی / تاس با مبلغ کیف‌پول) به ربات‌های مستأجر، از ربات اصلی.

=== PATCH_WAGER_ADDON_V1 ===
مسیر مالک: ربات‌های من ← انتخاب ربات ← «🎮 بازی‌های گروهی» ← پرداخت (از کیف‌پول بات‌ساز) ← فعال.
قیمت و روشن/خاموش فروش را مدیر اصلی از «🎮 بازی‌ها و شرط‌بندی» تعیین می‌کند (پیش‌فرض ۱۰۰٬۰۰۰ تومان).
پرداخت یک‌بار و دائمی است (BotAddon)؛ بعد از آن ایکس‌او/سنگ‌کاغذ‌قیچی/تاس در گروه همان ربات کار می‌کند
(tenant/shared/wager_games.py). مدیر اصلی برای ربات خودش رایگان فعال می‌کند. خرید دوباره‌کاری و کسر دوباره ندارد
(بررسی وجود فعال‌سازی *بعد از* قفل ردیف کیف‌پول).
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import bot_addons
from config import PLATFORM_ADMIN_ID, Session
from logging_setup import get_logger as _get_logger
from models import Bot as BotRow, BotAddon, PlatformTxn
from premium_emoji import hx_button

logger = _get_logger(__name__)

platform_wager_sale_router = Router(name="platform_wager_sale")

DEFAULT_PRICE = 100_000
# برای مالک ربات (خریدار) عمدا صریح است: بازی‌ها با پول کیف‌پول انجام می‌شود
LEGAL_NOTE = ("⚠️ این بازی‌ها با پول کیف‌پول کاربران انجام می‌شود (شرط‌بندی مالی بین بازیکن‌ها). "
              "مسئولیت قانونی راه‌اندازی و استفاده از آن در گروه و کشور شما با صاحب ربات است.")


class WagerSaleFSM(StatesGroup):
    price = State()


def _rial(n) -> str:
    return f"{int(n):,} تومان"


async def _pset(key: str, default: str = "") -> str:
    from platform_bot import get_pset
    return await get_pset(key, default)


async def sale_price() -> int:
    raw = (await _pset("wager_sale_price", str(DEFAULT_PRICE))).replace(",", "").replace("،", "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_PRICE


async def sale_enabled() -> bool:
    return (await _pset("wager_sale_enabled", "on")) == "on"


# ───────────────────────── دکمهٔ «ربات‌های من» ─────────────────────────

async def mybot_button(bot_id: int, field: str | None, plan: str | None):
    """دکمهٔ صفحهٔ هر ربات؛ None اگر نباید نشان داده شود (نوع ربات پشتیبانی نمی‌شود یا فروش خاموش است و فعال هم نشده)."""
    if not bot_addons.wager_eligible(field, plan):
        return None
    active = await bot_addons.addon_active(bot_id)
    if not active and not await sale_enabled():
        return None
    return hx_button("🎮 بازی‌های گروهی" + (" ✅" if active else ""), callback_data=f"wgr:menu:{bot_id}")


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


def _bid(cb: CallbackQuery) -> int:
    try:
        return int((cb.data or "").split(":")[2])
    except (IndexError, ValueError):
        return 0


async def _menu_content(r: BotRow, uid: int):
    active = await bot_addons.addon_active(r.id)
    price = 0 if uid == PLATFORM_ADMIN_ID else await sale_price()
    head = (
        f"🎮 <b>بازی‌های گروهی برای @{r.username}</b>\n\n"
        "با فعال‌سازی، کاربران ربات تو می‌توانند داخل گروه، با موجودی کیف‌پول همین ربات، "
        "<b>ایکس‌او، سنگ‌کاغذ‌قیچی و تاس</b> بازی کنند. برنده جایزه را می‌گیرد.\n\n"
        "• برای کاربران کاملا اختیاری است: هر کس خودش دستور را می‌نویسد یا روی «قبول» می‌زند.\n"
        "• کارمزد (اختیاری)، حداقل و حداکثر مبلغ، زمان انتظار و روشن/خاموش‌کردن هر بازی دست خودت است "
        "(در ربات خودت: دکمه‌ها و تنظیمات ← تنظیمات گروه ← تنظیمات بازی).\n"
        "• ایکس‌او: سازنده نماد خودش (❌ یا ⭕) را انتخاب می‌کند. تاس: سازنده تعیین می‌کند عدد بزرگ‌تر برنده است یا "
        "کوچک‌تر. سنگ‌کاغذ‌قیچی: هر کس زودتر ۳ راند ببرد برنده است. تاس و سنگ‌کاغذ‌قیچی ۵ دقیقه مهلت دارند.\n"
        "• تاس را خود تلگرام می‌اندازد؛ مبلغ‌ها اتمیک جابه‌جا و در مساوی یا لغو برگردانده می‌شود.\n"
        "• بازی «شیر یا خط» همین الان برای ربات‌های واجد شرایط جداست و به این خرید وابسته نیست.\n\n")
    if active:
        body = ("✅ <b>برای این ربات فعال است.</b>\n\n"
                "مراحل استفاده:\n"
                "1) ربات را در گروه ادمین کن (یا Group Privacy را در BotFather خاموش کن).\n"
                "2) در ربات: دکمه‌ها و تنظیمات ← تنظیمات گروه ← «🎲 تنظیمات بازی» ← کلید بازی را روشن کن.\n"
                "3) در گروه بنویس: <code>xo 10000</code> یا <code>سنگ کاغذ قیچی 10000</code> یا <code>تاس 10000</code>.")
        kb = [[hx_button("⬅️ بازگشت", callback_data=f"mb:{r.id}", style=ButtonStyle.DANGER)]]
        return head + body, kb
    if not bot_addons.wager_eligible(r.field, r.plan):
        body = ("این نوع ربات فعلا از بازی‌های گروهی کیف‌پولی پشتیبانی نمی‌کند "
                "(فقط ربات‌های خدمات مجازی و VPN با پلن رشد/حرفه‌ای).")
        return head + body, [[hx_button("⬅️ بازگشت", callback_data=f"mb:{r.id}", style=ButtonStyle.DANGER)]]
    if not await sale_enabled():
        return head + "فروش این افزونه موقتا غیرفعال است.", [
            [hx_button("⬅️ بازگشت", callback_data=f"mb:{r.id}", style=ButtonStyle.DANGER)]]
    price_line = ("💰 قیمت: <b>رایگان</b> (مدیر اصلی)" if uid == PLATFORM_ADMIN_ID and price == 0
                  else f"💰 قیمت: <b>{_rial(price)}</b> — یک‌بار، دائمی برای همین ربات؛ از کیف‌پول بات‌ساز تو کسر می‌شود.")
    body = f"{price_line}\n{LEGAL_NOTE}\n\nوضعیت: ❌ فعال نشده"
    label = "✅ پرداخت و فعال‌سازی" + (f" ({_rial(price)})" if price else "")
    kb = [[hx_button(label, callback_data=f"wgr:buy:{r.id}", style=ButtonStyle.SUCCESS)],
          [hx_button("⬅️ بازگشت", callback_data=f"mb:{r.id}", style=ButtonStyle.DANGER)]]
    return head + body, kb


@platform_wager_sale_router.callback_query(F.data.startswith("wgr:menu:"))
async def wgr_menu(cb: CallbackQuery):
    r = await _owned_bot(cb, _bid(cb))
    if not r:
        return
    text, rows = await _menu_content(r, cb.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_wager_sale_router.callback_query(F.data.startswith("wgr:buy:"))
async def wgr_buy(cb: CallbackQuery):
    from platform_bot import _insufficient_balance, get_user
    r = await _owned_bot(cb, _bid(cb))
    if not r:
        return
    if not bot_addons.wager_eligible(r.field, r.plan):
        await cb.answer("این نوع ربات از این قابلیت پشتیبانی نمی‌کند.", show_alert=True)
        return
    uid = cb.from_user.id
    is_admin = uid == PLATFORM_ADMIN_ID
    if not is_admin and not await sale_enabled():
        await cb.answer("فروش این افزونه موقتا غیرفعال است.", show_alert=True)
        return
    price = 0 if is_admin else await sale_price()
    async with Session() as s:
        u = await get_user(s, uid, cb.from_user.full_name, for_update=True, username=cb.from_user.username)
        # بعد از قفل کیف‌پول چک کن: کلیک هم‌زمان دوم هرگز دوبار پول نمی‌دهد
        if await s.scalar(select(BotAddon.id).where(BotAddon.bot_id == r.id, BotAddon.addon == bot_addons.WAGER_ADDON)):
            await s.commit()
            bot_addons.invalidate(r.id)
            await cb.answer("برای این ربات قبلا فعال شده است.", show_alert=True)
            return
        if price and float(u.balance) < price:
            await s.commit()
            await _insufficient_balance(cb, price, note="برای فعال‌سازی بازی‌های گروهی لازم است.")
            return
        if price:
            u.balance = float(u.balance) - price
            s.add(PlatformTxn(user_telegram_id=uid, amount=-price, kind="purchase",
                              note=f"فعال‌سازی بازی‌های گروهی برای @{r.username}"[:200]))
        s.add(BotAddon(bot_id=r.id, addon=bot_addons.WAGER_ADDON, amount=price, paid_by=uid,
                       source="free" if not price else "wallet"))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            await cb.answer("برای این ربات قبلا فعال شده است.", show_alert=True)
            return
    bot_addons.invalidate(r.id)
    await cb.answer("✅ فعال شد")
    paid = f"\n💳 {_rial(price)} از کیف‌پول بات‌ساز تو کسر شد." if price else ""
    await cb.message.answer(
        f"✅ بازی‌های گروهی برای @{r.username} فعال شد (حداکثر تا ۱۵ ثانیه دیگر در ربات اعمال می‌شود).{paid}\n\n"
        "مراحل: ربات را در گروه ادمین کن ← در ربات: دکمه‌ها و تنظیمات ← تنظیمات گروه ← «🎲 تنظیمات بازی» ← "
        "بازی را روشن کن ← در گروه بنویس <code>xo 10000</code> یا <code>تاس 10000</code>.",
        parse_mode="HTML")
    try:                                   # خبر به مدیر اصلی (بی‌خطر)
        if not is_admin:
            await cb.bot.send_message(
                PLATFORM_ADMIN_ID,
                f"🎮 فعال‌سازی بازی‌های گروهی: @{r.username} (مالک {uid}) — {_rial(price)}")
    except Exception:  # noqa: BLE001
        logger.info("wager sale: admin notice skipped")


# ───────────────────────── تنظیمات فروش (مدیر اصلی، داخل «بازی‌ها و شرط‌بندی») ─────────────────────────

async def hub_sale_section() -> tuple[str, list[list]]:
    price, on, n = await sale_price(), await sale_enabled(), await bot_addons.active_count()
    text = (f"\n\n🏪 <b>فروش به ربات‌های مشتری</b>\n"
            f"فروش: {'✅ روشن' if on else '❌ خاموش'} | قیمت: {_rial(price)} | ربات‌های فعال‌شده: {n}")
    rows = [[hx_button(f"💵 قیمت فعال‌سازی ({_rial(price)})", callback_data="wgrs:price")],
            [hx_button("🔴 خاموش‌کردن فروش" if on else "🟢 روشن‌کردن فروش", callback_data="wgrs:toggle")]]
    return text, rows


def _admin(cb: CallbackQuery) -> bool:
    return cb.from_user.id == PLATFORM_ADMIN_ID


@platform_wager_sale_router.callback_query(F.data == "wgrs:toggle")
async def wgrs_toggle(cb: CallbackQuery):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    from platform_bot import set_pset
    await set_pset("wager_sale_enabled", "off" if await sale_enabled() else "on")
    from platform_wager import wager_hub
    await wager_hub(cb)


@platform_wager_sale_router.callback_query(F.data == "wgrs:price")
async def wgrs_price_start(cb: CallbackQuery, state: FSMContext):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(WagerSaleFSM.price)
    await cb.message.answer("💵 قیمت فعال‌سازی بازی‌های گروهی برای هر ربات را به تومان بفرست (مثلا 100000؛ صفر = رایگان):")
    await cb.answer()


@platform_wager_sale_router.message(WagerSaleFSM.price, F.text)
async def wgrs_price_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    raw = (msg.text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).replace(",", "").replace("،", "").strip()
    if not raw.isdigit() or int(raw) > 10**9:
        await msg.answer("❌ یک عدد معتبر (تومان) بفرست.")
        return
    await state.clear()
    from platform_bot import set_pset
    await set_pset("wager_sale_price", str(int(raw)))
    await msg.answer(f"✅ قیمت فعال‌سازی: {_rial(int(raw))}")
