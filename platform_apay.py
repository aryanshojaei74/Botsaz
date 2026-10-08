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


class PApayFSM(StatesGroup):
    token = State()
    secret = State()
    min_amount = State()
    max_amount = State()
    display_name = State()
    amount = State()


async def _g(key: str, default: str = "") -> str:
    from platform_bot import get_pset
    return await get_pset(key, default)


async def _s(key: str, value: str) -> None:
    from platform_bot import set_pset
    await set_pset(key, value)


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


async def is_available(where: str = "") -> bool:
    if (await _g("papay_enabled", "0")) != "1" or not await _g("papay_token", ""):
        return False
    if where == "wallet":
        return (await _g("papay_show_wallet", "1")) == "1"
    if where == "purchase":
        return (await _g("papay_show_purchase", "1")) == "1"
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
        chat_id, msg_id = ch.chat_id, ch.msg_id

    code, res = await AP.verify_payment(token, payment_id)
    if code != 200 or not isinstance(res, dict):
        return False, AP.err_text(code, res), 0
    pay = res.get("payment") or {}
    if not res.get("verified") or str(pay.get("status")) != "paid":
        return False, ("مهلتِ این فاکتور تمام شده و پرداختی ثبت نشد."
                       if str(pay.get("status")) == "expired" else "هنوز پرداختی ثبت نشده است."), 0
    if str(pay.get("order_id")) != order_id or int(pay.get("amount") or 0) != credit * 10:
        logger.error("platform apay: عدمِ تطابقِ پرداخت (order=%s): %s", order_id, pay)
        return False, "اطلاعاتِ پرداخت با فاکتور نمی‌خواند؛ با پشتیبانی تماس بگیر.", 0

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
    await _after_paid(tg_id, credit, new_bal, uname, ufull, chat_id, msg_id)
    return True, "پرداخت تأیید و کیف پول شارژ شد.", credit


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
                await s.execute(sa_update(PlatformApayCharge).where(
                    PlatformApayCharge.order_id == oid, PlatformApayCharge.status == "waiting"
                ).values(status="expired"))
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
    txt = ("💳 <b>درگاهِ آپی (کارت‌به‌کارتِ آنی)</b>\n\n"
           "کاربر مبلغ را وارد می‌کند، در صفحهٔ پرداختِ آپی کارت و <b>مبلغِ دقیق</b> را می‌بیند و "
           "کارت‌به‌کارت می‌کند؛ با رسیدنِ پیامکِ بانک، کیف پولش خودکار شارژ می‌شود.\n\n"
           f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n"
           f"🔑 توکنِ API: <code>{(tok[:6] + '…' + tok[-4:]) if len(tok) > 12 else ('ثبت شده' if tok else '—')}</code>\n"
           f"🏷 اسمِ نمایشی برایِ کاربر: <b>{await display_name()}</b>\n"
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
    mn, mx = await min_amount(), await max_amount()
    if amount < mn or amount > mx:
        await msg.answer(f"❌ مبلغ باید بینِ {mn:,} و {mx:,} تومان باشد.")
        return
    token = await _g("papay_token", "")
    oid = "pb" + AP.new_order_id()[2:]
    me_user = ""
    try:
        from main_platform import platform_bot
        me_user = (await platform_bot.get_me()).username or ""
    except Exception:
        logger.debug("platform apay: نامِ کاربریِ ربات خوانده نشد")
    wait = await msg.answer("⏳ در حالِ ساختِ فاکتور…")
    ret = f"https://t.me/{me_user}" if me_user else ""
    code, res = await AP.create_payment(token, amount_toman=amount, order_id=oid,
                                        description="شارژ کیف پول بات‌ساز",
                                        callback_url=await webhook_url(), return_url=ret)
    if code == 400 and isinstance(res, dict) and (res.get("error") or {}).get("code") == "invalid_url":
        code, res = await AP.create_payment(token, amount_toman=amount, order_id=oid, return_url=ret)
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
                                amount_toman=amount, pay_amount_rial=pay_rial or None,
                                pay_url=str(res["pay_url"])[:300], status="waiting")
        s.add(ch)
        await s.commit()
    card, holder = await AP.card_for(token, res)
    text, kb = AP.invoice_view(
        title=await display_name(), pay_rial=pay_rial, card=card, holder=holder, order_id=oid,
        pay_url=str(res["pay_url"]), check_cb=f"papay:check:{oid}", cancel_cb=f"papay:cancel:{oid}",
        support_url="https://t.me/BaBotSaz?direct")
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
        await s.commit()
    await cb.answer("فاکتور لغو شد.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("platform apay: تمیزکاریِ پیام ناموفق بود")
    await cb.message.answer("❌ فاکتور لغو شد.\n\nاگر قبل از لغو واریز کرده بودی نگران نباش؛ "
                            "به‌محضِ تأییدِ بانک کیف پولت خودکار شارژ می‌شود.")
