"""عیب‌یابیِ lookupِ TGTools روی سرور:  python check_tgtools_user.py Ejei09 [tgt_API_KEY]
پاسخِ خامِ /api/purchase/user/<username> را با و بدونِ کلید چاپ می‌کند."""
import asyncio
import sys

from tenant.bots.virtual_service import tgtools_api as tg


async def main(username: str, key: str | None):
    for label, k in (("بدونِ کلید", None), ("با کلید", key)):
        if label == "با کلید" and not key:
            continue
        try:
            u = await tg.lookup_user(username, k)
            print(f"[{label}] {u!r}  → {tg.lookup_verdict(u)}")
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] خطا: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
