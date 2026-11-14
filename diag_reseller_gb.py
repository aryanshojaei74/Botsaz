#!/usr/bin/env python3
"""
تشخیصِ مستقیمِ تعرفهٔ گیگی/روزانهٔ «نمایندگیِ خودکار» (rs_price_gb /
rs_price_day) — همون مکانیزمی که پنلِ اختصاصیِ نمایندگی (web_panel.py،
مسیرِ /panel/r/custom) موقعِ فروش به‌جای قیمتِ عادی استفاده می‌کنه.

برخلافِ diag_reseller.py (که فقط پلهٔ درصدیِ reseller_enabled/reseller_tiers
رو نگاه می‌کنه)، این اسکریپت دقیقاً همون مسیرِ کدی رو که هنگامِ فروش از
پنلِ نماینده اجرا می‌شه، برایِ یک نمایندهٔ مشخص شبیه‌سازی می‌کنه.

فقط می‌خونه، چیزی رو تغییر نمی‌ده.

اجرا (از داخلِ /opt/botsaz):
    python3 diag_reseller_gb.py <bot_id> <telegram_id_نماینده>
"""
import asyncio
import sys


async def main():
    if len(sys.argv) < 3:
        print("Usage: python3 diag_reseller_gb.py <bot_id> <telegram_id نماینده>")
        return

    bot_id = int(sys.argv[1])
    tg_id = int(sys.argv[2])

    from config import Session
    from models import VpnPartner, VpnPanel
    from sqlalchemy import select
    from tenant import get_setting
    from tenant.bots.vpn.vpn_user import _vc_all_for_user, _plan_audience_filter, _vc
    from tenant.bots.vpn.vpn_partners import partner_discount_percent

    print("=" * 70)

    # ۱) وضعیتِ VpnPartner همین کاربر برایِ همین بات
    async with Session() as s:
        p = await s.scalar(select(VpnPartner).where(
            VpnPartner.bot_id == bot_id, VpnPartner.telegram_id == tg_id))
        panels = list((await s.scalars(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id))).all())

    if not p:
        print(f"❌ هیچ رکوردِ VpnPartner برایِ bot_id={bot_id}, telegram_id={tg_id} پیدا نشد.")
        print("   یعنی این کاربر از نظرِ سیستم اصلاً «نماینده» نیست؛")
        print("   پس _is_rs همیشه False می‌شه و rs_price_gb/day هیچ‌وقت اعمال نمی‌شه.")
    else:
        print(f"👤 VpnPartner پیدا شد → active={p.active}  web_enabled={p.web_enabled}  "
              f"web_username={p.web_username!r}")
        if not p.active:
            print("   ⚠️  اینجا مشکله: active=False → _plan_audience_filter نمایندگی رو "
                  "نمی‌بینه، پس تعرفهٔ گیگی اعمال نمی‌شه.")

    # ۲) خروجیِ واقعیِ _plan_audience_filter (دقیقاً همون تابعی که تصمیم می‌گیره)
    _, is_rs = await _plan_audience_filter(bot_id, tg_id)
    print(f"\n🔎 _plan_audience_filter → is_rs={is_rs}")
    if not is_rs:
        print("   ⚠️  چون is_rs=False است، شرطِ «if pct <= 0 and is_dedicated_panel» هیچ‌وقت "
              "به بخشِ خوندنِ rs_price_gb/day نمی‌رسه — قیمت همیشه پایه/اصلی می‌مونه.")

    # ۳) تخفیفِ اختصاصیِ دستی (اگه باشه، اولویت داره و rs_price_gb رو خنثی می‌کنه)
    if not panels:
        print("\n⚠️ هیچ پنلِ VPN برایِ این بات ثبت نشده.")
    for pnl in panels:
        pct = await partner_discount_percent(bot_id, tg_id, pnl.panel_key, "custom",
                                             include_auto_reseller=False)
        rs_g = (await _vc(bot_id, "rs_price_gb", pnl.panel_key)).strip()
        rs_d = (await _vc(bot_id, "rs_price_day", pnl.panel_key)).strip()
        print(f"\n📦 پنل: {pnl.panel_key}")
        print(f"   تخفیفِ اختصاصیِ دستیِ همین همکار (pct) → {pct}")
        print(f"   rs_price_gb (خام)  → {rs_g!r}")
        print(f"   rs_price_day (خام) → {rs_d!r}")
        if pct > 0:
            print("   ⚠️  چون تخفیفِ اختصاصیِ دستی > 0 است، این اولویت دارد و rs_price_gb/day "
                  "برای این پنل نادیده گرفته می‌شود (طبق PATCH_PARTNER_CUSTOM_PRICE_PRIORITY_V1).")
        if not rs_g.isdigit() or int(rs_g) <= 0:
            print("   ⚠️  rs_price_gb تنظیم نشده (صفر/خالی) → تعرفهٔ گیگی برایِ این پنل اعمال نمی‌شه.")
        if not rs_d.isdigit() or int(rs_d) <= 0:
            print("   ℹ️  rs_price_day تنظیم نشده (صفر/خالی) → فقط تعرفهٔ روزانه اعمال نمی‌شه "
                  "(اگه فروش گیگی/پلنی صرفاً بر اساسِ حجم باشه، مهم نیست).")

        # ۴) شبیه‌سازیِ دقیقِ همون فراخوانی‌ای که پنلِ نماینده انجام می‌ده
        cfg_dedicated = await _vc_all_for_user(bot_id, tg_id, pnl.panel_key,
                                               is_dedicated_panel=True)
        cfg_normal = await _vc_all_for_user(bot_id, tg_id, pnl.panel_key,
                                            is_dedicated_panel=False)
        print(f"   💰 قیمتِ نهایی از مسیرِ «پنلِ نماینده» (is_dedicated_panel=True)  → "
              f"هر گیگ {cfg_dedicated['vc_price_gb']} / هر روز {cfg_dedicated['vc_price_day']}")
        print(f"   💰 قیمتِ نهایی از مسیرِ «ربات عادی» (is_dedicated_panel=False) → "
              f"هر گیگ {cfg_normal['vc_price_gb']} / هر روز {cfg_normal['vc_price_day']}")
        if cfg_dedicated['vc_price_gb'] == cfg_normal['vc_price_gb']:
            print("   ⚠️  این دو عدد یکسان‌اند — یعنی تعرفهٔ ویژهٔ نمایندگی هیچ اثری نداشت؛ "
                  "علتش یکی از موردهای بالاست (is_rs=False، rs_price_gb تنظیم‌نشده، یا "
                  "تخفیفِ دستیِ اختصاصیِ همین همکار جایگزینش شده).")

    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
