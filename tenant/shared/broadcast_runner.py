"""broadcast_runner.py — PATCH_BROADCAST_DURABLE_V1

گزارشِ ادمین (تازیان): «پیامِ همگانی از ظهر زدم، هنوز ارسال نشده». ارسالِ همگانی یک تسکِ حافظه‌ایِ
داخلِ پروسهٔ وب بود؛ هر ری‌استارت (همان روز ۱۳ بار با اعمالِ زیپ) آن را بی‌صدا می‌کشت، ردیف برایِ
همیشه «sending» می‌ماند و نه پیامی می‌رفت نه به ادمین خبری می‌رسید.

حالا:
  • دکمهٔ «ارسال» فقط کار را (متن، مدیا، دکمه‌ها، پیامِ پیشرفت) در جدولِ broadcasts ذخیره می‌کند.
  • ورکرِ پس‌زمینه (worker_tenant) کارهایِ «sending» را برمی‌دارد، به کاربران به ترتیبِ id می‌فرستد و
    هر چند نفر جایگاه (cursor_uid) و شمارنده‌ها را ذخیره می‌کند.
  • اگر پروسه وسطِ کار بمیرد، ضربان (updated_at) کهنه می‌شود و بعد از ری‌استارت دقیقاً از نفرِ بعدی
    ادامه می‌دهد — نه از اول (پیامِ تکراری نمی‌رود) و نه گم می‌شود.
  • «🛑 لغو ارسال» مثلِ قبل با status=cancelled کار می‌کند.
  • ردیف‌هایِ قدیمیِ گیرکرده (قبل از این پچ؛ chat_id ندارند) «interrupted» می‌شوند و به مالک خبر می‌رسد.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json

from sqlalchemy import or_, select, update

from config import Session
from logging_setup import get_logger as _get_logger
from models import Bot as BotRow, BotUser, Broadcast

logger = _get_logger(__name__)

_STALE_SEC = 90          # ضربانِ کهنه‌تر از این ⇒ اجراکننده مرده است
_SAVE_EVERY = 20         # هر چند ارسال، جایگاه ذخیره شود
_HEARTBEAT_SEC = 15
_BATCH = 200
_SEND_GAP = 0.05         # ≈ ۲۰ پیام در ثانیه (زیرِ سقفِ تلگرام)
_RUNNING: set[int] = set()
_TASKS: set = set()


# === PATCH_BROADCAST_AUDIENCE_V1 === (تیکت #863) انتخابِ مخاطبِ پیامِ همگانی بر اساسِ سرویس‌هایِ VPN
AUD_ALL = ""
AUD_ANY = "svc_any"
AUD_UNLIMITED = "svc_unlimited"
AUD_NONE = "svc_none"
AUD_PANEL = "panel:"


def audience_clause(bot_id: int, audience: str | None):
    """شرطِ SQLAlchemy روی BotUser برایِ مخاطب؛ None یعنی همهٔ کاربران."""
    if not audience:
        return None
    from models import PanelAccount
    q = select(PanelAccount.user_telegram_id).where(PanelAccount.bot_id == bot_id)
    if audience == AUD_UNLIMITED:
        q = q.where(PanelAccount.volume_gb == 0, PanelAccount.volume_mb.is_(None))
    elif audience.startswith(AUD_PANEL):
        q = q.where(PanelAccount.panel_key == audience[len(AUD_PANEL):])
    elif audience not in (AUD_ANY, AUD_NONE):
        return None
    cond = BotUser.telegram_id.in_(q)
    return ~cond if audience == AUD_NONE else cond


async def audience_count(bot_id: int, audience: str | None) -> int:
    from sqlalchemy import func
    cond = audience_clause(bot_id, audience)
    q = select(func.count(BotUser.id)).where(BotUser.bot_id == bot_id, BotUser.is_banned == False)   # noqa: E712
    if cond is not None:
        q = q.where(cond)
    async with Session() as s:
        return int(await s.scalar(q) or 0)


async def audience_options(bot_id: int) -> list[tuple[str, str, int]]:
    """[(کلید، برچسب، تعدادِ مخاطب)] — فقط اگر ربات اصلاً سرویسِ VPN دارد؛ وگرنه []."""
    from models import PanelAccount
    from sqlalchemy import func
    async with Session() as s:
        n_acc = int(await s.scalar(select(func.count(PanelAccount.id)).where(PanelAccount.bot_id == bot_id)) or 0)
        panel_keys = [k for k in (await s.scalars(select(PanelAccount.panel_key).where(
            PanelAccount.bot_id == bot_id).distinct())).all() if k]
    if not n_acc:
        return []
    out = [(AUD_ALL, "👥 همهٔ کاربران", await audience_count(bot_id, AUD_ALL)),
           (AUD_ANY, "📡 فقط دارندگانِ سرویس", await audience_count(bot_id, AUD_ANY)),
           (AUD_UNLIMITED, "♾ فقط دارندگانِ سرویسِ نامحدود", await audience_count(bot_id, AUD_UNLIMITED))]
    try:
        from tenant.bots.vpn.vpn_admin import panel_label
    except Exception:   # noqa: BLE001
        panel_label = None
    for k in sorted(panel_keys):
        try:
            lbl = await panel_label(bot_id, k) if panel_label else k
        except Exception:   # noqa: BLE001
            lbl = k
        a = AUD_PANEL + k
        out.append((a, f"🛰 فقط دارندگانِ سرویسِ «{lbl}»", await audience_count(bot_id, a)))
    out.append((AUD_NONE, "🆕 بدونِ سرویس (هنوز نخریده‌اند)", await audience_count(bot_id, AUD_NONE)))
    return out


def audience_label(options: list[tuple[str, str, int]], audience: str | None) -> str:
    for k, lbl, _n in options:
        if k == (audience or ""):
            return lbl
    return "👥 همهٔ کاربران"


def _now() -> _dt.datetime:
    return _dt.datetime.utcnow()


async def _claim(bc_id: int) -> bool:
    """فقط یک اجراکننده: ضربان را به «الان» می‌برد، به شرطی که خالی یا کهنه باشد."""
    cutoff = _now() - _dt.timedelta(seconds=_STALE_SEC)
    async with Session() as s:
        res = await s.execute(update(Broadcast).where(
            Broadcast.id == bc_id, Broadcast.status == "sending",
            or_(Broadcast.updated_at.is_(None), Broadcast.updated_at < cutoff),
        ).values(updated_at=_now()))
        await s.commit()
    return (res.rowcount or 0) == 1


async def _save(bc_id: int, cursor: int, sent: int, failed: int) -> str:
    """جایگاه و شمارنده‌ها را ذخیره می‌کند؛ وضعیتِ فعلیِ ردیف را برمی‌گرداند (برایِ لغو)."""
    async with Session() as s:
        bc = await s.get(Broadcast, bc_id)
        if bc is None:
            return "missing"
        bc.cursor_uid, bc.sent_count, bc.failed_count, bc.updated_at = cursor, sent, failed, _now()
        await s.commit()
        return bc.status


def _make_bot(token: str):
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def _edit_progress(bot, bc: Broadcast, text: str, final: bool = False) -> None:
    if not bc.chat_id or not bc.msg_id:
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    if final:
        from tenant import back_inline
        kb = back_inline()
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🛑 لغو ارسال", callback_data=f"bc:stop:{bc.id}")]])
    try:
        await bot.edit_message_text(text, chat_id=bc.chat_id, message_id=bc.msg_id, reply_markup=kb)
    except Exception as e:   # «message is not modified» / پیام پاک شده — مهم نیست
        logger.debug("broadcast #%s: ویرایشِ پیامِ پیشرفت ممکن نشد: %s", bc.id, e)


async def run_broadcast(bc_id: int) -> None:
    if bc_id in _RUNNING or not await _claim(bc_id):
        return
    _RUNNING.add(bc_id)
    bot = None
    try:
        async with Session() as s:
            bc = await s.get(Broadcast, bc_id)
            row = await s.get(BotRow, bc.bot_id) if bc else None
        if not bc or not row or not row.token:
            return
        from aiogram.exceptions import TelegramRetryAfter
        from aiogram.types import InlineKeyboardMarkup
        from tenant import _BC_MEDIA_SEND, _is_blocked_error, mark_blocked
        kb = InlineKeyboardMarkup.model_validate_json(bc.kb_json) if bc.kb_json else None
        bot = _make_bot(row.token)
        cursor = int(bc.cursor_uid or 0)
        sent, failed = int(bc.sent_count or 0), int(bc.failed_count or 0)
        total = int(bc.total_count or 0)
        last_save = _now()
        since_save = 0
        status = "sending"
        resumed = cursor > 0
        if resumed:
            await _edit_progress(bot, bc, f"📢 ادامهٔ ارسالِ همگانی #{bc_id} پس از ری‌استارت… "
                                          f"({sent + failed:,} از {total:,})")
        while status == "sending":
            _aud = audience_clause(bc.bot_id, bc.audience)    # PATCH_BROADCAST_AUDIENCE_V1
            _q = select(BotUser.id, BotUser.telegram_id, BotUser.is_blocked).where(
                BotUser.bot_id == bc.bot_id, BotUser.is_banned == False,   # noqa: E712
                BotUser.id > cursor)
            if _aud is not None:
                _q = _q.where(_aud)
            async with Session() as s:
                users = (await s.execute(_q.order_by(BotUser.id).limit(_BATCH))).all()
            if not users:
                break
            for uid, tg_id, was_blocked in users:
                while True:
                    try:
                        if bc.media_id:
                            await getattr(bot, _BC_MEDIA_SEND.get(bc.media_type or "photo", "send_photo"))(
                                tg_id, bc.media_id, caption=bc.text or None, reply_markup=kb)
                        else:
                            await bot.send_message(tg_id, bc.text, reply_markup=kb)
                        sent += 1
                        if was_blocked:
                            await mark_blocked(bc.bot_id, tg_id, False)
                    except TelegramRetryAfter as e:   # محدودیتِ نرخِ تلگرام: صبر و همان نفر دوباره
                        await asyncio.sleep(float(getattr(e, "retry_after", 5) or 5) + 0.5)
                        continue
                    except Exception as e:
                        failed += 1
                        if _is_blocked_error(e):
                            await mark_blocked(bc.bot_id, tg_id, True)
                    break
                cursor = uid
                since_save += 1
                await asyncio.sleep(_SEND_GAP)
                if since_save >= _SAVE_EVERY or (_now() - last_save).total_seconds() >= _HEARTBEAT_SEC:
                    status = await _save(bc_id, cursor, sent, failed)
                    since_save, last_save = 0, _now()
                    if status != "sending":
                        break
                    if total:
                        await _edit_progress(bot, bc, f"📢 در حالِ ارسالِ همگانی #{bc_id}… "
                                                      f"{sent + failed:,} از {total:,}\n✅ {sent:,} | ❌ {failed:,}")
        status = await _save(bc_id, cursor, sent, failed)
        async with Session() as s:
            b2 = await s.get(Broadcast, bc_id)
            if b2 and b2.status == "sending":
                b2.status = "done"
                await s.commit()
        head = "🛑 ارسالِ همگانی لغو شد" if status == "cancelled" else "✅ ارسالِ همگانی تمام شد"
        await _edit_progress(bot, bc, f"{head} #{bc_id}.\nموفق: {sent:,} | ناموفق: {failed:,}", final=True)
        logger.info("broadcast #%s: %s — موفق %s، ناموفق %s", bc_id, status, sent, failed)
    except Exception:
        logger.exception("broadcast #%s: اجرا با خطا متوقف شد (بعد از %ss ادامه داده می‌شود)", bc_id, _STALE_SEC)
    finally:
        _RUNNING.discard(bc_id)
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:
                logger.debug("broadcast #%s: بستنِ نشستِ ربات ناموفق", bc_id)


def spawn(bc_id: int) -> None:
    t = asyncio.get_running_loop().create_task(run_broadcast(bc_id))
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)


async def _mark_legacy_interrupted() -> None:
    """ردیف‌هایِ «sending»ِ قبل از این پچ (chat_id ندارند) قابلِ ادامه نیستند (مدیا/دکمه ذخیره نشده بود)."""
    async with Session() as s:
        rows = (await s.scalars(select(Broadcast).where(
            Broadcast.status == "sending", Broadcast.chat_id.is_(None)))).all()
        if not rows:
            return
        notify = []
        recent = _now() - _dt.timedelta(days=2)
        for bc in rows:
            bc.status = "interrupted"
            if bc.created_at and bc.created_at >= recent:
                notify.append((bc.id, bc.bot_id))
        await s.commit()
    for bc_id, bot_id in notify:
        async with Session() as s:
            row = await s.get(BotRow, bot_id)
        if not row or not row.token:
            continue
        bot = _make_bot(row.token)
        try:
            await bot.send_message(
                row.owner_id,
                f"⚠️ ارسالِ همگانیِ #{bc_id} به‌خاطرِ ری‌استارتِ سرور نیمه‌کاره ماند "
                "(ممکن است به بخشی از کاربران رسیده باشد).\n\n"
                "این مشکل رفع شده و از این به بعد ارسالِ همگانی بعد از ری‌استارت خودش ادامه پیدا می‌کند. "
                "لطفاً پیامت را یک‌بار دیگر از «📣 ارسال پیام همگانی» بفرست.")
        except Exception as e:
            logger.info("broadcast #%s: اطلاعِ نیمه‌کاره‌ماندن به مالک نرسید: %s", bc_id, e)
        finally:
            await bot.session.close()


async def broadcast_loop(interval: float = 10.0) -> None:
    """ورکر: کارهایِ «sending» که اجراکننده ندارند (تازه یا مرده) را اجرا/ادامه می‌دهد."""
    await asyncio.sleep(5)
    try:
        await _mark_legacy_interrupted()
    except Exception:
        logger.exception("broadcast: علامت‌زدنِ ارسال‌هایِ قدیمیِ گیرکرده ناموفق بود")
    while True:
        try:
            cutoff = _now() - _dt.timedelta(seconds=_STALE_SEC)
            async with Session() as s:
                ids = (await s.scalars(select(Broadcast.id).where(
                    Broadcast.status == "sending", Broadcast.chat_id.is_not(None),
                    or_(Broadcast.updated_at.is_(None), Broadcast.updated_at < cutoff),
                ).order_by(Broadcast.id))).all()
            for bc_id in ids:
                if bc_id not in _RUNNING:
                    spawn(bc_id)
        except Exception:
            logger.exception("broadcast_loop: دورِ بررسی ناموفق بود")
        await asyncio.sleep(interval)

