# -*- coding: utf-8 -*-
"""
reply_label_registry.py — بازیابیِ متنِ دکمه‌هایِ کیبوردِ پایینِ صفحه وقتی ایموجی‌شان آیکونِ پرمیوم شده.

=== PATCH_REPLY_LABEL_RESTORE_V1 ===
گزارشِ ادمین (سهیل): «وقتی حالتِ منو را روی عادی (کیبورد) می‌گذارم و دکمه‌ها را می‌زنم چیزی
نمی‌آید؛ حالتِ شیشه‌ای اوکی است». در اسکرین‌شات هم پیامِ خودِ ادمین «بازگشت به خانه» بود، بدونِ 🏠.

علتِ ریشه‌ای: میان‌افزارِ ایموجیِ پرمیوم (premium_emoji_dynamic) موقعِ *ارسال*، ایموجیِ داخلِ متنِ
دکمه‌هایِ ReplyKeyboard را برمی‌دارد و به‌شکلِ آیکون (icon_custom_emoji_id) رویِ دکمه می‌گذارد
(«🏠 بازگشت به خانه» ⇒ متنِ «بازگشت به خانه» + آیکون). وقتی کاربر دکمه را می‌زند تلگرام فقط *متنِ*
دکمه را می‌فرستد، یعنی «بازگشت به خانه» — ولی صدها هندلرِ ربات با تطابقِ دقیق دنبالِ
«🏠 بازگشت به خانه» می‌گردند. نتیجه: دکمه بی‌واکنش. (در حالتِ شیشه‌ای callback_data دارد و متن مهم
نیست، برای همین آن حالت سالم بود.) هر رباتی که نگاشتِ ایموجیِ پرمیوم دارد و حالتِ کیبوردی، دچار بود.

راه‌حل: موقعِ ارسال، برایِ هر کیبوردِ پایینِ صفحه یادداشت می‌کنیم «متنِ بدونِ ایموجی ← متنِ اصلی»
(به‌ازایِ هر چت، فقط کیبوردِ *آخر*، چون همان است که کاربر می‌بیند). موقعِ دریافت، اگر متنِ پیام
در این یادداشت بود، قبل از رسیدن به هندلرها با متنِ اصلی جایگزین می‌شود. هندلرها هیچ تغییری
نمی‌کنند و همان متنِ همیشگی (با ایموجی) را می‌بینند.

پایداری: یادداشت‌ها در Redis هم نگه داشته می‌شوند (چندین ورکر/ری‌استارت؛ FSM هم همین‌طور در Redis
است) و یک کشِ کوتاهِ درون‌حافظه‌ای جلوی یک رفت‌وبرگشتِ Redis به‌ازایِ هر پیام را می‌گیرد. اگر Redis
در دسترس نبود، فقط حافظهٔ همان پروسه استفاده می‌شود (خطا هرگز پیام را نمی‌اندازد).

محدودیت‌های عمدی: فقط چتِ خصوصی؛ فقط رباتی که همین الان نگاشتِ پرمیوم دارد (بقیه هیچ سربار ندارند)؛
اگر دو دکمهٔ متفاوتِ همان کیبورد بعد از حذفِ ایموجی هم‌متن شوند، یا یک دکمهٔ ساده (بدونِ ایموجی)
همان متن را داشته باشد، برایِ آن متن هیچ بازیابی‌ای انجام نمی‌شود (ابهام = دست‌نزدن).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict

from aiogram import BaseMiddleware
from aiogram.types import Message

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

_TTL_S = 30 * 24 * 3600        # عمرِ یادداشت در Redis
_LOCAL_READ_TTL_S = 5.0        # تازه‌بودنِ کشِ خواندن (ورکرهایِ دیگر ممکن است کیبورد فرستاده باشند)
_REDIS_REWRITE_SKIP_S = 300.0  # نوشتنِ تکراریِ همان کیبورد در این بازه به Redis نمی‌رود
_LOCAL_MAX = 5000

# key -> (fetched_at, {stripped: original}, redis_written_at)
_local: "OrderedDict[str, tuple[float, dict[str, str], float]]" = OrderedDict()
_redis = None
_redis_init_tried = False
_last_warn = 0.0


def _key(token: str, chat_id) -> str:
    return f"rl:{hashlib.sha1(str(token).encode()).hexdigest()[:12]}:{chat_id}"


def _warn(msg: str, *a) -> None:
    """هشدارِ Redis حداکثر دقیقه‌ای یک‌بار (تا لاگ پر نشود)."""
    global _last_warn
    now = time.monotonic()
    if now - _last_warn > 60:
        _last_warn = now
        logger.warning(msg, *a)


def _get_redis():
    global _redis, _redis_init_tried
    if _redis is not None or _redis_init_tried:
        return _redis
    _redis_init_tried = True
    if os.environ.get("REPLY_LABEL_REDIS", "1") == "0":
        return None
    try:
        import redis.asyncio as aioredis
        _redis = aioredis.Redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            socket_timeout=2, socket_connect_timeout=2, decode_responses=True)
    except Exception as e:  # noqa: BLE001
        _warn("[reply_label] اتصال به Redis ساخته نشد؛ فقط حافظهٔ پروسه (%s)", e)
        _redis = None
    return _redis


def _put_local(key: str, fetched_at: float, mapping: dict, redis_written_at: float) -> None:
    _local[key] = (fetched_at, mapping, redis_written_at)
    _local.move_to_end(key)
    while len(_local) > _LOCAL_MAX:
        _local.popitem(last=False)


async def remember(token: str, chat_id, pairs: dict[str, str]) -> None:
    """کیبوردِ فعلیِ این چت = pairs (جایگزینِ کاملِ قبلی؛ خالی = پاک شود)."""
    if chat_id is None:
        return
    key = _key(token, chat_id)
    now = time.monotonic()
    prev = _local.get(key)
    same_recent = (prev is not None and prev[1] == pairs and (now - prev[2]) < _REDIS_REWRITE_SKIP_S)
    redis_written_at = prev[2] if same_recent else 0.0
    if not same_recent:
        r = _get_redis()
        if r is not None:
            try:
                if pairs:
                    await r.set(key, json.dumps(pairs, ensure_ascii=False), ex=_TTL_S)
                else:
                    await r.delete(key)
                redis_written_at = now
            except Exception as e:  # noqa: BLE001
                _warn("[reply_label] نوشتن در Redis ناموفق بود (%s)", e)
    _put_local(key, now, dict(pairs), redis_written_at)


async def forget(token: str, chat_id) -> None:
    """کیبورد از رویِ صفحه برداشته شد (ReplyKeyboardRemove)."""
    await remember(token, chat_id, {})


async def restore(token: str, chat_id, text: str | None) -> str | None:
    """متنِ اصلیِ دکمه (با ایموجی) برای متنِ رسیده، یا None اگر چیزی برای بازیابی نیست."""
    if not text or chat_id is None:
        return None
    key = _key(token, chat_id)
    now = time.monotonic()
    ent = _local.get(key)
    if ent is None or (now - ent[0]) > _LOCAL_READ_TTL_S:
        mapping: dict[str, str] = ent[1] if ent else {}
        r = _get_redis()
        if r is not None:
            try:
                raw = await r.get(key)
                mapping = json.loads(raw) if raw else {}
            except Exception as e:  # noqa: BLE001
                _warn("[reply_label] خواندن از Redis ناموفق بود (%s)", e)
        _put_local(key, now, mapping, ent[2] if ent else 0.0)
        ent = _local[key]
    m = ent[1]
    return m.get(text) or m.get(text.strip())


class ReplyLabelRestoreMiddleware(BaseMiddleware):
    """میان‌افزارِ بیرونیِ پیام: متنِ دکمهٔ بدونِ ایموجی را به متنِ اصلی برمی‌گرداند."""

    async def __call__(self, handler, event, data):
        try:
            event = await self._maybe_restore(event, data)
        except Exception:  # noqa: BLE001 — هرگز نباید پیامِ کاربر را بیندازد
            logger.exception("[reply_label] بازیابیِ متنِ دکمه خطا داد؛ پیام بدونِ تغییر پردازش شد")
        return await handler(event, data)

    async def _maybe_restore(self, event, data):
        if not isinstance(event, Message) or not event.text:
            return event
        if getattr(event.chat, "type", None) != "private":
            return event
        bot = data.get("bot")
        if bot is None:
            return event
        from premium_emoji_dynamic import _map_for_token
        if not await _map_for_token(bot.token):
            return event                      # این ربات ایموجیِ پرمیوم ندارد ⇒ هیچ سربار/تغییری
        orig = await restore(bot.token, event.chat.id, event.text)
        if orig and orig != event.text:
            logger.debug("[reply_label] «%s» ← «%s»", event.text, orig)
            return event.model_copy(update={"text": orig})
        return event


def install_reply_label_restore(dp) -> None:
    """یک‌بار رویِ Dispatcher نصب می‌شود (idempotent)."""
    if getattr(dp, "_reply_label_restore_installed", False):
        return
    dp.message.outer_middleware(ReplyLabelRestoreMiddleware())
    dp._reply_label_restore_installed = True
