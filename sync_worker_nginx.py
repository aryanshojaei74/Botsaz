#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_worker_nginx.py — نسخهٔ عمومیِ «نقشهٔ ریستارتِ ایزوله»: برایِ هر
نوعِ رباتی که یک ورکرِ جدا دارد (مثلِ vpn، virtual_service، …)، همهٔ
ربات‌هایِ همان نوع را در nginx به پورتِ همان ورکر وصل می‌کند.

=== PATCH_TENANT_WORKER_ISOLATION_GENERIC_V1 ===
(جایگزینِ sync_vpn_worker_nginx.py که فقط VPN را می‌شناخت.)

استفاده:
    cd /opt/botsaz
    python3 sync_worker_nginx.py <field> <port>              # dry-run
    sudo python3 sync_worker_nginx.py <field> <port> --apply
    sudo python3 sync_worker_nginx.py <field> <port> --remove

مثال‌هایِ <field> (دقیقاً همان مقداری که در ستونِ bots.field ذخیره شده):
    vpn_sales        (پورتِ پیشنهادی: 8011)
    virtual_service  (پورتِ پیشنهادی: 8012)
    digital_shop     (پورتِ پیشنهادی: 8013)
    booking          (پورتِ پیشنهادی: 8014)
    community_mgmt   (پورتِ پیشنهادی: 8015)

⚠️ «ad_bot» فایلِ اختصاصیِ خودش را ندارد (bot-type-i مستقل نیست؛ فقط
پیش‌تنظیمِ فروشگاهِ دیجیتال + تبلیغِ زمان‌بندی‌شده است) — رباتِ ad_bot را
به همان ورکرِ digital_shop بفرست (field='digital_shop' نزن، ولی اگر
خواستی ad_bot را هم همراهِ همان ورکر ببری، این اسکریپت را با یک SQL
دستی یا فیلترِ سفارشی صدا بزن؛ به‌صورتِ پیش‌فرض این اسکریپت هر بار فقط
دقیقاً یک مقدارِ field را می‌فرستد).

⚠️ برایِ safety عمداً خودش reload نمی‌زند — بعد از --apply، خودت با دقت:
    sudo nginx -t   ← اگر OK نبود، STOP و به من خبر بده
    sudo systemctl reload nginx
"""
from __future__ import annotations

import asyncio
import re
import shutil
import sys

FIELD = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else ""
PORT = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 0
APPLY = "--apply" in sys.argv
REMOVE = "--remove" in sys.argv

NGINX_PATH = "/etc/nginx/sites-available/botsaz"
MARKER_TAG = f"TENANT_WORKER_ISOLATION_{FIELD.upper()}_V1"
MARKER_START = f"# === PATCH_{MARKER_TAG} START ==="
MARKER_END = f"# === PATCH_{MARKER_TAG} END ==="
UPSTREAM = f"http://127.0.0.1:{PORT}"
ANCHOR = "location ~ ^/webhook/ {"
TOKEN_RE = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{30,50}$")


def _dsn() -> str:
    with open(".env", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^DATABASE_URL=(.+)$", line.strip())
            if m:
                return m.group(1).strip().strip('"').replace(
                    "postgresql+asyncpg://", "postgresql://", 1)
    raise SystemExit("DATABASE_URL در .env پیدا نشد")


async def fetch_tokens() -> list[tuple[int, str, str | None]]:
    import asyncpg
    con = await asyncpg.connect(_dsn())
    try:
        rows = await con.fetch(
            "SELECT id, token, username FROM bots "
            "WHERE field=$1 AND archived_at IS NULL "
            "ORDER BY id", FIELD)
        return [(r["id"], r["token"], r["username"]) for r in rows]
    finally:
        await con.close()


def build_block(tokens: list[tuple[int, str, str | None]]) -> str:
    lines = [MARKER_START,
             f"# ساخته‌شده توسط sync_worker_nginx.py — {len(tokens)} رباتِ {FIELD}",
             "# دستی ویرایش نکن؛ دوباره اجرایِ اسکریپت رویِ این بخش را عوض می‌کند."]
    bad = 0
    for bot_id, token, username in tokens:
        if not TOKEN_RE.match(token or ""):
            bad += 1
            continue
        uname = f" ({username})" if username else ""
        lines.append(f"    # bot #{bot_id}{uname}")
        lines.append(f"    location = /webhook/{token} {{")
        lines.append(f"        proxy_pass {UPSTREAM};")
        lines.append("        proxy_set_header Host $host;")
        lines.append("        proxy_set_header X-Real-IP $remote_addr;")
        lines.append("        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;")
        lines.append("        proxy_set_header X-Forwarded-Proto $scheme;")
        lines.append("    }")
    lines.append(MARKER_END)
    if bad:
        print(f"⚠️  {bad} توکن با فرمتِ عجیب رد شد (برایِ ایمنی وارد nginx نشد).")
    return "\n    ".join(lines) + "\n"


def strip_old_block(text: str) -> tuple[str, bool]:
    if MARKER_START not in text:
        return text, False
    s = text.index(MARKER_START)
    e = text.index(MARKER_END, s) + len(MARKER_END)
    while e < len(text) and text[e] in "\n":
        e += 1
    return text[:s] + text[e:], True


def insert_after_anchor(text: str, block: str) -> str:
    if ANCHOR not in text:
        raise SystemExit(f"❌ الگویِ «{ANCHOR}» در فایل پیدا نشد — دستی باید اضافه شود.")
    if text.count(ANCHOR) > 1:
        raise SystemExit("❌ الگو بیش از یک‌بار تکرار شده — برایِ ایمنی متوقف شدم.")
    start = text.index(ANCHOR)
    brace_pos = text.index("{", start)
    depth, i = 1, brace_pos + 1
    while depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[:i] + "\n    " + block + text[i:]


def main() -> None:
    # === PATCH_PREFLIGHT_FALSE_POSITIVE_FIX_V1 ===
    # اعتبارسنجیِ واقعیِ آرگومان‌ها این‌جاست، نه سطحِ ماژول — preflight.py
    # هر ماژول را فقط import می‌کند تا سینتکسش را چک کند؛ اگر sys.exit
    # همان لحظهٔ import اجرا شود، preflight به‌اشتباه این را «اسکریپتِ
    # پچِ یک‌بارمصرف» تشخیص می‌دهد. حالا فقط با اجرایِ مستقیم (نه import)
    # این چک واقعاً اجرا می‌شود.
    if not FIELD or not PORT:
        sys.exit(__doc__)
    tokens = asyncio.run(fetch_tokens()) if not REMOVE else []
    print(f"ربات‌هایِ فعالِ field=\'{FIELD}\' در دیتابیس: {len(tokens)}   →   پورتِ {PORT}")
    for bot_id, token, username in tokens[:10]:
        masked = token[:10] + "…" + token[-6:]
        print(f"   #{bot_id}  @{username or '?'}  {masked}")
    if len(tokens) > 10:
        print(f"   … و {len(tokens) - 10} تایِ دیگر")

    if not APPLY and not REMOVE:
        print("\nاین dry-run بود؛ هیچ فایلی تغییر نکرد. برایِ اعمال: sudo python3 ... --apply")
        return

    with open(NGINX_PATH, encoding="utf-8") as f:
        text = f.read()
    text, had_old = strip_old_block(text)

    if REMOVE:
        if not had_old:
            print("ℹ️  بلوکی برایِ حذف پیدا نشد؛ کاری لازم نبود.")
            return
        backup = NGINX_PATH + f".bak_remove_{FIELD}"
        shutil.copy2(NGINX_PATH, backup)
        with open(NGINX_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"✅ بلوکِ {FIELD} حذف شد. بک‌آپ: {backup}")
        print("حالا: sudo nginx -t   ←  اگر OK بود: sudo systemctl reload nginx")
        return

    if not tokens:
        print(f"ℹ️  هیچ رباتِ {FIELD} فعالی پیدا نشد؛ چیزی برایِ اضافه‌کردن نیست.")
        return

    block = build_block(tokens)
    new_text = insert_after_anchor(text, block)

    backup = NGINX_PATH + f".bak_sync_{FIELD}"
    shutil.copy2(NGINX_PATH, backup)
    with open(NGINX_PATH, "w", encoding="utf-8") as f:
        f.write(new_text)

    print(f"\n✅ {len(tokens)} رباتِ {FIELD} به {UPSTREAM} وصل شدند "
          f"({'جایگزینِ بلوکِ قبلی شد' if had_old else 'بلوکِ تازه'}).")
    print(f"📦 بک‌آپ: {backup}")
    print("حالا حتماً به‌ترتیب:")
    print("   sudo nginx -t          ← اگر خطا داد STOP و به من بگو")
    print("   sudo systemctl reload nginx")


if __name__ == "__main__":
    main()
