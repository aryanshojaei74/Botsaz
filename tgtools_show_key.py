"""
tgtools_show_key.py — کلیدِ TGTools ذخیره‌شدهٔ *یک رباتِ مشخص* را (با تأییدِ دستی) فقط در ترمینالِ خودت چاپ می‌کند،
تا به صاحبش (که خودش خواسته) برگردانی. هیچ‌جا ذخیره/لاگ نمی‌شود.

اجرا:
    cd /opt/botsaz && source venv/bin/activate
    python3 tgtools_show_key.py 1234        # شمارهٔ داخلی، آیدیِ تلگرامِ ربات یا یوزرنیم

⚠️ کلید را فقط به خودِ صاحبِ ربات بده (نه در گروه/چتِ عمومی). TGTools کلید را فقط یک‌بار نشان می‌دهد؛ بعد از تحویل
بهتر است صاحبش در سایتِ TGTools کلیدِ جدید بسازد و در ربات بگذارد.
"""
import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import Bot, Setting


async def _find_bot(s, arg: str):
    """arg می‌تواند شمارهٔ داخلیِ ربات (bots.id)، آیدی عددیِ تلگرامِ ربات (بخشِ اولِ توکن) یا بخشی از یوزرنیم باشد."""
    arg = (arg or "").strip().lstrip("@")
    if arg.isdigit():
        n = int(arg)
        if n <= 2147483647:
            b = await s.get(Bot, n)
            if b:
                return [b]
        return list((await s.scalars(select(Bot).where(Bot.token.like(f"{n}:%")))).all())
    return list((await s.scalars(select(Bot).where(Bot.username.ilike(f"%{arg}%")))).all())


async def main(bot_id: str):
    if not bot_id:
        print("استفاده: python3 tgtools_show_key.py <شمارهٔ داخلی | آیدیِ تلگرامِ ربات | یوزرنیم>")
        return
    async with Session() as s:
        bots = await _find_bot(s, bot_id)
        if len(bots) != 1:
            print("ربات‌های پیداشده:", [(x.id, x.username) for x in bots] or "هیچ", "— دقیق‌تر بنویس.")
            return
        b = bots[0]
        enc = await s.scalar(select(Setting.value).where(Setting.bot_id == b.id, Setting.key == "tgtools_api_key"))
    if not enc:
        print(f"روی ربات {b.id} (@{b.username}) کلیدِ TGTools ذخیره نشده.")
        return
    print(f"ربات {b.id} — @{b.username} (مالک: {b.owner_id})")
    if input("کلیدِ این ربات چاپ شود؟ (برای تأیید yes بنویس): ").strip().lower() != "yes":
        print("لغو شد.")
        return
    from wallet_crypto import decrypt_secret
    try:
        print("\n" + decrypt_secret(enc) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"رمزگشایی ممکن نشد ({type(e).__name__}).")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
