#!/usr/bin/env python3
import asyncio
import httpx

BASE_URL = "https://zerofee.ok-exor.ir"
USERNAME = "Vsteam"
PASSWORD = "36nd85HNvm^8eKShi*"


async def main():
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        base = BASE_URL.rstrip("/")

        print("=" * 60)
        print("1) لاگین با POST /api/admin/token ...")
        try:
            r = await client.post(
                f"{base}/api/admin/token",
                data={"username": USERNAME, "password": PASSWORD},
            )
            print(f"   status: {r.status_code}")
            print(f"   body: {r.text[:500]}")
            r.raise_for_status()
            token = r.json().get("access_token")
            if not token:
                print("   ❌ توکن در پاسخ نبود.")
                return
            print("   ✅ لاگین موفق.")
        except Exception as e:
            print(f"   ❌ لاگین ناموفق: {e}")
            return

        headers = {"Authorization": f"Bearer {token}"}

        print("=" * 60)
        print("2) GET /api/admin (چک is_sudo) ...")
        try:
            r = await client.get(f"{base}/api/admin", headers=headers)
            print(f"   status: {r.status_code}")
            print(f"   body: {r.text[:500]}")
            if r.status_code == 200:
                d = r.json()
                print(f"   👉 username: {d.get('username')} | is_sudo: {d.get('is_sudo')}")
        except Exception as e:
            print(f"   ⚠️  {e}")

        print("=" * 60)
        print("3) GET /api/groups ...")
        try:
            r = await client.get(f"{base}/api/groups", headers=headers)
            print(f"   status: {r.status_code}")
            print(f"   body: {r.text[:400]}")
            groups_json = r.json() if r.status_code == 200 else {}
        except Exception as e:
            print(f"   ❌ {e}")
            groups_json = {}

        group_ids = []
        try:
            gs = groups_json.get("groups") or []
            if gs:
                group_ids = [gs[0]["id"]]
        except Exception:
            pass
        print(f"   → group_ids برای تست: {group_ids}")

        print("=" * 60)
        print("4) POST /api/user — تست ساخت کانفیگِ ۱ گیگ / ۱ روزه ...")
        test_username = "diagtest_1gb_1day"
        payload = {
            "username": test_username,
            "proxies": {"vless": {}},
            "group_ids": group_ids,
            "expire": 0,
            "data_limit": 1 * 1024 * 1024 * 1024,
            "data_limit_reset_strategy": "no_reset",
        }
        print(f"   payload: {payload}")
        try:
            r = await client.post(f"{base}/api/user", headers=headers, json=payload)
            print(f"   status: {r.status_code}")
            print(f"   body: {r.text[:1000]}")
            if r.status_code == 401:
                print("   ⚠️  401 — با مرحلهٔ ۲ (is_sudo) مقایسه کن.")
            elif r.status_code in (200, 201):
                print("   ✅ ساخت موفق! این یوزر تست رو از پنل دستی پاک کن.")
            else:
                print("   ⚠️  خطای دیگری غیر از 401.")
        except Exception as e:
            print(f"   ❌ {e}")

        print("=" * 60)
        print("تمام. کل این خروجی رو کپی کن و بفرست.")


if __name__ == "__main__":
    asyncio.run(main())
