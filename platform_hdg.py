"""platform_hdg.py — صفحهٔ مدیر: «💽 مصرفِ دیسک و پاکسازیِ ربات‌ها» (PATCH_HOSTING_DISK_GUARD_V1). منطق در hosting_disk_guard.py."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from config import PLATFORM_ADMIN_ID
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

hdg_router = Router()

_CAPS_MB = [2048, 5120, 10240, 20480]
_DAYS = [3, 7, 14, 30]


async def hdg_screen() -> tuple[str, InlineKeyboardMarkup]:
    import hosting_disk_guard as G
    cfg = await G.get_cfg()
    rep = await G.load_report()
    txt = ("💽 <b>مصرفِ دیسک و پاکسازیِ ربات‌هایِ میزبانی‌شده</b>\n\n" + G.report_text(rep) + "\n\n"
           f"⚙️ سقفِ هر اپ: <b>{G.fmt_bytes(cfg['cap_bytes'])}</b> · نگهداریِ فایلِ دانلودی/آپلودی: <b>{cfg['days']} روز</b>\n"
           f"🧹 پاکسازیِ خودکار: <b>{'روشن' if cfg['clean'] else 'خاموش'}</b> · بعد از سقف: "
           f"<b>{'خاموش‌کردنِ ربات' if cfg['action'] == 'stop' else 'فقط هشدار'}</b>\n"
           f"📁 پوشه‌هایِ موقت: <code>{', '.join(sorted(cfg['dirs']))}</code>\n\n"
           "فقط فایل‌هایِ <b>داخلِ این پوشه‌ها</b> که بیش از مدتِ بالا دست‌نخورده‌اند پاک می‌شوند؛ کد/.env/venv هرگز. "
           "اجرایِ خودکار: هر ساعت (باید تایمرِ botsaz-disk-guard فعال باشد).")
    rows = [
        [hx_button("🧹 اسکن و پاکسازی همین حالا", callback_data="hdg:run", style=ButtonStyle.SUCCESS)],
        [hx_button("🔍 فقط اسکن (بدونِ پاک‌کردن)", callback_data="hdg:dry", style=ButtonStyle.PRIMARY)],
        [hx_button(f"💾 سقفِ هر اپ: {G.fmt_bytes(cfg['cap_bytes'])} (تغییر)", callback_data="hdg:cap", style=ButtonStyle.PRIMARY)],
        [hx_button(f"🗓 نگهداری: {cfg['days']} روز (تغییر)", callback_data="hdg:days", style=ButtonStyle.PRIMARY)],
        [hx_button("🧹 پاکسازیِ خودکار: " + ("✅ روشن" if cfg["clean"] else "❌ خاموش"), callback_data="hdg:clean",
                   style=(ButtonStyle.SUCCESS if cfg["clean"] else ButtonStyle.DANGER))],
        [hx_button("🛑 بعد از سقف: " + ("خاموش‌کردنِ ربات" if cfg["action"] == "stop" else "فقط هشدار") + " (تغییر)",
                   callback_data="hdg:action", style=ButtonStyle.PRIMARY)],
        [hx_button("⬅️ بازگشت", callback_data="adm:cat:system", style=ButtonStyle.DANGER)],
    ]
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(cb: CallbackQuery) -> None:
    txt, kb = await hdg_screen()
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        if "not modified" not in str(e).lower():
            await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")


def _guard(cb: CallbackQuery) -> bool:
    return cb.from_user.id == PLATFORM_ADMIN_ID


@hdg_router.callback_query(F.data == "hdg:menu")
async def hdg_menu(cb: CallbackQuery):
    if not _guard(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await _show(cb)
    await cb.answer()


@hdg_router.callback_query(F.data.in_({"hdg:run", "hdg:dry"}))
async def hdg_run(cb: CallbackQuery, bot: Bot):
    if not _guard(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    import hosting_disk_guard as G
    dry = cb.data == "hdg:dry"
    await cb.answer("⏳ در حالِ اسکن… (چند ثانیه تا چند دقیقه)")
    try:
        rep = await G.run_once(dry_run=dry, bot=bot)
    except Exception as e:      # noqa: BLE001
        logger.exception("hdg: اسکن ناموفق")
        await cb.message.answer(f"❌ اسکن ناموفق بود: {e}")
        return
    if dry:                                         # گزارشِ «فقط اسکن» ذخیره نمی‌شود؛ مستقیم نشان می‌دهیم
        await cb.message.answer("🔍 <b>نتیجهٔ اسکن (بدونِ پاک‌کردن)</b>\n\n" + G.report_text(rep), parse_mode="HTML")
        return
    await _show(cb)


async def _cycle(cb: CallbackQuery, key: str, values: list[int], cur: int) -> None:
    import hosting_disk_guard as G
    nxt = values[(values.index(cur) + 1) % len(values)] if cur in values else values[0]
    await G.set_cfg(key, str(nxt))
    await _show(cb)


@hdg_router.callback_query(F.data == "hdg:cap")
async def hdg_cap(cb: CallbackQuery):
    if not _guard(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    import hosting_disk_guard as G
    await _cycle(cb, "hdg_cap_mb", _CAPS_MB, (await G.get_cfg())["cap_mb"])
    await cb.answer()


@hdg_router.callback_query(F.data == "hdg:days")
async def hdg_days(cb: CallbackQuery):
    if not _guard(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    import hosting_disk_guard as G
    await _cycle(cb, "hdg_days", _DAYS, (await G.get_cfg())["days"])
    await cb.answer()


@hdg_router.callback_query(F.data.in_({"hdg:clean", "hdg:action"}))
async def hdg_toggle(cb: CallbackQuery):
    if not _guard(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    import hosting_disk_guard as G
    cfg = await G.get_cfg()
    if cb.data == "hdg:clean":
        await G.set_cfg("hdg_clean", "0" if cfg["clean"] else "1")
    else:
        await G.set_cfg("hdg_action", "warn" if cfg["action"] == "stop" else "stop")
    await _show(cb)
    await cb.answer()
