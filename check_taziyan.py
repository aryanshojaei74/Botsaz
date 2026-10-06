"""
check_taziyan.py — اسکریپتِ تشخیصی، فقط خواندنی (چیزی را تغییر نمی‌دهد).

اجرا از ریشه‌ی پروژه (همون‌جایی که config.py و models.py هستن):
    cd /opt/botsaz
    source venv/bin/activate
    python3 check_taziyan.py @Taziyanteambot

نشون می‌ده: bot_id، تعداد دکمه‌ها (پلن‌ها)، تعداد ردیف‌های Settings و
کلیدهاشون، پنل‌های VPN وصل‌شده، و تعداد کاربر/سفارش (که طبقِ کد نباید با
ریست پاک شده باشن).
"""
import asyncio
import sys

from sqlalchemy import func, select

from config import Session
from models import Bot as BotRow, MainButton, Setting, VpnPanel, BotUser, Order


async def main(identifier: str):
    async with Session() as s:
        if identifier.lstrip("@").isdigit():
            row = await s.get(BotRow, int(identifier))
        else:
            uname = identifier.lstrip("@")
            row = await s.scalar(select(BotRow).where(BotRow.username == uname))

        if not row:
            print(f"❌ رباتی با مشخصات «{identifier}» پیدا نشد.")
            return

        bot_id = row.id
        print(f"✅ پیدا شد: @{row.username}  (bot_id={bot_id}, status={row.status}, plan={row.plan})")
        print("-" * 50)

        n_btn = await s.scalar(select(func.count()).select_from(MainButton)
                                .where(MainButton.bot_id == bot_id)) or 0
        print(f"🎛 تعداد دکمه‌ها/پلن‌های فعلی (MainButton): {n_btn}")

        n_set = await s.scalar(select(func.count()).select_from(Setting)
                                .where(Setting.bot_id == bot_id)) or 0
        print(f"⚙️ تعداد ردیف‌های Settings فعلی: {n_set}")

        keys = (await s.scalars(select(Setting.key).where(Setting.bot_id == bot_id))).all()
        print(f"   کلیدهای موجود: {sorted(keys) if keys else '(هیچ‌کدام — یعنی احتمالاً ریست شده)'}")

        panels = (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == bot_id))).all()
        print(f"🌐 پنل‌های VPN ثبت‌شده: {len(panels)}")
        for p in panels:
            print(f"   - {p.panel_key} | enabled={p.enabled} | base_url={p.base_url}")

        n_users = await s.scalar(select(func.count()).select_from(BotUser)
                                  .where(BotUser.bot_id == bot_id)) or 0
        n_orders = await s.scalar(select(func.count()).select_from(Order)
                                   .where(Order.bot_id == bot_id)) or 0
        print(f"👤 کاربران ثبت‌شده: {n_users}   🧾 سفارش‌های ثبت‌شده: {n_orders}")

    print("-" * 50)
    if n_btn == 0 and n_set == 0:
        print("🔎 نتیجه: هم دکمه‌ها و هم تنظیمات صفرن — با نشونه‌های ریستِ کامل (scope=all) کاملاً هم‌خوانی داره.")
    elif n_btn == 0 and n_set > 0:
        print("🔎 نتیجه: دکمه‌ها صفرن ولی تنظیمات هست — بیشتر شبیهِ ریستِ «فقط دکمه‌ها»ست.")
    else:
        print("🔎 نتیجه: دکمه/تنظیمات صفر نیستن — پس این یه ریستِ کاملِ کلاسیک نبوده؛ باید دنبال دلیلِ دیگه گشت.")


if __name__ == "__main__":
    ident = sys.argv[1] if len(sys.argv) > 1 else "@Taziyanteambot"
    asyncio.run(main(ident))
