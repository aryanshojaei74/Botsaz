#!/usr/bin/env python3
"""
PATCH_REFERRAL_JOIN_NOTIFY_V1
اطلاع‌رسانیِ ساده به دعوت‌کننده وقتی یه دوست با لینک رفرالش وارد ربات
می‌شود — مستقل از فعال‌بودنِ پاداش. قبلاً فقط وقتی حالتِ پاداش روی
«هنگامِ عضویت» بود و مبلغِ پاداش هم صفر نبود، پیامی می‌رفت.

استفاده:
    cd /opt/botsaz
    python3 patch_referral_join_notify.py            # dry-run
    python3 patch_referral_join_notify.py --apply
    systemctl restart botsaz-tenant
"""
import sys
import py_compile
from pathlib import Path

TARGET = Path("tenant/shared/extras.py")

OLD_1 = '''        ref.join_count += 1
        ref.click_count += 1
        inviter_user_id = ref.user_id
        await s.commit()'''

NEW_1 = '''        ref.join_count += 1
        ref.click_count += 1
        inviter_user_id = ref.user_id
        inviter_row = await s.get(BotUser, inviter_user_id)
        inviter_tg_for_notify = inviter_row.telegram_id if inviter_row else None
        await s.commit()'''

OLD_2 = '''    mode = await _eget(bot_id, "referral_reward_mode", "join")
    if mode != "join":
        return
    reward = await _get_referral_reward(bot_id)
    if reward <= 0:
        return'''

NEW_2 = '''    mode = await _eget(bot_id, "referral_reward_mode", "join")
    if mode != "join":
        # === PATCH_REFERRAL_JOIN_NOTIFY_V1 ===
        # حالتِ «پاداش فقط بعد از خرید»: دعوت‌کننده تا الان هیچ پیامی
        # دربارهٔ پیوستنِ زیرمجموعه‌اش نمی‌گرفت. حالا یک اطلاع‌رسانیِ سادهٔ
        # بدونِ پاداش فرستاده می‌شود تا فوراً از پیوستنِ دوستش باخبر شود.
        if inviter_tg_for_notify:
            try:
                await bot.send_message(
                    inviter_tg_for_notify,
                    "🎉 یک دوست با لینک دعوت شما وارد ربات شد!"
                )
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    reward = await _get_referral_reward(bot_id)
    if reward <= 0:
        # === PATCH_REFERRAL_JOIN_NOTIFY_V1 ===
        # پاداشِ رفرال غیرفعال است؛ همان اطلاع‌رسانیِ سادهٔ بدونِ پاداش.
        if inviter_tg_for_notify:
            try:
                await bot.send_message(
                    inviter_tg_for_notify,
                    "🎉 یک دوست با لینک دعوت شما وارد ربات شد!"
                )
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return'''


def main():
    apply = "--apply" in sys.argv
    if not TARGET.exists():
        print(f"❌ فایل پیدا نشد: {TARGET}")
        sys.exit(1)

    src = TARGET.read_text(encoding="utf-8")

    problems = []
    if src.count(OLD_1) != 1:
        problems.append(f"بخشِ اول دقیقاً یک‌بار پیدا نشد (یافت‌شده: {src.count(OLD_1)}).")
    if src.count(OLD_2) != 1:
        problems.append(f"بخشِ دوم دقیقاً یک‌بار پیدا نشد (یافت‌شده: {src.count(OLD_2)}).")
    if NEW_1 in src or NEW_2 in src:
        problems.append("به‌نظر می‌رسد این پچ قبلاً اعمال شده.")

    if problems:
        print("⚠️ پچ قابلِ اعمال نیست:")
        for p in problems:
            print(f"   - {p}")
        print("\nفایل را دستی بررسی کن یا با من هماهنگ کن.")
        sys.exit(2)

    print("✅ فایل دقیقاً مطابقِ انتظاره؛ پچ قابلِ اعماله.")
    if not apply:
        print("این dry-run بود. برایِ نوشتنِ واقعی: --apply")
        return

    new_src = src.replace(OLD_1, NEW_1).replace(OLD_2, NEW_2)
    backup = TARGET.with_suffix(".py.bak_before_referral_join_notify")
    backup.write_text(src, encoding="utf-8")
    TARGET.write_text(new_src, encoding="utf-8")
    print(f"   بک‌آپ گرفته شد: {backup}")
    print(f"   نوشته شد: {TARGET}")

    try:
        py_compile.compile(str(TARGET), doraise=True)
        print("   py_compile: OK ✅")
    except py_compile.PyCompileError as e:
        print(f"   ❌ خطای سینتکس بعد از پچ: {e}")
        print("   بازگردانی از بک‌آپ...")
        TARGET.write_text(src, encoding="utf-8")
        sys.exit(1)

    print("\nبعدش سرویسِ tenant را ری‌استارت کن:\n    systemctl restart botsaz-tenant")


if __name__ == "__main__":
    main()
