"""
platform_apay.py — درگاهِ «آپی» (کارت‌به‌کارتِ آنی — apaygate.shop) برایِ کیف پولِ خودِ بات‌ساز
(PATCH_PLATFORM_APAY_V1).

جایگزینِ «شارژِ خودکار با مبلغِ اختصاصی (فورواردِ پیامک)»: همان ایده (مبلغِ یکتا + پیامکِ بانک)،
ولی تطبیق و صفِ پیامک را خودِ آپی انجام می‌دهد. کلاینتِ API و قواعدِ امنیتی همانِ نسخهٔ
مستأجر است (tenant/gateways/apay.py)؛ این‌جا فقط ذخیره، کیف پولِ پلتفرم و منوها است.

امنیت: شارژ فقط بعد از POST /payments/:id/verify ِ خودِ آپی و تطبیقِ order_id و مبلغ با
رکوردِ ما؛ وب‌هوک (با امضایِ HMAC اگر کلید ثبت شده) فقط زنگِ بیدارباش است. شارژ اتمیک و یک‌بار.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, update as sa_update

from config import Session, WEBHOOK_BASE
from logging_setup import get_logger as _get_logger
from models import PlatformApayCharge, PlatformTxn
from tenant.gateways import apay as AP

logger = _get_logger(__name__)

papay_router = Router(name="platform_apay")

DEFAULT_LABEL = "💳 کارت‌به‌کارتِ آنی"
DEFAULT_MIN = 10_000
POLL_FOR_MINUTES = 30
PRESETS = [50_000, 100_000, 200_000, 400_000]
# === PATCH_APAY_ONE_OPEN_INVOICE_V1 === فاکتور آپی ۱۵ دقیقه اعتبار دارد؛ تا آن موقع «باز» حساب می‌شود
OPEN_INVOICE_MIN = 15
_invoice_locks: dict[int, "asyncio.Lock"] = {}


class PApayFSM(StatesGroup):
    token = State()
    secret = State()
    min_amount = State()
    max_amount = State()
    display_name = State()
    amount = State()
    fee_amount = State()     # === PATCH_APAY_FEE_PAYER_V1 ===


async def _g(key: str, default: str = "") -> str:
    # === PATCH_PARTNER_APAY_V1 === مستقیم از Setting(bot_id=0) (همان جایی که get_pset می‌خواند)؛
    # تا سرویسِ مستأجر (که حق ندارد platform_bot را import کند) هم بتواند از این ماژول استفاده کند.
    from models import Setting
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == key))
    return r.value if (r and r.value is not None) else default


async def _s(key: str, value: str) -> None:
    from models import Setting
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == key))
        if r:
            r.value = value
        else:
            s.add(Setting(bot_id=0, key=key, value=value))
        await s.commit()


def _hx(*a, **k):
    from premium_emoji import hx_button
    return hx_button(*a, **k)


async def _is_super(uid: int) -> bool:
    from config import PLATFORM_ADMIN_ID
    return bool(PLATFORM_ADMIN_ID) and uid == PLATFORM_ADMIN_ID


async def display_name() -> str:
    v = ((await _g("papay_display_name", "")) or "").strip()
    return v[:40] if v else DEFAULT_LABEL


async def min_amount() -> int:
    try:
        n = int(await _g("papay_min_amount", str(DEFAULT_MIN)))
    except (TypeError, ValueError):
        n = DEFAULT_MIN
    return max(n, AP.API_MIN_TOMAN)


async def max_amount() -> int:
    try:
        n = int(await _g("papay_max_amount", "0"))
    except (TypeError, ValueError):
        n = 0
    return min(n, AP.API_MAX_TOMAN) if n > 0 else AP.API_MAX_TOMAN


async def fee_cfg() -> tuple[str, int]:
    """=== PATCH_APAY_FEE_PAYER_V1 === (پرداخت‌کنندهٔ کارمزد، مبلغ)."""
    payer = await _g("papay_fee_payer", "seller")
    try:
        amt = int(await _g("papay_fee_amount", str(AP.FEE_TOMAN)))
    except (TypeError, ValueError):
        amt = AP.FEE_TOMAN
    return (payer if payer in AP.FEE_PAYERS else "seller"), max(amt, 0)


async def is_available(where: str = "") -> bool:
    if (await _g("papay_enabled", "0")) != "1" or not await _g("papay_token", ""):
        return False
    if where == "wallet":
        return (await _g("papay_show_wallet", "1")) == "1"
    if where == "purchase":
        return (await _g("papay_show_purchase", "1")) == "1"
    return True


async def open_invoice(tg_id: int):
    """فاکتور باز (پرداخت‌نشده، لغو‌نشده و هنوز در مهلت اعتبار) از نوع شارژ کیف پول این کاربر؛ وگرنه None."""
    since = datetime.utcnow() - timedelta(minutes=OPEN_INVOICE_MIN)
    async with Session() as s:
        return await s.scalar(select(PlatformApayCharge).where(
            PlatformApayCharge.user_telegram_id == int(tg_id), PlatformApayCharge.status == "waiting",
            PlatformApayCharge.purpose == "wallet", PlatformApayCharge.created_at >= since
        ).order_by(PlatformApayCharge.id.desc()))


async def _block_if_open(target, tg_id: int) -> bool:
    """اگر فاکتور بازی دارد، به‌جای ساختن فاکتور دوم پیام «یا پرداخت کن یا لغو کن» را می‌دهد (True)."""
    ch = await open_invoice(tg_id)
    if ch is None:
        return False
    age = max(0, int((datetime.utcnow() - (ch.created_at or datetime.utcnow())).total_seconds() // 60))
    rows = []
    if (ch.pay_url or "").startswith("http"):
        rows.append([InlineKeyboardButton(text="💳 رفتن به صفحهٔ پرداخت", url=ch.pay_url)])
    rows.append([InlineKeyboardButton(text="🔄 بررسی پرداخت", callback_data=f"papay:check:{ch.order_id}"),
                 InlineKeyboardButton(text="❌ لغو این فاکتور", callback_data=f"papay:cancel:{ch.order_id}",
                                      style=ButtonStyle.DANGER)])
    await target.answer(
        "⚠️ <b>شما یک فاکتور باز دارید</b>\n\n"
        f"💰 مبلغ: <b>{int(ch.amount_toman or 0):,} تومان</b> — {age} دقیقه پیش ساخته شده.\n\n"
        "برای ساختن فاکتور جدید، اول همین فاکتور را <b>پرداخت کن</b> یا <b>لغوش کن</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    return True


async def webhook_url() -> str:
    base = ((await _g("platform_zarinpal_webhook_base", WEBHOOK_BASE or "")) or "").rstrip("/")
    return f"{base}/apay/platform/webhook" if base.startswith("https://") else ""


# ═══════════════════ تسویه ═══════════════════
async def settle_charge(order_id: str) -> tuple[bool, str, int]:
    """تنها نقطه‌ای که موجودی را زیاد می‌کند. خروجی: (شارژ شد؟، پیام، مبلغ)."""
    token = await _g("papay_token", "")
    if not token:
        return False, "درگاه تنظیم نشده است.", 0
    async with Session() as s:
        ch = await s.scalar(select(PlatformApayCharge).where(PlatformApayCharge.order_id == order_id))
        if not ch:
            return False, "این فاکتور پیدا نشد.", 0
        if ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        if not ch.payment_id:
            return False, "این فاکتور شناسهٔ پرداخت ندارد.", 0
        payment_id, credit, tg_id = ch.payment_id, int(ch.amount_toman or 0), int(ch.user_telegram_id)
        expected = credit + int(ch.fee_toman or 0)     # PATCH_APAY_FEE_PAYER_V1
        chat_id, msg_id = ch.chat_id, ch.msg_id
        purpose, ref_id = (ch.purpose or "wallet"), ch.ref_id
        ask_chat, ask_msg = ch.admin_ask_chat, ch.admin_ask_msg      # PATCH_APAY_ASK_CLEANUP_V1

    code, res = await AP.verify_payment(token, payment_id)
    if code != 200 or not isinstance(res, dict):
        return False, AP.err_text(code, res), 0
    pay = res.get("payment") or {}
    if not res.get("verified") or str(pay.get("status")) != "paid":
        return False, ("مهلتِ این فاکتور تمام شده و پرداختی ثبت نشد."
                       if str(pay.get("status")) == "expired" else "هنوز پرداختی ثبت نشده است."), 0
    if str(pay.get("order_id")) != order_id or int(pay.get("amount") or 0) != expected * 10:
        logger.error("platform apay: عدمِ تطابقِ پرداخت (order=%s): %s", order_id, pay)
        return False, "اطلاعاتِ پرداخت با فاکتور نمی‌خواند؛ با پشتیبانی تماس بگیر.", 0

    if purpose == "partner_order":      # === PATCH_PARTNER_APAY_V1 ===
        _r = await _settle_partner_order(order_id, ref_id, tg_id, credit, chat_id, msg_id)
        if _r[0]:
            await _drop_admin_ask(ask_chat, ask_msg)
        return _r

    from platform_bot import get_user
    async with Session() as s:
        claim = await s.execute(sa_update(PlatformApayCharge).where(
            PlatformApayCharge.order_id == order_id, PlatformApayCharge.status != "paid"
        ).values(status="paid", paid_at=datetime.utcnow()))
        if claim.rowcount == 0:
            await s.rollback()
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        u = await get_user(s, tg_id, for_update=True)
        u.balance = float(u.balance or 0) + credit
        s.add(PlatformTxn(user_telegram_id=tg_id, amount=float(credit), kind="charge",
                          note=f"شارژ با درگاه آپی؛ {payment_id}"
                               + (" (پیامکِ دیرهنگام)" if pay.get("late") else "")))
        new_bal, uname, ufull = float(u.balance), u.username, u.full_name
        await s.commit()
    await _drop_admin_ask(ask_chat, ask_msg)
    await _after_paid(tg_id, credit, new_bal, uname, ufull, chat_id, msg_id)
    return True, "پرداخت تأیید و کیف پول شارژ شد.", credit


async def _drop_admin_ask(chat_id, msg_id) -> None:
    """=== PATCH_APAY_ASK_CLEANUP_V1 === فاکتور خودکار تسویه شد → پیامِ «واریز شد؟»یِ مدیر پاک می‌شود
    تا دوباره رویش «تأیید» نزند (و کیف پول دوبار شارژ نشود)."""
    if not (chat_id and msg_id):
        return
    try:
        from config import PLATFORM_BOT_TOKEN
        from aiogram import Bot as _B
        b = _B(token=PLATFORM_BOT_TOKEN)
        try:
            await b.delete_message(chat_id=int(chat_id), message_id=int(msg_id))
        finally:
            await b.session.close()
    except Exception:
        logger.info("platform apay: حذفِ پیامِ پرسش از مدیر ممکن نشد")


# === PATCH_PARTNER_APAY_V1 === خریدِ عمدهٔ همکار با آپی
async def _settle_partner_order(order_id: str, ref_id, tg_id: int, amount: int, chat_id, msg_id):
    """پرداختِ آپی تأیید شد → سفارشِ خریدِ عمده تأیید و موجودی اضافه می‌شود (کیف پول دست نمی‌خورد)."""
    async with Session() as s:
        claim = await s.execute(sa_update(PlatformApayCharge).where(
            PlatformApayCharge.order_id == order_id, PlatformApayCharge.status != "paid"
        ).values(status="paid", paid_at=datetime.utcnow()))
        await s.commit()
        if claim.rowcount == 0:
            return False, "این فاکتور قبلاً تسویه شده است.", 0
    ok = False
    try:
        from platform_partner import pp_order_auto_approve
        ok = await pp_order_auto_approve(int(ref_id), method_text=await display_name(),
                                         log_note="خرید عمده (تاییدِ خودکار — آپی)")
    except Exception:
        logger.exception("platform apay: تأییدِ سفارشِ خریدِ عمده ناموفق بود (order=%s ref=%s)", order_id, ref_id)
    if not ok:
        # پرداخت انجام شده ولی سفارش pending نبوده/تأیید نشد → به مدیر خبر بده تا دستی رسیدگی کند
        try:
            from platform_partner import _pp_send_platform
            from config import PLATFORM_ADMIN_ID
            if PLATFORM_ADMIN_ID:
                await _pp_send_platform(
                    PLATFORM_ADMIN_ID,
                    "⚠️ <b>پرداختِ آپی برایِ خریدِ عمدهٔ همکار تأیید شد ولی سفارش خودکار تأیید نشد</b>\n\n"
                    f"👤 همکار: <code>{tg_id}</code>\n💰 مبلغ: {amount:,} تومان\n"
                    f"🧾 سفارش #{ref_id} | فاکتور: <code>{order_id}</code>\n"
                    "لطفاً دستی بررسی کن (ممکن است قبلاً تأیید/رد شده باشد).")
        except Exception:
            logger.exception("platform apay: اطلاع به مدیر ناموفق بود")
    await _delete_partner_invoice(ref_id, chat_id, msg_id)
    return True, "پرداخت تأیید و موجودیِ همکار افزوده شد.", amount


async def _delete_partner_invoice(ref_id, chat_id, msg_id) -> None:
    """فاکتور در رباتِ خودِ همکار فرستاده شده؛ با توکنِ همان ربات پاک می‌شود."""
    if not (chat_id and msg_id and ref_id):
        return
    try:
        from models import Bot as BotRow, PartnerBot, PartnerStockOrder
        async with Session() as s:
            o = await s.get(PartnerStockOrder, int(ref_id))
            pb = await s.get(PartnerBot, o.partner_bot_id) if o else None
            brow = await s.get(BotRow, pb.bot_id) if pb else None
            token = brow.token if brow else None
        if token:
            from aiogram import Bot as AioBot
            tb = AioBot(token=token)
            try:
                await tb.delete_message(chat_id=chat_id, message_id=msg_id)
            finally:
                await tb.session.close()
    except Exception:
        logger.info("platform apay: حذفِ فاکتورِ خریدِ عمده ممکن نشد")


async def create_partner_invoice(msg, tg_id: int, order_row_id: int, amount: int, back_kb=None):
    """فاکتورِ آپی برایِ سفارشِ خریدِ عمدهٔ همکار؛ پیام در رباتِ خودِ همکار (msg.answer) ساخته می‌شود.
    خروجی: True اگر فاکتور ساخته شد."""
    mn, mx = await min_amount(), await max_amount()
    if amount < mn or amount > mx:
        await msg.answer(f"❌ مبلغِ این سفارش ({amount:,} تومان) خارج از بازهٔ مجازِ درگاه ({mn:,} تا {mx:,}) است.")
        return False
    token = await _g("papay_token", "")
    oid = "pp" + AP.new_order_id()[2:]
    _payer, _famt = await fee_cfg()
    fee = AP.fee_for_buyer(_payer, _famt)
    me_user = ""
    try:
        me_user = (await msg.bot.get_me()).username or ""
    except Exception:
        logger.debug("platform apay: نامِ کاربریِ رباتِ همکار خوانده نشد")
    ret = f"https://t.me/{me_user}" if me_user else ""
    code, res = await AP.create_payment(token, amount_toman=amount + fee, order_id=oid,
                                        description=f"خرید عمده همکار #{order_row_id}",
                                        callback_url=await webhook_url(), return_url=ret)
    if code == 400 and isinstance(res, dict) and (res.get("error") or {}).get("code") == "invalid_url":
        code, res = await AP.create_payment(token, amount_toman=amount + fee, order_id=oid, return_url=ret)
    if code not in (200, 201) or not isinstance(res, dict) or not res.get("pay_url"):
        logger.warning("platform apay (partner): ساختِ پرداخت ناموفق: %s %s", code, str(res)[:300])
        await msg.answer("❌ ساختِ فاکتور ناموفق بود.\n" + AP.err_text(code, res), reply_markup=back_kb)
        return False
    pay_rial = int(res.get("pay_amount") or 0)
    async with Session() as s:
        s.add(PlatformApayCharge(user_telegram_id=tg_id, order_id=oid, payment_id=str(res.get("id") or ""),
                                 amount_toman=amount, fee_toman=fee, pay_amount_rial=pay_rial or None,
                                 pay_url=str(res["pay_url"])[:300], status="waiting",
                                 purpose="partner_order", ref_id=order_row_id))
        await s.commit()
    card, holder = await AP.card_for(token, res)
    text, kb = AP.invoice_view(
        title=f"🛒 خریدِ عمده — {await display_name()}", pay_rial=pay_rial, card=card, holder=holder,
        order_id=oid, pay_url=str(res["pay_url"]), check_cb=f"ppay:check:{oid}", cancel_cb=f"ppay:cancel:{oid}",
        support_url="https://t.me/BaBotSaz?direct", fee_toman=fee, credit_toman=amount)
    sent = await msg.answer(text, reply_markup=kb)
    async with Session() as s:
        await s.execute(sa_update(PlatformApayCharge).where(PlatformApayCharge.order_id == oid)
                        .values(chat_id=sent.chat.id, msg_id=sent.message_id))
        await s.commit()
    AP.spawn_watch(str(res.get("id") or ""), lambda: _is_open(oid), lambda: settle_charge(oid))
    return True


async def _after_paid(tg_id, credit, new_bal, uname, ufull, chat_id, msg_id) -> None:
    try:
        from main_platform import platform_bot
    except Exception:
        logger.exception("platform apay: platform_bot در دسترس نیست")
        return
    name = await display_name()
    try:
        await platform_bot.send_message(
            tg_id,
            "✅ <b>کیف پولت با موفقیت و به‌صورتِ خودکار شارژ شد!</b>\n"
            f"💰 مبلغ: <b>{credit:,} تومان</b>\n"
            f"👛 موجودی فعلی: <b>{new_bal:,.0f} تومان</b>\n"
            f"🧾 روش پرداخت: {name}")
    except Exception:
        logger.exception("platform apay: اطلاع به کاربر ناموفق بود")
    if chat_id and msg_id:
        try:
            await platform_bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            logger.info("platform apay: حذفِ پیامِ فاکتور ممکن نشد")
    try:
        from config import PLATFORM_ADMIN_ID
        if PLATFORM_ADMIN_ID and int(tg_id) != int(PLATFORM_ADMIN_ID):   # خودِ ادمین دو پیام نگیرد
            await platform_bot.send_message(
                PLATFORM_ADMIN_ID,
                "💳 <b>شارژِ خودکارِ کیفِ پول انجام شد</b>\n\n"
                f"👤 کاربر: <code>{tg_id}</code> ({'@' + uname if uname else (ufull or '—')})\n"
                f"💰 مبلغ: <b>{credit:,} تومان</b>\n"
                f"👛 موجودیِ جدید: <b>{new_bal:,.0f} تومان</b>\n"
                "🧾 روش: درگاهِ آپی")
    except Exception:
        logger.exception("platform apay: اطلاع به ادمین ناموفق بود")


async def handle_webhook(raw: bytes, ts: str, sig: str) -> int:
    import json
    secret = ((await _g("papay_secret", "")) or "").strip()
    if secret and not AP.signature_ok(secret, raw, ts, sig):
        logger.warning("platform apay: وب‌هوک با امضایِ نامعتبر رد شد")
        return 401
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return 400
    pay = (body or {}).get("payment") or {}
    order_id, payment_id = str(pay.get("order_id") or ""), str(pay.get("id") or "")
    if not order_id:
        return 200
    async with Session() as s:
        ch = await s.scalar(select(PlatformApayCharge).where(PlatformApayCharge.order_id == order_id))
    if not ch or (payment_id and ch.payment_id and ch.payment_id != payment_id):
        return 200
    if ch.status != "paid":
        try:
            await settle_charge(order_id)
        except Exception:
            logger.exception("platform apay: تسویه از وب‌هوک ناموفق بود (order=%s)", order_id)
    return 200


async def poll_pending_once() -> int:
    now = datetime.utcnow()
    async with Session() as s:
        rows = (await s.scalars(select(PlatformApayCharge).where(
            PlatformApayCharge.status.in_(("waiting", "cancelled")),
            PlatformApayCharge.created_at >= now - timedelta(hours=6)))).all()
        # لغوشده‌ها هم تا ۳۰ دقیقه چک می‌شوند (شاید کاربر بعد از «لغو» واریز کرده باشد)
        items = [(r.order_id, r.created_at) for r in rows
                 if r.status == "waiting" or (r.created_at and
                                              r.created_at > now - timedelta(minutes=POLL_FOR_MINUTES))]
    n = 0
    for oid, created in items:
        try:
            ok, _m, _a = await settle_charge(oid)
        except Exception:
            logger.exception("platform apay: poll خطا (order=%s)", oid)
            continue
        if ok:
            n += 1
        elif created and created <= now - timedelta(minutes=POLL_FOR_MINUTES):
            async with Session() as s:
                ex = await s.execute(sa_update(PlatformApayCharge).where(
                    PlatformApayCharge.order_id == oid, PlatformApayCharge.status == "waiting"
                ).values(status="expired"))
                await s.commit()
                if ex.rowcount:     # PATCH_PARTNER_APAY_V1 — سفارشِ خریدِ عمدهٔ پرداخت‌نشده رد می‌شود
                    ch = await s.scalar(select(PlatformApayCharge).where(PlatformApayCharge.order_id == oid))
                    if ch is not None:       # PATCH_APAY_ASK_CLEANUP_V2 — منقضی و پرداخت‌نشده ⇒ پرسش بی‌پاسخ مدیر پاک شود
                        await _drop_admin_ask(ch.admin_ask_chat, ch.admin_ask_msg)
                    if ch and ch.purpose == "partner_order" and ch.ref_id:
                        from models import PartnerStockOrder
                        await s.execute(sa_update(PartnerStockOrder).where(
                            PartnerStockOrder.id == ch.ref_id, PartnerStockOrder.status == "pending"
                        ).values(status="rejected", decided_at=datetime.utcnow()))
                        await s.commit()
    return n


# ═══════════════════ پنلِ ادمین ═══════════════════
async def _menu() -> tuple[str, InlineKeyboardMarkup]:
    tok = await _g("papay_token", "")
    sec = await _g("papay_secret", "")
    on = (await _g("papay_enabled", "0")) == "1"
    w = (await _g("papay_show_wallet", "1")) == "1"
    b = (await _g("papay_show_purchase", "1")) == "1"
    mn, mx = await min_amount(), await max_amount()
    mx_txt = "بدونِ سقف" if mx >= AP.API_MAX_TOMAN else f"{mx:,}"
    hook = await webhook_url()
    sms_on = (await _g("platform_sms_enabled", "0")) == "1"
    fee_payer, fee_amt = await fee_cfg()
    txt = ("💳 <b>درگاهِ آپی (کارت‌به‌کارتِ آنی)</b>\n\n"
           "کاربر مبلغ را وارد می‌کند، در صفحهٔ پرداختِ آپی کارت و <b>مبلغِ دقیق</b> را می‌بیند و "
           "کارت‌به‌کارت می‌کند؛ با رسیدنِ پیامکِ بانک، کیف پولش خودکار شارژ می‌شود.\n\n"
           f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n"
           f"🔑 توکنِ API: <code>{(tok[:6] + '…' + tok[-4:]) if len(tok) > 12 else ('ثبت شده' if tok else '—')}</code>\n"
           f"🏷 اسمِ نمایشی برایِ کاربر: <b>{await display_name()}</b>\n"
           f"💸 کارمزدِ هر پرداخت: <b>{fee_amt:,}</b> تومان — پرداخت توسطِ <b>{AP.FEE_PAYERS[fee_payer]}</b>\n"
           f"🔐 کلیدِ وب‌هوک: {'✅ ثبت شده' if sec else '— (اختیاری)'}\n"
           f"🔽 حداقل: <b>{mn:,}</b> تومان | 🔼 حداکثر: <b>{mx_txt}</b>\n"
           f"👛 کیف پول: {'✅' if w else '❌'} | 🛒 پرداختِ محصول: {'✅' if b else '❌'}\n\n"
           + (f"🔗 آدرسِ وب‌هوک:\n<code>{hook}</code>\n\n" if hook else "")
           + ("⚠️ «شارژِ خودکار با مبلغِ اختصاصی (SMS)» هم روشن است؛ اگر آپی جایگزینش است، "
              "از «💳 مدیریت پرداخت» خاموشش کن تا دو دکمهٔ مشابه دیده نشود.\n\n" if sms_on else "")
           + "📖 راه‌اندازیِ کامل و اتصالِ گوشیِ اندروید/آیفون در «📖 راهنما».")
    rows = [
        [_hx(("⛔️ غیرفعال‌کردن" if on else "✅ فعال‌کردن"), callback_data="papay:toggle",
             style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [_hx("🔑 ثبت / تغییرِ توکن", callback_data="papay:setkey"),
         _hx("🔐 کلیدِ وب‌هوک", callback_data="papay:setsecret")],
        [_hx(f"🏷 اسمِ نمایشی: {await display_name()}", callback_data="papay:setname")],
        [_hx("💸 کارمزد با: " + ("👤 خریدار" if fee_payer == "buyer" else "🏪 فروشنده"),
             callback_data="papay:feepayer"),
         _hx(f"💸 مبلغِ کارمزد: {fee_amt:,}", callback_data="papay:feeamt")],
        [_hx(f"🔽 حداقل: {mn:,}", callback_data="papay:setmin"),
         _hx(f"🔼 حداکثر: {mx_txt}", callback_data="papay:setmax")],
        [_hx(f"👛 نمایش در کیف پول: {'✅' if w else '❌'}", callback_data="papay:vis:wallet")],
        [_hx(f"🛒 نمایش در پرداختِ محصول: {'✅' if b else '❌'}", callback_data="papay:vis:purchase")],
        [_hx("🧪 تستِ اتصال و موجودی", callback_data="papay:test", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🤖 رباتِ آپی", url="https://t.me/APayGateBot"),
         _hx("📖 راهنما", callback_data="papay:guide")],
        [_hx("⬅️ بازگشت", callback_data="set:hub_pay", style=ButtonStyle.DANGER)],
    ]
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target) -> None:
    txt, kb = await _menu()
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await m.answer(txt, reply_markup=kb, disable_web_page_preview=True)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_hx("❌ لغو", callback_data="papay:menu")]])


@papay_router.callback_query(F.data.in_({"set:papay", "papay:menu"}))
async def papay_menu(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    await state.clear()
    await _show(cb)
    await cb.answer()


@papay_router.callback_query(F.data == "papay:toggle")
async def papay_toggle(cb: CallbackQuery):
    if not await _is_super(cb.from_user.id):
        return await cb.answer()
    on = (await _g("papay_enabled", "0")) == "1"
    if not on and not await _g("papay_token", ""):
        return await cb.answer("اول توکنِ API را ثبت کن.", show_alert=True)
    await _s("papay_enabled", "0" if on else "1")
    await cb.answer("خاموش شد." if on else "فعال شد.")
    await _show(cb)


@papay_router.callback_query(F.data.startswith("papay:vis:"))
async def papay_vis(cb: CallbackQuery):
    if not await _is_super(cb.from_user.id):
        return await cb.answer()
    key = "papay_show_wallet" if cb.data.endswith("wallet") else "papay_show_purchase"
    cur = (await _g(key, "1")) == "1"
    await _s(key, "0" if cur else "1")
    await cb.answer("پنهان شد" if cur else "نمایش داده می‌شود")
    await _show(cb)


_PROMPTS = {
    "setkey": (PApayFSM.token, "🔑 <b>توکنِ API</b> (با <code>apay_</code> شروع می‌شود) را از رباتِ آپی بفرست."),
    "setsecret": (PApayFSM.secret, "🔐 <b>کلیدِ وب‌هوک</b> (webhook secret) را از رباتِ آپی بفرست؛ برایِ حذف ۰ بفرست."),
    "setmin": (PApayFSM.min_amount, f"🔽 حداقلِ شارژ را به تومان بفرست (کمترین مقدارِ مجاز {AP.API_MIN_TOMAN:,})."),
    "setmax": (PApayFSM.max_amount, "🔼 حداکثرِ شارژ را به تومان بفرست؛ برایِ برداشتنِ سقف ۰ بفرست."),
    "feeamt": (PApayFSM.fee_amount, "💸 مبلغِ کارمزدِ هر پرداخت را به تومان بفرست. فقط وقتی «کارمزد با خریدار» "
                                    "باشد رویِ فاکتورِ مشتری اضافه می‌شود."),
    "setname": (PApayFSM.display_name, "🏷 اسمی که کاربر رویِ دکمه می‌بیند را بفرست (حداکثر ۴۰ حرف؛ "
                                       "مثلاً <code>💳 کارت به کارت آنی</code>). برایِ اسمِ پیش‌فرض، "
                                       "<code>پیش‌فرض</code> بفرست."),
}


@papay_router.callback_query(F.data.in_({f"papay:{k}" for k in _PROMPTS}))
async def papay_prompt(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        return await cb.answer()
    st, txt = _PROMPTS[cb.data.split(":", 1)[1]]
    if cb.data == "papay:setsecret":
        hook = await webhook_url()
        if hook:
            txt += f"\n\nآدرسِ وب‌هوکی که باید در آپی ثبت کنی:\n<code>{hook}</code>"
    await state.set_state(st)
    await cb.message.answer(txt, reply_markup=_cancel_kb())
    await cb.answer()


async def _drop(msg: Message) -> None:
    try:
        await msg.delete()
    except Exception:
        logger.debug("platform apay: حذفِ پیامِ حساس ناموفق بود")


@papay_router.message(PApayFSM.token, F.text)
async def papay_token_save(msg: Message, state: FSMContext):
    if not await _is_super(msg.from_user.id):
        return
    await state.clear()
    tok = (msg.text or "").strip()
    await _drop(msg)
    if len(tok) < 10 or " " in tok:
        return await msg.answer("توکن نامعتبر است.")
    ok, why = await AP.validate_token(tok)
    if not ok:
        return await msg.answer(f"❌ {why}")
    await _s("papay_token", tok)
    await msg.answer("✅ توکن ذخیره شد.\n" + why)
    await _show(msg)


@papay_router.message(PApayFSM.secret, F.text)
async def papay_secret_save(msg: Message, state: FSMContext):
    if not await _is_super(msg.from_user.id):
        return
    await state.clear()
    sec = (msg.text or "").strip()
    await _drop(msg)
    if sec in {"0", "۰"}:
        await _s("papay_secret", "")
        await msg.answer("✅ کلیدِ وب‌هوک حذف شد.")
    elif len(sec) < 8 or " " in sec:
        return await msg.answer("کلید نامعتبر است.")
    else:
        await _s("papay_secret", sec)
        await msg.answer("✅ کلیدِ وب‌هوک ذخیره شد.")
    await _show(msg)


@papay_router.message(PApayFSM.min_amount, F.text)
async def papay_min_save(msg: Message, state: FSMContext):
    from platform_bot import fa_to_int
    if not await _is_super(msg.from_user.id):
        return
    await state.clear()
    n = fa_to_int(msg.text)
    if not n or n < AP.API_MIN_TOMAN:
        return await msg.answer(f"عدد باید حداقل {AP.API_MIN_TOMAN:,} باشد.")
    await _s("papay_min_amount", str(int(n)))
    await _show(msg)


@papay_router.message(PApayFSM.max_amount, F.text)
async def papay_max_save(msg: Message, state: FSMContext):
    from platform_bot import fa_to_int
    if not await _is_super(msg.from_user.id):
        return
    await state.clear()
    n = fa_to_int(msg.text)
    if n is None or n < 0:
        return await msg.answer("عددِ نامعتبر.")
    await _s("papay_max_amount", str(int(n)))
    await _show(msg)


@papay_router.callback_query(F.data == "papay:feepayer")
async def papay_feepayer(cb: CallbackQuery):
    if not await _is_super(cb.from_user.id):
        return await cb.answer()
    payer, _a = await fee_cfg()
    nxt = "seller" if payer == "buyer" else "buyer"
    await _s("papay_fee_payer", nxt)
    await cb.answer("کارمزد با " + ("خریدار" if nxt == "buyer" else "فروشنده"))
    await _show(cb)


@papay_router.message(PApayFSM.fee_amount, F.text)
async def papay_feeamt_save(msg: Message, state: FSMContext):
    from platform_bot import fa_to_int
    if not await _is_super(msg.from_user.id):
        return
    await state.clear()
    n = fa_to_int(msg.text)
    if n is None or n < 0 or n > 1_000_000:
        return await msg.answer("عددِ نامعتبر.")
    await _s("papay_fee_amount", str(int(n)))
    await _show(msg)


@papay_router.message(PApayFSM.display_name, F.text)
async def papay_name_save(msg: Message, state: FSMContext):
    if not await _is_super(msg.from_user.id):
        return
    await state.clear()
    name = (msg.text or "").strip()
    if name in ("پیش‌فرض", "پیشفرض", "default", "-"):
        await _s("papay_display_name", "")
        await msg.answer(f"✅ اسمِ نمایشی به پیش‌فرض ({DEFAULT_LABEL}) برگشت.")
    elif name:
        await _s("papay_display_name", name[:40])
        await msg.answer(f"✅ اسمِ نمایشی رویِ «{name[:40]}» تنظیم شد.")
    await _show(msg)


@papay_router.callback_query(F.data == "papay:test")
async def papay_test(cb: CallbackQuery):
    if not await _is_super(cb.from_user.id):
        return await cb.answer()
    tok = await _g("papay_token", "")
    if not tok:
        return await cb.answer("اول توکن را ثبت کن.", show_alert=True)
    await cb.answer("در حالِ تست…")
    ok, why = await AP.validate_token(tok)
    await cb.message.answer(("✅ " if ok else "❌ ") + why)


@papay_router.callback_query(F.data.startswith("papay:guide"))
async def papay_guide(cb: CallbackQuery):
    if not await _is_super(cb.from_user.id):
        return await cb.answer()
    from tenant.gateways.apay_admin import guide_content
    page = cb.data.split(":", 2)[2] if cb.data.count(":") == 2 else ""
    txt, kb = await guide_content(page, await webhook_url(), "papay")
    try:
        await cb.message.edit_text(txt, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, disable_web_page_preview=True)
    await cb.answer()


# ═══════════════════ جریانِ کاربر ═══════════════════
def _amount_kb() -> InlineKeyboardMarkup:
    from platform_bot import rial
    rows = [[_hx(rial(PRESETS[i]), callback_data=f"papay:amt:{PRESETS[i]}", style=ButtonStyle.SUCCESS),
             _hx(rial(PRESETS[i + 1]), callback_data=f"papay:amt:{PRESETS[i + 1]}", style=ButtonStyle.SUCCESS)]
            for i in (0, 2)]
    rows.append([_hx("✏️ مبلغِ دلخواه", callback_data="papay:amt:custom", style=ButtonStyle.PRIMARY)])
    rows.append([_hx("❌ لغو", callback_data="pfsms:chg:cancel", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@papay_router.callback_query(F.data.in_({"charge_papay", "charge_papay_buy"}))
async def charge_papay(cb: CallbackQuery, state: FSMContext):
    where = "purchase" if cb.data.endswith("_buy") else "wallet"
    if not await is_available(where):
        return await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
    await state.clear()
    await cb.answer()
    if await _block_if_open(cb.message, cb.from_user.id):      # PATCH_APAY_ONE_OPEN_INVOICE_V1
        return
    await cb.message.answer(
        f"{await display_name()}\n\nمبلغِ شارژ را انتخاب کن یا «✏️ مبلغِ دلخواه» را بزن "
        f"(حداقل <b>{await min_amount():,}</b> تومان):",
        reply_markup=_amount_kb())


@papay_router.callback_query(F.data.startswith("papay:amt:"))
async def papay_amount_pick(cb: CallbackQuery, state: FSMContext):
    if not await is_available():
        return await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
    val = cb.data.split(":", 2)[2]
    if val == "custom":
        await state.set_state(PApayFSM.amount)
        await cb.message.answer("مبلغِ دلخواه را به <b>تومان</b> بفرست (فقط عدد):",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                    _hx("❌ لغو", callback_data="pfsms:chg:cancel")]]))
        return await cb.answer()
    await state.clear()
    await cb.answer("در حالِ ساختِ فاکتور…")
    await _make_invoice(cb.message, cb.from_user.id, int(val))


@papay_router.message(PApayFSM.amount, F.text)
async def papay_amount(msg: Message, state: FSMContext):
    from platform_bot import fa_to_int
    n = fa_to_int(msg.text)
    mn, mx = await min_amount(), await max_amount()
    if not n or n < mn:
        return await msg.answer(f"❌ حداقلِ مبلغ {mn:,} تومان است. دوباره بفرست:")
    if n > mx:
        return await msg.answer(f"❌ حداکثرِ مبلغ {mx:,} تومان است. دوباره بفرست:")
    await state.clear()
    await _make_invoice(msg, msg.from_user.id, int(n))


async def _make_invoice(msg: Message, tg_id: int, amount: int) -> None:
    # === PATCH_APAY_ONE_OPEN_INVOICE_V1 === بررسی «فاکتور باز» و ساخت فاکتور زیر یک قفل هر-کاربر،
    # تا دو کلیک پشت‌سرهم دو فاکتور نسازد.
    async with _invoice_locks.setdefault(int(tg_id), asyncio.Lock()):
        if await _block_if_open(msg, tg_id):
            return
        await _make_invoice_locked(msg, tg_id, amount)


async def _make_invoice_locked(msg: Message, tg_id: int, amount: int) -> None:
    mn, mx = await min_amount(), await max_amount()
    if amount < mn or amount > mx:
        await msg.answer(f"❌ مبلغ باید بینِ {mn:,} و {mx:,} تومان باشد.")
        return
    token = await _g("papay_token", "")
    oid = "pb" + AP.new_order_id()[2:]
    _payer, _famt = await fee_cfg()                     # PATCH_APAY_FEE_PAYER_V1
    fee = AP.fee_for_buyer(_payer, _famt)
    me_user = ""
    try:
        from main_platform import platform_bot
        me_user = (await platform_bot.get_me()).username or ""
    except Exception:
        logger.debug("platform apay: نامِ کاربریِ ربات خوانده نشد")
    wait = await msg.answer("⏳ در حالِ ساختِ فاکتور…")
    ret = f"https://t.me/{me_user}" if me_user else ""
    code, res = await AP.create_payment(token, amount_toman=amount + fee, order_id=oid,
                                        description="شارژ کیف پول بات‌ساز",
                                        callback_url=await webhook_url(), return_url=ret)
    if code == 400 and isinstance(res, dict) and (res.get("error") or {}).get("code") == "invalid_url":
        code, res = await AP.create_payment(token, amount_toman=amount + fee, order_id=oid, return_url=ret)
    try:
        await wait.delete()
    except Exception:
        logger.debug("platform apay: حذفِ پیامِ انتظار ناموفق بود")
    if code not in (200, 201) or not isinstance(res, dict) or not res.get("pay_url"):
        logger.warning("platform apay: ساختِ پرداخت ناموفق: %s %s", code, str(res)[:300])
        await msg.answer("❌ ساختِ فاکتور ناموفق بود.\n" + AP.err_text(code, res))
        return
    pay_rial = int(res.get("pay_amount") or 0)
    async with Session() as s:
        ch = PlatformApayCharge(user_telegram_id=tg_id, order_id=oid, payment_id=str(res.get("id") or ""),
                                amount_toman=amount, fee_toman=fee, pay_amount_rial=pay_rial or None,
                                pay_url=str(res["pay_url"])[:300], status="waiting")
        s.add(ch)
        await s.commit()
    card, holder = await AP.card_for(token, res)
    text, kb = AP.invoice_view(
        title=await display_name(), pay_rial=pay_rial, card=card, holder=holder, order_id=oid,
        pay_url=str(res["pay_url"]), check_cb=f"papay:check:{oid}", cancel_cb=f"papay:cancel:{oid}",
        support_url="https://t.me/BaBotSaz?direct", fee_toman=fee, credit_toman=amount)
    sent = await msg.answer(text, reply_markup=kb)
    async with Session() as s:
        await s.execute(sa_update(PlatformApayCharge).where(PlatformApayCharge.order_id == oid)
                        .values(chat_id=sent.chat.id, msg_id=sent.message_id))
        await s.commit()
    # === PATCH_APAY_FAST_WATCH_V1 === هر ۵ ثانیه وضعیت؛ با «paid» تسویه، پیام و حذفِ فاکتور
    AP.spawn_watch(str(res.get("id") or ""), lambda: _is_open(oid), lambda: settle_charge(oid))


async def _is_open(order_id: str) -> bool:
    async with Session() as s:
        st = await s.scalar(select(PlatformApayCharge.status).where(PlatformApayCharge.order_id == order_id))
    return st in ("waiting", "cancelled")


@papay_router.callback_query(F.data.startswith("papay:check:"))
async def papay_check(cb: CallbackQuery):
    oid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(PlatformApayCharge).where(PlatformApayCharge.order_id == oid))
    if not ch or ch.user_telegram_id != cb.from_user.id:
        return await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
    if ch.status == "paid":
        return await cb.answer("✅ این فاکتور پرداخت و کیف پولت شارژ شده است.", show_alert=True)
    await cb.answer("در حالِ استعلام…")
    ok, note, _c = await settle_charge(oid)
    if not ok:
        await cb.message.answer("ℹ️ " + note)


@papay_router.callback_query(F.data.startswith("papay:cancel:"))
async def papay_cancel(cb: CallbackQuery):
    """لغو فقط سمتِ ماست؛ اگر کاربر بعداً پرداخت کند، وب‌هوکِ آپی همچنان شارژ می‌کند."""
    oid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(PlatformApayCharge).where(PlatformApayCharge.order_id == oid))
        if not ch or ch.user_telegram_id != cb.from_user.id:
            return await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        if ch.status == "paid":
            return await cb.answer("این فاکتور قبلاً پرداخت شده است.", show_alert=True)
        ch.status = "cancelled"
        ask_chat, ask_msg, inv_chat, inv_msg = ch.admin_ask_chat, ch.admin_ask_msg, ch.chat_id, ch.msg_id
        await s.commit()
    await cb.answer("فاکتور لغو شد.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("platform apay: تمیزکاریِ پیام ناموفق بود")
    # === PATCH_APAY_ASK_CLEANUP_V2 === لغو کاربر ⇒ پیام «واریز شد؟»ی مدیر دیگر لازم نیست
    await _drop_admin_ask(ask_chat, ask_msg)
    if inv_chat and inv_msg and inv_msg != cb.message.message_id:       # لغو از پیام «فاکتور باز دارید» زده شده
        try:
            await cb.bot.edit_message_reply_markup(chat_id=inv_chat, message_id=inv_msg, reply_markup=None)
        except Exception:
            logger.debug("platform apay: دکمه‌های پیام اصلی فاکتور برداشته نشد")
    await cb.message.answer("❌ فاکتور لغو شد.\n\nاگر قبل از لغو واریز کرده بودی نگران نباش؛ "
                            "به‌محضِ تأییدِ بانک کیف پولت خودکار شارژ می‌شود.")


# === PATCH_APAY_ADMIN_ASK_V1 === «فلانی خواست شارژ کند؛ واریز شد؟» بعد از ۵ دقیقه
async def ask_admin_pending() -> int:
    from config import PLATFORM_ADMIN_ID
    if not PLATFORM_ADMIN_ID:
        return 0
    now = datetime.utcnow()
    async with Session() as s:
        ids = (await s.scalars(select(PlatformApayCharge.id).where(
            PlatformApayCharge.status.in_(("waiting", "expired")),   # PATCH_APAY_NO_ASK_ON_CANCEL_V1: لغوِ خودِ کاربر → نپرس
            PlatformApayCharge.admin_asked_at.is_(None),
            PlatformApayCharge.created_at <= now - timedelta(minutes=AP.ASK_AFTER_MIN),
            PlatformApayCharge.created_at >= now - timedelta(hours=6)))).all()
    n = 0
    for cid in ids:
        async with Session() as s:
            claim = await s.execute(sa_update(PlatformApayCharge).where(
                PlatformApayCharge.id == cid, PlatformApayCharge.admin_asked_at.is_(None),
                PlatformApayCharge.status != "paid").values(admin_asked_at=now))
            await s.commit()
            if not claim.rowcount:
                continue
            ch = await s.get(PlatformApayCharge, cid)
            from models import PlatformUser
            u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == ch.user_telegram_id))
        if int(ch.user_telegram_id) == int(PLATFORM_ADMIN_ID):
            continue
        who = ("@" + u.username) if u and u.username else ((u.full_name if u else "") or "—")
        if (ch.purpose or "wallet") == "partner_order":
            who += f" — 🛒 خریدِ عمدهٔ همکار (سفارش #{ch.ref_id})"
        try:
            from main_platform import platform_bot
            _sent = await platform_bot.send_message(PLATFORM_ADMIN_ID, AP.admin_ask_text(
                who=who, tg_id=ch.user_telegram_id, amount=ch.amount_toman, fee=ch.fee_toman or 0,
                pay_rial=ch.pay_amount_rial, order_id=ch.order_id, status=ch.status,
                created=ch.created_at),
                reply_markup=AP.admin_ask_kb("papadm", cid))
            if getattr(_sent, "message_id", None):
                async with Session() as s:
                    await s.execute(sa_update(PlatformApayCharge).where(PlatformApayCharge.id == cid).values(
                        admin_ask_chat=int(PLATFORM_ADMIN_ID), admin_ask_msg=_sent.message_id))
                    await s.commit()
            n += 1
        except Exception as e:
            logger.info("platform apay: پرسش از ادمین ناموفق بود: %s", e)
    return n


async def manual_settle(charge_id: int, admin_uid: int) -> tuple[bool, str]:
    from platform_bot import get_user
    async with Session() as s0:      # === PATCH_PARTNER_APAY_V1 === خریدِ عمدهٔ همکار → تأییدِ سفارش، نه شارژِ کیف پول
        _c0 = await s0.get(PlatformApayCharge, charge_id)
        _p0 = (_c0.purpose if _c0 else None) or "wallet"
        _r0 = (_c0.ref_id, _c0.order_id, _c0.chat_id, _c0.msg_id, int(_c0.amount_toman or 0),
               int(_c0.user_telegram_id)) if _c0 else None
    if _r0 and _p0 == "partner_order":
        ref_id, oid, c_chat, c_msg, amt, tg0 = _r0
        async with Session() as s:
            claim = await s.execute(sa_update(PlatformApayCharge).where(
                PlatformApayCharge.id == charge_id, PlatformApayCharge.status != "paid"
            ).values(status="paid", paid_at=datetime.utcnow()))
            await s.commit()
            if claim.rowcount == 0:
                return False, "این فاکتور قبلاً تأیید شده است."
        from platform_partner import pp_order_auto_approve
        ok = await pp_order_auto_approve(int(ref_id), method_text="تأییدِ دستیِ ادمین (آپی)",
                                         log_note="خرید عمده (تأییدِ دستیِ ادمین — آپی)")
        await _delete_partner_invoice(ref_id, c_chat, c_msg)
        return (True, f"✅ سفارشِ خریدِ عمده تأیید و موجودی افزوده شد ({amt:,} تومان).") if ok else \
               (False, "پرداخت ثبت شد ولی سفارش pending نبود (قبلاً تأیید/رد شده).")
    async with Session() as s:
        claim = await s.execute(sa_update(PlatformApayCharge).where(
            PlatformApayCharge.id == charge_id, PlatformApayCharge.status != "paid"
        ).values(status="paid", paid_at=datetime.utcnow()))
        if claim.rowcount == 0:
            await s.rollback()
            return False, "این فاکتور قبلاً شارژ شده یا پیدا نشد."
        ch = await s.get(PlatformApayCharge, charge_id)
        credit, tg_id = int(ch.amount_toman or 0), int(ch.user_telegram_id)
        u = await get_user(s, tg_id, for_update=True)
        u.balance = float(u.balance or 0) + credit
        s.add(PlatformTxn(user_telegram_id=tg_id, amount=float(credit), kind="charge",
                          note=f"شارژِ آپی با تأییدِ دستیِ ادمین ({admin_uid})؛ {ch.order_id}"))
        new_bal, chat_id, msg_id = float(u.balance), ch.chat_id, ch.msg_id
        await s.commit()
    try:
        from main_platform import platform_bot
        await platform_bot.send_message(
            tg_id, "✅ <b>کیف پولت پس از بررسیِ ادمین شارژ شد!</b>\n"
                   f"💰 مبلغ: <b>{credit:,} تومان</b>\n👛 موجودی فعلی: <b>{new_bal:,.0f} تومان</b>")
        if chat_id and msg_id:
            try:
                await platform_bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
    except Exception:
        logger.exception("platform apay: اطلاع به کاربر ناموفق بود")
    return True, f"✅ {credit:,} تومان به کیف پولِ کاربر اضافه شد."


@papay_router.callback_query(F.data.startswith("papadm:"))
async def papay_admin_answer(cb: CallbackQuery):
    from platform_bot import has_perm
    if not await has_perm(cb.from_user.id, "adm:fin"):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    try:
        _p, act, cid = cb.data.split(":")
        cid = int(cid)
    except ValueError:
        return await cb.answer()
    if act == "ok":
        ok, msg = await manual_settle(cid, cb.from_user.id)
        tail = "\n\n✅ <b>تأیید شد و کیف پولِ کاربر شارژ گردید.</b>" if ok else f"\n\n⚠️ {msg}"
    else:
        ok, msg = True, "ثبت شد."
        tail = "\n\n❌ <b>واریز نشده — شارژی انجام نشد.</b>"
    try:
        await cb.message.edit_text((cb.message.html_text or "") + tail, reply_markup=None)
    except Exception:
        logger.info("platform apay: ویرایشِ پیامِ پرسش ممکن نشد")
    # === PATCH_APAY_ASK_CLEANUP_V2 === پرسش جواب داده شد؛ پیامش (با نتیجه) می‌ماند و دیگر پاک نمی‌شود
    async with Session() as s:
        await s.execute(sa_update(PlatformApayCharge).where(PlatformApayCharge.id == cid)
                        .values(admin_ask_chat=None, admin_ask_msg=None))
        await s.commit()
    await cb.answer(msg if not ok else ("شارژ شد ✅" if act == "ok" else "ثبت شد"), show_alert=not ok)
