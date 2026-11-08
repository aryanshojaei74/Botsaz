"""chpost_userbot.py — ارسالِ پستِ کانال «از طرفِ اکانتِ خودِ مدیر» (PATCH_CHPOST_USERBOT_V1).

چرا: تلگرام ایموجیِ پرمیوم را فقط از اکانتِ پرمیوم می‌پذیرد؛ رباتِ معمولی (حتی با copyMessage یا حالتِ اینلاین) آن‌ها را به ایموجیِ
ساده تبدیل می‌کند. پس پست با نشستِ Telethonِ اکانتِ خودِ مدیر در کانال گذاشته می‌شود (در کانال با نامِ خودِ کانال دیده می‌شود، نه
یوزرنیمِ مدیر، مگر «Sign messages» روشن باشد) و بعد خودِ ربات دکمه‌ها را زیرِ همان پست می‌چسباند (editMessageReplyMarkup).

نشست (StringSession) رمزنگاری‌شده در تنظیماتِ پلتفرم ذخیره می‌شود؛ لاگ/خروجی هرگز آن را نشان نمی‌دهد.
"""
from __future__ import annotations

import os

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)


class UserbotError(Exception):
    """پیامِ فارسیِ قابلِ‌نمایش به مدیر."""


def _telethon():
    try:
        import telethon
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.tl import types as T
    except ImportError as e:      # pragma: no cover
        raise UserbotError("کتابخانهٔ telethon روی سرور نصب نیست (pip install telethon).") from e
    return telethon, TelegramClient, StringSession, T


# نوعِ entityِ Bot API → سازندهٔ Telethon
def entities_to_telethon(raw: list[dict] | None) -> list:
    """entityهایِ ذخیره‌شدهٔ aiogram (dict) را به entityهایِ Telethon تبدیل می‌کند؛ نوعِ ناشناخته بی‌صدا رد می‌شود.
    offset/length هر دو UTF-16 هستند، پس بدونِ تغییر منتقل می‌شوند."""
    _t, _c, _s, T = _telethon()
    out = []
    simple = {
        "bold": T.MessageEntityBold, "italic": T.MessageEntityItalic, "underline": T.MessageEntityUnderline,
        "strikethrough": T.MessageEntityStrike, "spoiler": T.MessageEntitySpoiler, "code": T.MessageEntityCode,
        "url": T.MessageEntityUrl, "mention": T.MessageEntityMention, "hashtag": T.MessageEntityHashtag,
        "cashtag": T.MessageEntityCashtag, "bot_command": T.MessageEntityBotCommand, "email": T.MessageEntityEmail,
        "phone_number": T.MessageEntityPhone,
    }
    for e in raw or []:
        try:
            ty, off, ln = e.get("type"), int(e["offset"]), int(e["length"])
            if ty in simple:
                out.append(simple[ty](off, ln))
            elif ty == "pre":
                out.append(T.MessageEntityPre(off, ln, e.get("language") or ""))
            elif ty == "text_link" and e.get("url"):
                out.append(T.MessageEntityTextUrl(off, ln, e["url"]))
            elif ty == "custom_emoji" and e.get("custom_emoji_id"):
                out.append(T.MessageEntityCustomEmoji(off, ln, int(e["custom_emoji_id"])))
            elif ty in ("blockquote", "expandable_blockquote"):
                out.append(T.MessageEntityBlockquote(off, ln, collapsed=(ty == "expandable_blockquote")))
        except (KeyError, ValueError, TypeError):
            logger.debug("chpost userbot: entityِ نامعتبر رد شد: %r", e)
    return out


def serialize_entities(entities) -> list[dict]:
    """entityهایِ aiogram را به dictِ JSON-پذیر تبدیل می‌کند (برایِ نگه‌داشتن در FSM)."""
    res = []
    for e in entities or []:
        try:
            d = e.model_dump(exclude_none=True)
        except AttributeError:
            d = dict(e)
        # فقط فیلدهایِ لازم (user/ ... حذف می‌شود)
        keep = {k: d[k] for k in ("type", "offset", "length", "url", "language", "custom_emoji_id") if k in d}
        res.append(keep)
    return res


def _client(api_id: int, api_hash: str, session: str):
    _t, TelegramClient, StringSession, _T = _telethon()
    return TelegramClient(StringSession(session), int(api_id), api_hash)


async def validate_session(api_id: int, api_hash: str, session: str) -> str:
    """نشست را امتحان می‌کند؛ نامِ اکانت (و یوزرنیم) و پرمیوم‌بودنش را برمی‌گرداند یا UserbotError."""
    c = _client(api_id, api_hash, session)
    try:
        await c.connect()
        if not await c.is_user_authorized():
            raise UserbotError("نشست معتبر نیست؛ دوباره با tools/make_telethon_session.py بساز.")
        me = await c.get_me()
        name = f"{(me.first_name or '')} {(me.last_name or '')}".strip() or "—"
        return name + (f" (@{me.username})" if me.username else "") + ("" if getattr(me, "premium", False) else " — ⚠️ پرمیوم نیست")
    except UserbotError:
        raise
    except Exception as e:      # noqa: BLE001
        raise UserbotError(f"اتصال با این api_id/api_hash/نشست ممکن نشد ({type(e).__name__}).") from e
    finally:
        try:
            await c.disconnect()
        except Exception:      # noqa: BLE001
            logger.debug("chpost userbot: بستنِ اتصال ممکن نشد", exc_info=True)


async def _resolve(c, chat_id):
    cid = int(chat_id)
    try:
        return await c.get_input_entity(cid)
    except ValueError:
        await c.get_dialogs()          # کشِ entityهایِ اکانت را پر می‌کند؛ بعد دوباره
        return await c.get_input_entity(cid)


async def send_post(api_id: int, api_hash: str, session: str, chat_id, text: str, entities_raw: list[dict] | None,
                    photo_path: str | None = None) -> int:
    """پست را با اکانتِ مدیر در chat_id می‌فرستد؛ شناسهٔ پیام را برمی‌گرداند."""
    ents = entities_to_telethon(entities_raw)
    c = _client(api_id, api_hash, session)
    try:
        await c.connect()
        if not await c.is_user_authorized():
            raise UserbotError("نشستِ اکانت منقضی/نامعتبر است؛ دوباره وصلش کن.")
        try:
            target = await _resolve(c, chat_id)
        except Exception as e:      # noqa: BLE001
            raise UserbotError("اکانت به این کانال/گروه دسترسی ندارد (باید عضو/ادمین باشد).") from e
        if photo_path:
            m = await c.send_file(target, photo_path, caption=text or None, formatting_entities=ents or None)
        else:
            m = await c.send_message(target, text, formatting_entities=ents or None, link_preview=False)
        return int(m.id)
    except UserbotError:
        raise
    except Exception as e:      # noqa: BLE001
        name = type(e).__name__
        if "Forbidden" in name or "Admin" in name or "Chat" in name:
            raise UserbotError(f"اکانت اجازهٔ ارسال در این چت را ندارد ({name}).") from e
        logger.exception("chpost userbot: ارسال ناموفق")
        raise UserbotError(f"ارسال با اکانت ناموفق بود ({name}).") from e
    finally:
        try:
            await c.disconnect()
        except Exception:      # noqa: BLE001
            logger.debug("chpost userbot: بستنِ اتصال ممکن نشد", exc_info=True)


def default_api() -> tuple[str, str]:
    """api_id/api_hashِ مشترکِ سرور (اختیاری، همان ESCROW_API_*)."""
    i, h = (os.getenv("ESCROW_API_ID") or "").strip(), (os.getenv("ESCROW_API_HASH") or "").strip()
    return (i, h) if i.isdigit() and h else ("", "")


# ═══════════ ورود با شماره و کد (بدونِ نیاز به ساختِ StringSession) ═══════════
def friendly_error(e: Exception) -> str:
    """خطایِ Telethon → پیامِ فارسیِ روشن (بر اساسِ نامِ کلاس؛ بدونِ import از telethon.errors)."""
    n = type(e).__name__
    if n == "PhoneNumberInvalidError":
        return "شمارهٔ تلفن نامعتبر است. با کدِ کشور بفرست (مثلاً +98912…)."
    if n == "PhoneNumberBannedError":
        return "این شماره در تلگرام مسدود است."
    if n in ("PhoneCodeInvalidError", "PhoneCodeEmptyError"):
        return "کد اشتباه است. دوباره بفرست."
    if n == "PhoneCodeExpiredError":
        return "کد منقضی شده؛ از «اتصالِ اکانت» دوباره شروع کن تا کدِ تازه بیاید."
    if n in ("PasswordHashInvalidError",):
        return "رمزِ دومرحله‌ای اشتباه است. دوباره بفرست."
    if n in ("ApiIdInvalidError", "ApiIdPublishedFloodError"):
        return "api_id/api_hash معتبر نیست (از my.telegram.org بگیر)."
    if n == "FloodWaitError":
        return f"تلگرام موقتاً محدود کرد؛ {getattr(e, 'seconds', '؟')} ثانیه بعد دوباره امتحان کن."
    if n == "SendCodeUnavailableError":
        return "ارسالِ کد فعلاً ممکن نیست؛ کمی بعد دوباره امتحان کن."
    return f"خطایِ تلگرام ({n})."


class Login:
    """ورودِ مرحله‌ای: شماره ← کد ← (رمزِ دومرحله‌ای). خروجی: StringSession. تست‌ها make_login را جایگزین می‌کنند."""

    def __init__(self, api_id: int, api_hash: str):
        _t, TelegramClient, StringSession, _T = _telethon()
        self.api_id, self.api_hash = int(api_id), api_hash
        self.client = TelegramClient(StringSession(), self.api_id, api_hash)
        self.phone = ""
        self.hash = None

    async def send_code(self, phone: str) -> None:
        try:
            await self.client.connect()
            self.phone = phone
            sent = await self.client.send_code_request(phone)
            self.hash = sent.phone_code_hash
        except UserbotError:
            raise
        except Exception as e:      # noqa: BLE001
            raise UserbotError(friendly_error(e)) from e

    async def sign_in_code(self, code: str) -> str | None:
        """رشتهٔ نشست، یا None اگر رمزِ دومرحله‌ای لازم است."""
        try:
            await self.client.sign_in(self.phone, code, phone_code_hash=self.hash)
        except Exception as e:      # noqa: BLE001
            if type(e).__name__ == "SessionPasswordNeededError":
                return None
            raise UserbotError(friendly_error(e)) from e
        return self.client.session.save()

    async def sign_in_password(self, password: str) -> str:
        try:
            await self.client.sign_in(password=password)
        except Exception as e:      # noqa: BLE001
            raise UserbotError(friendly_error(e)) from e
        return self.client.session.save()

    async def close(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:      # noqa: BLE001
            logger.debug("chpost userbot: بستنِ اتصالِ ورود ممکن نشد", exc_info=True)


def make_login(api_id: int, api_hash: str):
    return Login(api_id, api_hash)


import time as _time

_LOGINS: dict[int, tuple[object, float]] = {}       # مدیر → (login, زمان)
_LOGIN_TTL = 600


def parse_code(text: str) -> str:
    """کدِ ورود: فقط رقم‌ها. مدیر می‌تواند با فاصله/خط بفرستد (۱-۲-۳-۴-۵) تا تلگرام «کدِ فاش‌شده» حسابش نکند."""
    t = (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    return "".join(ch for ch in t if ch.isdigit())


def parse_phone(text: str) -> str:
    t = (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    digits = "".join(ch for ch in t if ch.isdigit())
    if t.strip().startswith("00"):
        digits = digits[2:]
    elif digits.startswith("09") and len(digits) == 11:      # ۰۹۱۲… ⇒ +۹۸۹۱۲…
        digits = "98" + digits[1:]
    return ("+" + digits) if digits else ""


async def put_login(uid: int, login) -> None:
    await drop_login(uid)
    _LOGINS[uid] = (login, _time.monotonic())


def get_login(uid: int):
    ent = _LOGINS.get(uid)
    if ent and _time.monotonic() - ent[1] <= _LOGIN_TTL:
        return ent[0]
    return None


async def drop_login(uid: int) -> None:
    ent = _LOGINS.pop(uid, None)
    if ent:
        try:
            await ent[0].close()
        except Exception:      # noqa: BLE001
            logger.debug("chpost userbot: بستنِ ورود ممکن نشد", exc_info=True)
