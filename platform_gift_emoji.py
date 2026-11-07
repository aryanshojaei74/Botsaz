"""
platform_gift_emoji.py — نگاشتِ «گیفتِ تلگرام ← ایموجیِ پرمیومِ همان گیفت» (PATCH_GIFT_CEMOJI_V1).

شناسهٔ گیفت‌ها در کلِ تلگرام یکی است، پس نگاشت سراسری و یک‌جا نگه‌داری می‌شود و همهٔ ربات‌هایِ مشتری از آن می‌خوانند:
  • خودکار (/giftmap): گیفت‌هایِ تلگرام با پکِ ایموجیِ گیفت‌ها (پیش‌فرض gifts_emoji_by_gifts_changes_bot) از راهِ
    file_unique_idِ استیکر مقایسه می‌شوند؛ هر جا یکی بود custom_emoji_id ذخیره می‌شود.
  • دستی (/gifte <gift_id> + یک ایموجیِ پرمیوم در همان پیام): برایِ مواردی که خودکار پیدا نشد (نیازمندِ اکانتِ پریمیوم).
نمایشِ این آیکون در دکمه‌هایِ فروشگاهِ گیفتِ هر ربات فقط با تیکِ مالک است و فقط برایِ ربات‌هایی کار می‌کند که مالکشان
پریمیوم باشد یا یوزرنیمِ اضافهٔ فرگمنت داشته باشد (قاعدهٔ تلگرام).
دستورها فقط برایِ مدیرِ اصلی (چتِ خصوصی): /giftmap · /gifte · /giftepack · /giftmaplist
"""
from __future__ import annotations

import json

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from config import PLATFORM_ADMIN_ID, Session
from models import PlatformSetting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

gift_emoji_router = Router(name="gift_emoji")

KEY_AUTO, KEY_MANUAL, KEY_PACK = "gift_cemoji_map", "gift_cemoji_manual", "gift_emoji_pack"
DEFAULT_PACK = "gifts_emoji_by_gifts_changes_bot"


async def _get(key: str, default: str = "") -> str:
    async with Session() as s:
        r = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
    return r.value if (r and r.value is not None) else default


async def _set(key: str, value: str) -> None:
    async with Session() as s:
        r = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
        if r:
            r.value = value
        else:
            s.add(PlatformSetting(key=key, value=value))
        await s.commit()


def _load(raw: str) -> dict:
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


_CACHE: dict = {"t": 0.0, "map": {}}


async def get_map(fresh: bool = False) -> dict[str, str]:
    """{gift_id: custom_emoji_id}؛ ورودیِ دستی بر خودکار می‌چربد. ۶۰ ثانیه کش می‌شود (فروشگاه هر بار نخواند)."""
    import time
    if not fresh and time.time() - _CACHE["t"] < 60:
        return _CACHE["map"]
    auto, manual = _load(await _get(KEY_AUTO)), _load(await _get(KEY_MANUAL))
    m = {**{str(k): str(v) for k, v in auto.items()}, **{str(k): str(v) for k, v in manual.items()}}
    _CACHE["t"], _CACHE["map"] = time.time(), m
    return m


async def sync(bot) -> dict:
    """نگاشتِ گیفت ← custom_emoji_id را می‌سازد. اولویت: خودِ استیکرِ گیفت (در تلگرام استیکرِ گیفت‌ها از نوعِ custom_emoji است و
    custom_emoji_id دارد؛ گیفتِ تازه هم خودکار می‌آید). اگر نبود، با پکِ ایموجی تطبیق می‌دهیم (اندازهٔ فایل + ایموجی، بعد file_unique_id).
    خروجی: {matched, total, own, unmatched:[gift_id], pack, pack_size, error}."""
    pack_name = (await _get(KEY_PACK, DEFAULT_PACK)).strip() or DEFAULT_PACK
    out = {"matched": 0, "own": 0, "total": 0, "unmatched": [], "pack": pack_name, "pack_size": 0, "error": ""}
    try:
        gifts = (await bot.get_available_gifts()).gifts
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return out
    out["total"] = len(gifts)
    pack = None
    if any(not getattr(getattr(g, "sticker", None), "custom_emoji_id", None) for g in gifts):
        try:
            pack = await bot.get_sticker_set(pack_name)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"پک: {type(e).__name__}: {str(e)[:120]}"
    by_unique, by_size = {}, {}
    for s in (pack.stickers if pack else []):
        if getattr(s, "custom_emoji_id", None):
            by_unique[s.file_unique_id] = s.custom_emoji_id
            if getattr(s, "file_size", None) and getattr(s, "emoji", None):
                by_size.setdefault((s.file_size, s.emoji), s.custom_emoji_id)
    out["pack_size"] = len(pack.stickers) if pack else 0
    auto = {}
    for g in gifts:
        st = getattr(g, "sticker", None)
        cid = getattr(st, "custom_emoji_id", None)
        if cid:
            out["own"] += 1
        else:
            cid = by_unique.get(getattr(st, "file_unique_id", None))
            if not cid and getattr(st, "file_size", None) and getattr(st, "emoji", None):
                cid = by_size.get((st.file_size, st.emoji))
        if cid:
            auto[str(g.id)] = cid
        else:
            out["unmatched"].append(str(g.id))
    out["matched"] = len(auto)
    await _set(KEY_AUTO, json.dumps(auto))
    _CACHE["t"] = 0.0
    return out


def _is_admin(msg: Message) -> bool:
    return bool(PLATFORM_ADMIN_ID) and bool(msg.from_user) and int(msg.from_user.id) == int(PLATFORM_ADMIN_ID)


@gift_emoji_router.message(Command("giftmap"), lambda m: m.chat.type == "private")
async def cmd_map(msg: Message):
    if not _is_admin(msg):
        return
    r = await sync(msg.bot)
    if r["error"]:
        return await msg.answer(f"❌ {r['error']}\nنامِ پک: <code>{r['pack']}</code> (با /giftepack عوض می‌شود)", parse_mode="HTML")
    text = (f"🎁 نگاشتِ گیفت ← ایموجیِ پرمیوم\n✅ خودکار پیدا شد: {r['matched']} از {r['total']} گیفت "
            f"({r['own']} تا از خودِ استیکرِ گیفت)")
    if r["unmatched"]:
        text += "\n\n❓ بدونِ تطبیق (با /gifte دستی بده):\n" + "\n".join(f"<code>{g}</code>" for g in r["unmatched"][:20])
    await msg.answer(text, parse_mode="HTML")


@gift_emoji_router.message(Command("gifte"), lambda m: m.chat.type == "private")
async def cmd_manual(msg: Message):
    """/gifte <gift_id> 🧸  — ایموجی باید «پرمیوم» باشد (entity از نوعِ custom_emoji)."""
    if not _is_admin(msg):
        return
    parts = (msg.text or "").split()
    ents = [e for e in (msg.entities or []) if e.type == "custom_emoji" and e.custom_emoji_id]
    if len(parts) < 2 or not ents:
        return await msg.answer("فرمت: <code>/gifte شناسهٔ‌گیفت</code> و در همان پیام یک ایموجیِ پرمیوم.", parse_mode="HTML")
    man = _load(await _get(KEY_MANUAL))
    man[parts[1]] = ents[0].custom_emoji_id
    await _set(KEY_MANUAL, json.dumps(man))
    _CACHE["t"] = 0.0
    await msg.answer(f"✅ ثبت شد: <code>{parts[1]}</code> ← <code>{ents[0].custom_emoji_id}</code>", parse_mode="HTML")


@gift_emoji_router.message(Command("giftepack"), lambda m: m.chat.type == "private")
async def cmd_pack(msg: Message):
    if not _is_admin(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        return await msg.answer(f"پکِ فعلی: <code>{await _get(KEY_PACK, DEFAULT_PACK)}</code>\nفرمت: <code>/giftepack نامِ‌پک</code>",
                                parse_mode="HTML")
    await _set(KEY_PACK, parts[1].replace("https://t.me/addemoji/", "").strip("/"))
    await msg.answer("✅ ثبت شد؛ حالا /giftmap را بزن.")


def _row(s) -> str:
    th = getattr(s, "thumbnail", None)
    return " | ".join(str(x) for x in (
        getattr(s, "emoji", None), getattr(s, "file_unique_id", None), getattr(s, "file_size", None),
        f"{getattr(s, 'width', '')}x{getattr(s, 'height', '')}", getattr(s, "type", None),
        "anim" if getattr(s, "is_animated", False) else ("video" if getattr(s, "is_video", False) else "static"),
        getattr(s, "set_name", None), getattr(s, "custom_emoji_id", None), getattr(th, "file_unique_id", None) if th else None))


@gift_emoji_router.message(Command("giftdump"), lambda m: m.chat.type == "private")
async def cmd_dump(msg: Message):
    """فایلِ تشخیصی: مشخصاتِ استیکرِ گیفت‌ها و ایموجی‌هایِ پک کنارِ هم، تا کلیدِ تطبیق پیدا شود."""
    if not _is_admin(msg):
        return
    from aiogram.types import BufferedInputFile
    pack_name = (await _get(KEY_PACK, DEFAULT_PACK)).strip() or DEFAULT_PACK
    try:
        gifts = (await msg.bot.get_available_gifts()).gifts
        pack = await msg.bot.get_sticker_set(pack_name)
    except Exception as e:  # noqa: BLE001
        return await msg.answer(f"❌ {type(e).__name__}: {str(e)[:160]}")
    cols = "emoji | unique_id | size | WxH | type | kind | set_name | custom_emoji_id | thumb_unique_id"
    lines = [f"# گیفت‌ها ({len(gifts)})", "gift_id | stars | total | " + cols]
    for g in gifts:
        lines.append(f"{g.id} | {g.star_count} | {getattr(g, 'total_count', None)} | {_row(g.sticker)}")
    lines += ["", f"# پک {pack_name} ({len(pack.stickers)})", "idx | " + cols]
    for i, s in enumerate(pack.stickers):
        lines.append(f"{i} | {_row(s)}")
    await msg.answer_document(BufferedInputFile("\n".join(lines).encode(), filename="gift-dump.txt"),
                              caption="این فایل را برایِ بررسیِ کلیدِ تطبیق بفرست.")


@gift_emoji_router.message(Command("giftmaplist"), lambda m: m.chat.type == "private")
async def cmd_list(msg: Message):
    if not _is_admin(msg):
        return
    auto, man = _load(await _get(KEY_AUTO)), _load(await _get(KEY_MANUAL))
    await msg.answer(f"🎁 نگاشتِ گیفت‌ها: خودکار {len(auto)} · دستی {len(man)} · کل (ادغام) {len(await get_map())}")
