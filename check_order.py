"""
check_order.py — فقط خواندنی. یک سفارش را با شناسه/کد پیدا می‌کند و وضعیتِ
دقیقش + این‌که آیا کانفیگی واقعاً بهش تخصیص/تحویل داده شده یا نه رو نشون
می‌ده.

اجرا:
    cd /opt/botsaz
    source venv/bin/activate
    python3 check_order.py 2317
"""
import asyncio
import sys

from sqlalchemy import select, or_

from config import Session
from models import Order, BotUser, Config, PanelAccount

BOT_ID = 313


async def main(code: str):
    async with Session() as s:
        o = None
        # ۱) اول به‌عنوان آیدیِ عددیِ خودِ سفارش امتحان کن
        if code.isdigit():
            o = await s.get(Order, int(code))
            if o and o.bot_id != BOT_ID:
                o = None

        # ۲) اگر نبود، دنبال tracking_code / invoice_no که شاملِ این کد باشه بگرد
        if not o:
            o = await s.scalar(select(Order).where(
                Order.bot_id == BOT_ID,
                or_(Order.tracking_code.contains(code), Order.invoice_no.contains(code))
            ))

        if not o:
            print(f"❌ هیچ سفارشی با کد/آیدیِ «{code}» برای bot_id={BOT_ID} پیدا نشد.")
            print("   (شاید این کد مالِ جدولِ دیگه‌ای باشه، مثلاً svc_code توی panel_accounts.)")
            pa = await s.scalar(select(PanelAccount).where(
                PanelAccount.bot_id == BOT_ID, PanelAccount.svc_code.contains(code)))
            if pa:
                print(f"   ✅ ولی به‌عنوان svc_code توی panel_accounts پیدا شد: id={pa.id}")
                print(f"      email={pa.email} status={pa.status} user_telegram_id={pa.user_telegram_id}")
                print(f"      volume_gb={pa.volume_gb} days={pa.days} created_at={pa.created_at}")
            return

        u = await s.get(BotUser, o.user_id)
        print("✅ سفارش پیدا شد:")
        print(f"   order.id       = {o.id}")
        print(f"   status         = {o.status}")
        print(f"   pay_method     = {o.pay_method}")
        print(f"   volume_gb/days/count = {o.volume_gb}/{o.days}/{o.count}")
        print(f"   amount         = {o.amount}")
        print(f"   invoice_no     = {o.invoice_no}")
        print(f"   tracking_code  = {o.tracking_code}")
        print(f"   reserved_config_id = {o.reserved_config_id}")
        print(f"   created_at     = {o.created_at}")
        if u:
            print(f"   خریدار: user_id={u.id} telegram_id={u.telegram_id} balance={u.balance}")

        print("-" * 50)
        # آیا کانفیگی (انبار دستی) به این کاربر/سفارش تخصیص داده شده؟
        if o.reserved_config_id:
            cfg = await s.get(Config, o.reserved_config_id)
            if cfg:
                print(f"📦 کانفیگِ رزروشده: id={cfg.id} status={cfg.status} owner_user_id={cfg.owner_user_id}")
            else:
                print("⚠️ reserved_config_id ست شده ولی خودِ ردیفِ Config پیدا نشد (پاک شده؟).")
        else:
            # برای سفارش‌های غیر mcfg_card، دنبال کانفیگی با همین حجم/روز که
            # مالکش همین کاربره بگرد (شاید بعداً assign شده باشه)
            if u:
                cfgs = (await s.scalars(select(Config).where(
                    Config.bot_id == BOT_ID, Config.owner_user_id == u.id,
                    Config.volume_gb == o.volume_gb, Config.days == o.days))).all()
                print(f"📦 تعداد کانفیگ‌های انبارِ متعلق به همین کاربر با همین حجم/روز: {len(cfgs)}")
                for c in cfgs:
                    print(f"   - cfg.id={c.id} status={c.status} code={getattr(c, 'code', None)}")

        # آیا سرویسِ زنده (PanelAccount) هم حوالیِ همون زمان برای همین کاربر ساخته شده؟
        if u:
            from datetime import timedelta
            window_start = o.created_at - timedelta(hours=6)
            window_end = o.created_at + timedelta(hours=6)
            pas = (await s.scalars(select(PanelAccount).where(
                PanelAccount.bot_id == BOT_ID,
                PanelAccount.user_telegram_id == u.telegram_id,
                PanelAccount.created_at >= window_start,
                PanelAccount.created_at <= window_end,
            ))).all()
            print(f"🌐 سرویس‌های زنده‌ی همین کاربر نزدیکِ همون زمان (±۶ساعت): {len(pas)}")
            for p in pas:
                print(f"   - panel_account.id={p.id} svc_code={p.svc_code} status={p.status} "
                      f"volume_gb={p.volume_gb} days={p.days} created_at={p.created_at}")

        print("-" * 50)
        if o.status == "paid" and not o.reserved_config_id:
            print("🔎 نکته: سفارش paid شده ولی reserved_config_id نداره — یعنی یا از انبارِ عمومی "
                  "(بر اساسِ حجم/روز) تخصیص خورده (بالا چک شد)، یا از مسیرِ دیگه‌ای (خریدِ مستقیمِ "
                  "پنل با کیف‌پول) بوده که اصلاً از جدولِ Order/Config نمی‌گذره — در اون صورت باید "
                  "دنبالِ لاگِ [VC_PAY] برای همین کاربر گشت.")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "2317"
    asyncio.run(main(arg))
