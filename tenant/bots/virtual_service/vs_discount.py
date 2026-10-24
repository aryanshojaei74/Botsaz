"""
vs_discount.py — کد تخفیف برایِ محصولاتِ «خدمات مجازی» (استارز، تون، گیفت، پرمیوم، شماره مجازی).

=== PATCH_VS_DISCOUNT_V1 ===
گزارشِ ادمین (فدایی): «روی گیفت/استارز/تون/شماره مجازی نمی‌شه کد تخفیف زد» و «بشه کد رو به
محصولاتِ خاص محدود کرد؛ اگه هیچی انتخاب نشه یعنی همه».

موتورِ تخفیف (tenant/gateways/discount_engine.py) از قبل بود ولی فقط تویِ سفارشِ SMM و فروشگاهِ
دیجیتال صدا زده می‌شد. این ماژول سه چیز اضافه می‌کند:

  ۱) دکمهٔ «🏷 کد تخفیف» رویِ فاکتورِ هر محصول + گرفتنِ کد از کاربر + نمایشِ مبلغِ نهایی
     (همان پیامِ فاکتور ویرایش می‌شود).
  ۲) توابعِ کمکیِ پرداخت (vsd_pick / vsd_charge / vsd_record) تا هر مسیرِ پرداخت *داخلِ همان
     تراکنشِ کسرِ پول* کد را دوباره اعتبارسنجی و مصرف را ثبت کند (اتمیک، مثلِ SMM).
  ۳) صفحهٔ ادمین «📦 دسته‌هایِ محصول» برایِ هر کد (disc:prod:*). ستونِ DiscountCode.product_keys؛
     خالی = همهٔ محصولات.

اصلِ ایمنی: کد فقط رویِ *همان پیامِ فاکتوری* اعمال می‌شود که کاربر رویش کد زده (message_id در
state ذخیره می‌شود). فاکتورِ دیگر/بازسازی‌شده بدونِ تخفیف است و مبلغِ نمایش‌داده‌شده همیشه با مبلغِ
کسرشده یکی است.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import Session
from models import DiscountCode

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

# کلید → برچسب. ترتیب = ترتیبِ نمایش.
KINDS: dict[str, str] = {
    "stars": "⭐ استارز",
    "ton": "💎 تون (TON)",
    "gift": "🎁 گیفت",
    "premium": "👑 پرمیوم",
    "vnum": "📱 شماره مجازی",
    "smm": "📈 سرویس‌هایِ SMM",
}

vsd_router = Router(name="vs_discount")


class VsdFSM(StatesGroup):
    code = State()


class VsdError(Exception):
    """پیامش مستقیم به کاربر نشان داده می‌شود."""


def parse_keys(raw: str | None) -> set[str]:
    return {k for k in (raw or "").split(",") if k in KINDS}


def dump_keys(keys: set[str]) -> str | None:
    s = ",".join(k for k in KINDS if k in keys)
    return s or None


def keys_label(raw: str | None) -> str:
    ks = parse_keys(raw)
    if not ks:
        return "همهٔ محصولات"
    return "، ".join(KINDS[k] for k in KINDS if k in ks)


# ═══════════════════════ سمتِ کاربر ═══════════════════════

async def active_code_count(bot_id: int) -> int:
    """PATCH_VSD_NO_CODE_HINT_V1 — تعدادِ کدهایِ is_active (برایِ هشدارِ «تیک روشن است ولی کدی نیست»)."""
    try:
        async with Session() as s:
            return int(await s.scalar(select(func.count()).select_from(DiscountCode).where(
                DiscountCode.bot_id == bot_id, DiscountCode.is_active == True)) or 0)  # noqa: E712
    except Exception:
        logger.exception("active_code_count ناموفق بود")
        return -1


async def vsd_enabled(bot_id: int) -> bool:
    """قابلیتِ کد تخفیف روشن است و دست‌کم یک کدِ فعال وجود دارد؟ (وگرنه هیچ تغییری در ظاهرِ ربات)"""
    try:
        from tenant import get_setting as _gs
        if await _gs(bot_id, "discount_purchase_enabled", "1") != "1":
            return False
        async with Session() as s:
            n = await s.scalar(select(func.count()).select_from(DiscountCode).where(
                DiscountCode.bot_id == bot_id, DiscountCode.is_active == True))  # noqa: E712
        return bool(n)
    except Exception:
        logger.exception("vsd_enabled: بررسیِ فعال‌بودنِ کد تخفیف ناموفق بود؛ خاموش فرض می‌شود")
        return False


async def vsd_rows(bot_id: int, kind: str, price: int) -> list[list[InlineKeyboardButton]]:
    """ردیفِ دکمهٔ «کد تخفیف» برایِ فاکتور؛ اگر قابلیت خاموش/بدونِ کد است، []."""
    if not await vsd_enabled(bot_id):
        return []
    return [[InlineKeyboardButton(text="🏷 کد تخفیف", callback_data=f"vsd:add:{kind}:{int(price)}")]]


def vsd_pick(data: dict | None, msg_id: int | None, kind: str) -> str | None:
    """کدِ اعمال‌شده رویِ *همین* پیامِ فاکتور (و همین دسته)، یا None."""
    v = (data or {}).get("vsd")
    if v and v.get("kind") == kind and v.get("msg_id") == msg_id and v.get("code"):
        return str(v["code"])
    return None


async def vsd_clear(state: FSMContext) -> None:
    try:
        await state.update_data(vsd=None, vsd_pending=None)
    except Exception:
        logger.exception("vsd_clear ناموفق بود")


async def vsd_charge(session, *, bot_id: int, tg_id: int, code: str, kind: str,
                     price: int) -> tuple[int, int, int]:
    """اعتبارسنجیِ دوبارهٔ کد داخلِ تراکنشِ پرداخت. → (قیمتِ نهایی, code_id, مبلغِ تخفیف).
    اگر کد دیگر معتبر نبود VsdError می‌دهد؛ عمداً بی‌صدا بدونِ تخفیف ادامه نمی‌دهیم چون مشتری
    مبلغِ تخفیف‌خورده را در فاکتور دیده است."""
    from tenant.gateways.discount_engine import validate_discount
    res = await validate_discount(bot_id=bot_id, code=code, telegram_id=tg_id, price=int(price),
                                  product_key=kind, session=session)
    if not res.ok:
        raise VsdError((res.reason or "کد تخفیف دیگر معتبر نیست.")
                       + "\nفاکتور را دوباره باز کن و بدونِ کد (یا با کدِ معتبر) پرداخت کن.")
    return int(res.final_price), int(res.code_id), int(res.discount)


async def vsd_record(session, *, bot_id: int, tg_id: int, code_id: int, discount: int) -> None:
    """ثبتِ مصرف؛ باید قبل از commitِ همان تراکنشِ کسرِ پول صدا زده شود."""
    from tenant.gateways.discount_engine import record_usage
    await record_usage(session, code_id=code_id, bot_id=bot_id, telegram_id=tg_id,
                       saved_amount=discount)


async def vsd_revert_order(session, order, buyer_tg_id: int | None) -> None:
    """رد/لغوِ سفارش ⇒ مصرفِ کدِ تخفیفِ آن سفارش برمی‌گردد (کاربر دوباره می‌تواند استفاده کند).
    باید قبل از commit و در همان تراکنشِ رد/عودت صدا زده شود."""
    cid = getattr(order, "discount_code_id", None)
    if not cid or not buyer_tg_id:
        return
    from tenant.gateways.discount_engine import revert_usage
    await revert_usage(session, code_id=int(cid), telegram_id=int(buyer_tg_id))
    order.discount_code_id = None


def _kb_dump(kb) -> list | None:
    try:
        return kb.model_dump(mode="json", exclude_none=True)["inline_keyboard"] if kb else None
    except Exception:
        return None


def _kb_load(rows) -> InlineKeyboardMarkup | None:
    try:
        return InlineKeyboardMarkup.model_validate({"inline_keyboard": rows}) if rows else None
    except Exception:
        return None


def _swap_add_btn(kb: InlineKeyboardMarkup | None, kind: str, price: int, applied: bool):
    """دکمهٔ «کد تخفیف» ↔ «حذف کد تخفیف» (در همان جایگاه)."""
    if not kb:
        return kb
    rows = []
    for r in kb.inline_keyboard:
        nr = []
        for b in r:
            cd = getattr(b, "callback_data", "") or ""
            if cd.startswith("vsd:add:") and applied:
                nr.append(InlineKeyboardButton(text="🗑 حذف کد تخفیف", callback_data=f"vsd:del:{kind}:{price}",
                                               style=ButtonStyle.DANGER))
            elif cd.startswith("vsd:del:") and not applied:
                nr.append(InlineKeyboardButton(text="🏷 کد تخفیف", callback_data=f"vsd:add:{kind}:{price}"))
            else:
                nr.append(b)
        rows.append(nr)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_invoice(bot: Bot, chat_id: int, msg_id: int, text: str, is_cap: bool, kb) -> bool:
    try:
        if is_cap:
            await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=text[:1024],
                                           parse_mode="HTML", reply_markup=kb)
        else:
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text,
                                        parse_mode="HTML", reply_markup=kb)
        return True
    except Exception as e:   # noqa: BLE001
        if "not modified" in str(e).lower():
            return True
        logger.exception("vsd: ویرایشِ پیامِ فاکتور ممکن نشد")
        return False


@vsd_router.callback_query(F.data.startswith("vsd:add:"))
async def vsd_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb, get_setting
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if await get_setting(row.id, "discount_purchase_enabled", "1") != "1":
        await cb.answer("❌ استفاده از کد تخفیف غیرفعال است.", show_alert=True)
        return
    parts = cb.data.split(":")
    kind = parts[2] if len(parts) > 2 else ""
    try:
        price = int(parts[3])
    except (IndexError, ValueError):
        price = 0
    if kind not in KINDS or price <= 0:
        await cb.answer("این فاکتور دیگر معتبر نیست؛ دوباره باز کن.", show_alert=True)
        return
    m = cb.message
    is_cap = bool(getattr(m, "photo", None) or getattr(m, "video", None) or getattr(m, "document", None))
    text_html = (m.html_text if hasattr(m, "html_text") else "") or ""
    prev = await state.get_state()
    await state.update_data(vsd_pending={
        "kind": kind, "price": price, "chat_id": m.chat.id, "msg_id": m.message_id,
        "is_cap": is_cap, "text": text_html, "kb": _kb_dump(m.reply_markup), "prev": prev,
    })
    await state.set_state(VsdFSM.code)
    await cb.answer()
    await m.answer("🏷 کد تخفیف را بفرست:\n(یا /cancel برای انصراف)", reply_markup=await smart_cancel_kb(bot))


@vsd_router.message(VsdFSM.code, F.text)
async def vsd_code_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    data = await state.get_data()
    pend = data.get("vsd_pending")
    if not row or not pend:
        await state.set_state(None)
        return
    raw = (msg.text or "").strip()
    if raw.lower() in ("/cancel", "انصراف"):
        await state.update_data(vsd_pending=None)
        await state.set_state(pend.get("prev"))
        await msg.answer("انصراف داده شد.")
        return
    from tenant.gateways.discount_engine import validate_discount
    res = await validate_discount(bot_id=row.id, code=raw, telegram_id=msg.from_user.id,
                                  price=int(pend["price"]), product_key=pend["kind"])
    if not res.ok:
        await msg.answer(f"❌ {res.reason or 'کد تخفیف نامعتبر است.'}\nدوباره بفرست یا /cancel بزن:")
        return
    kind, price = pend["kind"], int(pend["price"])
    import html as _h
    extra = (f"\n\n🏷 کد <code>{_h.escape(res.code or raw)}</code> اعمال شد (−{res.discount:,} تومان)\n"
             f"💰 <b>مبلغِ نهایی: {res.final_price:,} تومان</b>")
    orig_kb = _kb_load(pend.get("kb"))
    ok = await _edit_invoice(bot, pend["chat_id"], pend["msg_id"], (pend.get("text") or "") + extra,
                             bool(pend.get("is_cap")), _swap_add_btn(orig_kb, kind, price, True))
    await state.update_data(
        vsd={"kind": kind, "code": res.code or raw, "msg_id": pend["msg_id"], "price": price,
             "orig_text": pend.get("text") or "", "is_cap": bool(pend.get("is_cap")), "orig_kb": pend.get("kb")},
        vsd_pending=None)
    await state.set_state(pend.get("prev"))
    if not ok:
        await msg.answer(f"✅ کد تخفیف اعمال شد.\n💰 مبلغِ نهایی: {res.final_price:,} تومان\n"
                         "حالا همان فاکتورِ قبلی را پرداخت کن.")


@vsd_router.callback_query(F.data.startswith("vsd:del:"))
async def vsd_del(cb: CallbackQuery, bot: Bot, state: FSMContext):
    data = await state.get_data()
    v = data.get("vsd")
    parts = cb.data.split(":")
    kind = parts[2] if len(parts) > 2 else ""
    if not v or v.get("kind") != kind or v.get("msg_id") != cb.message.message_id:
        await cb.answer("این فاکتور را دوباره باز کن.", show_alert=True)
        return
    await vsd_clear(state)
    await cb.answer("🗑 کد تخفیف حذف شد")
    await _edit_invoice(bot, cb.message.chat.id, cb.message.message_id, v.get("orig_text") or "",
                        bool(v.get("is_cap")), _kb_load(v.get("orig_kb")))


# ═══════════════════════ سمتِ ادمین: دسته‌هایِ محصولِ هر کد ═══════════════════════

def _prod_kb(dc_id: int, keys: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for k, label in KINDS.items():
        rows.append([InlineKeyboardButton(
            text=("✅ " if k in keys else "⬜️ ") + label, callback_data=f"disc:prodt:{dc_id}:{k}",
            style=ButtonStyle.SUCCESS if k in keys else ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="🌐 همهٔ محصولات (پاک‌کردنِ انتخاب)",
                                      callback_data=f"disc:prodall:{dc_id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"disc:edit:{dc_id}",
                                      style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _prod_text(code: str, keys: set[str]) -> str:
    cur = "همهٔ محصولات" if not keys else "، ".join(KINDS[k] for k in KINDS if k in keys)
    return (f"📦 <b>دسته‌هایِ محصولِ کد <code>{code}</code></b>\n\n"
            f"الان معتبر برایِ: <b>{cur}</b>\n\n"
            "هر دسته‌ای را که تیک بزنی، کد فقط رویِ همان‌ها کار می‌کند. "
            "اگر هیچ‌کدام تیک نخورده باشد، کد رویِ همهٔ محصولات کار می‌کند.\n\n"
            "ℹ️ کدی که دسته دارد، در فروشگاه/VPN اعمال نمی‌شود.")


async def _load_owned(cb: CallbackQuery, bot: Bot, dc_id: int):
    from tenant import _owner_ok, _is_foreign
    row = await _owner_ok(cb, bot)
    if not row:
        return None, None
    dc = await _get(dc_id)
    if dc is None or dc.bot_id != row.id or await _is_foreign(dc, bot):
        await cb.answer("کد پیدا نشد.", show_alert=True)
        return None, None
    return row, dc


async def _get(dc_id: int):
    async with Session() as s:
        return await s.get(DiscountCode, dc_id)


@vsd_router.callback_query(F.data.startswith("disc:prod:"))
async def disc_prod_menu(cb: CallbackQuery, bot: Bot):
    dc_id = int(cb.data.split(":")[2])
    row, dc = await _load_owned(cb, bot, dc_id)
    if not dc:
        return
    keys = parse_keys(dc.product_keys)
    await cb.answer()
    try:
        await cb.message.edit_text(_prod_text(dc.code, keys), reply_markup=_prod_kb(dc_id, keys), parse_mode="HTML")
    except Exception:
        await cb.message.answer(_prod_text(dc.code, keys), reply_markup=_prod_kb(dc_id, keys), parse_mode="HTML")


@vsd_router.callback_query(F.data.startswith("disc:prodt:"))
async def disc_prod_toggle(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    dc_id, key = int(parts[2]), parts[3]
    row, dc0 = await _load_owned(cb, bot, dc_id)
    if not dc0 or key not in KINDS:
        return
    async with Session() as s:
        dc = await s.get(DiscountCode, dc_id)
        keys = parse_keys(dc.product_keys)
        keys.symmetric_difference_update({key})
        dc.product_keys = dump_keys(keys)
        code = dc.code
        await s.commit()
    await cb.answer()
    try:
        await cb.message.edit_text(_prod_text(code, keys), reply_markup=_prod_kb(dc_id, keys), parse_mode="HTML")
    except Exception as e:   # noqa: BLE001
        if "not modified" not in str(e).lower():
            logger.exception("disc_prod_toggle: ویرایشِ صفحه ممکن نشد")


@vsd_router.callback_query(F.data.startswith("disc:prodall:"))
async def disc_prod_all(cb: CallbackQuery, bot: Bot):
    dc_id = int(cb.data.split(":")[2])
    row, dc0 = await _load_owned(cb, bot, dc_id)
    if not dc0:
        return
    async with Session() as s:
        dc = await s.get(DiscountCode, dc_id)
        dc.product_keys = None
        code = dc.code
        await s.commit()
    await cb.answer("🌐 حالا رویِ همهٔ محصولات کار می‌کند")
    try:
        await cb.message.edit_text(_prod_text(code, set()), reply_markup=_prod_kb(dc_id, set()), parse_mode="HTML")
    except Exception as e:   # noqa: BLE001
        if "not modified" not in str(e).lower():
            logger.exception("disc_prod_all: ویرایشِ صفحه ممکن نشد")
