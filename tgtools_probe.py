"""
tgtools_probe.py — فقط خواندنی. با کلیدِ TGTools که *از قبل رویِ یک رباتِ مشخص ذخیره (رمزنگاری) شده*، چند درخواستِ GET
می‌زند و «شکلِ پاسخ‌ها» را چاپ می‌کند تا اتصالِ فروش/اجارهٔ گیفت را دقیق بنویسیم.

⚠️ ایمنی:
  • کلید هرگز چاپ/ذخیره نمی‌شود (فقط داخلِ همین پروسه رمزگشایی و در هدرِ درخواست استفاده می‌شود).
  • فقط متدِ GET؛ هیچ خرید/اجاره/تغییری انجام نمی‌شود (در کد هم متدِ دیگری مجاز نیست).
  • مقدارِ رشته‌ها کوتاه می‌شود و هر الگویی شبیهِ tgt_… ماسک می‌شود.

اجرا:
    cd /opt/botsaz && source venv/bin/activate
    python3 tgtools_probe.py express          # بخشی از یوزرنیمِ ربات (یا bot_id)
    # خروجی در tgtools_samples.txt هم ذخیره می‌شود؛ همان فایل را بفرست.
"""
import asyncio
import json
import re
import sys

import aiohttp
from sqlalchemy import select

from config import Session
from models import Bot, Setting

BASE = "https://api.tg-tools.shop"
# فقط GET و فقط این مسیرها
PATHS = [
    "/api/wallet/balance",
    "/api/marketplace/settings",
    "/api/marketplace/browse?limit=2&onSaleOnly=true",
    "/api/marketplace/feed?limit=2",
    "/api/marketplace/public/collections?limit=2",
    "/api/rental/listings",
    "/api/rental/listings/init-params",
    "/api/Purchase/gifts?lang=en",
    "/api/Purchase/prices",
    "/api/Purchase/rates",
    "/api/Purchase/quote?kind=premium&months=3",
]
_KEY_RE = re.compile(r"tgt_[A-Za-z0-9_\-]{6,}")


def _shape(v, depth=0):
    """ساختارِ JSON با یک نمونه‌ی کوتاه از هر لیست."""
    if isinstance(v, dict):
        return {k: _shape(x, depth + 1) for k, x in list(v.items())[:40]}
    if isinstance(v, list):
        return [_shape(v[0], depth + 1), f"... ({len(v)} مورد)"] if v else []
    if isinstance(v, str):
        return _KEY_RE.sub("tgt_***", v)[:80]
    return v


async def _find_bot(s, arg: str):
    """arg می‌تواند شمارهٔ داخلیِ ربات (bots.id)، آیدی عددیِ تلگرامِ ربات (بخشِ اولِ توکن) یا بخشی از یوزرنیم باشد."""
    arg = (arg or "").strip().lstrip("@")
    if arg.isdigit():
        n = int(arg)
        if n <= 2147483647:
            b = await s.get(Bot, n)
            if b:
                return [b]
        return list((await s.scalars(select(Bot).where(Bot.token.like(f"{n}:%")))).all())
    return list((await s.scalars(select(Bot).where(Bot.username.ilike(f"%{arg}%")))).all())


async def main(arg: str):
    async with Session() as s:
        bots = await _find_bot(s, arg)
        if len(bots) != 1:
            print("ربات‌های پیداشده:", [(b.id, b.username) for b in bots] or "هیچ")
            print("دقیق‌تر بنویس یا bot_id بده.")
            return
        b = bots[0]
        enc = await s.scalar(select(Setting.value).where(Setting.bot_id == b.id, Setting.key == "tgtools_api_key"))
    if not enc:
        print(f"روی ربات {b.id} (@{b.username}) کلیدِ TGTools ذخیره نشده.")
        return
    from wallet_crypto import decrypt_secret
    try:
        key = decrypt_secret(enc)
    except Exception as e:  # noqa: BLE001
        print(f"رمزگشاییِ کلید ممکن نشد ({type(e).__name__}).")
        return
    out = [f"ربات {b.id} @{b.username} — کلیدِ ذخیره‌شده: ✅ (نمایش داده نمی‌شود)\n"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as cs:
        for p in PATHS:
            try:
                async with cs.request("GET", BASE + p, headers={"X-Api-Key": key, "Accept": "application/json"}) as r:
                    txt = await r.text()
                    try:
                        body = json.dumps(_shape(json.loads(txt)), ensure_ascii=False, indent=1)
                    except ValueError:
                        body = _KEY_RE.sub("tgt_***", txt)[:300]
                    out.append(f"=== GET {p}\nHTTP {r.status}\n{body[:2500]}\n")
            except Exception as e:  # noqa: BLE001
                out.append(f"=== GET {p}\nخطا: {type(e).__name__}\n")
    text = "\n".join(out).replace(key, "tgt_***")
    print(text)
    with open("tgtools_samples.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print("\n✅ در tgtools_samples.txt هم ذخیره شد؛ همان فایل را بفرست.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
