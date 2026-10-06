#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_wg_sub_fallback_v1.py — گزارشِ WHALE SUPPORT: «ساب خالی می‌فرسته، بدون فایل و کانفیگ»
(ربات @Gamersping_bot، ساب وایرگاردی روی پنلِ پاسارگاد پشتِ کلادفلر).

بررسی: ساب‌لینکِ مشتری از بیرون سالم است و ۴ لینکِ wireguard:// دارد. دو مشکل در ربات:

۱) وابستگیِ کامل به لاگینِ API پنل.  دکمهٔ «🛡 دریافت کانفیگ‌های وایرگارد» (پیامِ تحویل) و
   خودِ ارسالِ فایل‌ها فقط با get_client_links(email) ساخته می‌شوند که اول به API پنل
   لاگین می‌کند. اگر کلادفلرِ پنل لاگینِ سرورِ ربات را ۴۰۳ کند (در لاگ‌ها برای همین ربات
   بارها دیده شد)، استثنا بی‌صدا بلعیده می‌شود ← نه دکمه، نه فایل؛ فقط ساب‌لینک
   (که اپِ وایرگارد اصلاً نمی‌تواند ایمپورتش کند) می‌ماند = «ساب خالی».
   این پچ: اگر API شکست خورد/خالی بود، محتوای ساب‌لینکِ ذخیره‌شدهٔ اکانت (بدونِ نیاز به
   لاگین) خوانده و از آن فایل‌ها ساخته می‌شود. اگر آن هم نشد، خطای اولیه دوباره بالا می‌آید.

۲) PresharedKey جا می‌افتاد.  لینک‌هایِ ساب فیلدِ presharedkey دارند ولی
   _parse_wireguard_uri آن را در فایلِ .conf نمی‌نوشت؛ فایل ایمپورت می‌شد ولی وصل
   نمی‌شد (پنلِ سمتِ سرور PSK را لازم دارد).

استفاده:
    cd /opt/botsaz
    python3 patch_wg_sub_fallback_v1.py           # dry-run
    python3 patch_wg_sub_fallback_v1.py --apply   # اعمال + بک‌آپ (.bak_before_wg_sub_fallback_v1)
    systemctl restart botsaz-tenant               # (و سرویس‌هایِ tenant دیگر اگر جدا هستند)
idempotent؛ قبل از نوشتن ast.parse می‌زند؛ اگر الگو نخواند چیزی نمی‌نویسد.
"""
import ast
import shutil
import sys

PATH = "tenant/bots/vpn/vpn_user.py"
APPLY = "--apply" in sys.argv

EDITS = [
    # ---- ۲) PresharedKey در پارسر
    (
'''        keepalive = _q("keepalive")
        dns = _q("dns")
        if not (private_key and public_key and address and host and port):
''',
'''        keepalive = _q("keepalive")
        dns = _q("dns")
        # === PATCH_WG_SUB_FALLBACK_V1 === presharedkey قبلاً دور ریخته می‌شد
        preshared = _q("presharedkey") or _q("preshared_key")
        if not (private_key and public_key and address and host and port):
'''),
    (
'''        lines += ["", "[Peer]", f"PublicKey = {public_key}",
                  f"AllowedIPs = {allowed_ips}", f"Endpoint = {host}:{port}"]
''',
'''        lines += ["", "[Peer]", f"PublicKey = {public_key}"]
        if preshared:  # === PATCH_WG_SUB_FALLBACK_V1 ===
            lines.append(f"PresharedKey = {preshared}")
        lines += [f"AllowedIPs = {allowed_ips}", f"Endpoint = {host}:{port}"]
'''),

    # ---- ۱) هلپر با فالبک به ساب
    (
'''import re as _re


# === PATCH_PANEL_DEFAULT_IP_LIMIT_V1 ===
''',
'''# === PATCH_WG_SUB_FALLBACK_V1 ===
async def _links_with_sub_fallback(client, email: str, sub_link: str) -> list[str]:
    """get_client_links از API پنل؛ اگر شکست خورد (مثلاً ۴۰۳ِ کلادفلر روی لاگین)
    یا خالی بود، محتوایِ ساب‌لینکِ ذخیره‌شدهٔ اکانت (بدونِ لاگین) خوانده می‌شود.
    اگر فالبک هم نتیجه نداد، خطای اولیه (در صورتِ وجود) دوباره بالا می‌آید."""
    first_exc = None
    try:
        links = await client.get_client_links(email)
        if links:
            return links
    except Exception as e:  # noqa: BLE001
        first_exc = e
        logger.exception("get_client_links از API پنل شکست خورد؛ فالبک به ساب‌لینک")
    if sub_link and hasattr(client, "_fetch_links_from_sub"):
        try:
            fb = await client._fetch_links_from_sub(sub_link)
            if fb:
                return fb
        except Exception:  # noqa: BLE001
            logger.exception("فالبکِ خواندنِ ساب‌لینک هم شکست خورد")
    if first_exc is not None:
        raise first_exc
    return []


import re as _re


# === PATCH_PANEL_DEFAULT_IP_LIMIT_V1 ===
'''),

    # ---- پیامِ تحویل: چکِ دکمهٔ وایرگارد
    (
'''            try:
                _links_wg = await _client_wg.get_client_links(acc_email)
            finally:
                await _client_wg.close()
''',
'''            try:
                _links_wg = await _links_with_sub_fallback(_client_wg, acc_email, sub_link)  # PATCH_WG_SUB_FALLBACK_V1
            finally:
                await _client_wg.close()
'''),

    # ---- هندلرِ ارسالِ فایل‌ها
    (
'''        email = a.email
        panel = await _panel_for_account(s, row.id, a)
    await cb.answer("در حال دریافت کانفیگِ وایرگارد…")
''',
'''        email = a.email
        _acc_sub_link = a.sub_link or ""  # PATCH_WG_SUB_FALLBACK_V1
        panel = await _panel_for_account(s, row.id, a)
    await cb.answer("در حال دریافت کانفیگِ وایرگارد…")
'''),
    (
'''                # همهٔ لوکیشن‌ها شکست خوردند؛ به مسیرِ قدیمی برو (پایینِ همین تابع)
            links = await client.get_client_links(email)
''',
'''                # همهٔ لوکیشن‌ها شکست خوردند؛ به مسیرِ قدیمی برو (پایینِ همین تابع)
            links = await _links_with_sub_fallback(client, email, _acc_sub_link)  # PATCH_WG_SUB_FALLBACK_V1
'''),
]


def main():
    with open(PATH, encoding="utf-8") as f:
        src = f.read()
    out = src
    done = 0
    for old, new in EDITS:
        if new in out:
            done += 1
            continue
        if out.count(old) != 1:
            sys.exit(f"❌ الگو {out.count(old)} بار پیدا شد (انتظار ۱) — فایلِ سرور با نسخهٔ "
                     f"تحلیل‌شده فرق دارد؛ چیزی نوشته نشد:\n{old[:120]!r}")
        out = out.replace(old, new)
    if done == len(EDITS):
        print("✅ قبلاً اعمال شده؛ کاری نمانده.")
        return
    ast.parse(out, filename=PATH)
    print("✔ ast.parse OK")
    if not APPLY:
        print("dry-run: چیزی نوشته نشد. برای اعمال --apply بزن.")
        return
    shutil.copy2(PATH, PATH + ".bak_before_wg_sub_fallback_v1")
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(out)
    print("✅ اعمال شد. سرویسِ tenant را ری‌استارت کن.")


if __name__ == "__main__":
    main()
