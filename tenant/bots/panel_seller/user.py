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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from config import Session
from models import PanelSellerAccount, PanelSellerPackage, VpnPanel

from tenant.bots.panel_seller import adapter as AD
from tenant.bots.panel_seller import service as SV
from tenant.bots.panel_seller.common import (days_text, exp_text, HOME_ROW, REASON_FA, STATUS_FA, back_row, btn, edit_or_send, esc, fa,
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
    if (await SV.custom_settings(row.id))["price"] > 0:      # PATCH_PSEL_CUSTOM_VOLUME_V1
        rows.insert(0, [btn("📦 خریدِ حجمِ دلخواه", "psel:cvb", style=ButtonStyle.SUCCESS)])
    if await SV.test_available(row.id, uid):          # PATCH_PSEL_FREE_TEST_V1
        rows.insert(0, [btn("🧪 تستِ رایگان", "psel:tst", style=ButtonStyle.PRIMARY)])
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
async def buy_back(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    row = await _row(bot)
    if row:
        await show_buy(cb, bot, row, cb.from_user.id)
    await cb.answer()


class PselBuyFSM(StatesGroup):
    username = State()       # PATCH_PSEL_PICK_USERNAME_V1
    custom_gb = State()      # PATCH_PSEL_CUSTOM_VOLUME_V1


_TAKEN_HINTS = ("exist", "already", "409", "duplicate", "taken", "unique")


async def _provision(event, bot: Bot, row, uid: int, *, pid: int | None = None, gb: int | None = None,
                     username: str | None = None, retry_cb: str = "") -> dict:
    """خریدِ بسته (pid) یا حجمِ دلخواه (gb) و نمایشِ نتیجه. event: CallbackQuery یا Message. خروجی: دیکشنریِ نتیجه (status…)."""
    r = (await SV.buy_custom(row.id, uid, int(gb), bot=bot, row=row, username=username) if gb is not None
         else await SV.buy(row.id, uid, int(pid), bot=bot, row=row, username=username))
    st = r["status"]
    msg_obj = event.message if isinstance(event, CallbackQuery) else event
    if st == "insufficient":
        from tenant.shared import vs_pay
        await vs_pay.insufficient_notice(msg_obj, row.id, uid, int(r["need"]), retry_cb or "psel:buy")
    elif st == "panel_failed":
        await tell_owner_failure(bot, row, uid, "ساختِ پنل", r.get("error"))      # PATCH_PSEL_OWNER_FAIL_NOTICE_V1
        if not (username and any(h in (r.get("error") or "").lower() for h in _TAKEN_HINTS)):
            await edit_or_send(event, "❌ ساختِ پنل با خطا روبه‌رو شد؛ مبلغی کم نشد (اگر کم شده بود برگشته).\n"
                                      "کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.", kb(back_row("psel:buy")))
    elif st != "ok":
        msg = {"bad_username": "نام‌کاربری نامعتبر است.", "username_taken": "این نام‌کاربری قبلاً گرفته شده.",
               "bad_amount": "مقدارِ نامعتبر.", "disabled": "این بخش فعال نیست."}.get(st) or _ERR_FA.get(st, "خرید انجام نشد.")
        await edit_or_send(event, "❌ " + msg, kb(back_row("psel:buy")))
    else:
        await edit_or_send(
            event, "✅ <b>پنلِ تو آماده است!</b>\n\n"
                   f"🌐 آدرس: <code>{esc(r['panel_url'])}</code>\n🆔 نام‌کاربری: <code>{esc(r['username'])}</code>\n"
                   f"🔑 رمز: <code>{esc(r['password'])}</code>\n🔖 کدِ پیگیری: <code>{esc(r['tracking'])}</code>\n\n"
                   "رمز را جایی امن نگه دار. وضعیتِ پنل از «🗂 پنل‌هایِ من» قابلِ مشاهده است.",
            kb(*_login_rows(r.get("panel_url")), [btn("🗂 پنل‌هایِ من", "psel:my", style=ButtonStyle.PRIMARY)], HOME_ROW))
    return r


def _login_rows(url: str | None) -> list:
    """PATCH_PSEL_LOGIN_BUTTON_V1 — دکمهٔ «ورود به پنل» (لینکِ مستقیم) اگر آدرسِ پنل http(s) باشد."""
    u = (url or "").strip()
    return [[btn("🌐 ورود به پنل", url=u, style=ButtonStyle.SUCCESS)]] if u.startswith(("http://", "https://")) else []


_USERNAME_PROMPT = ("🆔 <b>نام‌کاربریِ دلخواهِ پنل</b> را بفرست.\n\n"
                    "۳ تا ۳۲ نویسه؛ فقط حروفِ انگلیسی، عدد و <code>_</code> و با حرف شروع شود. مثلاً <code>shop_ali</code>")


@psel_user_router.callback_query(F.data.regexp(r"^psel:bok:\d+$"))
async def buy_do(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_or_create_user
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    pid = int(cb.data.split(":")[2])
    uid = cb.from_user.id
    await get_or_create_user(row.id, uid, cb.from_user.full_name or "", cb.from_user.username or "")
    if await SV.pick_username_enabled(row.id):                    # PATCH_PSEL_PICK_USERNAME_V1
        await state.clear()
        await state.set_state(PselBuyFSM.username)
        await state.update_data(pu_pid=pid, pu_gb=None)
        await edit_or_send(cb, _USERNAME_PROMPT, kb(back_row("psel:buy", "✖️ لغو")))
        return await cb.answer()
    await cb.answer("در حالِ ساختِ پنل…")
    await _provision(cb, bot, row, uid, pid=pid, retry_cb=cb.data)


@psel_user_router.message(PselBuyFSM.username, F.text)
async def username_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await state.clear()
    u = (msg.text or "").strip()
    if not SV.valid_username(u):
        return await msg.answer("❌ نام‌کاربری نامعتبر است.\n\n" + _USERNAME_PROMPT, parse_mode="HTML")
    if await SV.username_taken(row.id, u):
        return await msg.answer("❌ این نام‌کاربری قبلاً گرفته شده؛ یکی دیگر بفرست.", parse_mode="HTML")
    d = await state.get_data()
    pid, gb = d.get("pu_pid"), d.get("pu_gb")
    if pid is None and gb is None:
        await state.clear()
        return
    ok, why = await sales_gate(row)
    if not ok:
        await state.clear()
        return await msg.answer(f"⛔️ {why}")
    r = await _provision(msg, bot, row, msg.from_user.id, pid=pid, gb=gb, username=u,
                         retry_cb=(f"psel:cgo:{gb}" if gb is not None else f"psel:bok:{pid}"))
    if r["status"] == "panel_failed" and any(h in (r.get("error") or "").lower() for h in _TAKEN_HINTS):
        return await msg.answer("❌ این نام‌کاربری روی پنل گرفته شده؛ یکی دیگر بفرست.", parse_mode="HTML")
    await state.clear()


# ═══════════════════════ فروشِ حجمیِ دلخواه ═══════════════════════

@psel_user_router.callback_query(F.data == "psel:cvb")      # (قبلاً psel:cv بود و با دکمهٔ مدیر تداخل داشت ⇒ «فقط مدیرِ ربات»)
async def custom_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    cfg = await SV.custom_settings(row.id)
    if cfg["price"] <= 0:
        return await cb.answer("این بخش فعال نیست.", show_alert=True)
    await state.clear()
    await state.set_state(PselBuyFSM.custom_gb)
    await edit_or_send(
        cb, "📦 <b>خریدِ حجمِ دلخواه</b>\n\n"
            f"💵 هر گیگ: <b>{n(cfg['price'])}</b> تومان\n📏 حداقل {fa(cfg['min_gb'])} و حداکثر {fa(cfg['max_gb'])} گیگ\n"
            f"⏳ مدت: {days_text(cfg['days'])} · 👥 کاربر: {users_text(cfg['users'])}\n\n"
            "تعدادِ گیگِ موردنظرت را (فقط عدد) بفرست:", kb(back_row("psel:buy", "✖️ لغو")))
    await cb.answer()


@psel_user_router.message(PselBuyFSM.custom_gb, F.text)
async def custom_gb_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import fa_to_int
    row = await _row(bot)
    if not row:
        return await state.clear()
    cfg = await SV.custom_settings(row.id)
    v = fa_to_int((msg.text or "").strip())
    if cfg["price"] <= 0 or v is None or not (cfg["min_gb"] <= v <= max(cfg["min_gb"], cfg["max_gb"])):
        return await msg.answer(f"❌ عددی بین {fa(cfg['min_gb'])} تا {fa(cfg['max_gb'])} بفرست.")
    await state.clear()
    await msg.answer(f"📦 <b>{fa(v)} گیگ</b>\n💰 مبلغ: <b>{n(v * cfg['price'])}</b> تومان از کیف‌پول کسر می‌شود.\n"
                     f"⏳ مدت: {days_text(cfg['days'])}", parse_mode="HTML",
                     reply_markup=kb([btn("✅ خرید با کیف‌پول", f"psel:cgo:{v}", style=ButtonStyle.SUCCESS)], back_row("psel:buy")))


@psel_user_router.callback_query(F.data.regexp(r"^psel:cgo:\d+$"))
async def custom_buy(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_or_create_user
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    gb, uid = int(cb.data.split(":")[2]), cb.from_user.id
    await get_or_create_user(row.id, uid, cb.from_user.full_name or "", cb.from_user.username or "")
    if await SV.pick_username_enabled(row.id):
        await state.clear()
        await state.set_state(PselBuyFSM.username)
        await state.update_data(pu_pid=None, pu_gb=gb)
        await edit_or_send(cb, _USERNAME_PROMPT, kb(back_row("psel:buy", "✖️ لغو")))
        return await cb.answer()
    await cb.answer("در حالِ ساختِ پنل…")
    await _provision(cb, bot, row, uid, gb=gb, retry_cb=cb.data)


async def tell_owner_failure(bot: Bot, row, buyer_tg: int, what: str, error: str | None) -> None:
    """PATCH_PSEL_OWNER_FAIL_NOTICE_V1 — وقتی ساخت/تمدیدِ پنل روی خودِ پنل خطا می‌دهد، مشتری فقط پیامِ کلی می‌بیند؛ علتِ دقیق
    (مثلاً دسترسیِ sudo، آدرس/پورت، ورژنِ API) برایِ مالک و ادمین‌ها فرستاده می‌شود (حداکثر هر ۱۰ دقیقه یک‌بار برایِ هر ربات)."""
    try:
        from log_throttle import should_log
        if not should_log(f"psel_fail:{row.id}", 600):
            return
        from tenant import _notify_admins
        await _notify_admins(bot, row, (
            f"⚠️ <b>{esc(what)} برایِ مشتری <code>{buyer_tg}</code> روی پنل ناموفق بود</b>\n\n"
            f"🔎 علتِ پنل:\n<code>{esc((error or 'پاسخی از پنل نیامد')[:300])}</code>\n\n"
            "بررسی کن: آدرس/پورتِ پنل، نام‌کاربری و رمزِ sudo، اینکه حسابِ اتصال «sudo» باشد، و اینکه ربات به پنل دسترسیِ شبکه دارد."))
    except Exception:      # noqa: BLE001
        logger.exception("panel_seller: اطلاعِ خطایِ پنل به مالک ناموفق بود")


# ═══════════════════════ تستِ رایگان ═══════════════════════

@psel_user_router.callback_query(F.data.regexp(r"^psel:(tst|tstok)$"))
async def free_test(cb: CallbackQuery, bot: Bot):
    from tenant import get_or_create_user
    row = await _row(bot)
    if not row:
        return await cb.answer()
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    uid = cb.from_user.id
    cfg = await SV.test_settings(row.id)
    if not await SV.test_available(row.id, uid):
        return await cb.answer("تستِ رایگان فعال نیست یا قبلاً از آن استفاده کرده‌ای.", show_alert=True)
    if cb.data.endswith(":tst"):
        return await edit_or_send(
            cb, "🧪 <b>تستِ رایگان</b>\n\n"
                f"⏳ مدت: {fa(cfg['hours'])} ساعت · 📊 حجم: {gb_text(cfg['gb'])} · 👥 کاربر: {users_text(cfg['users'])}\n\n"
                "هر نفر یک‌بار می‌تواند تست بگیرد.",
            kb([btn("✅ ساختِ پنلِ تست", "psel:tstok", style=ButtonStyle.SUCCESS)], back_row("psel:buy")))
    await get_or_create_user(row.id, uid, cb.from_user.full_name or "", cb.from_user.username or "")
    await cb.answer("در حالِ ساختِ پنلِ تست…")
    r = await SV.buy_test(row.id, uid, bot=bot, row=row)
    st = r["status"]
    if st == "panel_failed":
        await tell_owner_failure(bot, row, uid, "ساختِ پنلِ تست", r.get("error"))
        return await edit_or_send(cb, "❌ ساختِ پنل با خطا روبه‌رو شد؛ کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.", kb(back_row("psel:buy")))
    if st != "ok":
        msg = {"test_used": "قبلاً تست گرفته‌ای.", "disabled": "تستِ رایگان فعال نیست.",
               "no_panel": "فعلاً پنلی برایِ تست متصل نیست.", "unsupported": "پنلِ متصل برایِ تست پشتیبانی نمی‌شود."}.get(st, "انجام نشد.")
        return await edit_or_send(cb, "❌ " + msg, kb(back_row("psel:buy")))
    await edit_or_send(
        cb, "✅ <b>پنلِ تستِ تو آماده است!</b>\n\n"
            f"🌐 آدرس: <code>{esc(r['panel_url'])}</code>\n🆔 نام‌کاربری: <code>{esc(r['username'])}</code>\n"
            f"🔑 رمز: <code>{esc(r['password'])}</code>\n\n"
            f"⏳ {fa(r['hours'])} ساعت · 📊 {gb_text(r['gb'])} · 👥 {users_text(r['users'])}\n"
            "بعد از پایانِ تست برایِ ادامه از «🧩 خریدِ پنل» بسته بخر.",
        kb(*_login_rows(r.get("panel_url")), [btn("🗂 پنل‌هایِ من", "psel:my", style=ButtonStyle.PRIMARY)], HOME_ROW))


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
            f"⏳ پایان: {exp_text(a.expires_at)}")
    if panel and a.status == "active":
        live = await _live_usage(a, panel)
        if live:
            used, users = live
            text += (f"\n📊 مصرف: {fa(f'{used:.1f}')} از {gb_text(a.data_gb)}\n👥 کاربران: {fa(users)} از {users_text(a.max_users)}")
    rows = []
    aid = a.id
    # === PATCH_PSEL_ACCOUNT_BUTTONS_V1 === (Whale Support) مثلِ صفحهٔ سرویس در رباتِ VPN
    rows.append([btn("📊 مشخصات و مصرف", f"psel:a:{aid}", style=ButtonStyle.SUCCESS)])
    if panel and (panel.base_url or "").startswith(("http://", "https://")):
        rows.append([btn("🌐 ورود به پنل وب", url=panel.base_url, style=ButtonStyle.PRIMARY)])
    try:
        from tenant import get_setting
        from tenant.shared.support_ticket import _support_contact_url
        _sup = _support_contact_url(await get_setting(row.id, "support_direct_contact", ""))
        if _sup:
            rows.append([btn("🆘 آدرسِ پشتیبانی", url=_sup, style=ButtonStyle.PRIMARY)])
    except Exception:      # noqa: BLE001
        logger.exception("panel_seller: ساختِ دکمهٔ پشتیبانی ناموفق بود")
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
    # === PATCH_PSEL_EXTRA_VOLUME_V1 ===
    if a.data_gb and (a.status == "active" or (a.status == "suspended" and a.suspend_reason == "traffic")) \
            and await SV.extra_gb_price(row.id) > 0:
        rows.append([btn("📈 خریدِ حجمِ اضافه", f"psel:xg:{aid}", style=ButtonStyle.SUCCESS)])
    if await _perm(row, "psel_allow_delete"):
        rows.append([btn("🗑 حذفِ دائمیِ سرویس", f"psel:dl:{aid}", style=ButtonStyle.DANGER)])
    rows.append(back_row("psel:my"))
    await edit_or_send(cb, text, kb(*rows))
    await cb.answer()


@psel_user_router.callback_query(F.data.regexp(r"^psel:(dl|dlok):\d+$"))
async def delete_flow(cb: CallbackQuery, bot: Bot):
    """PATCH_PSEL_ACCOUNT_BUTTONS_V1 — حذفِ دائمیِ پنل توسطِ خودِ مشتری (فقط اگر مالک اجازه داده؛ با تأییدِ دو مرحله‌ای)."""
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    if not await _perm(row, "psel_allow_delete"):
        return await cb.answer("حذفِ پنل توسطِ مدیر بسته است.", show_alert=True)
    if parts[1] == "dl":
        return await edit_or_send(
            cb, f"🗑 <b>حذفِ دائمیِ {esc(a.title)}</b>\n\nادمینِ <code>{esc(a.admin_username)}</code> و <b>همهٔ کاربرانش</b> از پنل پاک می‌شود "
                "و قابلِ برگشت نیست. مبلغی برگردانده نمی‌شود.\n\nمطمئنی؟",
            kb([btn("✅ بله، برای همیشه حذف کن", f"psel:dlok:{a.id}", style=ButtonStyle.DANGER)], back_row(f"psel:a:{a.id}")))
    await cb.answer("در حالِ حذف…")
    if not await SV.delete_account(row.id, a.id):
        return await edit_or_send(cb, "❌ حذف انجام نشد؛ کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.", kb(back_row(f"psel:a:{a.id}")))
    await edit_or_send(cb, "✅ پنل برایِ همیشه حذف شد.", kb(back_row("psel:my")))


_XG_PICKS = (5, 10, 20, 50, 100)


@psel_user_router.callback_query(F.data.regexp(r"^psel:xg:\d+$"))
async def extra_gb_menu(cb: CallbackQuery, bot: Bot):
    row, a = await _acc_for(cb, bot, int(cb.data.split(":")[2]))
    if not a:
        return
    unit = await SV.extra_gb_price(row.id)
    if unit <= 0 or not a.data_gb:
        return await cb.answer("خریدِ حجمِ اضافه برایِ این پنل فعال نیست.", show_alert=True)
    picks = [btn(f"{fa(g)} گیگ · {n(g * unit)} ت", f"psel:xb:{a.id}:{g}", style=ButtonStyle.PRIMARY) for g in _XG_PICKS]
    rows = [picks[i:i + 2] for i in range(0, len(picks), 2)]
    rows.append(back_row(f"psel:a:{a.id}"))
    await edit_or_send(cb, f"📈 <b>حجمِ اضافه برایِ {esc(a.title)}</b>\n\n💵 هر گیگ: <b>{n(unit)}</b> تومان\n"
                           f"📦 حجمِ فعلی: {gb_text(a.data_gb)}\n\nاز کیف‌پول کسر می‌شود و همین‌جا به حجمِ همین دوره اضافه می‌شود "
                           "(با تمدید، حجم به مقدارِ بسته برمی‌گردد).", kb(*rows))
    await cb.answer()


@psel_user_router.callback_query(F.data.regexp(r"^psel:xb:\d+:\d+$"))
async def extra_gb_buy(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    row, a = await _acc_for(cb, bot, int(parts[2]))
    if not a:
        return
    ok, why = await sales_gate(row)
    if not ok:
        return await cb.answer(why, show_alert=True)
    await cb.answer("در حالِ ثبت…")
    r = await SV.add_volume(row.id, a.id, cb.from_user.id, int(parts[3]), bot=bot, row=row)
    st = r["status"]
    if st == "insufficient":
        from tenant.shared import vs_pay
        return await vs_pay.insufficient_notice(cb.message, row.id, cb.from_user.id, int(r["need"]), cb.data)
    if st != "ok":
        msg = {"disabled": "خریدِ حجمِ اضافه فعال نیست.", "unlimited": "حجمِ این پنل نامحدود است.",
               "bad_amount": "مقدارِ نامعتبر.", "not_found": "این پنل الان قابلِ افزودنِ حجم نیست."}.get(st)
        if st == "panel_failed":
            await tell_owner_failure(bot, row, cb.from_user.id, "افزودنِ حجم", r.get("error"))
            msg = "پنل جواب نداد؛ مبلغی کم نشد. کمی بعد دوباره امتحان کن."
        return await edit_or_send(cb, "❌ " + (msg or "انجام نشد."), kb(back_row(f"psel:a:{a.id}")))
    extra = f"\n🔑 رمزِ تازه (چون پنل بسته شده بود): <code>{esc(r['new_password'])}</code>" if r.get("new_password") else ""
    await edit_or_send(cb, f"✅ حجمِ اضافه ثبت شد. حجمِ جدید: <b>{gb_text(r['new_gb'])}</b>{extra}", kb(back_row(f"psel:a:{a.id}")))


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
    if r["status"] == "panel_failed":
        await tell_owner_failure(bot, row, cb.from_user.id, "تمدیدِ پنل", r.get("error"))
    if r["status"] != "ok":
        return await edit_or_send(cb, "❌ تمدید انجام نشد" + (f":\n<code>{esc(r.get('error'))}</code>" if r.get("error") else ".")
                                  , kb(back_row(f"psel:a:{a.id}")))
    extra = f"\n🔑 رمزِ تازه: <code>{esc(r['new_password'])}</code>" if r.get("new_password") else ""
    await edit_or_send(cb, f"✅ تمدید شد. پایان: {exp_text(r['expires_at'])}{extra}", kb(back_row(f"psel:a:{a.id}")))


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
