"""
user.py — سمتِ مشتریِ رباتِ پنل‌فروشی (PATCH_PANEL_SELLER_V1).

خرید/تمدید فقط از کیف‌پول است؛ اگر موجودی کم بود، همان «شارژ حساب» (کارت‌به‌کارت/درگاه‌ها) باز می‌شود و بعد از شارژ
دکمهٔ ثبتِ همین خرید برایِ مشتری فرستاده می‌شود. تغییرِ رمز و خاموش/روشنِ اشتراک‌ها فقط وقتی که مالک اجازه داده باشد.
callbackها: psel:b* (خرید) · psel:a* / psel:rn* / psel:pw* / psel:tg* (پنل‌هایِ من)
"""
from __future__ import annotations

import asyncio
import html as _html

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery
from sqlalchemy import select

from config import Session
from models import PanelSellerAccount, PanelSellerPackage, VpnPanel

from tenant.bots.panel_seller import adapter as AD
from tenant.bots.panel_seller import service as SV
from tenant.bots.panel_seller.common import (HOME_ROW, REASON_FA, STATUS_FA, back_row, btn, edit_or_send, esc, fa,
                                             fmt_time, gb_text, kb, n, pk_line, sales_gate, users_text)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

psel_user_router = Router(name="psel_user")

_ERR_FA = {"no_package": "این بسته دیگر موجود نیست.", "no_panel": "پنلِ این بسته فعلاً در دسترس نیست.",
           "unsupported": "این پنل فعلاً قابلِ فروش نیست.", "not_found": "پیدا نشد.",
           "inactive": "این پنل فعال نیست."}


async def _row(bot: Bot):
    from tenant import get_bot_row
    return await get_bot_row(bot)


async def _perm(row, key: str) -> bool:
    from tenant import get_setting
    from tenant.bots.panel_seller.admin import PERMS
    return (await get_setting(row.id, key, PERMS[key][1])) == "1"


# ═══════════════════════ خرید ═══════════════════════

async def show_buy(event, bot: Bot, row, uid: int) -> None:
    ok, why = await sales_gate(row)
    if not ok:
        return await edit_or_send(event, f"⛔️ {why}", kb(HOME_ROW))
    async with Session() as s:
        pks = (await s.scalars(select(PanelSellerPackage).where(
            PanelSellerPackage.bot_id == row.id, PanelSellerPackage.active == True)   # noqa: E712
            .order_by(PanelSellerPackage.price_toman))).all()
        panels = {p.id: p for p in (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == row.id))).all()}
    pks = [p for p in pks if panels.get(p.panel_id) and panels[p.panel_id].enabled and AD.supports(panels[p.panel_id].panel_key)]
    if not pks:
        return await edit_or_send(event, "📦 فعلاً بسته‌ای برایِ فروش نیست؛ بعداً سر بزن.", kb(HOME_ROW))
    rows = [[btn(f"{p.title[:28]} — {n(p.price_toman)} تومان", f"psel:b:{p.id}", style=ButtonStyle.SUCCESS)] for p in pks]
    rows.append(HOME_ROW)
    await edit_or_send(event, "🧩 <b>خریدِ پنل</b>\n\nیک بسته را انتخاب کن؛ بعد از پرداخت، ادمینِ پنل با نام‌کاربری و رمز برایت ساخته می‌شود.",
                       kb(*rows))


@psel_user_router.callback_query(F.data.regexp(r"^psel:b:\d+$"))
async def buy_detail(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(PanelSellerPackage, pid)
    if not p or p.bot_id != row.id or not p.active:
        return await cb.answer("این بسته دیگر موجود نیست.", show_alert=True)
    await edit_or_send(cb, f"{pk_line(p)}\n\nپرداخت از کیف‌پولِ ربات انجام می‌شود (اگر موجودی کم بود، شارژِ حساب باز می‌شود).",
                       kb([btn("✅ خرید با کیف‌پول", f"psel:bok:{pid}", style=ButtonStyle.SUCCESS)], back_row("psel:buy")))
    await cb.answer()


@psel_user_router.callback_query(F.data == "psel:buy")
async def buy_back(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if row:
        await show_buy(cb, bot, row, cb.from_user.id)
    await cb.answer()


@psel_user_router.callback_query(F.data.regexp(r"^psel:bok:\d+$"))
async def buy_do(cb: CallbackQuery, bot: Bot):
    from tenant import get_or_create_user, _purchase_alert
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    pid = int(cb.data.split(":")[2])
    uid = cb.from_user.id
    await get_or_create_user(row.id, uid, cb.from_user.full_name or "", cb.from_user.username or "")
    await cb.answer("در حالِ ساختِ پنل…")
    r = await SV.buy(row.id, uid, pid)
    st = r["status"]
    if st == "insufficient":
        from tenant.shared import vs_pay
        return await vs_pay.insufficient_notice(cb.message, row.id, uid, int(r["need"]), cb.data)
    if st == "panel_failed":
        return await edit_or_send(cb, "❌ ساختِ پنل با خطا روبه‌رو شد؛ مبلغی کم نشد (اگر کم شده بود برگشته).\n"
                                      "کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.", kb(back_row("psel:buy")))
    if st != "ok":
        return await edit_or_send(cb, "❌ " + _ERR_FA.get(st, "خرید انجام نشد."), kb(back_row("psel:buy")))
    await edit_or_send(
        cb, "✅ <b>پنلِ تو آماده است!</b>\n\n"
            f"🌐 آدرس: <code>{esc(r['panel_url'])}</code>\n🆔 نام‌کاربری: <code>{esc(r['username'])}</code>\n"
            f"🔑 رمز: <code>{esc(r['password'])}</code>\n🔖 کدِ پیگیری: <code>{esc(r['tracking'])}</code>\n\n"
            "رمز را جایی امن نگه دار. وضعیتِ پنل از «🗂 پنل‌هایِ من» قابلِ مشاهده است.",
        kb([btn("🗂 پنل‌هایِ من", "psel:my", style=ButtonStyle.PRIMARY)], HOME_ROW))
    try:
        async with Session() as s:
            pk = await s.get(PanelSellerPackage, pid)
            from models import BotUser
            buyer = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == uid))
        await _purchase_alert(bot, row, buyer=buyer, section="پنل‌فروشی", detail=_html.escape(pk.title if pk else ""),
                              amount=pk.price_toman if pk else 0, tracking_code=r["tracking"])
    except Exception:      # noqa: BLE001
        logger.exception("panel_seller: اعلانِ خرید به مالک ناموفق بود")


# ═══════════════════════ پنل‌هایِ من ═══════════════════════

async def show_my(event, bot: Bot, row, uid: int) -> None:
    async with Session() as s:
        accs = (await s.scalars(select(PanelSellerAccount).where(
            PanelSellerAccount.bot_id == row.id, PanelSellerAccount.buyer_tg == uid,
            PanelSellerAccount.status != "deleted").order_by(PanelSellerAccount.id.desc()))).all()
    if not accs:
        return await edit_or_send(event, "🗂 هنوز پنلی نخریده‌ای.", kb([btn("🧩 خریدِ پنل", "psel:buy", style=ButtonStyle.SUCCESS)], HOME_ROW))
    rows = [[btn(f"{STATUS_FA.get(a.status, '')[:1]} {a.title[:24]} · {a.admin_username}", f"psel:a:{a.id}")] for a in accs[:20]]
    rows.append(HOME_ROW)
    await edit_or_send(event, "🗂 <b>پنل‌هایِ من</b>", kb(*rows))


@psel_user_router.callback_query(F.data == "psel:my")
async def my_list(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if row:
        await show_my(cb, bot, row, cb.from_user.id)
    await cb.answer()


async def _live_usage(acc, panel):
    """(مصرف‌گیگ, تعدادِ کاربر) یا None اگر پنل جواب نداد — فقط نمایشی."""
    try:
        api = AD.PanelAdminAPI.for_panel(panel)
        try:
            used = await asyncio.wait_for(api.usage_bytes(acc.admin_username), 15)
            users = await asyncio.wait_for(api.user_count(acc.admin_username), 15)
        finally:
            await api.close()
        return used / SV.GB, users
    except Exception:      # noqa: BLE001
        return None


async def _acc_for(cb: CallbackQuery, bot: Bot, aid: int):
    row = await _row(bot)
    if not row:
        return None, None
    async with Session() as s:
        a = await s.get(PanelSellerAccount, aid)
    if not a or a.bot_id != row.id or a.buyer_tg != cb.from_user.id or a.status == "deleted":
        await cb.answer("پیدا نشد.", show_alert=True)
        return row, None
    return row, a


@psel_user_router.callback_query(F.data.regexp(r"^psel:a:\d+$"))
async def my_detail(cb: CallbackQuery, bot: Bot):
    row, a = await _acc_for(cb, bot, int(cb.data.split(":")[2]))
    if not a:
        return
    async with Session() as s:
        panel = await s.get(VpnPanel, a.panel_id)
    why = f" ({REASON_FA.get(a.suspend_reason, a.suspend_reason)})" if a.status == "suspended" and a.suspend_reason else ""
    text = (f"🗂 <b>{esc(a.title)}</b>\n\n🌐 <code>{esc(panel.base_url) if panel else '—'}</code>\n"
            f"🆔 <code>{esc(a.admin_username)}</code>\nوضعیت: {STATUS_FA.get(a.status, a.status)}{why}\n"
            f"⏳ پایان: {fmt_time(a.expires_at)}")
    if panel and a.status == "active":
        live = await _live_usage(a, panel)
        if live:
            used, users = live
            text += (f"\n📊 مصرف: {fa(f'{used:.1f}')} از {gb_text(a.data_gb)}\n👥 کاربران: {fa(users)} از {users_text(a.max_users)}")
    rows = []
    aid = a.id
    if await _perm(row, "psel_allow_renew"):
        rows.append([btn("🔁 تمدید", f"psel:rn:{aid}", style=ButtonStyle.SUCCESS)])
    if a.status == "active":
        pw_btn = [btn("🔑 رمزِ تازه", f"psel:pw:{aid}", style=ButtonStyle.PRIMARY)] if await _perm(row, "psel_allow_pw") else []
        tg_btns = ([btn("⏸ خاموشِ اشتراک‌ها", f"psel:tg:{aid}:0", style=ButtonStyle.PRIMARY),
                    btn("▶️ روشنِ اشتراک‌ها", f"psel:tg:{aid}:1", style=ButtonStyle.SUCCESS)]
                   if await _perm(row, "psel_allow_toggle") else [])
        if pw_btn:
            rows.append(pw_btn)
        if tg_btns:
            rows.append(tg_btns)
    rows.append(back_row("psel:my"))
    await edit_or_send(cb, text, kb(*rows))
    await cb.answer()


@psel_user_router.callback_query(F.data.regexp(r"^psel:(rn|rnok):\d+$"))
async def renew_flow(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    if not await _perm(row, "psel_allow_renew"):
        return await cb.answer("تمدید توسطِ مدیر بسته است.", show_alert=True)
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    async with Session() as s:
        pk = await s.get(PanelSellerPackage, a.package_id) if a.package_id else None
    if not pk or not pk.active:
        return await cb.answer("بسته‌یِ این پنل دیگر برایِ تمدید موجود نیست؛ با مدیر هماهنگ کن.", show_alert=True)
    if parts[1] == "rn":
        return await edit_or_send(cb, f"🔁 تمدیدِ <b>{esc(a.title)}</b>\n\n{pk_line(pk)}\n\nاز کیف‌پول کسر می‌شود.",
                                  kb([btn("✅ تأیید و پرداخت", f"psel:rnok:{a.id}", style=ButtonStyle.SUCCESS)], back_row(f"psel:a:{a.id}")))
    await cb.answer("در حالِ تمدید…")
    r = await SV.renew(row.id, a.id, cb.from_user.id)
    if r["status"] == "insufficient":
        from tenant.shared import vs_pay
        return await vs_pay.insufficient_notice(cb.message, row.id, cb.from_user.id, int(r["need"]), cb.data)
    if r["status"] != "ok":
        return await edit_or_send(cb, "❌ تمدید انجام نشد" + (f":\n<code>{esc(r.get('error'))}</code>" if r.get("error") else ".")
                                  , kb(back_row(f"psel:a:{a.id}")))
    extra = f"\n🔑 رمزِ تازه: <code>{esc(r['new_password'])}</code>" if r.get("new_password") else ""
    await edit_or_send(cb, f"✅ تمدید شد. پایان: {fmt_time(r['expires_at'])}{extra}", kb(back_row(f"psel:a:{a.id}")))


@psel_user_router.callback_query(F.data.regexp(r"^psel:(pw|pwok):\d+$"))
async def password_flow(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    if not await _perm(row, "psel_allow_pw"):
        return await cb.answer("تغییرِ رمز توسطِ مدیر بسته است.", show_alert=True)
    if parts[1] == "pw":
        return await edit_or_send(cb, "🔑 رمزِ فعلیِ پنل باطل و رمزِ تازه ساخته می‌شود. ادامه می‌دهی؟",
                                  kb([btn("✅ بله، رمزِ تازه", f"psel:pwok:{a.id}", style=ButtonStyle.DANGER)], back_row(f"psel:a:{a.id}")))
    await cb.answer("در حالِ تغییر…")
    r = await SV.change_password(row.id, a.id, cb.from_user.id)
    if r["status"] != "ok":
        return await edit_or_send(cb, "❌ تغییرِ رمز انجام نشد" + (f":\n<code>{esc(r.get('error'))}</code>" if r.get("error") else "."),
                                  kb(back_row(f"psel:a:{a.id}")))
    await edit_or_send(cb, f"✅ رمزِ تازه:\n🆔 <code>{esc(r['username'])}</code>\n🔑 <code>{esc(r['password'])}</code>",
                       kb(back_row(f"psel:a:{a.id}")))


@psel_user_router.callback_query(F.data.regexp(r"^psel:tg:\d+:[01]$"))
async def toggle_flow(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    if not await _perm(row, "psel_allow_toggle"):
        return await cb.answer("این کار توسطِ مدیر بسته است.", show_alert=True)
    await cb.answer("در حالِ انجام…")
    r = await SV.set_users_enabled(row.id, a.id, cb.from_user.id, parts[3] == "1")
    msg = "✅ انجام شد." if r["status"] == "ok" else "❌ " + (r.get("error") or _ERR_FA.get(r["status"], "انجام نشد."))
    await cb.message.answer(msg[:300])
