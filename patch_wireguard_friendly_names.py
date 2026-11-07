#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_wireguard_friendly_names.py — نمایشِ اسمِ واقعیِ لوکیشن رویِ فایل‌هایِ
وایرگاردِ ارسالی از ربات، دقیقاً مثلِ چیزی که تویِ صفحهٔ ساب (سابسکریپشن)
دیده می‌شه (مثلاً «Turkey1-Gaming»، «Germany-N1»، …) به‌جایِ اسم‌هایِ
بی‌معنیِ فعلی («wg2-94.conf»، «wg3-185.conf»).

علتِ ریشه‌ای:
اسمِ فایل و لیبلِ کپشن الان فقط از خطِ «Endpoint = host:port» داخلِ خودِ
کانفیگ ساخته می‌شه (_wg_endpoint_label / _wg_short_filename). وقتی
Endpoint یه دامنه باشه (مثلِ wiregerman.asan-ps.ir) نتیجه قابلِ‌فهمه،
ولی وقتی Endpoint یه آی‌پیِ خام باشه (مثلِ 94.183.227.198 — دقیقاً حالتِ
گزارش‌شده)، تنها چیزی که از رویِ اولین «.» جدا می‌شه همون اکتتِ اولِ
آی‌پیه: «94»، «185»، … بی‌معنی.

درحالی‌که خودِ لینکِ wireguard:// که پنل می‌ده معمولاً اسمِ واقعیِ
لوکیشن رو به‌صورتِ فرگمنتِ #name (دقیقاً مثلِ سایرِ پروتکل‌ها —
vless://...#Germany-N1) همراه داره؛ این فرگمنت الان تویِ
_parse_wireguard_uri کلاً نادیده گرفته می‌شه و دور ریخته می‌شه.

این پچ:
  ۱) _parse_wireguard_uri را طوری تغییر می‌ده که اگه فرگمنتِ #name تویِ
     لینکِ wireguard:// بود، به‌عنوانِ یه خطِ کامنتِ «# Name: ...» در
     ابتدایِ خودِ فایلِ .conf ذخیره‌ش کنه (وایرگارد خطِ # رو نادیده
     می‌گیره، پس ایمپورت هیچ‌جوره خراب نمی‌شه).
  ۲) یه تابعِ کمکیِ _wg_conf_name اضافه می‌کنه که این کامنت رو می‌خونه.
  ۳) _wg_short_filename و هر دو جایِ ساختِ کپشن (ارسالِ تکی + ارسالِ
     همه‌یِ لینک‌ها) رو طوری عوض می‌کنه که اگه این اسمِ واقعی موجود بود
     ترجیحش بده؛ اگه نبود (لینکِ خام/بدونِ فرگمنت) دقیقاً مثلِ قبل از
     رویِ Endpoint حدس بزنه — یعنی هیچ رگرسیونی برایِ پنل‌هایی که این
     فرگمنت رو نمی‌فرستن وجود نداره.

⚠️ این پچ یه فرض داره که تأییدش نیاز به دیتایِ واقعیِ پنلِ همین ربات
داره (که من بهش دسترسی ندارم): اینکه پنلِ متصل به این ربات واقعاً
فرگمنتِ #name رو تویِ لینکِ wireguard:// می‌فرسته. اگه این‌طور نباشه،
پچ کاملاً بی‌خطره و دقیقاً رفتارِ قبلی (Endpoint-based) رو حفظ می‌کنه —
فقط مشکل حل نمی‌شه. بعدِ اجرا، برایِ همون کاربر یه کانفیگِ وایرگارد
بگیر و ببین اسمِ فایل/کپشن عوض شده یا نه؛ اگه نشده یعنی فرضِ بالا برایِ
این پنل درست نبوده و باید دیدِ دیگه‌ای (کدِ خودِ پنل) رو چک کرد.

استفاده:
    cd /opt/botsaz
    python3 patch_wireguard_friendly_names.py           # فقط چک می‌کند (dry-run)
    python3 patch_wireguard_friendly_names.py --apply   # واقعاً می‌نویسد

قبلش خودش یک بک‌آپِ tenant/bots/vpn/vpn_user.py.bak_before_wgnames می‌سازد.
اجرا کردنش دوباره (بعد از اعمال) بی‌خطر است: می‌بیند پچ قبلاً خورده و
کاری نمی‌کند (idempotent — دنبالِ MARKER_WG_FRIENDLY_NAME_V1 می‌گردد).
"""
import sys

PATH = "tenant/bots/vpn/vpn_user.py"
MARKER = "MARKER_WG_FRIENDLY_NAME_V1"

# ───────── بخشِ ۱: _parse_wireguard_uri — گرفتنِ اسم از فرگمنتِ لینک ─────────
OLD_PARSE = (
    'def _parse_wireguard_uri(uri: str) -> str | None:\n'
    '    from urllib.parse import urlparse, parse_qs, unquote\n'
    '    try:\n'
    '        u = urlparse(uri)\n'
    '        if u.scheme != "wireguard":\n'
    '            return None\n'
    '        private_key = unquote(u.username or "")\n'
    '        host = u.hostname or ""\n'
    '        port = u.port\n'
    '        qs = parse_qs(u.query)\n'
    '        def _q(key, default=""):\n'
    '            v = qs.get(key)\n'
    '            return unquote(v[0]) if v else default\n'
    '        public_key = _q("publickey")\n'
    '        address = _q("address")\n'
    '        mtu = _q("mtu")\n'
    '        allowed_ips = _q("allowedips", "0.0.0.0/0")\n'
    '        keepalive = _q("keepalive")\n'
    '        dns = _q("dns")\n'
    '        if not (private_key and public_key and address and host and port):\n'
    '            return None\n'
    '        lines = ["[Interface]", f"PrivateKey = {private_key}", f"Address = {address}"]\n'
    '        if dns:\n'
    '            lines.append(f"DNS = {dns}")\n'
    '        if mtu:\n'
    '            lines.append(f"MTU = {mtu}")\n'
    '        lines += ["", "[Peer]", f"PublicKey = {public_key}",\n'
    '                  f"AllowedIPs = {allowed_ips}", f"Endpoint = {host}:{port}"]\n'
    '        if keepalive:\n'
    '            lines.append(f"PersistentKeepalive = {keepalive}")\n'
    '        return "\\n".join(lines)\n'
    '    except Exception:\n'
    '        logger.exception("پارسِ wireguard:// ناموفق بود")\n'
)
NEW_PARSE = (
    f'def _parse_wireguard_uri(uri: str) -> str | None:\n'
    '    from urllib.parse import urlparse, parse_qs, unquote\n'
    '    try:\n'
    '        u = urlparse(uri)\n'
    '        if u.scheme != "wireguard":\n'
    '            return None\n'
    '        private_key = unquote(u.username or "")\n'
    '        host = u.hostname or ""\n'
    '        port = u.port\n'
    '        qs = parse_qs(u.query)\n'
    '        def _q(key, default=""):\n'
    '            v = qs.get(key)\n'
    '            return unquote(v[0]) if v else default\n'
    '        public_key = _q("publickey")\n'
    '        address = _q("address")\n'
    '        mtu = _q("mtu")\n'
    '        allowed_ips = _q("allowedips", "0.0.0.0/0")\n'
    '        keepalive = _q("keepalive")\n'
    '        dns = _q("dns")\n'
    '        if not (private_key and public_key and address and host and port):\n'
    '            return None\n'
    f'        # === {MARKER} ===\n'
    '        # پنل معمولاً اسمِ واقعیِ لوکیشن (همونی که تویِ صفحهٔ ساب دیده\n'
    '        # می‌شه، مثلِ «Turkey1-Gaming») رو به‌صورتِ فرگمنتِ #name تویِ\n'
    '        # همین لینکِ wireguard:// می‌ذاره — قبلاً کلاً نادیده گرفته\n'
    '        # می‌شد. اگه باشه، به‌عنوانِ یه کامنتِ اولِ فایل نگه‌ش می‌داریم\n'
    '        # تا برایِ اسمِ فایل/کپشن استفاده بشه؛ وایرگارد خطِ # رو\n'
    '        # نادیده می‌گیره پس ایمپورت شکسته نمی‌شه.\n'
    '        _name = unquote(u.fragment or "").strip()\n'
    '        lines = ["[Interface]", f"PrivateKey = {private_key}", f"Address = {address}"]\n'
    '        if dns:\n'
    '            lines.append(f"DNS = {dns}")\n'
    '        if mtu:\n'
    '            lines.append(f"MTU = {mtu}")\n'
    '        lines += ["", "[Peer]", f"PublicKey = {public_key}",\n'
    '                  f"AllowedIPs = {allowed_ips}", f"Endpoint = {host}:{port}"]\n'
    '        if keepalive:\n'
    '            lines.append(f"PersistentKeepalive = {keepalive}")\n'
    '        text = "\\n".join(lines)\n'
    '        if _name:\n'
    '            text = f"# Name: {_name}\\n" + text\n'
    '        return text\n'
    '    except Exception:\n'
    '        logger.exception("پارسِ wireguard:// ناموفق بود")\n'
)

# ───────── بخشِ ۲: تابعِ کمکیِ خواندنِ اسم + اولویت‌دادن در _wg_short_filename ─────────
OLD_SHORTFN = (
    'def _wg_endpoint_label(cfg_text: str) -> str | None:\n'
    '    """میزبان (و در صورتِ وجود، پورت) رویِ خطِ Endpointِ یک کانفیگِ\n'
    '    وایرگارد را برمی‌گرداند — مثلاً \'wiregerman.asan-ps.ir:51820\'."""\n'
    '    m = _WG_ENDPOINT_RE.search(cfg_text or "")\n'
    '    if not m:\n'
    '        return None\n'
    '    host = m.group(1)\n'
    '    port = m.group(2)\n'
    '    return f"{host}:{port}" if port else host\n'
    '\n'
    '\n'
    '# === PATCH_WG_SHORT_FILENAME_V1 ===\n'
)
NEW_SHORTFN = (
    'def _wg_endpoint_label(cfg_text: str) -> str | None:\n'
    '    """میزبان (و در صورتِ وجود، پورت) رویِ خطِ Endpointِ یک کانفیگِ\n'
    '    وایرگارد را برمی‌گرداند — مثلاً \'wiregerman.asan-ps.ir:51820\'."""\n'
    '    m = _WG_ENDPOINT_RE.search(cfg_text or "")\n'
    '    if not m:\n'
    '        return None\n'
    '    host = m.group(1)\n'
    '    port = m.group(2)\n'
    '    return f"{host}:{port}" if port else host\n'
    '\n'
    '\n'
    f'# === {MARKER} ===\n'
    '_WG_NAME_RE = re.compile(r"^\\s*#\\s*Name:\\s*(.+?)\\s*$", re.IGNORECASE | re.MULTILINE)\n'
    '\n'
    '\n'
    'def _wg_conf_name(cfg_text: str) -> str | None:\n'
    '    """اسمِ واقعیِ لوکیشن (مثلِ \'Turkey1-Gaming\') اگه به‌صورتِ کامنتِ\n'
    '    «# Name: ...» تویِ متنِ کانفیگ باشه (توسطِ _parse_wireguard_uri از\n'
    '    فرگمنتِ لینکِ پنل اضافه می‌شه). اگه نبود None — یعنی این کانفیگ از\n'
    '    یه لینکِ خام/بدونِ اسم اومده و باید مثلِ قبل از رویِ Endpoint حدس زد."""\n'
    '    m = _WG_NAME_RE.search(cfg_text or "")\n'
    '    return m.group(1).strip() if m else None\n'
    '\n'
    '\n'
    '# === PATCH_WG_SHORT_FILENAME_V1 ===\n'
)

OLD_SLUG = (
    '    حداکثر ۱۵ کاراکتر (بدونِ .conf)."""\n'
    '    label = _wg_endpoint_label(cfg_text)\n'
    '    slug = ""\n'
    '    if label:\n'
    '        host = label.split(":", 1)[0]\n'
    '        first_label = host.split(".", 1)[0]  # فقط زیردامنهٔ متمایزکننده، نه کلِ دامنه\n'
    '        slug = re.sub(r"[^a-zA-Z0-9\\-]+", "", first_label)\n'
)
NEW_SLUG = (
    '    حداکثر ۱۵ کاراکتر (بدونِ .conf)."""\n'
    f'    # === {MARKER} ===\n'
    '    # اگه اسمِ واقعیِ لوکیشن رو داریم (از فرگمنتِ لینکِ پنل، مثلِ\n'
    '    # «Turkey1-Gaming») همونو ترجیح می‌دیم؛ فقط وقتی نبود برمی‌گردیم\n'
    '    # به حدسِ قدیمی از رویِ Endpoint (که برایِ آی‌پیِ خام بی‌معنی بود).\n'
    '    name = _wg_conf_name(cfg_text)\n'
    '    slug = re.sub(r"[^a-zA-Z0-9\\-]+", "", name.replace(" ", "-")) if name else ""\n'
    '    if not slug:\n'
    '        label = _wg_endpoint_label(cfg_text)\n'
    '        if label:\n'
    '            host = label.split(":", 1)[0]\n'
    '            first_label = host.split(".", 1)[0]  # فقط زیردامنهٔ متمایزکننده، نه کلِ دامنه\n'
    '            slug = re.sub(r"[^a-zA-Z0-9\\-]+", "", first_label)\n'
)

# ───────── بخشِ ۳: دو جایِ ساختِ کپشن — ترجیحِ اسمِ واقعی روی لیبلِ IP ─────────
OLD_CAP1 = '            _wg_label = _wg_endpoint_label(link)\n'
NEW_CAP1 = (
    f'            # === {MARKER} ===\n'
    '            _wg_label = _wg_conf_name(link) or _wg_endpoint_label(link)\n'
)

OLD_CAP2 = '        _wg_label = _wg_endpoint_label(cfg_text)\n'
NEW_CAP2 = (
    f'        # === {MARKER} ===\n'
    '        _wg_label = _wg_conf_name(cfg_text) or _wg_endpoint_label(cfg_text)\n'
)

REPLACEMENTS = [
    ("پارسِ wireguard:// (گرفتنِ اسم از فرگمنت)", OLD_PARSE, NEW_PARSE),
    ("تابعِ کمکیِ _wg_conf_name", OLD_SHORTFN, NEW_SHORTFN),
    ("اولویت‌دادنِ اسمِ واقعی در _wg_short_filename", OLD_SLUG, NEW_SLUG),
    ("کپشن/اسم — ارسالِ همه‌یِ لینک‌ها", OLD_CAP1, NEW_CAP1),
    ("کپشن/اسم — ارسالِ تکیِ وایرگارد (vwgcfg)", OLD_CAP2, NEW_CAP2),
]


def main():
    apply = "--apply" in sys.argv
    with open(PATH, "r", encoding="utf-8") as f:
        text = f.read()

    if MARKER in text:
        print("✅ این پچ قبلاً اعمال شده؛ کاری برای انجام نیست.")
        return

    problems = []
    for label, old, new in REPLACEMENTS:
        c = text.count(old)
        if c != 1:
            problems.append(f"«{label}»: پیدا نشد یا تکراریه (count={c})")

    if problems:
        print("❌ فایل با نسخه‌ای که این پچ روش تست شده فرق داره:")
        for p in problems:
            print("   -", p)
        print()
        print("چیزی نوشته نشد. فایل را بفرست بررسی کنیم به‌جایِ اعمالِ کورکورانه.")
        return

    print("✅ فایل دقیقاً مطابقِ انتظاره؛ پچ قابلِ اعماله.")
    if not apply:
        print("این dry-run بود. برایِ نوشتنِ واقعی: --apply")
        return

    import shutil
    backup = PATH + ".bak_before_wgnames"
    shutil.copy(PATH, backup)
    print("   بک‌آپ گرفته شد: %s" % backup)

    new_text = text
    for _label, old, new in REPLACEMENTS:
        new_text = new_text.replace(old, new, 1)

    with open(PATH, "w", encoding="utf-8") as f:
        f.write(new_text)
    print("   نوشته شد: %s" % PATH)

    import py_compile
    try:
        py_compile.compile(PATH, doraise=True)
        print("   py_compile: OK ✅")
    except py_compile.PyCompileError as e:
        print("   ❌ py_compile خطا داد، برگرداندنِ بک‌آپ:")
        print(e)
        shutil.copy(backup, PATH)
        return

    print()
    print("بعدش سرویسِ tenant را ری‌استارت کن:")
    print("    systemctl restart botsaz-tenant")
    print()
    print("⚠️ تستِ لازم: برایِ یه اکانتِ چند-لوکیشنه یه بار «کانفیگِ وایرگارد» رو")
    print("از ربات بگیر. اگه اسمِ فایل/کپشن حالا اسمِ واقعیِ کشور/کاربری رو نشون")
    print("داد، یعنی پنل فرگمنتِ #name رو می‌فرسته و مشکل حل شد. اگه هنوز همون")
    print("wgN-xx قبلیه، یعنی این پنلِ خاص اون فرگمنت رو نمی‌فرسته و باید دید")
    print("دیگه‌ای (مثلِ افزودنِ get_wireguard_endpoints برایِ همین خانوادهٔ پنل،")
    print("مثلِ marzban_panel.py) رو بررسی کرد.")


if __name__ == "__main__":
    main()
