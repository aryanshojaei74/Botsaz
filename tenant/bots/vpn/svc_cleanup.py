"""
svc_cleanup.py — «🧹 سرویس‌هایِ منقضی‌شده» برایِ مالکِ رباتِ VPN (PATCH_SVC_CLEANUP_V1، تیکت #863).

درخواست: لیستِ سرویس‌هایی که مدتشان تمام شده ولی هنوز در ربات/پنل مانده‌اند (ربات خودکار حذفشان نکرده)، تا مالک دستی
حذفشان کند — بدونِ اینکه پیامی به کاربر برود (کاربر یا پیامِ حذف نمی‌گیرد، و خودِ کاربر از ربات پاک نمی‌شود).

  • فقط سرویس‌هایِ *منقضی‌شده بر اساسِ تاریخ* (expires_at < الان) نشان داده می‌شوند، نه حجم‌تمام‌شده‌ها (آن‌ها مهلتِ تمدید دارند).
  • حذفِ تکی (با تأیید) یا گروهی (همه / بیش از ۳، ۷، ۳۰ روز) — «از پنل و ربات» یا «فقط از ربات».
  • هیچ پیامی به کاربر نمی‌رود.
"""
from __future__ import annotations

import datetime as _dt

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from config import Session
from models import PanelAccount

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

svc_cleanup_router = Router()

PAGE = 8
BTN_TEXT = "🧹 سرویس‌های منقضی‌شده (حذف دستی)"


def _now() -> _dt.datetime:
    return _dt.datetime.utcnow()


def _gone(exc: Exception) -> bool:
    """پنل می‌گوید این کاربر دیگر وجود ندارد ⇒ حذف از پنل موفق حساب می‌شود."""
    t = str(exc).lower()
    return any(x in t for x in ("404", "not found", "no such", "does not exist", "یافت نشد"))


async def expired_accounts(bot_id: int, min_days: int = 0) -> list[PanelAccount]:
    """سرویس‌هایِ منقضی‌شده (قدیمی‌ترینِ انقضا اول)."""
    cutoff = _now() - _dt.timedelta(days=min_days)
    async with Session() as s:
        return list((await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == bot_id, PanelAccount.expires_at.is_not(None),
            PanelAccount.expires_at < cutoff).order_by(PanelAccount.expires_at.asc()))).all())


def _ago(dt: _dt.datetime | None) -> str:
    if not dt:
        return "—"
    d = max(0, (_now() - dt).days)
    return "امروز" if d == 0 else f"{d} روز پیش"


async def render_list(bot_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    accs = await expired_accounts(bot_id)
    n = len(accs)
    pages = max(1, (n + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = accs[page * PAGE:(page + 1) * PAGE]
    rows: list[list[InlineKeyboardButton]] = []
    if not n:
        text = ("🧹 <b>سرویس‌های منقضی‌شده</b>\n\n✅ سرویسِ منقضی‌شده‌ای در ربات نمانده؛ همه‌چیز تمیز است.")
    else:
        text = (f"🧹 <b>سرویس‌های منقضی‌شده</b>\n\n"
                f"{n:,} سرویس مدتش تمام شده و هنوز در ربات/پنل مانده است.\n"
                "با زدنِ هر کدام می‌توانی دستی حذفش کنی — <b>هیچ پیامی به کاربر نمی‌رود</b> و خودِ کاربر از ربات پاک نمی‌شود.\n\n"
                f"صفحهٔ {page + 1} از {pages}")
        for a in chunk:
            tag = a.plan_title or a.email
            rows.append([InlineKeyboardButton(
                text=f"{tag} — {_ago(a.expires_at)} — 👤{a.user_telegram_id}",
                callback_data=f"tvpn:expone:{a.id}:{page}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"tvpn:exp:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"tvpn:exp:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="🗑 حذفِ گروهی…", callback_data="tvpn:expall",
                                          style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به VPN خودکار", callback_data="tvpn:menu",
                                      style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def delete_one(bot_id: int, aid: int, panel_too: bool = True) -> tuple[bool, str]:
    """حذفِ یک سرویس. (موفق؟، دلیلِ خطا). هیچ پیامی به کاربر نمی‌رود."""
    async with Session() as s:
        a = await s.get(PanelAccount, aid)
        if not a or a.bot_id != bot_id:
            return False, "سرویس پیدا نشد"
    if panel_too:
        from tenant import _admin_svc_client
        client, email = await _admin_svc_client(aid, bot_id)
        if not client:
            return False, "پنلِ این سرویس در دسترس/فعال نیست (می‌توانی «فقط از ربات» را بزنی)"
        try:
            await client.delete_account(email)
        except Exception as e:  # noqa: BLE001
            if not _gone(e):
                return False, f"حذف از پنل ناموفق: {str(e)[:100]}"
        finally:
            try:
                await client.close()
            except Exception:
                logger.debug("بستنِ کلاینتِ پنل ناموفق بود")
    async with Session() as s:
        a = await s.get(PanelAccount, aid)
        if a and a.bot_id == bot_id:
            await s.delete(a)
            await s.commit()
    return True, ""


async def delete_many(bot_id: int, min_days: int, panel_too: bool) -> dict:
    accs = await expired_accounts(bot_id, min_days)
    ok = bad = 0
    reasons: dict[str, int] = {}
    for a in accs:
        good, why = await delete_one(bot_id, a.id, panel_too)
        if good:
            ok += 1
        else:
            bad += 1
            reasons[why] = reasons.get(why, 0) + 1
    return {"deleted": ok, "failed": bad, "reasons": reasons, "total": len(accs)}


def entry_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=BTN_TEXT, callback_data="tvpn:exp:0", style=ButtonStyle.PRIMARY)


# ───────────────────────── هندلرها ─────────────────────────

async def _owner(cb: CallbackQuery, bot: Bot):
    from tenant.bots.vpn.vpn_admin import _owner as _o
    return await _o(cb, bot)


async def _show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@svc_cleanup_router.callback_query(F.data.startswith("tvpn:exp:"))
async def exp_list(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    text, kb = await render_list(row.id, int(cb.data.split(":")[2]))
    await _show(cb, text, kb)
    await cb.answer()


@svc_cleanup_router.callback_query(F.data.startswith("tvpn:expone:"))
async def exp_one(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, aid, page = cb.data.split(":")
    async with Session() as s:
        a = await s.get(PanelAccount, int(aid))
    if not a or a.bot_id != row.id:
        await cb.answer("این سرویس دیگر وجود ندارد.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 حذف از پنل و ربات", callback_data=f"tvpn:expdo:{aid}:{page}:p",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="🧹 حذف فقط از ربات (پنل دست‌نخورده)", callback_data=f"tvpn:expdo:{aid}:{page}:b")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"tvpn:exp:{page}", style=ButtonStyle.DANGER)],
    ])
    await _show(cb, (f"🗑 <b>حذفِ سرویس</b>\n\n"
                     f"سرویس: <code>{a.email}</code>\n"
                     f"کاربر: <code>{a.user_telegram_id}</code>\n"
                     f"پایانِ مدت: {_ago(a.expires_at)}\n\n"
                     "پیامی به کاربر نمی‌رود و خودِ کاربر پاک نمی‌شود."), kb)
    await cb.answer()


@svc_cleanup_router.callback_query(F.data.startswith("tvpn:expdo:"))
async def exp_do(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, aid, page, mode = cb.data.split(":")
    await cb.answer("در حال حذف…")
    ok, why = await delete_one(row.id, int(aid), panel_too=(mode == "p"))
    if not ok:
        await cb.message.answer(f"❌ حذف نشد: {why}")
        return
    text, kb = await render_list(row.id, int(page))
    await _show(cb, "✅ سرویس حذف شد (بدونِ پیام به کاربر).\n\n" + text, kb)


@svc_cleanup_router.callback_query(F.data == "tvpn:expall")
async def exp_all_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    counts = {d: len(await expired_accounts(row.id, d)) for d in (0, 3, 7, 30)}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *[[InlineKeyboardButton(text=f"{lbl} ({counts[d]:,})", callback_data=f"tvpn:expalls:{d}")]
          for d, lbl in ((0, "همهٔ منقضی‌ها"), (3, "منقضی بیش از ۳ روز"),
                         (7, "منقضی بیش از ۷ روز"), (30, "منقضی بیش از ۳۰ روز"))],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tvpn:exp:0", style=ButtonStyle.DANGER)],
    ])
    await _show(cb, "🗑 <b>حذفِ گروهی</b>\n\nکدام گروه حذف شود؟ (در مرحلهٔ بعد تأیید می‌گیرم)", kb)
    await cb.answer()


@svc_cleanup_router.callback_query(F.data.startswith("tvpn:expalls:"))
async def exp_all_sure(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    d = int(cb.data.split(":")[2])
    n = len(await expired_accounts(row.id, d))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 بله، {n:,} سرویس از پنل و ربات", callback_data=f"tvpn:expalldo:{d}:p",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="🧹 فقط از ربات (پنل دست‌نخورده)", callback_data=f"tvpn:expalldo:{d}:b")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="tvpn:exp:0", style=ButtonStyle.DANGER)],
    ])
    await _show(cb, f"⚠️ <b>تأیید</b>\n\n{n:,} سرویسِ منقضی‌شده حذف می‌شود. این کار برگشت‌پذیر نیست "
                    "و هیچ پیامی به کاربرها نمی‌رود.", kb)
    await cb.answer()


@svc_cleanup_router.callback_query(F.data.startswith("tvpn:expalldo:"))
async def exp_all_do(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, d, mode = cb.data.split(":")
    await cb.answer("در حال حذف… ممکن است چند دقیقه طول بکشد")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("پاک‌کردنِ کیبوردِ تأیید ممکن نشد (بی‌اهمیت)")
    res = await delete_many(row.id, int(d), panel_too=(mode == "p"))
    lines = [f"✅ <b>پایانِ حذفِ گروهی</b>", f"حذف‌شده: {res['deleted']:,} از {res['total']:,}"]
    if res["failed"]:
        lines.append(f"ناموفق: {res['failed']:,}")
        for why, c in list(res["reasons"].items())[:3]:
            lines.append(f"• {c:,}× {why}")
    text, kb = await render_list(row.id, 0)
    await cb.message.answer("\n".join(lines) + "\n\n" + text, reply_markup=kb)
