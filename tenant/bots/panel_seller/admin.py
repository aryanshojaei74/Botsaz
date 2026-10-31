"""
admin.py — پنلِ مدیریتِ مالکِ رباتِ پنل‌فروشی (PATCH_PANEL_SELLER_V1).

پنل‌ها: همان ویزاردِ «پنل‌یار» (pyar:*) برایِ اتصالِ پنل استفاده می‌شود؛ اینجا بسته‌ها، ادمین‌هایِ فروخته‌شده،
ریستارتِ پنل و دسترسی‌هایِ مشتری است. همهٔ صفحه‌ها با ادیتِ درجا و دکمهٔ بازگشت.
callbackها: psel:adm · psel:pk* · psel:ac* · psel:rs* · psel:set*
"""
from __future__ import annotations

import html as _html

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from config import Session
from models import PanelSellerAccount, PanelSellerPackage, VpnPanel

from tenant.bots.panel_seller import adapter as AD
from tenant.bots.panel_seller import service as SV
from tenant.bots.panel_seller.common import (HOME_ROW, REASON_FA, STATUS_FA, back_row, btn, edit_or_send, esc, fa,
                                             fmt_time, gb_text, is_admin, kb, n, pk_line, status_line, users_text)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

psel_admin_router = Router(name="psel_admin")
MENU = "psel:adm"
PAGE = 8

# تنظیماتِ دسترسیِ مشتری: کلید → (برچسب, پیش‌فرض)
PERMS = {
    "psel_allow_renew": ("🔁 تمدیدِ پنل توسطِ مشتری", "1"),
    "psel_allow_pw": ("🔑 تغییرِ رمزِ پنل توسطِ مشتری", "0"),
    "psel_allow_toggle": ("⏯ خاموش/روشنِ اشتراک‌ها توسطِ مشتری", "0"),
}


class PselFSM(StatesGroup):
    title = State()
    gb = State()
    users = State()
    days = State()
    price = State()
    extra_price = State()      # PATCH_PSEL_EXTRA_VOLUME_V1


async def _guard(event, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    uid = event.from_user.id if getattr(event, "from_user", None) else 0
    if not row or row.field != "panel_seller" or not await is_admin(row, uid):
        if hasattr(event, "data"):
            try:
                await event.answer("فقط مدیرِ ربات.", show_alert=True)
            except Exception:      # noqa: BLE001
                pass
        return None
    return row


async def _panels(bot_id: int) -> list[VpnPanel]:
    async with Session() as s:
        return list((await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == bot_id).order_by(VpnPanel.id))).all())


# ═══════════════════════ منوی اصلی ═══════════════════════

async def menu_content(row):
    panels = await _panels(row.id)
    sup = [p for p in panels if AD.supports(p.panel_key)]
    async with Session() as s:
        pk = int(await s.scalar(select(func.count(PanelSellerPackage.id)).where(
            PanelSellerPackage.bot_id == row.id, PanelSellerPackage.active == True)) or 0)   # noqa: E712
        act = int(await s.scalar(select(func.count(PanelSellerAccount.id)).where(
            PanelSellerAccount.bot_id == row.id, PanelSellerAccount.status == "active")) or 0)
        sus = int(await s.scalar(select(func.count(PanelSellerAccount.id)).where(
            PanelSellerAccount.bot_id == row.id, PanelSellerAccount.status == "suspended")) or 0)
    text = (f"🧩 <b>مدیریتِ پنل‌فروشی</b>\n\n{await status_line(row)}\n\n"
            f"🗂 پنل‌هایِ متصل: <b>{fa(len(panels))}</b> (قابلِ فروش: {fa(len(sup))})\n"
            f"📦 بسته‌هایِ فعال: <b>{fa(pk)}</b>\n"
            f"👤 ادمین‌هایِ فروخته‌شده: 🟢 {fa(act)} · 🔴 {fa(sus)}")
    if panels and not sup:
        text += "\n\n⚠️ هیچ‌کدام از پنل‌هایِ متصل «ادمین» ندارند؛ فقط خانوادهٔ مرزبان (مرزبان/ربکا/پاسارگادِ قدیمی/ریک‌پنل) قابلِ فروش است."
    return text, kb(
        [btn("🗂 پنل‌هایِ متصل (اتصال/حذف)", "pyar:menu", style=ButtonStyle.PRIMARY)],
        [btn("📦 بسته‌هایِ فروش", "psel:pk", style=ButtonStyle.SUCCESS),
         btn("👤 ادمین‌هایِ فروخته‌شده", "psel:ac:0", style=ButtonStyle.PRIMARY)],
        [btn("🔄 ریستارتِ پنل", "psel:rs", style=ButtonStyle.PRIMARY),
         btn("⚙️ دسترسی‌هایِ مشتری", "psel:set", style=ButtonStyle.PRIMARY)],
        HOME_ROW)


async def show_menu(event, bot: Bot, row) -> None:
    text, markup = await menu_content(row)
    await edit_or_send(event, text, markup)


@psel_admin_router.callback_query(F.data == MENU)
async def adm_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    await show_menu(cb, bot, row)
    await cb.answer()


# ═══════════════════════ بسته‌ها ═══════════════════════

async def _packages(bot_id: int) -> list[PanelSellerPackage]:
    async with Session() as s:
        return list((await s.scalars(select(PanelSellerPackage).where(
            PanelSellerPackage.bot_id == bot_id).order_by(PanelSellerPackage.id))).all())


@psel_admin_router.callback_query(F.data == "psel:pk")
async def pk_list(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    pks = await _packages(row.id)
    rows = [[btn(("🟢 " if p.active else "⚪️ ") + p.title[:30] + f" — {n(p.price_toman)}", f"psel:pk:{p.id}")] for p in pks]
    rows.append([btn("➕ بستهٔ جدید", "psel:pk:new", style=ButtonStyle.SUCCESS)])
    rows.append(back_row(MENU))
    await edit_or_send(cb, "📦 <b>بسته‌هایِ فروش</b>\n\n"
                           "هر بسته = یک ادمینِ بدونِ sudo روی یکی از پنل‌هایِ متصل، با سقفِ حجم/کاربر و مدتِ مشخص."
                       + ("" if pks else "\n\nهنوز بسته‌ای نساخته‌ای."), kb(*rows))
    await cb.answer()


@psel_admin_router.callback_query(F.data.regexp(r"^psel:pk:\d+$"))
async def pk_detail(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(PanelSellerPackage, pid)
        panel = await s.get(VpnPanel, p.panel_id) if p else None
    if not p or p.bot_id != row.id:
        return await cb.answer("پیدا نشد.", show_alert=True)
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS
    plabel = PANEL_LABELS.get(panel.panel_key, panel.panel_key) if panel else "— حذف‌شده —"
    await edit_or_send(cb, f"{pk_line(p)}\n🗂 پنل: {esc(plabel)}\nوضعیت: {'🟢 فعال' if p.active else '⚪️ غیرفعال'}",
                       kb([btn("⚪️ غیرفعال‌کردن" if p.active else "🟢 فعال‌کردن", f"psel:pkt:{pid}", style=ButtonStyle.PRIMARY)],
                          [btn("🗑 حذف", f"psel:pkd:{pid}", style=ButtonStyle.DANGER)], back_row("psel:pk")))
    await cb.answer()


@psel_admin_router.callback_query(F.data.regexp(r"^psel:pk[td]:\d+$"))
async def pk_toggle_delete(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    act, pid = cb.data.split(":")[1], int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(PanelSellerPackage, pid)
        if not p or p.bot_id != row.id:
            return await cb.answer("پیدا نشد.", show_alert=True)
        if act == "pkt":
            p.active = not p.active
            await s.commit()
            await cb.answer("انجام شد.")
        else:
            await s.delete(p)                 # حسابِ فروخته‌شده بسته‌اش را با package_id نگه می‌دارد؛ تمدیدش «بدونِ بسته» رد می‌شود
            await s.commit()
            await cb.answer("حذف شد.")
    if act == "pkt":
        return await pk_detail(cb, bot)
    await pk_list(cb, bot, _NullState())


class _NullState:
    async def clear(self):
        return None


@psel_admin_router.callback_query(F.data == "psel:pk:new")
async def pk_new(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS
    panels = [p for p in await _panels(row.id) if AD.supports(p.panel_key)]
    if not panels:
        return await cb.answer("اول از «🗂 پنل‌هایِ متصل» یک پنلِ مرزبان‌خانواده وصل کن.", show_alert=True)
    rows = [[btn(PANEL_LABELS.get(p.panel_key, p.panel_key), f"psel:pkp:{p.id}", style=ButtonStyle.PRIMARY)] for p in panels]
    rows.append(back_row("psel:pk"))
    await state.clear()
    await edit_or_send(cb, "🗂 این بسته روی کدام پنل ساخته شود؟", kb(*rows))
    await cb.answer()


@psel_admin_router.callback_query(F.data.startswith("psel:pkp:"))
async def pk_panel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        panel = await s.get(VpnPanel, pid)
    if not panel or panel.bot_id != row.id or not AD.supports(panel.panel_key):
        return await cb.answer("پنل معتبر نیست.", show_alert=True)
    await state.set_state(PselFSM.title)
    await state.update_data(psel_panel=pid, psel_msg=(cb.message.chat.id, cb.message.message_id))
    await edit_or_send(cb, "✏️ نامِ بسته را بفرست (مثلاً «ادمین ۱۰۰ گیگ ماهانه»):", kb(back_row("psel:pk")))
    await cb.answer()


async def _step(msg: Message, bot: Bot, state: FSMContext, text: str, nxt) -> None:
    await state.set_state(nxt)
    try:
        await msg.delete()
    except Exception:      # noqa: BLE001
        pass
    await msg.answer(text, reply_markup=kb(back_row("psel:pk", "✖️ لغو")), parse_mode="HTML")


def _num(text: str | None) -> int | None:
    from tenant import fa_to_int
    v = fa_to_int((text or "").replace(",", "").replace("٬", ""))
    return v if v is not None and v >= 0 else None


@psel_admin_router.message(PselFSM.title, F.text)
async def pk_title(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard(msg, bot):
        return
    t = (msg.text or "").strip()
    if not t or len(t) > 100:
        return await msg.answer("نام باید ۱ تا ۱۰۰ کاراکتر باشد.")
    await state.update_data(psel_title=t)
    await _step(msg, bot, state, "📊 حجمِ مجازِ مصرفِ کلِ کاربرانِ این ادمین به <b>گیگابایت</b> (۰ = نامحدود):", PselFSM.gb)


@psel_admin_router.message(PselFSM.gb, F.text)
async def pk_gb(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard(msg, bot):
        return
    v = _num(msg.text)
    if v is None:
        return await msg.answer("یک عددِ صحیح بفرست (۰ = نامحدود).")
    await state.update_data(psel_gb=v)
    await _step(msg, bot, state, "👥 سقفِ تعدادِ کاربرانی که این ادمین می‌سازد (۰ = نامحدود):", PselFSM.users)


@psel_admin_router.message(PselFSM.users, F.text)
async def pk_users(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard(msg, bot):
        return
    v = _num(msg.text)
    if v is None:
        return await msg.answer("یک عددِ صحیح بفرست (۰ = نامحدود).")
    await state.update_data(psel_users=v)
    await _step(msg, bot, state, "📅 مدتِ بسته به <b>روز</b> (مثلاً ۳۰):", PselFSM.days)


@psel_admin_router.message(PselFSM.days, F.text)
async def pk_days(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard(msg, bot):
        return
    v = _num(msg.text)
    if not v or v > 3650:
        return await msg.answer("یک عددِ صحیح بین ۱ تا ۳۶۵۰ بفرست.")
    await state.update_data(psel_days=v)
    await _step(msg, bot, state, "💰 قیمت به <b>تومان</b> (۰ = رایگان):", PselFSM.price)


@psel_admin_router.message(PselFSM.price, F.text)
async def pk_price(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return
    v = _num(msg.text)
    if v is None:
        return await msg.answer("یک عددِ صحیح بفرست.")
    d = await state.get_data()
    await state.clear()
    async with Session() as s:
        p = PanelSellerPackage(bot_id=row.id, panel_id=int(d["psel_panel"]), title=d["psel_title"], data_gb=int(d["psel_gb"]),
                               max_users=int(d["psel_users"]), days=int(d["psel_days"]), price_toman=v, active=True)
        s.add(p)
        await s.commit()
    await msg.answer("✅ بسته ساخته شد:\n" + pk_line(p), reply_markup=kb(
        [btn("📦 بسته‌ها", "psel:pk", style=ButtonStyle.PRIMARY)], back_row(MENU)), parse_mode="HTML")


# ═══════════════════════ ادمین‌هایِ فروخته‌شده ═══════════════════════

@psel_admin_router.callback_query(F.data.regexp(r"^psel:ac:\d+$"))
async def ac_list(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    page = int(cb.data.split(":")[2])
    async with Session() as s:
        accs = (await s.scalars(select(PanelSellerAccount).where(
            PanelSellerAccount.bot_id == row.id, PanelSellerAccount.status != "deleted")
            .order_by(PanelSellerAccount.id.desc()).offset(page * PAGE).limit(PAGE + 1))).all()
    more = len(accs) > PAGE
    accs = accs[:PAGE]
    rows = [[btn(f"{STATUS_FA.get(a.status, a.status)[:1]} {a.admin_username} · {a.buyer_tg}", f"psel:ac:v:{a.id}")] for a in accs]
    nav = []
    if page:
        nav.append(btn("⬅️ قبلی", f"psel:ac:{page - 1}"))
    if more:
        nav.append(btn("بعدی ➡️", f"psel:ac:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(back_row(MENU))
    await edit_or_send(cb, "👤 <b>ادمین‌هایِ فروخته‌شده</b>" + ("" if accs else "\n\nهنوز فروشی ثبت نشده."), kb(*rows))
    await cb.answer()


async def _acc_view(row, aid: int):
    async with Session() as s:
        a = await s.get(PanelSellerAccount, aid)
        panel = await s.get(VpnPanel, a.panel_id) if a else None
    if not a or a.bot_id != row.id:
        return None, None
    why = f" ({REASON_FA.get(a.suspend_reason, a.suspend_reason)})" if a.status == "suspended" and a.suspend_reason else ""
    text = (f"👤 <b>{esc(a.title)}</b>\n🆔 <code>{esc(a.admin_username)}</code>\n"
            f"خریدار: <code>{a.buyer_tg}</code>\nوضعیت: {STATUS_FA.get(a.status, a.status)}{why}\n"
            f"📊 {gb_text(a.data_gb)} · 👥 {users_text(a.max_users)}\n⏳ پایان: {fmt_time(a.expires_at)}\n"
            f"🔖 {esc(a.tracking_code or '—')}\n🌐 <code>{esc(panel.base_url) if panel else '—'}</code>")
    rows = []
    if a.status == "active":
        rows.append([btn("⏸ خاموشِ اشتراک‌ها", f"psel:ato:{aid}:0", style=ButtonStyle.PRIMARY),
                     btn("▶️ روشنِ اشتراک‌ها", f"psel:ato:{aid}:1", style=ButtonStyle.SUCCESS)])
        rows.append([btn("🔴 تعلیقِ ادمین", f"psel:asp:{aid}", style=ButtonStyle.DANGER)])
    elif a.status == "suspended":
        rows.append([btn("🟢 فعال‌سازیِ دوباره (رمزِ تازه)", f"psel:are:{aid}", style=ButtonStyle.SUCCESS)])
    rows.append([btn("🗑 حذفِ ادمین از پنل", f"psel:adl:{aid}", style=ButtonStyle.DANGER)])
    rows.append(back_row("psel:ac:0"))
    return text, kb(*rows)


@psel_admin_router.callback_query(F.data.regexp(r"^psel:ac:v:\d+$"))
async def ac_view(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    text, markup = await _acc_view(row, int(cb.data.split(":")[3]))
    if not text:
        return await cb.answer("پیدا نشد.", show_alert=True)
    await edit_or_send(cb, text, markup)
    await cb.answer()


@psel_admin_router.callback_query(F.data.regexp(r"^psel:ato:\d+:[01]$"))
async def ac_toggle_users(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, aid, en = cb.data.split(":")
    r = await SV.set_users_enabled(row.id, int(aid), None, en == "1")
    await cb.answer("✅ انجام شد." if r["status"] == "ok" else f"❌ {r.get('error') or r['status']}"[:190], show_alert=r["status"] != "ok")


@psel_admin_router.callback_query(F.data.regexp(r"^psel:asp:\d+$"))
async def ac_suspend(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    aid = int(cb.data.split(":")[2])
    async with Session() as s:
        a = await s.get(PanelSellerAccount, aid)
    if not a or a.bot_id != row.id:
        return await cb.answer("پیدا نشد.", show_alert=True)
    ok = await SV.suspend(aid, "owner")
    await cb.answer("🔴 معلق شد." if ok else "❌ تعلیق روی پنل انجام نشد.", show_alert=not ok)
    text, markup = await _acc_view(row, aid)
    if text:
        await edit_or_send(cb, text, markup)


@psel_admin_router.callback_query(F.data.regexp(r"^psel:are:\d+$"))
async def ac_resume(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    aid = int(cb.data.split(":")[2])
    r = await SV.resume(row.id, aid)
    if r["status"] != "ok":
        return await cb.answer(f"❌ {r.get('error') or r['status']}"[:190], show_alert=True)
    try:
        await bot.send_message(r["buyer_tg"], "🟢 پنلِ تو دوباره فعال شد.\n"
                               f"🆔 <code>{esc(r['username'])}</code>\n🔑 رمزِ تازه: <code>{esc(r['password'])}</code>", parse_mode="HTML")
    except Exception:      # noqa: BLE001
        logger.info("panel_seller: خبرِ فعال‌سازی به خریدار نرسید")
    await cb.answer("🟢 فعال شد و رمزِ تازه برایِ خریدار فرستاده شد.", show_alert=True)
    text, markup = await _acc_view(row, aid)
    if text:
        await edit_or_send(cb, text, markup)


@psel_admin_router.callback_query(F.data.regexp(r"^psel:adl(ok)?:\d+$"))
async def ac_delete(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    parts = cb.data.split(":")
    aid = int(parts[2])
    if parts[1] == "adl":
        return await _ask_confirm(cb, f"⚠️ ادمین از روی پنل <b>حذف</b> می‌شود (اشتراک‌هایش می‌مانند ولی صاحب ندارند). مطمئنی؟",
                                  f"psel:adlok:{aid}", f"psel:ac:v:{aid}")
    ok = await SV.delete_account(row.id, aid)
    await cb.answer("🗑 حذف شد." if ok else "❌ حذف روی پنل انجام نشد.", show_alert=not ok)
    if ok:
        await edit_or_send(cb, "🗑 ادمین حذف شد.", kb(back_row("psel:ac:0")))


async def _ask_confirm(cb: CallbackQuery, text: str, yes_cb: str, no_cb: str) -> None:
    await edit_or_send(cb, text, kb([btn("✅ بله", yes_cb, style=ButtonStyle.DANGER), btn("⬅️ نه، بازگشت", no_cb)]))
    await cb.answer()


# ═══════════════════════ ریستارتِ پنل ═══════════════════════

@psel_admin_router.callback_query(F.data == "psel:rs")
async def rs_list(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS
    panels = [p for p in await _panels(row.id) if AD.supports(p.panel_key)]
    rows = [[btn(PANEL_LABELS.get(p.panel_key, p.panel_key), f"psel:rs:{p.id}", style=ButtonStyle.PRIMARY)] for p in panels]
    rows.append(back_row(MENU))
    await edit_or_send(cb, "🔄 <b>ریستارتِ هستهٔ پنل</b>\n\nکدام پنل ریستارت شود؟" + ("" if panels else "\n\nپنلِ پشتیبانی‌شده‌ای وصل نیست."),
                       kb(*rows))
    await cb.answer()


@psel_admin_router.callback_query(F.data.regexp(r"^psel:rs(ok)?:\d+$"))
async def rs_do(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    parts = cb.data.split(":")
    pid = int(parts[2])
    async with Session() as s:
        panel = await s.get(VpnPanel, pid)
    if not panel or panel.bot_id != row.id or not AD.supports(panel.panel_key):
        return await cb.answer("پنل معتبر نیست.", show_alert=True)
    if parts[1] == "rs":
        return await _ask_confirm(cb, "⚠️ با ریستارت، اتصالِ همهٔ کاربرانِ آن پنل چند ثانیه قطع می‌شود. مطمئنی؟",
                                  f"psel:rsok:{pid}", "psel:rs")
    await cb.answer("در حالِ ریستارت…")
    try:
        api = AD.PanelAdminAPI.for_panel(panel)
        try:
            await api.restart_core()
        finally:
            await api.close()
    except Exception as e:  # noqa: BLE001
        return await edit_or_send(cb, f"❌ ریستارت انجام نشد:\n<code>{_html.escape(str(e)[:300])}</code>", kb(back_row("psel:rs")))
    await edit_or_send(cb, "✅ دستورِ ریستارت به پنل فرستاده شد.", kb(back_row("psel:rs")))


# ═══════════════════════ دسترسی‌هایِ مشتری ═══════════════════════

async def _set_view(row):
    from tenant import get_setting
    rows = []
    for key, (label, default) in PERMS.items():
        on = (await get_setting(row.id, key, default)) == "1"
        rows.append([btn(f"{label}: {'✅ مجاز' if on else '⛔️ ممنوع'}", f"psel:set:{key}",
                         style=ButtonStyle.SUCCESS if on else ButtonStyle.DANGER)])
    from tenant.bots.panel_seller.service import extra_gb_price
    _xp = await extra_gb_price(row.id)
    rows.append([btn(f"📈 حجمِ اضافه: {fa(f'{_xp:,}') + ' تومان/گیگ' if _xp else '⛔️ خاموش'}", "psel:xp",
                     style=ButtonStyle.SUCCESS if _xp else ButtonStyle.DANGER)])
    rows.append(back_row(MENU))
    return ("⚙️ <b>دسترسی‌هایِ مشتری</b>\n\nمشخص کن مشتری از «🗂 پنل‌هایِ من» چه کارهایی بتواند بکند. "
            "هر کدام را بزنی جابه‌جا می‌شود."), kb(*rows)


@psel_admin_router.callback_query(F.data == "psel:set")
async def set_menu(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    text, markup = await _set_view(row)
    await edit_or_send(cb, text, markup)
    await cb.answer()


@psel_admin_router.callback_query(F.data.startswith("psel:set:"))
async def set_toggle(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if key not in PERMS:
        return await cb.answer()
    from tenant import get_setting, set_setting
    cur = (await get_setting(row.id, key, PERMS[key][1])) == "1"
    await set_setting(row.id, key, "0" if cur else "1")
    text, markup = await _set_view(row)
    await edit_or_send(cb, text, markup)
    await cb.answer("انجام شد.")


# === PATCH_PSEL_EXTRA_VOLUME_V1 === قیمتِ هر گیگ حجمِ اضافه (۰ = خاموش)
@psel_admin_router.callback_query(F.data == "psel:xp")
async def extra_price_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.set_state(PselFSM.extra_price)
    await edit_or_send(cb, "📈 قیمتِ <b>هر گیگ</b> حجمِ اضافه را به تومان بفرست.\n"
                           "مشتری از «🗂 پنل‌هایِ من» هر مقدار (۵/۱۰/۲۰/۵۰/۱۰۰ گیگ) بخرد، به همین نرخ حساب می‌شود.\n"
                           "برایِ خاموش‌کردنِ فروشِ حجمِ اضافه <code>0</code> بفرست.", kb(back_row("psel:set", "✖️ لغو")))
    await cb.answer()


@psel_admin_router.message(PselFSM.extra_price, F.text)
async def extra_price_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return
    v = _num(msg.text)
    if v is None or v > 100_000_000:
        return await msg.answer("❌ یک عددِ معتبر (تومان) بفرست؛ یا 0 برایِ خاموش.")
    from tenant import set_setting
    await set_setting(row.id, "psel_extra_gb_price", str(int(v)))
    await state.clear()
    text, markup = await _set_view(row)
    await msg.answer(("✅ فروشِ حجمِ اضافه روشن شد.\n\n" if v else "✅ فروشِ حجمِ اضافه خاموش شد.\n\n") + text,
                     reply_markup=markup, parse_mode="HTML")
