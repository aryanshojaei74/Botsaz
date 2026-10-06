"""
panel_shop.py — فروشِ «پنل» به مشتری (PATCH_PANEL_SHOP_ACTION_V1)

عملکردِ مستقلِ دکمه: «🏷 فروش پنل (ثبت و فروش پنل)». قبلاً فروشِ پنل از مسیرِ «خرید کانفیگ ← فقط پنل‌ها» رد می‌شد؛
آن مسیر پنل‌ها را (چون حجم/روز ندارند) اصلاً نشان نمی‌داد و فیلترِ kind=panel در آن اعمال نمی‌شد.

جریان: لیستِ پنل‌هایِ موجود (گروه‌بندی با دسته + قیمت) ← پیش‌فاکتور ← پرداخت از کیف‌پول (برایِ موجودیِ کم:
دکمه‌هایِ شارژ) ← تحویلِ آدرس/یوزرنیم/پسورد با دکمهٔ کپی. فقط آدرس/یوزرنیم/پسوردِ **خریدار** نشان داده می‌شود.
"""
from __future__ import annotations

import html as _html
import json

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import Session
from models import BotUser, Config, Order, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

panel_shop_router = Router()


def _fmt(n) -> str:
    return f"{int(n):,}"


def _attrs(c: Config) -> tuple[str, str, str, str]:
    """(حجم, مدت, اینباندها, قابلیت‌ها) از payload — همه اختیاری."""
    try:
        d = json.loads(c.payload) or {}
    except Exception:
        d = {}
    return tuple(str(d.get(k) or "") for k in ("traffic", "duration", "inbounds", "features"))  # type: ignore[return-value]


def _gkey(c: Config) -> tuple:
    t, du, ib, ft = _attrs(c)
    return (c.category or "", int(c.price), t, du, ib, ft)


def _group_label(category: str | None, price: int, stock: int, traffic: str = "", duration: str = "") -> str:
    parts = [p for p in (category or "پنل", traffic, duration) if p]
    return f"🏷 {' · '.join(parts)} — {_fmt(price)} تومان (موجودی {stock})"


async def show_panel_shop(target: Message, bot_id: int, header: str = "",
                          support_enabled: bool = False, support_contact: str | None = None,
                          mb=None) -> None:
    from tenant import inline_back_row, _with_btn_header, _attach_support
    async with Session() as s:
        cfgs = (await s.scalars(select(Config).where(
            Config.bot_id == bot_id, Config.kind == "panel", Config.status == "in_stock",
            Config.price > 0).order_by(Config.id))).all()
    groups: dict[tuple, list[int]] = {}
    for c in cfgs:
        groups.setdefault(_gkey(c), []).append(c.id)
    rows = []
    for key, ids in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2], kv[0][3])):
        cat, price, tr, du = key[0], key[1], key[2], key[3]
        rows.append([InlineKeyboardButton(
            text=_group_label(cat or None, price, len(ids), tr, du)[:64],
            callback_data=f"pnlbuy:pre:{ids[0]}", style=ButtonStyle.PRIMARY)])
    br = await inline_back_row(bot_id)
    if br:
        rows.append(br)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        kb = await _attach_support(bot_id, kb, support_enabled, contact=support_contact, mb=mb)
    except Exception:
        logger.exception("[panel_shop] اتصالِ دکمهٔ پشتیبانی ناموفق بود")
    if not groups:
        text = "🏷 فعلاً پنلی برایِ فروش موجود نیست.\nبعداً دوباره سر بزن."
    else:
        text = "🏷 <b>خریدِ پنل</b>\n\nیکی را انتخاب کن:"
    await target.answer(_with_btn_header(text, header), reply_markup=kb, parse_mode="HTML")


async def _cfg_for(cb: CallbackQuery, bot: Bot, cfg_id: int):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return None, None
    async with Session() as s:
        c = await s.get(Config, cfg_id)
    if not c or c.bot_id != row.id or c.kind != "panel":
        return row, None
    return row, c


@panel_shop_router.callback_query(F.data.startswith("pnlbuy:pre:"))
async def pnlbuy_pre(cb: CallbackQuery, bot: Bot):
    from tenant import wallet_pay_on, wallet_topup_rows
    cfg_id = int(cb.data.split(":")[2])
    row, c = await _cfg_for(cb, bot, cfg_id)
    if not row:
        return await cb.answer()
    if not c:
        return await cb.answer("این پنل دیگر موجود نیست.", show_alert=True)
    price, cat = int(c.price), c.category
    key = _gkey(c)
    async with Session() as s:
        same = (await s.scalars(select(Config).where(
            Config.bot_id == row.id, Config.kind == "panel", Config.status == "in_stock",
            Config.price == price,
            (Config.category == cat) if cat else Config.category.is_(None)))).all()
        stock = sum(1 for x in same if _gkey(x) == key)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        bal = float(u.balance or 0) if u else 0.0
    if stock < 1:
        return await cb.answer("این پنل دیگر موجود نیست.", show_alert=True)
    enough = "" if bal >= price else "\n⚠️ موجودی کیف پول کافی نیست؛ اول شارژ کن."
    text = (
        "🧾 <b>پیش‌فاکتورِ خریدِ پنل</b>\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"🏷 دسته: <b>{_html.escape(cat or 'پنل')}</b>\n"
        + "".join(f"{ic} {lb}: <b>{_html.escape(v)}</b>\n" for ic, lb, v in (
            ("📦", "حجم", key[2]), ("📅", "مدت", key[3]), ("🔌", "اینباندها", key[4]), ("✨", "قابلیت‌ها", key[5])) if v)
        + f"💰 قیمت: <b>{_fmt(price)} تومان</b>\n"
        f"💳 موجودی کیف پول: {_fmt(bal)} تومان{enough}\n\n"
        "بعد از پرداخت، آدرس، یوزرنیم و پسوردِ پنل برایت فرستاده می‌شود."
    )
    kb_rows = []
    if await wallet_pay_on(row.id):
        kb_rows.append([InlineKeyboardButton(text="✅ پرداخت از کیف پول",
                                             callback_data=f"pnlbuy:pay:{c.id}", style=ButtonStyle.SUCCESS)])
    if bal < price:
        try:
            kb_rows.extend(await wallet_topup_rows(row.id))
        except Exception:
            logger.exception("[panel_shop] ردیف‌هایِ شارژ ساخته نشد")
    try:   # === PATCH_WALLET_CHARGE_BTN_V1 ===
        from tenant import wallet_charge_btn_rows as _wcb, _has_wallet_charge_btn as _hwc
        if not _hwc(kb_rows):
            kb_rows.extend(await _wcb(row.id))
    except Exception:
        logger.exception("[panel_shop] دکمهٔ شارژ کیف‌پول اضافه نشد")
    kb_rows.append([InlineKeyboardButton(text="❌ لغو", callback_data="buy:cancel")])
    await cb.answer()
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")


@panel_shop_router.callback_query(F.data.startswith("pnlbuy:pay:"))
async def pnlbuy_pay(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, send_config_to_user
    cfg_id = int(cb.data.split(":")[2])
    row, c0 = await _cfg_for(cb, bot, cfg_id)
    if not row:
        return await cb.answer()
    if not c0:
        return await cb.answer("این پنل دیگر موجود نیست.", show_alert=True)
    price, cat = int(c0.price), c0.category
    err = None
    assigned = None
    order_id = None
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id).with_for_update())
        if u is None:
            u = BotUser(bot_id=row.id, telegram_id=cb.from_user.id, full_name=cb.from_user.full_name)
            s.add(u)
            await s.flush()
        # یک پنلِ موجود با همان دسته و قیمت (همان کلیک‌شده یا هم‌گروهش) — با قفلِ ردیف برایِ جلوگیریِ خریدِ دوباره
        key = _gkey(c0)
        q = select(Config).where(
            Config.bot_id == row.id, Config.kind == "panel", Config.status == "in_stock",
            Config.price == price,
            (Config.category == cat) if cat else Config.category.is_(None))
        cands = [x.id for x in (await s.scalars(q.order_by(Config.id))).all() if _gkey(x) == key]
        cfg = None
        for _cid in cands[:5]:   # با قفلِ ردیف؛ اگر همزمان فروخته شد، بعدی
            cfg = await s.scalar(select(Config).where(
                Config.id == _cid, Config.status == "in_stock").with_for_update())
            if cfg:
                break
        if not cfg:
            err = "این پنل دیگر موجود نیست."
        elif float(u.balance or 0) < price:
            err = "موجودی کیف پول کافی نیست. اول شارژ کن."
        else:
            u.balance = float(u.balance) - price
            cfg.status = "sold"
            cfg.owner_user_id = u.id
            o = Order(bot_id=row.id, user_id=u.id, volume_gb=0, days=0, count=1, amount=price,
                      pay_method="wallet", status="paid")
            s.add(o)
            s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=-price, kind="purchase",
                            note=f"خریدِ پنل (کد {cfg.code})"))
            await s.commit()
            assigned = cfg
            order_id = o.id
            buyer = u
    if err:
        return await cb.answer(err, show_alert=True)
    await cb.answer("پرداخت موفق ✅")
    try:
        await cb.message.edit_text("✅ پرداخت موفق! مشخصاتِ پنل ارسال می‌شود…")
    except Exception:
        logger.info("[panel_shop] ویرایشِ پیامِ پرداخت ممکن نشد")
    try:
        await send_config_to_user(bot, cb.from_user.id, assigned)
    except Exception:
        logger.exception("[panel_shop] تحویلِ پنل ناموفق بود — cfg=%s", getattr(assigned, "id", None))
        await bot.send_message(
            cb.from_user.id,
            "⚠️ پرداخت انجام شد ولی ارسالِ مشخصاتِ پنل با مشکل مواجه شد. "
            "از «🏷 پنل‌های خریداری‌شده» در «کانفیگ‌های من» ببین یا به پشتیبانی پیام بده.")
    try:
        from tenant.shared.extras import handle_referral_purchase
        await handle_referral_purchase(row.id, cb.from_user.id, bot, amount=price)
    except Exception:
        logger.exception("[panel_shop] پاداشِ رفرال ناموفق بود")
    try:
        from tenant import _purchase_alert
        try:
            d = json.loads(assigned.payload) or {}
        except Exception:
            d = {}
        await _purchase_alert(bot, row, buyer=buyer, section="فروش پنل",
                              detail=f"🏷 پنل «{d.get('name') or assigned.code}»",
                              public_detail="🏷 پنل", amount=price, order_id=order_id)
    except Exception:
        logger.exception("[panel_shop] اطلاعِ خرید به ادمین ناموفق بود")
