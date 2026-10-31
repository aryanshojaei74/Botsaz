"""
daily_report.py — «📅 گزارشِ روزانه» به تلگرامِ مالک (PATCH_DAILY_REPORT_V1).

هر روز در ساعتِ دلخواه (به وقتِ ایران) خلاصهٔ ۲۴ ساعتِ گذشته برایِ مالکِ ربات فرستاده می‌شود: کاربرانِ جدید، سفارش/فروش،
شارژها، زیرمجموعه‌ها، موجودیِ کلِ کیف‌پول‌ها و (ربات VPN) سرویس‌هایِ نزدیک به انقضا. پیش‌فرض خاموش؛ متن قابلِ ویرایش است
با متغیرهایِ {{date}} {{new_users}} {{orders}} {{sales}} {{charges}} {{charge_total}} {{referrals}} {{wallet_total}} {{expiring}}.
دسترسی: «آمار و گزارشات ← 📈 آمار زمانی ← 📅 گزارشِ روزانه».  callbackها: drep:*
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import re

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import Session
from models import Bot as BotRow, BotUser, PanelAccount, ReferralJoin, WalletTxn

from tenant.shared import period_stats as PS

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

daily_report_router = Router(name="daily_report")

_IR = _dt.timedelta(hours=3, minutes=30)
INTERVAL = 300
VARS = ("date", "new_users", "orders", "sales", "charges", "charge_total", "referrals", "wallet_total", "expiring")

DEFAULT_TEXT = ("📅 <b>گزارشِ روزانه — {{date}}</b>\n\n"
                "🆕 کاربرانِ جدید: <b>{{new_users}}</b>\n"
                "🛒 سفارش: <b>{{orders}}</b> — فروش: {{sales}} تومان\n"
                "👛 شارژِ کیف‌پول: <b>{{charges}}</b> بار — {{charge_total}} تومان\n"
                "👥 زیرمجموعهٔ جدید: <b>{{referrals}}</b>\n"
                "💰 مجموعِ موجودیِ کیف‌پولِ مشتری‌ها: {{wallet_total}} تومان\n"
                "⏳ سرویس‌هایِ نزدیک به انقضا (۳ روز): {{expiring}}")


class DrepFSM(StatesGroup):
    text = State()


def _n(v) -> str:
    return f"{int(v or 0):,}"


async def metrics(bot_id: int, now: _dt.datetime | None = None) -> dict:
    now = now or _dt.datetime.utcnow()
    since = now - _dt.timedelta(hours=24)
    async with Session() as s:
        orders = sales = 0
        for model, ts, amt, where in PS._order_sources():
            c, t = await PS._count_sum(s, model, ts, amt, where, getattr(model, "bot_id"), bot_id, since)
            orders, sales = orders + c, sales + t
        ch, cht = await PS._count_sum(s, WalletTxn, WalletTxn.created_at, WalletTxn.amount,
                                      (WalletTxn.kind.in_(PS.CHARGE_KINDS)) & (WalletTxn.amount > 0), WalletTxn.bot_id, bot_id, since)
        rf, _ = await PS._count_sum(s, ReferralJoin, ReferralJoin.created_at, ReferralJoin.reward_paid, ReferralJoin.id > 0,
                                    ReferralJoin.bot_id, bot_id, since)
        new_users = int(await s.scalar(select(func.count(BotUser.id)).where(BotUser.bot_id == bot_id, BotUser.joined_at >= since)) or 0)
        wallet = int(await s.scalar(select(func.coalesce(func.sum(BotUser.balance), 0)).where(BotUser.bot_id == bot_id)) or 0)
        soon = int(await s.scalar(select(func.count(PanelAccount.id)).where(
            PanelAccount.bot_id == bot_id, PanelAccount.status == "active", PanelAccount.expires_at.is_not(None),
            PanelAccount.expires_at >= now, PanelAccount.expires_at <= now + _dt.timedelta(days=3))) or 0)
    ir = now + _IR
    try:
        import jdatetime
        date = jdatetime.datetime.fromgregorian(datetime=ir).strftime("%Y/%m/%d")
    except Exception:      # noqa: BLE001
        date = ir.strftime("%Y-%m-%d")
    return {"date": date, "new_users": _n(new_users), "orders": _n(orders), "sales": _n(sales), "charges": _n(ch),
            "charge_total": _n(cht), "referrals": _n(rf), "wallet_total": _n(wallet), "expiring": _n(soon)}


def render(template: str, m: dict) -> str:
    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", lambda x: str(m.get(x.group(1), x.group(0))), template or DEFAULT_TEXT)


async def build(bot_id: int, now: _dt.datetime | None = None) -> str:
    from tenant import get_setting
    tpl = (await get_setting(bot_id, "daily_report_text", "") or "").strip() or DEFAULT_TEXT
    return render(tpl, await metrics(bot_id, now))


# ───────────────────────── ارسال ─────────────────────────

async def due(bot_id: int, now: _dt.datetime | None = None) -> bool:
    from tenant import get_setting
    if (await get_setting(bot_id, "daily_report_on", "0")) != "1":
        return False
    ir = (now or _dt.datetime.utcnow()) + _IR
    try:
        hour = int(await get_setting(bot_id, "daily_report_hour", "9"))
    except ValueError:
        hour = 9
    return ir.hour >= hour and (await get_setting(bot_id, "daily_report_last", "")) != ir.strftime("%Y-%m-%d")


async def run_bot_once(bot_id: int, bot, owner_id: int, now: _dt.datetime | None = None) -> bool:
    from tenant import set_setting
    now = now or _dt.datetime.utcnow()
    if not await due(bot_id, now):
        return False
    await set_setting(bot_id, "daily_report_last", (now + _IR).strftime("%Y-%m-%d"))      # اول ثبت؛ تکرارِ همان روز نشود
    await bot.send_message(owner_id, await build(bot_id, now), parse_mode="HTML")
    return True


async def run_once() -> int:
    sent = 0
    async with Session() as s:
        rows = (await s.scalars(select(BotRow).where(BotRow.status == "active"))).all()
    for row in rows:
        try:
            if not await due(row.id):
                continue
            bot = Bot(token=row.token)
            try:
                if await run_bot_once(row.id, bot, row.owner_id):
                    sent += 1
            finally:
                await bot.session.close()
        except Exception:      # noqa: BLE001
            logger.exception("daily_report: خطا (bot=%s)", row.id)
    return sent


async def daily_report_loop() -> None:
    import asyncio
    await asyncio.sleep(60)
    while True:
        try:
            await run_once()
        except Exception:      # noqa: BLE001
            logger.exception("daily_report loop crashed; retrying")
        await asyncio.sleep(INTERVAL)


# ───────────────────────── مالک ─────────────────────────

async def _view(bot_id: int):
    from tenant import get_setting
    on = (await get_setting(bot_id, "daily_report_on", "0")) == "1"
    hour = await get_setting(bot_id, "daily_report_hour", "9")
    custom = bool((await get_setting(bot_id, "daily_report_text", "") or "").strip())
    text = ("📅 <b>گزارشِ روزانه</b>\n\nهر روز خلاصهٔ ۲۴ ساعتِ گذشته در تلگرامِ مالک می‌آید.\n"
            f"وضعیت: {'✅ روشن' if on else '⬜️ خاموش'}\n⏰ ساعتِ ارسال (وقتِ ایران): <b>{hour}:00</b>\n"
            f"🧾 متن: {'سفارشی' if custom else 'پیش‌فرض'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬜️ خاموش کن" if on else "✅ روشن کن", callback_data="drep:toggle",
                              style=ButtonStyle.DANGER if on else ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text=f"⏰ ساعت: {hour}:00 (تغییر)", callback_data="drep:hour")],
        [InlineKeyboardButton(text="✏️ ویرایشِ متن", callback_data="drep:edit"),
         InlineKeyboardButton(text="♻️ متنِ پیش‌فرض", callback_data="drep:reset")],
        [InlineKeyboardButton(text="🧪 ارسالِ همین حالا", callback_data="drep:now", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="pst:menu", style=ButtonStyle.DANGER)]])
    return text, kb


async def _show(cb: CallbackQuery, bot_id: int):
    text, kb = await _view(bot_id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


async def _owner(cb, bot):
    from tenant import _owner_ok
    return await _owner_ok(cb, bot)


@daily_report_router.callback_query(F.data == "drep:menu")
async def menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row.id)
    await cb.answer()


@daily_report_router.callback_query(F.data.in_({"drep:toggle", "drep:hour", "drep:reset"}))
async def change(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting, set_setting
    row = await _owner(cb, bot)
    if not row:
        return
    if cb.data == "drep:toggle":
        on = (await get_setting(row.id, "daily_report_on", "0")) == "1"
        await set_setting(row.id, "daily_report_on", "0" if on else "1")
        if not on:
            await set_setting(row.id, "daily_report_last", "")
    elif cb.data == "drep:hour":
        h = int(await get_setting(row.id, "daily_report_hour", "9") or 9)
        await set_setting(row.id, "daily_report_hour", str((h + 1) % 24))
    else:
        await set_setting(row.id, "daily_report_text", "")
    await _show(cb, row.id)
    await cb.answer("انجام شد.")


@daily_report_router.callback_query(F.data == "drep:edit")
async def edit(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(DrepFSM.text)
    await cb.message.answer("✏️ متنِ گزارش را بفرست (HTML ساده مجاز). متغیرها:\n"
                            + " ".join(f"<code>{{{{{v}}}}}</code>" for v in VARS), parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="✖️ لغو", callback_data="drep:menu", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@daily_report_router.message(DrepFSM.text, F.text)
async def edit_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting
    row = await _owner_ok_msg(msg, bot)
    if not row:
        return
    t = (msg.text or "").strip()
    if not t or len(t) > 1500:
        return await msg.answer("متن باید ۱ تا ۱۵۰۰ کاراکتر باشد.")
    await set_setting(row.id, "daily_report_text", t)
    await state.clear()
    try:
        preview = await build(row.id)
        await msg.answer("✅ ذخیره شد. پیش‌نمایش:\n\n" + preview, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        await msg.answer("✅ ذخیره شد ولی متن با HTML سازگار نیست؛ تگ‌ها را بررسی کن.")


@daily_report_router.callback_query(F.data == "drep:now")
async def send_now(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    try:
        await cb.message.answer(await build(row.id), parse_mode="HTML")
    except Exception as e:      # noqa: BLE001
        await cb.message.answer(f"❌ ساختِ گزارش ناموفق بود: <code>{_html.escape(str(e)[:200])}</code>", parse_mode="HTML")
    await cb.answer()
