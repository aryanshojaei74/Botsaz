"""
tg_stall_test.py — فقط خواندنی (فقط getMe). آیا تماس‌های گیرکردهٔ تلگرام از «اتصالِ قدیمیِ keep-alive» می‌آیند؟
یک session پایدار با فاصله‌های بیکاریِ مختلف + شاهد (هر بار اتصالِ تازه). حدودِ ۱۰ دقیقه طول می‌کشد.

اجرا:  cd /opt/botsaz && source venv/bin/activate && python3 tg_stall_test.py
(کوتاه‌تر: GAPS=1,5,15,30 ROUNDS=1 python3 tg_stall_test.py)
"""
import asyncio, os, time, statistics
import aiohttp
from config import PLATFORM_BOT_TOKEN

GAPS = [float(x) for x in os.environ.get("GAPS", "1,5,10,14,16,20,30,45,60,90").split(",")]
ROUNDS = int(os.environ.get("ROUNDS", "2"))
BASE = os.environ.get("TG_API_BASE", "https://api.telegram.org")
URL = f"{BASE}/bot{PLATFORM_BOT_TOKEN}/getMe"
TMO = aiohttp.ClientTimeout(total=12)


async def call(cs):
    t = time.monotonic()
    try:
        async with cs.get(URL, timeout=TMO) as r:
            await r.read()
        return time.monotonic() - t, None
    except Exception as e:  # noqa: BLE001
        return time.monotonic() - t, type(e).__name__


async def main():
    print("=== A) session پایدار (مثلِ ربات): زمانِ تماس بعد از بیکاریِ N ثانیه ===")
    res = {g: [] for g in GAPS}
    async with aiohttp.ClientSession() as cs:
        await call(cs)
        for _ in range(ROUNDS):
            for g in GAPS:
                await asyncio.sleep(g)
                res[g].append(await call(cs))
    for g in GAPS:
        s = " ".join(f"{d:.2f}s" + (f"({e})" if e else "") for d, e in res[g])
        print(f"  بعد از {g:>5g}s بیکاری: {s}")
    print("\n=== B) شاهد: هر تماس با اتصالِ تازه (۱۵ بار) ===")
    ds = []
    for _ in range(15):
        async with aiohttp.ClientSession() as cs:
            ds.append(await call(cs))
        await asyncio.sleep(1)
    ok = sorted(d for d, e in ds if not e)
    print(f"  میانه={statistics.median(ok):.2f}s بیشینه={max(ok):.2f}s خطا={sum(1 for _, e in ds if e)}")
    bad = [(g, d, e) for g in GAPS for d, e in res[g] if e or d > 4]
    print("\nنتیجه:", "تماس‌های کند/گیرکرده در A: " + str(bad) if bad else "در A هیچ stallی دیده نشد")


if __name__ == "__main__":
    asyncio.run(main())
