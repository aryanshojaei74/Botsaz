"""acc_gift.py — ارسالِ خودکارِ گیفتِ استارزی با «اکانتِ پشتیبانی» (همان StringSessionِ واسطه‌گری).

=== PATCH_ACC_GIFT_V1 ===
اکانتِ پشتیبانی (Telethon) اگر استارز داشته باشد، گیفت را مستقیماً با MTProto می‌فرستد:
  payments.GetPaymentForm(InputInvoiceStarGift) → payments.SendStarsForm.
نکته‌ها:
  • فرستادنِ گیفت idempotent نیست؛ پس خطایِ «قطعی» (سرور پاسخِ خطا داد = ارسال نشده) با خطایِ «نامشخص»
    (قطعِ شبکه/تایم‌اوتِ حینِ SendStarsForm = شاید ارسال شده) جدا می‌شود و صدازننده هرگز روی نامشخص retry نمی‌کند.
  • مقصد با یوزرنیم resolve می‌شود (اکانتِ پشتیبانی لزوماً accessHashِ کاربر را ندارد)؛ بدونِ یوزرنیم، فقط اگر اکانت
    قبلاً با آن کاربر در تماس بوده باشد کار می‌کند — وگرنه خطایِ قطعی و برگشت به تحویلِ دستی.
  • نام فرستنده را می‌شود مخفی کرد (hide_name) — برخلافِ Bot API.
"""
from __future__ import annotations

import asyncio

from logging_setup import get_logger

logger = get_logger(__name__)

_CONNECT_TIMEOUT = 20
_CALL_TIMEOUT = 40


class AccGiftError(Exception):
    """definite=True: ارسال قطعاً انجام نشده. definite=False: نامشخص (ممکن است ارسال شده باشد)."""

    def __init__(self, message: str, definite: bool = True):
        super().__init__(message)
        self.message = message
        self.definite = definite


async def _creds(bot_id: int) -> tuple[int, str, str] | None:
    from tenant import get_setting
    api_id = (await get_setting(bot_id, "escrow_api_id", "") or "").strip()
    api_hash = (await get_setting(bot_id, "escrow_api_hash", "") or "").strip()
    session = (await get_setting(bot_id, "escrow_session", "") or "").strip()
    if not (api_id.isdigit() and api_hash and session):
        return None
    return int(api_id), api_hash, session


async def has_account(bot_id: int) -> bool:
    return (await _creds(bot_id)) is not None


def _classify(e: Exception) -> str:
    """ترجمهٔ خطایِ RPCِ تلگرام به متنِ خوانا."""
    n = type(e).__name__
    s = str(e)
    up = (n + " " + s).upper()
    flat = up.replace("_", "").replace(" ", "")      # PATCH_ACC_GIFT_PEER_MSG_V1: نامِ کلاسِ Telethon «PeerIdInvalidError» بدونِ underscore است
    if "PEERIDINVALID" in flat:
        return ("گیرنده برایِ اکانتِ پشتیبانی معتبر نبود (PeerIdInvalid): یوزرنیم مالِ یک کاربرِ عادی نیست "
                "(ممکن است ربات/کانال/حسابِ حذف‌شده یا محدود باشد) یا اکانت نمی‌تواند به او گیفت بدهد. "
                "گیفت را دستی بفرست، یا از مشتری یوزرنیمِ شخصیِ درست بخواه.")
    if "BALANCE_TOO_LOW" in up or "STARS_BALANCE" in up or "NOT_ENOUGH" in up:
        return "موجودیِ استارزِ اکانتِ پشتیبانی کافی نیست."
    if "STARGIFT_INVALID" in up or "GIFT_INVALID" in up:
        return "این گیفت برایِ ارسال معتبر نیست (شناسهٔ گیفت)."
    if "STARGIFT_USAGE_LIMITED" in up or "SOLD_OUT" in up:
        return "این گیفت تمام شده (sold out)."
    if "PEER_ID_INVALID" in up or "USERNAME_NOT_OCCUPIED" in up or "USERNAME_INVALID" in up:
        return "کاربرِ مقصد پیدا نشد (یوزرنیم/آیدی نامعتبر)."
    if "USER_PRIVACY" in up or "PRIVACY" in up:
        return "تنظیماتِ حریمِ خصوصیِ کاربر اجازهٔ دریافتِ گیفت را نمی‌دهد."
    if "MESSAGEIDINVALID" in flat or "MSGIDINVALID" in flat:
        return "پست پیدا نشد (شناسهٔ پیام نامعتبر است)."
    if "CHANNELPRIVATE" in flat or "CHANNELINVALID" in flat:
        return "کانال خصوصی است یا برایِ اکانتِ پشتیبانی در دسترس نیست."
    if "FLOOD" in up:
        return "تلگرام موقتاً محدود کرد (FloodWait)؛ کمی بعد دوباره امتحان کن."
    if "AUTH_KEY" in up or "SESSION_REVOKED" in up or "USER_DEACTIVATED" in up:
        return "نشستِ اکانتِ پشتیبانی نامعتبر شده؛ از «🤝 واسطه‌گری ← اتصالِ اکانت» دوباره وصلش کن."
    return f"{n}: {s[:160]}"


class _Conn:
    """اتصالِ کوتاه‌مدتِ Telethon (connect/disconnect داخلِ هر عملیات)."""

    def __init__(self, creds):
        self.creds = creds
        self.c = None

    async def __aenter__(self):
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError as e:
            raise AccGiftError("کتابخانهٔ telethon نصب نیست (pip install telethon)") from e
        api_id, api_hash, session = self.creds
        self.c = TelegramClient(StringSession(session), api_id, api_hash)
        try:
            await asyncio.wait_for(self.c.connect(), _CONNECT_TIMEOUT)
            if not await asyncio.wait_for(self.c.is_user_authorized(), _CALL_TIMEOUT):
                raise AccGiftError("نشستِ اکانتِ پشتیبانی دیگر معتبر نیست؛ دوباره وصلش کن.")
        except AccGiftError:
            await self._close()
            raise
        except Exception as e:  # noqa: BLE001 — هنوز چیزی ارسال نشده
            await self._close()
            raise AccGiftError(f"اتصال به تلگرام ناموفق بود: {_classify(e)}") from e
        return self.c

    async def _close(self):
        try:
            if self.c is not None:
                await self.c.disconnect()
        except Exception:  # noqa: BLE001
            logger.info("acc_gift: بستنِ اتصال ممکن نشد (بی‌اهمیت)")

    async def __aexit__(self, *a):
        await self._close()


async def status(bot_id: int) -> dict:
    """{ok, name, username, stars, error} — برایِ نمایش در منو. هرگز استثنا نمی‌دهد."""
    creds = await _creds(bot_id)
    if not creds:
        return {"ok": False, "error": "اکانتِ پشتیبانی وصل نشده."}
    try:
        from telethon.tl.functions.payments import GetStarsStatusRequest
        from telethon.tl.types import InputPeerSelf
        async with _Conn(creds) as c:
            me = await asyncio.wait_for(c.get_me(), _CALL_TIMEOUT)
            stars = None
            try:
                st = await asyncio.wait_for(c(GetStarsStatusRequest(peer=InputPeerSelf())), _CALL_TIMEOUT)
                bal = getattr(st, "balance", None)
                stars = int(getattr(bal, "amount", bal) or 0)
            except Exception as e:  # noqa: BLE001
                logger.info("acc_gift: موجودیِ استارز خوانده نشد: %s", e)
            return {"ok": True, "name": " ".join(x for x in (me.first_name, me.last_name) if x) or "—",
                    "username": me.username or "", "stars": stars}
    except AccGiftError as e:
        return {"ok": False, "error": e.message}
    except Exception as e:  # noqa: BLE001
        logger.exception("acc_gift: status ناموفق")
        return {"ok": False, "error": _classify(e)}


async def send_gift(bot_id: int, *, username: str | None, tg_id: int | None, gift_id,
                    text: str | None = None, hide_name: bool = False) -> None:
    """یک گیفت می‌فرستد؛ در موفقیت None، وگرنه AccGiftError (definite یا نامشخص)."""
    try:
        gid = int(str(gift_id))
    except (TypeError, ValueError):
        raise AccGiftError("شناسهٔ گیفت عددی نیست؛ این گیفت با اکانت قابلِ ارسال نیست.")
    creds = await _creds(bot_id)
    if not creds:
        raise AccGiftError("اکانتِ پشتیبانی وصل نشده.")
    from telethon.tl.functions.payments import GetPaymentFormRequest, SendStarsFormRequest
    from telethon.tl.types import InputInvoiceStarGift, TextWithEntities
    async with _Conn(creds) as c:
        # ۱) resolve (هنوز چیزی ارسال نشده ⇒ هر خطا قطعی است)
        peer = None
        last: Exception | None = None
        for target in ([username] if username else []) + ([int(tg_id)] if tg_id else []):
            try:
                peer = await asyncio.wait_for(c.get_input_entity(target), _CALL_TIMEOUT)
                break
            except Exception as e:  # noqa: BLE001
                last = e
        if peer is None:
            raise AccGiftError("کاربرِ مقصد برایِ اکانتِ پشتیبانی پیدا نشد"
                               + (" (یوزرنیم ندارد و اکانت قبلاً با او در تماس نبوده)" if not username else "")
                               + (f": {_classify(last)}" if last else "") + ".")
        msg = TextWithEntities(text=text[:128], entities=[]) if text else None
        inv = InputInvoiceStarGift(peer=peer, gift_id=gid, hide_name=bool(hide_name) or None, message=msg)
        # ۲) فرم (هنوز پولی کم نشده ⇒ قطعی)
        try:
            form = await asyncio.wait_for(c(GetPaymentFormRequest(invoice=inv)), _CALL_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            raise AccGiftError(_classify(e)) from e
        # ۳) پرداخت/ارسال — اینجا قطعِ شبکه یعنی «نامشخص»
        try:
            await asyncio.wait_for(c(SendStarsFormRequest(form_id=form.form_id, invoice=inv)), _CALL_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, OSError) as e:
            raise AccGiftError(f"پاسخِ تلگرام نرسید ({type(e).__name__}); ممکن است گیفت ارسال شده باشد.",
                               definite=False) from e
        except Exception as e:  # noqa: BLE001
            from telethon.errors import RPCError
            raise AccGiftError(_classify(e), definite=isinstance(e, RPCError)) from e


# === PATCH_ACC_STAR_REACTION_V1 ===
_REACTION_MAX_PER_CALL = 2500
_POST_RE = None


def parse_post_link(link: str) -> tuple[str, int] | None:
    """https://t.me/<channel>/<id> ⇒ (channel, id)؛ لینکِ نامعتبر ⇒ None."""
    import re
    m = re.match(r"^https?://t\.me/([A-Za-z0-9_]{4,32})/(\d+)/?$", (link or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


_rid_last = 0.0
_rid_offset = 0


def _paid_random_id() -> int:
    """random_idِ ری‌اکشنِ استارزی باید «زمان‌محور» باشد (مثلِ msg_idِ MTProto: ثانیه << 32 + شمارنده)؛ عددِ تصادفیِ خام
    را تلگرام با RANDOM_ID_EXPIRED رد می‌کند."""
    import time
    global _rid_last, _rid_offset
    now = time.time()
    _rid_offset = _rid_offset + 4 if now == _rid_last else 0
    _rid_last = now
    return int(now * 2 ** 32) + _rid_offset


def _reaction_privacy(anonymous: bool):
    """نسخه‌هایِ جدیدِ Telethon نوعِ PaidReactionPrivacy دارند؛ قدیمی‌ها فقط Bool."""
    try:
        from telethon.tl.types import PaidReactionPrivacyAnonymous, PaidReactionPrivacyDefault
        return PaidReactionPrivacyAnonymous() if anonymous else PaidReactionPrivacyDefault()
    except ImportError:
        return True if anonymous else None


async def send_paid_reaction(bot_id: int, *, link: str, count: int, anonymous: bool = False,
                             on_chunk=None) -> None:
    """``count`` ری‌اکشنِ استارزی رویِ پستِ ``link`` می‌گذارد (هر ری‌اکشن = ۱ ⭐ از موجودیِ اکانتِ پشتیبانی).

    هر فراخوانی حداکثر ۲۵۰۰ ⭐ است؛ تعدادِ بیشتر تکه‌تکه می‌رود. بعد از هر تکهٔ موفق ``on_chunk(done_so_far)`` صدا زده
    می‌شود تا صدازننده پیشرفت را ذخیره کند. خطایِ قطعی (RPCError = ارسال نشده) با نامشخص (قطعِ شبکه/تایم‌اوت) جدا است؛
    روی نامشخص هرگز retry نکن.
    """
    parsed = parse_post_link(link)
    if not parsed:
        raise AccGiftError("لینکِ پست معتبر نیست (باید به شکلِ https://t.me/channel/123 باشد).")
    if int(count) < 1:
        raise AccGiftError("تعدادِ ری‌اکشن معتبر نیست.")
    creds = await _creds(bot_id)
    if not creds:
        raise AccGiftError("اکانتِ پشتیبانی وصل نشده.")
    channel, msg_id = parsed
    try:
        from telethon.tl.functions.messages import SendPaidReactionRequest
    except ImportError as e:
        raise AccGiftError("نسخهٔ telethon قدیمی است و ری‌اکشنِ استارزی ندارد (pip install -U telethon).") from e
    async with _Conn(creds) as c:
        try:
            peer = await asyncio.wait_for(c.get_input_entity(channel), _CALL_TIMEOUT)
        except Exception as e:  # noqa: BLE001 — هنوز چیزی ارسال نشده
            raise AccGiftError(f"کانالِ پست برایِ اکانتِ پشتیبانی پیدا نشد: {_classify(e)}") from e
        left, done = int(count), 0
        while left > 0:
            n = min(left, _REACTION_MAX_PER_CALL)
            req = SendPaidReactionRequest(peer=peer, msg_id=msg_id, count=n,
                                          random_id=_paid_random_id(), private=_reaction_privacy(anonymous))
            try:
                await asyncio.wait_for(c(req), _CALL_TIMEOUT)
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                raise AccGiftError(f"پاسخِ تلگرام نرسید ({type(e).__name__}); ممکن است ری‌اکشن ثبت شده باشد.",
                                   definite=False) from e
            except Exception as e:  # noqa: BLE001
                from telethon.errors import RPCError
                raise AccGiftError(_classify(e), definite=isinstance(e, RPCError)) from e
            left -= n
            done += n
            if on_chunk is not None:
                await on_chunk(done)
