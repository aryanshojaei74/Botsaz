"""
ton_autosend.py — «ارسالِ خودکارِ تون» برایِ فروشِ تون (PATCH_TON_AUTOSEND_V1) با TON Center API v2.

قبلاً فروشِ تون همیشه دستی بود (مشتری می‌خرد ← ادمین خودش تون را می‌فرستد ← «ارسال شد»). حالا مالک می‌تواند «ارسالِ خودکار» را روشن کند:
بعد از پرداختِ مشتری، ربات تون را از **کیف‌پولِ حضانتیِ همین ربات** (FragmentWallet؛ همان که برایِ استارزِ خودکار ساخته می‌شود) به آدرسِ مشتری می‌فرستد.

TON Center API v2 (https://docs.ton.org/api/v2/overview) — فقط سه مسیر:
  GET  /getAddressBalance       موجودیِ کیف‌پول        (fragment_stars.fetch_balance_ton)
  GET  /getWalletInformation    seqno                  (fragment_stars._get_seqno)
  POST /sendBoc                 ارسالِ تراکنشِ امضاشده  (fragment_stars._send_boc)
امضا با tonsdk و seedِ رمزنگاری‌شده (wallet_crypto) انجام می‌شود؛ هیچ کلیدی از ربات بیرون نمی‌رود.

ایمنی (پایِ پولِ واقعی — محافظه‌کارانه):
  • پیش‌فرض خاموش؛ فقط مالک (نه ادمینِ فرعی) روشنش می‌کند؛ سقفِ هر سفارش (پیش‌فرض ۱۰ TON).
  • فقط آدرسِ استانداردِ معتبر (نه «.ton»)، فقط سفارشِ awaiting_admin، با «قفلِ ردیف» (status=sending) تا دو بار نرود.
  • موجودی ≥ مبلغ + ذخیرهٔ کارمزد؛ و برایِ هر ربات یک قفل: تراکنش‌ها پشتِ هم با seqnoِ تازه (منتظرِ افزایشِ seqno) — دو ارسالِ هم‌زمان با یک seqno ممکن نیست.
  • هر خطا ⇒ هرگز تلاشِ دوباره‌یِ خودکار. خطایِ «قبل از ارسال» ⇒ سفارش به مسیرِ دستی برمی‌گردد (awaiting_admin) و ادمین خبر می‌گیرد.
    خطایِ «نامشخص» (ارسال شد ولی seqno جلو نرفت) ⇒ status=sending می‌ماند، ارسالِ خودکارِ بعدی ۱۵ دقیقه متوقف می‌شود و ادمین باید روی بلاک‌چین
    چک کند و «ارسال شد» یا «بازپرداخت» را بزند (تا ارسالِ دوباره نشود).
"""
from __future__ import annotations

import asyncio
import base64
import time

from sqlalchemy import select

from config import Session
from models import BotUser, FragmentWallet, GiftOrder

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

DEFAULT_MAX_TON = 10.0
CAPS = (1.0, 5.0, 10.0, 25.0, 50.0, 100.0)
FEE_RESERVE_TON = 0.05
CONFIRM_WAIT_S = 45
CONFIRM_POLL_S = 3
PAUSE_S = 900
_TON_CENTI = 100

_LOCKS: dict[int, asyncio.Lock] = {}
_PAUSED_UNTIL: dict[int, float] = {}


def _lock(bot_id: int) -> asyncio.Lock:
    return _LOCKS.setdefault(int(bot_id), asyncio.Lock())


def paused(bot_id: int) -> bool:
    return _PAUSED_UNTIL.get(int(bot_id), 0) > time.monotonic()


async def _get_setting(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def settings(bot_id: int) -> dict:
    """هر خطایِ خواندنِ تنظیمات ⇒ «خاموش» (ارسالِ پول هرگز به‌خاطرِ خطا روشن نمی‌ماند)."""
    try:
        if (await _get_setting(bot_id, "ton_autosend", "0")) != "1":
            return {"enabled": False, "max": DEFAULT_MAX_TON}
        try:
            cap = float(await _get_setting(bot_id, "ton_autosend_max", str(DEFAULT_MAX_TON)) or DEFAULT_MAX_TON)
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_TON
        return {"enabled": True, "max": cap}
    except Exception:      # noqa: BLE001
        logger.exception("ton_autosend: خواندنِ تنظیمات ناموفق بود (bot=%s)", bot_id)
        return {"enabled": False, "max": DEFAULT_MAX_TON}


def valid_address(addr: str | None) -> bool:
    """فقط آدرسِ استاندارد (user-friendly یا raw)؛ دامنهٔ «.ton» برایِ ارسالِ خودکار پشتیبانی نمی‌شود."""
    a = (addr or "").strip()
    if not a or a.lower().endswith(".ton"):
        return False
    try:
        from tonsdk.utils import Address
        Address(a)
        return True
    except Exception:      # noqa: BLE001
        return False


async def wallet_info(bot_id: int) -> tuple[str | None, float | None]:
    """(آدرسِ کیف‌پولِ حضانتی, موجودیِ زنده)؛ اگر کیفی نیست (None, None)."""
    from tenant.bots.virtual_service import fragment_stars as FS
    async with Session() as s:
        w = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == bot_id))
    if not w:
        return None, None
    return w.address, await FS.fetch_balance_ton(w.address)


async def find_tx_hash(wallet_addr: str, to_addr: str, amount_ton: float) -> str | None:
    """هشِ تراکنشِ خروجیِ همین ارسال را از TON Center (getTransactions) پیدا می‌کند: آخرین تراکنشِ کیف‌پول که پیامِ خروجی‌اش به همان مقصد و همان مبلغ است.
    نیافتن/خطا ⇒ None (فقط دکمهٔ «هشِ تراکنش» نمایش داده نمی‌شود؛ ارسال تأثیر نمی‌پذیرد)."""
    try:
        from tonsdk.utils import Address, to_nano
        from tenant.bots.virtual_service import fragment_stars as FS
        data, err = await FS._toncenter_call("GET", f"{FS.TONCENTER_BASE}/getTransactions",
                                             params={"address": wallet_addr, "limit": 10})
        if not isinstance(data, dict) or not data.get("ok"):
            return None
        want_dest = Address(to_addr).to_string(False)
        want_val = int(to_nano(float(amount_ton), "ton"))
        for tx in data.get("result") or []:
            for m in tx.get("out_msgs") or []:
                try:
                    if Address(m.get("destination") or "").to_string(False) == want_dest and int(m.get("value") or 0) == want_val:
                        h = (tx.get("transaction_id") or {}).get("hash")
                        return base64.b64decode(h).hex() if h else None
                except Exception:      # noqa: BLE001
                    continue
    except Exception:      # noqa: BLE001
        logger.exception("ton_autosend: یافتنِ هشِ تراکنش ناموفق بود")
    return None


async def send_ton(bot_id: int, to_addr: str, amount_ton: float, comment: str | None = None) -> tuple[str, str, str | None]:
    """ارسالِ TON از کیف‌پولِ حضانتی. خروجی: ("ok"|"fail"|"unconfirmed", پیام, هشِ تراکنش|None).
    fail = قطعاً ارسال نشده (امن برایِ بازگشت به دستی)؛ unconfirmed = ممکن است رفته باشد — خودکار تکرار نمی‌شود."""
    from tenant.bots.virtual_service import fragment_stars as FS
    try:
        from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        from tonsdk.utils import to_nano
        from wallet_crypto import decrypt_secret
    except Exception as e:      # noqa: BLE001
        return "fail", f"کتابخانهٔ TON در دسترس نیست ({type(e).__name__}).", None
    if amount_ton <= 0:
        return "fail", "مقدارِ نامعتبر.", None
    async with Session() as s:
        w = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == bot_id))
    if not w or not w.enabled:
        return "fail", "کیف‌پولِ حضانتیِ ربات ساخته/فعال نشده (از «💎 Fragment» بساز و شارژ کن).", None
    bal = await FS.fetch_balance_ton(w.address)
    if bal is None:
        return "fail", "موجودیِ کیف‌پول از شبکه خوانده نشد.", None
    if bal < amount_ton + FEE_RESERVE_TON:
        return "fail", f"موجودیِ کیف‌پولِ ربات کافی نیست ({bal:.3f} TON؛ لازم: {amount_ton + FEE_RESERVE_TON:.3f}).", None
    try:
        mnemonic = decrypt_secret(w.encrypted_mnemonic)
    except Exception:      # noqa: BLE001
        logger.exception("ton_autosend: رمزگشاییِ seed نشد (bot=%s)", bot_id)
        return "fail", "کلیدِ کیف‌پول قابلِ بازگشایی نبود.", None
    seqno = await FS._get_seqno(w.address)
    if seqno is None:
        return "fail", "وضعیتِ کیف‌پول (seqno) از شبکه خوانده نشد.", None
    try:
        _m, _pub, _priv, wc = Wallets.from_mnemonics(mnemonic.split(), version=WalletVersionEnum.v4r2, workchain=0)
        q = wc.create_transfer_message(to_addr=to_addr, amount=to_nano(float(amount_ton), "ton"), seqno=seqno,
                                       payload=((comment or "")[:120] or None), send_mode=3)
        boc = base64.b64encode(q["message"].to_boc(False)).decode()
    except Exception as e:      # noqa: BLE001
        logger.exception("ton_autosend: ساختِ تراکنش نشد (bot=%s)", bot_id)
        return "fail", f"ساختِ تراکنش نشد ({type(e).__name__}).", None
    ok, note = await FS._send_boc(boc)
    if not ok:
        # شکستِ sendBoc می‌تواند «تایم‌اوتِ پس از ارسال» هم باشد ⇒ نامشخص، نه قطعاً-نرفته
        return "unconfirmed", f"پاسخِ شبکه به ارسال: {note}", None
    deadline = time.monotonic() + CONFIRM_WAIT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(CONFIRM_POLL_S)
        nxt = await FS._get_seqno(w.address)
        if nxt is not None and nxt > seqno:
            return "ok", "تراکنش در شبکه ثبت شد.", await find_tx_hash(w.address, to_addr, amount_ton)
    return "unconfirmed", "تراکنش ارسال شد ولی تأییدِ ثبت در شبکه (seqno) نیامد.", None


async def _forced_cap(bot_id: int) -> float:
    try:
        return float(await _get_setting(bot_id, "ton_autosend_max", str(DEFAULT_MAX_TON)) or DEFAULT_MAX_TON)
    except (TypeError, ValueError):
        return DEFAULT_MAX_TON


async def deliver_order(bot, row, order_id: int, force: bool = False) -> tuple[str, str]:
    """تلاش برایِ تحویلِ خودکارِ یک سفارشِ تون. هرگز استثنا نمی‌دهد. force=True: «🤖 ارسالِ خودکارِ مجدد» توسطِ مالک (خاموش‌بودنِ تنظیم/توقفِ موقت را نادیده می‌گیرد؛ سقف و بقیهٔ شرط‌ها می‌مانند).
    خروجی: ("disabled"|"manual"|"sent"|"unconfirmed", پیام). manual/unconfirmed ⇒ فراخوان باید ادمین را خبر کند."""
    try:
        cfg = await settings(row.id)
        if not cfg["enabled"] and not force:
            return "disabled", ""
        if cfg["enabled"] is False:                  # force روی رباتی که سقف ندارد ⇒ سقفِ پیش‌فرض
            cfg = {**cfg, "max": (await _forced_cap(row.id))}
        if paused(row.id) and not force:
            return "manual", "ارسالِ خودکار به‌خاطرِ یک خطایِ نامشخصِ اخیر موقتاً متوقف است؛ دستی بفرست."
        async with _lock(row.id):
            async with Session() as s:
                o = await s.get(GiftOrder, order_id, with_for_update=True)
                if (not o or o.bot_id != row.id or not (o.gift_id or "").startswith("tonsell_")
                        or o.status != "awaiting_admin"):
                    return "manual", "سفارش در وضعیتِ قابلِ‌ارسال نیست."
                amount = (o.star_count or 0) / _TON_CENTI
                addr = (o.dest_label or "").strip()
                memo = (o.dest_memo or "").strip() or None
                if amount <= 0 or amount > cfg["max"]:
                    return "manual", f"مقدار ({amount:g} TON) از سقفِ ارسالِ خودکار ({cfg['max']:g} TON) بیشتر است."
                if not valid_address(addr):
                    return "manual", "آدرسِ مقصد برایِ ارسالِ خودکار پشتیبانی نمی‌شود (مثلاً دامنهٔ .ton)."
                o.status = "sending"          # قفلِ ردیف: از این لحظه هیچ مسیرِ دیگری ارسال نمی‌کند
                o.deliver_via = "auto"
                await s.commit()
            res, note, txh = await send_ton(row.id, addr, amount, memo)
            async with Session() as s:
                o = await s.get(GiftOrder, order_id, with_for_update=True)
                if o is not None and o.status == "sending":
                    if res == "ok":
                        o.status = "sent"
                        o.fail_reason = None
                        o.tx_hash = txh
                    elif res == "fail":
                        o.status = "awaiting_admin"        # قطعاً نرفته ⇒ مسیرِ دستی
                        o.deliver_via = ""
                        o.fail_reason = note[:500]
                    else:
                        o.fail_reason = ("⚠️ نامشخص: " + note)[:500]     # status=sending می‌ماند تا ادمین چک کند
                    await s.commit()
            if res == "unconfirmed":
                _PAUSED_UNTIL[int(row.id)] = time.monotonic() + PAUSE_S
            if res == "ok":
                return "sent", note
            return ("manual" if res == "fail" else "unconfirmed"), note
    except Exception as e:      # noqa: BLE001
        logger.exception("ton_autosend: خطایِ غیرمنتظره (bot=%s order=%s)", getattr(row, "id", None), order_id)
        # اگر خطا بعد از قفلِ ردیف رخ داده باشد، وضعیتِ سفارش را نامشخص نگه می‌داریم (امن‌تر از بازگرداندن)
        sending = False
        try:
            async with Session() as s:
                o = await s.get(GiftOrder, order_id)
                sending = bool(o and o.status == "sending")
        except Exception:      # noqa: BLE001
            logger.exception("ton_autosend: خواندنِ وضعیتِ سفارش پس از خطا ناموفق بود")
        return ("unconfirmed" if sending else "manual"), f"خطایِ غیرمنتظره: {type(e).__name__}"
