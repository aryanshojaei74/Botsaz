"""
diag_marzban.py — تستِ مستقیمِ ساختِ کانفیگ رویِ یه پنلِ مرزبان، با همون
کدِ دقیقِ خودِ ربات (tenant/marzban_panel.py) — نه یه کدِ جدا.

اجرا (از پوشه‌ی /opt/botsaz):
    python3 diag_marzban.py

خروجی دقیقاً نشون می‌ده: لاگین موفق بود یا نه، چه اینباندهایی رو پنل
واقعاً داره، و اگه ساختِ کانفیگ (۱ گیگ / ۱ ساعته) شکست خورد، دقیقاً چرا.
"""
import asyncio
import sys

sys.path.insert(0, ".")

PANEL_URL = "https://wgmg.dreamdress.ir:8444/"
USERNAME = "gm2oz0r"
PASSWORD = "MT45EHGg3r"

# اگه می‌خوای یه تگِ اینباندِ خاص رو تست کنی (نه «همه»)، اینجا بذار،
# مثلاً ["VLESS TCP REALITY"]. خالی یعنی از همون auto-detect ربات استفاده کن.
INBOUND_TAGS_OVERRIDE = None


async def main():
    from tenant.shared.panel_clients.marzban_panel import MarzbanPanelClient, PanelError

    client = MarzbanPanelClient(
        PANEL_URL, USERNAME, PASSWORD,
        inbound_ids=INBOUND_TAGS_OVERRIDE, verify_ssl=False, timeout=15,
    )

    print("=== ۱) تلاش برایِ لاگین ===")
    try:
        await client.login(force=True)
        print("✅ لاگین موفق بود.")
    except PanelError as e:
        print(f"❌ لاگین ناموفق بود: {e}")
        await client.close()
        return

    print("\n=== ۲) گرفتنِ لیستِ واقعیِ اینباندهایِ این پنل ===")
    try:
        inbounds = await client.list_inbounds()
        if not inbounds:
            print("⚠️ پنل هیچ اینباندی برنگردوند (لیست خالیه).")
        else:
            print(f"✅ {len(inbounds)} اینباند پیدا شد:")
            for ib in inbounds:
                print(f"   - tag={ib.get('tag')!r}  protocol={ib.get('protocol')}")
    except PanelError as e:
        print(f"❌ گرفتنِ لیستِ اینباندها ناموفق بود: {e}")
        await client.close()
        return

    print(f"\n=== ۳) تگ‌هایی که ربات با اون‌ها کانفیگ می‌سازه (inbound_ids override={INBOUND_TAGS_OVERRIDE!r}) ===")
    tags = await client.active_inbound_ids()
    print(f"   تگ‌هایِ هدف: {tags!r}")

    print("\n=== ۴) ساختِ کانفیگِ تستی (۱ گیگ / ۱ ساعت) ===")
    test_email = "diag_test_1h"
    try:
        result = await client.create_account(
            test_email, gb=1, days=0, hours=1,
        )
        print("✅ کانفیگ با موفقیت ساخته شد:")
        print(f"   sub_link: {result.get('sub_link')}")
        print(f"   uuid: {result.get('uuid')}")
        print("\n   (الان همین کاربرِ تستی رو پاک می‌کنم...)")
        try:
            await client.delete_account(result.get("email", test_email))
            print("   ✅ کاربرِ تستی پاک شد.")
        except Exception as de:
            print(f"   ⚠️ پاک‌کردنِ کاربرِ تستی ناموفق بود (دستی پاکش کن): {de}")
    except PanelError as e:
        print(f"❌ ساختِ کانفیگ ناموفق بود:\n{e}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
