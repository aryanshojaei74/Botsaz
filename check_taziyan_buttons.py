"""
check_taziyan_buttons.py — فقط خواندنی. عنوانِ دقیقِ دکمه‌های فعلیِ
رباتِ taziyan (bot_id=313) رو نشون می‌ده تا معلوم بشه این ۷ تا واقعاً
پلن‌های فروشن یا فقط منوهای عمومی (خانه/بازگشت/تنظیمات و ...).

اجرا:
    cd /opt/botsaz
    source venv/bin/activate
    python3 check_taziyan_buttons.py
"""
import asyncio

from sqlalchemy import select

from config import Session
from models import MainButton, Setting

BOT_ID = 313


async def main():
    async with Session() as s:
        btns = (await s.scalars(
            select(MainButton).where(MainButton.bot_id == BOT_ID)
            .order_by(MainButton.id))).all()
        print(f"🎛 دکمه‌های bot_id={BOT_ID}: {len(btns)} عدد")
        for b in btns:
            # فقط فیلدهای رایج را نشان می‌دهیم؛ اگر مدل فیلد دیگری هم دارد
            # مهم نیست، هدف فقط شناساییِ نوع دکمه‌هاست.
            title = getattr(b, "title", None) or getattr(b, "text", None) or "(بدون‌عنوان)"
            btype = getattr(b, "button_type", None) or getattr(b, "kind", None) or "?"
            print(f"   id={b.id} | title={title!r} | type={btype}")

        print("-" * 50)
        # چند تا از مهم‌ترین تنظیماتِ فروش را (مقدارشان را هم، خلاصه) نشان بده
        important_keys = [
            "card_number", "card_holder", "card_enabled", "card_purchase_enabled",
            "price_gb", "price_day", "vc_pasargad_enabled", "vc_pasargad_price_gb",
            "vc_pasargad_price_day", "receipt_channel_enabled", "receipt_channel_id",
            "auto_approve_receipts",
        ]
        rows = (await s.scalars(select(Setting).where(
            Setting.bot_id == BOT_ID, Setting.key.in_(important_keys)))).all()
        by_key = {r.key: r.value for r in rows}
        print("⚙️ مقدارِ چند تنظیمِ کلیدیِ فروش:")
        for k in important_keys:
            v = by_key.get(k, "❌ (اصلاً ردیفش نیست)")
            if k == "card_number" and v and v not in ("", "❌ (اصلاً ردیفش نیست)"):
                v = v[:4] + "****" + v[-4:] if len(v) >= 8 else "****"
            print(f"   {k} = {v!r}")


if __name__ == "__main__":
    asyncio.run(main())
