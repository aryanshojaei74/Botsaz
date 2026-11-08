"""
svc_delete_report.py — «گزارشِ حذفِ سرویس» (VPN)

وقتی مشتری سرویسش را از «سرویس‌های من» حذف می‌کند، آخرین اطلاعاتِ آن سرویس (کاربر، نام/کد، لینک، حجم و مدت،
مصرف، انقضا، پنل) در کانال/گروهی که مالکِ ربات وصل کرده پست می‌شود، تا بعداً قابلِ پیگیری باشد.

کلیدهایِ تنظیمات (به‌ازایِ هر ربات):
  svc_del_channel  آیدیِ عددیِ کانال/گروه
  svc_del_link     "1" (پیش‌فرض) = لینکِ سرویس هم در پست باشد
  svc_del_admins   "1" = همین گزارش به‌صورتِ پیام در ربات برایِ مالک/ادمین‌ها هم بیاید
"""
from __future__ import annotations

import html as _html
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

svc_delete_report_router = Router()
_MAX_LINK = 900
_GB = 1024 ** 3


class SvcDelRepFSM(StatesGroup):
    wait = State()


def _tehran(dt: datetime | None = None) -> str:
    t = (dt or datetime.utcnow()) + timedelta(hours=3, minutes=30)
    try:
        import jdatetime as _jdt
        d = _jdt.datetime.fromgregorian(datetime=t).strftime("%Y/%m/%d")
    except Exception:
        d = t.strftime("%Y/%m/%d")
    return f"{d} {t.strftime('%H:%M')}"


def build_report(*, user_id, username, full_name, email, svc_code, title, volume, duration, panel,
                 used_gb=None, total_gb=None, expires_at=None, link="", created_at=None, paid=None,
                 show_link=True, ident_mode: str = "both") -> str:
    """متنِ HTMLِ گزارش (تابعِ خالص؛ قابلِ تست)."""
    lines = ["#حذف_سرویس", ""]
    if show_link and link:
        lk = str(link).strip()
        if len(lk) > _MAX_LINK:
            lk = lk[:_MAX_LINK] + "…"
        lines += ["🔗 لینک:", f"<code>{_html.escape(lk)}</code>", ""]
    if title:
        lines.append(f"📛 {_html.escape(str(title))}")
    lines.append(f"📧 نام فنی: <code>{_html.escape(str(email))}</code>")
    if svc_code:
        lines.append(f"🔢 کد سرویس: <code>{_html.escape(str(svc_code))}</code>")
    lines.append(f"⏱ {_html.escape(str(duration))} | 📦 {_html.escape(str(volume))}")
    if used_gb is not None:
        cap = f" از {total_gb:g} گیگ" if total_gb else " (نامحدود)"
        lines.append(f"📊 مصرف: {used_gb:g} گیگ{cap}")
    if expires_at:
        lines.append(f"⏳ انقضا: {_tehran(expires_at)}")
    if created_at:
        lines.append(f"🛒 خرید: {_tehran(created_at)}")
    if paid:
        lines.append(f"💰 پرداخت: {int(paid):,} تومان")
    if panel:
        lines.append(f"🚀 پنل: {_html.escape(str(panel))}")
    from tenant.shared.buyer_ident import normalize as _norm
    mode = _norm(ident_mode)
    if mode == "id" or (mode == "username" and not username):
        lines.append(f"🆔 id: <code>{user_id}</code>")
    elif mode == "username":
        lines.append(f"👤 {_html.escape('@' + str(username).lstrip('@'))}")
    else:
        lines.append(f"🆔 id: <code>{user_id}</code>")
        lines.append(f"👤 {_html.escape(full_name or '—')} | {_html.escape('@' + username if username else '—')}")
    lines.append(f"🕒 {_tehran()}")
    return "\n".join(lines)


async def report(bot, bot_id: int, acc, *, username=None, full_name=None, usage: dict | None = None,
                 panel_name: str | None = None, title: str = "", volume: str = "", duration: str = "") -> None:
    """پستِ گزارش؛ هر خطایی فقط لاگ می‌شود و هرگز جلوی حذفِ کاربر را نمی‌گیرد."""
    try:
        from tenant import get_setting
        ch = (await get_setting(bot_id, "svc_del_channel", "") or "").strip()
        to_admins = (await get_setting(bot_id, "svc_del_admins", "0")) == "1"
        if not ch and not to_admins:
            return
        show_link = (await get_setting(bot_id, "svc_del_link", "1")) != "0"
        used_gb = total_gb = None
        exp = getattr(acc, "expires_at", None)
        if usage:
            used_gb = round((int(usage.get("up", 0) or 0) + int(usage.get("down", 0) or 0)) / _GB, 2)
            cap = int(usage.get("total", 0) or 0)
            total_gb = round(cap / _GB, 2) if cap else 0
            ms = int(usage.get("expiryTime", 0) or 0)
            if ms > 0:
                try:
                    exp = datetime.utcfromtimestamp(ms / 1000)
                except (OverflowError, OSError, ValueError):
                    pass
        from tenant.shared.buyer_ident import get_mode
        text = build_report(
            user_id=acc.user_telegram_id, username=username, full_name=full_name, email=acc.email,
            svc_code=getattr(acc, "svc_code", None), title=title, volume=volume, duration=duration,
            panel=panel_name, used_gb=used_gb, total_gb=total_gb, expires_at=exp,
            link=getattr(acc, "sub_link", "") or "", created_at=getattr(acc, "created_at", None),
            paid=getattr(acc, "paid_toman", None), show_link=show_link, ident_mode=await get_mode(bot_id))
        if ch:
            try:
                chat_id = int(ch)
            except ValueError:
                chat_id = ch
            try:
                await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
            except Exception as e:
                logger.warning("svc_delete_report: ارسال به کانال ناموفق (bot_id=%s): %s", bot_id, e)
        if to_admins:
            try:
                import tenant as _t
                async with _t.Session() as s:
                    brow = await s.get(_t.BotRow, bot_id)
                if brow is not None:
                    await _t._notify_admins(bot, brow, text)
            except Exception as e:
                logger.warning("svc_delete_report: ارسال به ادمین‌ها ناموفق (bot_id=%s): %s", bot_id, e)
    except Exception as e:
        logger.warning("svc_delete_report: گزارش ناموفق بود (bot_id=%s): %s", bot_id, e)


# ───────────────────────── پنلِ مالک ─────────────────────────
async def _content(bot_id: int):
    from tenant import get_setting
    ch = await get_setting(bot_id, "svc_del_channel", "")
    show = (await get_setting(bot_id, "svc_del_link", "1")) != "0"
    adm = (await get_setting(bot_id, "svc_del_admins", "0")) == "1"
    txt = ("🗑 <b>گزارشِ حذفِ سرویس</b>\n\n"
           "هر بار مشتری سرویسی را از «سرویس‌های من» حذف کند، آخرین اطلاعاتش در کانال/گروهِ شما پست می‌شود: "
           "کاربر، نام و کدِ سرویس، حجم/مدت، مصرف، انقضا، پنل و (اختیاری) لینک.\n\n")
    if ch:
        txt += f"وضعیت: ✅ متصل به <code>{_html.escape(str(ch))}</code>"
    else:
        txt += ("وضعیت: ❌ تنظیم نشده\n\nربات را در کانال/گروه <b>ادمین</b> کن، بعد یک پیام از آن‌جا "
                "این‌جا فوروارد کن (یا آیدیِ عددیِ کانال/گروه را بفرست).")
    txt += "\n\n📩 پیامِ داخلِ ربات به ادمین‌ها: " + ("✅ روشن" if adm else "❌ خاموش")
    rows = [[InlineKeyboardButton(text=f"📩 پیام به ادمین‌ها در ربات: {'✅' if adm else '❌'}",
                                  callback_data="sdrep:adm", style=ButtonStyle.PRIMARY)]]
    if ch or adm:
        rows.append([InlineKeyboardButton(text="🧪 ارسالِ گزارشِ نمونه", callback_data="sdrep:test", style=ButtonStyle.SUCCESS)])
        rows.append([InlineKeyboardButton(text=f"🔗 نمایشِ لینکِ سرویس در گزارش: {'✅' if show else '❌'}",
                                          callback_data="sdrep:link", style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(text="🗑 حذفِ اتصال", callback_data="sdrep:clear", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="🔗 اتصال / تغییرِ کانال", callback_data="sdrep:set", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _gate(cb_or_msg, bot: Bot):
    from tenant import _owner_ok, _owner_ok_msg
    is_msg = isinstance(cb_or_msg, Message)
    row = await (_owner_ok_msg(cb_or_msg, bot) if is_msg else _owner_ok(cb_or_msg, bot))
    if not row:
        return None
    if getattr(row, "field", None) != "vpn_sales":
        t = "این قابلیت فقط برایِ رباتِ فروشِ VPN است."
        if is_msg:
            await cb_or_msg.answer(t)
        else:
            await cb_or_msg.answer(t, show_alert=True)
        return None
    return row


async def _show(target, txt, kb):
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(txt, reply_markup=kb, parse_mode="HTML")


@svc_delete_report_router.callback_query(F.data == "set:sdrep")
async def sdrep_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _gate(cb, bot)
    if not row:
        return
    await state.clear()
    txt, kb = await _content(row.id)
    await _show(cb, txt, kb)
    await cb.answer()


@svc_delete_report_router.callback_query(F.data == "sdrep:set")
async def sdrep_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import smart_cancel_kb
    row = await _gate(cb, bot)
    if not row:
        return
    await state.set_state(SvcDelRepFSM.wait)
    await cb.message.answer(
        "یک پیام از کانال/گروهِ موردنظر را فوروارد کن، یا آیدیِ عددیِ آن (مثلاً <code>-1001234567890</code>) را بفرست.\n\n"
        "⚠️ ربات باید آن‌جا ادمین باشد.", reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@svc_delete_report_router.message(SvcDelRepFSM.wait)
async def sdrep_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _gate(msg, bot)
    if not row:
        return
    chat_id = None
    fwd = getattr(msg, "forward_from_chat", None)
    if fwd is not None and getattr(fwd, "type", "") in ("channel", "supergroup", "group"):
        chat_id = fwd.id
    else:
        t = (msg.text or "").strip()
        if t.lstrip("-").isdigit():
            chat_id = int(t)
    if chat_id is None:
        return await msg.answer("یک پیام از کانال فوروارد کن یا آیدیِ عددیِ کانال/گروه را بفرست.")
    try:
        m = await bot.send_message(chat_id, "✅ کانالِ گزارشِ حذفِ سرویس وصل شد.")
        try:
            await bot.unpin_chat_message(chat_id, message_id=m.message_id)
        except Exception:
            logger.debug("svc_delete_report: unpin لازم نبود")
    except Exception as e:
        return await msg.answer("❌ ارسال به این کانال ممکن نشد. مطمئن شو ربات آن‌جا <b>ادمین</b> است.\n\n"
                                f"<code>{_html.escape(str(e))}</code>", parse_mode="HTML")
    await set_setting(row.id, "svc_del_channel", str(chat_id))
    await state.clear()
    txt, kb = await _content(row.id)
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(txt, reply_markup=kb, parse_mode="HTML")


async def _toggle(cb: CallbackQuery, bot: Bot, key: str, default: str, on_note: str, off_note: str):
    from tenant import get_setting, set_setting
    row = await _gate(cb, bot)
    if not row:
        return
    cur = (await get_setting(row.id, key, default)) != "0" if default == "1" else (await get_setting(row.id, key, default)) == "1"
    await set_setting(row.id, key, "0" if cur else "1")
    txt, kb = await _content(row.id)
    await _show(cb, txt, kb)
    await cb.answer(off_note if cur else on_note)


@svc_delete_report_router.callback_query(F.data == "sdrep:link")
async def sdrep_link(cb: CallbackQuery, bot: Bot):
    await _toggle(cb, bot, "svc_del_link", "1", "با لینک", "بدون لینک")


@svc_delete_report_router.callback_query(F.data == "sdrep:adm")
async def sdrep_adm(cb: CallbackQuery, bot: Bot):
    await _toggle(cb, bot, "svc_del_admins", "0", "از این پس هر حذف برایِ ادمین‌ها پیام می‌شود", "پیام به ادمین‌ها خاموش شد")


@svc_delete_report_router.callback_query(F.data == "sdrep:test")
async def sdrep_test(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting
    row = await _gate(cb, bot)
    if not row:
        return
    if not await get_setting(row.id, "svc_del_channel", "") and await get_setting(row.id, "svc_del_admins", "0") != "1":
        return await cb.answer("کانالی تنظیم نشده و پیام به ادمین هم خاموش است.", show_alert=True)

    class _A:
        user_telegram_id = cb.from_user.id
        email = "sample123"
        svc_code = "123456789"
        sub_link = "https://example.com/sub/SAMPLE"
        expires_at = datetime.utcnow() + timedelta(days=12)
        created_at = datetime.utcnow() - timedelta(days=18)
        paid_toman = 150000
    await report(bot, row.id, _A(), username=cb.from_user.username, full_name=cb.from_user.full_name,
                 usage={"up": 2 * _GB, "down": 7 * _GB, "total": 30 * _GB}, panel_name="پنل نمونه",
                 title="سرویس نمونه", volume="30 گیگ", duration="30 روز")
    await cb.answer("گزارشِ نمونه ارسال شد.", show_alert=True)


@svc_delete_report_router.callback_query(F.data == "sdrep:clear")
async def sdrep_clear(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    row = await _gate(cb, bot)
    if not row:
        return
    await set_setting(row.id, "svc_del_channel", "")
    txt, kb = await _content(row.id)
    await _show(cb, txt, kb)
    await cb.answer("اتصال حذف شد.")
