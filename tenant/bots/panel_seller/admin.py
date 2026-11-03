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
from tenant.bots.panel_seller.common import (exp_text, HOME_ROW, REASON_FA, STATUS_FA, back_row, btn, edit_or_send, esc, fa,
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
    "psel_allow_delete": ("🗑 حذفِ دائمیِ پنل توسطِ مشتری", "0"),     # PATCH_PSEL_ACCOUNT_BUTTONS_V1
    "psel_allow_showpw": ("🔑 نمایشِ رمزِ پنل به مشتری", "1"),         # PATCH_PSEL_PASSWORD_VAULT_V1
    "psel_keep_login": ("🔓 پایانِ حجم: ورودِ مشتری باز بماند", "1"),    # PATCH_PSEL_KEEP_LOGIN_V1 (فقط کاربرانِ پنل خاموش می‌شوند)
}


class PselFSM(StatesGroup):
    title = State()
    gb = State()
    users = State()
    days = State()
    price = State()
    extra_price = State()      # PATCH_PSEL_EXTRA_VOLUME_V1
    test_cfg = State()         # PATCH_PSEL_FREE_TEST_V1
    custom_cfg = State()       # PATCH_PSEL_CUSTOM_VOLUME_V1
    test_channel = State()     # PATCH_PSEL_TEST_NOTIFY_V1
    lookup = State()           # PATCH_PSEL_ADMIN_LOOKUP_V1
    vol_delta = State()        # PATCH_PSEL_ADMIN_LOOKUP_V1
    note = State()             # PATCH_PSEL_NOTES_V1


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
        [btn("🛠 مدیریتِ کاملِ پنل (کاربران، ساخت، گزارش، ادمین‌ها)", "psel:mg", style=ButtonStyle.SUCCESS)],     # PATCH_PSEL_MANAGE_V1
        [btn("🔎 جستجو و مدیریتِ یک پنل (نام‌کاربری/کد)", "psel:lk", style=ButtonStyle.SUCCESS)],
        [btn("🔄 ریستارتِ پنل", "psel:rs", style=ButtonStyle.PRIMARY),
         btn("⚙️ دسترسی‌هایِ مشتری", "psel:set", style=ButtonStyle.PRIMARY)],
        [btn("🛡 نقشِ ادمینِ پنل (پاسارگاردِ جدید)", "psel:role", style=ButtonStyle.PRIMARY)],
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
    await _step(msg, bot, state, "📅 مدتِ بسته به <b>روز</b> (مثلاً ۳۰) — <code>0</code> = نامحدود (بدونِ انقضا):", PselFSM.days)


@psel_admin_router.message(PselFSM.days, F.text)
async def pk_days(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard(msg, bot):
        return
    v = _num(msg.text)
    if v is None or v > 3650:
        return await msg.answer("یک عددِ صحیح بین ۰ تا ۳۶۵۰ بفرست (۰ = نامحدود).")
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


async def _acc_view(row, aid: int, live: bool = False):
    async with Session() as s:
        a = await s.get(PanelSellerAccount, aid)
        panel = await s.get(VpnPanel, a.panel_id) if a else None
    if not a or a.bot_id != row.id:
        return None, None
    why = f" ({REASON_FA.get(a.suspend_reason, a.suspend_reason)})" if a.status == "suspended" and a.suspend_reason else ""
    text = (f"👤 <b>{esc(a.title)}</b>\n🆔 <code>{esc(a.admin_username)}</code>\n"
            f"خریدار: <code>{a.buyer_tg}</code>\nوضعیت: {STATUS_FA.get(a.status, a.status)}{why}\n"
            f"📊 {gb_text(a.data_gb)} · 👥 {users_text(a.max_users)}\n⏳ پایان: {exp_text(a.expires_at)}\n"
            f"🔖 {esc(a.tracking_code or '—')}\n🌐 <code>{esc(panel.base_url) if panel else '—'}</code>")
    if live and panel and a.status in ("active", "suspended"):             # PATCH_PSEL_ADMIN_LOOKUP_V1
        from tenant.bots.panel_seller.user import _live_usage
        lv = await _live_usage(a, panel)
        text += (f"\n📈 مصرفِ زنده: {fa(f'{lv[0]:.1f}')} از {gb_text(a.data_gb)} · 👥 {fa(lv[1])} کاربر" if lv
                 else "\n📈 مصرفِ زنده: ❌ پنل جواب نداد")
    rows = []
    if a.data_gb and a.status in ("active", "suspended"):
        rows.append([btn("➕ افزایشِ حجم", f"psel:avl:{aid}:p", style=ButtonStyle.SUCCESS),
                     btn("➖ کاهشِ حجم", f"psel:avl:{aid}:m", style=ButtonStyle.DANGER)])
    if a.status == "active":
        rows.append([btn("⏸ خاموشِ اشتراک‌ها", f"psel:ato:{aid}:0", style=ButtonStyle.PRIMARY),
                     btn("▶️ روشنِ اشتراک‌ها", f"psel:ato:{aid}:1", style=ButtonStyle.SUCCESS)])
        rows.append([btn("🔴 تعلیقِ ادمین", f"psel:asp:{aid}", style=ButtonStyle.DANGER)])
    elif a.status == "suspended":
        rows.append([btn("🟢 فعال‌سازیِ دوباره (رمزِ تازه)", f"psel:are:{aid}", style=ButtonStyle.SUCCESS)])
    rows.append([btn("🗑 حذفِ ادمین از پنل", f"psel:adl:{aid}", style=ButtonStyle.DANGER)])
    if a.status in ("active", "suspended") and int(a.price_toman or 0) > 0:       # PATCH_PSEL_REFUND_V1
        rows.append([btn(f"💸 بازگشتِ وجه ({n(a.price_toman)} تومان) و حذفِ پنل", f"psel:rfd:{aid}", style=ButtonStyle.DANGER)])
    rows.append(back_row("psel:ac:0"))
    return text, kb(*rows)


@psel_admin_router.callback_query(F.data.regexp(r"^psel:ac:v:\d+$"))
async def ac_view(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    text, markup = await _acc_view(row, int(cb.data.split(":")[3]), live=True)
    if not text:
        return await cb.answer("پیدا نشد.", show_alert=True)
    await edit_or_send(cb, text, markup)
    await cb.answer()


# ═══════════════════════ جستجو و مدیریتِ یک پنل ═══════════════════════
# === PATCH_PSEL_ADMIN_LOOKUP_V1 === (Whale Support) مثلِ «مدیریت کدِ سرویس» در رباتِ کانفیگ‌فروش

@psel_admin_router.callback_query(F.data == "psel:lk")
async def lk_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.set_state(PselFSM.lookup)
    await edit_or_send(cb, "🔎 <b>جستجوی پنل</b>\n\nیکی از این‌ها را بفرست:\n• نام‌کاربریِ ادمینِ پنل\n• کدِ پیگیری\n• آیدیِ عددیِ خریدار",
                       kb(back_row(MENU, "✖️ لغو")))
    await cb.answer()


def _acc_pick_kb(accs) -> object:
    rows = [[btn(f"{STATUS_FA.get(a.status, a.status)[:1]} {a.admin_username} · {a.buyer_tg}", f"psel:ac:v:{a.id}")] for a in accs]
    rows.append(back_row(MENU))
    return kb(*rows)


@psel_admin_router.message(PselFSM.lookup, F.text)
async def lk_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return await state.clear()
    accs = await SV.find_accounts(row.id, msg.text or "")
    if not accs:
        return await msg.answer("❌ پنلی پیدا نشد. نام‌کاربری، کدِ پیگیری یا آیدیِ خریدار را دوباره بفرست (یا «✖️ لغو»).",
                                reply_markup=kb(back_row(MENU, "✖️ لغو")))
    await state.clear()
    if len(accs) == 1:
        text, markup = await _acc_view(row, accs[0].id, live=True)
        return await msg.answer(text, reply_markup=markup, parse_mode="HTML")
    await msg.answer(f"🔎 {fa(len(accs))} پنل پیدا شد؛ یکی را انتخاب کن:", reply_markup=_acc_pick_kb(accs))


@psel_admin_router.callback_query(F.data.regexp(r"^psel:avl:\d+:[pm]$"))
async def vol_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, aid, sign = cb.data.split(":")
    async with Session() as s:
        a = await s.get(PanelSellerAccount, int(aid))
    if not a or a.bot_id != row.id or not a.data_gb:
        return await cb.answer("این پنل حجمِ مشخص ندارد.", show_alert=True)
    await state.set_state(PselFSM.vol_delta)
    await state.update_data(vl_aid=int(aid), vl_sign=sign)
    word = "افزایش" if sign == "p" else "کاهش"
    await edit_or_send(cb, f"{'➕' if sign == 'p' else '➖'} چند گیگ <b>{word}</b> پیدا کند؟ (عدد)\nحجمِ فعلی: <b>{gb_text(a.data_gb)}</b>",
                       kb(back_row(f"psel:ac:v:{aid}", "✖️ لغو")))
    await cb.answer()


@psel_admin_router.message(PselFSM.vol_delta, F.text)
async def vol_do(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return await state.clear()
    t = (msg.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if not t.isdigit() or not (1 <= int(t) <= 100000):
        return await msg.answer("❌ یک عددِ صحیحِ مثبت بفرست (مثلاً 20).")
    d = await state.get_data()
    aid, sign = int(d.get("vl_aid") or 0), d.get("vl_sign")
    await state.clear()
    r = await SV.admin_adjust_volume(row.id, aid, int(t) * (1 if sign == "p" else -1))
    err = {"unlimited": "این پنل حجمِ نامحدود دارد.", "below_min": "حجم نمی‌تواند کمتر از ۱ گیگ شود.",
           "not_found": "پنل پیدا نشد.", "no_panel": "پنلِ متصل پیدا نشد.", "bad_amount": "مقدارِ نامعتبر."}
    if r["status"] != "ok":
        return await msg.answer("❌ " + (err.get(r["status"]) or f"روی پنل انجام نشد:\n{esc(r.get('error') or r['status'])}"),
                                parse_mode="HTML", reply_markup=kb(back_row(f"psel:ac:v:{aid}")))
    note = f"\n🔓 پنل دوباره فعال شد." if r.get("reopened") else ""
    note += f"\n🔑 رمزِ تازه: <code>{esc(r['new_password'])}</code>" if r.get("new_password") else ""
    try:
        verb = "افزایش" if sign == "p" else "کاهش"
        await bot.send_message(r["buyer_tg"], f"📊 حجمِ پنلِ <code>{esc(r['username'])}</code> توسطِ مدیر {verb} یافت.\n"
                               f"حجمِ جدید: <b>{gb_text(r['new_gb'])}</b>" + (f"\n🔑 رمزِ تازه: <code>{esc(r['new_password'])}</code>" if r.get("new_password") else ""),
                               parse_mode="HTML")
    except Exception:      # noqa: BLE001
        logger.info("panel_seller: خبرِ تغییرِ حجم به خریدار نرسید")
    text, markup = await _acc_view(row, aid)
    await msg.answer(f"✅ حجمِ جدید: <b>{gb_text(r['new_gb'])}</b> (به خریدار هم اطلاع داده شد).{note}\n\n{text}",
                     reply_markup=markup, parse_mode="HTML")


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


@psel_admin_router.callback_query(F.data.regexp(r"^psel:rfd(ok)?:\d+$"))
async def ac_refund(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    parts = cb.data.split(":")
    aid = int(parts[2])
    async with Session() as s:
        a = await s.get(PanelSellerAccount, aid)
    if not a or a.bot_id != row.id or a.status == "deleted":
        return await cb.answer("پیدا نشد.", show_alert=True)
    if parts[1] == "rfd":
        return await _ask_confirm(
            cb, f"💸 <b>بازگشتِ وجه</b>\n\nمبلغِ <b>{n(a.price_toman)}</b> تومان به کیف‌پولِ خریدار (<code>{a.buyer_tg}</code>) برمی‌گردد و "
                f"پنلِ <code>{esc(a.admin_username)}</code> از سرور <b>حذف</b> می‌شود.\nاین کار برگشت‌پذیر نیست. مطمئنی؟",
            f"psel:rfdok:{aid}", f"psel:ac:v:{aid}")
    r = await SV.refund_account(row.id, aid)
    if r["status"] != "ok":
        err = {"nothing_to_refund": "مبلغی برای بازگشت ثبت نشده.", "not_found": "پیدا نشد (شاید قبلاً انجام شده)."}
        return await cb.answer("❌ " + (err.get(r["status"]) or f"حذفِ پنل انجام نشد: {r.get('error')}")[:190], show_alert=True)
    try:
        await bot.send_message(r["buyer_tg"], f"💸 مبلغِ <b>{n(r['amount'])}</b> تومان بابتِ پنلِ <code>{esc(r['username'])}</code> به کیف‌پولت برگشت.\n"
                               f"💰 موجودیِ فعلی: <b>{n(r['balance'])}</b> تومان", parse_mode="HTML")
    except Exception:      # noqa: BLE001
        logger.info("panel_seller: خبرِ بازگشتِ وجه به خریدار نرسید")
    await edit_or_send(cb, f"✅ <b>{n(r['amount'])}</b> تومان به کیف‌پولِ خریدار برگشت و پنل حذف شد.", kb(back_row("psel:ac:0")))
    await cb.answer()


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
    from tenant.bots.panel_seller.service import custom_settings, extra_gb_price, pick_username_enabled, test_settings
    _pu = await pick_username_enabled(row.id)
    rows.append([btn(f"👤 انتخابِ نام‌کاربری توسطِ مشتری: {'✅ روشن' if _pu else '⛔️ خاموش'}", "psel:pu",
                     style=ButtonStyle.SUCCESS if _pu else ButtonStyle.DANGER)])
    _cv = await custom_settings(row.id)
    _cv_label = ("📦 فروشِ حجمِ دلخواه: " + (fa(f"{_cv['price']:,}") + f" تومان/گیگ · حداقل {fa(_cv['min_gb'])}" if _cv["price"] else "⛔️ خاموش"))
    rows.append([btn(_cv_label, "psel:cv", style=ButtonStyle.SUCCESS if _cv["price"] else ButtonStyle.PRIMARY)])
    _tc = await test_settings(row.id)
    rows.append([btn(f"🧪 تستِ رایگان: {'✅ روشن' if _tc['enabled'] else '⛔️ خاموش'}", "psel:tt",
                     style=ButtonStyle.SUCCESS if _tc["enabled"] else ButtonStyle.DANGER)])
    rows.append([btn(f"⚙️ مشخصاتِ تست: {fa(_tc['hours'])} ساعت · {fa(_tc['gb'])} گیگ · {fa(_tc['users'])} کاربر", "psel:tc",
                     style=ButtonStyle.PRIMARY)])
    from tenant.bots.panel_seller.service import test_notify_settings
    _tn = await test_notify_settings(row.id)
    rows.append([btn(f"📣 اعلانِ تست‌ها به ادمین: {'✅ روشن' if _tn['admins'] else '⛔️ خاموش'}", "psel:tn",
                     style=ButtonStyle.SUCCESS if _tn["admins"] else ButtonStyle.DANGER)])
    rows.append([btn("📢 کانالِ لاگِ تست‌ها: " + (_tn["channel"] if _tn["channel"] else "❌ متصل نیست"), "psel:tch",
                     style=ButtonStyle.SUCCESS if _tn["channel"] else ButtonStyle.PRIMARY)])
    if _tn["admins"] or _tn["channel"]:                # PATCH_PSEL_TEST_REPORT_PARITY_V1
        from tenant.shared.buyer_ident import LABELS as _BL, get_mode as _gm
        rows.append([btn(f"🆔 نمایشِ مشتری در اعلان: {_BL[await _gm(row.id)]}", "psel:tid", style=ButtonStyle.PRIMARY),
                     btn("🧪 اعلانِ نمونه", "psel:tsm", style=ButtonStyle.SUCCESS)])
    from tenant import get_setting as _gs
    _ni = bool((await _gs(row.id, "psel_note_invoice", "")).strip())
    _nd = bool((await _gs(row.id, "psel_note_delivery", "")).strip())
    rows.append([btn(f"🧾 یادداشتِ پیش‌فاکتور: {'✅ دارد' if _ni else '—'}", "psel:nti", style=ButtonStyle.PRIMARY),
                 btn(f"📨 یادداشتِ تحویل: {'✅ دارد' if _nd else '—'}", "psel:ntd", style=ButtonStyle.PRIMARY)])
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


# === PATCH_PSEL_FREE_TEST_V1 === تستِ رایگان: روشن/خاموش + مشخصات (ساعت، گیگ، کاربر)
@psel_admin_router.callback_query(F.data == "psel:tt")
async def test_toggle(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    from tenant import get_setting, set_setting
    cur = (await get_setting(row.id, "psel_test_enabled", "0")) == "1"
    await set_setting(row.id, "psel_test_enabled", "0" if cur else "1")
    text, markup = await _set_view(row)
    await edit_or_send(cb, text, markup)
    await cb.answer("تستِ رایگان روشن شد ✅" if not cur else "خاموش شد.")


@psel_admin_router.callback_query(F.data == "psel:tc")
async def test_cfg_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.set_state(PselFSM.test_cfg)
    await edit_or_send(cb, "⚙️ مشخصاتِ تستِ رایگان را در <b>یک پیام</b> و با فاصله بفرست:\n"
                           "<code>ساعت  گیگ  حداکثرِ‌کاربر</code>\n\nمثلاً <code>24 1 3</code> یعنی ۲۴ ساعت، ۱ گیگ، ۳ کاربر "
                           "(حداکثرِ کاربر <code>0</code> = نامحدود).", kb(back_row("psel:set", "✖️ لغو")))
    await cb.answer()


@psel_admin_router.message(PselFSM.test_cfg, F.text)
async def test_cfg_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return
    parts = [_num(x) for x in (msg.text or "").replace("،", " ").replace(",", " ").split()]
    if len(parts) != 3 or any(v is None for v in parts):
        return await msg.answer("❌ سه عدد بفرست: ساعت، گیگ، حداکثرِ کاربر؛ مثلاً <code>24 1 3</code>", parse_mode="HTML")
    hours, gb, users = parts
    if not (1 <= hours <= 720 and 1 <= gb <= 1000 and 0 <= users <= 10000):
        return await msg.answer("❌ ساعت ۱ تا ۷۲۰، گیگ ۱ تا ۱۰۰۰، کاربر ۰ تا ۱۰۰۰۰.")
    from tenant import set_setting
    await set_setting(row.id, "psel_test_hours", str(hours))
    await set_setting(row.id, "psel_test_gb", str(gb))
    await set_setting(row.id, "psel_test_users", str(users))
    await state.clear()
    text, markup = await _set_view(row)
    await msg.answer("✅ مشخصاتِ تست ذخیره شد.\n\n" + text, reply_markup=markup, parse_mode="HTML")


# === PATCH_PSEL_PASARGUARD_ROLE_V1 === انتخابِ نقشِ ادمینِ فروخته‌شده (پاسارگاردِ جدید «role_id» اجباری می‌خواهد)
@psel_admin_router.callback_query(F.data == "psel:role")
async def role_panels(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    sup = [p for p in await _panels(row.id) if AD.supports(p.panel_key)]
    if not sup:
        return await cb.answer("پنلِ قابلِ‌فروشی متصل نیست.", show_alert=True)
    from tenant import get_setting
    rows = []
    for p in sup:
        cur = (await get_setting(row.id, f"psel_role_{p.id}", "")) or "خودکار"
        rows.append([btn(f"{(p.base_url or '')[:30]} — نقش: {cur}", f"psel:roleP:{p.id}", style=ButtonStyle.PRIMARY)])
    rows.append(back_row(MENU))
    await edit_or_send(cb, "🛡 <b>نقشِ ادمینِ پنل</b>\n\nپاسارگاردِ جدید برایِ هر ادمین یک «نقش» می‌خواهد. نقشی را انتخاب کن که "
                           "فقط کاربرانِ خودِ مشتری را مدیریت می‌کند (بدونِ دسترسیِ مدیریتِ ادمین‌ها/تنظیمات). "
                           "اگر نقشی انتخاب نکنی و پنل فقط یک نقشِ غیرِمالک داشته باشد، همان استفاده می‌شود.", kb(*rows))
    await cb.answer()


@psel_admin_router.callback_query(F.data.regexp(r"^psel:roleP:\d+$"))
async def role_list(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        panel = await s.get(VpnPanel, pid)
    if not panel or panel.bot_id != row.id or not AD.supports(panel.panel_key):
        return await cb.answer("پنل معتبر نیست.", show_alert=True)
    try:
        async with AD.PanelAdminAPI.for_panel(panel) as api:
            roles = await api.list_roles()
    except Exception as e:      # noqa: BLE001
        return await edit_or_send(cb, f"❌ خواندنِ نقش‌ها ممکن نشد:\n<code>{_html.escape(str(e)[:300])}</code>", kb(back_row("psel:role")))
    cand = [r for r in roles if not r.get("is_owner")]
    if not cand:
        return await edit_or_send(cb, "این پنل نقشی برایِ ادمین ندارد (پنلِ نسخهٔ قدیمی)؛ نیازی به انتخاب نیست.", kb(back_row("psel:role")))
    rows = [[btn(f"{str(r.get('name'))[:30]} (#{r['id']})", f"psel:roleS:{pid}:{int(r['id'])}", style=ButtonStyle.SUCCESS)] for r in cand]
    rows.append([btn("🔄 خودکار (پیش‌فرض)", f"psel:roleS:{pid}:0", style=ButtonStyle.PRIMARY)])
    rows.append(back_row("psel:role"))
    await edit_or_send(cb, "🛡 یکی از نقش‌هایِ زیر را برایِ ادمین‌هایِ فروخته‌شده انتخاب کن:", kb(*rows))
    await cb.answer()


@psel_admin_router.callback_query(F.data.regexp(r"^psel:roleS:\d+:\d+$"))
async def role_set(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, pid, rid = cb.data.split(":")
    async with Session() as s:
        panel = await s.get(VpnPanel, int(pid))
    if not panel or panel.bot_id != row.id:
        return await cb.answer("پنل معتبر نیست.", show_alert=True)
    from tenant import set_setting
    await set_setting(row.id, f"psel_role_{int(pid)}", "" if int(rid) == 0 else str(int(rid)))
    await cb.answer("ذخیره شد ✅")
    await role_panels(cb, bot)


# === PATCH_PSEL_PICK_USERNAME_V1 ===
@psel_admin_router.callback_query(F.data == "psel:pu")
async def pick_username_toggle(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    from tenant import get_setting, set_setting
    cur = (await get_setting(row.id, "psel_pick_username", "0")) == "1"
    await set_setting(row.id, "psel_pick_username", "0" if cur else "1")
    text, markup = await _set_view(row)
    await edit_or_send(cb, text, markup)
    await cb.answer("مشتری نام‌کاربری را خودش انتخاب می‌کند ✅" if not cur else "نامِ تصادفی (پیش‌فرض).")


# === PATCH_PSEL_CUSTOM_VOLUME_V1 === فروشِ حجمیِ دلخواه: قیمتِ هر گیگ، حداقل/حداکثر، سقفِ کاربر، روز (۰ = نامحدود)
@psel_admin_router.callback_query(F.data == "psel:cv")
async def custom_cfg_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.set_state(PselFSM.custom_cfg)
    await edit_or_send(
        cb, "📦 <b>فروشِ حجمیِ دلخواه</b>\n\nمشتری تعدادِ گیگ را خودش می‌نویسد (با حداقل) و پنل با همان حجم ساخته می‌شود.\n\n"
            "پنج عدد را در <b>یک پیام</b> و با فاصله بفرست:\n"
            "<code>قیمتِ‌هرگیگ  حداقلِ‌گیگ  حداکثرِ‌گیگ  حداکثرِ‌کاربر  روز</code>\n\n"
            "مثلاً <code>5000 10 500 0 0</code> یعنی هر گیگ ۵٬۰۰۰ تومان، حداقل ۱۰ و حداکثر ۵۰۰ گیگ، کاربرِ نامحدود، <b>زمانِ نامحدود</b> "
            "(برایِ کاربر و روز، ۰ = نامحدود).\nبرایِ خاموش‌کردنِ این فروش قیمت را <code>0</code> بفرست (مثلاً <code>0 0 0 0 0</code>).",
        kb(back_row("psel:set", "✖️ لغو")))
    await cb.answer()


@psel_admin_router.message(PselFSM.custom_cfg, F.text)
async def custom_cfg_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return
    parts = [_num(x) for x in (msg.text or "").replace("،", " ").replace(",", " ").replace("٬", " ").split()]
    if len(parts) != 5 or any(v is None for v in parts):
        return await msg.answer("❌ پنج عدد بفرست؛ مثلاً <code>5000 10 500 0 0</code>", parse_mode="HTML")
    price, mn, mx, users, days = parts
    if price > 0 and not (1 <= mn <= mx <= 100000 and users <= 100000 and days <= 3650):
        return await msg.answer("❌ شرط: حداقل ≥ ۱ و ≤ حداکثر، کاربر ≤ ۱۰۰۰۰۰، روز ≤ ۳۶۵۰.")
    from tenant import set_setting
    await set_setting(row.id, "psel_custom_price_gb", str(price))
    if price > 0:
        for k, v in (("min_gb", mn), ("max_gb", mx), ("users", users), ("days", days)):
            await set_setting(row.id, f"psel_custom_{k}", str(v))
    await state.clear()
    text, markup = await _set_view(row)
    await msg.answer(("✅ فروشِ حجمیِ دلخواه روشن شد.\n\n" if price else "✅ فروشِ حجمیِ دلخواه خاموش شد.\n\n") + text,
                     reply_markup=markup, parse_mode="HTML")


# === PATCH_PSEL_TEST_NOTIFY_V1 === اعلانِ تست‌ها: به ادمین‌هایِ ربات و/یا کانالِ لاگ
@psel_admin_router.callback_query(F.data == "psel:tn")
async def test_notify_toggle(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    from tenant import get_setting, set_setting
    cur = (await get_setting(row.id, "psel_test_notify", "0")) == "1"
    await set_setting(row.id, "psel_test_notify", "0" if cur else "1")
    text, markup = await _set_view(row)
    await edit_or_send(cb, text, markup)
    await cb.answer("از این به بعد با هر تست به ادمین‌ها پیام می‌آید ✅" if not cur else "خاموش شد.")


# ═══════════════════════ یادداشتِ پیش‌فاکتور / تحویل ═══════════════════════
# === PATCH_PSEL_NOTES_V1 === مثلِ «یادداشت در فاکتور» و «یادداشتِ تحویل» رباتِ VPN
NOTE_KEYS = {"i": ("psel_note_invoice", "🧾 یادداشتِ پیش‌فاکتور", "زیرِ قیمت، در صفحهٔ هر بسته (قبل از خرید) به مشتری نمایش داده می‌شود."),
             "d": ("psel_note_delivery", "📨 یادداشتِ تحویل", "زیرِ پیامِ «پنلِ تو آماده است» بعد از هر خرید/تست می‌آید (مثلاً آموزشِ ورود یا قوانین).")}
NOTE_MAX = 600


@psel_admin_router.callback_query(F.data.regexp(r"^psel:nt[id]$"))
async def note_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    from tenant import get_setting
    k = cb.data[-1]
    key, title, where = NOTE_KEYS[k]
    cur = (await get_setting(row.id, key, "")).strip()
    await state.set_state(PselFSM.note)
    await state.update_data(note_k=k)
    await edit_or_send(cb, f"<b>{title}</b>\n\n{where}\n\nمتنِ فعلی: " + (f"\n<blockquote>{esc(cur)}</blockquote>" if cur else "—") +
                       f"\n\nمتنِ جدید را بفرست (حداکثر {fa(NOTE_MAX)} نویسه). برایِ حذف <code>-</code> بفرست.", kb(back_row("psel:set", "✖️ لغو")))
    await cb.answer()


@psel_admin_router.message(PselFSM.note, F.text)
async def note_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return await state.clear()
    from tenant import set_setting
    d = await state.get_data()
    k = d.get("note_k")
    if k not in NOTE_KEYS:
        return await state.clear()
    t = (msg.text or "").strip()
    if len(t) > NOTE_MAX:
        return await msg.answer(f"❌ بیش از {fa(NOTE_MAX)} نویسه است ({fa(len(t))}). کوتاه‌ترش کن.")
    await set_setting(row.id, NOTE_KEYS[k][0], "" if t in ("-", "—") else t)
    await state.clear()
    text, markup = await _set_view(row)
    await msg.answer(("🗑 حذف شد." if t in ("-", "—") else "✅ ذخیره شد.") + "\n\n" + text, reply_markup=markup, parse_mode="HTML")


@psel_admin_router.callback_query(F.data == "psel:tid")
async def test_ident_cycle(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    from tenant.shared.buyer_ident import LABELS, cycle
    new = await cycle(row.id)
    text, markup = await _set_view(row)
    await edit_or_send(cb, text, markup)
    await cb.answer("✅ " + LABELS[new])


@psel_admin_router.callback_query(F.data == "psel:tsm")
async def test_sample(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    from types import SimpleNamespace
    fake = SimpleNamespace(full_name=cb.from_user.full_name, username=cb.from_user.username, telegram_id=cb.from_user.id)
    sent = await SV.notify_test_taken(bot, row, fake, "sample_admin", 24, 1, 5, "نمونه")
    await cb.answer("✅ اعلانِ نمونه فرستاده شد (به همان مقصدهایِ روشن)." if sent else "هیچ مقصدی روشن نیست.", show_alert=True)


@psel_admin_router.callback_query(F.data == "psel:tch")
async def test_channel_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.set_state(PselFSM.test_channel)
    await edit_or_send(
        cb, "📢 <b>کانالِ لاگِ تست‌ها</b>\n\nبا هر تستِ رایگان، در این کانال پیام می‌آید که <b>چه کسی چه حجمی گرفته</b>.\n\n"
            "۱) ربات را در کانال <b>ادمین</b> کن (اجازهٔ ارسالِ پیام)\n۲) یوزرنیمِ کانال را بفرست؛ مثلاً <code>@my_channel</code> یا "
            "<code>https://t.me/my_channel</code> (برایِ کانالِ خصوصی: آیدیِ عددی مثلِ <code>-1001234567890</code>)\n\n"
            "برایِ قطعِ اتصال <code>-</code> بفرست.", kb(back_row("psel:set", "✖️ لغو")))
    await cb.answer()


@psel_admin_router.message(PselFSM.test_channel, F.text)
async def test_channel_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return
    from tenant import set_setting
    from tenant.bots.panel_seller.service import parse_channel
    raw = (msg.text or "").strip()
    if raw in ("-", "—"):
        await set_setting(row.id, "psel_test_log_channel", "")
        await state.clear()
        text, markup = await _set_view(row)
        return await msg.answer("✅ کانالِ لاگ قطع شد.\n\n" + text, reply_markup=markup, parse_mode="HTML")
    ch = parse_channel(raw)
    if not ch:
        return await msg.answer("❌ نامعتبر. مثلاً <code>@my_channel</code> یا <code>-1001234567890</code> بفرست (یا <code>-</code> برایِ قطع).",
                                parse_mode="HTML")
    try:                                      # با یک پیامِ آزمایشی مطمئن می‌شویم ربات واقعاً می‌تواند بنویسد
        await bot.send_message(ch, "✅ کانالِ لاگِ تست‌هایِ پنل‌فروشی متصل شد؛ از این به بعد با هر تستِ رایگان اینجا اعلان می‌آید.")
    except Exception as e:      # noqa: BLE001
        return await msg.answer("❌ ربات نتوانست در این کانال پیام بفرستد. اول ربات را در کانال ادمین کن و دوباره بفرست.\n"
                                f"🔎 <code>{_html.escape(str(e)[:200])}</code>", parse_mode="HTML")
    await set_setting(row.id, "psel_test_log_channel", ch)
    await state.clear()
    text, markup = await _set_view(row)
    await msg.answer("✅ کانالِ لاگ متصل شد.\n\n" + text, reply_markup=markup, parse_mode="HTML")
