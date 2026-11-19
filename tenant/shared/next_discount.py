"""«تخفیف خرید بعدی» (Kasra): بعد از هر خریدِ موفق، یک کدِ تخفیفِ شخصی (فقط برایِ همان خریدار، یک‌بارمصرف، با انقضا) ساخته و
در پیوی برایش فرستاده می‌شود: «در خریدِ بعدی ۱۰٪ تخفیف می‌گیری؛ کدت: NX-ABC123».

=== PATCH_NEXT_DISCOUNT_V1 ===
• پیش‌فرض خاموش (هر ربات جدا)؛ فقط پلن‌هایی که «کدهایِ تخفیف» دارند. از همان موتور و جدولِ کدهایِ تخفیف (DiscountCode) استفاده می‌کند ⇒
  کد در هر مسیری که «🏷 کد تخفیف» دارد (VPN، فروشگاه دیجیتال، SMM، خدماتِ مجازی) کار می‌کند و مصرفش با همان رویِ ثبت می‌شود.
• یک خریدار هم‌زمان فقط یک کدِ فعالِ استفاده‌نشدهٔ «خرید بعدی» دارد (پشتِ‌سرهم خریدِ بی‌کد ⇒ کدِ تازه نمی‌سازد، بی‌هدف انباشته نمی‌شود).
• هرگز استثنا بیرون نمی‌دهد: خطایِ این بخش نباید جلویِ ثبت یا گزارشِ خرید را بگیرد.
• تنظیم: ادمین ← کدهایِ تخفیف ← «🎁 تخفیفِ خریدِ بعدی»: روشن/خاموش، درصد، مدتِ اعتبار، حداقلِ مبلغِ خریدِ فعلی.
"""
from __future__ import annotations

import html as _h
import secrets
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from config import Session
from logging_setup import get_logger as _get_logger
from models import DiscountCode

logger = _get_logger(__name__)

next_discount_router = Router(name="next_discount")

K_ON, K_PCT, K_DAYS, K_MIN = "nextdisc_on", "nextdisc_pct", "nextdisc_days", "nextdisc_min"
PREFIX = "NX-"
PCT_CHOICES = (5, 10, 15, 20, 25, 30)
DAYS_CHOICES = (7, 14, 30, 60, 0)                       # ۰ = بدونِ انقضا
MIN_CHOICES = (0, 50_000, 100_000, 200_000, 500_000)    # تومانِ خریدِ فعلی برایِ گرفتنِ کد


def _int(v, default: int) -> int:
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


async def load_cfg(bot_id: int) -> dict:
    from tenant import get_setting
    return {
        "on": (await get_setting(bot_id, K_ON, "0")) == "1",
        "pct": max(1, min(90, _int(await get_setting(bot_id, K_PCT, "10"), 10))),
        "days": max(0, min(365, _int(await get_setting(bot_id, K_DAYS, "30"), 30))),
        "min": max(0, _int(await get_setting(bot_id, K_MIN, "0"), 0)),
    }


async def _active_code(s, bot_id: int, uid: int):
    """کدِ «خرید بعدیِ» فعال و استفاده‌نشدهٔ این کاربر (اگر هست)."""
    now = datetime.utcnow()
    rows = (await s.scalars(select(DiscountCode).where(
        DiscountCode.bot_id == bot_id, DiscountCode.restricted_user_id == uid,
        DiscountCode.code.like(PREFIX + "%"), DiscountCode.is_active == True))).all()    # noqa: E712
    for c in rows:
        if (c.expires_at is None or c.expires_at > now) and int(c.used_count or 0) < int(c.max_uses or 1):
            return c
    return None


def _message(code: str, pct: int, expires_at) -> str:
    from tenant import _fmt_exp_jalali
    exp = f"\n⏳ اعتبار تا: <b>{_fmt_exp_jalali(expires_at, with_time=False)}</b>" if expires_at else ""
    return (f"🎁 <b>تخفیفِ خریدِ بعدی برایِ تو!</b>\n\n"
            f"از خریدت ممنونیم 🙏 در خریدِ بعدی <b>{pct}٪ تخفیف</b> می‌گیری.\n"
            f"🏷 کدِ تخفیفِ مخصوصِ تو: <code>{_h.escape(code)}</code>{exp}\n\n"
            "هنگامِ پرداخت دکمهٔ «🏷 کد تخفیف» را بزن و این کد را بفرست. (فقط برایِ خودت و یک‌بارمصرف است.)")


async def issue(bot, row, buyer, amount) -> str | None:
    """کدِ تخفیفِ خریدِ بعدی را (اگر شرایط داشت) می‌سازد و برایِ خریدار می‌فرستد. خروجی: کدِ ساخته‌شده یا None. هرگز استثنا نمی‌دهد."""
    try:
        from plans import Feature, has_feature
        if buyer is None or not getattr(buyer, "telegram_id", None) or not has_feature(row.plan, Feature.DISCOUNT_CODES):
            return None
        cfg = await load_cfg(row.id)
        try:
            paid = int(float(amount or 0))
        except (TypeError, ValueError):
            paid = 0
        if not cfg["on"] or paid <= 0 or paid < cfg["min"]:
            return None
        uid = int(buyer.telegram_id)
        expires = (datetime.utcnow() + timedelta(days=cfg["days"])) if cfg["days"] else None
        async with Session() as s:
            if await _active_code(s, row.id, uid) is not None:
                return None
            code = None
            for _ in range(5):                                       # برخوردِ تصادفیِ کد (بسیار بعید)
                cand = PREFIX + secrets.token_hex(3).upper()
                if await s.scalar(select(DiscountCode.id).where(DiscountCode.bot_id == row.id, DiscountCode.code == cand)) is None:
                    code = cand
                    break
            if code is None:
                return None
            s.add(DiscountCode(bot_id=row.id, code=code, percent=cfg["pct"], disc_type="percent", max_uses=1,
                               expires_at=expires, restricted_user_id=uid, is_active=True))
            await s.commit()
        try:
            await bot.send_message(uid, _message(code, cfg["pct"], expires), parse_mode="HTML")
        except Exception:  # noqa: BLE001 — کاربر ربات را بلاک کرده/نبسته؛ کد در پروفایلش می‌ماند ولی بی‌اطلاع می‌شود
            logger.info("next_discount: DM to %s failed (code %s stays valid)", uid, code)
        return code
    except Exception:  # noqa: BLE001
        logger.exception("next_discount: issuing failed (ignored)")
        return None


# ───────────── پنلِ ادمین ─────────────
def _btn(text, data, on=False):
    return InlineKeyboardButton(text=text, callback_data=data, style=(ButtonStyle.SUCCESS if on else ButtonStyle.PRIMARY))


async def _view(bot_id: int):
    cfg = await load_cfg(bot_id)
    days_txt = (str(cfg["days"]) + " روز") if cfg["days"] else "بدونِ انقضا"
    min_txt = (format(cfg["min"], ",") + " تومان") if cfg["min"] else "ندارد"
    text = ("🎁 <b>تخفیفِ خریدِ بعدی</b>\n\n"
            "بعد از هر خریدِ موفق، یک کدِ تخفیفِ شخصی (مخصوصِ همان مشتری، یک‌بارمصرف) ساخته و در پیوی برایِ مشتری فرستاده می‌شود: "
            "«در خریدِ بعدی X٪ تخفیف می‌گیری؛ کدت: …». مشتری موقعِ پرداخت دکمهٔ «🏷 کد تخفیف» را می‌زند و کد را می‌فرستد.\n\n"
            f"وضعیت: <b>{'✅ روشن' if cfg['on'] else '❌ خاموش'}</b>\n"
            f"🔢 درصدِ تخفیف: <b>{cfg['pct']}٪</b>\n"
            f"⏳ مدتِ اعتبار: <b>{days_txt}</b>\n"
            f"💰 حداقلِ مبلغِ خریدِ فعلی برایِ گرفتنِ کد: <b>{min_txt}</b>\n\n"
            "ℹ️ هر مشتری هم‌زمان فقط یک کدِ استفاده‌نشدهٔ «خرید بعدی» دارد. کدها در «لیستِ کدهایِ تخفیف» با پیشوندِ NX- دیده می‌شوند.")
    rows = [[_btn("🔴 خاموش‌کردن" if cfg["on"] else "🟢 روشن‌کردن", "nxd:tog", on=not cfg["on"])],
            [_btn(("✅ " if cfg["pct"] == p else "") + f"{p}٪", f"nxd:p:{p}", on=cfg["pct"] == p) for p in PCT_CHOICES[:3]],
            [_btn(("✅ " if cfg["pct"] == p else "") + f"{p}٪", f"nxd:p:{p}", on=cfg["pct"] == p) for p in PCT_CHOICES[3:]],
            [_btn(("✅ " if cfg["days"] == d else "") + (f"{d} روز" if d else "بدونِ انقضا"), f"nxd:d:{d}", on=cfg["days"] == d)
             for d in DAYS_CHOICES],
            [_btn(("✅ " if cfg["min"] == m else "") + (f"حداقل {m:,}" if m else "بدونِ حداقل"), f"nxd:m:{m}", on=cfg["min"] == m)
             for m in MIN_CHOICES[:3]],
            [_btn(("✅ " if cfg["min"] == m else "") + f"حداقل {m:,}", f"nxd:m:{m}", on=cfg["min"] == m) for m in MIN_CHOICES[3:]],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:discounts", style=ButtonStyle.DANGER)]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@next_discount_router.callback_query(F.data.startswith("nxd:") | (F.data == "nxd"))
async def nxd_menu(cb: CallbackQuery, bot):
    from plans import Feature
    from tenant import _gate, set_setting
    row = await _gate(cb, bot, Feature.DISCOUNT_CODES)
    if not row:
        return
    parts = (cb.data or "").split(":")
    act = parts[1] if len(parts) > 1 else "menu"
    val = parts[2] if len(parts) > 2 else ""
    if act == "tog":
        cur = (await load_cfg(row.id))["on"]
        await set_setting(row.id, K_ON, "0" if cur else "1")
        await cb.answer("خاموش شد" if cur else "روشن شد")
    elif act == "p" and _int(val, 0) in PCT_CHOICES:
        await set_setting(row.id, K_PCT, str(_int(val, 10)))
        await cb.answer(f"{val}٪")
    elif act == "d" and _int(val, -1) in DAYS_CHOICES:
        await set_setting(row.id, K_DAYS, str(_int(val, 30)))
        await cb.answer("ثبت شد")
    elif act == "m" and _int(val, -1) in MIN_CHOICES:
        await set_setting(row.id, K_MIN, str(_int(val, 0)))
        await cb.answer("ثبت شد")
    else:
        await cb.answer()
    text, kb = await _view(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        if "not modified" not in str(e).lower():
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
