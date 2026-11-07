"""
platform_miniapp.py — بخشِ «فعال‌سازیِ گروهیِ مینی‌اپ» جدا از platform_bot.py.

از تنظیماتِ بات‌ساز، ادمینِ اصلی می‌تواند مینی‌اپ را برایِ همهٔ ربات‌هایِ
مستأجرِ یک *زمینهٔ* خاص (وی‌پی‌ان / فروشگاهی / تبلیغاتی / خدمات مجازی) یا
همهٔ زمینه‌ها یک‌جا روشن یا خاموش کند. سوییچِ سراسریِ خودِ قابلیت
(miniapp_feature_enabled) جداست و در platform_bot.py می‌ماند — این‌جا فقط
وضعیتِ per-bot (Bot.miniapp_enabled) دست‌کاری می‌شود.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy import select, func, Integer

from config import Session, PLATFORM_ADMIN_ID, WEBHOOK_BASE
from models import Bot as BotRow
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

miniapp_router = Router()


# ───────────── لِیزی‌رَپِرها (جلوگیری از importِ حلقوی) ─────────────
async def _apply_bot_menu_button(row: BotRow) -> str | None:
    from platform_bot import _apply_bot_menu_button as _fn
    return await _fn(row)


async def _settings_text() -> str:
    from platform_bot import _settings_text as _fn
    return await _fn()


async def _settings_kb() -> InlineKeyboardMarkup:
    from platform_bot import settings_kb as _fn
    return await _fn()


async def _get_pset(key: str, default: str = "") -> str:
    from platform_bot import get_pset as _fn
    return await _fn(key, default)


def _fields() -> dict[str, str]:
    from platform_bot import FIELDS as _f
    return _f


# ───────────── ثابت‌ها ─────────────
# زمینه‌هایی که در فهرستِ انتخاب نشان داده می‌شوند. «partner» عمداً نیست:
# ربات‌های همکار مینی‌اپِ مستقل ندارند.
_MINIAPP_FIELDS = ("vpn_sales", "digital_shop", "ad_bot", "virtual_service")


def _field_label(key: str) -> str:
    return _fields().get(key, key)


async def _count_by_field() -> dict[str, tuple[int, int]]:
    """برای هر زمینه: (تعدادِ رباتِ فعال، تعدادی که مینی‌اپشان روشن است)."""
    out: dict[str, tuple[int, int]] = {}
    async with Session() as s:
        rows = (await s.execute(
            select(BotRow.field,
                   func.count(BotRow.id),
                   func.sum(func.cast(BotRow.miniapp_enabled, Integer)))
            .where(BotRow.archived_at.is_(None), BotRow.status == "active")
            .group_by(BotRow.field)
        )).all()
    for field, total, on in rows:
        out[field] = (int(total or 0), int(on or 0))
    return out


# ───────────── صفحهٔ انتخابِ زمینه ─────────────
async def _picker_text() -> str:
    stats = await _count_by_field()
    feature_on = (await _get_pset("miniapp_feature_enabled", "on")) != "off"
    lines = ["🛍 <b>فعال‌سازیِ گروهیِ مینی‌اپ</b>", ""]
    if not feature_on:
        lines.append("⚠️ سوییچِ سراسریِ مینی‌اپ خاموش است؛ تا روشنش نکنی "
                     "مستأجرها مینی‌اپ را نمی‌بینند.")
        lines.append("")
    lines.append("وضعیتِ فعلیِ ربات‌هایِ فعال:")
    total_all = on_all = 0
    for key in _MINIAPP_FIELDS:
        total, on = stats.get(key, (0, 0))
        total_all += total
        on_all += on
        lines.append(f"• {_field_label(key)} — {on} از {total} روشن")
    lines.append("")
    lines.append(f"🔢 مجموع: {on_all} از {total_all} روشن")
    lines.append("")
    lines.append("برای کدام دسته اعمال شود؟ 👇")
    return "\n".join(lines)


async def _picker_kb() -> InlineKeyboardMarkup:
    stats = await _count_by_field()
    rows = [[hx_button("👇 یک دسته را انتخاب کن", callback_data="noop")]]
    for key in _MINIAPP_FIELDS:
        total, on = stats.get(key, (0, 0))
        rows.append([hx_button(f"{_field_label(key)} ({on}/{total})",
                               callback_data=f"mapall:pick:{key}",
                               style=ButtonStyle.PRIMARY)])
    rows.append([hx_button("✅ همهٔ ربات‌ها (همهٔ دسته‌ها)",
                           callback_data="mapall:pick:all",
                           style=ButtonStyle.PRIMARY)])
    rows.append([hx_button("⬅️ بازگشت به تنظیمات",
                           callback_data="mapall:back",
                           style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@miniapp_router.callback_query(F.data == "set:miniapp_bulk")
async def miniapp_bulk_open(cb: CallbackQuery):
    """ورودی از منویِ تنظیماتِ پلتفرم."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await cb.message.edit_text(await _picker_text(), reply_markup=await _picker_kb())
    await cb.answer()


@miniapp_router.callback_query(F.data == "mapall:back")
async def miniapp_bulk_back(cb: CallbackQuery):
    """یک سطح بالاتر: منویِ تنظیمات."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await cb.message.edit_text(await _settings_text(), reply_markup=await _settings_kb())
    await cb.answer()


# ───────────── صفحهٔ روشن/خاموش برای دستهٔ انتخاب‌شده ─────────────
def _scope_label(scope: str) -> str:
    return "همهٔ دسته‌ها" if scope == "all" else _field_label(scope)


async def _scope_counts(scope: str) -> tuple[int, int]:
    """(تعدادِ کل، تعدادِ روشن) برای دامنهٔ انتخاب‌شده."""
    stats = await _count_by_field()
    keys = _MINIAPP_FIELDS if scope == "all" else (scope,)
    total = sum(stats.get(k, (0, 0))[0] for k in keys)
    on = sum(stats.get(k, (0, 0))[1] for k in keys)
    return total, on


async def _confirm_kb(scope: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("🟢 روشن‌کردن برای این دسته",
                   callback_data=f"mapall:go:{scope}:on",
                   style=ButtonStyle.SUCCESS)],
        [hx_button("🔴 خاموش‌کردن برای این دسته",
                   callback_data=f"mapall:go:{scope}:off",
                   style=ButtonStyle.DANGER)],
        [hx_button("⬅️ بازگشت", callback_data="set:miniapp_bulk",
                   style=ButtonStyle.DANGER)],
    ])


@miniapp_router.callback_query(F.data.startswith("mapall:pick:"))
async def miniapp_bulk_pick(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    scope = cb.data.split(":")[2]
    if scope != "all" and scope not in _MINIAPP_FIELDS:
        await cb.answer("دستهٔ نامعتبر", show_alert=True)
        return
    total, on = await _scope_counts(scope)
    txt = (f"🛍 <b>{_scope_label(scope)}</b>\n\n"
           f"🤖 ربات‌های فعالِ این دسته: {total}\n"
           f"🟢 مینی‌اپشان روشن است: {on}\n"
           f"🔴 خاموش است: {total - on}\n\n"
           "چه کاری انجام شود؟ 👇")
    await cb.message.edit_text(txt, reply_markup=await _confirm_kb(scope))
    await cb.answer()


# ───────────── اجرا ─────────────
@miniapp_router.callback_query(F.data.startswith("mapall:go:"))
async def miniapp_bulk_apply(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    parts = cb.data.split(":")  # mapall:go:{scope}:{on|off}
    if len(parts) < 4:
        await cb.answer("درخواستِ نامعتبر", show_alert=True)
        return
    scope, mode = parts[2], parts[3]
    if scope != "all" and scope not in _MINIAPP_FIELDS:
        await cb.answer("دستهٔ نامعتبر", show_alert=True)
        return
    turn_on = mode == "on"

    if turn_on and not WEBHOOK_BASE.startswith("https://"):
        await cb.answer("⚠️ اول WEBHOOK_BASE (با https) را روی سرور تنظیم کن.",
                        show_alert=True)
        return

    await cb.answer("در حال اعمال…")

    keys = list(_MINIAPP_FIELDS) if scope == "all" else [scope]
    async with Session() as s:
        rows = (await s.scalars(
            select(BotRow).where(
                BotRow.archived_at.is_(None),
                BotRow.status == "active",
                BotRow.field.in_(keys),
            )
        )).all()
        total = len(rows)
        changed = []
        for r in rows:
            if r.miniapp_enabled != turn_on:
                r.miniapp_enabled = turn_on
                changed.append(r)
        await s.commit()
        for r in changed:
            await s.refresh(r)

    try:
        await cb.message.edit_text(
            f"⏳ در حال اعمال روی {len(changed)} ربات از «{_scope_label(scope)}»…")
    except Exception:
        logger.debug("ویرایشِ پیامِ «در حال اعمال» ناموفق بود", exc_info=True)

    ok = fail = 0
    for r in changed:
        try:
            _e = await _apply_bot_menu_button(r)
            if _e:
                fail += 1
                logger.warning("ستِ دکمهٔ منو ناموفق (bot_id=%s): %s", r.id, _e)
            else:
                ok += 1
        except Exception:
            fail += 1
            logger.exception("خطا در تنظیمِ دکمهٔ منویِ مینی‌اپ (bot_id=%s)", r.id)

    word = "روشن" if turn_on else "خاموش"
    txt = (f"✅ مینی‌اپ برای <b>{ok}</b> ربات {word} شد.\n\n"
           f"📂 دسته: {_scope_label(scope)}\n"
           f"🤖 کلِ ربات‌های فعالِ این دسته: {total}\n"
           f"➖ از قبل همین وضعیت را داشتند: {total - len(changed)}")
    if fail:
        txt += f"\n⚠️ ناموفق (خطا در تنظیمِ دکمهٔ منو): {fail}"
    try:
        await cb.message.edit_text(txt, reply_markup=await _confirm_kb(scope))
    except Exception:
        await cb.message.answer(txt, reply_markup=await _confirm_kb(scope))
