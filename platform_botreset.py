"""
platform_botreset.py — بخشِ «ریستِ کاملِ ربات» جدا از platform_bot.py.

مالکِ ربات از «🗂 ربات‌های من ← ربات ← ♻️ ریستِ کاملِ ربات» می‌تواند چیدمانِ
دکمه‌ها، تنظیمات، محصولات، و اگر صریحاً خودش بخواهد حتی موجودی/سفارش‌ها را
صفر کند و از نو بسازد — بدونِ اینکه مجبور باشد ربات را حذف و دوباره بسازد.

=== PATCH_BOTRESET_CHECKLIST_V2 ===
گزارشِ ادمین (فدایی): نسخهٔ قبلی («فقط دکمه‌ها» / «دکمه‌ها + تنظیمات») هیچ‌وقت
محصولات، پنل‌ها، موجودی یا سفارش‌ها را پاک نمی‌کرد — عمداً، برایِ امنیت. ولی
بعضی ادمین‌ها (مثلاً موقعِ تست یا راه‌اندازیِ دوباره) واقعاً یک پاک‌سازیِ
کامل‌تر می‌خواهند. به‌جایِ اینکه خودمان تصمیم بگیریم، حالا خودِ ادمین با یک
چک‌لیستِ مستقل انتخاب می‌کند دقیقاً چه چیزی پاک شود:

  🎛 دکمه‌ها          — دکمه‌ها/زیرمنوها/اسلایدها
  ⚙️ تنظیمات          — همهٔ ردیف‌هایِ Setting (به‌جزِ چند کلیدِ حیاتی)
  🛒 محصولات و پنل‌ها  — SmmService/SmmCategory/SmmPanel/VpnPlan/VpnPanel
  💰 موجودی و سفارش‌ها — ⚠️ خطرناک: موجودیِ کیف‌پولِ همه صفر، سفارش‌ها و
                          تیکت‌ها پاک می‌شوند. این یکی به‌تنهایی کافی نیست؛
                          باید یوزرنیمِ ربات را هم تایپ کنی تا فعال شود —
                          چون این تنها گزینه‌ای است که رویِ پولِ واقعیِ
                          مشتری‌ها اثر می‌گذارد.

هیچ‌وقت پاک نمی‌شوند (در هیچ ترکیبی): خودِ ردیفِ کاربران (BotUser)، خودِ
ردیفِ ربات (توکن/پلن/سررسید).
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select, update

from config import Session
from models import (Bot as BotRow, BotUser, ButtonSetting, ButtonSlide, ButtonTrialClaim,
                    BotMenuItem, MainButton, Order, PanelAccount, Setting, SmmCategory,
                    SmmOrder, SmmPanel, SmmService, TenantTicket, TenantTicketReply,
                    VnumNumber, VpnPanel, VpnPlan)
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

botreset_router = Router()


class ResetFSM(StatesGroup):
    confirm_money = State()  # === PATCH_BOTRESET_CHECKLIST_V2 === تایپِ یوزرنیمِ ربات


# کلیدهایی که حتی در ریستِ «تنظیمات» هم باید بمانند — پاک‌شدنشان یعنی ربات
# عملاً از دسترس خارج می‌شود یا یک قابلیتِ خریداری‌شده از بین می‌رود.
_KEEP_KEYS = {
    "menu_mode",          # حالتِ کیبورد/شیشه‌ای — بدونش منو ناهماهنگ می‌شود
    "bot_lang",
    "custom_domain",      # دامنهٔ اختصاصیِ خریداری‌شده
    "miniapp_secret",
}

_FLAG_LABEL = {
    "btn": "🎛 دکمه‌ها و زیرمنوها",
    "set": "⚙️ تنظیمات (کارت، درگاه‌ها، متن‌ها)",
    "prod": "🛒 محصولات و پنل‌ها",
    "money": "💰 موجودی و سفارش‌ها ⚠️",
}
_FLAG_ORDER = ["btn", "set", "prod", "money"]


def _parse_flags(s: str) -> set[str]:
    return {f for f in s.split(",") if f in _FLAG_LABEL} if s else set()


def _flags_str(flags: set[str]) -> str:
    return ",".join(f for f in _FLAG_ORDER if f in flags)


async def _counts(bot_id: int) -> dict[str, int]:
    """شمارشِ ردیف‌ها برایِ نمایش در تأییدیه — یک کوئریِ سبک برایِ هرکدام."""
    async with Session() as s:
        async def c(model, *where):
            return int(await s.scalar(select(func.count()).select_from(model)
                                      .where(model.bot_id == bot_id, *where)) or 0)
        return {
            "btn": await c(MainButton),
            "set": await c(Setting, Setting.key.notin_(list(_KEEP_KEYS))),
            "smm_svc": await c(SmmService),
            "smm_cat": await c(SmmCategory),
            "smm_panel": await c(SmmPanel),
            "vpn_plan": await c(VpnPlan),
            "vpn_panel": await c(VpnPanel),
            "orders": await c(Order),
            "smm_orders": await c(SmmOrder),
            "panel_accounts": await c(PanelAccount),
            "tickets": await c(TenantTicket),
        }


async def _owns(bot_id: int, user_id: int) -> BotRow | None:
    """ردیفِ ربات را فقط اگر متعلق به همین کاربر باشد برمی‌گرداند."""
    async with Session() as s:
        r = await s.get(BotRow, bot_id)
        if not r or r.owner_id != user_id or r.archived_at:
            return None
        return r


async def do_reset(bot_id: int, flags: set[str]) -> dict[str, int]:
    """ریست را طبقِ flags انجام می‌دهد و شمارشِ هرچیزِ پاک‌شده را برمی‌گرداند.

    ترتیب مهم است (به‌خاطرِ FOREIGN KEY):
      ۱) اگر «money» است: قبل از حذفِ SmmOrder، سرِ VnumNumber.sold_order_pk
         که به آن اشاره می‌کند را خالی می‌کنیم (فقط اگر «prod» نبود که
         خودِ VnumNumber را کامل حذف می‌کند).
      ۲) اگر «prod» است: اول VnumNumber (چون NOT NULL به SmmService وصل
         است)، بعد خودِ SmmService/SmmCategory/SmmPanel/VpnPlan/VpnPanel.
      ۳) اگر «money» است: Order، SmmOrder، PanelAccount، تیکت‌ها (اول
         TenantTicketReply چون به TenantTicket وصل است)، و صفرکردنِ
         BotUser.balance.
      ۴) دکمه‌ها و تنظیمات — دقیقاً مثلِ نسخهٔ قبلی.
    """
    n: dict[str, int] = {}
    async with Session() as s:
        if "money" in flags and "prod" not in flags:
            await s.execute(update(VnumNumber).where(VnumNumber.bot_id == bot_id)
                            .values(sold_order_pk=None))

        if "prod" in flags:
            res = await s.execute(delete(VnumNumber).where(VnumNumber.bot_id == bot_id))
            n["vnum"] = res.rowcount or 0
            res = await s.execute(delete(SmmService).where(SmmService.bot_id == bot_id))
            n["smm_svc"] = res.rowcount or 0
            res = await s.execute(delete(SmmCategory).where(SmmCategory.bot_id == bot_id))
            n["smm_cat"] = res.rowcount or 0
            res = await s.execute(delete(SmmPanel).where(SmmPanel.bot_id == bot_id))
            n["smm_panel"] = res.rowcount or 0
            res = await s.execute(delete(VpnPlan).where(VpnPlan.bot_id == bot_id))
            n["vpn_plan"] = res.rowcount or 0
            res = await s.execute(delete(VpnPanel).where(VpnPanel.bot_id == bot_id))
            n["vpn_panel"] = res.rowcount or 0

        if "money" in flags:
            res = await s.execute(delete(Order).where(Order.bot_id == bot_id))
            n["orders"] = res.rowcount or 0
            res = await s.execute(delete(SmmOrder).where(SmmOrder.bot_id == bot_id))
            n["smm_orders"] = res.rowcount or 0
            res = await s.execute(delete(PanelAccount).where(PanelAccount.bot_id == bot_id))
            n["panel_accounts"] = res.rowcount or 0
            ticket_ids = [i for i in (await s.scalars(
                select(TenantTicket.id).where(TenantTicket.bot_id == bot_id))).all()]
            if ticket_ids:
                await s.execute(delete(TenantTicketReply)
                                .where(TenantTicketReply.ticket_id.in_(ticket_ids)))
            res = await s.execute(delete(TenantTicket).where(TenantTicket.bot_id == bot_id))
            n["tickets"] = res.rowcount or 0
            res = await s.execute(update(BotUser).where(BotUser.bot_id == bot_id, BotUser.balance != 0)
                                  .values(balance=0))
            n["wallets_zeroed"] = res.rowcount or 0

        if "btn" in flags:
            btn_ids = [i for i in (await s.scalars(
                select(MainButton.id).where(MainButton.bot_id == bot_id))).all()]
            if btn_ids:
                await s.execute(delete(ButtonSlide).where(ButtonSlide.button_id.in_(btn_ids)))
            await s.execute(delete(ButtonTrialClaim).where(ButtonTrialClaim.bot_id == bot_id))
            await s.execute(delete(BotMenuItem).where(BotMenuItem.bot_id == bot_id))
            await s.execute(delete(ButtonSetting).where(ButtonSetting.bot_id == bot_id))
            res = await s.execute(delete(MainButton).where(MainButton.bot_id == bot_id))
            n["btn"] = res.rowcount or len(btn_ids)

        if "set" in flags:
            res = await s.execute(delete(Setting).where(
                Setting.bot_id == bot_id, Setting.key.notin_(list(_KEEP_KEYS))))
            n["set"] = res.rowcount or 0

        await s.commit()
    logger.info("[RESET] bot=%s flags=%s result=%s", bot_id, sorted(flags), n)
    return n


def _menu_kb(bot_id: int, flags: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for f in _FLAG_ORDER:
        on = f in flags
        rows.append([hx_button(
            f"{'✅' if on else '⬜'} {_FLAG_LABEL[f]}",
            callback_data=f"mbrs:tog:{bot_id}:{f}:{_flags_str(flags)}",
            style=ButtonStyle.DANGER if (on and f == "money") else ButtonStyle.PRIMARY)])
    rows.append([hx_button("▶️ ادامه", callback_data=f"mbrs:ask:{bot_id}:{_flags_str(flags)}",
                           style=ButtonStyle.SUCCESS)])
    rows.append([hx_button("⬅️ بی‌خیال", callback_data=f"mb:{bot_id}", style=ButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@botreset_router.callback_query(F.data.startswith("mbrs:menu:"))
async def reset_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        bot_id = int(cb.data.split(":")[2])
    except Exception:
        await cb.answer("داده نامعتبر", show_alert=True)
        return
    r = await _owns(bot_id, cb.from_user.id)
    if not r:
        await cb.answer("ربات یافت نشد", show_alert=True)
        return
    text = (
        f"♻️ <b>ریستِ ربات @{r.username}</b>\n\n"
        "هرکدام را که می‌خواهی پاک شود تیک بزن، بقیه دست‌نخورده می‌مانند:\n\n"
        "🎛 <b>دکمه‌ها</b> — دکمه‌ها، زیرمنوها، اسلایدها\n"
        "⚙️ <b>تنظیمات</b> — کارت، کلیدهایِ درگاه‌ها، متن‌های خوش‌آمد و…\n"
        "🛒 <b>محصولات و پنل‌ها</b> — سرویس‌ها/دسته‌بندی‌هایِ SMM، پلن‌ها و "
        "اتصالِ پنل‌هایِ VPN\n"
        "💰 <b>موجودی و سفارش‌ها</b> — ⚠️ کیف‌پولِ همهٔ کاربران صفر می‌شود؛ "
        "تاریخچهٔ سفارش‌ها و تیکت‌ها پاک می‌شود. این گزینه به تأییدِ اضافه "
        "نیاز دارد.\n\n"
        "✅ همیشه دست‌نخورده می‌مانند: خودِ کاربران، توکن و پلنِ ربات.\n\n"
        "⚠️ هرچه انتخاب کنی، برگشت‌پذیر نیست."
    )
    try:
        await cb.message.edit_text(text, reply_markup=_menu_kb(bot_id, set()))
    except Exception:
        await cb.message.answer(text, reply_markup=_menu_kb(bot_id, set()))
    await cb.answer()


@botreset_router.callback_query(F.data.startswith("mbrs:tog:"))
async def reset_toggle(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        _, _, sid, flag, cur = cb.data.split(":", 4)
        bot_id = int(sid)
    except Exception:
        await cb.answer("داده نامعتبر", show_alert=True)
        return
    r = await _owns(bot_id, cb.from_user.id)
    if not r:
        await cb.answer("ربات یافت نشد", show_alert=True)
        return
    flags = _parse_flags(cur)
    flags.symmetric_difference_update({flag})
    try:
        await cb.message.edit_reply_markup(reply_markup=_menu_kb(bot_id, flags))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (رفرشِ چک‌لیستِ ریست)")
    await cb.answer()


def _summary_lines(counts: dict[str, int], flags: set[str]) -> list[str]:
    lines = []
    if "btn" in flags:
        lines.append(f"🎛 <b>{counts['btn']}</b> دکمه/زیرمنو")
    if "set" in flags:
        lines.append(f"⚙️ <b>{counts['set']}</b> ردیفِ تنظیمات")
    if "prod" in flags:
        lines.append(f"🛒 <b>{counts['smm_svc']}</b> سرویسِ SMM، "
                     f"<b>{counts['smm_cat']}</b> دسته، "
                     f"<b>{counts['vpn_plan']}</b> پلنِ VPN، "
                     f"<b>{counts['smm_panel'] + counts['vpn_panel']}</b> اتصالِ پنل")
    if "money" in flags:
        lines.append(f"💰 <b>{counts['orders'] + counts['smm_orders']}</b> سفارش، "
                     f"<b>{counts['panel_accounts']}</b> سرویسِ فروخته‌شدهٔ VPN، "
                     f"<b>{counts['tickets']}</b> تیکت — و "
                     "<b>موجودیِ کیف‌پولِ همهٔ کاربران صفر می‌شود</b>")
    return lines


@botreset_router.callback_query(F.data.startswith("mbrs:ask:"))
async def reset_ask(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        _, _, sid, fstr = cb.data.split(":", 3)
        bot_id = int(sid)
    except Exception:
        await cb.answer("داده نامعتبر", show_alert=True)
        return
    flags = _parse_flags(fstr)
    if not flags:
        await cb.answer("هیچی انتخاب نکردی.", show_alert=True)
        return
    r = await _owns(bot_id, cb.from_user.id)
    if not r:
        await cb.answer("ربات یافت نشد", show_alert=True)
        return
    counts = await _counts(bot_id)
    body = "\n".join(_summary_lines(counts, flags))
    if "money" in flags:
        # === PATCH_BOTRESET_CHECKLIST_V2 ===
        # تنها گزینه‌ای که رویِ پولِ واقعیِ مشتری‌ها اثر می‌گذارد؛ همین
        # دکمه‌زدن کافی نیست — باید یوزرنیمِ ربات را هم تایپ کند.
        await state.set_state(ResetFSM.confirm_money)
        await state.update_data(bot_id=bot_id, flags=_flags_str(flags))
        await cb.message.edit_text(
            f"⚠️ <b>مطمئنی؟</b>\n\nربات: @{r.username}\n{body}\n\n"
            "این کار برگشت‌پذیر نیست و رویِ پولِ واقعیِ کاربرانت اثر می‌گذارد.\n\n"
            f"برایِ تأیید، دقیقاً همین را تایپ کن: <code>{r.username}</code>\n"
            "(یا /cancel برایِ لغو)")
        await cb.answer()
        return
    await cb.message.edit_text(
        f"⚠️ <b>مطمئنی؟</b>\n\nربات: @{r.username}\n{body}\n\nاین کار برگشت‌پذیر نیست.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [hx_button("✅ بله، ریست کن", callback_data=f"mbrs:go:{bot_id}:{_flags_str(flags)}",
                       style=ButtonStyle.DANGER)],
            [hx_button("⬅️ بی‌خیال", callback_data=f"mbrs:menu:{bot_id}", style=ButtonStyle.PRIMARY)],
        ]))
    await cb.answer()


@botreset_router.message(ResetFSM.confirm_money, F.text)
async def reset_money_confirm_text(msg: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("bot_id")
    fstr = data.get("flags", "")
    if msg.text.strip() == "/cancel":
        await state.clear()
        await msg.answer("لغو شد.")
        return
    r = await _owns(bot_id, msg.from_user.id)
    if not r:
        await state.clear()
        await msg.answer("ربات یافت نشد.")
        return
    if msg.text.strip().lstrip("@").lower() != (r.username or "").lower():
        await msg.answer(
            f"❌ درست نبود. برایِ تأیید، دقیقاً <code>{r.username}</code> را بفرست، "
            "یا /cancel برایِ لغو.")
        return
    await state.clear()
    flags = _parse_flags(fstr)
    counts = await _counts(bot_id)
    body = "\n".join(_summary_lines(counts, flags))
    await msg.answer(
        f"⚠️ <b>آخرین تأیید</b>\n\nربات: @{r.username}\n{body}\n\nاین کار برگشت‌پذیر نیست.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [hx_button("✅ بله، پاک کن", callback_data=f"mbrs:go:{bot_id}:{fstr}",
                       style=ButtonStyle.DANGER)],
            [hx_button("⬅️ بی‌خیال", callback_data=f"mbrs:menu:{bot_id}", style=ButtonStyle.PRIMARY)],
        ]))


@botreset_router.callback_query(F.data.startswith("mbrs:go:"))
async def reset_go(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    try:
        _, _, sid, fstr = cb.data.split(":", 3)
        bot_id = int(sid)
    except Exception:
        await cb.answer("داده نامعتبر", show_alert=True)
        return
    flags = _parse_flags(fstr)
    if not flags:
        await cb.answer("داده نامعتبر", show_alert=True)
        return
    r = await _owns(bot_id, cb.from_user.id)
    if not r:
        await cb.answer("ربات یافت نشد", show_alert=True)
        return
    uname = r.username
    try:
        n = await do_reset(bot_id, flags)
    except Exception as e:
        logger.exception("ریستِ ربات ناموفق بود (bot_id=%s)", bot_id)
        import html as _h
        await cb.message.answer("❌ ریست انجام نشد؛ هیچ‌چیز تغییر نکرد.\n\n"
                                f"<code>{_h.escape(str(e)[:200])}</code>")
        await cb.answer()
        return

    lines = [f"✅ رباتِ @{uname} ریست شد.", ""]
    if "btn" in flags:
        lines.append(f"🎛 دکمه‌هایِ پاک‌شده: <b>{n.get('btn', 0)}</b>")
    if "set" in flags:
        lines.append(f"⚙️ تنظیماتِ پاک‌شده: <b>{n.get('set', 0)}</b>")
    if "prod" in flags:
        lines.append(f"🛒 محصولات/پنل‌هایِ پاک‌شده: "
                     f"<b>{n.get('smm_svc', 0) + n.get('vpn_plan', 0)}</b> محصول، "
                     f"<b>{n.get('smm_panel', 0) + n.get('vpn_panel', 0)}</b> اتصالِ پنل")
    if "money" in flags:
        lines.append(f"💰 سفارش‌هایِ پاک‌شده: <b>{n.get('orders', 0) + n.get('smm_orders', 0)}</b>، "
                     f"موجودیِ صفرشده: <b>{n.get('wallets_zeroed', 0)}</b> کاربر، "
                     f"تیکت‌هایِ پاک‌شده: <b>{n.get('tickets', 0)}</b>")
    lines += ["", "الان صفحهٔ اصلیِ ربات ممکن است خالی باشد. از «🎨 بازسازی چیدمان دکمه‌ها» "
              "شروع کن.", "",
              "ℹ️ تا حدودِ ۱۰ ثانیه ممکنه رباتِ مستأجر هنوز نسخهٔ کش‌شدهٔ "
              "تنظیماتِ قبلی رو نشون بده؛ بعدش خودکار تازه می‌شه."]
    await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[hx_button("🎨 بازسازی چیدمان دکمه‌ها",
                                    callback_data=f"mbedit:{bot_id}",
                                    style=ButtonStyle.SUCCESS)],
                         [hx_button("⬅️ بازگشت به ربات", callback_data=f"mb:{bot_id}",
                                    style=ButtonStyle.PRIMARY)]]))
    await cb.answer("✅ ریست شد")
