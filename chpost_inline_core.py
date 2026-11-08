"""chpost_inline_core.py — منطقِ مشترکِ «ارسالِ اینلاینِ پست» (PATCH_CHPOST_INLINE_V1).

هم ربات‌هایِ مشتری (tenant) و هم رباتِ اصلیِ بات‌ساز (platform_chpost.py) از همین ماژول استفاده می‌کنند؛ عمداً مستقل از هر دو
(بدونِ import از tenant/ یا platform_bot) تا جفت‌شدنِ سرویس‌ها پیش نیاید.

ایده: پست (متن/عکس + دکمه‌ها) به‌صورتِ پیش‌نویس در DB ذخیره می‌شود (ChpostDraft؛ bot_id=0 یعنی رباتِ اصلی). ادمین با
@ربات در هر چت/کانال پیش‌نویس را از فهرستِ اینلاین انتخاب می‌کند؛ پیام از طرفِ «خودِ او» ارسال می‌شود و ایموجیِ پرمیومِ
اکانتِ او سالم می‌ماند (رباتِ معمولی نمی‌تواند ایموجیِ پرمیوم بفرستد).
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json as _json
import re as _re
from typing import Awaitable, Callable

from aiogram.types import (
    InlineKeyboardMarkup, InlineQueryResultArticle, InlineQueryResultCachedPhoto, InputTextMessageContent,
)
from sqlalchemy import select

import config
from models import ChpostDraft

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

DRAFT_KEEP = 20
DRAFT_TTL_DAYS = 14
LIST_LIMIT = 10
QUERY_RE = _re.compile(r"cp(\d+)")


async def draft_save(bot_id: int, uid: int, data: dict) -> int:
    """پیش‌نویس را ذخیره می‌کند و شناسه‌اش را برمی‌گرداند. فقط ۲۰ پیش‌نویسِ آخرِ هر ادمین (و حداکثر ۱۴ روز) نگه داشته می‌شود."""
    async with config.Session() as s:
        d = ChpostDraft(bot_id=bot_id, user_telegram_id=uid, text=data.get("chpost_text") or "",
                        photo=data.get("chpost_photo"),
                        buttons=_json.dumps(data.get("chpost_buttons") or [], ensure_ascii=False),
                        button_ids=_json.dumps(data.get("chpost_button_ids") or []),
                        guide_keys=_json.dumps(list(data.get("chpost_guide_keys") or data.get("chpost_home_keys") or [])))
        s.add(d)
        await s.commit()
        did = d.id
        old = (await s.scalars(select(ChpostDraft).where(
            ChpostDraft.bot_id == bot_id, ChpostDraft.user_telegram_id == uid)
            .order_by(ChpostDraft.id.desc()).offset(DRAFT_KEEP))).all()
        cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=DRAFT_TTL_DAYS)
        stale = (await s.scalars(select(ChpostDraft).where(
            ChpostDraft.bot_id == bot_id, ChpostDraft.created_at < cutoff))).all()
        for o in {x.id: x for x in [*old, *stale]}.values():
            await s.delete(o)
        await s.commit()
    return did


async def drafts_for(bot_id: int, uid: int, query: str) -> list:
    """query = «cp12» ⇒ همان پیش‌نویس؛ خالی/هر چیزِ دیگر ⇒ ۱۰ پیش‌نویسِ آخرِ همین ادمین."""
    m = QUERY_RE.fullmatch((query or "").strip().lower())
    async with config.Session() as s:
        if m:
            d = await s.get(ChpostDraft, int(m.group(1)))
            return [d] if d and d.bot_id == bot_id and d.user_telegram_id == uid else []
        return list((await s.scalars(select(ChpostDraft).where(
            ChpostDraft.bot_id == bot_id, ChpostDraft.user_telegram_id == uid)
            .order_by(ChpostDraft.id.desc()).limit(LIST_LIMIT))).all())


def plain_title(html_text: str, photo: bool) -> str:
    t = _re.sub(r"<[^>]+>", "", html_text or "")
    t = " ".join(_html.unescape(t).split())
    return (("🖼 " if photo else "") + (t[:48] or "پست")).strip()


def draft_parts(d) -> tuple[list, list, list]:
    try:
        return (_json.loads(d.buttons or "[]"), _json.loads(d.button_ids or "[]"), _json.loads(d.guide_keys or "[]"))
    except ValueError:
        return [], [], []


KbBuilder = Callable[[object], Awaitable["InlineKeyboardMarkup | None"]]


async def build_results(drafts: list, kb_builder: KbBuilder) -> list:
    """نتیجه‌هایِ اینلاین. kb_builder(draft) → کیبوردِ دکمه‌هایِ همان پست (یا None)؛ خطایش پست را بی‌دکمه می‌فرستد، نه اینکه کلِ فهرست خراب شود."""
    results = []
    for d in drafts:
        try:
            kb = await kb_builder(d)
        except Exception:      # noqa: BLE001
            logger.exception("chpost inline: ساختِ دکمه‌ها ناموفق (بدونِ دکمه)")
            kb = None
        title = plain_title(d.text or "", bool(d.photo))
        rid = f"cp{d.id}"
        if d.photo:
            results.append(InlineQueryResultCachedPhoto(id=rid, photo_file_id=d.photo, title=title,
                                                        caption=(d.text or None), parse_mode="HTML", reply_markup=kb))
        else:
            results.append(InlineQueryResultArticle(
                id=rid, title=title, description="پیش‌نویسِ پستِ کانال — برایِ ارسال بزن",
                input_message_content=InputTextMessageContent(message_text=d.text or "—", parse_mode="HTML"),
                reply_markup=kb))
    return results


HINT_INLINE_OFF = (
    "⚠️ <b>حالتِ اینلاینِ ربات خاموش است.</b>\n\n"
    "یک‌بار این‌ها را در @BotFather بزن:\n"
    "۱) <code>/setinline</code>\n۲) رباتت را انتخاب کن\n۳) یک متنِ راهنما بفرست (مثلاً: <code>پست</code>)\n\n"
    "بعد همین دکمه را دوباره بزن. (این تنظیم فقط از BotFather ممکن است؛ از داخلِ ربات قابلِ‌روشن‌شدن نیست.)")


def ready_text(username: str, did: int) -> str:
    u = username or "ربات"
    return ("✨ <b>ارسالِ اینلاین آماده است</b>\n\n"
            "۱) روی دکمهٔ زیر بزن و کانال/گروهِ مقصد را انتخاب کن.\n"
            f"۲) در همان چت پیامی مثلِ <code>@{u} cp{did}</code> باز می‌شود؛ روی نتیجه (پیش‌نمایشِ پست) بزن تا ارسال شود.\n\n"
            "💡 پیام از طرفِ <b>خودِ تو</b> (با برچسبِ «via ربات») می‌رود، پس ایموجی‌هایِ پرمیومِ اکانتِ تو سالم می‌ماند. "
            f"برایِ دیدنِ همهٔ پیش‌نویس‌هایت در هر چت فقط <code>@{u}</code> را بنویس.\n"
            "⚠️ ارسال‌کننده باید ادمینِ همان کانال باشد و اکانتِ خودت پرمیوم.")
