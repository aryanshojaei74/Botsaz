"""شرط‌بندی ایکس‌او / سنگ‌کاغذ‌قیچی / تاس در گروه ربات‌های مستأجر (کیف‌پول همان ربات).

=== PATCH_WAGER_TENANT_V1 ===
• فعال‌سازی: مالک ربات از ربات اصلی (ربات‌های من ← انتخاب ربات ← «بازی‌های شرط‌بندی») مبلغی که مدیر بات‌ساز تعیین
  کرده را می‌پردازد؛ ردیف BotAddon ساخته می‌شود. بدون آن، این بازی‌ها در آن ربات کار نمی‌کنند (و فقط به مالک/ادمین
  توضیح داده می‌شود). شیر یا خط قدیمی (group_betting.py) از این کنترل جداست و دست‌نخورده.
• فقط برای ربات‌هایی که قابلیت‌های گروهی کیف‌پولی دارند (خدمات مجازی / VPN رشد و حرفه‌ای)، مثل انتقال و شیر یا خط.
• منطق بازی و مالی همان هستهٔ platform_wager است (رزرو و تسویهٔ اتمیک، مساوی ⇒ برگشت هر دو، مهلت حرکت ⇒ باخت،
  تاس را خود تلگرام می‌اندازد)، فقط با کیف‌پول BotUser و دفتر WalletTxn همین ربات و callbackهای twg:*.
• کنترل مالک در «تنظیمات بازی» (همان صفحهٔ شیر یا خط): کلید اصلی، کارمزد، حداقل/حداکثر، زمان انتظار + روشن/خاموش هر بازی.
"""
from __future__ import annotations

import asyncio

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

import platform_wager as core
from bot_addons import WAGER_ADDON, addon_active, wager_eligible
from config import Session
from logging_setup import get_logger as _get_logger
from models import Bot as BotRow, PlatformWager
from platform_group_money import _int, _reply_target

logger = _get_logger(__name__)

wager_games_router = Router(name="wager_games")

ANON_NOTICE = ("❌ با حالت «ادمین ناشناس» یا هویت کانال نمی‌شود شرط بست (معلوم نیست کیف‌پول چه کسی باید کم شود). "
               "حالت ناشناس را خاموش کن و دوباره بنویس.")
LOCKED_HINT = ("🔒 بازی‌های شرط‌بندی (ایکس‌او، سنگ‌کاغذ‌قیچی، تاس) برای این ربات فعال نشده است.\n"
               "فعال‌سازی: ربات اصلی بات‌ساز ← ربات‌های من ← همین ربات ← «🎰 بازی‌های شرط‌بندی».\n\n"
               "(این پیام فقط برای مالک/ادمین ربات نمایش داده می‌شود.)")


async def _row_and_admin(bot: Bot, uid: int):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row:
        return None, False
    return row, await is_owner_or_admin(row.id, uid, row.owner_id)


def _tuple(u) -> tuple:
    return (u.id, u.full_name or "", u.username or "")


@wager_games_router.callback_query(F.data == "twg:noop")
async def tw_noop(cb: CallbackQuery):
    await cb.answer()


def _filter(m: Message) -> bool:
    return core.parse_command(m.text) is not None


@wager_games_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_filter))
async def tw_create(msg: Message, bot: Bot):
    if not msg.from_user:
        raise SkipHandler
    row, is_admin = await _row_and_admin(bot, msg.from_user.id)
    if not row or not wager_eligible(row.field, row.plan):
        raise SkipHandler                       # ربات از این قابلیت پشتیبانی نمی‌کند ⇒ بقیهٔ هندلرها
    if not await addon_active(row.id, WAGER_ADDON):
        if is_admin:
            await msg.reply(LOCKED_HINT)
            return
        raise SkipHandler
    if msg.sender_chat:
        await msg.reply(ANON_NOTICE)
        return
    game, amount = core.parse_command(msg.text)
    try:
        await _create_and_post(msg, bot, row, game, amount)
    except SkipHandler:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("tenant wager: create failed (bot=%s chat=%s user=%s)", row.id, msg.chat.id, msg.from_user.id)
        extra = f"\n({type(e).__name__}: {str(e)[:150]})" if is_admin else ""
        try:
            await msg.reply("❌ ساخت شرط‌بندی با خطا روبه‌رو شد؛ چند لحظه بعد دوباره امتحان کن." + extra)
        except Exception:  # noqa: BLE001
            logger.exception("tenant wager: could not report the failure")


async def _create_and_post(msg: Message, bot: Bot, row, game: str, amount: int) -> None:
    from tenant import get_or_create_user
    if not await core.master_enabled(row.id):
        await msg.reply("❌ قابلیت بازی در این ربات فعال نیست.")
        return
    if not await core.game_enabled(game, row.id):
        await msg.reply(f"❌ شرط‌بندی {core.GAME_FA[game]} در این ربات فعال نیست.")
        return
    target = _reply_target(msg)
    if target and (target.is_bot or target.id == msg.from_user.id):
        await msg.reply("❌ سازنده نباید خودش یا یک ربات را هدف بگیرد.")
        return
    await core.sweep_expired(bot, row.id)
    cfg = await core.load_cfg(row.id)
    await get_or_create_user(row.id, msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or "")
    w, err = await core.create_wager(
        game, msg.chat.id, _tuple(msg.from_user), amount, _tuple(target) if target else None, cfg, bot_id=row.id)
    if err:
        await msg.reply(err)
        return
    text, kb = core.render(w)
    try:
        sent = await msg.reply(text, reply_markup=kb, parse_mode="HTML")
    except Exception:  # noqa: BLE001 — پیام نرفت: مبلغ را برگردان
        logger.exception("tenant wager #%s: sending the message failed; refunding", w.id)
        async with core._wlock(w.id):
            await core.cancel_wager(w.id, w.creator_id, row.id)
        raise
    async with Session() as s:
        r = await s.get(PlatformWager, w.id)
        r.message_id = sent.message_id
        await s.commit()


def _wid(cb: CallbackQuery, idx: int = 2) -> int:
    parts = (cb.data or "").split(":")
    return _int(parts[idx]) if len(parts) > idx else 0


async def _show(cb: CallbackQuery, w: PlatformWager) -> None:
    text, kb = core.render(w)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("tenant wager edit failed: %s", e)


async def _cb_row(cb: CallbackQuery, bot: Bot):
    """ردیف ربات؛ در هر اقدامی (حتی حرکت) فقط لازم است ربات پشتیبانی کند و پیام در گروه باشد."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or not cb.message or cb.message.chat.type not in ("group", "supergroup"):
        await cb.answer("قابلیت فعال نیست.", show_alert=True)
        return None
    return row


@wager_games_router.callback_query(F.data.startswith("twg:join:"))
async def tw_join(cb: CallbackQuery, bot: Bot):
    from tenant import get_or_create_user
    row = await _cb_row(cb, bot)
    if not row:
        return
    if not wager_eligible(row.field, row.plan) or not await addon_active(row.id, WAGER_ADDON) \
            or not await core.master_enabled(row.id):
        await cb.answer("قابلیت فعال نیست.", show_alert=True)
        return
    wid = _wid(cb)
    cfg = await core.load_cfg(row.id)
    await get_or_create_user(row.id, cb.from_user.id, cb.from_user.full_name or "", cb.from_user.username or "")
    async with core._wlock(wid):
        peek = await _peek(wid)
        if peek is not None and peek.bot_id == row.id and not await core.game_enabled(peek.game, row.id):
            await cb.answer("این بازی فعال نیست.", show_alert=True)
            return
        r = await core.join_wager(wid, _tuple(cb.from_user), cfg["move"], bot_id=row.id)
    if r.w is not None:
        await _show(cb, r.w)
    await cb.answer(r.err or "به شرط‌بندی پیوستی؛ موفق باشی!", show_alert=bool(r.err))


async def _peek(wid: int):
    async with Session() as s:
        return await s.get(PlatformWager, wid)


@wager_games_router.callback_query(F.data.startswith("twg:cancel:"))
async def tw_cancel(cb: CallbackQuery, bot: Bot):
    row = await _cb_row(cb, bot)
    if not row:
        return
    wid = _wid(cb)
    async with core._wlock(wid):
        r = await core.cancel_wager(wid, cb.from_user.id, row.id)
    if r.w is not None:
        await _show(cb, r.w)
    await cb.answer(r.err or "لغو شد", show_alert=bool(r.err))


@wager_games_router.callback_query(F.data.startswith("twg:x:"))
async def tw_xo(cb: CallbackQuery, bot: Bot):
    row = await _cb_row(cb, bot)
    if not row:
        return
    parts = (cb.data or "").split(":")
    wid, cell = _wid(cb), (int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1)
    cfg = await core.load_cfg(row.id)
    async with core._wlock(wid):
        r = await core.xo_move(wid, cb.from_user.id, cell, cfg["move"], bot_id=row.id)
    if r.w is not None:
        await _show(cb, r.w)
    await cb.answer(r.err or "", show_alert=bool(r.err))


@wager_games_router.callback_query(F.data.startswith("twg:r:"))
async def tw_rps(cb: CallbackQuery, bot: Bot):
    row = await _cb_row(cb, bot)
    if not row:
        return
    parts = (cb.data or "").split(":")
    wid, choice = _wid(cb), (parts[3] if len(parts) > 3 else "")
    async with core._wlock(wid):
        r = await core.rps_pick(wid, cb.from_user.id, choice, bot_id=row.id)
    if r.w is not None:
        await _show(cb, r.w)
    await cb.answer(r.err or r.note or "", show_alert=bool(r.err))


@wager_games_router.callback_query(F.data.startswith("twg:d:"))
async def tw_dice(cb: CallbackQuery, bot: Bot):
    row = await _cb_row(cb, bot)
    if not row:
        return
    wid, uid = _wid(cb), cb.from_user.id
    async with core._wlock(wid):
        r = await core.dice_claim(wid, uid, bot_id=row.id)
    if r.err:
        if r.w is not None:
            await _show(cb, r.w)
        await cb.answer(r.err, show_alert=True)
        return
    await cb.answer("🎲")
    try:
        sent = await bot.send_dice(cb.message.chat.id, emoji="🎲")
        value = int(sent.dice.value)
    except Exception:  # noqa: BLE001 — پرتاب نشد: دوباره اجازه بده
        logger.exception("tenant wager #%s: send_dice failed", wid)
        await core.dice_release(wid, uid, bot_id=row.id)
        await cb.message.answer("❌ پرتاب تاس انجام نشد؛ دوباره امتحان کن.")
        return
    async with core._wlock(wid):
        r = await core.dice_record(wid, uid, value, bot_id=row.id)
    if r.w is not None:
        if r.w.status != "playing":
            await asyncio.sleep(4)            # بگذار انیمیشن تاس تمام شود
        await _show(cb, r.w)


# ───────────────────────── کنترل مالک (داخل «تنظیمات بازی») ─────────────────────────

async def settings_extra(bot_id: int) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """متن و ردیف‌های اضافه برای صفحهٔ «تنظیمات بازی»: وضعیت فعال‌سازی و روشن/خاموش هر بازی."""
    if not await addon_active(bot_id, WAGER_ADDON):
        return ("\n\n🔒 <b>بازی‌های شرط‌بندی</b> (ایکس‌او، سنگ‌کاغذ‌قیچی، تاس): فعال نشده.\n"
                "فعال‌سازی: ربات اصلی بات‌ساز ← ربات‌های من ← این ربات ← «🎰 بازی‌های شرط‌بندی»."), []
    flags = {g: await core.game_enabled(g, bot_id) for g in core.GAMES}
    text = "\n\n🎮 <b>بازی‌های شرط‌بندی</b> (با کلید «وضعیت» بالا روشن/خاموش می‌شوند):\n" + "\n".join(
        f"{'✅' if flags[g] else '❌'} {core.GAME_FA[g]}" for g in core.GAMES)
    rows = [[InlineKeyboardButton(text=f"{'🔴 خاموش' if flags[g] else '🟢 روشن'}: {core.GAME_FA[g]}",
                                  callback_data=f"twg:set:{g}") for g in core.GAMES]]
    return text, rows


@wager_games_router.callback_query(F.data.startswith("twg:set:"))
async def tw_game_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    from tenant.shared.group_betting import _bet_settings_view
    row, is_admin = await _row_and_admin(bot, cb.from_user.id)
    if not row or not is_admin:
        await cb.answer()
        return
    game = (cb.data or "").split(":", 2)[2]
    if game not in core.GAMES or not await addon_active(row.id, WAGER_ADDON):
        await cb.answer()
        return
    cur = await core.game_enabled(game, row.id)
    await set_setting(row.id, f"wager_game_{game}", "off" if cur else "on")
    text, kb = await _bet_settings_view(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await cb.answer("✅ ذخیره شد")


# ───────────────────────── تسویهٔ شرط‌های منقضی (حلقهٔ worker) ─────────────────────────

async def expire_wagers_once() -> int:
    """شرط‌های باز و گذشته از مهلت همهٔ ربات‌های مستأجر را تسویه/برگشت می‌دهد و پیامشان را به‌روز می‌کند."""
    from datetime import datetime
    async with Session() as s:
        rows = (await s.execute(select(PlatformWager.id, PlatformWager.bot_id).where(
            PlatformWager.bot_id != 0, PlatformWager.status.in_(core.ACTIVE),
            PlatformWager.expires_at.is_not(None), PlatformWager.expires_at <= datetime.utcnow()).limit(200))).all()
    if not rows:
        return 0
    done: list[PlatformWager] = []
    for wid, _bot_id in rows:
        try:
            async with core._wlock(wid):
                w = await core.expire_one(wid)
            if w is not None:
                done.append(w)
        except Exception:  # noqa: BLE001
            logger.exception("tenant wager sweep #%s failed", wid)
    if not done:
        return 0
    async with Session() as s:
        bot_rows = {b.id: b for b in (await s.scalars(select(BotRow).where(BotRow.id.in_({w.bot_id for w in done})))).all()}
    bots: dict[int, Bot] = {}
    try:
        for w in done:
            b_row = bot_rows.get(w.bot_id)
            if not b_row or not b_row.token or not w.message_id:
                continue
            bot = bots.get(w.bot_id) or bots.setdefault(w.bot_id, Bot(token=b_row.token))
            text, kb = core.render(w)
            try:
                await bot.edit_message_text(text, chat_id=w.chat_id, message_id=w.message_id,
                                            reply_markup=kb, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001 — پیام پاک شده/بدون تغییر؛ مبلغ‌ها برگشته‌اند
                logger.info("tenant wager #%s: final edit skipped: %s", w.id, e)
    finally:
        for b in bots.values():
            await b.session.close()
    return len(done)


async def wager_expiry_loop(interval_sec: int = 20):
    """loop مستقل worker برای تعیین‌تکلیف خودکار شرط‌های منقضی‌شدهٔ ربات‌های مستأجر."""
    await asyncio.sleep(25)
    while True:
        try:
            n = await expire_wagers_once()
            if n:
                logger.info("wager_expiry_loop: %s شرط تسویه/برگشت شد", n)
        except Exception:  # noqa: BLE001
            logger.exception("wager_expiry_loop: اجرای دور ناموفق بود")
        await asyncio.sleep(interval_sec)
