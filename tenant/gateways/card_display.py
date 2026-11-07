"""card_display.py — نمایشِ هر دو کارت به مشتری (=== PATCH_CARD_BOTH_V1 ===)

مالک با دکمهٔ «🔀 نمایشِ هر دو کارت به مشتری» (در تنظیماتِ کارت‌به‌کارت) تعیین
می‌کند مشتری فقط کارتِ اصلی را ببیند یا هر دو کارت (اصلی + دوم) را و به
هرکدام که خواست واریز کند. پیش‌فرض خاموش است (رفتارِ قبلی).
"""
from __future__ import annotations

from sqlalchemy import select

from config import Session
from models import Setting


async def _get(bot_id: int, key: str) -> str:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
    return (r.value or "") if r else ""


async def both_enabled(bot_id: int) -> bool:
    return (await _get(bot_id, "card_show_both")) == "1" and bool((await _get(bot_id, "card_number2")).strip())


async def extra_card_block(bot_id: int) -> str:
    """بلوکِ HTMLِ کارتِ دوم برایِ چسباندن زیرِ کارتِ اصلی؛ اگر خاموش بود ''."""
    if not await both_enabled(bot_id):
        return ""
    num = (await _get(bot_id, "card_number2")).strip()
    holder = (await _get(bot_id, "card_holder2")).strip()
    return ("\n🔁 <b>یا کارتِ دوم</b> (به هرکدام خواستی واریز کن):\n"
            f"<code>{num}</code>\n"
            + (f"👤 به نام: <b>{holder}</b>\n" if holder else ""))


async def receipt_dest_values(bot_id: int) -> tuple[str, str, str]:
    """(کارت، شبا، حساب) برایِ تأییدِ خودکارِ رسید؛ اگر هر دو کارت فعال باشند
    مقدارها با «|» جدا می‌شوند و evaluate_receipt هر دو را قبول می‌کند."""
    c, sh, ac = (await _get(bot_id, "card_number"), await _get(bot_id, "card_sheba"),
                 await _get(bot_id, "card_account_number"))
    if await both_enabled(bot_id):
        c2, sh2, ac2 = (await _get(bot_id, "card_number2"), await _get(bot_id, "card_sheba2"),
                        await _get(bot_id, "card_account_number2"))
        c = f"{c}|{c2}" if c2 else c
        sh = f"{sh}|{sh2}" if (sh and sh2) else (sh or sh2)
        ac = f"{ac}|{ac2}" if (ac and ac2) else (ac or ac2)
    return c, sh, ac
