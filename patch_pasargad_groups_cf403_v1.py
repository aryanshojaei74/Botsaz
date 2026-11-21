#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_pasargad_groups_cf403_v1.py — رفعِ دو خطایی که فروشنده (WHALE SUPPORT) گزارش داد.

مشکل ۱) «ورود به پنل ناموفق بود (HTTP 403)» + کلِ HTML صفحهٔ کلادفلر
    علت: کلادفلرِ جلوی پنلِ فروشنده، درخواستِ لاگینِ سرورِ ربات را بلاک می‌کند
    (Bot Fight Mode / WAF / Country block / آی‌پیِ دیتاسنتر). یوزر/پسورد/آدرس
    مشکلی ندارد، ولی پیامِ فعلی این را نمی‌گوید و HTML خام نشان می‌دهد.
    این پچ: امضایِ کلادفلر را در login() تشخیص می‌دهد و به‌جایِ HTML، پیامِ
    واضح و قابلِ‌اقدام می‌دهد.  (خودِ بلاک را فقط صاحبِ پنل می‌تواند رفع کند.)

مشکل ۲) با «همه» در پاسارگاد: «هیچ inbound فعالی روی پنل پیدا نشد»
    علت: در پاسارگاد (PasarGuard) کاربر به «گروه» وصل می‌شود، نه مستقیم به
    inbound. اینباند فعال در پنل کافی نیست؛ باید حداقل یک «گروه» ساخته شده
    باشد. اگر گروهی نباشد (یا ادمین اجازهٔ خواندنش را نداشته باشد)، لیست خالی
    برمی‌گردد و ربات پیامِ گمراه‌کنندهٔ «inbound» می‌دهد.
    این پچ: (الف) list_groups شکل‌های بیشتری از پاسخ را می‌پذیرد و دلیلِ خالی‌بودن
    را ثبت می‌کند؛ (ب) پیامِ ربات برایِ پاسارگاد دقیقاً می‌گوید «گروه» بساز.

استفاده:
    cd /opt/botsaz
    python3 patch_pasargad_groups_cf403_v1.py           # فقط بررسی (dry-run)
    python3 patch_pasargad_groups_cf403_v1.py --apply   # اعمال + بک‌آپ + py_compile
    systemctl restart botsaz-tenant                     # بعد از اعمال

idempotent است: دنبالِ MARKER می‌گردد و دوباره اعمال نمی‌کند.
قبل از تغییرِ هر فایل، <file>.bak_before_pasargad_groups_cf403_v1 ساخته می‌شود؛
اگر py_compile شکست بخورد، فایل از بک‌آپ برمی‌گردد.
"""
import py_compile
import shutil
import sys

MARKER = "PATCH_PASARGAD_GROUPS_CF403_V1"
BAK_SUFFIX = ".bak_before_pasargad_groups_cf403_v1"

F_MARZBAN = "tenant/shared/panel_clients/marzban_panel.py"
F_PASARGAD = "tenant/shared/panel_clients/pasargad_panel.py"
F_ADMIN = "tenant/bots/vpn/vpn_admin.py"

# ───────────────────────── ۱) کلادفلر در login ─────────────────────────
OLD_CF = (
    "            if status != 200:\n"
    "                # === PATCH_FRONTEND_404_HINT_V1 ===\n"
)
NEW_CF = (
    "            # === PATCH_PASARGAD_GROUPS_CF403_V1 ===\n"
    "            # ۴۰۳ِ کلادفلر: صفحهٔ خطایِ کلادفلر (نه پاسخِ API پنل). یوزر/پسورد\n"
    "            # مشکلی ندارد؛ کلادفلر آی‌پیِ سرورِ ربات را بلاک کرده. به‌جایِ\n"
    "            # نمایشِ HTML خام، پیامِ قابلِ‌اقدام بده.\n"
    "            if status in (403, 503):\n"
    "                _low = (text or \"\")[:4000].lower()\n"
    "                _srv = (headers.get(\"Server\", \"\") or \"\").lower()\n"
    "                if (\"cloudflare\" in _srv or headers.get(\"CF-RAY\") is not None\n"
    "                        or \"cf-ray\" in _low or \"attention required\" in _low\n"
    "                        or \"just a moment\" in _low or \"cf-chl\" in _low\n"
    "                        or \"no-js ie6 oldie\" in _low):\n"
    "                    raise PanelError(\n"
    "                        f\"کلادفلرِ جلوی پنل، درخواستِ ورودِ رباتِ ما را بلاک کرد \"\n"
    "                        f\"(HTTP {status} از Cloudflare). مشکلی در آدرس/یوزر/پسورد \"\n"
    "                        \"نیست.\\n\\n\"\n"
    "                        \"راه‌حل (باید صاحبِ پنل انجام دهد — یکی از این‌ها):\\n\"\n"
    "                        \"۱) Cloudflare ← Security ← WAF ← Tools ← IP Access Rules: \"\n"
    "                        \"آی‌پیِ سرورِ ربات را Allow کن\\n\"\n"
    "                        \"۲) Bot Fight Mode / Under Attack Mode / بلاکِ کشور را برایِ \"\n"
    "                        \"مسیرِ /api/ خاموش کن\\n\"\n"
    "                        \"۳) دامنه‌ای بده که پروکسیِ کلادفلر (ابرِ نارنجی) ندارد، یا \"\n"
    "                        \"آدرسِ مستقیمِ IP:PORT پنل\")\n"
    "            if status != 200:\n"
    "                # === PATCH_FRONTEND_404_HINT_V1 ===\n"
)

# ───────────────────────── ۲) list_groups در پاسارگاد ─────────────────────────
OLD_GROUPS = (
    "        for path in (\"/api/groups\", \"/api/groups/simple\"):\n"
    "            try:\n"
    "                res = await self._request(\"GET\", path) or {}\n"
    "            except PanelError as e:\n"
    "                if \"HTTP 403\" in str(e) or \"HTTP 401\" in str(e):\n"
    "                    continue\n"
    "                raise\n"
    "            if isinstance(res, dict):\n"
    "                items = res.get(\"groups\") or []\n"
    "            elif isinstance(res, list):\n"
    "                items = res\n"
    "            else:\n"
    "                items = []\n"
    "            if items:\n"
    "                return items\n"
    "        return []\n"
)
NEW_GROUPS = (
    "        # === PATCH_PASARGAD_GROUPS_CF403_V1 ===\n"
    "        # علتِ خالی‌بودنِ لیست را ثبت می‌کنیم (self._groups_diag) تا ربات به‌جایِ\n"
    "        # «inbound پیدا نشد»ِ گمراه‌کننده، دلیلِ واقعی را نشان دهد.\n"
    "        self._groups_diag = \"\"\n"
    "        _notes = []\n"
    "        for path in (\"/api/groups\", \"/api/groups/simple\"):\n"
    "            try:\n"
    "                res = await self._request(\"GET\", path)\n"
    "            except PanelError as e:\n"
    "                if \"HTTP 403\" in str(e) or \"HTTP 401\" in str(e):\n"
    "                    _notes.append(f\"{path}: دسترسی رد شد (ادمین sudo نیست؟)\")\n"
    "                    continue\n"
    "                raise\n"
    "            if res is None:\n"
    "                _notes.append(f\"{path}: ۴۰۴ (این مسیر روی این نسخه از پنل نیست)\")\n"
    "                continue\n"
    "            items = []\n"
    "            if isinstance(res, dict):\n"
    "                for _k in (\"groups\", \"items\", \"data\", \"results\"):\n"
    "                    if isinstance(res.get(_k), list):\n"
    "                        items = res[_k]\n"
    "                        break\n"
    "            elif isinstance(res, list):\n"
    "                items = res\n"
    "            if items:\n"
    "                return items\n"
    "            _notes.append(f\"{path}: پاسخ آمد ولی لیستِ گروه‌ها خالی بود\")\n"
    "        self._groups_diag = \" | \".join(_notes)\n"
    "        return []\n"
)

# ───────────────────────── ۳) پیامِ ربات برایِ «همه» ─────────────────────────
OLD_MSG = (
    "            if not all_ids:\n"
    "                await msg.answer(\n"
    "                    \"⚠️ هیچ inbound فعالی روی پنل پیدا نشد. دوباره بفرست:\",\n"
    "                    reply_markup=cancel_kb())\n"
    "                return\n"
)
NEW_MSG = (
    "            if not all_ids:\n"
    "                # === PATCH_PASARGAD_GROUPS_CF403_V1 ===\n"
    "                if _bk(key) in (\"pasargad\", \"pasarguard\"):\n"
    "                    _why = _html_escape_mod.escape(\n"
    "                        str(getattr(probe, \"_groups_diag\", \"\") or \"\"))[:300]\n"
    "                    await msg.answer(\n"
    "                        \"⚠️ لاگین موفق بود، ولی هیچ <b>گروهی</b> روی پنل پاسارگاد \"\n"
    "                        \"پیدا نشد.\\n\\n\"\n"
    "                        \"در پاسارگاد کاربر به «گروه» وصل می‌شود، نه مستقیم به \"\n"
    "                        \"inbound؛ پس فعال‌بودنِ inbound کافی نیست. در پنل: \"\n"
    "                        \"<b>Groups ← Create Group</b> را بزن، inboundهای موردنظر را \"\n"
    "                        \"داخلش انتخاب کن و ذخیره کن. بعد دوباره «همه» بفرست \"\n"
    "                        \"(یا آی‌دیِ گروه، مثل <code>1</code>).\\n\\n\"\n"
    "                        \"اگر گروه داری و باز هم این پیام آمد، ادمینِ واردشده باید \"\n"
    "                        \"<b>sudo</b> باشد.\"\n"
    "                        + (f\"\\n\\nجزئیات: <code>{_why}</code>\" if _why else \"\"),\n"
    "                        reply_markup=cancel_kb())\n"
    "                    return\n"
    "                await msg.answer(\n"
    "                    \"⚠️ هیچ inbound فعالی روی پنل پیدا نشد. دوباره بفرست:\",\n"
    "                    reply_markup=cancel_kb())\n"
    "                return\n"
)

PATCHES = [
    (F_MARZBAN, OLD_CF, NEW_CF),
    (F_PASARGAD, OLD_GROUPS, NEW_GROUPS),
    (F_ADMIN, OLD_MSG, NEW_MSG),
]


def main():
    apply = "--apply" in sys.argv
    texts = {}
    plan = []
    for path, old, new in PATCHES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                t = f.read()
        except FileNotFoundError:
            print(f"❌ فایل پیدا نشد: {path} (از /opt/botsaz اجرا کن)")
            sys.exit(1)
        texts[path] = t
        if MARKER in t:
            print(f"✅ قبلاً اعمال شده: {path}")
            continue
        n = t.count(old)
        if n != 1:
            print(f"❌ {path}: بلوکِ هدف {n} بار پیدا شد (انتظار: ۱). فایل تغییر کرده؛ "
                  "چیزی نوشته نشد.")
            sys.exit(1)
        plan.append((path, old, new))

    if not plan:
        print("✅ همهٔ بخش‌ها قبلاً اعمال شده؛ کاری برای انجام نیست.")
        return

    if not apply:
        for path, _, _ in plan:
            print(f"🔎 dry-run: {path} قابلِ پچ است.")
        print("برایِ اعمالِ واقعی: --apply")
        return

    done = []
    try:
        for path, old, new in plan:
            shutil.copy2(path, path + BAK_SUFFIX)
            done.append(path)
            with open(path, "w", encoding="utf-8") as f:
                f.write(texts[path].replace(old, new, 1))
            py_compile.compile(path, doraise=True)
            print(f"✅ اعمال شد: {path}")
    except Exception as e:
        print(f"❌ خطا: {e} — برگرداندنِ فایل‌ها از بک‌آپ…")
        for path in done:
            shutil.copy2(path + BAK_SUFFIX, path)
        sys.exit(1)

    print("\nحالا سرویس را ری‌استارت کن:  systemctl restart botsaz-tenant")


if __name__ == "__main__":
    main()
