"""مسابقه‌هایِ پولیِ دونفرهٔ «حدس عدد / سرعت / ریاضی» در گروهِ ربات‌هایِ مستأجر (کیف‌پولِ همان ربات).

=== PATCH_WAGER_RACE_TENANT_V1 ===
گزارشِ فدایی: بازی‌هایِ گروهِ بات‌ساز برایِ ربات‌هایِ مشتری هم بیاید، ولی «شیرینی» نه — با پولِ کیف‌پولِ همان ربات، مثلِ بقیهٔ
بازی‌ها («سرعت 1000»).
• منطقِ بازی/پیام همان platform_wager_race است (ساخت/پیوستن/تسویهٔ اتمیکِ هستهٔ platform_wager با bot_id)، فقط callbackها twr:*
  و کیف‌پول BotUser/WalletTxn همین ربات.
• همان قفلِ addonِ «بازی‌هایِ گروهی» و کلیدِ اصلیِ «تنظیمات بازی» لازم است؛ هر مسابقه کلیدِ جدا دارد (پیش‌فرض روشن) که مالک
  از «تنظیمات بازی» خاموش می‌کند.
• چند مسابقه هم‌زمان در یک گروه مجاز است؛ هر نفر هم‌زمان فقط در یک مسابقهٔ زنده بازی می‌کند.
• بازیِ رایگان/شیرینی برایِ ربات‌هایِ مشتری وجود ندارد.
"""
from __future__ import annotations

import asyncio
import html as _html
import re
import time

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

import platform_wager as core
import platform_wager_race as R
from bot_addons import WAGER_ADDON, addon_active, wager_eligible
from config import Session
from logging_setup import get_logger as _get_logger
from models import PlatformWager
from platform_group_money import _int, _reply_target, _rial
from tenant.shared.wager_games import ANON_NOTICE, LOCKED_HINT, _row_and_admin, _tuple

logger = _get_logger(__name__)

wager_race_router = Router(name="wager_race")

_NO_AMOUNT = re.compile(r"^(?:شرط\s*بندی|بازی)\s+(حدس\s*عدد|سرعت|ریاضی|الگو)$")


def _is_create(m: Message) -> bool:
    return R.parse_race_command(m.text) is not None


def _is_hint(m: Message) -> bool:
    return bool(m.text and _NO_AMOUNT.match(core._norm(m.text)))


async def _gate(msg: Message, bot: Bot, game: str | None = None):
    """(row, is_admin) اگر این ربات اجازهٔ مسابقه در این گروه را دارد؛ وگرنه None یا SkipHandler/پیامِ مناسب."""
    if not msg.from_user or msg.from_user.is_bot:
        raise SkipHandler
    if game and game not in R.TENANT_RACE_GAMES:
        raise SkipHandler
    row, is_admin = await _row_and_admin(bot, msg.from_user.id)
    if not row or not wager_eligible(row.field, row.plan):
        raise SkipHandler
    if not await addon_active(row.id, WAGER_ADDON):
        if is_admin:
            await msg.reply(LOCKED_HINT)
            return None, is_admin
        raise SkipHandler
    if msg.sender_chat:
        await msg.reply(ANON_NOTICE)
        return None, is_admin
    if not await core.master_enabled(row.id):
        await msg.reply("❌ قابلیت بازی در این ربات فعال نیست.")
        return None, is_admin
    if game and not await core.game_enabled(game, row.id):
        await msg.reply(f"❌ بازی {core.GAME_FA[game]} در این ربات فعال نیست.")
        return None, is_admin
    return row, is_admin


@wager_race_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_hint))
async def twr_hint(msg: Message, bot: Bot):
    """«بازی سرعت» بدونِ مبلغ ⇒ مبلغ را می‌خواهد."""
    row, _adm = await _gate(msg, bot)
    if not row:
        return
    game = {"حدسعدد": "حدس عدد", "سرعت": "سرعت", "ریاضی": "ریاضی", "الگو": "الگو"}[
        re.sub(r"\s+", "", _NO_AMOUNT.match(core._norm(msg.text)).group(1))]
    cfg = await core.load_cfg(row.id)
    ex = core.example_amount(cfg)
    lim = (f"از {_rial(cfg['min'])} تا {_rial(cfg['max'])}" if cfg.get("max") else f"حداقل {_rial(cfg['min'])}")
    await msg.reply(f"💰 برایِ این بازی مبلغ را هم بنویس (به تومان، {lim}):\n<code>{game} {ex}</code>", parse_mode="HTML")


@wager_race_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_create))
async def twr_create(msg: Message, bot: Bot):
    from tenant import get_or_create_user
    game, amount = R.parse_race_command(msg.text)
    row, is_admin = await _gate(msg, bot, game)
    if not row:
        return
    target = _reply_target(msg)
    if target and (target.is_bot or target.id == msg.from_user.id):
        await msg.reply("❌ سازنده نباید خودش یا یک ربات را هدف بگیرد.")
        return
    try:
        await core.sweep_expired(bot, row.id)
        cfg = await core.load_cfg(row.id)
        await get_or_create_user(row.id, msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or "")
        w, err = await core.create_wager(game, msg.chat.id, _tuple(msg.from_user), amount,
                                         _tuple(target) if target else None, cfg, bot_id=row.id)
        if err:
            await msg.reply(err)
            return
        R.LIVE[w.id] = {"wid": w.id, "chat": msg.chat.id, "bot": row.id, "kind": game, "players": set(),
                        "answer": None, "n": None, "until": time.monotonic() + cfg["ttl"] + 5}
        text, kb = R.render_race(w)
        try:
            sent = await msg.reply(text, reply_markup=kb, parse_mode="HTML")
        except Exception:  # noqa: BLE001 — پیام نرفت: مبلغ را برگردان
            logger.exception("tenant race #%s: sending the message failed; refunding", w.id)
            R.LIVE.pop(w.id, None)
            async with core._wlock(w.id):
                await core.cancel_wager(w.id, w.creator_id, row.id)
            raise
        async with Session() as s:
            r = await s.get(PlatformWager, w.id)
            r.message_id = sent.message_id
            await s.commit()
    except SkipHandler:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("tenant race: create failed (bot=%s chat=%s user=%s)", row.id, msg.chat.id, msg.from_user.id)
        extra = f"\n({type(e).__name__}: {str(e)[:150]})" if is_admin else ""
        try:
            await msg.reply("❌ ساخت بازی با خطا روبه‌رو شد؛ چند لحظه بعد دوباره امتحان کن." + extra)
        except Exception:  # noqa: BLE001
            logger.exception("tenant race: could not report the failure")


async def _cb_row(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if (not row or not cb.message or cb.message.chat.type not in ("group", "supergroup")
            or not wager_eligible(row.field, row.plan) or not await addon_active(row.id, WAGER_ADDON)
            or not await core.master_enabled(row.id)):
        await cb.answer("قابلیت فعال نیست.", show_alert=True)
        return None
    return row


def _wid(cb: CallbackQuery) -> int:
    parts = (cb.data or "").split(":")
    return _int(parts[2]) if len(parts) > 2 else 0


@wager_race_router.callback_query(F.data.startswith("twr:join:"))
async def twr_join(cb: CallbackQuery, bot: Bot):
    from tenant import get_or_create_user
    row = await _cb_row(cb, bot)
    if not row:
        return
    wid = _wid(cb)
    await get_or_create_user(row.id, cb.from_user.id, cb.from_user.full_name or "", cb.from_user.username or "")
    async with core._wlock(wid):
        async with Session() as s:
            peek = await s.get(PlatformWager, wid)
        if peek is not None and peek.bot_id == row.id and not await core.game_enabled(peek.game, row.id):
            await cb.answer("این بازی فعال نیست.", show_alert=True)
            return
        r = await R.join_race(wid, _tuple(cb.from_user), row.id)
    if r.w is not None and r.w.status == "playing":
        st = core._st(r.w)
        R.LIVE[r.w.id] = {"wid": r.w.id, "chat": r.w.chat_id, "bot": row.id, "kind": r.w.game,
                          "players": {r.w.creator_id, r.w.opponent_id}, "answer": st.get("a"), "n": st.get("n"),
                          "until": time.monotonic() + R.SECS[r.w.game] + 5}
        await R._edit(bot, r.w)
        asyncio.create_task(R._expire_later(bot, r.w.id, R.SECS[r.w.game]))
        await R._announce_turn(bot, r.w)
    elif r.w is not None:
        await R._edit(bot, r.w)
    await cb.answer(r.err or "به بازی پیوستی؛ موفق باشی!", show_alert=bool(r.err))


@wager_race_router.callback_query(F.data.startswith("twr:cancel:"))
async def twr_cancel(cb: CallbackQuery, bot: Bot):
    row = await _cb_row(cb, bot)
    if not row:
        return
    wid = _wid(cb)
    async with core._wlock(wid):
        r = await core.cancel_wager(wid, cb.from_user.id, row.id)
    if r.w is not None:
        R.LIVE.pop(r.w.id, None)
        await R._edit(bot, r.w)
    await cb.answer(r.err or "لغو شد و مبلغ برگشت.", show_alert=bool(r.err))


def _maybe_answer(m: Message) -> bool:
    """فیلترِ همگام: این کاربر بازیکنِ یک مسابقهٔ زندهٔ مستأجر در همین گروه است؟ (تأییدِ نهایی در هندلر، با شناسهٔ ربات)"""
    if not m.from_user or not m.text:
        return False
    now = time.monotonic()
    uid, cid = m.from_user.id, m.chat.id
    return (any(lv.get("bot") and lv.get("chat") == cid and uid in lv["players"] and lv.get("until", 0) > now
                for lv in R.LIVE.values())
            or R._digit_guess(m))                            # عددِ ۱–۱۰۰: بازیابیِ بازیِ نوبتی بعد از ری‌استارت در هندلر


@wager_race_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_maybe_answer))
async def twr_answer(msg: Message, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        raise SkipHandler
    live = R._match(msg, row.id)
    if not live and R._digit_guess(msg):
        live = await R.restore_live(msg.chat.id, msg.from_user.id, row.id)
    if not live:
        raise SkipHandler
    uid, now = msg.from_user.id, time.monotonic()
    if live["kind"] == "guess":
        if now - R._LAST_GUESS.get((msg.chat.id, uid), -1e9) < R.GUESS_GAP:
            return
        R._LAST_GUESS[(msg.chat.id, uid)] = now
        if await R.guess_step(bot, msg, live) != "win":
            return
    w = await R.settle_to(live["wid"], uid)
    if w is None:
        if not await R._still_active(live["wid"]):
            R.LIVE.pop(live["wid"], None)
        return
    R.LIVE.pop(live["wid"], None)
    await R._edit(bot, w)
    await msg.reply(f"🏁 <b>{_html.escape(msg.from_user.full_name or str(uid))}</b> برنده شد!\n"
                    f"🎁 {_rial(w.payout_amount)} به کیف پولش اضافه شد.", parse_mode="HTML",
                    reply_markup=await core.result_kb(w))
