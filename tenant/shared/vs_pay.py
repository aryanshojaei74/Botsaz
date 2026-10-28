"""
vs_pay.py — پرداخت از صفحهٔ فاکتورِ خدماتِ مجازی (PATCH_VS_PAY_V1).

گزارشِ فدایی (ربات Express): صفحهٔ فاکتورِ استارز/گیفت/پریمیوم روش‌هایِ پرداخت را دستی می‌ساخت و
  ۱) تیکِ «👛 پرداخت از کیف پول در خرید محصولات» را نادیده می‌گرفت (دکمهٔ «از کیف‌پول» همیشه بود)،
  ۲) تیکِ «🔋 شارژ کیف پول در صفحه خرید» را نادیده می‌گرفت و برایِ شارژ فقط کارت‌به‌کارت بود،
  ۳) وقتی موجودی کم بود فقط یک هشدار می‌داد؛ راهی برایِ «شارژ حساب و ثبت سفارش» نبود،
  ۴) کارت‌به‌کارتِ مستقیم را بدونِ هیچ چکِ سقفِ احرازِ هویت می‌پذیرفت.

این ماژول یک نقطهٔ مشترک است:
  • pay_rows(...)        — دکمه‌هایِ روشِ پرداختِ فاکتور با رعایتِ تیک‌هایِ مالک.
  • kyc_block(...)       — سقفِ احراز پیش از نمایشِ شماره‌کارت (همان گیتِ شارژ، با شمارشِ خریدهایِ کارتیِ مستقیم).
  • insufficient_notice  — «موجودی کافی نیست» + دکمهٔ «➕ شارژ حساب و ثبت سفارش».
  • vsc:o:<id>           — لیستِ روش‌هایِ فعالِ شارژ؛ برایِ درگاه‌هایی که مبلغِ ثابت می‌گیرند همان مبلغِ فاکتور از قبل پر است.
  • هوکِ after_flush     — وقتی کیف پول شارژ شد، به کاربر «✅ ثبتِ سفارش» (همان دکمهٔ پرداختِ فاکتور) فرستاده می‌شود.

فاکتورهایِ اخیرِ هر کاربر در جدولِ Setting (کلیدِ vs_inv:<tg>) نگه داشته می‌شود؛ مبلغ از سمتِ سرور خوانده می‌شود،
نه از callback.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import event, select
from sqlalchemy.orm import Session as SyncSession

from config import Session
from models import Bot as BotRow, BotUser, Setting, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

vs_pay_router = Router(name="vs_pay")

INV_KEY = "vs_inv:"            # + telegram_id
MAX_ITEMS = 6
TTL_SEC = 6 * 3600             # فاکتورِ قدیمی‌تر از این دیگر ادامه داده نمی‌شود

# درگاه‌هایی که یک callbackِ «مبلغِ ثابت» دارند: callbackِ عمومیِ کیف پول ← (کلیدِ محدودیت، قالبِ callback)
_FIXED_AMOUNT = {
    "w:charge": ("card", "wcamt:{a}"),
    "w:zarinpal": ("zarinpal", "wzp:amt:{a}"),
    "w:zibal": ("zibal", "wzb:amt:{a}"),
    "w:blupal": ("blupal", "bpu:amt:{a}"),      # PATCH_BLUPAL_UX_V1: مبلغِ کسری روی دکمه و فاکتورِ مستقیم
}


# ───────────────────────── تیک‌هایِ مالک ─────────────────────────

async def wallet_pay_allowed(bot_id: int) -> bool:
    from tenant import wallet_pay_on
    return await wallet_pay_on(bot_id)


async def charge_btn_enabled(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, "wallet_charge_in_purchase_enabled", "0")) == "1"


async def card_pay_allowed(bot_id: int) -> bool:
    """همان شرطِ pay_method_kb_full: کارت‌به‌کارتِ فعال، فعال در بخشِ خرید، و جایگزینِ آبان‌گیت‌وی نشده."""
    from tenant import _card_replaced, get_setting
    if (await get_setting(bot_id, "card_enabled", "1")) != "1":
        return False
    if (await get_setting(bot_id, "card_purchase_enabled", "1")) != "1":
        return False
    return not await _card_replaced(bot_id)


# ───────────────────────── ذخیرهٔ فاکتورها ─────────────────────────

def _new_id(pay_cb: str) -> str:
    return hashlib.sha1(pay_cb.encode("utf-8")).hexdigest()[:8]


def _prune(data: dict, now: int) -> dict:
    items = {k: v for k, v in (data.get("items") or {}).items() if now - int(v.get("ts", 0)) <= TTL_SEC}
    if len(items) > MAX_ITEMS:
        keep = sorted(items, key=lambda k: items[k].get("ts", 0), reverse=True)[:MAX_ITEMS]
        items = {k: items[k] for k in keep}
    armed = data.get("armed") if data.get("armed") in items else None
    return {"items": items, "armed": armed}


async def _load(bot_id: int, tg_id: int) -> dict:
    async with Session() as s:
        v = await s.scalar(select(Setting.value).where(
            Setting.bot_id == bot_id, Setting.key == INV_KEY + str(tg_id)))
    try:
        d = json.loads(v) if v else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


async def _save(bot_id: int, tg_id: int, data: dict) -> None:
    key = INV_KEY + str(tg_id)
    val = json.dumps(data, ensure_ascii=False)
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
        if row:
            row.value = val
        else:
            s.add(Setting(bot_id=bot_id, key=key, value=val))
        try:
            await s.commit()
        except Exception:  # noqa: BLE001 — رقابتِ هم‌زمان؛ آخرین نوشتن برنده است
            await s.rollback()
            logger.info("vs_pay: ذخیرهٔ فاکتورِ %s ناموفق بود", tg_id)


async def remember(bot_id: int, tg_id: int, pay_cb: str, price: int) -> str:
    """فاکتور (callbackِ پرداخت + مبلغ) را برایِ «شارژ و ادامه» نگه می‌دارد؛ شناسهٔ کوتاه برمی‌گرداند."""
    now = int(time.time())
    iid = _new_id(pay_cb)
    d = _prune(await _load(bot_id, tg_id), now)
    d["items"][iid] = {"cb": pay_cb, "price": int(price), "ts": now}
    await _save(bot_id, tg_id, _prune(d, now))
    return iid


async def _balance(bot_id: int, tg_id: int) -> int:
    async with Session() as s:
        v = await s.scalar(select(BotUser.balance).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


# ───────────────────────── دکمه‌هایِ فاکتور ─────────────────────────

def tg_of(target) -> int:
    """آیدیِ تلگرامِ کاربرِ این گفتگو (پیامی که ربات در چتِ خصوصی می‌فرستد)."""
    try:
        return int(getattr(getattr(target, "chat", None), "id", 0) or 0)
    except (TypeError, ValueError):
        return 0


async def pay_rows(bot_id: int, tg_id: int, price: int, pay_cb: str, card_cb: str | None = None,
                   wallet_label: str = "💰 از کیف‌پول") -> list[list[InlineKeyboardButton]]:
    """ردیف‌هایِ روشِ پرداختِ فاکتور؛ خالی یعنی مالک همهٔ روش‌ها را خاموش کرده."""
    rows: list[list[InlineKeyboardButton]] = []
    wallet_on = await wallet_pay_allowed(bot_id)
    if wallet_on:
        rows.append([InlineKeyboardButton(text=wallet_label, callback_data=pay_cb, style=ButtonStyle.SUCCESS)])
    if card_cb and await card_pay_allowed(bot_id):
        rows.append([InlineKeyboardButton(text="💳 کارت‌به‌کارت", callback_data=card_cb, style=ButtonStyle.PRIMARY)])
    if wallet_on and await charge_btn_enabled(bot_id):
        short = max(0, int(price) - await _balance(bot_id, tg_id))
        iid = await remember(bot_id, tg_id, pay_cb, price)
        rows.append([InlineKeyboardButton(
            text=("➕ شارژ حساب و ثبت سفارش" if short else "🔋 شارژ حساب"),
            callback_data=f"vsc:o:{iid}", style=ButtonStyle.SUCCESS)])
    return rows


async def charge_row(bot_id: int, tg_id: int, price: int) -> list[list[InlineKeyboardButton]]:
    """یک ردیفِ «➕ شارژ حساب» برایِ پیام‌هایِ «موجودی کافی نیست» که خودشان ادامهٔ سفارش ندارند (تون، سفارشِ SMM…):
    بعد از شارژ کاربر دوباره از موجودی خرید می‌کند. بدونِ ثبتِ ادامه (callbackِ پرداختِ فاکتور ندارد)."""
    try:
        iid = await remember(bot_id, tg_id, f"charge:{int(price)}", price)
    except Exception:  # noqa: BLE001 — نبودِ دکمه نباید پیامِ اصلی را از بین ببرد
        logger.exception("vs_pay: ثبتِ دکمهٔ شارژ ناموفق بود")
        return []
    return [[InlineKeyboardButton(text="➕ شارژ حساب", callback_data=f"vsc:o:{iid}", style=ButtonStyle.SUCCESS)]]


NO_METHOD_NOTE = "\n\n⚠️ در حالِ حاضر هیچ روشِ پرداختی برایِ این فاکتور فعال نیست؛ با پشتیبانی تماس بگیر."


async def insufficient_notice(target, bot_id: int, tg_id: int, price: int, pay_cb: str) -> None:
    """وقتی «پرداخت از کیف‌پول» موجودی کافی نداشت: پیام + دکمهٔ «➕ شارژ حساب و ثبت سفارش».
    target: Message یا CallbackQuery.message (جایی که پیام فرستاده می‌شود)."""
    bal = await _balance(bot_id, tg_id)
    short = max(0, int(price) - bal)
    iid = await remember(bot_id, tg_id, pay_cb, price)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ شارژ حساب و ثبت سفارش", callback_data=f"vsc:o:{iid}",
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="✖️ بستن", callback_data="vsc:x")],
    ])
    await target.answer(
        "⚠️ <b>موجودیِ کیف‌پولت برایِ این سفارش کافی نیست.</b>\n\n"
        f"💰 موجودی: {bal:,} تومان\n🧾 مبلغِ سفارش: {int(price):,} تومان\n"
        f"➕ کمبود: <b>{short:,} تومان</b>\n\n"
        "با «شارژ حساب و ثبت سفارش» یکی از روش‌هایِ پرداختِ فعال را انتخاب کن؛ بعد از شارژ، "
        "دکمهٔ ثبتِ سفارش برایت فرستاده می‌شود.",
        reply_markup=kb, parse_mode="HTML")


# ───────────────────────── احرازِ هویت ─────────────────────────

async def kyc_block(bot_id: int, tg_id: int, amount: int, back_cb: str = "") -> tuple[str, InlineKeyboardMarkup] | None:
    """(متن، کیبورد) اگر سقفِ احرازِ هویت اجازهٔ این پرداخت را نمی‌دهد؛ وگرنه None. خطایِ داخلی مانعِ فروش نمی‌شود."""
    try:
        from tenant.shared import kyc as K
        why = await K.gate(bot_id, tg_id, amount)
    except Exception:  # noqa: BLE001
        logger.exception("vs_pay: چکِ سقفِ احراز ناموفق بود (bot=%s tg=%s)", bot_id, tg_id)
        return None
    if not why:
        return None
    rows = [[InlineKeyboardButton(text="🛡 احرازِ هویت", callback_data="kycu:menu", style=ButtonStyle.SUCCESS)]]
    if back_cb:
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb, style=ButtonStyle.DANGER)])
    return why, InlineKeyboardMarkup(inline_keyboard=rows)


# ───────────────────────── انتخابِ روشِ شارژ ─────────────────────────

async def _fixed_amount(bot_id: int, gw: str, need: int) -> int | None:
    """مبلغِ شارژ برایِ این درگاه: حداقل = کفِ درگاه؛ اگر از سقفِ درگاه بیشتر شد None (کاربر خودش مبلغ را می‌زند)."""
    try:
        from tenant.gateways import wallet_limits as _wl
        mn, mx = await _wl.get_limits(bot_id, gw)
    except Exception:  # noqa: BLE001
        return int(need)
    amt = max(int(need), int(mn))
    if mx and amt > mx:
        return None
    return amt


async def chooser_rows(bot_id: int, need: int, colored: bool) -> list[list[InlineKeyboardButton]]:
    from tenant import wallet_topup_rows
    rows = await wallet_topup_rows(bot_id, colored=colored, in_purchase=True, force_card=True)
    out: list[list[InlineKeyboardButton]] = []
    for r in rows:
        new_r = []
        for b in r:
            spec = _FIXED_AMOUNT.get(b.callback_data or "")
            amt = await _fixed_amount(bot_id, spec[0], need) if spec else None
            if spec and amt:
                b = b.model_copy(update={"callback_data": spec[1].format(a=amt),
                                         "text": f"{b.text} — {amt:,} تومان"})
            new_r.append(b)
        out.append(new_r)
    return out


@vs_pay_router.callback_query(F.data == "vsc:x")
async def vsc_close(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:  # noqa: BLE001
        logger.debug("vs_pay: بستنِ پیام ممکن نشد")
    await cb.answer()


@vs_pay_router.callback_query(F.data.startswith("vsc:o:"))
async def vsc_open(cb: CallbackQuery, bot: Bot):
    from plans import Feature, has_feature
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    iid = cb.data.rsplit(":", 1)[1]
    tg = cb.from_user.id
    now = int(time.time())
    d = _prune(await _load(row.id, tg), now)
    item = d["items"].get(iid)
    if not item:
        await cb.answer("این فاکتور منقضی شده؛ دوباره از فروشگاه وارد شو.", show_alert=True)
        return
    price = int(item["price"])
    bal = await _balance(row.id, tg)
    wallet_on = await wallet_pay_allowed(row.id)
    need = price - bal if bal < price else price
    resumable = not str(item["cb"]).startswith("charge:")
    if wallet_on and resumable:       # بعد از شارژ، دکمهٔ «ثبت سفارش» برایِ همین فاکتور فرستاده می‌شود
        d["armed"] = iid
        await _save(row.id, tg, d)
    colored = bool(has_feature(row.plan, Feature.COLORED_BUTTONS))
    rows = await chooser_rows(row.id, need, colored)
    if not rows:
        await cb.answer("در حال حاضر هیچ روشِ شارژی فعال نیست؛ با پشتیبانی تماس بگیر.", show_alert=True)
        return
    rows.append([InlineKeyboardButton(text="✖️ بستن", callback_data="vsc:x")])
    after = ("بعد از شارژ، دکمهٔ «✅ ثبتِ سفارش» برایت فرستاده می‌شود." if (wallet_on and resumable)
             else "بعد از شارژ، دوباره سفارش را ثبت کن.")
    await cb.message.answer(
        "➕ <b>شارژ حساب</b>\n\n"
        f"🧾 مبلغِ سفارش: {price:,} تومان\n💰 موجودیِ فعلی: {bal:,} تومان\n"
        f"💳 مبلغِ لازم برایِ شارژ: <b>{need:,} تومان</b>\n\n"
        f"روشِ پرداخت را انتخاب کن. {after}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


# ───────────────────────── ادامهٔ سفارش بعد از شارژ ─────────────────────────

_NON_DEPOSIT: frozenset[str] | None = None


def _non_deposit_kinds() -> frozenset[str]:
    global _NON_DEPOSIT
    if _NON_DEPOSIT is None:
        from tenant.shared.kyc import _NOT_DEPOSIT_KINDS
        _NON_DEPOSIT = frozenset(_NOT_DEPOSIT_KINDS) | {"charge_bonus", "purchase", "renew"}
    return _NON_DEPOSIT


@event.listens_for(SyncSession, "after_flush")
def _after_flush(session, flush_context):
    try:
        for t in [o for o in session.new if isinstance(o, WalletTxn)]:
            from tenant.shared.kyc import is_deposit_txn      # PATCH_KYC_DEPOSIT_WHITELIST_V1: فقط شارژِ واقعی
            if not is_deposit_txn(t.kind, t.note, t.amount):
                continue
            tg = session.execute(select(BotUser.telegram_id).where(BotUser.id == t.user_id)).scalar()
            if tg is None:
                continue
            has = session.execute(select(Setting.id).where(
                Setting.bot_id == t.bot_id, Setting.key == INV_KEY + str(int(tg)))).first()
            if has:
                session.info.setdefault("_vs_resume", set()).add((int(t.bot_id), int(tg)))
    except Exception:   # نباید شارژِ اصلی را خراب کند
        logger.exception("vs_pay: ثبتِ ادامهٔ سفارش ناموفق بود")


@event.listens_for(SyncSession, "after_commit")
def _after_commit(session):
    items = session.info.pop("_vs_resume", None)
    if not items:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("vs_pay: حلقهٔ asyncio در دسترس نبود؛ پیامِ ادامهٔ سفارش نرفت")
        return
    for bot_id, tg in items:
        loop.create_task(_notify_resume(bot_id, tg))


@event.listens_for(SyncSession, "after_rollback")
def _after_rollback(session):
    session.info.pop("_vs_resume", None)


async def _notify_resume(bot_id: int, tg: int) -> None:
    try:
        d = _prune(await _load(bot_id, tg), int(time.time()))
        iid = d.get("armed")
        item = d["items"].get(iid) if iid else None
        if not item:
            return
        price = int(item["price"])
        bal = await _balance(bot_id, tg)
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        if not brow:
            return
        if bal >= price:
            text = ("✅ <b>حسابت شارژ شد.</b>\n\n"
                    f"💰 موجودی: {bal:,} تومان\n🧾 مبلغِ سفارش: {price:,} تومان\n\n"
                    "برایِ ثبتِ سفارش دکمهٔ زیر را بزن.")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ ثبتِ سفارش", callback_data=item["cb"], style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton(text="✖️ بستن", callback_data="vsc:x")]])
            d["armed"] = None            # یک‌بار اعلام شود؛ شارژِ بعدی دوباره پیام نمی‌دهد مگر دوباره «شارژ حساب» بزند
            await _save(bot_id, tg, d)
        else:
            text = ("✅ حسابت شارژ شد، ولی هنوز برایِ این سفارش کافی نیست.\n\n"
                    f"💰 موجودی: {bal:,} تومان\n🧾 مبلغِ سفارش: {price:,} تومان\n"
                    f"➕ کمبود: <b>{price - bal:,} تومان</b>")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ شارژ حساب و ثبت سفارش", callback_data=f"vsc:o:{iid}",
                                      style=ButtonStyle.SUCCESS)]])
        tb = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await tb.send_message(tg, text, reply_markup=kb)
        finally:
            await tb.session.close()
    except Exception:  # noqa: BLE001
        logger.info("vs_pay: پیامِ ادامهٔ سفارش برایِ %s نرفت", tg)
